import copy
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from jarvis import trace
from jarvis.agent import Agent
from jarvis.autonomy import AutonomousController
from jarvis.llm import GenerationLimitError, OllamaClient
from jarvis.monitor import Follower, Renderer


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.recorder = trace.Recorder(self.temp.name)
        mocked = patch.object(trace, '_recorder', self.recorder)
        mocked.start()
        self.addCleanup(mocked.stop)

    def events(self):
        return [json.loads(line) for line in self.recorder.path.read_text(encoding='utf-8').splitlines()]

    def test_concurrent_events_are_complete_and_context_does_not_leak(self):
        def worker(number):
            with trace.scope(source='worker', turn_id=str(number)):
                for index in range(20):
                    trace.emit('test', number=number, index=index, text='大肥鱼')
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(worker, range(4)))
        trace.emit('outside')
        events = self.events()
        self.assertEqual(len(events), 81)
        self.assertEqual([r['seq'] for r in events], list(range(1, 82)))
        for r in events[:-1]:
            self.assertEqual(r['context']['turn_id'], str(r['data']['number']))
        self.assertEqual(events[-1]['context'], {})

    def test_streaming_accumulates_thinking_text_and_multiple_tools_without_execution(self):
        chunks = [
            {'message': {'thinking': '先看看'}},
            {'message': {'thinking': '桌面。', 'content': '好'}},
            {'message': {'content': '的。', 'tool_calls': [{'function': {'name': 'sit_down', 'arguments': {}}}]}},
            {'message': {'tool_calls': [{'function': {'name': 'observe', 'arguments': {'duration_seconds': 3}}}]}},
            {'done': True, 'done_reason': 'stop', 'message': {}, 'eval_count': 12}]
        stream = io.BytesIO(b''.join(json.dumps(c, ensure_ascii=False).encode() + b'\n' for c in chunks))
        with patch('jarvis.llm.urlopen', return_value=stream) as request:
            result = OllamaClient().chat([{'role': 'user', 'content': '动一动'}], [])
        self.assertTrue(json.loads(request.call_args.args[0].data)['stream'])
        self.assertEqual(result['thinking'], '先看看桌面。')
        self.assertEqual(result['content'], '好的。')
        self.assertEqual([c['function']['name'] for c in result['tool_calls']], ['sit_down', 'observe'])
        events = self.events()
        self.assertEqual(''.join(e['data']['text'] for e in events if e['event'] == 'model.thinking'), result['thinking'])
        self.assertFalse(any(e['event'] == 'tool.start' for e in events))

    def test_incomplete_stream_never_returns_partial_tool_proposals(self):
        chunk = {'message': {'tool_calls': [{'function': {'name': 'jump', 'arguments': {}}}]}, 'done': False}
        with patch('jarvis.llm.urlopen', return_value=io.BytesIO(json.dumps(chunk).encode() + b'\n')):
            with self.assertRaisesRegex(RuntimeError, 'before completion'):
                OllamaClient().chat([], [])
        self.assertEqual(self.events()[-1]['event'], 'model.error')
        self.assertFalse(any(e['event'] == 'tool.start' for e in self.events()))

    def test_length_retry_preserves_both_attempts_but_only_returns_completed_response(self):
        responses = [
            io.BytesIO(b'{"message":{"thinking":"first","tool_calls":[{"function":{"name":"jump","arguments":{}}}]},"done":true,"done_reason":"length"}\n'),
            io.BytesIO(b'{"message":{"thinking":"second","content":"ok"},"done":true,"done_reason":"stop"}\n')]
        with patch('jarvis.llm.urlopen', side_effect=responses):
            result = OllamaClient().chat([], [])
        self.assertEqual(result['content'], 'ok')
        self.assertNotIn('tool_calls', result)
        discarded = next(e for e in self.events() if e['event'] == 'model.discarded')
        self.assertFalse(discarded['data']['tool_calls_executed'])
        self.assertEqual([e['context']['attempt'] for e in self.events() if e['event'] == 'model.thinking'], [1, 2])

    def test_agent_trace_explains_a_tool_loop_and_keeps_thinking_out_of_history(self):
        class Tools:
            schemas = []
            def run(self, name, args): return {'state': 'succeeded'}
        class Model:
            def chat(self, messages, schemas):
                return {'role': 'assistant', 'thinking': 'LOCAL_MODEL_THOUGHT', 'content': '',
                        'tool_calls': [{'function': {'name': 'observe', 'arguments': {}}}]}
        agent = Agent(Model(), Tools(), max_steps=2)
        agent.reply('你干点别的事行吗？')
        events = self.events()
        self.assertEqual([e['event'] for e in events].count('tool.start'), 2)
        limit = next(e for e in events if e['event'] == 'agent.limit')
        self.assertEqual(limit['data']['rounds'], 2)
        self.assertEqual(limit['data']['tool_count'], 2)
        self.assertNotIn('LOCAL_MODEL_THOUGHT', json.dumps(agent.history))
        self.assertEqual(events[-1]['event'], 'chat.answer')

    def test_autonomy_records_the_proposal_and_why_it_was_rejected(self):
        class Desktop:
            def call(self, name, args):
                return {'scene_session': 'scene', 'character': {'current_action': 'none'}, 'objects': []}
        controller = AutonomousController(Desktop(), lambda ctx: {'decision': 'continue'})
        self.addCleanup(controller.close)
        with patch.object(controller, 'start'):
            controller.enable()
        controller.tick()
        controller.plan_future.result(1)
        controller.tick()
        events = self.events()
        proposed = next(e for e in events if e['event'] == 'plan.proposed')
        rejected = next(e for e in events if e['event'] == 'plan.rejected')
        self.assertEqual(proposed['context']['plan_id'], rejected['context']['plan_id'])
        self.assertIn('No existing', rejected['data']['error'])

    def test_logging_failure_does_not_prevent_a_tool_from_running(self):
        with patch.object(self.recorder, 'write', side_effect=OSError('disk full')), patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(trace.call('stand_up', {}, lambda: {'state': 'succeeded'}), {'state': 'succeeded'})


