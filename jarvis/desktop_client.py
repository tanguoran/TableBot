"""Small multiplexed MCP stdio client used by the existing Ollama Agent."""
import atexit
import json
import os
from pathlib import Path
from queue import Queue, Empty
import subprocess
import sys
import threading
from .text_encoding import safe_json_dumps


class DesktopMCPClient:
    def __init__(self, bridge_url='http://127.0.0.1:18765', command=None):
        self.command = command or [sys.executable, '-m', 'jarvis.desktop_mcp', '--bridge-url', bridge_url]
        self.process = None
        self.start_lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending = {}
        self.sequence = 0
        self.tools = []
        atexit.register(self.close)

    def start(self):
        with self.start_lock:
            if self.process and self.process.poll() is None:
                return
            self.tools = []
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1,
                                            cwd=Path(__file__).resolve().parent.parent,
                                            env=dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1'))
            threading.Thread(target=self._read, args=(self.process,), daemon=True).start()
            response = self._request('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                                   'clientInfo': {'name': 'jarvis-local', 'version': '1.0.0'}}, 5)
            if response.get('protocolVersion') not in ('2025-11-25', '2025-06-18', '2025-03-26'):
                self.close()
                raise RuntimeError('Unsupported desktop MCP protocol version')
            self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            self.tools = self._request('tools/list', {}, 5)['tools']

    def _send(self, message):
        with self.write_lock:
            if not self.process or self.process.poll() is not None:
                raise RuntimeError('Desktop MCP process is not running')
            self.process.stdin.write(safe_json_dumps(message, allow_nan=False) + '\n')
            self.process.stdin.flush()

    def _read(self, process):
        try:
            for line in process.stdout:
                message = json.loads(line)
                with self.pending_lock:
                    pending = self.pending.get(message.get('id'))
                if pending:
                    pending.put(message)
        except (ValueError, OSError):
            pass
        finally:
            with self.pending_lock:
                if self.process is process:
                    for pending in self.pending.values():
                        pending.put({'error': {'message': 'Desktop MCP process disconnected'}})

    def _request(self, method, params, timeout):
        with self.pending_lock:
            self.sequence += 1
            request_id = self.sequence
            pending = Queue()
            self.pending[request_id] = pending
        try:
            self._send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
            try:
                response = pending.get(timeout=timeout)
            except Empty:
                self._send({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                            'params': {'requestId': request_id, 'reason': 'Client timeout'}})
                raise TimeoutError('Desktop MCP request timed out')
            if 'error' in response:
                raise RuntimeError(response['error']['message'])
            return response['result']
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def tool_schemas(self):
        self.start()
        return [{'type': 'function', 'function': {'name': tool['name'], 'description': tool['description'],
                                                 'parameters': tool['inputSchema']}} for tool in self.tools]

    def call(self, name, arguments):
        try:
            self.start()
            response = self._request('tools/call', {'name': name, 'arguments': arguments}, 70)
            result = response.get('structuredContent')
            if result is None:
                result = json.loads(next(c['text'] for c in response['content'] if c['type'] == 'text'))
            if response.get('isError') and 'error' not in result:
                result['error'] = 'Desktop action did not complete successfully'
            return result
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            return {'error': str(error)}

    def close(self):
        process = self.process
        if not process:
            return
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        finally:
            if process.stdout:
                process.stdout.close()
            self.process = None
