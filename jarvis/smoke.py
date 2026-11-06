"""Opt-in real-model checks. All writes use an isolated in-memory database."""
from datetime import datetime, timedelta
from time import perf_counter
from zoneinfo import ZoneInfo

from . import config
from .agent import Agent
from .llm import create_chat_client
from .storage import Store
from .tools import Tools


class RecordingTools(Tools):
    def __init__(self, store):
        super().__init__(store, 'smoke-test')
        self.calls = []
        self.results = []

    def run(self, name, args):
        self.calls.append(name)
        result = super().run(name, args)
        self.results.append(result)
        return result


def run():
    store = Store(':memory:')
    tools = RecordingTools(store)
    agent = Agent(create_chat_client(), tools)

    def ask(prompt, expected_tool=None, allow_error=False):
        started = perf_counter()
        previous = len(tools.calls)
        print(f'You> {prompt}', flush=True)
        answer = agent.reply(prompt)
        print(f'JARVIS> {answer}', flush=True)
        calls = tools.calls[previous:]
        print(f'  tools={calls}, elapsed={perf_counter() - started:.1f}s', flush=True)
        if not answer or answer == '模型未返回文本。':
            raise AssertionError('Model did not return an answer')
        if expected_tool and expected_tool not in calls:
            raise AssertionError(f'Expected tool was not called: {expected_tool}')
        if not allow_error:
            for result in tools.results[previous:]:
                if 'error' in result:
                    raise AssertionError(f'Tool failed: {result["error"]}')

    try:
        ask('你好，请用一句中文介绍你自己。')
        due = (datetime.now(ZoneInfo(config.TIMEZONE)) + timedelta(days=1)).replace(microsecond=0)
        ask(f'请在 {due.isoformat()} 提醒我检查测试电池。时区是 {config.TIMEZONE}。',
            'create_reminder')
        if len(store.list_reminders('smoke-test')) != 1:
            raise AssertionError('Reminder was not created')
        reminder = store.list_reminders('smoke-test')[0]
        if datetime.fromisoformat(reminder['due_at']) != due:
            raise AssertionError('Reminder time did not match the requested instant')
        ask('请列出我目前待办的提醒。', 'list_reminders')
        ask('请取消刚才的检查测试电池提醒。', 'cancel_reminder')
        if store.list_reminders('smoke-test'):
            raise AssertionError('Reminder was not cancelled')
        if not config.SEARCH_API_KEY:
            previous = len(tools.results)
            ask('请联网搜索今天的机器人新闻。', 'search_web', allow_error=True)
            if not any(result.get('error_code') == 'missing_api_key' for result in tools.results[previous:]):
                raise AssertionError('Expected Doubao missing-key error was not returned')
            print('Search: verified missing-key handling; live results not tested.')
        else:
            print('Search: key present; use --search-test for an explicit real Doubao search.')
        print('PASS: real Ollama conversation and reminder tool loops.')
        return 0
    except (AssertionError, RuntimeError, OSError) as exc:
        print(f'FAIL: {exc}', flush=True)
        return 1
    finally:
        store.conn.close()

