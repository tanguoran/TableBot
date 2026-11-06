import json
import threading
import time
import unittest
import uuid
from queue import Queue
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from jarvis.__main__ import interact
from jarvis.unity_chat import UnityChat


class UnityChatTests(unittest.TestCase):
    def setUp(self):
        self.chat = UnityChat(port=0)
        self.addCleanup(self.chat.close)
        self.url = 'http://127.0.0.1:' + str(self.chat.server.server_port)

    def post(self, text, request_id, **headers):
        req = Request(self.url + '/message', data=json.dumps(dict(request_id=request_id, text=text)).encode(),
                      headers={'Content-Type': 'application/json', **headers})
        with urlopen(req, timeout=2) as reply:
            return json.load(reply)

    def state(self):
        with urlopen(self.url + '/state', timeout=2) as reply:
            return json.load(reply)

    def test_inputs_share_one_agent_thread_and_only_completed_answers_are_published(self):
        terminal = Queue()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def reply(text):
            calls.append((text, threading.get_ident()))
            entered.set()
            release.wait(3)
            return '收到：' + text
        agent, store = Mock(), Mock()
        agent.reply.side_effect = reply
        store.due_reminders.return_value = []
        worker = threading.Thread(target=interact, args=(agent, None, store, 'owner', None, self.chat))
        with patch('builtins.input', side_effect=lambda _: terminal.get(timeout=5)):
            worker.start()
            try:
                key = uuid.uuid4().hex
                self.post('你好🐋', key)
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.state()['reply'], '')
                self.post('你好🐋', key)
                with self.assertRaises(HTTPError) as busy:
                    self.post('新的', uuid.uuid4().hex)
                self.assertEqual(busy.exception.code, 409)
                release.set()
                for _ in range(100):
                    if self.state()['status'] == 'done': break
                    time.sleep(.01)
                self.assertEqual(self.state()['reply'], '收到：你好🐋')
                terminal.put('还记得吗')
                terminal.put('/quit')
                worker.join(2)
                self.assertEqual(calls, [('你好🐋', worker.ident), ('还记得吗', worker.ident)])
                self.assertEqual(self.state()['reply'], '收到：还记得吗')
            finally:
                release.set()
                terminal.put('/quit')
                worker.join(2)

    def test_rejects_browser_requests_and_cli_control_commands(self):
        for text, headers, code in [('你好', {'Origin': 'https://example.com'}, 403),
                                    ('/quit', {}, 400), (' ', {}, 400)]:
            with self.assertRaises(HTTPError) as rejected:
                self.post(text, uuid.uuid4().hex, **headers)
            self.assertEqual(rejected.exception.code, code)
        self.assertEqual(self.state()['status'], 'idle')
