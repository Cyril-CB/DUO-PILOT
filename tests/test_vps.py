import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat

from dotenv import dotenv_values
import pytest
from duopilot import create_app
from duopilot.db import get_db

spec = importlib.util.spec_from_file_location('vps_configure', Path(__file__).parents[1] / 'deploy' / 'configure.py')
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


def test_daily_enqueue_only_is_idempotent_and_worker_runs_it(tmp_path, monkeypatch):
    app = create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'queue.sqlite3'), 'SECRET_KEY': 'test'})
    runner = app.test_cli_runner()
    for _ in range(2):
        result = runner.invoke(args=['daily', '--mode', 'demo', '--enqueue-only'])
        assert result.exit_code == 0, result.output
    with app.app_context():
        db = get_db()
        assert db.execute('SELECT count(*) FROM cycles').fetchone()[0] == 1
        assert db.execute('SELECT status FROM cycles').fetchone()[0] == 'queued'
        assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
    monkeypatch.setattr('duopilot.executor.queue_pr_syncs', lambda: None)
    result = runner.invoke(args=['worker', '--once'])
    assert result.exit_code == 0, result.output
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM messages').fetchone()[0] == 12
        assert get_db().execute('SELECT status FROM cycles').fetchone()[0] == 'completed'


def test_live_enqueue_only_checks_readiness_without_starting_ai(tmp_path, monkeypatch):
    app = create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'queue.sqlite3')})
    monkeypatch.setattr('duopilot.web.readiness', lambda: ['Connexion manquante'])
    result = app.test_cli_runner().invoke(args=['daily', '--mode', 'live', '--enqueue-only'])
    assert result.exit_code != 0
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM cycles').fetchone()[0] == 0
    monkeypatch.setattr('duopilot.web.readiness', lambda: [])
    result = app.test_cli_runner().invoke(args=['daily', '--mode', 'live', '--enqueue-only'])
    assert result.exit_code == 0
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM messages').fetchone()[0] == 0


def test_initial_config_is_private_stable_and_keeps_existing_credentials(tmp_path, capsys):
    configure.initialize(tmp_path)
    path = tmp_path / '.env'
    first = path.read_bytes()
    values = dotenv_values(path)
    assert len(values['SECRET_KEY']) == 64
    assert values['USE_SUBSCRIPTION_CLI'] == 'false'
    assert values['GITHUB_TOKEN'] == ''
    configure.initialize(tmp_path)
    assert path.read_bytes() == first
    if os.name == 'posix':
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert values['SECRET_KEY'] not in capsys.readouterr().out


def test_dotenv_secret_roundtrip_atomic_replacement(tmp_path):
    path = tmp_path / '.env'
    values = {'SECRET_KEY': 'old', 'GITHUB_TOKEN': 'secret_test_only'}
    configure.write_env(path, values)
    old = path.read_bytes()
    with pytest.raises(ValueError):
        configure.write_env(path, {'INVALID\nKEY': 'ignored'})
    assert path.read_bytes() == old
    assert list(tmp_path.iterdir()) == [path]
    configure.write_env(path, {'SECRET_KEY': 'new', 'GITHUB_TOKEN': 'other_test_token'})
    assert dotenv_values(path)['GITHUB_TOKEN'] == 'other_test_token'


@pytest.mark.skipif(os.name != 'posix', reason='Linux deployment')
def test_config_refuses_symlink(tmp_path):
    external = tmp_path / 'external'
    external.write_text('unchanged')
    (tmp_path / '.env').symlink_to(external)
    with pytest.raises(ValueError):
        configure.initialize(tmp_path)
    with pytest.raises(ValueError):
        configure.write_env(tmp_path / '.env', {})
    assert external.read_text() == 'unchanged'


@pytest.mark.parametrize('status', ['queued', 'running'])
@pytest.mark.parametrize('table', ['cycles', 'executions'])
def test_reconfigure_refuses_pending_or_running_work(tmp_path, status, table):
    (tmp_path / 'instance').mkdir()
    with sqlite3.connect(tmp_path / 'instance' / 'duopilot.sqlite3') as db:
        db.execute(f'CREATE TABLE {table}(status TEXT)')
        db.execute(f'INSERT INTO {table} VALUES(?)', (status,))
    with pytest.raises(ValueError, match='travail'):
        configure.ensure_idle(tmp_path)


def test_reconfigure_requires_automatic_development_disabled(tmp_path):
    (tmp_path / 'instance').mkdir()
    with sqlite3.connect(tmp_path / 'instance' / 'duopilot.sqlite3') as db:
        db.execute('CREATE TABLE app_settings(id INTEGER, data TEXT)')
        db.execute('INSERT INTO app_settings VALUES(1,?)', (json.dumps({'EXECUTION_ENABLED': True}),))
    with pytest.raises(ValueError, match='Désactive'):
        configure.ensure_idle(tmp_path)


def test_guided_config_preserves_secret_and_validates_repos(tmp_path, monkeypatch, capsys):
    configure.initialize(tmp_path)
    before = dotenv_values(tmp_path / '.env')['SECRET_KEY']
    answers = iter(['https://github.com/owner/one', 'owner/one', '', 'owner/two', 'main'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    monkeypatch.setattr(configure.getpass, 'getpass', lambda prompt: 'github_pat_TESTPRIVATE')
    configure.configure(tmp_path)
    values = dotenv_values(tmp_path / '.env')
    assert values['SECRET_KEY'] == before
    assert values['CSPILOT_REPO'] == 'owner/one'
    assert values['CSPILOT_BASE_BRANCH'] == 'dev'
    assert values['GITHUB_TOKEN'] == values['GITHUB_WRITE_TOKEN'] == 'github_pat_TESTPRIVATE'
    assert values['USE_SUBSCRIPTION_CLI'] == 'true'
    output = capsys.readouterr().out
    assert before not in output and 'github_pat_TESTPRIVATE' not in output
