import json
import time
import uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from . import config
from . import trace
from .text_encoding import normalize_data


class GenerationLimitError(RuntimeError):
    """No complete response was produced within the bounded generation budget."""


def create_chat_client(profile=None):
    config.chat_model()  # Validate selection instead of silently falling back.
    if config.CHAT_PROVIDER == 'deepseek':
        from .deepseek import DeepSeekClient
        return DeepSeekClient(profile=profile)
    return OllamaClient(profile=profile)


class OllamaClient:
    def __init__(self, model=None, url=None, profile=None):
        self.model = model or config.MODEL
        self.url = (url or config.BASE_URL).rstrip('/')
        self.last_response_info = {}
        self.profile = profile

    def chat(self, messages, tools):
        with trace.scope(model_call_id=uuid.uuid4().hex, profile=self.profile or 'chat', provider='ollama'):
            try:
                return self._chat(messages, tools)
            except Exception as error:
                trace.emit('model.error', error=str(error))
                raise

    def _read_response(self, response, streaming):
        if not streaming:
            data = json.load(response)
            message = data.get('message', {}) if isinstance(data, dict) else {}
            self._trace_message(message)
            return data
        message = {'role': 'assistant', 'content': ''}
        final = None
        seen_message = False
        for line in response:
            if not line.strip():
                continue
            chunk = json.loads(line)
            if not isinstance(chunk, dict) or chunk.get('error'):
                raise RuntimeError('Ollama stream error: ' + str(chunk))
            part = chunk.get('message', {})
            if not isinstance(part, dict):
                raise RuntimeError('Ollama returned an invalid streamed message')
            seen_message = seen_message or 'message' in chunk
            self._trace_message(part)
            for key in ('thinking', 'content'):
                if part.get(key):
                    message[key] = message.get(key, '') + part[key]
            if part.get('tool_calls'):
                message.setdefault('tool_calls', []).extend(part['tool_calls'])
            if chunk.get('done') is True:
                final = dict(chunk, message=message)
                break
        if final is None:
            raise RuntimeError('Ollama stream ended before completion; partial tool calls were not executed')
        if not seen_message:
            raise RuntimeError('Ollama returned an invalid chat response')
        return final

    @staticmethod
    def _trace_message(message):
        if not isinstance(message, dict):
            return
        for field in ('thinking', 'content'):
            if message.get(field):
                trace.emit('model.' + field, text=message[field])
        if message.get('tool_calls'):
            trace.emit('model.tool_calls', tool_calls=message['tool_calls'], stage='proposal_only')

    def _chat(self, messages, tools):
        if not 0 < config.NUM_PREDICT <= config.MAX_NUM_PREDICT < config.NUM_CTX:
            raise ValueError('Require 0 < OLLAMA_NUM_PREDICT <= '
                             'OLLAMA_MAX_NUM_PREDICT < OLLAMA_NUM_CTX.')
        if config.CHAT_TIMEOUT <= 0:
            raise ValueError('OLLAMA_CHAT_TIMEOUT must be positive.')
        self.last_response_info = {}
        # Previous reasoning is not conversation history. Keep complete tool exchanges.
        clean_messages = [{k: v for k, v in message.items() if k not in ('thinking', 'reasoning_content')}
                          for message in messages]
        autonomous = self.profile == 'autonomy'
        budgets = [768 if autonomous else config.NUM_PREDICT]
        if not autonomous and config.MAX_NUM_PREDICT > config.NUM_PREDICT:
            budgets.append(config.MAX_NUM_PREDICT)
        for attempt, budget in enumerate(budgets, 1):
            # The bounded background planner keeps its original 20-second response timeout.
            streaming = trace.enabled() and config.TRACE_STREAM and not autonomous
            payload = {
                'model': self.model, 'messages': clean_messages, 'tools': tools,
                'stream': streaming, 'think': False if autonomous else config.THINK,
                'options': {'temperature': 0.2, 'num_ctx': config.NUM_CTX,
                            'num_predict': budget},
            }
            req = Request(self.url + '/api/chat',
                          data=json.dumps(normalize_data(payload), ensure_ascii=False).encode('utf-8'),
                          headers={'Content-Type': 'application/json'}, method='POST')
            trace.emit('model.request', provider='ollama', model=self.model, attempt=attempt, stream=streaming,
                       think=payload['think'], num_predict=budget, message_count=len(clean_messages),
                       tools=[t.get('function', {}).get('name') for t in tools])
            started = time.monotonic()
            try:
                with urlopen(req, timeout=20 if autonomous else config.CHAT_TIMEOUT) as response:
                    with trace.scope(attempt=attempt):
                        data = self._read_response(response, streaming)
            except HTTPError as exc:
                raise RuntimeError(f'Ollama HTTP {exc.code}: '
                                   f'{exc.read().decode(errors="replace")[:400]}') from exc
            except TimeoutError as exc:
                raise RuntimeError('Ollama 请求超时；模型未完成答复。可调整 '
                                   'OLLAMA_CHAT_TIMEOUT，或缩短本次输入。') from exc
            except URLError as exc:
                raise RuntimeError('Cannot connect to Ollama. Start `ollama serve`.') from exc
            if not isinstance(data, dict) or not isinstance(data.get('message'), dict) \
                    or data.get('done') is False:
                raise RuntimeError('Ollama returned an invalid chat response.')
            self.last_response_info = {key: data.get(key) for key in
                                       ('done_reason', 'prompt_eval_count', 'eval_count')}
            self.last_response_info.update(
                attempts=attempt, num_predict=budget, think=False if autonomous else config.THINK,
                thinking_chars=len(data['message'].get('thinking') or ''))
            trace.emit('model.done', **self.last_response_info, elapsed_seconds=time.monotonic() - started,
                       tool_calls_usable=data.get('done_reason') != 'length')
            if data.get('done_reason') != 'length':
                return data['message']
            trace.emit('model.discarded', attempt=attempt, reason='generation_length',
                       retry=attempt < len(budgets), tool_calls_executed=False)
            # Discard this entire unfinished response, including any tool calls.
            # Regenerate from the same completed inputs with more space. No tools
            # execute here, so the retry cannot repeat a write or paid search.
        raise GenerationLimitError(
            f'模型达到生成上限（最多 {budgets[-1]} token），仍未完成答复。'
            '本次未执行截断响应中的工具调用。')
