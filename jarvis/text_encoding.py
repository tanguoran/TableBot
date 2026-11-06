"""UTF-8 boundaries for terminal input, model requests and diagnostic JSON."""
import json
import re
import sys

_SURROGATE = re.compile(r'[\ud800-\udfff]')
_PAIR = re.compile(r'([\ud800-\udbff])([\udc00-\udfff])')


class InputEncodingError(ValueError):
    pass


def normalize_text(text, *, strict=False):
    if not isinstance(text, str):
        raise TypeError('text must be a string')
    if not _SURROGATE.search(text):
        return text
    # Some JSON/Windows sources represent a valid supplementary character as two code units.
    text = _PAIR.sub(lambda m: chr(0x10000 + ((ord(m[1]) - 0xd800) << 10) + ord(m[2]) - 0xdc00), text)
    if not _SURROGATE.search(text):
        return text
    try:
        # Recover complete UTF-8 bytes preserved by Python's surrogateescape input decoder.
        recovered = text.encode('utf-8', errors='surrogateescape').decode('utf-8', errors='strict')
        if not _SURROGATE.search(recovered):
            return recovered
    except UnicodeError:
        pass
    if strict:
        raise InputEncodingError('输入含有不完整的编码字符，请重新粘贴这条消息；若反复出现，请重启更新后的 wsl.ps1。')
    # Preserve an explicit representation in model context; never silently drop a filename byte.
    return _SURROGATE.sub(lambda m: f'\\u{ord(m[0]):04x}', text)


def normalize_data(value):
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, dict):
        return {normalize_text(k) if isinstance(k, str) else k: normalize_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_data(v) for v in value]
    return value


def safe_json_dumps(value, **kwargs):
    """Keep original JSON values, escaping unpaired surrogates into valid UTF-8 JSON."""
    kwargs.setdefault('ensure_ascii', False)
    result = json.dumps(value, **kwargs)
    return result.encode('utf-8', errors='backslashreplace').decode('utf-8') if _SURROGATE.search(result) else result


def configure_stdio():
    # stdin keeps undecodable bytes long enough to report/recover them before model history.
    for stream, errors in ((sys.stdin, 'surrogateescape'), (sys.stdout, 'backslashreplace'), (sys.stderr, 'backslashreplace')):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors=errors)
            except (OSError, ValueError):
                pass