class FollowerTests(unittest.TestCase):
    def test_tail_follow_handles_new_files_partial_utf8_and_large_partial_records(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / 'one.jsonl'
            def record(seq):
                return json.dumps({'time': str(seq), 'seq': seq, 'event': 'test', 'data': {'text': '鲸'}},
                                  ensure_ascii=False).encode() + b'\n'
            first.write_bytes(record(1) + record(2))
            follower = Follower(directory)
            try:
                self.assertEqual([e['seq'] for e in follower.initial(1)], [2])
                third = record(3)
                split = third.index('鲸'.encode()) + 1
                with first.open('ab') as output: output.write(third[:split])
                self.assertEqual(follower.poll(), [])
                with first.open('ab') as output: output.write(third[split:])
                self.assertEqual([e['seq'] for e in follower.poll()], [3])
                (Path(directory) / 'two.jsonl').write_bytes(record(4))
                self.assertEqual([e['seq'] for e in follower.poll()], [4])
            finally:
                follower.close()
            first.write_bytes(b'{"event":"big","data":{"text":"' + b'x' * 20000)
            follower = Follower(directory)
            try:
                follower.initial(0)
                with first.open('ab') as output: output.write(b'"}}\n')
                event = follower.poll()[0]
                self.assertEqual(len(event['data']['text']), 20000)
            finally:
                follower.close()

    def test_renderer_keeps_all_thought_text_and_distinguishes_proposal_from_execution(self):
        output = io.StringIO()
        renderer = Renderer(output)
        base = {'time': '2026-11-03T00:00:00+00:00', 'pid': 1, 'context': {'source': 'chat', 'model_call_id': 'abc'}}
        for event, data in [('model.thinking', {'text': '第一段'}), ('model.thinking', {'text': '第二段'}),
                            ('model.tool_calls', {'tool_calls': [{'function': {'name': 'jump'}}]}),
                            ('tool.result', {'name': 'jump', 'result': {'state': 'failed', 'error': 'busy'}})]:
            renderer.write(dict(base, event=event, data=data))
        renderer.finish()
        text = output.getvalue()
        self.assertIn('第一段第二段', text)
        self.assertIn('尚未执行', text)
        self.assertIn('工具实际结果', text)
        self.assertIn('busy', text)
