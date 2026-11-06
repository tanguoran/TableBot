import copy
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from unittest.mock import Mock

from jarvis.autonomy import AutonomousController, InferenceGate, validate_plan
from jarvis.desktop_schema import validate_arguments


def state():
    return {'scene_session': 'scene1', 'character': {'current_action': 'none', 'action_id': '',
            'movement_state': 'idle', 'control_revision': 0},
            'control': {'revision': 0, 'last_action_id': ''},
            'objects': [{'object_id': 'book', 'capabilities': ['observe', 'approach_object']}]}


def plan(name='sit_down', **args):
    return {'decision': 'replace_pending', 'steps': [{'name': name, 'arguments': args}]}


class FakeDesktop:
    def __init__(self):
        self.snapshot = state()
        self.calls = []
        self.started = threading.Event()
        self.finish = threading.Event()

    def call(self, name, arguments):
        if name == 'get_desktop_state':
            return copy.deepcopy(self.snapshot)
        self.calls.append((name, arguments))
        self.snapshot['control']['revision'] += 1
        self.snapshot['control']['last_action_id'] = arguments['request_id']
        if name == 'cancel_move':
            self.finish.set()
            return {'state': 'succeeded'}
        self.started.set()
        self.finish.wait(2)
        return {'state': 'succeeded'}


class AutonomyTests(unittest.TestCase):
    def test_general_gaze_plan_supports_camera_object_and_world_coordinates(self):
        live = state()
        live['objects'][0]['capabilities'].append('look_at')
        for args in ({'target_type': 'camera'}, {'target_type': 'object', 'object_id': 'book'},
                     {'target_type': 'position', 'x': 0, 'y': 1.2, 'z': -.4, 'turn_body': False}):
            self.assertEqual(validate_plan(plan('look_at', **args), live)['steps'][0]['arguments'], args)
        with self.assertRaises(ValueError):
            validate_plan(plan('look_at', target_type='object', object_id='missing'), live)

    def setUp(self):
        self.desktop = FakeDesktop()
        self.now = 0
        self.controller = AutonomousController(self.desktop, lambda ctx: plan(), clock=lambda: self.now)
        with patch.object(self.controller, 'start'):
            self.controller.enable()

    def tearDown(self):
        self.desktop.finish.set()
        self.controller.close()

    def decide(self):
        self.controller.tick()
        self.controller.plan_future.result(timeout=1)
        self.controller.tick()

    def test_pending_can_change_without_interrupting_active_action(self):
        self.decide()
        self.assertTrue(self.desktop.started.wait(1))
        first = self.controller.current
        self.now = 10
        self.controller.planner = lambda ctx: plan('observe', object_id='book', duration_seconds=2)
        self.controller.tick()
        self.controller.plan_future.result(timeout=1)
        self.controller.tick()
        self.assertEqual(self.controller.current, first)
        self.assertEqual([s['name'] for s in self.controller.pending], ['observe'])
        self.assertEqual([c[0] for c in self.desktop.calls], ['sit_down'])
        self.desktop.finish.set()
        self.controller.action_future.result(1)
        self.controller.tick()
        self.controller.action_future.result(1)
        self.assertEqual([c[0] for c in self.desktop.calls], ['sit_down', 'observe'])

    def test_expression_is_dispatched_separately_from_plan_steps(self):
        self.desktop.snapshot['available_actions'] = ['set_expression', 'sit_down']
        self.controller.planner = lambda ctx: dict(plan(), expression={'emotion': 'relaxed'})
        self.decide()
        self.desktop.finish.set()
        self.controller.expression_future.result(1)
        self.controller.action_future.result(1)
        self.assertEqual(self.controller.current['name'], 'sit_down')
        self.assertCountEqual([c[0] for c in self.desktop.calls], ['set_expression', 'sit_down'])
        with self.assertRaises(ValueError):
            validate_plan({'decision': 'wait', 'expression': {'emotion': 'surprised'}}, self.desktop.snapshot)

    def test_user_pause_discards_in_flight_plan_even_after_resume(self):
        released = threading.Event()
        self.controller.planner = lambda ctx: (released.wait(2), plan())[1]
        self.controller.tick()
        self.controller.pause()
        with patch.object(self.controller, 'start'):
            self.controller.enable()
        released.set()
        self.controller.plan_future.result(1)
        self.controller.tick()
        self.assertIsNone(self.controller.current)
        self.assertFalse(self.desktop.calls)

    def test_foreign_command_or_manual_input_or_scene_change_pauses(self):
        for change in ('foreign', 'manual', 'scene'):
            with self.subTest(change=change):
                with patch.object(self.controller, 'start'):
                    self.controller.enable()
                self.controller.next_decision = 999
                self.controller.tick()
                if change == 'foreign':
                    self.desktop.snapshot['control'] = {'revision': 22, 'last_action_id': 'user-action'}
                elif change == 'manual':
                    self.desktop.snapshot['character']['control_revision'] += 1
                else:
                    self.desktop.snapshot['scene_session'] = 'scene2'
                self.controller.tick()
                self.assertEqual(self.controller.mode, 'paused')
                self.assertFalse(self.controller.pending)

    def test_one_planner_and_minimum_ten_second_interval(self):
        calls = []
        released = threading.Event()
        def planner(ctx):
            calls.append(ctx)
            released.wait(2)
            return {'decision': 'wait', 'wait_seconds': 20}
        self.controller.planner = planner
        self.controller.tick()
        first = self.controller.plan_future
        for _ in range(5):
            self.now += 1
            self.controller.tick()
            self.assertIs(self.controller.plan_future, first)
        released.set()
        first.result(1)
        self.controller.tick()
        self.now = 24
        self.controller.tick()
        self.assertEqual(len(calls), 1)
        self.assertIsNone(self.controller.plan_future)

    def test_pause_stops_only_own_execution_and_clears_followups(self):
        self.decide()
        self.assertTrue(self.desktop.started.wait(1))
        self.controller.pending.append({'name': 'jump', 'arguments': {}})
        self.controller.pause('用户输入')
        self.assertIsNone(self.controller.action_future)
        self.assertIsNone(self.controller.current)
        self.assertFalse(self.controller.pending)
        self.assertEqual(self.desktop.calls[-1][0], 'cancel_move')

    def test_failed_action_invalidates_a_plan_generated_while_it_was_running(self):
        self.decide()
        self.assertTrue(self.desktop.started.wait(1))
        released = threading.Event()
        self.controller.planner = lambda ctx: (released.wait(2), plan('jump'))[1]
        self.now = 10
        self.controller.tick()
        future = self.controller.plan_future
        # Simulate the real executor returning a navigation failure.
        self.desktop.finish.set()
        self.controller.action_future.result(1)
        from concurrent.futures import Future
        failed = Future()
        failed.set_result({'state': 'failed', 'error': 'unreachable'})
        self.controller.action_future = failed
        released.set()
        future.result(1)
        self.controller.tick()
        self.assertIsNone(self.controller.current)
        self.assertFalse(self.controller.pending)
        self.assertEqual([c[0] for c in self.desktop.calls], ['sit_down'])

    def test_plan_and_heading_validation(self):
        validate_arguments('move_to', {'x': 0, 'z': 0, 'heading_degrees': -90})
        for heading in (float('nan'), float('inf'), True, 361):
            with self.assertRaises(ValueError):
                validate_arguments('move_to', {'x': 0, 'z': 0, 'heading_degrees': heading})
        invalid = [plan('cancel_move'), plan('observe', object_id='hidden'),
                   plan('wait', seconds=float('nan')), plan('jump', height=99),
                   plan('move_to', x=0, z=0, request_id='model-owned'),
                   {'decision': 'replace_pending', 'steps': plan()['steps'] * 3}]
        for proposal in invalid:
            with self.assertRaises(ValueError):
                validate_plan(proposal, state())


