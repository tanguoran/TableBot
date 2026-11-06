"""DeepSeek Chat Completions adapter for the existing Agent/tool protocol."""
import json
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import config, trace
from .llm import GenerationLimitError
from .hunger import BalanceUnavailable
from .text_encoding import normalize_data


def api_messages(messages):
    """Keep tool IDs and reasoning required by DeepSeek, without mutating shared history."""
    result, pending = [], []
    for index, message in enumerate(messages):
        role = message['role']
        item = {'role': role, 'content': message.get('content') or ''}
        if message.get('images'):
            item['content'] = [{'type': 'text', 'text': item['content']}]
            for encoded in message['images']:
                mime = 'image/png' if encoded.startswith('iVBOR') else 'image/jpeg'
                item['content'].append({'type': 'image_url', 'image_url': {
                    'url': f'data:{mime};base64,{encoded}'}})
        if role == 'assistant':
            # The API requires these across turns when tools are supplied, even on final replies.
            item['reasoning_content'] = message.get('reasoning_content') or ''
            calls = []
            for number, call in enumerate(message.get('tool_calls') or []):
                function = call['function']
                call_id = call.get('id') or f'call_{index}_{number}'
                arguments = function.get('arguments', {})
                calls.append({'id': call_id, 'type': 'function', 'function': {
                    'name': function['name'], 'arguments': arguments if isinstance(arguments, str)
                    else json.dumps(arguments, ensure_ascii=False, allow_nan=False)}})
                pending.append((call_id, function['name']))
            if calls:
                item['tool_calls'] = calls
        if role == 'tool':
            call_id = message.get('tool_call_id')
            match = next((entry for entry in pending if entry[0] == call_id), None) if call_id else next(
                (entry for entry in pending if entry[1] == message.get('tool_name')), None)
            if match is None:
                raise ValueError('DeepSeek 工具结果缺少匹配的 tool_call_id')
            item['tool_call_id'] = match[0]
            pending.remove(match)
        result.append(item)
    return normalize_data(result)


