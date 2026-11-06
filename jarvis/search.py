"""Doubao Search Custom REST adapter; uses only the Python standard library."""
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urlsplit
from urllib.request import Request, urlopen

from . import config


SEARCH_URL = 'https://open.feedcoopapi.com/search_api/web_search'
TIME_RANGES = ('OneDay', 'OneWeek', 'OneMonth', 'OneYear')
MAX_RESULTS = 5
MAX_CONTENT_CHARS = 1000
ERROR_HINTS = {
    '10400': 'Invalid search parameters.',
    '10401': 'Invalid authentication token.',
    '10402': 'Web search type is unavailable; enable Custom web search.',
    '10403': 'Search permission denied; check the key and enabled search service.',
    '10406': 'Free search quota exhausted; check usage and billing in the console.',
    '10408': 'The search service has not been activated for this account.',
    '10409': 'This key or subscription does not support Custom web search.',
    '10410': 'No active search subscription is available for this key.',
    '10412': 'Search subscription quota exhausted.',
    '700429': 'Search rate limit exceeded; try again later.',
    '10500': 'Doubao search service error; try again later.',
    '401': 'Search authentication failed; check DOUBAO_SEARCH_API_KEY.',
    '403': 'Search permission denied; check the key and enabled search service.',
    '429': 'Search rate limit exceeded; try again later.',
}


class DoubaoSearchClient:
    def __init__(self, key=None):
        self.key = (config.SEARCH_API_KEY if key is None else key).strip()

    def _safe(self, value, limit=300):
        text = str(value)
        if self.key:
            text = text.replace(self.key, '[redacted]')
        return text[:limit]

    def _error(self, code, message, request_id=None):
        result = {'provider': 'doubao', 'error_code': self._safe(code, 80),
                  'error': self._safe(message)}
        if request_id:
            result['request_id'] = self._safe(request_id, 100)
        return result

    def _api_error(self, data):
        if not isinstance(data, dict):
            return None
        metadata = data.get('ResponseMetadata')
        if not isinstance(metadata, dict) or not metadata.get('Error'):
            return None
        error = metadata['Error']
        if not isinstance(error, dict):
            return self._error('invalid_response', 'Malformed Doubao search error response.')
        code = str(error.get('Code') or error.get('CodeN') or 'unknown')
        message = ERROR_HINTS.get(code) or f'Doubao search error {code}: {error.get("Message", "unknown error")}'
        return self._error(code, message, metadata.get('RequestId'))

    def search(self, query, time_range=None):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
            return self._error('invalid_arguments', 'Search query must contain 1 to 100 characters.')
        if time_range is not None and time_range not in TIME_RANGES:
            return self._error('invalid_arguments', 'Unsupported time_range; use OneDay, OneWeek, OneMonth or OneYear.')
        if not self.key:
            return self._error('missing_api_key', 'DOUBAO_SEARCH_API_KEY not configured; web search unavailable.')
        payload = {
            'Query': query.strip(), 'SearchType': 'web', 'Count': MAX_RESULTS,
            'Filter': {'NeedUrl': True, 'NeedContent': False},
            'QueryControl': {'QueryRewrite': False}, 'ContentFormats': 'text',
        }
        if time_range is not None:
            payload['TimeRange'] = time_range
        request = Request(SEARCH_URL, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                          headers={'Content-Type': 'application/json',
                                   'Authorization': 'Bearer ' + self.key}, method='POST')
        try:
            with urlopen(request, timeout=20) as response:
                data = json.load(response)
        except HTTPError as exc:
            try:
                error = self._api_error(json.loads(exc.read(65536)))
                if error:
                    return error
            except (OSError, ValueError):
                pass
            return self._error(str(exc.code), ERROR_HINTS.get(str(exc.code),
                               f'Doubao search HTTP {exc.code}; request failed.'))
        except (TimeoutError, URLError, OSError):
            return self._error('network_error', 'Cannot reach Doubao search or the request timed out.')
        except ValueError:
            return self._error('invalid_response', 'Doubao search returned invalid JSON.')

        error = self._api_error(data)
        if error:
            return error
        if not isinstance(data, dict) or not isinstance(data.get('Result'), dict):
            return self._error('invalid_response', 'Doubao search response is missing Result.')
        result = data['Result']
        count = result.get('ResultCount')
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return self._error('invalid_response', 'Doubao search response has invalid ResultCount.')
        items = result.get('WebResults')
        if items is None and count == 0:
            items = []
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            return self._error('invalid_response', 'Doubao search response has invalid WebResults.')

        results, seen = [], set()
        for item in items:
            title, url = item.get('Title'), item.get('Url')
            if not isinstance(title, str) or not isinstance(url, str):
                continue
            url = url.strip()
            try:
                parsed = urlsplit(url)
                identity = urldefrag(url)[0]
                if parsed.scheme not in ('http', 'https') or not parsed.hostname or identity in seen:
                    continue
            except ValueError:
                continue
            seen.add(identity)
            # Summary contains query-relevant passages. Snippet is only a last resort.
            content, content_source = '', 'none'
            for field in ('Summary', 'Content', 'Snippet'):
                value = item.get(field)
                if isinstance(value, str) and value.strip():
                    content, content_source = value.strip()[:MAX_CONTENT_CHARS], field.lower()
                    break
            entry = {'title': title.strip()[:300], 'url': url, 'content': content,
                     'content_source': content_source}
            for source, target in (('PublishTime', 'published_at'), ('SiteName', 'site'),
                                   ('AuthInfoDes', 'authority')):
                value = item.get(source)
                if isinstance(value, str) and value.strip():
                    entry[target] = value.strip()[:100]
            results.append(entry)
            if len(results) == MAX_RESULTS:
                break
        response = {'provider': 'doubao', 'results': results}
        metadata = data.get('ResponseMetadata')
        if isinstance(metadata, dict) and metadata.get('RequestId'):
            response['request_id'] = self._safe(metadata['RequestId'], 100)
        if count > 0 and not results:
            response['warning'] = 'Search returned no usable HTTP(S) source links.'
        return response


def check_search():
    """One explicit real search, independent of the LLM and memory database."""
    result = DoubaoSearchClient().search('火山引擎豆包搜索官方文档')
    if 'error' in result:
        print(f'Search [FAIL] {result["error_code"]}: {result["error"]}')
        if result.get('request_id'):
            print(f'Request ID: {result["request_id"]}')
        return 1
    print(f'Doubao Custom [OK]: {len(result["results"])} results')
    for item in result['results']:
        print(f'{item["title"]}\n{item["url"]}')
    if result.get('warning'):
        print(f'Warning: {result["warning"]}')
    return 0
