import os
import tempfile
import unittest
from pathlib import Path

from jarvis.knowledge import KnowledgeService, chunk_markdown, resolve_root
from jarvis.agent import Agent
from jarvis.storage import Store
from jarvis.tools import Tools


class FakeEmbedder:
    model = 'knowledge-test:v1'

    def __init__(self):
        self.documents = []

    def embed_documents(self, texts):
        self.documents.extend(texts)
        return [[1, 0, 0] if '暖黄' in text else [0, 1, 0] if '京剧' in text
                else [0, 0, 1] for text in texts]

    def embed_query(self, query):
        return [1, 0, 0] if '照明' in query else [0, 1, 0] if '戏曲' in query else [-1, -1, -1]


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(':memory:')
        self.embedder = FakeEmbedder()
        self.knowledge = KnowledgeService(self.store, self.embedder, self.root, top_k=2, min_score=0.5)

    def tearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_semantic_query_returns_source_and_original_line_range(self):
        text = '# 家庭说明\n\n## 阅读环境\n使用暖黄色灯光。\n'
        self.write('居家/说明.md', text)
        result = self.knowledge.search('照明喜好')
        self.assertEqual(result['retrieval'], 'semantic')
        chunk = result['chunks'][0]
        self.assertEqual(chunk['source'], '居家/说明.md')
        self.assertEqual(chunk['heading'], '家庭说明 > 阅读环境')
        self.assertEqual((chunk['start_line'], chunk['end_line']), (3, 4))
        self.assertEqual(chunk['citation'], '居家/说明.md:L3-L4')
        self.assertIn(chunk['content'], '\n'.join(text.splitlines()[2:4]))

    def test_unchanged_files_are_not_embedded_again(self):
        self.write('a.md', '# 灯具\n暖黄色灯光')
        self.knowledge.search('照明')
        calls = len(self.embedder.documents)
        result = self.knowledge.sync()
        self.assertEqual(result['indexed_files'], 0)
        self.assertEqual(result['unchanged_files'], 1)
        self.knowledge.search('照明')
        self.assertEqual(len(self.embedder.documents), calls)

    def test_edit_replaces_old_text_and_vectors(self):
        self.write('a.md', '# 说明\n暖黄色灯光')
        self.assertTrue(self.knowledge.search('照明')['chunks'])
        self.write('a.md', '# 说明\n喜欢京剧')
        self.assertEqual(self.knowledge.search('照明')['chunks'], [])
        self.assertIn('京剧', self.knowledge.search('戏曲')['chunks'][0]['content'])
        rows = self.store.indexed_knowledge_chunks(str(self.root), self.embedder.model)
        self.assertFalse(any('暖黄' in row['content'] for row in rows))

    def test_delete_removes_file_and_chunks(self):
        path = self.write('a.md', '# 灯具\n暖黄色灯光')
        self.knowledge.sync()
        path.unlink()
        self.assertEqual(self.knowledge.search('照明')['chunks'], [])
        self.assertEqual(self.knowledge.status()['files'], 0)
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM knowledge_chunks').fetchone()[0], 0)

    def test_rename_updates_citations_and_removes_old_path(self):
        path = self.write('a.md', '# 灯具\n暖黄色灯光')
        self.knowledge.sync()
        path.rename(self.root / 'b.md')
        result = self.knowledge.search('照明')
        self.assertEqual({chunk['source'] for chunk in result['chunks']}, {'b.md'})
        self.assertEqual(self.knowledge.status()['files'], 1)

    def test_empty_file_removes_previously_indexed_content(self):
        self.write('a.md', '# 灯具\n暖黄色灯光')
        self.knowledge.sync()
        self.write('a.md', '')
        self.assertEqual(self.knowledge.search('照明')['chunks'], [])
        self.assertEqual(self.knowledge.status()['files'], 1)
        self.assertEqual(self.knowledge.status()['chunks'], 0)

    def test_failed_sync_does_not_publish_partial_updates_or_deletions(self):
        self.write('a.md', '# 灯具\n暖黄色灯光')
        b = self.write('b.md', '# 戏曲\n京剧')
        self.knowledge.sync()
        self.write('a.md', '# 新资料\n京剧')
        b.unlink()
        self.write('c.md', '# 故障\n模拟故障')

        class FailingEmbedder(FakeEmbedder):
            def embed_documents(self, texts):
                if any('故障' in text for text in texts):
                    raise RuntimeError('embedding unavailable')
                return super().embed_documents(texts)

        failed = KnowledgeService(self.store, FailingEmbedder(), self.root)
        result = failed.search('照明')
        self.assertIn('error', result)
        self.assertEqual(result['chunks'], [])
        self.assertEqual({row['relative_path'] for row in self.store.knowledge_files(str(self.root))}, {'a.md', 'b.md'})
        self.assertTrue(any('暖黄' in row['content'] for row in self.store.indexed_knowledge_chunks(
            str(self.root), self.embedder.model)))
        (self.root / 'c.md').unlink()
        self.assertNotIn('error', self.knowledge.sync())
        self.assertEqual(self.knowledge.search('照明')['chunks'], [])

    def test_missing_root_does_not_erase_cached_records(self):
        path = self.write('a.md', '# 灯具\n暖黄色灯光')
        self.knowledge.sync()
        path.unlink()
        self.root.rmdir()
        result = self.knowledge.search('照明')
        self.assertIn('error', result)
        self.assertEqual(result['chunks'], [])
        self.assertEqual(self.knowledge.status()['files'], 1)

    def test_invalid_utf8_does_not_publish_an_incomplete_scan(self):
        self.write('a.md', '# 灯具\n暖黄色灯光')
        self.knowledge.sync()
        (self.root / 'bad.md').write_bytes(b'\xff\xfe\xfa')
        self.assertIn('error', self.knowledge.search('照明'))
        self.assertEqual(self.knowledge.status()['files'], 1)

    def test_oversized_file_is_reported(self):
        (self.root / 'large.md').write_bytes(b'x' * (2 * 1024 * 1024 + 1))
        result = self.knowledge.sync()
        self.assertIn('exceeds 2 MiB', result['error'])
        self.assertEqual(self.knowledge.status()['files'], 0)

    def test_nested_markdown_and_uppercase_extension_ignore_hidden_and_other_files(self):
        self.write('nested/a.MD', '# 灯具\n暖黄色灯光')
        self.write('plain.txt', 'not Markdown')
        self.write('.private.md', 'hidden')
        self.write('.obsidian/hidden.md', 'hidden')
        result = self.knowledge.sync()
        self.assertEqual(result['files'], 1)
        self.assertEqual(self.store.knowledge_files(str(self.root))[0]['relative_path'], 'nested/a.MD')

    def test_symlinks_are_not_followed(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside)
            (target / 'secret.md').write_text('暖黄', encoding='utf-8')
            try:
                os.symlink(target / 'secret.md', self.root / 'link.md')
                os.symlink(target, self.root / 'linked-directory', target_is_directory=True)
            except OSError as exc:
                self.skipTest(str(exc))
            self.assertEqual(self.knowledge.sync()['files'], 0)

    def test_model_change_and_force_rebuild_refresh_index(self):
        self.write('a.md', '# 灯具\n暖黄色灯光')
        self.knowledge.sync()
        self.embedder.model = 'knowledge-test:v2'
        self.assertEqual(self.knowledge.sync()['indexed_files'], 1)
        self.assertEqual(self.knowledge.sync()['indexed_files'], 0)
        self.assertEqual(self.knowledge.sync(force=True)['indexed_files'], 1)
        self.assertTrue(self.knowledge.search('照明')['chunks'])

    def test_empty_and_unrelated_queries_do_not_return_recent_records(self):
        self.write('a.md', '# 火箭\n暖黄色灯光')
        self.assertIn('error', self.knowledge.search('  '))
        self.assertEqual(self.knowledge.search('火箭')['chunks'], [])

    def test_top_k_limits_context_and_root_filter_precedes_ranking(self):
        for i in range(4):
            self.write(f'{i}.md', '# 灯具\n暖黄色灯光')
        with tempfile.TemporaryDirectory() as outside:
            (Path(outside) / 'outside.md').write_text('# 灯具\n暖黄色灯光', encoding='utf-8')
            KnowledgeService(self.store, self.embedder, outside).sync()
            result = self.knowledge.search('照明')
            self.assertEqual(len(result['chunks']), 2)
            self.assertFalse(any(chunk['source'] == 'outside.md' for chunk in result['chunks']))

    def test_query_embedding_failure_has_no_fallback(self):
        self.write('a.md', '# 灯具\n暖黄色灯光')
        self.knowledge.sync()

        class QueryFailure(FakeEmbedder):
            def embed_query(self, query):
                raise RuntimeError('query embedding unavailable')

        result = KnowledgeService(self.store, QueryFailure(), self.root).search('照明')
        self.assertIn('error', result)
        self.assertEqual(result['chunks'], [])

    def test_edit_during_embedding_prevents_obsolete_snapshot_commit(self):
        path = self.write('a.md', '# 灯具\n暖黄色灯光')

        class EditingEmbedder(FakeEmbedder):
            def embed_documents(self, texts):
                vectors = super().embed_documents(texts)
                path.write_text('# 戏曲\n喜欢京剧', encoding='utf-8')
                return vectors

        result = KnowledgeService(self.store, EditingEmbedder(), self.root).sync()
        self.assertIn('changed during indexing', result['error'])
        self.assertEqual(self.knowledge.status()['files'], 0)

    def test_persistent_index_is_reused_after_database_reopen(self):
        self.write('a.md', '# 灯具\n暖黄色灯光')
        db = str(self.root / 'index.db')
        store = Store(db)
        try:
            KnowledgeService(store, self.embedder, self.root).sync()
        finally:
            store.conn.close()
        count = len(self.embedder.documents)
        reopened = Store(db)
        try:
            service = KnowledgeService(reopened, self.embedder, self.root)
            self.assertEqual(service.sync()['unchanged_files'], 1)
            self.assertEqual(len(self.embedder.documents), count)
            self.assertTrue(service.search('照明')['chunks'])
        finally:
            reopened.conn.close()

    def test_tools_route_only_to_configured_knowledge_service(self):
        self.write('a.md', '# 灯具\n暖黄色灯光')
        result = Tools(self.store, knowledge=self.knowledge).run('search_knowledge', {'query': '照明'})
        self.assertTrue(result['chunks'])
        self.assertIn('error', Tools(self.store).run('search_knowledge', {'query': '照明'}))

    def test_windows_directory_mapping_in_wsl(self):
        if os.name != 'nt':
            self.assertEqual(str(resolve_root(r'C:\Notes\知识库')), '/mnt/c/Notes/知识库')

    def test_empty_model_answer_displays_knowledge_source_and_excerpt(self):
        self.write('a.md', '# 灯具\n暖黄色灯光')

        class EmptySummaryLLM:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, tools):
                self.calls += 1
                if self.calls == 1:
                    return {'role': 'assistant', 'content': '', 'tool_calls': [
                        {'function': {'name': 'search_knowledge', 'arguments': {'query': '照明'}}}]}
                return {'role': 'assistant', 'content': ''}

        answer = Agent(EmptySummaryLLM(), Tools(self.store, knowledge=self.knowledge)).reply('查询照明资料')
        self.assertIn('a.md:L1-L2', answer)
        self.assertIn('暖黄色灯光', answer)
        self.assertNotIn('/memories', answer)

    def test_nonempty_answer_retains_sources_when_model_omits_citations(self):
        self.write('居家/说明.md', '# 灯具\n暖黄色灯光')

        class MissingCitationLLM:
            def chat(self, messages, tools):
                if messages[-1]['role'] == 'user':
                    return {'role': 'assistant', 'content': '', 'tool_calls': [
                        {'function': {'name': 'search_knowledge', 'arguments': {'query': '照明'}}}]}
                return {'role': 'assistant', 'content': '资料建议使用暖黄色灯光。'}

        agent = Agent(MissingCitationLLM(), Tools(self.store, knowledge=self.knowledge))
        answer = agent.reply('照明方案')
        self.assertIn('资料建议使用暖黄色灯光。', answer)
        self.assertIn('本轮知识库检索来源', answer)
        self.assertEqual(answer.count('居家/说明.md:L1-L2'), 1)
        self.assertEqual(agent.history[-1]['content'], answer)

    def test_existing_knowledge_citation_is_not_appended_again(self):
        self.write('a.md', '# 灯具\n暖黄色灯光')

        class CitingLLM:
            def chat(self, messages, tools):
                if messages[-1]['role'] == 'user':
                    return {'role': 'assistant', 'content': '', 'tool_calls': [
                        {'function': {'name': 'search_knowledge', 'arguments': {'query': '照明'}}}]}
                return {'role': 'assistant', 'content': '使用暖黄色灯光。来源：a.md:L1-L2'}

        answer = Agent(CitingLLM(), Tools(self.store, knowledge=self.knowledge)).reply('照明方案')
        self.assertEqual(answer, '使用暖黄色灯光。来源：a.md:L1-L2')

    def test_knowledge_citations_do_not_leak_into_a_later_turn(self):
        self.write('a.md', '# 灯具\n暖黄色灯光')

        class FollowupLLM:
            def chat(self, messages, tools):
                if messages[-1].get('content') == '谢谢':
                    return {'role': 'assistant', 'content': '不客气。'}
                if messages[-1]['role'] == 'user':
                    return {'role': 'assistant', 'content': '', 'tool_calls': [
                        {'function': {'name': 'search_knowledge', 'arguments': {'query': '照明'}}}]}
                return {'role': 'assistant', 'content': '使用暖黄色灯光。'}

        agent = Agent(FollowupLLM(), Tools(self.store, knowledge=self.knowledge))
        self.assertIn('a.md:L1-L2', agent.reply('照明方案'))
        self.assertEqual(agent.reply('谢谢'), '不客气。')


