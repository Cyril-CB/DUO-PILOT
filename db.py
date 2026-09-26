"""Small SQLite journal. Each reserved turn counts, even if its CLI fails."""
import sqlite3
from datetime import datetime, timezone
from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
 id INTEGER PRIMARY KEY, run_date TEXT NOT NULL, mode TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL,
 finished_at TEXT, error TEXT, UNIQUE(run_date, mode)
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
 UNIQUE(session_id, turn), CHECK(actor IN ('A','B'))
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, turn);
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
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript(SCHEMA)
    # Serialize the additive migration when web and worker start together.
    # Old messages, decisions, identifiers and daily reservations stay intact.
    try:
        db.execute('BEGIN IMMEDIATE')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(sessions)')}
        if 'opening_actor' not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN opening_actor TEXT CHECK(opening_actor IN ('A','B'))")
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
