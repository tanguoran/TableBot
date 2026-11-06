"""Local diagnostic events, separate from model history and MCP stdout."""
import json
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from .text_encoding import safe_json_dumps

DEFAULT_DIRECTORY = Path(__file__).resolve().parent.parent / 'data' / 'traces'
_context = ContextVar('model_trace_context', default={})
_recorder = None
_lock = threading.Lock()
_warned = False


class Recorder:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        self.path = self.directory / f'{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}.jsonl'
        self.lock = threading.Lock()
        self.sequence = 0

    def write(self, event, data):
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.sequence += 1
            record = {'time': datetime.now(timezone.utc).isoformat(timespec='microseconds'),
                      'pid': os.getpid(), 'seq': self.sequence,
                      'thread': threading.current_thread().name,
                      'event': event, 'context': dict(_context.get()), 'data': data}
            line = safe_json_dumps(record, allow_nan=False, default=str) + '\n'
            # One process owns each file. A thread lock keeps complete JSONL records intact.
            with self.path.open('a', encoding='utf-8', newline='\n') as output:
                output.write(line)


def configure(enabled=True, directory=None, *, export=False):
    global _recorder
    directory = Path(directory or DEFAULT_DIRECTORY).resolve()
    with _lock:
        _recorder = Recorder(directory) if enabled else False
    if export:
        os.environ['JARVIS_TRACE_ENABLED'] = 'true' if enabled else 'false'
        os.environ['JARVIS_TRACE_DIR'] = str(directory)


def recorder():
    global _recorder
    with _lock:
        if _recorder is None:
            active = os.getenv('JARVIS_TRACE_ENABLED', 'false').lower() in ('true', '1', 'yes', 'on')
            _recorder = Recorder(os.getenv('JARVIS_TRACE_DIR') or DEFAULT_DIRECTORY) if active else False
        return _recorder


def enabled():
    return bool(recorder())


@contextmanager
def scope(**fields):
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def emit(event, **data):
    global _warned
    try:
        target = recorder()
        if target:
            target.write(event, data)
    except (OSError, ValueError, TypeError) as error:
        # Diagnostics must not fail an action, and never write into MCP's stdout.
        if not _warned:
            _warned = True
            print('Model trace unavailable: ' + str(error), file=sys.stderr)


def call(name, arguments, function, *, reused=False):
    with scope(tool_call_id=uuid.uuid4().hex):
        emit('tool.start', name=name, arguments=arguments, reused=reused)
        started = time.monotonic()
        try:
            result = function()
        except Exception as error:
            emit('tool.error', name=name, error=str(error), elapsed_seconds=time.monotonic() - started)
            raise
        emit('tool.result', name=name, result=result, elapsed_seconds=time.monotonic() - started)
        return result
