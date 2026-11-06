import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from jarvis import config, trace
from jarvis.agent import Agent
from jarvis.autonomy import InferenceGate, LocalPlanner
from jarvis.deepseek import DeepSeekClient, api_messages
from jarvis.embeddings import EmbeddingClient
from jarvis.knowledge import KnowledgeService
from jarvis.llm import GenerationLimitError, OllamaClient, create_chat_client
from jarvis.storage import Store
from jarvis.tools import Tools


def reply(content='你好', *, calls=None, reasoning='', finish='stop'):
    message = {'role': 'assistant', 'content': content, 'reasoning_content': reasoning}
    if calls:
        message['tool_calls'] = calls
    return {'choices': [{'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 32, 'completion_tokens': 12}}


def tool(name='search_knowledge', arguments='{"query":"照明"}', call_id='call_kb'):
    return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


def response(value):
    return io.BytesIO(json.dumps(value, ensure_ascii=False).encode('utf-8'))


def stream(chunks, done=True):
    data = b': keepalive\n\n' + b''.join(
        b'data: ' + json.dumps(chunk, ensure_ascii=False).encode('utf-8') + b'\n\n' for chunk in chunks)
    return io.BytesIO(data + (b'data: [DONE]\n\n' if done else b''))


class DeepSeekTests(unittest.TestCase):
    def setUp(self):
        settings = patch.multiple(config, DEEPSEEK_API_KEY='test-secret', DEEPSEEK_MODEL='deepseek-flash',
            DEEPSEEK_URL='https://api.deepseek.com', DEEPSEEK_THINK=False, DEEPSEEK_TIMEOUT=120,
            DEEPSEEK_MAX_TOKENS=8192, DEEPSEEK_MAX_RETRY_TOKENS=16384, TRACE_STREAM=True)
        settings.start()
        self.addCleanup(settings.stop)
        recorder = patch.object(trace, '_recorder', False)
        recorder.start()
        self.addCleanup(recorder.stop)

    def test_provider_switch_is_shared_by_chat_and_planning_and_embeddings_stay_local(self):
        with patch.object(config, 'CHAT_PROVIDER', 'deepseek'):
            self.assertIsInstance(create_chat_client(), DeepSeekClient)
            self.assertEqual(create_chat_client(profile='autonomy').profile, 'autonomy')
            self.assertEqual(config.chat_model(), 'deepseek-flash')
            self.assertEqual(EmbeddingClient().model, config.EMBED_MODEL)
            self.assertEqual(EmbeddingClient().url, config.EMBED_URL)
        with patch.object(config, 'CHAT_PROVIDER', 'ollama'):
            self.assertIsInstance(create_chat_client(), OllamaClient)
            self.assertEqual(config.chat_model(), config.MODEL)
        with patch.object(config, 'CHAT_PROVIDER', 'typo'):
            with self.assertRaisesRegex(ValueError, 'JARVIS_CHAT_PROVIDER'):
                create_chat_client()

    def test_missing_key_fails_before_http_with_switch_back_instructions(self):
        with patch.object(config, 'DEEPSEEK_API_KEY', ''), patch('jarvis.deepseek.urlopen') as request:
            with self.assertRaisesRegex(ValueError, 'DEEPSEEK_API_KEY.*ollama'):
                create = DeepSeekClient()
        request.assert_not_called()

    def test_request_uses_deepseek_settings_and_normalizes_unicode(self):
        with patch('jarvis.deepseek.urlopen', return_value=response(reply())) as request:
            client = DeepSeekClient()
            self.assertEqual(client.chat([{'role': 'user', 'content': '中文🐋\udce4'}], [])['content'], '你好')
        req = request.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, 'https://api.deepseek.com/chat/completions')
        self.assertEqual(req.get_header('Authorization'), 'Bearer test-secret')
        self.assertEqual(payload['model'], 'deepseek-flash')
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        self.assertEqual(payload['messages'][0]['content'], '中文🐋\\udce4')
        self.assertNotIn('tools', payload)
        self.assertNotIn('options', payload)
        self.assertEqual(request.call_args.kwargs['timeout'], 120)
        self.assertEqual(client.last_response_info['eval_count'], 12)

    def test_protocol_serializes_arguments_matches_parallel_ids_and_preserves_reasoning(self):
        messages = [{'role': 'assistant', 'content': '', 'reasoning_content': 'protocol reasoning',
                     'tool_calls': [tool(call_id='call_1', arguments={'query': '照明'}),
                                    tool(call_id='call_2', arguments={'query': '戏曲'})]},
                    {'role': 'tool', 'tool_name': 'search_knowledge', 'tool_call_id': 'call_2', 'content': 'two'},
                    {'role': 'tool', 'tool_name': 'search_knowledge', 'tool_call_id': 'call_1', 'content': 'one'}]
        original = copy.deepcopy(messages)
        converted = api_messages(messages)
        self.assertEqual(converted[0]['reasoning_content'], 'protocol reasoning')
        self.assertEqual(json.loads(converted[0]['tool_calls'][0]['function']['arguments']), {'query': '照明'})
        self.assertEqual([m['tool_call_id'] for m in converted[1:]], ['call_2', 'call_1'])
        self.assertNotIn('tool_name', converted[1])
        self.assertEqual(messages, original)
        with self.assertRaisesRegex(ValueError, 'tool_call_id'):
            api_messages([{'role': 'tool', 'content': '{}', 'tool_call_id': 'unknown'}])

    def test_images_are_translated_to_chat_completions_content_parts(self):
        converted = api_messages([{'role': 'user', 'content': '看图', 'images': ['iVBORw==', '/9j/AA==']}])
        self.assertEqual(converted[0]['content'][1]['image_url']['url'], 'data:image/png;base64,iVBORw==')
        self.assertEqual(converted[0]['content'][2]['image_url']['url'], 'data:image/jpeg;base64,/9j/AA==')

    def test_stream_joins_fragmented_arguments_and_records_thinking_without_execution(self):
        chunks = [
            {'choices': [{'delta': {'reasoning_content': '检查'}, 'finish_reason': None}]},
            {'choices': [{'delta': {'reasoning_content': '资料', 'tool_calls': [
                {'index': 0, **tool(arguments='{"query":')}]}, 'finish_reason': None}]},
            {'choices': [{'delta': {'tool_calls': [
                {'index': 0, 'function': {'arguments': '"照明"}'}},
                {'index': 1, **tool('list_reminders', '{}', 'call_reminders')}]}, 'finish_reason': None}]},
            {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}],
             'usage': {'prompt_tokens': 4, 'completion_tokens': 8}}]
        with tempfile.TemporaryDirectory() as directory:
            recorder = trace.Recorder(directory)
            with patch.object(trace, '_recorder', recorder), patch.object(config, 'DEEPSEEK_THINK', True), \
                    patch('jarvis.deepseek.urlopen', return_value=stream(chunks)):
                message = DeepSeekClient().chat([{'role': 'user', 'content': '查资料'}], [])
            events = [json.loads(line) for line in recorder.path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(message['reasoning_content'], '检查资料')
        self.assertEqual(message['tool_calls'][0]['function']['arguments'], {'query': '照明'})
        self.assertEqual(message['tool_calls'][1]['id'], 'call_reminders')
        self.assertEqual(''.join(e['data']['text'] for e in events if e['event'] == 'model.thinking'), '检查资料')
        self.assertFalse(any(e['event'] == 'tool.start' for e in events))
        self.assertNotIn('test-secret', json.dumps(events))

    def test_incomplete_stream_cannot_promote_partial_tools(self):
        chunks = [{'choices': [{'delta': {'tool_calls': [{'index': 0, **tool()}]}, 'finish_reason': None}]}]
        with patch.object(trace, 'enabled', return_value=True), \
                patch('jarvis.deepseek.urlopen', return_value=stream(chunks, done=False)):
            with self.assertRaisesRegex(RuntimeError, 'before completion'):
                DeepSeekClient().chat([], [])

    def test_length_retry_discards_incomplete_tools_and_reuses_inputs(self):
        with patch('jarvis.deepseek.urlopen', side_effect=[
                response(reply('', calls=[tool(arguments='{')], finish='length')),
                response(reply('完整答案'))]) as request:
            answer = DeepSeekClient().chat([{'role': 'user', 'content': '问题'}], [])
        payloads = [json.loads(c.args[0].data) for c in request.call_args_list]
        self.assertEqual([p['max_tokens'] for p in payloads], [8192, 16384])
        self.assertEqual(payloads[0]['messages'], payloads[1]['messages'])
        self.assertNotIn('tool_calls', answer)
        self.assertEqual(answer['content'], '完整答案')

    def test_autonomous_plan_uses_bounded_non_thinking_profile(self):
        plan = {'decision': 'wait', 'wait_seconds': 20, 'reason': '休息'}
        with patch.object(config, 'DEEPSEEK_THINK', True), patch('jarvis.deepseek.urlopen',
                return_value=response(reply('', calls=[tool('propose_plan', json.dumps(plan))],
                                           finish='tool_calls'))) as request:
            planner = LocalPlanner(DeepSeekClient(profile='autonomy'), InferenceGate())
            self.assertEqual(planner({'snapshot': {}, 'executing': None, 'pending': []}), plan)
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        self.assertEqual(payload['max_tokens'], 768)
        self.assertFalse(payload['stream'])
        self.assertEqual(request.call_args.kwargs['timeout'], 20)
        self.assertEqual(payload['tools'][0]['function']['name'], 'propose_plan')

    def test_autonomous_length_is_not_retried(self):
        with patch('jarvis.deepseek.urlopen', return_value=response(reply(finish='length'))) as request:
            with self.assertRaises(GenerationLimitError):
                DeepSeekClient(profile='autonomy').chat([], [])
        self.assertEqual(request.call_count, 1)

    def test_invalid_response_and_tool_json_never_return_executable_calls(self):
        for data in ({}, reply(calls=[tool(arguments='{')], finish='tool_calls'),
                     reply(calls=[tool(arguments='[]')], finish='tool_calls'),
                     reply(calls=[tool()], finish='content_filter')):
            with self.subTest(data=data), patch('jarvis.deepseek.urlopen', return_value=response(data)):
                with self.assertRaises(RuntimeError):
                    DeepSeekClient().chat([], [])

    def test_auth_error_is_actionable_redacts_key_and_is_not_retried(self):
        error = HTTPError('https://api.deepseek.com/chat/completions', 401, 'Unauthorized', {},
                          io.BytesIO(b'invalid test-secret'))
        with patch('jarvis.deepseek.urlopen', side_effect=error) as request:
            with self.assertRaisesRegex(RuntimeError, 'DEEPSEEK_API_KEY') as raised:
                DeepSeekClient().chat([], [])
        self.assertNotIn('test-secret', str(raised.exception))
        self.assertEqual(request.call_count, 1)

    def test_rag_tool_round_trip_reuses_index_and_retains_protocol_reasoning_only_for_chat(self):
        class Embedder:
            model = 'test-local-embedding'
            document_calls = 0
            def embed_documents(self, texts):
                self.document_calls += 1
                return [[1, 0] for _ in texts]
            def embed_query(self, query): return [1, 0]
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '照明.md').write_text('# 照明\n夜间阅读使用暖黄色台灯。\n', encoding='utf-8')
            store = Store(':memory:')
            self.addCleanup(store.conn.close)
            embedder = Embedder()
            knowledge = KnowledgeService(store, embedder, directory)
            self.assertEqual(knowledge.sync()['indexed_files'], 1)
            tools = Tools(store, knowledge=knowledge)
            client = DeepSeekClient()
            agent = Agent(client, tools)
            with patch.object(config, 'DEEPSEEK_THINK', True), patch('jarvis.deepseek.urlopen', side_effect=[
                    response(reply('', reasoning='protocol lookup', calls=[tool()], finish='tool_calls')),
                    response(reply('使用暖黄色台灯。照明.md:L1-L2', reasoning='protocol answer')),
                    response(reply('已经查过照明资料。', reasoning='protocol next turn'))]) as request:
                answer = agent.reply('根据知识库，夜间照明用什么？')
                self.assertIn('暖黄色', answer)
                second = json.loads(request.call_args_list[1].args[0].data)
                self.assertEqual(second['messages'][-1]['tool_call_id'], 'call_kb')
                self.assertEqual(second['messages'][-2]['reasoning_content'], 'protocol lookup')
                self.assertIn('暖黄色', second['messages'][-1]['content'])
                agent.reply('刚才查了什么？')
                third = json.loads(request.call_args_list[2].args[0].data)
                self.assertTrue(any(m.get('reasoning_content') == 'protocol answer' for m in third['messages']))
            self.assertNotIn('protocol lookup', json.dumps(agent.context.shared()))
            self.assertNotIn('protocol answer', json.dumps(agent.context.shared()))
            self.assertEqual(embedder.document_calls, 1)
            with patch('jarvis.llm.urlopen', return_value=response({'message': {'content': '本地模式可继续回答。'}})) as request:
                agent.llm = OllamaClient()
                self.assertEqual(agent.reply('继续'), '本地模式可继续回答。')
                local_payload = json.loads(request.call_args.args[0].data)
                self.assertNotIn('reasoning_content', json.dumps(local_payload))
            self.assertEqual(knowledge.sync()['indexed_files'], 0)


class DoctorProviderTests(unittest.TestCase):
    def test_deepseek_check_requires_only_local_embedding_not_local_chat(self):
        from jarvis.doctor import check
        with patch.multiple(config, CHAT_PROVIDER='deepseek', DEEPSEEK_API_KEY='test-secret',
                DEEPSEEK_MODEL='deepseek-flash', EMBED_MODEL='embed:latest'), \
                patch('jarvis.doctor.urlopen', side_effect=[response({'data': [{'id': 'deepseek-flash'}]}),
                                                          response({'models': [{'name': 'embed:latest'}]})]) as request, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(check(), 0)
        self.assertEqual(request.call_count, 2)
        self.assertNotIn('ollama pull qwen', output.getvalue())
        self.assertNotIn('test-secret', output.getvalue())


if __name__ == '__main__':
    unittest.main()
