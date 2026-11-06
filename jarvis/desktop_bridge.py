"""Loopback broker: Unity exchanges snapshots/events; MCP waits on actual outcomes.

Run independently with python -m jarvis.desktop_bridge, or let desktop_mcp host it.
No model, file execution, or generic Unity reflection is exposed here.
"""
import argparse
import copy
import json
import math
import threading
import time
import uuid
from collections import deque, OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .desktop_schema import validate_arguments
from .text_encoding import safe_json_dumps

TERMINAL = {'arrived', 'failed', 'cancelled', 'succeeded'}
SERVICE = 'whale-desktop-bridge'
BRIDGE_VERSION = 4


class DesktopBroker:
    def __init__(self, stale_after=3.0):
        self.condition = threading.Condition()
        self.snapshot = None
        self.updated_at = 0.0
        self.stale_after = stale_after
        self.session = None
        self.retired_sessions = set()
        self.actions = OrderedDict()
        self.queue = deque()
        self.active_move = None
        self.control_revision = 0
        self.last_action_id = ''

    def _finish(self, action, state, error='', **fields):
        action['state'] = state
        action['result'] = {'action_id': action['action_id'], 'state': state, **fields}
        if error:
            action['result']['error'] = error
        if self.active_move == action['action_id']:
            self.active_move = None
        self.condition.notify_all()

    def _fresh(self):
        if self.snapshot is None or time.monotonic() - self.updated_at > self.stale_after:
            raise ValueError('Unity desktop is offline or paused. Open WhaleDesk and enter Play mode.')

    def exchange(self, payload):
        snapshot = payload.get('snapshot')
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('character'), dict) or not isinstance(snapshot.get('objects'), list):
            raise ValueError('Expected a dedicated character state and objects array')
        session = snapshot.get('scene_session')
        if not isinstance(session, str) or not 1 <= len(session) <= 100:
            raise ValueError('Invalid scene_session')
        with self.condition:
            if session in self.retired_sessions:
                raise ValueError('Retired Unity session')
            if self.session != session:
                if self.session:
                    self.retired_sessions.add(self.session)
                for action in self.actions.values():
                    if action['state'] not in TERMINAL:
                        self._finish(action, 'failed', 'Unity scene session changed')
                self.queue.clear()
                self.session = session
            self.snapshot = copy.deepcopy(snapshot)
            self.updated_at = time.monotonic()
            for event in payload.get('events', []):
                action = self.actions.get(event.get('action_id'))
                if not action or action['scene_session'] != session or action['state'] in TERMINAL:
                    continue
                state = event.get('state')
                if state in TERMINAL:
                    self._finish(action, state, event.get('error', ''),
                                 position=event.get('position'), destination=event.get('destination'),
                                 heading_degrees=event.get('heading_degrees'), posture=event.get('posture'),
                                 gaze=event.get('gaze'), requested_destination=event.get('requested_destination'),
                                 object_state=event.get('object_state'),
                                 destination_adjusted=event.get('destination_adjusted', False))
                elif state == 'running':
                    action['state'] = state
            commands = []
            while self.queue:
                command = self.queue.popleft()
                action = self.actions.get(command['action_id'])
                if action and action['state'] not in TERMINAL:
                    commands.append(command)
            return {'commands': commands, 'server_time': time.time()}

    def state(self, arguments):
        with self.condition:
            self._fresh()
            result = copy.deepcopy(self.snapshot)
            position = result['character']['position']
            objects = result['objects']
            if arguments.get('types'):
                objects = [o for o in objects if o.get('object_type') in arguments['types']]
            if arguments.get('tags'):
                objects = [o for o in objects if set(arguments['tags']).issubset(o.get('tags', []))]
            if 'max_distance' in arguments:
                limit = arguments['max_distance']
                objects = [o for o in objects if math.hypot(o['position']['x'] - position['x'], o['position']['z'] - position['z']) <= limit]
            result['objects'] = objects
            result['snapshot_age_seconds'] = round(time.monotonic() - self.updated_at, 3)
            result['control'] = {'revision': self.control_revision, 'last_action_id': self.last_action_id}
            return result

    def call(self, name, arguments, cancelled=None):
        args = validate_arguments(name, arguments)
        if name == 'get_desktop_state':
            return self.state(args)
        with self.condition:
            self._fresh()
            if name == 'approach_object':
                obj = next((o for o in self.snapshot['objects'] if o['object_id'] == args['object_id']), None)
                if obj is None or 'approach_object' not in obj.get('capabilities', []):
                    raise ValueError('Object is not exposed or does not support approach_object')
            if name == 'interact_object':
                obj = next((o for o in self.snapshot['objects'] if o['object_id'] == args['object_id']), None)
                operations = [] if obj is None else [a for a in obj.get('interactions', [])
                    if a.get('operation') == args['operation'] and a.get('available') is True]
                if len(operations) != 1 or 'interact_object' not in obj.get('capabilities', []):
                    raise ValueError('Object operation is not exposed or available')
            if name in ('observe', 'look_at') and args.get('object_id'):
                if not any(o['object_id'] == args['object_id'] for o in self.snapshot['objects']):
                    raise ValueError('Observed object is not exposed')
            action_id = args.pop('request_id', None) or uuid.uuid4().hex
            fingerprint = json.dumps([name, args], sort_keys=True, allow_nan=False)
            action = self.actions.get(action_id)
            if action:
                if action['fingerprint'] != fingerprint or action['scene_session'] != self.session:
                    raise ValueError('request_id was already used for a different action/session')
            else:
                expression = name == 'set_expression'
                if name != 'cancel_move' and not expression and self.active_move:
                    raise ValueError('Character is busy. cancel_move first, or wait for the active action.')
                while len(self.actions) >= 256:
                    key = next((k for k, v in self.actions.items() if v['state'] in TERMINAL), None)
                    if key is None:
                        raise ValueError('Too many pending actions')
                    del self.actions[key]
                timeout = args.get('timeout_seconds', 5 if expression or name == 'cancel_move' else 30)
                action = {'action_id': action_id, 'fingerprint': fingerprint, 'scene_session': self.session,
                          'expression': expression, 'state': 'queued', 'deadline': time.monotonic() + timeout}
                self.actions[action_id] = action
                if not expression:
                    self.control_revision += 1
                    self.last_action_id = action_id
                command = {'action_id': action_id, 'scene_session': self.session, 'name': name,
                           'x': args.get('x', 0), 'y': args.get('y', 0), 'z': args.get('z', 0),
                           'target_type': args.get('target_type', 'camera' if name == 'look_at_camera' else ''),
                           'turn_body': args.get('turn_body', True),
                           'object_id': args.get('object_id', ''), 'side': args.get('side', 'nearest'),
                           'operation': args.get('operation', ''),
                           'has_heading': 'heading_degrees' in args,
                           'heading_degrees': args.get('heading_degrees', 0),
                           'emotion': args.get('emotion', ''), 'intensity': args.get('intensity', .7),
                           'duration_seconds': args.get('duration_seconds', 3 if expression else 5),
                           'height': args.get('height', .12),
                           'expires_at': time.time() + timeout}
                if name == 'cancel_move':
                    # A queued movement must not start after its cancellation.
                    for pending in self.actions.values():
                        if pending is not action and not pending.get('expression') and pending['state'] == 'queued':
                            self._finish(pending, 'cancelled', 'Cancelled before execution')
                    self.queue.appendleft(command)
                else:
                    if not expression:
                        self.active_move = action_id
                    self.queue.append(command)
            while action['state'] not in TERMINAL:
                if cancelled and cancelled.is_set():
                    if not action.get('expression'): self._queue_stop()
                    self._finish(action, 'cancelled', 'MCP request cancelled')
                    break
                if time.monotonic() > action['deadline']:
                    if not action.get('expression'): self._queue_stop()
                    self._finish(action, 'failed', 'Action timed out')
                    break
                if time.monotonic() - self.updated_at > self.stale_after:
                    if not action.get('expression'): self._queue_stop()
                    self._finish(action, 'failed', 'Unity disconnected')
                    break
                self.condition.wait(.1)
            return copy.deepcopy(action['result'])

    def _queue_stop(self):
        # Fire-and-forget safety stop; Unity also enforces the original action deadline.
        stop_id = uuid.uuid4().hex
        self.actions[stop_id] = {'action_id': stop_id, 'scene_session': self.session,
                                 'state': 'queued', 'fingerprint': 'internal-stop',
                                 'deadline': time.monotonic() + 5}
        self.queue.appendleft({'action_id': stop_id, 'scene_session': self.session,
                              'name': 'cancel_move', 'expires_at': time.time() + 5})


