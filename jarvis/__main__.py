import argparse
from .storage import Store
from .tools import Tools
from .llm import create_chat_client
from .hunger import BalanceHunger
from .agent import Agent
from . import config
from .images import parse_image_command, IMAGE_USAGE
from .character_context import CharacterContext, control_intent, control_parts
from .text_encoding import configure_stdio, normalize_text, InputEncodingError

COMMANDS = 'Commands: /help, /auto on|pause|resume|off|status, /image, /reminders, /kb, /kb-sync, /hunger [refresh], /clear, /quit'

def main():
    configure_stdio()
    parser=argparse.ArgumentParser(description='JARVIS-M1 Phase 1')
    parser.add_argument('--user',default='owner',help='User profile ID (not authentication)')
    automatic=parser.add_mutually_exclusive_group()
    automatic.add_argument('--autonomous', dest='autonomous', action='store_true', help='Enable autonomous desktop actions (default)')
    automatic.add_argument('--no-autonomous', dest='autonomous', action='store_false', help='Start with autonomous actions disabled')
    parser.set_defaults(autonomous=None)
    parser.add_argument('--no-trace', action='store_true', help='Disable local model/tool diagnostic logs')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--check',action='store_true',help='Check selected chat provider, local embeddings and timezone')
    mode.add_argument('--smoke-test',action='store_true',help='Test real model tools with an isolated temporary database')
    mode.add_argument('--search-test',action='store_true',help='Make one real Doubao Custom search (consumes search quota)')
    mode.add_argument('--reindex-knowledge',action='store_true',help='Rebuild the Markdown knowledge base index')
    args=parser.parse_args()
    if args.check:
        from .doctor import check
        return check()
    if args.smoke_test:
        from .smoke import run
        return run()
    if args.search_test:
        from .search import check_search
        return check_search()
    store=Store(config.DB_PATH)
    from .embeddings import EmbeddingClient
    from .knowledge import KnowledgeService, QUERY_INSTRUCTION
    knowledge=KnowledgeService(store,EmbeddingClient(query_instruction=QUERY_INSTRUCTION))
    if args.reindex_knowledge:
        try:
            result=knowledge.sync(force=True)
            print(result)
            return 1 if 'error' in result else 0
        finally:
            store.conn.close()
    from . import trace
    trace.configure(config.TRACE_ENABLED and not args.no_trace, config.TRACE_DIR, export=True)
    auto_enabled=config.AUTO_ENABLED if args.autonomous is None else args.autonomous
    try:
        chat_client = create_chat_client()
        planner_client = create_chat_client(profile='autonomy')
        meter = BalanceHunger() if config.CHAT_PROVIDER == 'deepseek' else None
    except ValueError as error:
        store.conn.close()
        parser.error(str(error))
    trace.emit('session.start', provider=config.CHAT_PROVIDER, model=chat_client.model, autonomous=auto_enabled)
    from .desktop_client import DesktopMCPClient
    desktop=DesktopMCPClient(config.DESKTOP_BRIDGE_URL) if config.DESKTOP_ENABLED else None
    if desktop:
        try: desktop.start()
        except Exception as error:
            print('Desktop MCP unavailable:',error)
            desktop.close();desktop=None
    from .autonomy import InferenceGate, CoordinatedLLM, LocalPlanner, AutonomousController
    gate=InferenceGate()
    shared_context=CharacterContext(hunger=meter)
    autonomy=AutonomousController(desktop,LocalPlanner(planner_client,gate,context=shared_context),
                                  context=shared_context,resume_delay=config.AUTO_RESUME_SECONDS) if desktop else None
    agent=Agent(CoordinatedLLM(chat_client,gate),Tools(store,args.user,knowledge=knowledge,desktop=desktop),context=shared_context)
    if auto_enabled and autonomy: autonomy.enable()
    print(f'JARVIS-M1 | provider={config.CHAT_PROVIDER} | model={chat_client.model} | user={args.user} | timezone={config.TIMEZONE}')
    print(COMMANDS)
    print('图片输入：' + IMAGE_USAGE)
    if autonomy:
        print(f'自主模式：{"默认开启" if auto_enabled else "已关闭"}；对话结束后安静 {config.AUTO_RESUME_SECONDS:g} 秒恢复，明确暂停后保持暂停。')
    if trace.enabled(): print('模型监控：另开 PowerShell 运行 ./monitor.ps1（日志：' + str(trace.recorder().path.parent) + '）')
    unity_chat = None
    if config.DESKTOP_ENABLED:
        from .unity_chat import UnityChat
        try:
            unity_chat = UnityChat()
            print('Unity 对话已就绪 · 在 Game 视图底部输入，点击发送或按 Ctrl + Enter。')
        except OSError as error:
            print('Unity 聊天入口启动失败（请检查是否已有 JARVIS 会话）：', error)
    try:
        interact(agent, autonomy, store, args.user, knowledge, unity_chat)
    finally:
        if unity_chat: unity_chat.close()
        if autonomy: autonomy.close()
        if desktop: desktop.close()
        store.conn.close()
        trace.emit('session.end')


