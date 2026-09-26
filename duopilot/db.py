"""Small SQLite journal. Each reserved turn counts, even if its CLI fails."""
import sqlite3
import time
from datetime import datetime, timezone
from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
 id INTEGER PRIMARY KEY, run_date TEXT NOT NULL, mode TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL,
 finished_at TEXT, error TEXT, sequence INTEGER NOT NULL DEFAULT 0,
 request_key TEXT UNIQUE, settings_snapshot TEXT, UNIQUE(run_date, mode, sequence)
);
CREATE TABLE IF NOT EXISTS sessions (
 id INTEGER PRIMARY KEY, cycle_id INTEGER NOT NULL REFERENCES cycles(id),
 topic TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', context TEXT,
 decision TEXT, plan_id TEXT, error TEXT,
 opening_actor TEXT CHECK(opening_actor IN ('A','B')),
 UNIQUE(cycle_id, topic)
);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id),
 turn INTEGER NOT NULL CHECK(turn BETWEEN 1 AND 6), actor TEXT NOT NULL,
 content TEXT NOT NULL DEFAULT '', payload TEXT, status TEXT NOT NULL DEFAULT 'reserved',
 input_tokens INTEGER, output_tokens INTEGER, model TEXT, created_at TEXT NOT NULL,
 input_context TEXT,
 UNIQUE(session_id, turn), CHECK(actor IN ('A','B'))
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, turn);
CREATE TABLE IF NOT EXISTS source_reads (
 id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id),
 number INTEGER NOT NULL, after_turn INTEGER NOT NULL CHECK(after_turn BETWEEN 0 AND 4),
 result TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(session_id, number)
);
CREATE TABLE IF NOT EXISTS tasks (
 id INTEGER PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('live','demo')),
 topic TEXT NOT NULL CHECK(topic IN ('cspilot','self')),
 title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 kind TEXT NOT NULL CHECK(kind IN ('bug','feature','task')),
 priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 3),
 status TEXT NOT NULL CHECK(status IN ('backlog','planned','in_progress','review','done','archived')),
 origin TEXT NOT NULL CHECK(origin IN ('human','agents')),
 developer TEXT CHECK(developer IN ('A','B')), reviewer TEXT CHECK(reviewer IN ('A','B')),
 latest_plan TEXT, version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_board ON tasks(mode,topic,status,priority,id);
CREATE TABLE IF NOT EXISTS task_events (
 id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
 actor TEXT NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_events_task ON task_events(task_id,id);
CREATE TABLE IF NOT EXISTS task_decisions (
 session_id INTEGER PRIMARY KEY REFERENCES sessions(id),
 task_id INTEGER REFERENCES tasks(id), outcome TEXT NOT NULL,
 note TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
 id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL, version INTEGER NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
 id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
 mode TEXT NOT NULL, topic TEXT NOT NULL, repo TEXT NOT NULL, base_branch TEXT NOT NULL,
 branch TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'queued', phase TEXT NOT NULL DEFAULT 'prepare',
 plan TEXT NOT NULL, task_version INTEGER NOT NULL, developer TEXT NOT NULL, reviewer TEXT NOT NULL,
 settings_snapshot TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,
 elapsed_seconds REAL NOT NULL DEFAULT 0, started_at TEXT, heartbeat_at TEXT,
 owner TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
 base_sha TEXT, tested_sha TEXT, approved_sha TEXT, published_sha TEXT,
 test_image_id TEXT, checkpoint TEXT, test_report TEXT, latest_review TEXT,
 review_reads INTEGER NOT NULL DEFAULT 0,
 pr_number INTEGER, pr_url TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS executions_task_active ON executions(task_id)
 WHERE status IN ('queued','running','paused','blocked','pr_open');
CREATE UNIQUE INDEX IF NOT EXISTS executions_repo_running ON executions(repo)
 WHERE status='running';
CREATE TABLE IF NOT EXISTS execution_events (
 id INTEGER PRIMARY KEY, execution_id INTEGER NOT NULL REFERENCES executions(id),
 kind TEXT NOT NULL, actor TEXT, status TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS execution_events_job ON execution_events(execution_id,id);
CREATE TABLE IF NOT EXISTS execution_sync_requests (
 execution_id INTEGER PRIMARY KEY REFERENCES executions(id),
 feedback INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
"""

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.config['DATABASE'], timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys=ON')
        g.db.execute('PRAGMA busy_timeout=15000')
    return g.db

def close_db(_error=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    # Switching journal modes may return SQLITE_BUSY immediately even with a
    # busy timeout when the web process and worker open an old DB together.
    deadline = time.monotonic() + 15
    while True:
        try:
            db.execute('PRAGMA journal_mode=WAL').fetchone()
            break
        except sqlite3.OperationalError as exc:
            if getattr(exc, 'sqlite_errorcode', None) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    db.executescript(SCHEMA)
    # Serialize the additive migration when web and worker start together.
    # Old messages, decisions, identifiers and daily reservations stay intact.
    try:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        cycle_columns = {r['name'] for r in db.execute('PRAGMA table_info(cycles)')}
        if 'sequence' not in cycle_columns:
            # Rebuild only the parent table; all existing IDs and original
            # columns survive. Child foreign keys keep referencing cycles.
            db.execute('''CREATE TABLE cycles_v4 (
                id INTEGER PRIMARY KEY, run_date TEXT NOT NULL, mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL,
                finished_at TEXT, error TEXT, sequence INTEGER NOT NULL DEFAULT 0,
                request_key TEXT UNIQUE, settings_snapshot TEXT,
                UNIQUE(run_date,mode,sequence))''')
            db.execute('''INSERT INTO cycles_v4(id,run_date,mode,status,created_at,finished_at,error)
                SELECT id,run_date,mode,status,created_at,finished_at,error FROM cycles''')
            db.execute('DROP TABLE cycles')
            db.execute('ALTER TABLE cycles_v4 RENAME TO cycles')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(sessions)')}
        if 'opening_actor' not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN opening_actor TEXT CHECK(opening_actor IN ('A','B'))")
        message_columns = {row['name'] for row in db.execute('PRAGMA table_info(messages)')}
        if 'input_context' not in message_columns:
            db.execute('ALTER TABLE messages ADD COLUMN input_context TEXT')
        task_columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
        if 'parent_id' not in task_columns:
            db.execute('ALTER TABLE tasks ADD COLUMN parent_id INTEGER REFERENCES tasks(id)')
            db.execute('ALTER TABLE tasks ADD COLUMN position INTEGER NOT NULL DEFAULT 0')
            db.execute('ALTER TABLE tasks ADD COLUMN depends_on INTEGER REFERENCES tasks(id)')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Références de données incohérentes pendant la migration.')
        # Historical roles come from the actual opening message, never from the
        # new alternation rule. A session with no messages stays unattributed.
        db.execute('''UPDATE sessions SET opening_actor=(
            SELECT actor FROM messages WHERE session_id=sessions.id AND turn=1
        ) WHERE opening_actor IS NULL AND EXISTS (
            SELECT 1 FROM messages WHERE session_id=sessions.id AND turn=1
        )''')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')
