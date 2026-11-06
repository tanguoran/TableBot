"""MCP stdio server for the whale desktop (JSON-RPC, tools capability).

Only stdout protocol messages; the Unity HTTP bridge is a separate internal transport.
The bridge is hosted in this process if no standalone bridge is already running.
"""
import argparse
import json
import sys
import threading
from .text_encoding import safe_json_dumps
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from .desktop_bridge import BRIDGE_VERSION, SERVICE, make_server
from .desktop_schema import DESKTOP_TOOLS, LEGACY_TOOLS, CANCELLABLE_NAMES, validate_arguments

VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')


class BridgeClient:
    def __init__(self, url='http://127.0.0.1:18765'):
        parsed = urlparse(url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path not in ('', '/') or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Bridge URL must be a loopback http origin')
        self.url = url.rstrip('/')
        self.port = parsed.port or 80
        self.server = None

    def ensure_running(self):
        try:
            with urlopen(self.url + '/health', timeout=1) as response:
                health = json.load(response)
                if health.get('service') != SERVICE:
                    raise ValueError('Port is occupied by another service')
                if health.get('version') != BRIDGE_VERSION:
                    raise ValueError('Restart the old desktop bridge to load the new action protocol')
                return
        except (URLError, TimeoutError):
            self.server = make_server(self.port)
            threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def call(self, name, arguments):
        arguments = validate_arguments(name, arguments)
        request = Request(self.url + '/tools/' + name,
                          data=safe_json_dumps(arguments, allow_nan=False).encode('utf-8'),
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=65) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                return json.load(error)
            except (ValueError, OSError):
                return {'error': f'Desktop bridge HTTP {error.code}'}
        except (URLError, TimeoutError, OSError) as error:
            return {'error': 'Desktop bridge unavailable: ' + str(error)}

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class StdioMCPServer:
    def __init__(self, bridge, output=None):
        self.bridge = bridge
        self.output = output or sys.stdout
        self.write_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending = {}
        self.initialized = False
        self.ready = False
        self.pool = ThreadPoolExecutor(max_workers=8)

    def send(self, message):
        with self.write_lock:
            self.output.write(safe_json_dumps(message, allow_nan=False) + '\n')
            self.output.flush()

    def result(self, request_id, result):
        self.send({'jsonrpc': '2.0', 'id': request_id, 'result': result})

    def error(self, request_id, code, message):
        self.send({'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}})

    def handle(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            return self.error(None, -32600, 'Invalid JSON-RPC request')
        method = message['method']
        request_id = message.get('id')
        params = message.get('params', {})
        if 'id' not in message:
            if method == 'notifications/initialized' and self.initialized:
                self.ready = True
            elif method == 'notifications/cancelled' and isinstance(params, dict):
                with self.pending_lock:
                    pending = self.pending.get(params.get('requestId')) in CANCELLABLE_NAMES
                if pending:
                    threading.Thread(target=self.bridge.call, args=('cancel_move', {}), daemon=True).start()
            return
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool) or not isinstance(params, dict):
            return self.error(None, -32600, 'Invalid request id or params')
        if method == 'initialize':
            if self.initialized:
                return self.error(request_id, -32600, 'Already initialized')
            self.initialized = True
            requested = params.get('protocolVersion')
            return self.result(request_id, {
                'protocolVersion': requested if requested in VERSIONS else VERSIONS[0],
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': 'whale-desktop', 'version': '1.0.0'},
                'instructions': 'Inspect live state before moving. character is separate from filtered objects. '
                                'Only state=arrived confirms arrival. Tools control this Unity desktop only.'})
        if method == 'ping':
            return self.result(request_id, {})
        if not self.ready:
            return self.error(request_id, -32000, 'Complete initialize and notifications/initialized first')
        if method == 'tools/list':
            if params.get('cursor'):
                return self.error(request_id, -32602, 'No pagination cursor is available')
            return self.result(request_id, {'tools': DESKTOP_TOOLS})
        if method == 'tools/call':
            name = params.get('name')
            arguments = params.get('arguments', {})
            if not any(t['name'] == name for t in DESKTOP_TOOLS + LEGACY_TOOLS):
                return self.error(request_id, -32602, 'Unknown tool')
            with self.pending_lock:
                if len(self.pending) >= 8 or request_id in self.pending:
                    return self.error(request_id, -32000, 'Request already pending or server busy')
                self.pending[request_id] = name
            self.pool.submit(self.call_tool, request_id, name, arguments)
            return
        self.error(request_id, -32601, 'Method not found')

    def call_tool(self, request_id, name, arguments):
        try:
            try:
                result = self.bridge.call(name, arguments)
            except (ValueError, TypeError) as error:
                result = {'error': str(error)}
            failed = 'error' in result or result.get('state') in ('failed', 'cancelled')
            self.result(request_id, {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
                                     'structuredContent': result, 'isError': failed})
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def run(self, source=None):
        source = source or sys.stdin
        try:
            while True:
                line = source.readline(1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 1024 * 1024:
                    self.error(None, -32700, 'Message too large')
                    break
                try:
                    self.handle(json.loads(line))
                except (ValueError, TypeError):
                    self.error(None, -32700, 'Parse error')
        finally:
            with self.pending_lock:
                pending = any(name in CANCELLABLE_NAMES or name == 'look_at_camera' for name in self.pending.values())
            if pending:
                self.bridge.call('cancel_move', {})
            self.pool.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bridge-url', default='http://127.0.0.1:18765')
    args = parser.parse_args()
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    bridge = BridgeClient(args.bridge_url)
    try:
        bridge.ensure_running()
        StdioMCPServer(bridge).run()
    finally:
        bridge.close()


if __name__ == '__main__':
    main()