class GateTests(unittest.TestCase):
    def test_cli_user_input_pauses_autonomy_before_calling_chat(self):
        import io
        from jarvis.__main__ import interact
        events = []
        agent, controller, store = Mock(), Mock(), Mock()
        agent.reply.side_effect = lambda prompt: events.append('chat') or 'ok'
        controller.enable.side_effect = lambda: events.append('on')
        controller.pause.side_effect = lambda *a, **k: events.append('off' if k.get('off') else 'pause')
        controller.begin_chat.side_effect = lambda: events.append('begin_chat')
        controller.end_chat.side_effect = lambda: events.append('end_chat')
        store.due_reminders.return_value = []
        with patch('builtins.input', side_effect=['/auto on', '你好', '/auto off', '/quit']), \
                patch('sys.stdout', new_callable=io.StringIO):
            interact(agent, controller, store, 'owner', Mock())
        self.assertEqual(events, ['on', 'begin_chat', 'chat', 'end_chat', 'off'])

    def test_background_yields_while_foreground_waits_and_no_overlap(self):
        gate = InferenceGate()
        entered = threading.Event()
        def foreground():
            with gate.acquire(foreground=True):
                entered.set()
        with ThreadPoolExecutor(1) as pool:
            with gate.acquire() as acquired:
                self.assertTrue(acquired)
                future = pool.submit(foreground)
                deadline = time.monotonic() + 1
                while not gate.foreground_waiters and time.monotonic() < deadline:
                    time.sleep(.001)
                with gate.acquire() as second:
                    self.assertFalse(second)
                self.assertFalse(entered.is_set())
            future.result(1)
        self.assertTrue(entered.is_set())
