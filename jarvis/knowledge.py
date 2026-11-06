"""Editable Markdown directory with an incremental, local, vector-only RAG index."""
import hashlib
import json
import math
import os
import re
import sqlite3
from pathlib import Path

from . import config
from .embeddings import normalize


QUERY_INSTRUCTION = 'Given a question, retrieve relevant passages from Markdown documents that answer the question.'
CHUNK_CHARS = 1000
OVERLAP_CHARS = 150
CHUNKER_VERSION = f'markdown-v1:{CHUNK_CHARS}:{OVERLAP_CHARS}'
MAX_FILE_BYTES = 2 * 1024 * 1024


def resolve_root(directory):
    value = str(directory)
    if os.name != 'nt' and re.match(r'^[A-Za-z]:[\\/]', value):
        value = '/mnt/' + value[0].lower() + '/' + value[3:].replace('\\', '/')
    return Path(value).expanduser().resolve()


def chunk_markdown(text, filename, chunk_chars=CHUNK_CHARS, overlap=OVERLAP_CHARS):
    """ATX heading-aware windows. Citation lines always refer to original text."""
    if chunk_chars < 2 or not 0 <= overlap < chunk_chars:
        raise ValueError('Invalid Markdown chunk size or overlap')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.splitlines(keepends=True)
    title = Path(filename).stem
    has_title = False
    headings, sections, buffer = [], [], []
    start_line, fence = 1, None
    for number, line in enumerate(lines, 1):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1]:
                fence = None
            heading = None
        else:
            if marker:
                fence = (marker[1][0], len(marker[1]))
            heading = None if marker else re.match(r'^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?\s*$', line)
        if heading:
            if buffer:
                sections.append((start_line, ' > '.join(item[1] for item in headings), ''.join(buffer)))
            level, label = len(heading[1]), heading[2].strip()
            headings = [item for item in headings if item[0] < level] + [(level, label)]
            if level == 1 and not has_title:
                title, has_title = label, True
            buffer, start_line = [], number
        buffer.append(line)
    if buffer:
        sections.append((start_line, ' > '.join(item[1] for item in headings), ''.join(buffer)))

    chunks = []
    for first_line, heading, body in sections:
        start = 0
        while start < len(body):
            end = min(start + chunk_chars, len(body))
            if end < len(body):
                boundary = body.rfind('\n\n', start + chunk_chars // 2, end)
                if boundary < 0:
                    boundary = body.rfind('\n', start + chunk_chars // 2, end)
                if boundary >= 0:
                    end = boundary + 1
            window = body[start:end]
            content = window.strip()
            if content:
                leading = len(window) - len(window.lstrip())
                actual_start = start + leading
                actual_end = start + len(window.rstrip())
                chunks.append({'title': title, 'heading': heading,
                               'start_line': first_line + body[:actual_start].count('\n'),
                               'end_line': first_line + body[:actual_end].count('\n'),
                               'content': content})
            if end == len(body):
                break
            start = max(start + 1, end - overlap)
    return chunks


class KnowledgeService:
    def __init__(self, store, embedder, directory=None, top_k=None, min_score=None):
        self.store, self.embedder = store, embedder
        self.root = resolve_root(config.KNOWLEDGE_DIR if directory is None else directory)
        self.top_k = config.KNOWLEDGE_TOP_K if top_k is None else top_k
        self.min_score = config.KNOWLEDGE_MIN_SCORE if min_score is None else min_score
        if not 1 <= self.top_k <= 20:
            raise ValueError('Knowledge top_k must be between 1 and 20')
        if not math.isfinite(self.min_score) or not -1 <= self.min_score <= 1:
            raise ValueError('Knowledge min_score must be between -1 and 1')

    def _scan(self):
        if not self.root.is_dir():
            raise FileNotFoundError(f'Knowledge directory does not exist: {self.root}')

        def fail(error):
            raise error

        documents = []
        for directory, folders, names in os.walk(self.root, followlinks=False, onerror=fail):
            parent = Path(directory)
            folders[:] = sorted(name for name in folders if not name.startswith('.')
                                and not (parent / name).is_symlink()
                                and (parent / name).resolve().is_relative_to(self.root))
            for name in sorted(names):
                path = parent / name
                if name.startswith('.') or path.suffix.lower() != '.md' or path.is_symlink():
                    continue
                if not path.resolve().is_relative_to(self.root):
                    continue
                with path.open('rb') as handle:
                    raw = handle.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES:
                    raise ValueError(f'Markdown file exceeds 2 MiB: {path.relative_to(self.root)}')
                documents.append({'path': path.relative_to(self.root).as_posix(),
                                  'hash': hashlib.sha256(raw).hexdigest(),
                                  'text': raw.decode('utf-8-sig')})
        return documents

    def status(self):
        return {'root': str(self.root), 'model': self.embedder.model,
                **self.store.knowledge_stats(str(self.root), self.embedder.model)}

    def sync(self, force=False):
        try:
            documents = self._scan()
            previous = {row['relative_path']: row for row in self.store.knowledge_files(str(self.root))}
            manifest = {document['path']: document['hash'] for document in documents}
            changed, unchanged = [], 0
            for document in documents:
                old = previous.get(document['path'])
                if (not force and old and old['content_hash'] == document['hash']
                        and old['model'] == self.embedder.model and old['chunker_version'] == CHUNKER_VERSION):
                    unchanged += 1
                    continue
                chunks = chunk_markdown(document['text'], document['path'])
                dimension = None
                for start in range(0, len(chunks), 32):
                    batch = chunks[start:start + 32]
                    inputs = [f'Document: {document["path"]}\nTitle: {chunk["title"]}\n'
                              f'Heading: {chunk["heading"]}\n\n{chunk["content"]}' for chunk in batch]
                    vectors = self.embedder.embed_documents(inputs)
                    if len(vectors) != len(batch):
                        raise ValueError('Knowledge embedding response count does not match chunk count')
                    for chunk, vector in zip(batch, vectors):
                        vector = normalize(vector)
                        if dimension is not None and len(vector) != dimension:
                            raise ValueError('Knowledge embedding dimensions do not match')
                        dimension = len(vector)
                        chunk['vector'] = vector
                changed.append({'path': document['path'], 'hash': document['hash'],
                                'model': self.embedder.model, 'version': CHUNKER_VERSION, 'chunks': chunks})
            deleted = set(previous) - set(manifest)
            if changed or deleted:
                # Editing while embedding must not publish a mixed or already-obsolete snapshot.
                latest = {document['path']: document['hash'] for document in self._scan()}
                if latest != manifest:
                    raise RuntimeError('Knowledge files changed during indexing; retry synchronization')
                self.store.sync_knowledge(str(self.root), changed, deleted)
            return {**self.status(), 'indexed_files': len(changed), 'unchanged_files': unchanged,
                    'deleted_files': len(deleted)}
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            return {'root': str(self.root), 'error': f'Knowledge synchronization failed: {exc}'}

    def search(self, query):
        if not isinstance(query, str) or not query.strip():
            return {'chunks': [], 'retrieval': 'semantic', 'error': 'A nonempty knowledge query is required'}
        synced = self.sync()
        if 'error' in synced:
            return {'chunks': [], 'retrieval': 'semantic', 'error': synced['error']}
        try:
            rows = self.store.indexed_knowledge_chunks(str(self.root), self.embedder.model)
            if not rows:
                return {'chunks': [], 'retrieval': 'semantic'}
            query_vector = normalize(self.embedder.embed_query(query.strip()))
            ranked = []
            for row in rows:
                vector = normalize(json.loads(row['vector']))
                if len(vector) != len(query_vector):
                    raise ValueError('Knowledge index dimension changed; run --reindex-knowledge')
                score = sum(a * b for a, b in zip(query_vector, vector))
                if score >= self.min_score:
                    chunk = {key: row[key] for key in
                             ('title', 'heading', 'start_line', 'end_line', 'content', 'indexed_at')}
                    chunk['source'] = row['relative_path']
                    chunk['citation'] = f'{row["relative_path"]}:L{row["start_line"]}-L{row["end_line"]}'
                    chunk['similarity'] = round(score, 4)
                    ranked.append((score, row['relative_path'], row['chunk_index'], chunk))
            ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
            return {'chunks': [item[3] for item in ranked[:self.top_k]], 'retrieval': 'semantic'}
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            return {'chunks': [], 'retrieval': 'semantic', 'error': f'Knowledge search unavailable: {exc}'}
