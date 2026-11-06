import copy
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch

from jarvis.agent import Agent
from jarvis.autonomy import AutonomousController, InferenceGate, LocalPlanner
from jarvis.character_context import CharacterContext, control_intent


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        self.tools = copy.deepcopy(tools)
        return next(self.responses)


class Desktop:
    def __init__(self):
        self.snapshot = {'scene_session': 'one', 'character': {'current_action': 'none', 'control_revision': 0},
                         'objects': [], 'control': {'revision': 0, 'last_action_id': ''}}
        self.calls = []

    def call(self, name, args):
        if name == 'get_desktop_state':
            return copy.deepcopy(self.snapshot)
        self.calls.append((name, copy.deepcopy(args)))
        self.snapshot['control'] = {'revision': len(self.calls), 'last_action_id': args.get('request_id')}
        return {'state': 'succeeded', 'heading_degrees': 180, 'posture': 'sitting'}


def sit_plan(ctx):
    return {'decision': 'replace_pending', 'steps': [{'name': 'sit_down', 'arguments': {}}], 'reason': '歇一会'}


class SharedContextTests(unittest.TestCase):
    def test_chat_feedback_and_persona_are_visible_to_planning_and_action_results_return_to_chat(self):
        shared = CharacterContext(persona='统一人设：本鲸喜欢安静地陪伴。')
        chat = Model([{'role': 'assistant', 'content': '好，我换个地方。', 'thinking': 'PRIVATE_THOUGHT'},
                      {'role': 'assistant', 'content': '我已经坐下了。'}])
        agent = Agent(chat, Mock(schemas=[], desktop=None), context=shared)
        agent.reply('别一直盯着书，换个地方休息吧。')
        planner_model = Model([{'tool_calls': [{'function': {'name': 'propose_plan', 'arguments': sit_plan({})}}]}])
        planner = LocalPlanner(planner_model, InferenceGate(), context=shared)
        planner({'snapshot': Desktop().snapshot, 'recent': [], 'pending': [], 'executing': None})
        request = planner_model.requests[0]
        self.assertIn(shared.persona, request[0]['content'])
        self.assertIn(shared.persona, chat.requests[0][0]['content'])
        view = json.loads(request[1]['content'])['character_context']
        self.assertIn('现在执行本轮空闲自主规划', request[-1]['content'])
        self.assertNotIn('continue', planner_model.tools[0]['function']['parameters']['properties']['decision']['enum'])
        self.assertIn('换个地方休息', view['recent_dialogue'][0]['content'])
        self.assertEqual(view['last_user_request']['status'], 'responded')
        shared.record_plan(sit_plan({}))
        self.assertEqual(shared.shared()['current_intent']['status'], 'planned')
        shared.record_tool('sit_down', {}, {'state': 'succeeded', 'posture': 'sitting'}, 'autonomy')
        agent.reply('你现在在做什么？')
        self.assertIn('"posture": "sitting"', chat.requests[1][0]['content'])
        self.assertIn('"state": "succeeded"', chat.requests[1][0]['content'])
        self.assertNotIn('PRIVATE_THOUGHT', json.dumps(shared.shared(), ensure_ascii=False))

    def test_planner_view_is_bounded_excludes_images_and_clear_resets_both_paths(self):
        context = CharacterContext()
        context.set_history([{'role': 'user', 'content': 'hello', 'images': ['BASE64_PRIVATE'], 'thinking': 'PRIVATE'},
                             {'role': 'assistant', 'content': 'hi'}])
        for i in range(100):
            context.record_tool('observe', {'object_id': str(i)}, {'state': 'succeeded', 'extra': 'x' * 10000}, 'autonomy')
        view = context.shared()
        self.assertLess(len(json.dumps(view)), 15000)
        self.assertNotIn('BASE64_PRIVATE', json.dumps(view))
        self.assertNotIn('PRIVATE', json.dumps(view))
        Agent(None, None, context=context).clear()
        self.assertEqual(context.shared()['recent_dialogue'], [])
        self.assertEqual(context.shared()['recent_events'], [])

    def test_stop_commands_are_distinguished_from_questions_and_negations(self):
        for text in ('停下', '大肥鱼，停下！', '请先别动好吗？', '不要自己动，陪我聊天', 'pause auto',
                     '你能不能停下？', '可以别动了吗？', '请先停下来一下好吗？'):
            self.assertEqual(control_intent(text), 'pause', text)
        for text in ('不要停下', '为什么停下了？', '如果我说停下会怎样？', '停下是什么意思？', '今天自动模式怎么用？'):
            self.assertIsNone(control_intent(text), text)
        self.assertEqual(control_intent('关闭自动模式'), 'off')
        self.assertEqual(control_intent('继续活动'), 'on')

    def test_conversation_text_is_not_accepted_as_an_executable_plan(self):
        model = Model([{'content': '好，本鲸这就去歇一会。'}])
        planner = LocalPlanner(model, InferenceGate(), context=CharacterContext())
        with self.assertRaisesRegex(ValueError, 'received conversation text'):
            planner({'snapshot': Desktop().snapshot})


class IdleResumeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.desktop = Desktop()
        self.shared = CharacterContext()
        self.plans = []
        def planner(ctx):
            self.plans.append(ctx)
            return sit_plan(ctx)
        self.controller = AutonomousController(self.desktop, planner, context=self.shared,
                                               resume_delay=30, clock=lambda: self.now)
        self.addCleanup(self.controller.close)
        with patch.object(self.controller, 'start'):
            self.controller.enable()

    def test_no_resume_during_chat_and_thirty_seconds_count_from_reply_completion(self):
        self.controller.begin_chat()
        self.now = 100
        self.controller.tick()
        self.assertEqual(self.controller.mode, 'waiting')
        self.assertIsNone(self.controller.plan_future)
        self.controller.end_chat()
        self.now = 129
        self.controller.tick()
        self.assertIsNone(self.controller.plan_future)
        self.now = 130
        self.controller.tick()
        self.assertEqual(self.controller.mode, 'on')
        self.controller.plan_future.result(1)
        self.assertEqual(len(self.plans), 1)

    def test_new_message_restarts_quiet_timer(self):
        self.controller.begin_chat(); self.controller.end_chat()
        self.now = 20
        self.controller.begin_chat()
        self.now = 25
        self.controller.end_chat()
        self.now = 30
        self.controller.tick()
        self.assertEqual(self.controller.mode, 'waiting')
        self.now = 55
        self.controller.tick()
        self.assertEqual(self.controller.mode, 'on')

    def test_explicit_pause_off_and_stop_do_not_resume_after_chat(self):
        for off in (False, True):
            with patch.object(self.controller, 'start'):
                self.controller.enable()
            self.controller.pause(off=off)
            self.controller.begin_chat(); self.controller.end_chat()
            self.now += 1000
            self.controller.tick()
            self.assertEqual(self.controller.mode, 'off' if off else 'paused')
            self.assertIsNone(self.controller.resume_at)
        with patch.object(self.controller, 'start'):
            self.controller.enable()
        self.assertEqual(self.controller.request_stop()['state'], 'succeeded')
        self.controller.end_chat()
        self.assertFalse(self.controller.auto_enabled)
        self.assertEqual(self.desktop.calls[-1][0], 'cancel_move')

    def test_manual_control_during_quiet_time_prevents_auto_resume(self):
        self.controller.begin_chat(); self.controller.end_chat()
        self.controller.tick()
        self.desktop.snapshot['character']['control_revision'] = 1
        self.now = 10
        self.controller.tick()
        self.now = 100
        self.controller.tick()
        self.assertEqual(self.controller.mode, 'paused')

    def test_in_flight_plan_cannot_execute_after_chat_begins(self):
        released = threading.Event()
        self.controller.planner = lambda ctx: (released.wait(2), sit_plan(ctx))[1]
        self.controller.tick()
        future = self.controller.plan_future
        self.controller.begin_chat(); self.controller.end_chat()
        released.set(); future.result(1)
        self.controller.tick()
        self.assertFalse(self.desktop.calls)
        self.assertFalse(self.controller.pending)

    def test_cli_restores_idle_timer_even_when_chat_raises(self):
        from jarvis.__main__ import respond
        agent = Mock()
        agent.reply.side_effect = RuntimeError('model timeout')
        with self.assertRaises(RuntimeError):
            respond(agent, self.controller, '你好')
        self.assertFalse(self.controller.foreground_active)
        self.assertEqual(self.controller.resume_at, 30)


class StartupTests(unittest.TestCase):
    def test_named_stop_is_acknowledged_without_starting_another_model_loop(self):
        from jarvis.__main__ import interact
        agent, controller, store = Mock(), Mock(), Mock()
        store.due_reminders.return_value = []
        controller.request_stop.return_value = {'state': 'succeeded'}
        with patch('builtins.input', side_effect=['大肥鱼，停下！', '/quit']), \
                patch('sys.stdout', new_callable=io.StringIO):
            interact(agent, controller, store, 'owner', Mock())
        controller.request_stop.assert_called_once_with(off=False)
        agent.reply.assert_not_called()
        agent.note_exchange.assert_called_once()

    def test_default_auto_and_opt_out_share_one_context_instance(self):
        from jarvis.__main__ import main
        for flags, expected in (([], True), (['--no-autonomous'], False)):
            with patch('sys.argv', ['jarvis', *flags]), patch('sys.stdout', new_callable=io.StringIO), \
                 patch('jarvis.__main__.config.DB_PATH', ':memory:'), patch('jarvis.__main__.config.AUTO_ENABLED', True), \
                 patch('jarvis.__main__.Agent') as agent, patch('jarvis.desktop_client.DesktopMCPClient'), \
                 patch('jarvis.autonomy.AutonomousController') as factory, patch('jarvis.__main__.interact'), \
                 patch('jarvis.trace.configure'), patch('jarvis.trace.enabled', return_value=False):
                main()
                shared = agent.call_args.kwargs['context']
                self.assertIs(factory.call_args.kwargs['context'], shared)
                self.assertIs(factory.call_args.args[1].context, shared)
                self.assertEqual(factory.return_value.enable.called, expected)
