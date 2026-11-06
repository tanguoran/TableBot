"""Real vision and follow-up checks using a temporary synthetic PNG."""
import struct
import tempfile
import zlib
from pathlib import Path

from . import config
from .agent import Agent
from .images import parse_image_command
from .llm import create_chat_client


def sample_png():
    width, height = 320, 200
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            if 25 <= x < 140 and 70 <= y < 130:
                color = (240, 0, 0)
            elif (x - 235) ** 2 + (y - 100) ** 2 <= 50 ** 2:
                color = (0, 0, 240)
            else:
                color = (255, 255, 255)
            rows.extend(color)

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def run():
    class NoTools:
        def run(self, name, args):
            raise AssertionError(f'Pure vision should not call tools: {name}')

    try:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '视觉 测试.png'
            path.write_bytes(sample_png())
            client = create_chat_client()
            agent = Agent(client, NoTools())
            image_path, question = parse_image_command(
                f'/image "{path}" 请直接观察图片，左右两边分别是什么颜色、什么形状？用中文简短回答。')
            print(f'Model: {client.model}; testing image input with {config.CHAT_PROVIDER}.', flush=True)
            answer = agent.reply(question, image_paths=[image_path])
            print('First answer:', answer, flush=True)
            if not all(word in answer for word in ('红', '蓝', '圆')) or not any(word in answer for word in ('矩', '长方')):
                raise AssertionError('Expected red rectangle and blue circle in image-grounded answer')
            print('PASS: actual visual colors and shapes recognized.', flush=True)
            # The attachment is a byte snapshot, not a path the model can open.
            path.unlink()
            followup = agent.reply('刚才图片右边的形状是什么颜色？只回答颜色。')
            print('Follow-up:', followup, flush=True)
            if '蓝' not in followup:
                raise AssertionError('Image context was not retained for the follow-up')
            agent.clear()
            if agent.history:
                raise AssertionError('Clearing conversation did not release image history')
            print('PASS: follow-up retains image bytes; clear releases history. No web or database tools executed.', flush=True)
        return 0
    except (AssertionError, OSError, RuntimeError, ValueError) as exc:
        print(f'FAIL: {exc}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
