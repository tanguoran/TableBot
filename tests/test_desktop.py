import copy
import io
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from jarvis.desktop_bridge import DesktopBroker, make_server
from jarvis.desktop_client import DesktopMCPClient
from jarvis.desktop_mcp import StdioMCPServer
from jarvis.desktop_schema import DESKTOP_TOOLS, validate_arguments
from jarvis.agent import Agent
from jarvis.storage import Store
from jarvis.tools import Tools


def snapshot(session='test-scene'):
    return {'scene_session': session, 'scene_name': 'WhaleDesk',
            'character': {'character_id': 'whale_girl', 'position': {'x': 0, 'y': .76, 'z': 0},
                          'movement_state': 'idle', 'animation_state': 'idle'},
            'objects': [
                {'object_id': 'book_01', 'object_type': 'book', 'tags': ['reading'],
                 'position': {'x': 1, 'z': 0}, 'capabilities': ['approach_object']},
                {'object_id': 'laptop_01', 'object_type': 'computer', 'tags': ['work'],
                 'position': {'x': 2, 'z': 0}, 'capabilities': ['approach_object']}]}


class DesktopBrokerTests(unittest.TestCase):
    def setUp(self):
        self.broker = DesktopBroker()
        self.broker.exchange({'snapshot': snapshot()})
        self.pool = ThreadPoolExecutor(4)

    def tearDown(self):
        self.pool.shutdown(wait=True)

    def wait_queued(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self.broker.condition:
                if self.broker.queue:
                    return
            time.sleep(.005)
        self.fail('Command not queued')

    def exchange(self, events=(), session='test-scene'):
        return self.broker.exchange({'snapshot': snapshot(session), 'events': list(events)})['commands']

    def test_filters_never_remove_dedicated_character(self):
        result = self.broker.call('get_desktop_state', {'types': ['book'], 'tags': ['reading'], 'max_distance': 1.1})
        self.assertEqual(result['character']['character_id'], 'whale_girl')
        self.assertEqual([o['object_id'] for o in result['objects']], ['book_01'])
        self.assertEqual(self.broker.call('get_desktop_state', {'types': ['missing']})['objects'], [])
        self.assertEqual(len(self.broker.state({})['objects']), 2)

    def test_interaction_rechecks_capabilities_and_returns_real_state(self):
        live = snapshot()
        lamp = live['objects'][0]
        lamp.update(capabilities=['interact_object'], properties=[], state=[], interactions=[
            {'operation': 'turn_off', 'available': True, 'allow_autonomous': True}])
        self.broker.exchange({'snapshot': live})
        for object_id, operation in [('missing', 'turn_off'), ('book_01', 'invented')]:
            with self.assertRaises(ValueError):
                self.broker.call('interact_object', {'object_id': object_id, 'operation': operation})
        args = {'object_id': 'book_01', 'operation': 'turn_off', 'request_id': 'lamp-off'}
        result = self.pool.submit(self.broker.call, 'interact_object', args)
        self.wait_queued()
        commands = self.broker.exchange({'snapshot': live})['commands']
        self.assertEqual(commands[0]['operation'], 'turn_off')
        lamp['state'] = [{'key': 'is_on', 'type': 'boolean', 'boolean': False}]
        self.broker.exchange({'snapshot': live, 'events': [
            {'action_id': 'lamp-off', 'state': 'succeeded', 'object_state': lamp}]})
        self.assertEqual(result.result(1)['object_state']['state'], lamp['state'])
        self.assertEqual(self.broker.call('interact_object', args), result.result())
        self.assertFalse(self.broker.queue)

    def test_autonomy_only_uses_allowed_real_operations(self):
        from jarvis.autonomy import validate_plan
        live = snapshot()
        live['available_actions'] = ['interact_object']
        item = live['objects'][0]
        item.update(capabilities=['interact_object'], interactions=[
            {'operation': 'turn_on', 'available': True, 'allow_autonomous': True},
            {'operation': 'open_external', 'available': True, 'allow_autonomous': False}])
        plan = {'decision': 'replace_pending', 'steps': [{'name': 'interact_object',
            'arguments': {'object_id': 'book_01', 'operation': 'turn_on'}}]}
        validate_plan(plan, live)
        for operation in ['open_external', 'invented']:
            plan['steps'][0]['arguments']['operation'] = operation
            with self.assertRaises(ValueError): validate_plan(plan, live)

    def test_expression_can_finish_while_movement_keeps_ownership(self):
        walking = self.pool.submit(self.broker.call, 'move_to', {'x': .5, 'z': 0, 'request_id': 'walk'})
        self.wait_queued()
        self.exchange()
        control = self.broker.state({})['control']
        try:
            args = {'emotion': 'happy', 'duration_seconds': 3, 'request_id': 'face'}
            face = self.pool.submit(self.broker.call, 'set_expression', args)
            self.wait_queued()
            command = self.exchange()[0]
            self.assertEqual((command['emotion'], command['intensity']), ('happy', .7))
            self.exchange([{'action_id': 'face', 'state': 'succeeded'}])
            self.assertEqual(face.result(1), self.broker.call('set_expression', args))
            self.assertEqual(self.broker.active_move, 'walk')
            self.assertEqual(self.broker.state({})['control'], control)
            self.assertFalse(walking.done())
        finally:
            self.exchange([{'action_id': 'walk', 'state': 'arrived'}])
        self.assertEqual(walking.result(1)['state'], 'arrived')

    def test_file_index_and_visible_file_metadata_survive_mcp_state_filtering(self):
        live = snapshot()
        metadata = {'file_id': 'file_pdf', 'name': '报告.pdf', 'kind': 'document', 'extension': '.pdf',
                    'is_directory': False, 'visible': True, 'available_locally': True, 'size_bytes': 1200}
        live['objects'].append({'object_id': 'file_pdf', 'display_name': '报告.pdf', 'object_type': 'file',
                                'tags': ['desktop_file', 'document', 'pdf'], 'position': {'x': 0, 'z': 0},
                                'capabilities': ['approach_object', 'observe'], 'file': metadata})
        live['desktop_files'] = {'revision': 3, 'total': 2, 'page': 1, 'page_count': 2,
                                 'entries': [metadata, dict(metadata, file_id='file_hidden', name='下一页.pdf', visible=False)]}
        self.broker.exchange({'snapshot': live})
        state = self.broker.call('get_desktop_state', {'types': ['file'], 'tags': ['desktop_file']})
        self.assertEqual([o['object_id'] for o in state['objects']], ['file_pdf'])
        self.assertEqual(state['objects'][0]['file']['file_id'], 'file_pdf')
        self.assertEqual(state['desktop_files']['total'], 2)
        self.assertFalse(state['desktop_files']['entries'][1]['visible'])
        self.assertEqual(state['character']['character_id'], 'whale_girl')
        with self.assertRaisesRegex(ValueError, 'not exposed'):
            self.broker.call('approach_object', {'object_id': 'file_hidden'})

    def test_coordinates_are_finite_and_schema_is_strict(self):
        for args in ({'x': float('nan'), 'z': 0}, {'x': True, 'z': 0}, {'x': 0},
                     {'x': 0, 'z': 0, 'shell': 'noop'}, {'x': 0, 'z': 0, 'timeout_seconds': 99}):
            with self.assertRaises(ValueError):
                self.broker.call('move_to', args)

    def test_waits_for_arrival_not_merely_command_acceptance(self):
        future = self.pool.submit(self.broker.call, 'move_to', {'x': .5, 'z': -.3, 'request_id': 'move-1'})
        self.wait_queued()
        command = self.exchange()[0]
        self.assertEqual(command['x'], .5)
        self.exchange([{'action_id': 'move-1', 'state': 'running'}])
        self.assertFalse(future.done())
        self.exchange([{'action_id': 'move-1', 'state': 'arrived', 'position': {'x': .49, 'z': -.3}}])
        self.assertEqual(future.result(1)['state'], 'arrived')
        self.assertEqual(future.result()['position']['x'], .49)

    def test_unity_projected_destination_is_returned_without_changing_the_requested_command(self):
        future = self.pool.submit(self.broker.call, 'move_to', {'x': 100, 'z': 100, 'request_id': 'outside'})
        self.wait_queued()
        command = self.exchange()[0]
        self.assertEqual((command['x'], command['z']), (100, 100))
        requested = {'x': 100, 'y': .76, 'z': 100}
        actual = {'x': 1.4, 'y': .76, 'z': .5}
        self.exchange([{'action_id': 'outside', 'state': 'arrived', 'position': actual,
                        'destination': actual, 'requested_destination': requested, 'destination_adjusted': True}])
        result = future.result(1)
        self.assertEqual(result['requested_destination'], requested)
        self.assertEqual(result['destination'], actual)
        self.assertTrue(result['destination_adjusted'])

    def test_retries_are_idempotent_and_parameter_conflicts_fail(self):
        args = {'x': .5, 'z': 0, 'request_id': 'same-id'}
        first = self.pool.submit(self.broker.call, 'move_to', args)
        self.wait_queued()
        second = self.pool.submit(self.broker.call, 'move_to', args)
        self.assertEqual(len(self.exchange()), 1)
        self.exchange([{'action_id': 'same-id', 'state': 'arrived'}])
        self.assertEqual(first.result(1), second.result(1))
        self.assertEqual(self.broker.call('move_to', args)['state'], 'arrived')
        self.assertEqual(self.exchange(), [])
        with self.assertRaises(ValueError):
            self.broker.call('move_to', dict(args, x=.8))

    def test_heading_and_posture_actions_round_trip_through_broker(self):
        for name, args in [('move_to', {'x': .5, 'z': 0, 'heading_degrees': 90}),
                           ('sit_down', {'heading_degrees': 180}),
                           ('observe', {'object_id': 'book_01', 'duration_seconds': 2}),
                           ('look_at_camera', {'duration_seconds': 2}),
                           ('look_at', {'target_type': 'position', 'x': .2, 'y': 1.1, 'z': -.6,
                                        'turn_body': False, 'duration_seconds': 2}),
                           ('stand_up', {}), ('jump', {'height': .1})]:
            action_id = 'check-' + name
            future = self.pool.submit(self.broker.call, name, dict(args, request_id=action_id))
            self.wait_queued()
            command = self.exchange()[0]
            self.assertEqual(command['has_heading'], 'heading_degrees' in args)
            self.assertEqual(command['heading_degrees'], args.get('heading_degrees', 0))
            if name == 'look_at':
                self.assertEqual(command['target_type'], 'position')
                self.assertEqual(command['y'], 1.1)
                self.assertIs(command['turn_body'], False)
            self.exchange([{'action_id': action_id, 'state': 'succeeded', 'heading_degrees': 180, 'posture': 'sitting'}])
            result = future.result(1)
            self.assertEqual(result['heading_degrees'], 180)
            self.assertEqual(result['posture'], 'sitting')
            self.assertEqual(self.broker.state({})['control']['last_action_id'], action_id)

    def test_camera_gaze_state_is_dedicated_and_action_arguments_are_bounded(self):
        live = snapshot()
        live['character']['gaze'] = {'mode': 'camera', 'tracking': True, 'head_yaw_degrees': 12}
        self.broker.exchange({'snapshot': live})
        state = self.broker.call('get_desktop_state', {'types': ['missing']})
        self.assertEqual(state['character']['gaze']['mode'], 'camera')
        self.assertEqual(state['objects'], [])
        for args in ({'duration_seconds': 0}, {'duration_seconds': 21}, {'object_id': 'fake_camera'},
                     {'heading_degrees': 180}, {'duration_seconds': float('nan')}):
            with self.assertRaises(ValueError):
                validate_arguments('look_at_camera', args)

    def test_general_look_at_requires_one_unambiguous_target_and_strict_boolean(self):
        good = [{'target_type': 'camera'}, {'target_type': 'object', 'object_id': 'book_01'},
                {'target_type': 'position', 'x': 0, 'y': 1.1, 'z': -.6, 'turn_body': False}]
        for args in good:
            self.assertEqual(validate_arguments('look_at', args), args)
        bad = [{}, {'target_type': 'unknown'}, {'target_type': 'object'},
               {'target_type': 'camera', 'object_id': 'book_01'},
               {'target_type': 'camera', 'x': 0, 'y': 1, 'z': 0},
               {'target_type': 'object', 'object_id': 'book_01', 'x': 1},
               {'target_type': 'position', 'x': 0, 'z': 1},
               {'target_type': 'position', 'x': 0, 'y': float('inf'), 'z': 1},
               {'target_type': 'position', 'x': 0, 'y': 1, 'z': 1, 'object_id': 'book_01'},
               {'target_type': 'camera', 'turn_body': 0}, {'target_type': 'camera', 'turn_body': 'false'}]
        for args in bad:
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_arguments('look_at', args)
        self.assertIn('look_at', [t['name'] for t in DESKTOP_TOOLS])
        self.assertNotIn('look_at_camera', [t['name'] for t in DESKTOP_TOOLS])

    def test_look_at_object_validates_visibility_and_preserves_completion_gaze(self):
        with self.assertRaisesRegex(ValueError, 'not exposed'):
            self.broker.call('look_at', {'target_type': 'object', 'object_id': 'hidden_file'})
        future = self.pool.submit(self.broker.call, 'look_at', {'target_type': 'object', 'object_id': 'book_01'})
        self.wait_queued()
        command = self.exchange()[0]
        self.assertEqual(command['object_id'], 'book_01')
        self.assertIs(command['turn_body'], True)
        gaze = {'mode': 'object', 'target_object_id': 'book_01', 'tracking': True, 'limited': False}
        self.exchange([{'action_id': command['action_id'], 'state': 'succeeded', 'gaze': gaze}])
        self.assertEqual(future.result(1)['gaze'], gaze)

    def test_busy_does_not_replace_existing_action_and_cancel_bypasses_it(self):
        moving = self.pool.submit(self.broker.call, 'move_to', {'x': .5, 'z': 0, 'request_id': 'move'})
        self.wait_queued()
        self.exchange([{'action_id': 'move', 'state': 'running'}])
        with self.assertRaisesRegex(ValueError, 'busy'):
            self.broker.call('move_to', {'x': -.5, 'z': 0})
        cancelling = self.pool.submit(self.broker.call, 'cancel_move', {'request_id': 'stop'})
        self.wait_queued()
        self.assertEqual(self.exchange()[0]['name'], 'cancel_move')
        self.exchange([{'action_id': 'move', 'state': 'cancelled'}, {'action_id': 'stop', 'state': 'succeeded'}])
        self.assertEqual(moving.result(1)['state'], 'cancelled')
        self.assertEqual(cancelling.result(1)['state'], 'succeeded')

    def test_scene_restart_fails_old_action_and_rejects_old_session(self):
        future = self.pool.submit(self.broker.call, 'move_to', {'x': 1, 'z': 0})
        self.wait_queued()
        self.assertEqual(self.exchange(session='new-scene'), [])
        self.assertIn('session changed', future.result(1)['error'])
        with self.assertRaisesRegex(ValueError, 'Retired'):
            self.exchange(session='test-scene')

    def test_stale_state_is_not_presented_as_live(self):
        self.broker.updated_at -= 10
        with self.assertRaisesRegex(ValueError, 'offline'):
            self.broker.call('get_desktop_state', {})

    def test_unknown_object_is_rejected_before_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'not exposed'):
            self.broker.call('approach_object', {'object_id': 'invented'})
        self.assertEqual(self.exchange(), [])

    def test_timeout_stops_action_and_never_replays_expired_move(self):
        result = self.broker.call('move_to', {'x': .2, 'z': 0, 'timeout_seconds': 1})
        self.assertEqual(result['state'], 'failed')
        self.assertIn('timed out', result['error'])
        self.assertEqual([c['name'] for c in self.exchange()], ['cancel_move'])


