import io
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from jarvis import trace
from jarvis.agent import Agent
from jarvis.character_context import CharacterContext
from jarvis.desktop_bridge import make_server
from jarvis.desktop_client import DesktopMCPClient
from jarvis.llm import OllamaClient
from jarvis.monitor import Renderer
from jarvis.text_encoding import (InputEncodingError, configure_stdio, normalize_data,
                                  normalize_text, safe_json_dumps)


class UnicodeTests(unittest.TestCase):
    def test_complete_utf8_surrogateescape_bytes_and_utf16_pairs_are_recovered(self):
        damaged = '桌面有' + '些什么'.encode('utf-8').decode('ascii', errors='surrogateescape')
        self.assertEqual(normalize_text(damaged, strict=True), '桌面有些什么')
        self.assertEqual(normalize_text('鲸鱼\ud83d\udc0b', strict=True), '鲸鱼🐋')
        self.assertEqual(normalize_text('正常中文📁报告.pdf'), '正常中文📁报告.pdf')

    def test_incomplete_input_is_rejected_and_external_names_are_explicitly_escaped(self):
        for damaged in ('\udce4', '桌面\ud800', '\udc00'):
            with self.assertRaisesRegex(InputEncodingError, '重新粘贴'):
                normalize_text(damaged, strict=True)
        name = '报告\udce4.pdf'
        clean = normalize_data({'name': name, 'file_id': 'file_123'})
        self.assertEqual(clean['name'], '报告\\udce4.pdf')
        self.assertEqual(clean['file_id'], 'file_123')
        json.dumps(clean, ensure_ascii=False).encode('utf-8')

    def test_diagnostic_json_preserves_the_original_unpaired_code_unit(self):
        value = {'text': '中文\udce4🐋'}
        encoded = safe_json_dumps(value).encode('utf-8')
        self.assertEqual(json.loads(encoded), value)

    def test_invalid_first_message_does_not_poison_a_later_normal_turn(self):
        context = CharacterContext()
        agent = Agent(OllamaClient(), Mock(schemas=[], desktop=None), context=context)
        with patch.object(trace, '_recorder', False), patch('jarvis.llm.urlopen') as request:
            with self.assertRaises(InputEncodingError):
                agent.reply('\udce4')
            request.assert_not_called()
            self.assertEqual(context.shared()['recent_events'], [])
            self.assertIsNone(context.last_user_request)
            request.return_value = io.BytesIO(b'{"message":{"role":"assistant","content":"ok"},"done":true,"done_reason":"stop"}')
            self.assertEqual(agent.reply('桌面有些什么'), 'ok')
            payload = json.loads(request.call_args.args[0].data)
            self.assertEqual(payload['messages'][-1]['content'], '桌面有些什么')

    def test_llm_transport_handles_legacy_bad_history_and_opaque_file_ids(self):
        messages = [{'role': 'system', 'content': '旧上下文\udce4'},
                    {'role': 'user', 'content': '看看📁报告\udce4.pdf'}]
        tool = {'type': 'function', 'function': {'name': 'example', 'description': '不完整\ud800'}}
        with patch.object(trace, '_recorder', False), patch('jarvis.llm.urlopen', return_value=io.BytesIO(
                b'{"message":{"content":"ok"},"done":true,"done_reason":"stop"}')) as request:
            self.assertEqual(OllamaClient().chat(messages, [tool])['content'], 'ok')
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload['messages'][0]['content'], '旧上下文\\udce4')
        self.assertEqual(payload['messages'][1]['content'], '看看📁报告\\udce4.pdf')
        self.assertEqual(payload['tools'][0]['function']['description'], '不完整\\ud800')

    def test_cli_repairs_input_and_rejects_incomplete_bytes_before_dispatch(self):
        from jarvis.__main__ import interact
        agent, store = Mock(), Mock()
        store.due_reminders.return_value = []
        repaired = '桌面' + '有些什么'.encode('utf-8').decode('ascii', errors='surrogateescape')
        with patch.object(trace, '_recorder', False), patch('sys.stdout', new_callable=io.StringIO) as output, \
                patch('builtins.input', side_effect=['\udce4', repaired, '/quit']):
            interact(agent, None, store, 'owner', Mock())
        agent.reply.assert_called_once_with('桌面有些什么')
        self.assertIn('重新粘贴', output.getvalue())

    def test_logger_and_utf8_monitor_do_not_crash_on_bad_input(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = trace.Recorder(directory)
            with patch.object(trace, '_recorder', recorder):
                trace.emit('input.invalid_unicode', text='坏输入\udce4', error='retry')
            record = json.loads(recorder.path.read_bytes())
            self.assertEqual(record['data']['text'], '坏输入\udce4')
            for raw in (False, True):
                data = io.BytesIO()
                output = io.TextIOWrapper(data, encoding='utf-8', errors='strict')
                renderer = Renderer(output, raw=raw)
                renderer.write(record); renderer.finish(); output.flush()
                rendered = data.getvalue().decode('utf-8')
                self.assertIn('\\udce4', rendered)
                if raw:
                    self.assertEqual(json.loads(rendered)['data']['text'], '坏输入\udce4')
                output.detach()

    def test_mcp_file_name_with_unpaired_code_unit_round_trips_without_losing_id(self):
        server = make_server(0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        server.broker.exchange({'snapshot': {'scene_session': 'unicode', 'character': {'position': {'x': 0, 'z': 0}},
            'objects': [{'object_id': 'file_123', 'display_name': '报告\udce4.pdf', 'object_type': 'file',
                         'position': {'x': 0, 'z': 0}}]}})
        client = DesktopMCPClient(f'http://127.0.0.1:{server.server_port}')
        try:
            state = client.call('get_desktop_state', {})
            self.assertEqual(state['objects'][0]['object_id'], 'file_123')
            self.assertEqual(state['objects'][0]['display_name'], '报告\udce4.pdf')
        finally:
            client.close(); server.shutdown(); server.server_close()

    def test_stdio_is_explicit_utf8_and_diagnostics_never_raise_on_invalid_unicode(self):
        streams = [io.TextIOWrapper(io.BytesIO(), encoding='gb18030') for _ in range(3)]
        with patch('sys.stdin', streams[0]), patch('sys.stdout', streams[1]), patch('sys.stderr', streams[2]):
            configure_stdio()
            self.assertEqual([s.encoding for s in streams], ['utf-8'] * 3)
            self.assertEqual(streams[0].errors, 'surrogateescape')
            streams[1].write('中文\udce4🐋'); streams[1].flush()
            self.assertIn(b'\\udce4', streams[1].buffer.getvalue())
        for stream in streams: stream.close()
