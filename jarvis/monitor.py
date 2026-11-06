"""Read-only, live local model/thinking/plan/tool event viewer."""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .text_encoding import configure_stdio, normalize_text, safe_json_dumps


def decode(line):
    try:
        record = json.loads(line)
        return record if isinstance(record, dict) and 'event' in record else None
    except (ValueError, UnicodeError):
        return None


class Follower:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.files = {}

    def paths(self):
        return sorted(self.directory.glob('*.jsonl'))

    def initial(self, tail=50):
        records = []
        for path in self.paths():
            handle = path.open('rb')
            handle.seek(0, 2)
            end = handle.tell()
            position, block = end, b''
            while position > 0 and block.count(b'\n') <= tail:
                count = min(8192, position)
                position -= count
                handle.seek(position)
                block = handle.read(count) + block
            if position:
                block = block.split(b'\n', 1)[-1]
            lines = block.split(b'\n')
            self.files[path] = (handle, lines[-1])
            handle.seek(end)
            if tail:
                records.extend(record for line in lines[:-1][-tail:] if (record := decode(line)))
        return sorted(records, key=lambda r: (r.get('time', ''), r.get('pid', 0), r.get('seq', 0)))[-tail:] if tail else []

    def poll(self):
        records = []
        for path in self.paths():
            if path not in self.files:
                self.files[path] = (path.open('rb'), b'')
            handle, pending = self.files[path]
            if path.stat().st_size < handle.tell():
                handle.seek(0)
                pending = b''
            # Bound each read so another busy process cannot starve the viewer.
            data = pending + handle.read(1024 * 1024)
            lines = data.split(b'\n')
            self.files[path] = (handle, lines[-1])
            records.extend(record for line in lines[:-1] if (record := decode(line)))
        return sorted(records, key=lambda r: (r.get('time', ''), r.get('pid', 0), r.get('seq', 0)))

    def close(self):
        for handle, _ in self.files.values():
            handle.close()


LABELS = {
    'session.start': '会话开始', 'session.end': '会话结束', 'chat.input': '用户输入', 'chat.answer': '最终答复', 'chat.error': '聊天错误',
    'agent.round': '决策轮次', 'agent.limit': '工具循环达到上限', 'model.request': '模型请求',
    'model.thinking': '思考', 'model.content': '正文', 'model.tool_calls': '工具提案（尚未执行）',
    'model.done': '模型返回', 'model.discarded': '截断输出已丢弃', 'model.error': '模型错误',
    'tool.start': '开始执行工具', 'tool.result': '工具实际结果', 'tool.error': '工具异常',
    'plan.input': '规划输入', 'plan.proposed': '模型提出计划', 'plan.accepted': '计划已采用',
    'plan.rejected': '计划被拒绝', 'plan.discarded': '旧计划已失效', 'plan.error': '规划错误',
    'action.queued': '动作出队', 'action.discarded': '动作未执行',
    'autonomy.mode': '自主模式', 'autonomy.error': '调度错误', 'world.snapshot': '场景状态',
    'autonomy.idle_wait': '等待对话空闲后恢复',
    'character.context': '共享角色上下文',
    'input.invalid_unicode': '输入编码异常（未写入上下文）',
}


def safe_text(value):
    # Display terminal controls literally; model output remains intact in JSONL.
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', lambda m: f'\\u{ord(m[0]):04x}', normalize_text(str(value)))


class Renderer:
    def __init__(self, output=None, *, raw=False, show_all=False):
        self.output = output or sys.stdout
        self.raw, self.show_all, self.active = raw, show_all, None

    def finish(self):
        if self.active:
            self.output.write('\n')
            self.output.flush()
            self.active = None

    def write(self, record):
        if self.raw:
            self.output.write(safe_json_dumps(record) + '\n')
            self.output.flush()
            return
        event = record['event']
        data, context = record.get('data', {}), record.get('context', {})
        if event == 'world.snapshot' and not self.show_all:
            return
        try:
            stamp = datetime.fromisoformat(record['time']).astimezone(ZoneInfo(config.TIMEZONE)).strftime('%H:%M:%S')
        except (ValueError, KeyError):
            stamp = record.get('time', '')
        source = context.get('source') or context.get('profile') or 'system'
        identity = context.get('model_call_id') or context.get('plan_id') or context.get('turn_id') or ''
        round_text = f' R{context["round"]}' if 'round' in context else ''
        header = f'[{stamp}][{source}{round_text}][{LABELS.get(event, event)}]'
        if identity:
            header += ' #' + str(identity)[:8]
        if event in ('model.thinking', 'model.content'):
            key = (record.get('pid'), identity, data.get('attempt', context.get('attempt')), event)
            if self.active != key:
                self.finish()
                self.output.write(header + '\n')
                self.active = key
            self.output.write(safe_text(data.get('text', '')))
        else:
            self.finish()
            self.output.write(header + '\n')
            if event in ('chat.input', 'chat.answer'):
                body = data.get('text', '')
            elif event == 'plan.input' and not self.show_all:
                snapshot = data.get('snapshot', {})
                body = json.dumps({'character': snapshot.get('character'), 'executing': data.get('executing'),
                                   'pending': data.get('pending'), 'recent': data.get('recent'),
                                   'character_context': data.get('character_context'),
                                   'object_count': len(snapshot.get('objects', [])),
                                   'planning_feedback': data.get('planning_feedback')}, ensure_ascii=False, indent=2)
            else:
                body = json.dumps(data, ensure_ascii=False, indent=2)
            self.output.write(safe_text(body) + '\n')
        self.output.flush()


def main():
    configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default=config.TRACE_DIR)
    parser.add_argument('--tail', type=int, default=50, help='Replay the most recent N events, then follow')
    parser.add_argument('--once', action='store_true', help='Show recent events and exit')
    parser.add_argument('--all', action='store_true', help='Include full automatic world snapshots and planning inputs')
    parser.add_argument('--raw', action='store_true', help='Print complete JSONL records')
    args = parser.parse_args()
    if args.tail < 0:
        parser.error('--tail must be nonnegative')
    follower = Follower(args.directory)
    renderer = Renderer(raw=args.raw, show_all=args.all)
    print('监控目录：' + str(Path(args.directory).resolve()), file=sys.stderr)
    print('显示时区：' + config.TIMEZONE, file=sys.stderr)
    print('等待已更新的 JARVIS 产生事件；Ctrl+C 只退出监控。', file=sys.stderr)
    try:
        for record in follower.initial(args.tail):
            renderer.write(record)
        while not args.once:
            for record in follower.poll():
                renderer.write(record)
            time.sleep(.15)
    except KeyboardInterrupt:
        pass
    finally:
        renderer.finish()
        follower.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
