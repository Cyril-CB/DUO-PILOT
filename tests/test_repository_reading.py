import base64
import hashlib
import json

import pytest

from duopilot import create_app, github, repository, providers
from duopilot.orchestrator import enqueue, run_cycle, cycle_detail
from duopilot.db import get_db


COMMIT = 'a' * 40
TREE = 'b' * 40


def fake_repo(monkeypatch, files=None, extra_entries=None):
    files = files or {
        'README.md': '# Application\nGère les activités et les validations du centre.\n',
        'docs/fonctionnalites.md': '# Fonctionnalités\nLes fiches passent du salarié au responsable puis à la direction.\n',
        'app.py': 'def explain_validation():\n    return "Vérifier le rôle et la signature"\n',
        'tests/test_app.py': 'def test_validation():\n    assert True\n',
    }
    entries, blobs, calls = [], {}, []
    for path, text in files.items():
        raw = text.encode() if isinstance(text, str) else text
        sha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        entries.append({'path': path, 'type': 'blob', 'mode': '100644', 'size': len(raw), 'sha': sha})
        blobs[sha] = {'sha': sha, 'size': len(raw), 'encoding': 'base64', 'content': base64.b64encode(raw).decode()}
    entries.extend(extra_entries or [])

    def get_list(repo, endpoint, params, token):
        calls.append((repo, endpoint, params))
        if endpoint == 'commits':
            return [{'sha': COMMIT, 'commit': {'tree': {'sha': TREE}, 'message': 'Dernier état'}}], False
        return [], False

    def get_object(repo, endpoint, params, token, max_bytes=1_000_000):
        calls.append((repo, endpoint, params))
        if endpoint == f'git/trees/{TREE}':
            return {'sha': TREE, 'tree': entries, 'truncated': False}
        if endpoint.startswith('git/blobs/'):
            return dict(blobs[endpoint.split('/')[-1]])
        pytest.fail('Unexpected endpoint: ' + endpoint)

    monkeypatch.setattr(github, '_get_json', get_list)
    monkeypatch.setattr(github, '_get_object', get_object)
    return calls, get_object


def config():
    return {'CSPILOT_REPO': 'owner/cspilot', 'SELF_REPO': 'owner/duo',
            'CSPILOT_BASE_BRANCH': 'dev', 'SELF_BASE_BRANCH': 'main',
            'MAX_CONTEXT_CHARS': 12000, 'GITHUB_TOKEN': 'private-token-for-test'}


def test_documentation_is_present_before_the_first_message_without_issues(monkeypatch):
    calls, _ = fake_repo(monkeypatch)
    reader = repository.build_discussion_context(config(), 'cspilot', 'live')
    assert reader.available
    assert [r['path'] for r in reader.events] == ['README.md', 'docs/fonctionnalites.md']
    assert all(r['after_turn'] == 0 and r['actor'] is None for r in reader.events)
    assert all(r['snapshot_commit'] == COMMIT and r['status'] == 'read' for r in reader.events)
    context = reader.render()
    assert 'Gère les activités' in context
    assert 'du salarié au responsable puis à la direction' in context
    assert 'private-token-for-test' not in context + json.dumps(reader.events)
    assert len(context) <= 12000
    assert all(endpoint in ('issues', 'pulls', 'commits') or endpoint.startswith('git/') for _, endpoint, _ in calls)