def interact(agent, autonomy, store, user, knowledge, unity_chat=None):
    while True:
        for reminder in store.due_reminders(user):
            print(f"[REMINDER #{reminder['id']}] {reminder['message']}")
        try:
            request_id, raw_prompt = unity_chat.read() if unity_chat else (None, input('\nYou> '))
            prompt=normalize_text(raw_prompt,strict=True).strip()
        except InputEncodingError as error:
            from . import trace
            trace.emit('input.invalid_unicode', text=raw_prompt, error=str(error))
            print('ERROR>',error);continue
        except (EOFError,KeyboardInterrupt): print();break
        if not prompt: continue
        if prompt in ('/quit','/exit'): break
        if prompt in ('/hunger', '/hunger refresh'):
            meter = agent.context.hunger
            if meter is None:
                print('本地模型模式，余额饥饿度未启用。')
            else:
                state = meter.refresh(force=prompt.endswith(' refresh'))
                labels = {'unknown': '暂时查不到余额', 'empty': '余额不足', 'hungry': '有些饿了',
                          'normal': '口粮够用', 'sated': '吃饱了'}
                score = state['value_01']
                print('脑力口粮 · ' + labels[state['level']] +
                      (f'（饥饿度 {score:.0%}）' if score is not None else ''))
            continue
        if prompt.split(maxsplit=1)[0]=='/auto':
            command=prompt.split(maxsplit=1)[1].strip() if ' ' in prompt else 'status'
            if not autonomy:
                print('Desktop MCP unavailable.');continue
            try:
                if command in ('on','resume'): autonomy.enable()
                elif command=='pause': autonomy.pause()
                elif command=='off': autonomy.pause(off=True)
                elif command!='status':
                    print('Usage: /auto on|pause|resume|off|status');continue
                print(autonomy.status())
            except Exception as error: print('ERROR>',error)
            continue
        if prompt=='/help':
            print(COMMANDS);print('图片输入：' + IMAGE_USAGE);continue
        if not prompt.startswith('/') and autonomy:
            intent=control_intent(prompt)
            if intent:
                try:
                    if intent=='on':
                        autonomy.enable()
                        answer='已恢复自主活动。'
                    else:
                        result=autonomy.request_stop(off=intent=='off')
                        answer='自主模式已关闭。' if intent=='off' else '自主行动已暂停，会保持暂停，直到你让我继续。'
                        if result.get('state')!='succeeded':
                            answer+=' Unity 停止尚未确认：'+str(result.get('error',result.get('state','unknown')))
                    # A compound request still receives a normal answer after control is applied.
                    _,remainder=control_parts(prompt)
                    if not remainder or remainder in ('谢谢','谢谢你'):
                        agent.note_exchange(prompt,answer)
                        if unity_chat: unity_chat.finish(request_id, answer)
                        print('JARVIS>',answer);continue
                except Exception as error:
                    if unity_chat: unity_chat.finish(request_id, error=str(error))
                    print('ERROR>',error);continue
        if prompt.split(maxsplit=1)[0]=='/image':
            try:
                path, question=parse_image_command(prompt)
                answer = respond(agent,autonomy,question,image_paths=[path])
                if unity_chat: unity_chat.finish(request_id, answer)
                print('JARVIS>',answer)
            except Exception as e: print('ERROR>',e)
            continue
        if prompt=='/clear':
            try:
                if autonomy: autonomy.begin_chat()
                agent.clear();print('Shared character context cleared.')
            finally:
                if autonomy: autonomy.end_chat()
            continue
        if prompt=='/reminders':
            for x in store.list_reminders(user): print(f"#{x['id']} {x['due_at']} {x['message']}")
            continue
        if prompt=='/kb':
            print(knowledge.status());continue
        if prompt=='/kb-sync':
            print(knowledge.sync());continue
        if prompt.startswith('/'):
            print('Unknown command. Use /help to see available commands.');continue
        try:
            answer = respond(agent,autonomy,prompt)
            if unity_chat: unity_chat.finish(request_id, answer)
            print('JARVIS>',answer)
        except Exception as e:
            if unity_chat: unity_chat.finish(request_id, error=str(e))
            print('ERROR>',e)


def respond(agent, autonomy, prompt, **kwargs):
    try:
        if autonomy: autonomy.begin_chat()
        return agent.reply(prompt,**kwargs)
    finally:
        if autonomy: autonomy.end_chat()

if __name__=='__main__': raise SystemExit(main())
