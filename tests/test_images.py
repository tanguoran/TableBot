import base64
import copy
import io
import os
import struct
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import Agent
from jarvis.images import (DEFAULT_QUESTION, MAX_IMAGE_BYTES, image_dimensions,
                           load_images, parse_image_command, resolve_image_path)


def png(width=2, height=2):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\xff\0\0' * 2) * 2))
            + chunk(b'IEND', b''))


class RecordingLLM:
    def __init__(self, replies=None):
        self.requests = []
        self.replies = list(replies or [])

    def chat(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        if self.replies:
            return self.replies.pop(0)
        return {'role': 'assistant', 'content': '图片回答'}


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '图片 示例.png'
        self.path.write_bytes(png())

    def test_command_preserves_windows_backslashes_spaces_and_question(self):
        path, question = parse_image_command('/image "C:\\My Photos\\截图.png" 这张图是什么？')
        self.assertEqual(path, 'C:\\My Photos\\截图.png')
        self.assertEqual(question, '这张图是什么？')
        self.assertEqual(parse_image_command("/image '/mnt/c/My Photos/截图.png' 读文字"),
                         ('/mnt/c/My Photos/截图.png', '读文字'))

    def test_default_question_and_malformed_command(self):
        self.assertEqual(parse_image_command('/image ./图.png'), ('./图.png', DEFAULT_QUESTION))
        for command in ['/image', '/image ""', '/image "not closed', '/image "x.png"question', '/images x']:
            with self.subTest(command=command), self.assertRaises(ValueError):
                parse_image_command(command)

    def test_windows_and_relative_paths(self):
        self.assertEqual(resolve_image_path(self.path), self.path.resolve())
        if os.name != 'nt':
            self.assertEqual(str(resolve_image_path('C:\\My Photos\\截图.png')),
                             '/mnt/c/My Photos/截图.png')
            self.assertEqual(str(resolve_image_path('D:/photos/test.jpg')), '/mnt/d/photos/test.jpg')

    def test_png_bytes_are_sent_without_path_or_data_url_prefix(self):
        encoded = load_images([self.path])[0]
        self.assertEqual(base64.b64decode(encoded), self.path.read_bytes())
        self.assertFalse(encoded.startswith('data:'))
        self.assertEqual(image_dimensions(self.path.read_bytes()), (2, 2))

    def test_jpeg_dimensions_after_metadata_and_progressive_marker(self):
        for marker in [0xc0, 0xc2]:
            raw = (b'\xff\xd8\xff\xe1\x00\x06Exif\xff' + bytes([marker])
                   + b'\x00\x0b\x08\x00\x14\x00\x1e\x01\x01\x11\x00\xff\xd9')
            self.assertEqual(image_dimensions(raw), (30, 20))
            self.path.write_bytes(raw)
            self.assertEqual(base64.b64decode(load_images([self.path])[0]), raw)

    def test_missing_directory_url_and_unsupported_bytes_fail_before_model(self):
        llm = RecordingLLM()
        agent = Agent(llm, None)
        agent.history = [{'role': 'user', 'content': '以前的问题'},
                         {'role': 'assistant', 'content': '以前的答案'}]
        before = copy.deepcopy(agent.history)
        self.path.write_text('This is not an image.', encoding='utf-8')
        for path in [self.path, self.path.parent, self.path.parent / 'missing.png', 'https://example.com/a.png']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                agent.reply('看图', image_paths=[path])
        self.assertEqual(llm.requests, [])
        self.assertEqual(agent.history, before)

    def test_attachment_count_and_oversize_file_are_rejected(self):
        for paths in [[], [self.path] * 3, str(self.path)]:
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                load_images(paths)
        self.path.write_bytes(b'x' * (MAX_IMAGE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, '10 MiB'):
            load_images([self.path])

    def test_invalid_pixel_dimensions_and_malformed_headers_are_rejected(self):
        for raw in [png(0, 2), png(5000, 5000), b'\x89PNG\r\n\x1a\n',
                    b'\xff\xd8\xff\xe1\xff\xffx']:
            self.path.write_bytes(raw)
            with self.subTest(raw=raw[:12]), self.assertRaises(ValueError):
                load_images([self.path])

    def test_image_survives_tool_roundtrip_and_text_followup(self):
        llm = RecordingLLM([
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'function': {'name': 'search_knowledge', 'arguments': {'query': '图片相关资料'}}}]},
            {'role': 'assistant', 'content': '根据图片和资料回答。'},
            {'role': 'assistant', 'content': '右边是蓝色。'},
        ])

        class Tools:
            def run(self, name, args):
                return {'chunks': []}

        agent = Agent(llm, Tools())
        agent.reply('结合资料看图', image_paths=[self.path])
        agent.reply('右边是什么颜色？')
        encoded = load_images([self.path])[0]
        for request in llm.requests:
            self.assertTrue(any(m.get('images') == [encoded] for m in request))
        self.assertTrue(any(m['role'] == 'tool' for m in llm.requests[1]))

    def test_base64_does_not_evict_text_history_and_clear_releases_images(self):
        llm = RecordingLLM()
        agent = Agent(llm, None)
        agent.history = [{'role': 'user', 'content': '刚才的图片', 'images': ['x' * 100_000]},
                         {'role': 'assistant', 'content': '先前的识图回答'}]
        agent.reply('继续看右边。')
        self.assertTrue(any(m.get('content') == '先前的识图回答' for m in llm.requests[0]))
        self.assertTrue(any(m.get('images') for m in llm.requests[0]))
        agent.clear()
        agent.reply('你好')
        self.assertFalse(any(m.get('images') for m in llm.requests[1]))

    def test_new_image_evicts_oldest_attachment_but_preserves_its_text(self):
        llm = RecordingLLM()
        agent = Agent(llm, None)
        agent.history = [{'role': 'user', 'content': '第一张', 'images': ['first']},
                         {'role': 'assistant', 'content': '第一次回答'},
                         {'role': 'user', 'content': '第二张', 'images': ['second']},
                         {'role': 'assistant', 'content': '第二次回答'}]
        agent.reply('第三张', image_paths=[self.path])
        request = llm.requests[0]
        images = [image for m in request for image in m.get('images', [])]
        self.assertEqual(len(images), 2)
        self.assertNotIn('first', images)
        self.assertIn('second', images)
        self.assertTrue(any('第一张' in m.get('content', '') and '重新发送' in m['content'] for m in request))
        self.assertEqual(sum(len(m.get('images', [])) for m in agent.history), 2)

    def test_cli_routes_quoted_image_and_recovers_from_bad_command(self):
        from jarvis.__main__ import main
        with patch('sys.argv', ['jarvis']), patch('jarvis.__main__.config.DB_PATH', ':memory:'), \
                patch('jarvis.__main__.Agent') as factory, redirect_stdout(io.StringIO()) as output, \
                patch('builtins.input', side_effect=['/image', f'/image "{self.path}" 描述图片', '/quit']):
            factory.return_value.reply.return_value = '识图答案'
            main()
        factory.return_value.reply.assert_called_once_with('描述图片', image_paths=[str(self.path)])
        self.assertIn('ERROR>', output.getvalue())
        self.assertIn('识图答案', output.getvalue())


if __name__ == '__main__':
    unittest.main()
