import unittest
import tempfile
import sqlite3
import io
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path
from datetime import datetime,timedelta,timezone
from jarvis.storage import Store
from jarvis.tools import Tools, TOOL_SCHEMAS
from jarvis.agent import Agent

class FakeLLM:
    def __init__(self):self.calls=0
    def chat(self,messages,tools):
        self.calls+=1
        if self.calls==1:return {'role':'assistant','content':'','tool_calls':[{'function':{'name':'create_reminder','arguments':{'message':'检查电池','due_at':'2099-01-01T10:00:00+09:00'}}}]}
        return {'role':'assistant','content':'已经创建。'}

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:');self.tools=Tools(self.store,'a',search_fn=lambda q:{'results':[{'title':'Test','url':'https://example.com'}]})
    def tearDown(self):
        self.store.conn.close()
    def test_removed_memory_tools_cannot_be_advertised_or_executed(self):
        names={tool['function']['name'] for tool in TOOL_SCHEMAS}
        for name in ['save_memory','search_memory','delete_memory','list_memories']:
            self.assertNotIn(name,names)
            self.assertIn('unknown tool',self.tools.run(name,{})['error'])
        tables={row[0] for row in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'memories','memory_embeddings'}.isdisjoint(tables))
    def test_removed_memory_command_does_not_reach_model(self):
        from jarvis.__main__ import main
        with patch('sys.argv',['jarvis']),patch('jarvis.__main__.config.DB_PATH',':memory:'), \
                patch('jarvis.__main__.Agent') as factory,redirect_stdout(io.StringIO()) as output, \
                patch('builtins.input',side_effect=['/memories','/quit']):
            main()
        factory.return_value.reply.assert_not_called()
        self.assertIn('Unknown command',output.getvalue())
    def test_search(self):self.assertEqual(len(self.tools.run('search_web',{'query':'test'})['results']),1)
    def test_reminder(self):
        due=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()
        self.assertTrue(self.tools.run('create_reminder',{'message':'喝水','due_at':due})['created'])
        self.assertEqual(len(self.tools.run('list_reminders',{})['reminders']),1)
        self.assertIn('error',self.tools.run('create_reminder',{'message':'test','due_at':'2030-01-01T10:00:00'}))
    def test_agent_tool_loop(self):
        agent=Agent(FakeLLM(),self.tools)
        self.assertEqual(agent.reply('2099年1月1日日本时间上午十点提醒我检查电池'),'已经创建。')
        self.assertEqual(len(self.store.list_reminders('a')),1)
    def test_reminder_survives_reopening_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'jarvis.db')
            store=Store(path)
            try:
                store.add_reminder('a','检查电池','2099-01-01T01:00:00+00:00')
            finally:
                store.conn.close()
            reopened=Store(path)
            try:
                self.assertEqual(reopened.list_reminders('a')[0]['message'],'检查电池')
                self.assertEqual(reopened.list_reminders('b'),[])
            finally:
                reopened.conn.close()
    def test_migration_removes_personal_memory_and_preserves_knowledge_and_reminders(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'legacy.db'
            store=Store(str(path))
            store.add_reminder('a','保留提醒','2099-01-01T00:00:00+00:00')
            store.sync_knowledge('/test-root',[{'path':'keep.md','hash':'same','model':'embed',
                'version':'v1','chunks':[{'title':'资料','heading':'资料','start_line':1,'end_line':2,
                'content':'保留知识库内容','vector':[1,0]}]}],[])
            before=[dict(row) for row in store.conn.execute('SELECT * FROM knowledge_chunks')]
            store.conn.executescript('''
                CREATE TABLE memories(id INTEGER PRIMARY KEY, user_id TEXT, content TEXT);
                CREATE TABLE memory_embeddings(memory_id INTEGER REFERENCES memories(id), vector TEXT);
                INSERT INTO memories VALUES(1,'a','personal-data-removal-sentinel-4826');
                INSERT INTO memory_embeddings VALUES(1,'[1,0]');
            ''')
            store.conn.close()
            self.assertIn(b'personal-data-removal-sentinel-4826',path.read_bytes())
            migrated=Store(str(path))
            try:
                tables={row[0] for row in migrated.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({'memories','memory_embeddings'}.isdisjoint(tables))
                self.assertEqual([dict(row) for row in migrated.conn.execute('SELECT * FROM knowledge_chunks')],before)
                self.assertEqual(migrated.knowledge_stats('/test-root','embed'),{'files':1,'chunks':1})
                self.assertEqual(migrated.list_reminders('a')[0]['message'],'保留提醒')
                self.assertEqual(migrated.conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            finally:
                migrated.conn.close()
            self.assertNotIn(b'personal-data-removal-sentinel-4826',path.read_bytes())
            reopened=Store(str(path))
            try:
                self.assertEqual(reopened.knowledge_stats('/test-root','embed'),{'files':1,'chunks':1})
            finally:
                reopened.conn.close()
    def test_reminder_isolation_and_cancellation(self):
        due=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        reminder=self.tools.run('create_reminder',{'message':'测试','due_at':due})
        self.assertEqual(self.store.list_reminders('b'),[])
        self.assertFalse(self.store.cancel_reminder('b',reminder['id']))
        self.assertTrue(self.tools.run('cancel_reminder',{'reminder_id':reminder['id']})['cancelled'])
        self.assertEqual(self.store.list_reminders('a'),[])
        self.assertFalse(self.tools.run('cancel_reminder',{'reminder_id':reminder['id']})['cancelled'])
    def test_due_reminders_are_consumed_once(self):
        past=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
        self.store.add_reminder('a','到期测试',past)
        self.assertEqual(self.store.due_reminders('b'),[])
        self.assertEqual(self.store.due_reminders('a')[0]['message'],'到期测试')
        self.assertEqual(self.store.due_reminders('a'),[])
    def test_reminder_rejects_past_time(self):
        past=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
        self.assertIn('error',self.tools.run('create_reminder',{'message':'测试','due_at':past}))
        self.assertEqual(self.store.list_reminders('a'),[])

if __name__=='__main__':unittest.main()