class MCPTests(unittest.TestCase):
    def test_empty_summary_reports_success_after_a_rejected_desktop_attempt(self):
        agent = Agent(None, None)
        answer = agent._empty_answer([
            ('jump', {'error': 'height is out of range'}),
            ('jump', {'state': 'succeeded', 'heading_degrees': 180, 'posture': 'standing'})])
        self.assertIn('succeeded', answer)
        self.assertIn('height is out of range', answer)
        self.assertIn('180', answer)

    def test_protocol_lifecycle_and_unknown_methods(self):
        output = io.StringIO()
        server = StdioMCPServer(None, output)
        server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}})
        server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        server.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'})
        server.handle({'jsonrpc': '2.0', 'id': 4, 'method': 'not-a-method'})
        server.pool.shutdown()
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertIn('error', messages[0])
        self.assertEqual(messages[1]['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(len(messages[2]['result']['tools']), len(DESKTOP_TOOLS))
        self.assertEqual(messages[3]['error']['code'], -32601)

    def test_real_stdio_client_server_and_http_bridge(self):
        http = make_server(0)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        http.broker.exchange({'snapshot': snapshot()})
        client = DesktopMCPClient(f'http://127.0.0.1:{http.server_port}')
        try:
            self.assertEqual(len(client.tool_schemas()), len(DESKTOP_TOOLS))
            result = client.call('get_desktop_state', {'types': ['book']})
            self.assertEqual(result['character']['character_id'], 'whale_girl')
            self.assertEqual(len(result['objects']), 1)
            self.assertIn('error', client.call('move_to', {'x': 'bad', 'z': 0}))
            self.assertEqual(client.call('get_desktop_state', {})['scene_session'], 'test-scene')
            request = Request(f'http://127.0.0.1:{http.server_port}/tools/get_desktop_state', data=b'{}',
                              headers={'Origin': 'https://example.com'})
            with self.assertRaises(HTTPError) as caught:
                urlopen(request)
            self.assertEqual(caught.exception.code, 403)
        finally:
            client.close()
            http.shutdown()
            http.server_close()

    def test_agent_uses_mcp_tools_and_refreshes_state_without_polluting_history(self):
        class Desktop:
            count = 0
            def tool_schemas(self):
                return [{'type': 'function', 'function': {'name': t['name'], 'description': t['description'], 'parameters': t['inputSchema']}} for t in DESKTOP_TOOLS]
            def call(self, name, args):
                if name == 'get_desktop_state':
                    self.count += 1
                    return dict(snapshot(), version=self.count)
                return {'state': 'arrived'}

        class Model:
            requests = []
            def chat(self, messages, tools):
                self.requests.append(copy.deepcopy(messages))
                if len(self.requests) == 1:
                    self.tools = tools
                    return {'role': 'assistant', 'content': '', 'tool_calls': [
                        {'function': {'name': 'move_to', 'arguments': {'x': .3, 'z': 0}}}]}
                return {'role': 'assistant', 'content': '已到达。'}

        store = Store(':memory:')
        try:
            model = Model()
            agent = Agent(model, Tools(store, desktop=Desktop()))
            self.assertEqual(agent.reply('往右走一点'), '已到达。')
            self.assertIn('move_to', [t['function']['name'] for t in model.tools])
            self.assertNotEqual(model.requests[0][0]['content'], model.requests[1][0]['content'])
            self.assertNotIn('scene_session', json.dumps(agent.history))
            self.assertEqual(agent.history[-2]['role'], 'tool')
        finally:
            store.conn.close()


if __name__ == '__main__':
    unittest.main()
