"""Loopback text chat. HTTP accepts input; the CLI thread runs every model turn."""
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from collections import OrderedDict

from .text_encoding import normalize_text, safe_json_dumps


class UnityChat:
    def __init__(self, port=18766):
        self.inbox = Queue()
        self.lock = threading.Lock()
        self.terminal_reading = False
        self.accepted = OrderedDict()
        self.state = dict(session=uuid.uuid4().hex, request_id='', status='idle', error='', reply_id='', reply='')
        chat = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def allowed(self):
                return (self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
                        and 'Origin' not in self.headers)

            def send_json(self, data, status=200):
                body = safe_json_dumps(data).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self.allowed() or self.path != '/state':
                    return self.send_json({'error': 'Invalid chat endpoint'}, 403)
                with chat.lock:
                    state = chat.state.copy()
                self.send_json(state)

            def do_POST(self):
                if not self.allowed() or self.path != '/message':
                    return self.send_json({'error': 'Invalid chat endpoint'}, 403)
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 32768 or self.headers.get_content_type() != 'application/json':
                        raise ValueError('Invalid message format')
                    data = json.loads(self.rfile.read(size))
                    request_id = uuid.UUID(data['request_id']).hex
                    prompt = normalize_text(data['text'], strict=True).strip()
                    if not prompt or len(prompt) > 4000 or prompt.startswith('/'):
                        raise ValueError('请输入 1 至 4000 字的对话。斜杠命令可在终端使用。')
                    with chat.lock:
                        if request_id in chat.accepted:
                            if chat.accepted[request_id] != prompt:
                                raise ValueError('消息 ID 已用于其他文本。')
                        else:
                            if chat.state['status'] == 'pending':
                                return self.send_json({'error': '请等大肥鱼回复后再发送。'}, 409)
                            chat.accepted[request_id] = prompt
                            if len(chat.accepted) > 128:
                                chat.accepted.popitem(last=False)
                            chat.state.update(request_id=request_id, status='pending', error='')
                            chat.inbox.put((request_id, prompt))
                    self.send_json({'request_id': request_id}, 202)
                except (ValueError, KeyError, TypeError, AttributeError) as error:
                    self.send_json({'error': str(error)}, 400)

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def read(self):
        if not self.terminal_reading:
            self.terminal_reading = True
            threading.Thread(target=self._read_terminal, daemon=True).start()
        request_id, prompt = self.inbox.get()
        if request_id is None:
            self.terminal_reading = False
            if isinstance(prompt, BaseException):
                raise prompt
        return request_id, prompt

    def _read_terminal(self):
        try:
            text = input('\nYou> ')
        except (EOFError, KeyboardInterrupt) as error:
            text = error
        self.inbox.put((None, text))

    def finish(self, request_id, answer=None, error=None):
        with self.lock:
            if request_id:
                self.state.update(status='failed' if error is not None else 'done', error=error or '')
            if answer:
                self.state.update(reply_id=uuid.uuid4().hex, reply=answer)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