def test_requested_code_is_pinned_numbered_and_shared_in_exactly_twelve_calls(monkeypatch, tmp_path):
    calls, _ = fake_repo(monkeypatch)
    app = create_app(dict(config(), TESTING=True, DATABASE=str(tmp_path / 'live.sqlite3')))
    observations = []

    class ReaderProvider:
        def generate(self, request):
            observations.append(request)
            reply = providers.DemoProvider().generate(request)
            if request['turn'] == 1:
                assert 'Gère les activités' in request['context']
                assert 'def explain_validation' not in request['context']
                reply.payload['read_requests'] = [{'path': 'app.py', 'start_line': 1}]
            elif request['turn'] == 2:
                assert '1: def explain_validation' in request['context']
                assert COMMIT in request['context']
                reply.payload['read_requests'] = [{'path': 'app.py', 'start_line': 1}]
            assert request['reading_available']
            return reply

    with app.app_context():
        cid, _ = enqueue('live')
        assert run_cycle(cid, provider=ReaderProvider())
        detail = cycle_detail(cid)
        assert detail['cycle']['status'] == 'completed'
        assert len(observations) == 12
        for session in detail['sessions']:
            assert len(session['messages']) == 6
            assert [r['status'] for r in session['source_reads']] == ['read', 'read', 'read', 'cached']
            assert session['source_reads'][2]['after_turn'] == 1
            assert session['source_reads'][2]['snapshot_commit'] == COMMIT
            assert session['source_reads'][3]['reference'] == session['source_reads'][2]['reference']
            for message in session['messages']:
                expected = next(r for r in observations if r['topic'] == session['topic'] and r['turn'] == message['turn'])
                assert message['input_context'] == expected['context']
            assert sum(m['actor'] == 'A' for m in session['messages']) == 3
        assert not run_cycle(cid, provider=ReaderProvider())
        assert len(observations) == 12
    # Each topic fetches two docs and one source; the repeated read is cached.
    assert sum(endpoint.startswith('git/blobs/') for _, endpoint, _ in calls) == 6
    exported = app.test_client().get(f'/cycles/{cid}/export').json
    assert exported['sessions'] == detail['sessions']
    assert exported['execution_enabled'] is False
    html = app.test_client().get(f'/cycles/{cid}').get_data(as_text=True)
    assert 'Documentation et code' in html
    assert 'Contexte envoyé pour ce tour' in html


def test_inaccessible_paths_and_symlinks_do_not_trigger_blob_requests(monkeypatch):
    calls, _ = fake_repo(monkeypatch, extra_entries=[
        {'path': 'link.py', 'type': 'blob', 'mode': '120000', 'size': 3, 'sha': 'c' * 40},
        {'path': 'huge.py', 'type': 'blob', 'mode': '100644', 'size': 300000, 'sha': 'd' * 40},
    ])
    reader = repository.build_discussion_context(config(), 'self', 'live')
    before = len(calls)
    for turn, paths in enumerate((('.env', '../app.py'), ('instance/client.py', 'https://example.org/app.py'),
                                  ('link.py', 'huge.py'), ('auth.json', '/app.py')), 1):
        reader.request_reads([{'path': p, 'start_line': 1} for p in paths], actor='A', after_turn=turn)
    assert len(calls) == before
    assert all(r['status'] in ('refused', 'not_found') for r in reader.events[2:])
    assert reader.used_requests == 8
    reader.request_reads([{'path': 'app.py', 'start_line': 1}], actor='B', after_turn=4)
    assert reader.events[-1]['status'] == 'limited'
    assert len(calls) == before


def test_context_budget_and_partial_lines_are_reported(monkeypatch):
    files = {f'module_{i}.py': ('# ' + 'x' * 500 + '\n') * 100 for i in range(10)}
    fake_repo(monkeypatch, files)
    reader = repository.build_discussion_context(config(), 'cspilot', 'live')
    for turn in range(1, 5):
        reader.request_reads([{'path': f'module_{2 * (turn - 1) + i}.py', 'start_line': 1} for i in range(2)],
                             actor='A', after_turn=turn)
        assert len(reader.render()) <= 12000
    assert any(r['status'] == 'limited' for r in reader.events)
    read = [r for r in reader.events if r['status'] == 'read']
    assert read and all(r['truncated'] and r['last_line_partial'] for r in read)
    assert all(r['end_line'] < r['total_lines'] for r in read)