def make_server(port=18765, broker=None):
    broker = broker or DesktopBroker()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, data, status=200):
            body = safe_json_dumps(data, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            host = self.headers.get('Host', '')
            return host in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'} and not self.headers.get('Origin')

        def do_GET(self):
            if not self.allowed():
                return self.reply({'error': 'Loopback native clients only'}, 403)
            if self.path == '/health':
                return self.reply({'service': SERVICE, 'version': BRIDGE_VERSION})
            self.reply({'error': 'Unknown endpoint'}, 404)

        def do_POST(self):
            if not self.allowed():
                return self.reply({'error': 'Loopback native clients only'}, 403)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 * 1024:
                    raise ValueError('Request body must be 1..1048576 bytes')
                payload = json.loads(self.rfile.read(size), parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
                if not isinstance(payload, dict):
                    raise ValueError('Expected an object')
                if self.path == '/unity/exchange':
                    result = broker.exchange(payload)
                elif self.path.startswith('/tools/'):
                    result = broker.call(self.path.removeprefix('/tools/'), payload)
                else:
                    return self.reply({'error': 'Unknown endpoint'}, 404)
                self.reply(result)
            except (ValueError, KeyError, TypeError) as error:
                self.reply({'error': str(error)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.broker = broker
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18765)
    args = parser.parse_args()
    server = make_server(args.port)
    print(f'{SERVICE} listening at http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
