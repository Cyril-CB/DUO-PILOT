import json
import uuid

import pytest

from duopilot import create_app, kanban, providers, settings
from duopilot.db import get_db, init_db
from duopilot.orchestrator import enqueue, run_cycle, cycle_detail, validate_plan, plan_hash
from test_kanban import fields, token, SelectingProvider


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING':True,'DATABASE':str(tmp_path/'db.sqlite3'),'SECRET_KEY':'test',
        'EXECUTION_ROOT':str(tmp_path/'executions'),'EXECUTION_ENABLED':True,
        'CSPILOT_REPO':'owner/cspilot','SELF_REPO':'owner/duo'})


def accepted_task(mode='live', *, split=False):
    def change(request,reply):
        if request['stage']=='propose' and split:
            plan = reply.payload['plan']
            leaf = {k:v for k,v in plan.items() if k not in ('subtasks','task_id')}
            plan.update(action='split',subtasks=[
                leaf|{'title':'Étape autonome','depends_on':0},
                leaf|{'title':'Étape dépendante','depends_on':1}])
    cid,_ = enqueue(mode,manual_key=uuid.uuid4().hex,topics=('cspilot',))
    run_cycle(cid,provider=SelectingProvider(hook=change),context_builder=lambda *_:'Contexte de test.')
    session = cycle_detail(cid)['sessions'][0]
    assert session['status']=='agreed',session['error']
    return kanban.task_detail(session['kanban']['task_id'])


def test_extra_cycles_keep_daily_history_and_distinct_roles(app):
    with app.app_context():
        daily,_ = enqueue('demo')
        run_cycle(daily)
        before = [tuple(r) for r in get_db().execute('SELECT * FROM messages')]
        key = uuid.uuid4().hex
        extra,created = enqueue('demo',manual_key=key,topics=('cspilot',))
        assert created and enqueue('demo',manual_key=key)==(extra,False)
        assert enqueue('demo')==(daily,False)
        run_cycle(extra)
        day,one = cycle_detail(daily),cycle_detail(extra)
        assert len(one['sessions'])==1 and len(one['sessions'][0]['messages'])==6
        assert day['sessions'][0]['opening_actor']!=one['sessions'][0]['opening_actor']
        assert before==[tuple(r) for r in get_db().execute('SELECT * FROM messages WHERE id<=12')]
        init_db()
        assert not get_db().execute('PRAGMA foreign_key_check').fetchall()
        both,_ = enqueue('demo',manual_key=uuid.uuid4().hex)
        run_cycle(both)
        assert sum(len(s['messages']) for s in cycle_detail(both)['sessions'])==12


def test_manual_cycle_post_nonce_and_csrf(app):
    client = app.test_client()
    csrf = token(client)
    data = {'mode':'demo','topic':'self','manual_key':uuid.uuid4().hex}
    assert client.post('/cycles',data=data).status_code==400
    for _ in range(2):
        response = client.post('/cycles',data=data|{'csrf_token':csrf},follow_redirects=True)
        assert response.status_code==200
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM cycles').fetchone()[0]==1
        assert get_db().execute('SELECT count(*) FROM messages').fetchone()[0]==6


def test_settings_preserve_snapshots_and_validate_changes(app):
    with app.app_context():
        settings.save(settings.DEFAULTS|{'CODEX_MODEL':'test-model','CLAUDE_MODEL':'opus','EXECUTION_MINUTES':42},0)
        cid,_ = enqueue('demo')
        settings.save(settings.DEFAULTS|{'CODEX_MODEL':'new-model'},1)
        snapshot = json.loads(get_db().execute('SELECT settings_snapshot FROM cycles WHERE id=?',(cid,)).fetchone()[0])
        assert snapshot['CODEX_MODEL']=='test-model' and snapshot['EXECUTION_MINUTES']==42
        assert 'GITHUB_WRITE_TOKEN' not in snapshot
        assert settings.effective_config()['CODEX_MODEL']=='new-model'
        with pytest.raises(ValueError,match='changé'):
            settings.save(settings.DEFAULTS,1)
    client = app.test_client()
    csrf = token(client)
    assert client.get('/configuration').status_code==200
    assert client.post('/configuration',data={'csrf_token':csrf,'version':2,'EXECUTION_MINUTES':0}).status_code==400


