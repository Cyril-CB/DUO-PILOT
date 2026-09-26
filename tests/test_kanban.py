import json

import pytest

from duopilot import create_app, kanban, providers
from duopilot.db import get_db, init_db
from duopilot.orchestrator import enqueue, run_cycle, cycle_detail, plan_hash


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'kanban.sqlite3'), 'SECRET_KEY': 'test'})


def fields(**changes):
    return dict(title='Comprendre une validation indisponible', description='Page heures : décrire pourquoi le bouton est désactivé.',
                topic='cspilot', kind='bug', priority=1, status='backlog') | changes


def token(client):
    assert client.get('/kanban/new').status_code == 200
    with client.session_transaction() as session:
        return session['csrf_token']


class SelectingProvider:
    def __init__(self, target=None, hook=None):
        self.base = providers.DemoProvider()
        self.target = target
        self.hook = hook
        self.calls = []

    def generate(self, request):
        self.calls.append(request)
        reply = self.base.generate(request)
        if request['topic'] == 'cspilot' and request['stage'] == 'propose':
            reply.payload['plan']['task_id'] = self.target
        if self.hook:
            self.hook(request, reply)
        return reply


def run_live(provider):
    cid, _ = enqueue('live')
    run_cycle(cid, provider=provider, context_builder=lambda *_: 'Contexte de test sans accès réseau.')
    return cycle_detail(cid)


def test_human_forms_csrf_validation_escaping_and_version_conflict(app):
    client = app.test_client()
    csrf = token(client)
    assert client.post('/kanban/new', data=fields()).status_code == 400
    response = client.post('/kanban/new', data=fields(csrf_token=csrf, title='<script>alert(1)</script>'), follow_redirects=True)
    assert response.status_code == 200
    assert b'&lt;script&gt;' in response.data and b'<script>alert(1)' not in response.data
    with app.app_context():
        row = kanban.task_detail(1)
        assert row['mode'] == 'live' and row['origin'] == 'human' and row['status'] == 'backlog'
    values = fields(csrf_token=csrf, version=1, title='Libellé corrigé', status='in_progress')
    assert client.post('/kanban/1', data=values).status_code == 302
    assert client.post('/kanban/1', data=values | {'title': 'Changement périmé'}).status_code == 409
    assert client.post('/kanban/1', data=values | {'version': 2, 'topic': 'self'}).status_code == 400
    with app.app_context():
        row = kanban.task_detail(1)
        assert row['title'] == 'Libellé corrigé' and row['version'] == 2
        assert row['events'][-1]['data']['title'] == '<script>alert(1)</script>'
    assert client.get('/kanban?mode=invalid').status_code == 400
    assert client.get('/kanban/999').status_code == 404
    assert client.get('/kanban', environ_base={'REMOTE_ADDR': '203.0.113.1'}).status_code == 403


def test_comments_archive_export_and_reopening_keep_history(app):
    client = app.test_client()
    csrf = token(client)
    client.post('/kanban/new', data=fields(csrf_token=csrf))
    response = client.post('/kanban/1/comments', data={'csrf_token': csrf, 'version': 1, 'comment': 'Aussi visible pour la direction.'})
    assert response.status_code == 302
    assert client.post('/kanban/1/comments', data={'csrf_token': csrf, 'version': 1, 'comment': 'Périmé'}).status_code == 409
    assert client.post('/kanban/1/comments', data={'csrf_token': csrf, 'version': 2, 'comment': ' '}).status_code == 400
    client.post('/kanban/1', data=fields(csrf_token=csrf, version=2, status='archived'))
    assert 'Comprendre une validation indisponible' not in client.get('/kanban').get_data(as_text=True)
    assert 'Comprendre une validation indisponible' in client.get('/kanban?archived=1').get_data(as_text=True)
    exported = client.get('/kanban/export').json
    assert exported['tasks'][0]['status'] == 'archived'
    assert len(exported['tasks'][0]['events']) == 3
    client.post('/kanban/1', data=fields(csrf_token=csrf, version=3, status='backlog'))
    with app.app_context():
        context, selected = kanban.discussion_snapshot('cspilot', 'live')
        assert selected == {1: 4} and 'Aussi visible' in context


