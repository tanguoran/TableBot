"""Local embeddings via Ollama; no Python package dependencies."""
import json
import math
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import config


def normalize(vector):
    if (not isinstance(vector, list) or not vector
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) for value in vector)):
        raise ValueError('Invalid embedding vector')
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError('Embedding vector must have a finite nonzero norm')
    return [value / norm for value in vector]


class EmbeddingClient:
    def __init__(self, model=None, url=None, query_instruction=None):
        self.model = model or config.EMBED_MODEL
        self.url = (url or config.EMBED_URL).rstrip('/')
        self.query_instruction = query_instruction or (
            'Given a question, retrieve relevant passages from Markdown documents that answer the question.')

    def embed_documents(self, texts):
        if not texts:
            return []
        payload = {'model': self.model, 'input': texts, 'truncate': False}
        request = Request(self.url + '/api/embed',
                          data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                          headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urlopen(request, timeout=120) as response:
                vectors = json.load(response)['embeddings']
        except HTTPError as exc:
            detail = exc.read().decode(errors='replace')[:300]
            raise RuntimeError(f'Embedding HTTP {exc.code}: {detail}') from exc
        except URLError as exc:
            raise RuntimeError('Cannot connect to Ollama embedding service') from exc
        except (KeyError, ValueError) as exc:
            raise RuntimeError('Invalid embedding response from Ollama') from exc
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError('Embedding response count does not match input count')
        vectors = [normalize(vector) for vector in vectors]
        if len({len(vector) for vector in vectors}) != 1:
            raise ValueError('Embedding response has inconsistent dimensions')
        return vectors

    def embed_query(self, query):
        if self.model.startswith('qwen3-embedding'):
            query = 'Instruct: ' + self.query_instruction + '\nQuery: ' + query
        return self.embed_documents([query])[0]
