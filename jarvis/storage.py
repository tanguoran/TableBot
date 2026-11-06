import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

SCHEMA='''
CREATE TABLE IF NOT EXISTS reminders (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id TEXT NOT NULL,
 message TEXT NOT NULL,
 due_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_files (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 root TEXT NOT NULL,
 relative_path TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 model TEXT NOT NULL,
 chunker_version TEXT NOT NULL,
 indexed_at TEXT NOT NULL,
 UNIQUE(root, relative_path)
);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 file_id INTEGER NOT NULL REFERENCES knowledge_files(id) ON DELETE CASCADE,
 chunk_index INTEGER NOT NULL,
 title TEXT NOT NULL,
 heading TEXT NOT NULL,
 start_line INTEGER NOT NULL,
 end_line INTEGER NOT NULL,
 content TEXT NOT NULL,
 vector TEXT NOT NULL,
 UNIQUE(file_id, chunk_index)
);
'''

def now_utc(): return datetime.now(timezone.utc).isoformat()

class Store:
    def __init__(self, path):
        self.path=path
        if path != ':memory:': Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.conn=sqlite3.connect(path)
        self.conn.row_factory=sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self._remove_legacy_memory()
        self.conn.executescript(SCHEMA)
    def _remove_legacy_memory(self):
        legacy=self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                 "AND name IN ('memories','memory_embeddings')").fetchall()
        if not legacy:
            return
        # Personal memory was removed at the user's request. Migrate existing
        # databases without retaining a backup of the deleted personal data.
        self.conn.execute('PRAGMA secure_delete=ON')
        with self.conn:
            self.conn.execute('DROP TABLE IF EXISTS memory_embeddings')
            self.conn.execute('DROP TABLE IF EXISTS memories')
        self.conn.execute('VACUUM')
    def knowledge_files(self,root):
        return [dict(row) for row in self.conn.execute(
            'SELECT * FROM knowledge_files WHERE root=? ORDER BY relative_path',(root,)).fetchall()]
    def sync_knowledge(self,root,documents,deleted):
        # All changed files and deletions are committed as one snapshot.
        with self.conn:
            self.conn.executemany('DELETE FROM knowledge_files WHERE root=? AND relative_path=?',
                                  [(root,path) for path in deleted])
            for document in documents:
                self.conn.execute('''INSERT INTO knowledge_files
                    (root,relative_path,content_hash,model,chunker_version,indexed_at) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(root,relative_path) DO UPDATE SET content_hash=excluded.content_hash,
                    model=excluded.model,chunker_version=excluded.chunker_version,indexed_at=excluded.indexed_at''',
                    (root,document['path'],document['hash'],document['model'],document['version'],now_utc()))
                file_id=self.conn.execute('SELECT id FROM knowledge_files WHERE root=? AND relative_path=?',
                                          (root,document['path'])).fetchone()['id']
                self.conn.execute('DELETE FROM knowledge_chunks WHERE file_id=?',(file_id,))
                self.conn.executemany('''INSERT INTO knowledge_chunks
                    (file_id,chunk_index,title,heading,start_line,end_line,content,vector)
                    VALUES(?,?,?,?,?,?,?,?)''',
                    [(file_id,index,chunk['title'],chunk['heading'],chunk['start_line'],chunk['end_line'],
                      chunk['content'],json.dumps(chunk['vector'],allow_nan=False))
                     for index,chunk in enumerate(document['chunks'])])
    def indexed_knowledge_chunks(self,root,model):
        return [dict(row) for row in self.conn.execute('''SELECT c.*,f.relative_path,f.indexed_at
            FROM knowledge_chunks c JOIN knowledge_files f ON f.id=c.file_id
            WHERE f.root=? AND f.model=? ORDER BY f.relative_path,c.chunk_index''',(root,model)).fetchall()]
    def knowledge_stats(self,root,model):
        files=self.conn.execute('SELECT COUNT(*) FROM knowledge_files WHERE root=? AND model=?',
                                (root,model)).fetchone()[0]
        chunks=self.conn.execute('''SELECT COUNT(*) FROM knowledge_chunks c
            JOIN knowledge_files f ON f.id=c.file_id WHERE f.root=? AND f.model=?''',(root,model)).fetchone()[0]
        return {'files':files,'chunks':chunks}
    def add_reminder(self,user_id,message,due_at):
        cur=self.conn.execute('INSERT INTO reminders(user_id,message,due_at,created_at) VALUES(?,?,?,?)',(user_id,message,due_at,now_utc()));self.conn.commit();return cur.lastrowid
    def list_reminders(self,user_id,status='pending'):
        rows=self.conn.execute('SELECT * FROM reminders WHERE user_id=? AND status=? ORDER BY due_at',(user_id,status)).fetchall()
        return [dict(r) for r in rows]
    def cancel_reminder(self,user_id,reminder_id):
        cur=self.conn.execute("UPDATE reminders SET status='cancelled' WHERE user_id=? AND id=? AND status='pending'",(user_id,reminder_id));self.conn.commit();return cur.rowcount>0
    def due_reminders(self,user_id):
        rows=self.conn.execute("SELECT * FROM reminders WHERE user_id=? AND status='pending' AND due_at<=? ORDER BY due_at",(user_id,now_utc())).fetchall()
        ids=[r['id'] for r in rows]
        if ids:
            self.conn.executemany("UPDATE reminders SET status='delivered' WHERE id=?",[(i,) for i in ids]);self.conn.commit()
        return [dict(r) for r in rows]