def test_agents_reuse_human_card_keep_request_and_read_relevant_feedback(app):
    with app.app_context():
        target = kanban.create_task(fields())
        kanban.add_comment(target, 1, 'Conserver le circuit salarié puis responsable puis direction.')
        kanban.create_task(fields(title='Besoin SELF seulement', topic='self'))
        kanban.create_task(fields(title='Demande DEMO seulement'), mode='demo')
        provider = SelectingProvider(target)
        result = run_live(provider)
        assert len(provider.calls) == 12
        for call in provider.calls[:6]:
            assert 'Comprendre une validation indisponible' in call['context']
            assert 'Conserver le circuit' in call['context']
            assert 'Besoin SELF seulement' not in call['context']
            assert 'Demande DEMO seulement' not in call['context']
        assert 'Besoin SELF seulement' in provider.calls[6]['context']
        assert 'Comprendre une validation indisponible' not in provider.calls[6]['context']
        session = result['sessions'][0]
        task = kanban.task_detail(target)
        assert session['kanban']['task_id'] == target
        assert task['status'] == 'planned'
        assert task['title'] == fields()['title'] and task['description'] == fields()['description']
        assert task['priority'] == 1 and task['origin'] == 'human'
        assert task['developer'] == session['roles']['opener']
        assert task['reviewer'] == session['roles']['reviewer']
        assert task['latest_plan']['task_id'] == target
        assert task['events'][1]['event'] == 'comment'
        assert all(m['input_context'] == call['context'] for m, call in zip(session['messages'], provider.calls))
        assert get_db().execute("SELECT count(*) FROM tasks WHERE topic='cspilot' AND mode='live'").fetchone()[0] == 1
        assert not run_cycle(result['cycle']['id'], provider=provider)
        assert len(provider.calls) == 12
    client = app.test_client()
    assert client.get('/kanban/1').status_code == 200
    assert 'Voir la carte #1' in client.get('/cycles/1').get_data(as_text=True)


@pytest.mark.parametrize('change', ['edit', 'comment', 'done', 'archive'])
def test_edit_during_discussion_is_not_overwritten(app, change):
    with app.app_context():
        target = kanban.create_task(fields())
        def hook(request, reply):
            if request['topic'] == 'cspilot' and request['turn'] == 6:
                if change == 'comment':
                    kanban.add_comment(target, 1, 'Une précision arrivée pendant le vote.')
                else:
                    status = {'done': 'done', 'archive': 'archived'}.get(change, 'backlog')
                    kanban.update_task(target, 1, fields(title='Précision pendant le tour', status=status))
        result = run_live(SelectingProvider(target, hook))
        session = result['sessions'][0]
        task = kanban.task_detail(target)
        assert session['status'] == 'agreed'
        assert session['kanban']['outcome'] == 'conflict'
        assert task['version'] == 2 and task['latest_plan'] is None
        assert task['status'] != 'planned'
        assert get_db().execute("SELECT count(*) FROM tasks WHERE topic='cspilot'").fetchone()[0] == 1


@pytest.mark.parametrize('invalid_target', ['other_topic', 'demo', 'missing'])
def test_cannot_target_unseen_or_other_project_or_demo_card(app, invalid_target):
    with app.app_context():
        target = 999 if invalid_target == 'missing' else kanban.create_task(
            fields(topic='self' if invalid_target == 'other_topic' else 'cspilot'),
            mode='demo' if invalid_target == 'demo' else 'live')
        result = run_live(SelectingProvider(target))
        assert result['sessions'][0]['status'] == 'failed'
        assert result['sessions'][0]['kanban'] is None
        if target != 999:
            assert kanban.task_detail(target)['version'] == 1


@pytest.mark.parametrize('ending', ['none', 'reject', 'failure'])
def test_no_mission_without_successful_agreement(app, ending):
    with app.app_context():
        def hook(request, reply):
            if request['stage'] == 'propose' and ending == 'none':
                reply.payload['plan']['action'] = 'none'
            if request['stage'] == 'vote':
                if ending == 'reject':
                    reply.payload['plan_id'] = 'not-the-agreed-plan'
                if ending == 'failure':
                    raise RuntimeError('failure')
        run_live(SelectingProvider(hook=hook))
        assert get_db().execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
        assert get_db().execute('SELECT count(*) FROM task_decisions').fetchone()[0] == 0


def test_card_and_agreement_commit_or_roll_back_together(app, monkeypatch):
    with app.app_context():
        def fail_event(*args):
            raise RuntimeError('simulated disk failure')
        monkeypatch.setattr(kanban, '_event', fail_event)
        result = run_live(SelectingProvider())
        assert all(s['status'] == 'failed' for s in result['sessions'])
        assert get_db().execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
        assert get_db().execute('SELECT count(*) FROM task_decisions').fetchone()[0] == 0


def test_queue_is_bounded_prioritized_and_excludes_closed_cards(app):
    with app.app_context():
        low = kanban.create_task(fields(title='Travail commencé', priority=3))
        kanban.update_task(low, 1, fields(title='Travail commencé', priority=3, status='in_progress'))
        closed = kanban.create_task(fields(title='Déjà terminé'))
        kanban.update_task(closed, 1, fields(title='Déjà terminé', status='done'))
        for i in range(12):
            kanban.create_task(fields(title=f'Demande {i}', description='Long texte. ' * 320))
        context, snapshot = kanban.discussion_snapshot('cspilot', 'live')
        data = json.loads(context.split(':\n', 1)[1])
        assert len(context) <= 4000 and len(snapshot) <= 8
        assert data['cards'][0]['id'] == low and closed not in snapshot
        assert data['active_total'] == 13 and data['omitted'] > 0
        assert any(c['description_truncated'] for c in data['cards'])