class DeepSeekClient:
    def __init__(self, model=None, url=None, api_key=None, profile=None):
        self.model = model or config.DEEPSEEK_MODEL
        self.url = (url or config.DEEPSEEK_URL).rstrip('/')
        self.api_key = config.DEEPSEEK_API_KEY if api_key is None else api_key.strip()
        self.profile = profile
        self.last_response_info = {}
        if not self.api_key:
            raise ValueError('请在 .env 中填写 DEEPSEEK_API_KEY；或将 JARVIS_CHAT_PROVIDER 改回 ollama。')

    @staticmethod
    def _trace_delta(delta):
        for source, event in (('reasoning_content', 'model.thinking'), ('content', 'model.content')):
            if delta.get(source):
                trace.emit(event, text=delta[source])

    def _read_response(self, response, streaming):
        if not streaming:
            data = json.load(response)
            choice = data['choices'][0]
            message = choice['message']
            self._trace_delta(message)
            return message, choice['finish_reason'], data.get('usage') or {}
        message = {'role': 'assistant', 'content': '', 'reasoning_content': ''}
        calls, finish, usage, done = {}, None, {}, False
        for line in response:
            line = line.decode('utf-8').strip()
            if not line.startswith('data:'):
                continue  # SSE keepalives and blank event separators.
            value = line[5:].strip()
            if value == '[DONE]':
                done = True
                break
            chunk = json.loads(value)
            if chunk.get('error'):
                raise RuntimeError('DeepSeek 流式响应报错；未执行部分工具调用。')
            usage = chunk.get('usage') or usage
            if not chunk.get('choices'):
                continue
            choice = chunk['choices'][0]
            delta = choice.get('delta') or {}
            self._trace_delta(delta)
            for key in ('content', 'reasoning_content'):
                message[key] += delta.get(key) or ''
            for fragment in delta.get('tool_calls') or []:
                call = calls.setdefault(fragment['index'], {'id': '', 'type': 'function',
                                                            'function': {'name': '', 'arguments': ''}})
                if fragment.get('id'):
                    call['id'] += fragment['id']
                for key in ('name', 'arguments'):
                    call['function'][key] += (fragment.get('function') or {}).get(key) or ''
            finish = choice.get('finish_reason') or finish
        if not done or finish is None:
            raise RuntimeError('DeepSeek stream ended before completion; partial tool calls were not executed')
        if calls:
            message['tool_calls'] = [calls[index] for index in sorted(calls)]
        return message, finish, usage

    @staticmethod
    def _agent_message(message):
        result = {'role': 'assistant', 'content': message.get('content') or '',
                  'reasoning_content': message.get('reasoning_content') or ''}
        calls = []
        for call in message.get('tool_calls') or []:
            function = call['function']
            arguments = function.get('arguments', '{}')
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict) or not call.get('id') or not function.get('name'):
                raise ValueError('Invalid DeepSeek tool call')
            calls.append({'id': call['id'], 'type': 'function', 'function': {
                'name': function['name'], 'arguments': arguments}})
        if calls:
            result['tool_calls'] = calls
            trace.emit('model.tool_calls', tool_calls=calls, stage='proposal_only')
        return result

    def chat(self, messages, tools):
        with trace.scope(model_call_id=uuid.uuid4().hex, profile=self.profile or 'chat', provider='deepseek'):
            try:
                return self._chat(messages, tools)
            except Exception as error:
                trace.emit('model.error', error=str(error).replace(self.api_key, '[REDACTED]'))
                raise

    def _chat(self, messages, tools):
        if not 0 < config.DEEPSEEK_MAX_TOKENS <= config.DEEPSEEK_MAX_RETRY_TOKENS <= 393216:
            raise ValueError('Require 0 < DEEPSEEK_MAX_TOKENS <= DEEPSEEK_MAX_RETRY_TOKENS <= 393216')
        if config.DEEPSEEK_TIMEOUT <= 0:
            raise ValueError('DEEPSEEK_TIMEOUT must be positive')
        autonomous = self.profile == 'autonomy'
        thinking = config.DEEPSEEK_THINK and not autonomous
        budgets = [768 if autonomous else config.DEEPSEEK_MAX_TOKENS]
        if not autonomous and config.DEEPSEEK_MAX_RETRY_TOKENS > budgets[0]:
            budgets.append(config.DEEPSEEK_MAX_RETRY_TOKENS)
        self.last_response_info = {}
        for attempt, budget in enumerate(budgets, 1):
            streaming = trace.enabled() and config.TRACE_STREAM and not autonomous
            payload = {'model': self.model, 'messages': api_messages(messages),
                       'stream': streaming, 'max_tokens': budget,
                       'thinking': {'type': 'enabled' if thinking else 'disabled'}}
            if not thinking:
                payload['temperature'] = 0.2
            if tools:
                payload['tools'] = tools
            request = Request(self.url + '/chat/completions', method='POST',
                data=json.dumps(normalize_data(payload), ensure_ascii=False, allow_nan=False).encode('utf-8'),
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.api_key})
            trace.emit('model.request', provider='deepseek', model=self.model, attempt=attempt,
                       stream=streaming, think=thinking, num_predict=budget, message_count=len(messages),
                       tools=[tool.get('function', {}).get('name') for tool in tools])
            started = time.monotonic()
            try:
                with urlopen(request, timeout=20 if autonomous else config.DEEPSEEK_TIMEOUT) as response:
                    with trace.scope(attempt=attempt):
                        message, finish, usage = self._read_response(response, streaming)
            except HTTPError as error:
                if error.code == 402:
                    raise BalanceUnavailable('DeepSeek HTTP 402：账户余额不足') from None
                detail = error.read().decode('utf-8', errors='replace').replace(self.api_key, '[REDACTED]')[:400]
                hint = {401: '检查 DEEPSEEK_API_KEY', 402: '检查 DeepSeek 账户余额',
                        429: '请求过于频繁，请稍后再试'}.get(error.code, '检查模型名与接口配置')
                raise RuntimeError(f'DeepSeek HTTP {error.code}：{hint}。{detail}') from None
            except TimeoutError:
                raise RuntimeError('DeepSeek 请求超时；可调整 DEEPSEEK_TIMEOUT。') from None
            except URLError:
                raise RuntimeError('无法连接 DeepSeek；请检查网络和 DEEPSEEK_BASE_URL。') from None
            except (KeyError, IndexError, TypeError, ValueError):
                raise RuntimeError('DeepSeek returned an invalid chat response; no tool calls executed') from None
            self.last_response_info = {'done_reason': finish, 'attempts': attempt, 'num_predict': budget,
                'think': thinking, 'thinking_chars': len(message.get('reasoning_content') or ''),
                'prompt_eval_count': usage.get('prompt_tokens'), 'eval_count': usage.get('completion_tokens')}
            trace.emit('model.done', **self.last_response_info, elapsed_seconds=time.monotonic() - started,
                       tool_calls_usable=finish in ('stop', 'tool_calls'))
            if finish == 'length':
                trace.emit('model.discarded', attempt=attempt, reason='generation_length',
                           retry=attempt < len(budgets), tool_calls_executed=False)
                continue
            if finish not in ('stop', 'tool_calls'):
                raise RuntimeError(f'DeepSeek 未正常完成（{finish}）；本轮未执行工具。')
            try:
                return self._agent_message(message)
            except (KeyError, TypeError, ValueError):
                raise RuntimeError('DeepSeek 工具参数无效；本轮未执行工具。') from None
        raise GenerationLimitError(f'DeepSeek 达到生成上限（最多 {budgets[-1]} token），本次未执行截断响应中的工具调用。')
