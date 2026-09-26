import json
from pathlib import Path
import sqlite3

from duopilot import create_app, kanban
from duopilot.db import get_db, init_db
from duopilot.orchestrator import enqueue


def test_v03_upgrade_preserves_all_existing_rows_and_links(tmp_path):
    path=tmp_path/'legacy.sqlite3'
    db=sqlite3.connect(path)
    db.executescript((Path(__file__).parent/'fixtures'/'v03.sql').read_text())
    stamp='2026-09-25T08:00:00+00:00'
    plan={'title':'Plan historique','action':'fix_bug','scope':['Calcul'],
          'rationale':'Erreur observée','acceptance_criteria':['Résultat correct'],'task_id':7}
    db.execute("INSERT INTO cycles VALUES(4,?,'live','completed',?,?,NULL)",('2026-09-25',stamp,stamp))
    db.execute("INSERT INTO sessions VALUES(9,4,'cspilot','agreed','Contexte historique',?,'empreinte-originale',NULL,'B')",(json.dumps(plan),))
    db.execute("INSERT INTO messages VALUES(11,9,1,'B','Observation conservée',NULL,'complete',2,42,'claude',?,'Contexte précis')",(stamp,))
    db.execute("INSERT INTO tasks VALUES(7,'live','cspilot','Demande de Cyril','Description à conserver','bug',1,'planned','human','B','A',?,3,?,?)",(json.dumps(plan),stamp,stamp))
    db.execute("INSERT INTO task_events VALUES(3,7,'human','comment',?,?)",(json.dumps({'text':'Retour utilisateur historique'}),stamp))
    db.execute("INSERT INTO task_decisions VALUES(9,7,'recorded','Ancienne note',?)",(stamp,))
    db.commit()
    tables=('cycles','sessions','messages','tasks','task_events','task_decisions')
    columns={table:[r[1] for r in db.execute(f'PRAGMA table_info({table})')] for table in tables}
    before={table:db.execute(f'SELECT * FROM {table}').fetchall() for table in tables}
    db.close()
    app=create_app({'TESTING':True,'DATABASE':str(path)})
    with app.app_context():
        for _ in range(2):
            init_db()
            for table in tables:
                assert [tuple(r) for r in get_db().execute(f"SELECT {','.join(columns[table])} FROM {table}")]==before[table]
        assert get_db().execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert not get_db().execute('PRAGMA foreign_key_check').fetchall()
        assert kanban.task_detail(7)['developer']=='B'
        assert enqueue('live','2026-09-25')==(4,False)
        extra,created=enqueue('live','2026-09-25',manual_key='a'*32,topics=('self',))
        assert created and extra!=4
        assert get_db().execute('SELECT sequence FROM cycles WHERE id=4').fetchone()[0]==0