@pytest.mark.parametrize('change',[
    {'EXECUTION_MINUTES':0},{'EXECUTION_ATTEMPTS':31},{'EXECUTION_CALL_SECONDS':2000},
    {'CODEX_MODEL':'x; run'},{'SELF_TEST_COMMAND':'python -m pytest'},
    {'SELF_TEST_COMMAND':['python','']},{'SELF_TEST_IMAGE':'x --privileged'},
])
def test_invalid_settings_are_rejected(change):
    with pytest.raises(ValueError):
        settings.validate(settings.DEFAULTS|change)


def test_split_creates_voted_children_and_respects_dependencies(app):
    with app.app_context():
        parent = accepted_task(split=True)
        children = parent['children']
        assert len(children)==2
        first,second = [kanban.task_detail(c['id']) for c in children]
        assert first['depends_on'] is None and second['depends_on']==first['id']
        assert all(c['mode']==parent['mode'] and c['topic']==parent['topic'] for c in (first,second))
        assert first['developer']==parent['developer'] and first['reviewer']==parent['reviewer']
        assert first['decisions']
        from duopilot.executor import _dependency_ready
        hypothetical_child = {'id':999,'depends_on':None,'parent_id':second['id']}
        assert not _dependency_ready(hypothetical_child)
        context,seen = kanban.discussion_snapshot('cspilot','live')
        assert parent['id'] not in seen and first['id'] in seen
        assert 'Étape dépendante' in context
        from duopilot.executor import enqueue_task
        from duopilot.execution_io import ExecutionError
        with pytest.raises(ExecutionError,match='découpée'):
            enqueue_task(parent['id'])
        with pytest.raises(ExecutionError,match='précédente'):
            enqueue_task(second['id'])
        job,_ = enqueue_task(first['id'])
        assert enqueue_task(first['id'])==(job,False)
    assert app.test_client().get(f"/kanban/{parent['id']}").status_code==200


def test_split_structure_is_part_of_exact_vote():
    plan = providers.DemoProvider().generate({'actor':'A','topic':'cspilot','stage':'propose'}).payload['plan']
    leaf = {k:v for k,v in plan.items() if k not in ('task_id','subtasks')}
    plan.update(action='split',subtasks=[leaf|{'depends_on':0},leaf|{'depends_on':1}])
    normalized = validate_plan(plan)
    original = plan_hash(normalized)
    plan['subtasks'][1]['depends_on']=0
    assert plan_hash(validate_plan(plan))!=original
    plan['subtasks'][0]['depends_on']=1
    with pytest.raises(ValueError,match='précédente'):
        validate_plan(plan)


def test_execution_simulation_can_be_queued_from_ui_and_exported(app):
    with app.app_context():
        task = accepted_task('demo')
    client = app.test_client()
    csrf = token(client)
    assert client.post(f"/executions/task/{task['id']}").status_code==400
    response = client.post(f"/executions/task/{task['id']}",data={'csrf_token':csrf},follow_redirects=True)
    assert response.status_code==200
    assert app.test_cli_runner().invoke(args=['worker','--once']).exit_code==0
    data = client.get('/executions/1/export').json
    assert data['status']=='demo_completed' and data['calls']==0 and data['pr_url'] is None
    assert [e['actor'] for e in data['events'] if e['kind'] in ('develop','review_code')]==[task['developer'],task['reviewer']]
    assert client.get('/executions').status_code==200
    assert client.get('/executions/1').status_code==200
    assert client.get('/executions/999').status_code==404


def test_models_are_forwarded_to_both_official_clis(monkeypatch):
    monkeypatch.setattr(providers,'validate_live_config',lambda _:None)
    monkeypatch.setattr(providers,'_check_codex_config',lambda:None)
    monkeypatch.setattr(providers.shutil,'which',lambda binary:binary)
    commands = []
    def run(args,*_,**kwargs):
        commands.append(args)
        value = json.dumps({'message':'Observation','read_requests':[]})
        return json.dumps({'result':value}) if args[0]=='claude' else value
    monkeypatch.setattr(providers,'_run_cli',run)
    provider = providers.SubscriptionCLIProvider({'CODEX_MODEL':'configured-openai','CLAUDE_MODEL':'sonnet'})
    for actor in ('A','B'):
        provider.generate({'stage':'discuss','actor':actor,'topic':'self'})
    assert [c[c.index('--model')+1] for c in commands]==['configured-openai','sonnet']
