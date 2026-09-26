import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from duopilot import create_app
from duopilot.db import get_db
from duopilot.orchestrator import enqueue, run_cycle, cycle_detail, mark_interrupted
from duopilot.providers import build_provider

@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'test.sqlite3'), 'SECRET_KEY':'test-only'})

def demo_context(config, topic, mode):
    return 'Contexte fictif pour un test isolé.'

def test_twelve_turns_and_exact_consensus(app):
    with app.app_context():
        cid, fresh = enqueue('demo')
        assert fresh
        assert run_cycle(cid)
        result = cycle_detail(cid)
        assert result['cycle']['status'] == 'completed'
        for s in result['sessions']:
            assert s['status'] == 'agreed'
            assert [m['turn'] for m in s['messages']] == list(range(1,7))
            assert sum(m['actor']=='A' for m in s['messages']) == 3
            assert sum(m['actor']=='B' for m in s['messages']) == 3
            vote = json.loads(s['messages'][-1]['payload'])
            assert vote['plan_id'] == s['plan_id']
        assert not run_cycle(cid)
        assert enqueue('demo') == (cid, False)
        assert get_db().execute('SELECT count(*) FROM messages').fetchone()[0] == 12

def test_concurrent_claim_only_once(app):
    with app.app_context():
        cid, _ = enqueue('demo')
    def go():
        with app.app_context():
            return run_cycle(cid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:go(), range(2)))
    assert sorted(results) == [False, True]
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM messages').fetchone()[0] == 12

def test_wrong_plan_vote_rejected(app):
    with app.app_context():
        base = build_provider(app.config, 'demo')
        class WrongVote:
            def generate(self, request):
                result = base.generate(request)
                if request['stage']=='vote':
                    result.payload['plan_id']='different-plan'
                return result
        cid, _ = enqueue('demo')
        run_cycle(cid, provider=WrongVote())
        assert all(s['status']=='rejected' for s in cycle_detail(cid)['sessions'])

def test_failure_is_recorded_without_retry_or_secrets(app):
    class Failed:
        calls = 0
        def generate(self, request):
            self.calls += 1
            raise RuntimeError('secret-token-should-not-appear')
    with app.app_context():
        provider=Failed()
        cid, _ = enqueue('demo')
        run_cycle(cid, provider=provider)
        detail = cycle_detail(cid)
        assert detail['cycle']['status']=='failed'
        assert provider.calls == 2
        assert 'secret-token' not in json.dumps(detail)
        assert all(s['messages'][0]['status']=='failed' for s in detail['sessions'])
        run_cycle(cid, provider=provider)
        assert provider.calls == 2

def test_expired_live_cycle_never_invokes_provider(app):
    class Never:
        def generate(self, request):
            pytest.fail('Provider must not be invoked')
    with app.app_context():
        cid, _=enqueue('live', '2000-01-01')
        run_cycle(cid, provider=Never(), context_builder=demo_context)
        detail=cycle_detail(cid)
        assert detail['cycle']['status']=='failed'
        assert not any(s['messages'] for s in detail['sessions'])

def test_interrupt_does_not_reset_budget(app):
    with app.app_context():
        cid,_=enqueue('demo')
        db=get_db()
        with db:
            db.execute("UPDATE cycles SET status='running' WHERE id=?", (cid,))
        assert mark_interrupted(cid)
        assert not run_cycle(cid)
        assert enqueue('demo')==(cid,False)

def test_ui_csrf_demo_export_and_local_only(app):
    client=app.test_client()
    assert client.get('/').status_code==200
    assert client.post('/cycles',data={'mode':'demo'}).status_code==400
    with client.session_transaction() as s:
        token=s['csrf_token']
    response=client.post('/cycles',data={'mode':'demo','csrf_token':token},follow_redirects=True)
    assert response.status_code==200
    assert b'CS-PILOT' in response.data
    exported=client.get('/cycles/1/export')
    assert exported.json['execution_enabled'] is False
    assert len(exported.json['sessions'])==2
    assert client.get('/',environ_base={'REMOTE_ADDR':'203.0.113.8'}).status_code==403
    assert client.get('/',headers={'Host':'evil.example'}).status_code==400
    assert client.get('/configuration').status_code==200
    assert client.get('/cycles/999').status_code==404

def test_cli_daily_idempotent(app):
    runner=app.test_cli_runner()
    result=runner.invoke(args=['daily','--mode','demo'])
    assert result.exit_code==0, result.output
    result=runner.invoke(args=['daily','--mode','demo'])
    assert result.exit_code==0
    assert 'Aucun nouvel appel' in result.output

def test_readiness_reports_config_problem_before_using_daily_slot(app, monkeypatch):
    from duopilot import providers
    from duopilot.web import readiness
    app.config.update(USE_SUBSCRIPTION_CLI=True, CSPILOT_REPO='owner/app', SELF_REPO='owner/duo')
    monkeypatch.setattr('duopilot.web.shutil.which', lambda _: '/fake/binary')
    def fail(_config):
        raise providers.ProviderError('Configuration Codex incompatible.')
    monkeypatch.setattr(providers, 'validate_live_config', fail)
    with app.app_context():
        assert 'Configuration Codex incompatible.' in readiness()
        assert get_db().execute('SELECT count(*) FROM cycles').fetchone()[0] == 0

def test_no_action_is_a_valid_agreement(app):
    with app.app_context():
        base=build_provider(app.config,'demo')
        class NoAction:
            def generate(self, request):
                reply=base.generate(request)
                if request['stage']=='propose':
                    reply.payload['plan']['action']='none'
                return reply
        cid,_=enqueue('demo')
        run_cycle(cid,provider=NoAction())
        assert all(s['status']=='no_action' for s in cycle_detail(cid)['sessions'])

def test_plan_bounds_are_consistent(app):
    from duopilot.providers import _schema, _validate_payload, ProviderError
    from duopilot.orchestrator import validate_plan
    plan={'title':'a'*201,'action':'investigate','rationale':'r','scope':['s'],'acceptance_criteria':['c']}
    assert _schema('propose')['properties']['plan']['properties']['title']['maxLength']==200
    with pytest.raises(ProviderError):
        _validate_payload({'message':'m','plan':plan},'propose')
    with pytest.raises(ValueError):
        validate_plan(plan)
