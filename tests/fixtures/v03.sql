
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
