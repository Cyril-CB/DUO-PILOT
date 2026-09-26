"""User-facing role rotation and preservation of existing conversation data."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from duopilot import create_app
from duopilot.db import get_db
from duopilot.orchestrator import enqueue, run_cycle, cycle_detail
from duopilot.providers import DemoProvider


def test_each_actor_opens_one_topic_and_swaps_next_day(tmp_path):
    app = create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'roles.sqlite3')})
    requests = []

    class ObservedDemo:
        def generate(self, request):
            requests.append(request)
            return DemoProvider().generate(request)

    with app.app_context():
        cycles = []
        for run_date in ('2026-09-26', '2026-09-27'):
            cid, _ = enqueue('demo', run_date)
            run_cycle(cid, provider=ObservedDemo())
            detail = cycle_detail(cid)
            assert detail['cycle']['status'] == 'completed'
            assert {s['messages'][0]['actor'] for s in detail['sessions']} == {'A', 'B'}
            assert sum(len(s['messages']) for s in detail['sessions']) == 12
            for session in detail['sessions']:
                opener = session['messages'][0]['actor']
                reviewer = session['messages'][1]['actor']
                assert opener != reviewer
                assert session['roles'] == {'opener': opener, 'developer': opener, 'reviewer': reviewer}
                assert session['messages'][4]['actor'] == opener
                assert session['messages'][5]['actor'] == reviewer
                assert all(request['roles'] == session['roles'] for request in requests[-12:]
                           if request['topic'] == session['topic'])
            cycles.append(detail)
        for previous, following in zip(cycles[0]['sessions'], cycles[1]['sessions']):
            assert previous['roles']['developer'] == following['roles']['reviewer']
            assert previous['roles']['reviewer'] == following['roles']['developer']
        assert cycle_detail(cycles[0]['cycle']['id']) == cycles[0]

    exported = app.test_client().get('/cycles/1/export').json
    assert exported['execution_enabled'] is False
    assert exported['sessions'][0]['roles'] == cycles[0]['sessions'][0]['roles']
    page = app.test_client().get('/cycles/1').get_data(as_text=True)
    assert 'Si une tâche est développée' in page


# Exact table definitions from 0.1, before the opening_actor column existed.
LEGACY_SCHEMA = '''
CREATE TABLE cycles (
 id INTEGER PRIMARY KEY, run_date TEXT NOT NULL, mode TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL,
 finished_at TEXT, error TEXT, UNIQUE(run_date, mode)
);
CREATE TABLE sessions (
 id INTEGER PRIMARY KEY, cycle_id INTEGER NOT NULL REFERENCES cycles(id),
 topic TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', context TEXT,
 decision TEXT, plan_id TEXT, error TEXT, UNIQUE(cycle_id, topic)
);
CREATE TABLE messages (
 id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id),
 turn INTEGER NOT NULL CHECK(turn BETWEEN 1 AND 6), actor TEXT NOT NULL,
 content TEXT NOT NULL DEFAULT '', payload TEXT, status TEXT NOT NULL DEFAULT 'reserved',
 input_tokens INTEGER, output_tokens INTEGER, model TEXT, created_at TEXT NOT NULL,
 UNIQUE(session_id, turn), CHECK(actor IN ('A','B'))
);
'''


def legacy_database(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.executescript(LEGACY_SCHEMA)
        db.execute("INSERT INTO cycles VALUES(1,'2026-09-26','live','completed','before','after',NULL)")
        for sid, topic in ((1, 'cspilot'), (2, 'self')):
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)',
                       (sid, 1, topic, 'agreed', 'Contexte historique intégral',
                        json.dumps({'title': 'Décision existante'}, ensure_ascii=False), 'legacy-plan', None))
            for turn in range(1, 7):
                db.execute('''INSERT INTO messages(session_id,turn,actor,content,payload,status,
                    input_tokens,output_tokens,model,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (sid, turn, 'A' if turn % 2 else 'B', f'Échange original {topic} {turn}',
                     '{"original":true}', 'complete', 17, 29, 'modèle historique', 'before'))
        # A failure before any intervention must not be assigned invented roles.
        db.execute("INSERT INTO cycles VALUES(2,'2026-09-25','live','failed','before','after','échec')")
        db.execute("INSERT INTO sessions(id,cycle_id,topic,status,error) VALUES(3,2,'self','failed','collecte échouée')")
        # Existing queued cycles can adopt the new rule before they start.
        db.execute("INSERT INTO cycles VALUES(3,'2026-09-24','demo','queued','before',NULL,NULL)")
        for sid, topic in ((4, 'cspilot'), (5, 'self')):
            db.execute('INSERT INTO sessions(id,cycle_id,topic) VALUES(?,3,?)', (sid, topic))
        return {table: [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY id')]
                for table in ('cycles', 'sessions', 'messages')}


def test_upgrade_preserves_every_legacy_field_and_real_openers(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    before = legacy_database(path)
    config = {'TESTING': True, 'DATABASE': str(path)}
    # Repeated startup must leave migrated data intact as well.
    create_app(config)
    app = create_app(config)
    with app.app_context():
        db = get_db()
        for table, rows in before.items():
            old_columns = ','.join(rows[0])
            after = [dict(r) for r in db.execute(f'SELECT {old_columns} FROM {table} ORDER BY id')]
            assert after == rows
        detail = cycle_detail(1)
        # Both 0.1 topics were opened by A. Do not rewrite the old self topic to B.
        assert [s['roles']['opener'] for s in detail['sessions']] == ['A', 'A']
        assert cycle_detail(2)['sessions'][0]['roles'] is None
        assert enqueue('live', '2026-09-26') == (1, False)
        assert run_cycle(1) is False
        assert run_cycle(3)
        assert {s['roles']['opener'] for s in cycle_detail(3)['sessions']} == {'A', 'B'}

    exported = app.test_client().get('/cycles/1/export').json
    assert exported['sessions'][1]['messages'][-1]['content'] == 'Échange original self 6'
    assert exported['sessions'][1]['roles']['developer'] == 'A'


def test_two_startups_can_upgrade_same_old_database(tmp_path):
    path = tmp_path / 'shared.sqlite3'
    legacy_database(path)

    def start(_):
        app = create_app({'TESTING': True, 'DATABASE': str(path)})
        with app.app_context():
            return get_db().execute('SELECT count(*) FROM messages').fetchone()[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(start, range(2))) == [12, 12]


def test_history_older_than_thirty_cycles_remains_readable_and_exportable(tmp_path):
    app = create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'history.sqlite3')})
    with app.app_context():
        for offset in range(35):
            cid, _ = enqueue('demo', (date(2026, 1, 1) + timedelta(days=offset)).isoformat())
            if offset == 0:
                run_cycle(cid)
        snapshot = cycle_detail(1)

    client = app.test_client()
    first = client.get('/').get_data(as_text=True)
    assert '2026-01-01' not in first
    assert 'Cycles plus anciens' in first
    older = client.get('/?page=2').get_data(as_text=True)
    assert '2026-01-01' in older
    assert 'Cycles plus récents' in older
    assert client.get('/cycles/1').status_code == 200
    exported = client.get('/cycles/1/export').json
    assert exported['sessions'] == snapshot['sessions']
    assert client.get('/?page=0').status_code == 400
    assert client.get('/?page=3').status_code == 404
