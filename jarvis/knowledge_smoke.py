"""Selected chat model plus local embeddings, temporary Markdown and an in-memory index."""
import tempfile
from pathlib import Path

from .agent import Agent
from .embeddings import EmbeddingClient
from .knowledge import KnowledgeService, QUERY_INSTRUCTION
from .llm import create_chat_client
from .storage import Store
from .tools import Tools


def run():
    store = Store(':memory:')
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '居家').mkdir()
            path = root / '居家' / '照明.md'
            path.write_text('# 阅读环境\n\n## 夜间看书\n演示资料建议使用暖黄色台灯，避免刺眼的冷白光直射眼睛。\n', encoding='utf-8')
            (root / '音乐.md').write_text('# 戏曲爱好\n演示人物喜欢京剧，尤其是《贵妃醉酒》。\n', encoding='utf-8')
            knowledge = KnowledgeService(store, EmbeddingClient(query_instruction=QUERY_INSTRUCTION), root)
            query = '根据资料，晚上阅读应该选择什么照明？'
            result = knowledge.search(query)
            if 'error' in result or not result['chunks'] or result['chunks'][0]['source'] != '居家/照明.md':
                raise AssertionError(f'Expected lighting document: {result}')
            print(f'PASS: semantic retrieval -> {result["chunks"][0]["citation"]}', flush=True)
            if knowledge.sync()['indexed_files'] != 0:
                raise AssertionError('Unchanged files were reindexed')

            class RecordingTools(Tools):
                def __init__(self):
                    super().__init__(store, knowledge=knowledge)
                    self.results = []
                    self.calls = []

                def run(self, name, args):
                    self.calls.append(name)
                    if name != 'search_knowledge':
                        return {'error': 'Only local knowledge search is permitted in this test'}
                    response = super().run(name, args)
                    self.results.append(response)
                    return response

            tools = RecordingTools()
            answer = Agent(create_chat_client(), tools).reply(
                query + ' 请检索知识库后回答，原样附上检索结果的 citation 字段作为来源。')
            if not tools.results or '暖黄' not in answer or '居家/照明.md:L' not in answer:
                raise AssertionError(f'Document-grounded answer failed: {answer}')
            print('PASS: selected chat model invoked search_knowledge and cited the Markdown source.', flush=True)

            (root / '设定').mkdir()
            (root / '设定' / '聚落.md').write_text(
                '# 蓝风镇\n蓝风镇是演示世界观中的虚构聚落，位于银松谷南部。'
                '镇长名为米尔，镇民以种植蓝莓和木雕为生。\n', encoding='utf-8')
            place_tools = RecordingTools()
            place_answer = Agent(create_chat_client(), place_tools).reply('蓝风镇的情报')
            if (not place_tools.calls or place_tools.calls[0] != 'search_knowledge'
                    or 'search_web' in place_tools.calls or '银松谷' not in place_answer
                    or '设定/聚落.md:L' not in place_answer):
                raise AssertionError(f'Unqualified place question failed: {place_tools.calls}; {place_answer}')
            print('PASS: a short place question uses local knowledge first, without web search.', flush=True)

            path.write_text('# 阅读环境\n\n## 最新方案\n演示资料已更新为使用可调亮度的中性白台灯。\n', encoding='utf-8')
            updated = knowledge.search(query)
            if 'error' in updated or not any('中性白' in chunk['content'] for chunk in updated['chunks']):
                raise AssertionError(f'Edited document was not retrieved: {updated}')
            if any('暖黄' in chunk['content'] for chunk in updated['chunks']):
                raise AssertionError('Old text remained in retrieval after edit')
            print('PASS: edits are automatically synchronized.', flush=True)
            path.unlink()
            deleted = knowledge.search(query)
            if 'error' in deleted or any(chunk['source'] == '居家/照明.md' for chunk in deleted['chunks']):
                raise AssertionError('Deleted Markdown file remained retrievable')
            print('PASS: deletion removes cached chunks; temporary files and index discarded.', flush=True)
        return 0
    except (AssertionError, OSError, RuntimeError, ValueError) as exc:
        print(f'FAIL: {exc}', flush=True)
        return 1
    finally:
        store.conn.close()


if __name__ == '__main__':
    raise SystemExit(run())
