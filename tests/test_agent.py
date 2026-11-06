import copy
import json
import unittest

from jarvis.agent import Agent
from jarvis.storage import Store
from jarvis.tools import Tools
from jarvis.llm import GenerationLimitError


def tool_call(name, args):
    return {'role': 'assistant', 'content': '', 'tool_calls': [
        {'function': {'name': name, 'arguments': args}}]}


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def chat(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        if not self.responses:
            raise AssertionError('Unexpected additional model request')
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class EmptyReplyTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.searches = []

        def search(query):
            self.searches.append(query)
            return {'provider': 'doubao', 'results': [
                {'title': '检索到的新闻标题', 'url': 'https://example.com/news',
                 'published_at': '2026-11-01T10:00:00+08:00', 'content': '测试片段'}]}

        self.tools = Tools(self.store, search_fn=search)

    def tearDown(self):
        self.store.conn.close()

    def test_empty_first_response_retries_without_exposing_thinking(self):
        llm = ScriptedLLM([
            {'role': 'assistant', 'content': '', 'thinking': 'PRIVATE_INTERNAL_TRACE'},
            {'role': 'assistant', 'content': '你好。'},
        ])
        agent = Agent(llm, self.tools)
        self.assertEqual(agent.reply('你好'), '你好。')
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(llm.requests[1][-1]['role'], 'system')
        self.assertEqual([m['role'] for m in agent.history], ['user', 'assistant'])
        self.assertNotIn('PRIVATE_INTERNAL_TRACE', json.dumps(agent.history))

    def test_empty_summary_retries_with_existing_search_result(self):
        llm = ScriptedLLM([
            tool_call('search_web', {'query': '今天头条新闻'}),
            {'role': 'assistant', 'content': '', 'thinking': 'PRIVATE_INTERNAL_TRACE'},
            {'role': 'assistant', 'content': '找到新闻：https://example.com/news'},
        ])
        answer = Agent(llm, self.tools).reply('帮我找一下今天头条新闻')
        self.assertIn('https://example.com/news', answer)
        self.assertEqual(len(self.searches), 1)
        self.assertTrue(any(m['role'] == 'tool' for m in llm.requests[2]))

    def test_repeated_empty_summary_shows_sources_instead_of_placeholder(self):
        llm = ScriptedLLM([
            tool_call('search_web', {'query': '今天头条新闻'}),
            {'role': 'assistant', 'content': '', 'thinking': 'PRIVATE_INTERNAL_TRACE'},
            {'role': 'assistant', 'content': None},
        ])
        agent = Agent(llm, self.tools)
        answer = agent.reply('帮我找一下今天头条新闻')
        self.assertIn('模型暂未完成整理', answer)
        self.assertIn('检索到的新闻标题', answer)
        self.assertIn('https://example.com/news', answer)
        self.assertIn('2026-11-01', answer)
        self.assertNotIn('PRIVATE_INTERNAL_TRACE', answer)
        self.assertEqual(agent.history[-1]['content'], answer)

    def test_empty_summary_after_search_failure_reports_the_actual_error(self):
        tools = Tools(self.store, search_fn=lambda query: {'error': 'Search quota exhausted'})
        llm = ScriptedLLM([
            tool_call('search_web', {'query': '新闻'}),
            {'role': 'assistant', 'content': ''},
            {'role': 'assistant', 'content': ''},
        ])
        self.assertIn('Search quota exhausted', Agent(llm, tools).reply('新闻'))

    def test_persistent_empty_reply_has_bounded_recovery(self):
        llm = ScriptedLLM([
            {'role': 'assistant', 'content': '  '},
            {'role': 'assistant', 'content': ''},
        ])
        answer = Agent(llm, self.tools).reply('你好')
        self.assertIn('请重试', answer)
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(self.searches, [])

    def test_recovery_does_not_repeat_a_successful_reminder_write(self):
        call = tool_call('create_reminder', {'message': '检查电池', 'due_at': '2099-01-01T10:00:00+09:00'})
        llm = ScriptedLLM([
            copy.deepcopy(call), {'role': 'assistant', 'content': ''},
            copy.deepcopy(call), {'role': 'assistant', 'content': '已经创建。'},
        ])
        self.assertEqual(Agent(llm, self.tools).reply('2099年1月1日日本时间上午十点提醒我检查电池'), '已经创建。')
        self.assertEqual(len(self.store.list_reminders('owner')), 1)

    def test_history_trimming_keeps_whole_tool_turns(self):
        llm = ScriptedLLM([{'role': 'assistant', 'content': '本次答复'}])
        agent = Agent(llm, self.tools)
        for n in range(9):
            agent.history.extend([
                {'role': 'user', 'content': f'问题{n}'},
                tool_call('search_web', {'query': str(n)}),
                {'role': 'tool', 'tool_name': 'search_web', 'content': '{}'},
                {'role': 'assistant', 'content': f'答复{n}'},
            ])
        agent.reply('下一个问题')
        self.assertLessEqual(len(agent.history), 30)
        self.assertEqual(agent.history[0]['role'], 'user')
        for index, message in enumerate(agent.history):
            if message['role'] == 'tool':
                self.assertTrue(agent.history[index - 1].get('tool_calls'))

    def test_large_search_history_retains_answer_without_large_tool_payload(self):
        llm = ScriptedLLM([{'role': 'assistant', 'content': '新的答复'}])
        agent = Agent(llm, self.tools)
        agent.history = [
            {'role': 'user', 'content': '上一轮新闻'},
            tool_call('search_web', {'query': '新闻'}),
            {'role': 'tool', 'content': '很长的网页正文' * 3000},
            {'role': 'assistant', 'content': '上一轮的摘要与来源'},
        ]
        agent.reply('继续聊')
        self.assertIn('上一轮的摘要与来源', json.dumps(llm.requests[0], ensure_ascii=False))
        self.assertLess(len(json.dumps(llm.requests[0], ensure_ascii=False)), 6000)
        self.assertFalse(any(m['role'] == 'tool' for m in llm.requests[0]))
        self.assertFalse(any(m.get('tool_calls') for m in llm.requests[0]))

    def test_orphaned_legacy_tool_messages_are_not_sent_to_model(self):
        llm = ScriptedLLM([{'role': 'assistant', 'content': '好的'}])
        agent = Agent(llm, self.tools)
        agent.history = [{'role': 'tool', 'content': '{}'},
                         {'role': 'assistant', 'content': '孤立的旧答复'}]
        agent.reply('你好')
        self.assertEqual([m['role'] for m in llm.requests[0]], ['system', 'user'])

    def test_step_limit_finishes_history_with_assistant_message(self):
        llm = ScriptedLLM([tool_call('search_web', {'query': '新闻'})])
        agent = Agent(llm, self.tools, max_steps=1)
        self.assertIn('工具调用达到上限', agent.reply('新闻'))
        self.assertEqual(agent.history[-1]['role'], 'assistant')

    def test_thinking_is_removed_from_tool_turns_final_answers_and_legacy_history(self):
        call = tool_call('search_web', {'query': '新闻'})
        call['thinking'] = 'PRIVATE_TOOL_TRACE'
        llm = ScriptedLLM([call, {'role': 'assistant', 'content': '答复',
                                  'thinking': 'PRIVATE_ANSWER_TRACE'}])
        agent = Agent(llm, self.tools)
        agent.history = [{'role': 'user', 'content': '上个问题'},
                         {'role': 'assistant', 'content': '上个答案', 'thinking': 'PRIVATE_OLD_TRACE'}]
        self.assertEqual(agent.reply('新闻'), '答复')
        self.assertNotIn('PRIVATE_', json.dumps(llm.requests + [agent.history]))
        self.assertEqual(len(self.searches), 1)

    def test_exhausted_generation_preserves_search_sources_and_does_not_retry_again(self):
        llm = ScriptedLLM([tool_call('search_web', {'query': '新闻'}),
                           GenerationLimitError('模型达到生成上限，仍未完成答复。')])
        agent = Agent(llm, self.tools)
        answer = agent.reply('新闻')
        self.assertIn('生成上限', answer)
        self.assertIn('https://example.com/news', answer)
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(len(self.searches), 1)
        self.assertEqual(agent.history[-1]['content'], answer)

    def test_exhausted_generation_before_tool_call_does_not_claim_tool_execution(self):
        llm = ScriptedLLM([GenerationLimitError('模型达到生成上限，仍未完成答复。')])
        agent = Agent(llm, self.tools)
        answer = agent.reply('2099年1月1日日本时间上午十点提醒我检查电池')
        self.assertIn('生成上限', answer)
        self.assertEqual(self.store.list_reminders('owner'), [])
        self.assertEqual(len(llm.requests), 1)


if __name__ == '__main__':
    unittest.main()