class MarkdownChunkTests(unittest.TestCase):
    def test_headings_inside_fenced_code_do_not_split_sections(self):
        text = '# 文档\n说明\n## 操作\n```markdown\n# 代码里的标题\n```\n正文\n## 其他\n结束\n'
        chunks = chunk_markdown(text, 'guide.md')
        self.assertEqual([chunk['heading'] for chunk in chunks], ['文档', '文档 > 操作', '文档 > 其他'])
        self.assertEqual(chunks[1]['start_line'], 3)
        self.assertEqual(chunks[1]['end_line'], 7)
        self.assertIn('# 代码里的标题', chunks[1]['content'])

    def test_long_windows_have_bounded_size_and_overlap(self):
        chunks = chunk_markdown('abcdefghij' * 300, 'long.md', chunk_chars=300, overlap=50)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk['content']) <= 300 for chunk in chunks))
        for left, right in zip(chunks, chunks[1:]):
            self.assertEqual(left['content'][-50:], right['content'][:50])
        self.assertTrue(all(chunk['start_line'] == chunk['end_line'] == 1 for chunk in chunks))

    def test_crlf_and_blank_lines_keep_correct_source_lines(self):
        chunks = chunk_markdown('\r\n# 标题\r\n\r\n正文\r\n', 'doc.md')
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0]['start_line'], chunks[0]['end_line']), (2, 4))
        self.assertEqual(chunks[0]['title'], '标题')


if __name__ == '__main__':
    unittest.main()
