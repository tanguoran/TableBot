"""Local PNG/JPEG attachments for Ollama's base64 images field."""
import base64
import os
import re
from pathlib import Path


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGES = 2
DEFAULT_QUESTION = '请描述这张图片中的主要内容；看不清的细节请明确说明。'
IMAGE_USAGE = '/image "图片完整路径" [你的问题]（支持 PNG、JPEG）'


def parse_image_command(command):
    parts = command.split(maxsplit=1)
    if not parts or parts[0] != '/image' or len(parts) != 2:
        raise ValueError('用法：' + IMAGE_USAGE)
    args = parts[1].strip()
    if args[0] in ('"', "'"):
        end = args.find(args[0], 1)
        if end < 0 or (len(args) > end + 1 and not args[end + 1].isspace()):
            raise ValueError('图片路径引号未闭合，或路径与问题之间缺少空格。用法：' + IMAGE_USAGE)
        path, question = args[1:end], args[end + 1:].strip()
    else:
        values = args.split(maxsplit=1)
        path, question = values[0], values[1] if len(values) == 2 else ''
    if not path:
        raise ValueError('图片路径不能为空。用法：' + IMAGE_USAGE)
    return path, question or DEFAULT_QUESTION


def resolve_image_path(value):
    value = str(value)
    if re.match(r'^[a-z][a-z0-9+.-]*://', value, re.I):
        raise ValueError('请提供本地图片路径；当前不下载图片网址。')
    if os.name != 'nt' and re.match(r'^[A-Za-z]:[\\/]', value):
        value = '/mnt/' + value[0].lower() + '/' + value[3:].replace('\\', '/')
    return Path(value).expanduser().resolve()


def image_dimensions(raw):
    # Header validation bounds decoded dimensions before contacting the model.
    # Full decoding is left to Ollama; corrupt image data may still be rejected.
    if (len(raw) >= 33 and raw.startswith(b'\x89PNG\r\n\x1a\n')
            and raw[8:16] == b'\x00\x00\x00\rIHDR'):
        return int.from_bytes(raw[16:20], 'big'), int.from_bytes(raw[20:24], 'big')
    if raw.startswith(b'\xff\xd8\xff'):
        offset = 2
        while offset < len(raw) and raw[offset] == 0xff:
            while offset < len(raw) and raw[offset] == 0xff:
                offset += 1
            if offset >= len(raw):
                break
            marker = raw[offset]
            offset += 1
            if marker in (0xd9, 0xda):
                break
            if marker == 0x01 or 0xd0 <= marker <= 0xd8:
                continue
            length = int.from_bytes(raw[offset:offset + 2], 'big')
            if length < 2 or offset + length > len(raw):
                break
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                          0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf) and length >= 8:
                return (int.from_bytes(raw[offset + 5:offset + 7], 'big'),
                        int.from_bytes(raw[offset + 3:offset + 5], 'big'))
            offset += length
    raise ValueError('图片格式无法识别或文件已损坏；请使用 PNG 或 JPEG。')


def load_images(paths):
    if isinstance(paths, (str, bytes, Path)):
        raise ValueError('image_paths 必须是图片路径列表。')
    paths = list(paths)
    if not 1 <= len(paths) <= MAX_IMAGES:
        raise ValueError(f'每轮请提供 1–{MAX_IMAGES} 张图片。')
    images = []
    for value in paths:
        path = resolve_image_path(value)
        if not path.is_file():
            raise ValueError(f'图片不存在或不是普通文件：{path}')
        try:
            with path.open('rb') as handle:
                raw = handle.read(MAX_IMAGE_BYTES + 1)
        except OSError as exc:
            raise ValueError(f'无法读取图片：{path}（{exc.strerror or type(exc).__name__}）') from exc
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f'单张图片不能超过 10 MiB：{path.name}')
        width, height = image_dimensions(raw)
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError(f'图片尺寸无效或超过 1600 万像素：{path.name}（{width}×{height}）')
        images.append(base64.b64encode(raw).decode('ascii'))
    return images