def test_demo_is_separate_and_can_be_browsed_exported_without_real_tasks(app):
    with app.app_context():
        target = kanban.create_task(fields())
        cid, _ = enqueue('demo')
        run_cycle(cid)
        assert kanban.task_detail(target)['status'] == 'backlog'
        assert len(kanban.board_tasks('demo')) == 2
        before = [tuple(r) for r in get_db().execute('SELECT * FROM messages ORDER BY id')]
        tasks_before = [tuple(r) for r in get_db().execute('SELECT * FROM tasks ORDER BY id')]
        init_db()
        assert before == [tuple(r) for r in get_db().execute('SELECT * FROM messages ORDER BY id')]
        assert tasks_before == [tuple(r) for r in get_db().execute('SELECT * FROM tasks ORDER BY id')]
    client = app.test_client()
    assert len(client.get('/kanban/export').json['tasks']) == 1
    assert len(client.get('/kanban/export?mode=demo').json['tasks']) == 2
    assert 'Kanban de démonstration' in client.get('/kanban?mode=demo').get_data(as_text=True)
    for task_id in (1, 2, 3):
        assert client.get(f'/kanban/{task_id}').status_code == 200


def test_import_old_agreement_is_explicit_and_idempotent_without_ai(app, monkeypatch):
    with app.app_context():
        cid, _ = enqueue('live')
        session = get_db().execute('SELECT * FROM sessions WHERE cycle_id=? ORDER BY id', (cid,)).fetchone()
        plan = {'title': 'Ancienne mission', 'action': 'investigate', 'rationale': 'Besoin à préciser',
                'scope': ['Examiner la documentation'], 'acceptance_criteria': ['Constat documenté']}
        with get_db():
            get_db().execute("UPDATE sessions SET status='agreed',decision=?,plan_id=? WHERE id=?",
                             (json.dumps(plan), plan_hash(plan), session['id']))
        assert not kanban.board_tasks()
    monkeypatch.setattr(providers, 'build_provider', lambda *_: pytest.fail('No model call allowed'))
    client = app.test_client()
    csrf = token(client)
    for _ in range(2):
        response = client.post(f"/kanban/sessions/{session['id']}", data={'csrf_token': csrf}, follow_redirects=True)
        assert response.status_code == 200
    with app.app_context():
        assert len(kanban.board_tasks()) == 1
        assert len(kanban.task_detail(1)['events']) == 1
        assert get_db().execute('SELECT count(*) FROM messages').fetchone()[0] == 0
        assert json.loads(get_db().execute('SELECT decision FROM sessions WHERE id=?', (session['id'],)).fetchone()[0]) == plan


def test_new_card_with_same_title_is_not_silently_duplicated(app):
    with app.app_context():
        provider = SelectingProvider()
        title = provider.base.generate({'actor': 'A', 'topic': 'cspilot', 'stage': 'propose'}).payload['plan']['title']
        target = kanban.create_task(fields(title=title.upper()))
        result = run_live(provider)
        assert result['sessions'][0]['kanban']['outcome'] == 'conflict'
        assert len(kanban.board_tasks(topic='cspilot')) == 1
        assert kanban.task_detail(target)['version'] == 1


@pytest.mark.parametrize('value', [True, 0, -1, '1', 2**63])
def test_structured_plan_rejects_bad_card_ids(value):
    payload = providers.DemoProvider().generate({'actor': 'A', 'topic': 'cspilot', 'stage': 'propose'}).payload
    payload['plan']['task_id'] = value
    with pytest.raises(providers.ProviderError):
        providers._validate_payload(payload, 'propose')


def test_task_id_is_part_of_the_exact_plan_vote():
    plan = providers.DemoProvider().generate({'actor': 'A', 'topic': 'cspilot', 'stage': 'propose'}).payload['plan']
    assert plan_hash(plan) != plan_hash(plan | {'task_id': 1})


def test_kanban_keeps_documentation_and_total_context_budget(app, monkeypatch):
    from test_repository_reading import fake_repo
    fake_repo(monkeypatch)
    app.config.update(CSPILOT_REPO='owner/cspilot', SELF_REPO='owner/duo')
    with app.app_context():
        for i in range(8):
            kanban.create_task(fields(title=f'Question {i}', description='Texte long pour tester le budget. ' * 100))
        provider = SelectingProvider()
        cid, _ = enqueue('live')
        run_cycle(cid, provider=provider)
        first = provider.calls[0]
        assert first['reading_available'] is True
        assert 'Gère les activités' in first['context'] and 'Question 0' in first['context']
        assert len(first['context'].split('\n\nMémoire courte', 1)[0]) <= 12000
