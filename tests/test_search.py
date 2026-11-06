import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from jarvis.search import DoubaoSearchClient, SEARCH_URL, check_search
from jarvis.storage import Store
from jarvis.tools import Tools


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.key = 'test-key-do-not-use'
        self.client = DoubaoSearchClient(self.key)

    def response(self, items=None):
        items = [] if items is None else items
        return {'ResponseMetadata': {'RequestId': 'test-request'},
                'Result': {'ResultCount': len(items), 'WebResults': items}}

    def call(self, data, query='机器人新闻', **kwargs):
        with patch('jarvis.search.urlopen', return_value=io.BytesIO(
                json.dumps(data, ensure_ascii=False).encode('utf-8'))) as mocked:
            return self.client.search(query, **kwargs), mocked

    def test_custom_request_and_time_range_protocol(self):
        result, mocked = self.call(self.response(), query='  中文新闻  ', time_range='OneDay')
        request = mocked.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, SEARCH_URL)
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.key)
        self.assertEqual(payload['Query'], '中文新闻')
        self.assertEqual(payload['SearchType'], 'web')
        self.assertEqual(payload['Count'], 5)
        self.assertEqual(payload['TimeRange'], 'OneDay')
        self.assertTrue(payload['Filter']['NeedUrl'])
        self.assertEqual(mocked.call_args.kwargs['timeout'], 20)
        self.assertEqual(result['provider'], 'doubao')

    def test_missing_key_never_sends_request(self):
        with patch('jarvis.search.urlopen') as mocked:
            result = DoubaoSearchClient('').search('新闻')
        mocked.assert_not_called()
        self.assertEqual(result['error_code'], 'missing_api_key')
        self.assertIn('DOUBAO_SEARCH_API_KEY', result['error'])

    def test_invalid_queries_are_rejected_without_silent_truncation(self):
        for query in ('', '   ', '中' * 101, None, [], 123):
            with self.subTest(query=query), patch('jarvis.search.urlopen') as mocked:
                result = self.client.search(query)
                self.assertEqual(result['error_code'], 'invalid_arguments')
                mocked.assert_not_called()
        result, _ = self.call(self.response(), query='中' * 100)
        self.assertNotIn('error', result)

    def test_invalid_time_range_does_not_send_request(self):
        with patch('jarvis.search.urlopen') as mocked:
            result = self.client.search('新闻', time_range='yesterday')
        self.assertEqual(result['error_code'], 'invalid_arguments')
        mocked.assert_not_called()

    def test_summary_and_source_metadata_are_preserved_with_content_budget(self):
        data = self.response([{'Title': '新闻标题', 'Url': 'https://example.com/news',
                              'Summary': '摘要' * 600, 'Content': '正文', 'Snippet': '短片段',
                              'PublishTime': '2026-11-01T10:00:00+08:00',
                              'SiteName': '示例站点', 'AuthInfoDes': '正常权威'}])
        result, _ = self.call(data)
        item = result['results'][0]
        self.assertEqual(item['content_source'], 'summary')
        self.assertEqual(len(item['content']), 1000)
        self.assertTrue(item['content'].startswith('摘要'))
        self.assertEqual(item['published_at'], '2026-11-01T10:00:00+08:00')
        self.assertEqual(item['site'], '示例站点')
        self.assertEqual(item['authority'], '正常权威')
        self.assertEqual(result['request_id'], 'test-request')

    def test_missing_summary_uses_content_then_snippet(self):
        result, _ = self.call(self.response([
            {'Title': '正文', 'Url': 'https://example.com/a', 'Summary': None, 'Content': '正文内容'},
            {'Title': '片段', 'Url': 'https://example.com/b', 'Content': '', 'Snippet': '仅有短片段'},
        ]))
        self.assertEqual(result['results'][0]['content_source'], 'content')
        self.assertEqual(result['results'][1]['content_source'], 'snippet')

    def test_only_unique_http_source_links_are_returned(self):
        result, _ = self.call(self.response([
            {'Title': '第一条', 'Url': 'https://example.com/news#first'},
            {'Title': '重复', 'Url': 'https://example.com/news#second'},
            {'Title': '无链接', 'Url': ''},
            {'Title': '无效协议', 'Url': 'javascript:alert(1)'},
            {'Title': '无域名', 'Url': 'https://'},
            {'Title': '第二条', 'Url': 'http://example.org/news'},
        ]))
        self.assertEqual([item['title'] for item in result['results']], ['第一条', '第二条'])

    def test_result_count_is_bounded(self):
        result, _ = self.call(self.response([
            {'Title': str(i), 'Url': f'https://example.com/{i}'} for i in range(9)
        ]))
        self.assertEqual(len(result['results']), 5)

    def test_empty_success_is_distinct_from_api_failure(self):
        data = self.response()
        data['Result']['WebResults'] = None
        result, _ = self.call(data)
        self.assertEqual(result['results'], [])
        self.assertNotIn('error', result)

    def test_business_errors_with_http_200_are_not_empty_successes(self):
        for code in ('10403', '10406', '10409', '10410', '10412', '700429'):
            with self.subTest(code=code):
                data = {'ResponseMetadata': {'RequestId': 'rejected-request',
                        'Error': {'Code': code, 'Message': 'upstream error'}}, 'Result': None}
                result, _ = self.call(data)
                self.assertEqual(result['error_code'], code)
                self.assertEqual(result['request_id'], 'rejected-request')
                self.assertNotIn('results', result)

    def test_http_errors_preserve_business_code_or_report_http_status(self):
        data = {'ResponseMetadata': {'Error': {'CodeN': 10403}}, 'Result': None}
        error = HTTPError(SEARCH_URL, 403, 'Forbidden', {}, io.BytesIO(json.dumps(data).encode()))
        with patch('jarvis.search.urlopen', side_effect=error):
            self.assertEqual(self.client.search('新闻')['error_code'], '10403')
        for code in (401, 429, 500):
            error = HTTPError(SEARCH_URL, code, 'Rejected', {}, io.BytesIO(b'not JSON'))
            with self.subTest(code=code), patch('jarvis.search.urlopen', side_effect=error):
                result = self.client.search('新闻')
                self.assertEqual(result['error_code'], str(code))

    def test_network_errors_and_timeouts_return_explicit_failure(self):
        for error in (URLError('offline'), TimeoutError('timed out')):
            with self.subTest(error=error), patch('jarvis.search.urlopen', side_effect=error):
                result = self.client.search('新闻')
                self.assertEqual(result['error_code'], 'network_error')

    def test_malformed_response_is_not_reported_as_no_results(self):
        for data in ([], {}, {'Result': None}, {'Result': {'ResultCount': True}},
                     {'Result': {'ResultCount': 1, 'WebResults': None}},
                     {'Result': {'ResultCount': 1, 'WebResults': [None]}}):
            with self.subTest(data=data):
                result, _ = self.call(data)
                self.assertEqual(result['error_code'], 'invalid_response')
        with patch('jarvis.search.urlopen', return_value=io.BytesIO(b'<html>error</html>')):
            self.assertEqual(self.client.search('新闻')['error_code'], 'invalid_response')

    def test_upstream_error_never_echoes_api_key(self):
        data = {'ResponseMetadata': {'RequestId': self.key,
                'Error': {'Code': 'debug', 'Message': 'bad token ' + self.key}}}
        result, _ = self.call(data)
        self.assertNotIn(self.key, json.dumps(result))

    def test_tools_use_doubao_by_default_and_forward_time_range(self):
        store = Store(':memory:')
        try:
            with patch('jarvis.config.SEARCH_API_KEY', self.key), patch(
                    'jarvis.search.urlopen', return_value=io.BytesIO(json.dumps(self.response()).encode())) as mocked:
                result = Tools(store).run('search_web', {'query': '中文新闻', 'time_range': 'OneWeek'})
            self.assertEqual(result['provider'], 'doubao')
            self.assertEqual(json.loads(mocked.call_args.args[0].data)['TimeRange'], 'OneWeek')
        finally:
            store.conn.close()

    def test_explicit_search_check_reports_missing_key_without_network(self):
        with patch('jarvis.config.SEARCH_API_KEY', ''), patch('jarvis.search.urlopen') as mocked:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(check_search(), 1)
        self.assertIn('missing_api_key', output.getvalue())
        mocked.assert_not_called()


if __name__ == '__main__':
    unittest.main()