def test_later_lines_use_the_same_blob_even_if_the_branch_moves(monkeypatch):
    calls, _ = fake_repo(monkeypatch, {'app.py': '\n'.join(f'# line {i}' for i in range(1, 141))})
    reader = repository.build_discussion_context(config(), 'cspilot', 'live')
    monkeypatch.setattr(github, '_get_json', lambda *args: pytest.fail('The branch must not be queried again'))
    reader.request_reads([{'path': 'app.py', 'start_line': 1}], actor='A', after_turn=1)
    reader.request_reads([{'path': 'app.py', 'start_line': 61}], actor='B', after_turn=2)
    assert '61: # line 61' in reader.render()
    assert all(r['snapshot_commit'] == COMMIT for r in reader.events)
    assert sum(endpoint.startswith('git/blobs/') for _, endpoint, _ in calls) == 1


def test_blob_mismatch_and_binary_are_not_presented_as_read(monkeypatch):
    _, original = fake_repo(monkeypatch, {'app.py': 'print(1)', 'binary.py': b'a\0b'})
    reader = repository.build_discussion_context(config(), 'self', 'live')

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[1].startswith('git/blobs/'):
            result['content'] = base64.b64encode(b'changed').decode()
        return result

    monkeypatch.setattr(github, '_get_object', changed)
    reader.request_reads([{'path': 'app.py', 'start_line': 1}], actor='A', after_turn=1)
    assert reader.events[-1]['status'] == 'error'
    assert 'changed' not in reader.render()
    monkeypatch.setattr(github, '_get_object', original)
    reader.request_reads([{'path': 'binary.py', 'start_line': 1}], actor='B', after_turn=2)
    assert reader.events[-1]['status'] == 'error'
    assert not reader.blocks


def test_tree_failure_is_explicit_and_does_not_invent_source_evidence(monkeypatch):
    fake_repo(monkeypatch)
    def fail(*args, **kwargs):
        raise github.GitHubError('Lecture GitHub impossible (HTTP 403).')
    monkeypatch.setattr(github, '_get_object', fail)
    reader = repository.build_discussion_context(config(), 'self', 'live')
    assert not reader.available
    assert 'indisponible' in reader.render()
    assert not reader.events


@pytest.mark.parametrize('items', [[{'path': 'app.py', 'start_line': True}],
                                    [{'path': 'app.py', 'start_line': 0}],
                                    [{'path': 'app.py', 'start_line': 1, 'url': 'https://example.org'}],
                                    [{'path': 'app.py', 'start_line': 1}] * 3])
def test_invalid_requests_are_rejected_by_the_connector(items):
    with pytest.raises(providers.ProviderError):
        providers._validate_payload({'message': 'Lire ce fichier', 'read_requests': items}, 'discuss')


def test_object_transport_stays_on_github_and_refuses_redirects(monkeypatch):
    observed = []
    class Response:
        status_code = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def iter_content(self, chunk_size): yield json.dumps({'sha': TREE, 'tree': []}).encode()
    def get(url, **kwargs):
        observed.append((url, kwargs))
        return Response()
    monkeypatch.setattr(github.requests, 'get', get)
    assert github._get_object('owner/repo', f'git/trees/{TREE}', {}, 'secret')['sha'] == TREE
    assert observed[0][0] == f'https://api.github.com/repos/owner/repo/git/trees/{TREE}'
    assert observed[0][1]['allow_redirects'] is False
    with pytest.raises(github.GitHubError):
        github._get_object('owner/repo', '../credentials', {}, 'secret')
    assert len(observed) == 1
    Response.status_code = 302
    with pytest.raises(github.GitHubError):
        github._get_object('owner/repo', f'git/trees/{TREE}', {}, 'secret')


def test_inspect_command_reads_docs_without_calling_models_or_creating_cycle(monkeypatch, tmp_path):
    fake_repo(monkeypatch)
    monkeypatch.setattr(providers, 'build_provider', lambda *a: pytest.fail('No AI call in inspection'))
    app = create_app(dict(config(), TESTING=True, DATABASE=str(tmp_path / 'inspection.sqlite3')))
    result = app.test_cli_runner().invoke(args=['inspect-repo', '--topic', 'cspilot'])
    assert result.exit_code == 0, result.output
    assert 'README.md : read' in result.output
    assert 'docs/fonctionnalites.md : read' in result.output
    assert 'Gère les activités' not in result.output
    assert 'private-token-for-test' not in result.output
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM cycles').fetchone()[0] == 0
