import io
import json
import unittest
from unittest.mock import patch

from jarvis.llm import GenerationLimitError, OllamaClient


class OllamaClientTests(unittest.TestCase):
    def setUp(self):
        # Existing fixtures are complete, non-streaming HTTP responses.
        tracing = patch('jarvis.llm.trace._recorder', False)
        tracing.start()
        self.addCleanup(tracing.stop)
        settings = patch.multiple('jarvis.llm.config', THINK=True, NUM_CTX=32768,
                                  NUM_PREDICT=8192, MAX_NUM_PREDICT=16384, CHAT_TIMEOUT=300)
        settings.start()
        self.addCleanup(settings.stop)

    def test_explicit_context_and_thinking_generation(self):
        response = {'message': {'role': 'assistant', 'content': '你好'},
                    'done_reason': 'stop', 'prompt_eval_count': 30, 'eval_count': 5}
        with patch('jarvis.llm.urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as mocked:
            client = OllamaClient()
            self.assertEqual(client.chat([{'role': 'user', 'content': '你好'}], [])['content'], '你好')
        payload = json.loads(mocked.call_args.args[0].data)
        self.assertIs(payload['think'], True)
        self.assertEqual(payload['options']['num_ctx'], 32768)
        self.assertEqual(payload['options']['num_predict'], 8192)
        self.assertEqual(mocked.call_args.kwargs['timeout'], 300)
        self.assertEqual(client.last_response_info['done_reason'], 'stop')

    def test_invalid_chat_response_is_reported_clearly(self):
        with patch('jarvis.llm.urlopen', return_value=io.BytesIO(b'{"done":true}')):
            with self.assertRaisesRegex(RuntimeError, 'invalid chat response'):
                OllamaClient().chat([], [])

    def test_autonomy_has_bounded_independent_profile_and_no_truncation_retry(self):
        response = {'message': {'content': ''}, 'done_reason': 'length'}
        with patch('jarvis.llm.urlopen', side_effect=lambda *a, **k: io.BytesIO(json.dumps(response).encode())) as mocked:
            with self.assertRaises(GenerationLimitError):
                OllamaClient(profile='autonomy').chat([], [])
        payload = json.loads(mocked.call_args.args[0].data)
        self.assertFalse(payload['think'])
        self.assertEqual(payload['options']['num_predict'], 768)
        self.assertEqual(mocked.call_args.kwargs['timeout'], 20)
        self.assertEqual(mocked.call_count, 1)

    def test_truncation_metadata_is_retained_without_promoting_thinking_to_answer(self):
        response = {'message': {'role': 'assistant', 'content': '', 'thinking': 'PRIVATE_INTERNAL_TRACE'},
                    'done_reason': 'length', 'eval_count': 16384}
        with patch('jarvis.llm.urlopen', side_effect=lambda *a, **k:
                   io.BytesIO(json.dumps(response).encode())) as mocked:
            client = OllamaClient()
            with self.assertRaises(GenerationLimitError):
                client.chat([], [])
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(client.last_response_info['done_reason'], 'length')
        self.assertNotIn('PRIVATE_INTERNAL_TRACE', json.dumps(client.last_response_info))

    def test_length_retry_increases_budget_keeps_thinking_and_reuses_complete_input(self):
        truncated = {'message': {'role': 'assistant', 'content': '半截答案',
                                 'thinking': 'PRIVATE_INTERNAL_TRACE',
                                 'tool_calls': [{'function': {'name': 'save_memory',
                                                              'arguments': {'content': '半截'}}}]},
                     'done_reason': 'length', 'eval_count': 8192}
        complete = {'message': {'role': 'assistant', 'content': '完整答案'},
                    'done_reason': 'stop', 'eval_count': 9000}
        messages = [{'role': 'user', 'content': '问题', 'images': ['aW1hZ2U=']},
                    {'role': 'assistant', 'content': '', 'thinking': 'OLD_TRACE',
                     'tool_calls': [{'function': {'name': 'search_web', 'arguments': {}}}]},
                    {'role': 'tool', 'tool_name': 'search_web', 'content': '搜索结果'}]
        with patch('jarvis.llm.urlopen', side_effect=[
                io.BytesIO(json.dumps(truncated).encode()),
                io.BytesIO(json.dumps(complete).encode())]) as mocked:
            client = OllamaClient()
            answer = client.chat(messages, [])
        payloads = [json.loads(call.args[0].data) for call in mocked.call_args_list]
        self.assertEqual([p['options']['num_predict'] for p in payloads], [8192, 16384])
        self.assertTrue(all(p['think'] is True for p in payloads))
        self.assertEqual(payloads[0]['messages'], payloads[1]['messages'])
        self.assertEqual(payloads[1]['messages'][0]['images'], ['aW1hZ2U='])
        self.assertEqual(payloads[1]['messages'][-1]['content'], '搜索结果')
        self.assertNotIn('PRIVATE_INTERNAL_TRACE', json.dumps(payloads))
        self.assertNotIn('OLD_TRACE', json.dumps(payloads))
        self.assertEqual(messages[1]['thinking'], 'OLD_TRACE')
        self.assertEqual(answer['content'], '完整答案')
        self.assertEqual(client.last_response_info['attempts'], 2)

    def test_equal_budgets_do_not_retry_identical_request(self):
        response = {'message': {'content': '半截正文'}, 'done_reason': 'length'}
        with patch('jarvis.llm.config.MAX_NUM_PREDICT', 8192), \
                patch('jarvis.llm.urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as mocked:
            with self.assertRaises(GenerationLimitError):
                OllamaClient().chat([], [])
        self.assertEqual(mocked.call_count, 1)

    def test_invalid_generation_budget_fails_before_request(self):
        with patch('jarvis.llm.config.NUM_CTX', 16384), patch('jarvis.llm.urlopen') as mocked:
            with self.assertRaisesRegex(ValueError, 'OLLAMA_MAX_NUM_PREDICT'):
                OllamaClient().chat([], [])
        mocked.assert_not_called()

    def test_timeout_has_an_actionable_error_and_is_not_retried(self):
        with patch('jarvis.llm.urlopen', side_effect=TimeoutError) as mocked:
            with self.assertRaisesRegex(RuntimeError, 'OLLAMA_CHAT_TIMEOUT'):
                OllamaClient().chat([], [])
        self.assertEqual(mocked.call_count, 1)


if __name__ == '__main__':
    unittest.main()
