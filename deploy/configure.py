"""Local administrative helper. Run as the dedicated service account, never as root."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def write_env(path, values):
    """Replace a private dotenv atomically; do not print credentials."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Le fichier .env ne doit pas être un lien symbolique.')
    fd, temporary = tempfile.mkstemp(prefix='.env-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            for key, value in values.items():
                if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
                    raise ValueError('Nom de configuration invalide.')
                stream.write(key + '=' + json.dumps(str(value or ''), ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def initial_values():
    return {
        'SECRET_KEY': secrets.token_hex(32), 'USE_SUBSCRIPTION_CLI': 'false',
        'CODEX_BIN': '/home/duopilot/.local/bin/codex',
        'CLAUDE_BIN': '/home/duopilot/.local/bin/claude',
        'CODEX_MODEL': '', 'CLAUDE_MODEL': '', 'CLI_TIMEOUT': '180',
        'CSPILOT_REPO': '', 'CSPILOT_BASE_BRANCH': 'dev',
        'SELF_REPO': '', 'SELF_BASE_BRANCH': 'main',
        'GITHUB_TOKEN': '', 'GITHUB_WRITE_TOKEN': '',
        'GIT_BIN': '/usr/bin/git', 'DOCKER_BIN': '/usr/bin/docker',
    }


def initialize(root=ROOT):
    path = root / '.env'
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise ValueError('Le fichier .env ne doit pas être un lien symbolique.')
        print('Configuration existante conservée.')
        return
    write_env(path, initial_values())
    print('Configuration privée créée ; mode réel désactivé.')


def ensure_idle(root=ROOT):
    path = root / 'instance' / 'duopilot.sqlite3'
    if not path.exists():
        return
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'app_settings' in tables:
            row = db.execute('SELECT data FROM app_settings WHERE id=1').fetchone()
            if row and json.loads(row[0]).get('EXECUTION_ENABLED'):
                raise ValueError('Désactive d’abord le développement dans Configuration, puis attends la fin ou mets en pause les missions avant cette opération.')
        for table in ('cycles', 'executions'):
            if table in tables and db.execute(
                f"SELECT 1 FROM {table} WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone():
                raise ValueError('Un travail est en file ou en cours. Attends sa fin, ou mets-le en pause depuis l’application avant cette opération.')


def configure(root=ROOT):
    from dotenv import dotenv_values
    from duopilot.execution_io import valid_branch
    ensure_idle(root)
    path = root / '.env'
    if path.is_symlink():
        raise ValueError('Le fichier .env ne doit pas être un lien symbolique.')
    values = initial_values() | dict(dotenv_values(path, interpolate=False))
    print('Les modèles, durées et tentatives se règlent ensuite dans l’interface.')
    for prefix, label in (('CSPILOT', 'CS-PILOT'), ('SELF', 'DUO PILOT')):
        key = prefix + '_REPO'
        while True:
            answer = input(f'Dépôt {label}, propriétaire/dépôt [{values[key]}] : ').strip() or values[key]
            if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', answer or ''):
                values[key] = answer
                break
            print('Utilise propriétaire/dépôt, sans URL.')
        key = prefix + '_BASE_BRANCH'
        while True:
            answer = input(f'Branche de départ [{values[key]}] : ').strip() or values[key]
            if valid_branch(answer):
                values[key] = answer
                break
            print('Nom de branche invalide.')
    print('Jeton GitHub limité à ces deux dépôts : Contents et Pull requests en écriture, Issues en lecture.')
    print('Saisie masquée. Entrée conserve le jeton actuel. Le jeton reste dans .env, mode 600.')
    token = getpass.getpass('Jeton GitHub : ').strip()
    if token:
        if not re.fullmatch(r'[A-Za-z0-9_]+', token):
            raise ValueError('Format du jeton GitHub invalide ; aucune modification enregistrée.')
        values['GITHUB_TOKEN'] = values['GITHUB_WRITE_TOKEN'] = token
    values['USE_SUBSCRIPTION_CLI'] = 'true'
    write_env(path, values)
    print('Configuration enregistrée. Aucun appel IA effectué.')


def check_ready():
    from duopilot import create_app
    from duopilot.web import readiness
    app = create_app()
    with app.app_context():
        errors = readiness()
        if errors:
            raise ValueError('\n'.join(errors))
    print('Configuration locale prête. Les droits GitHub seront vérifiés lors des lectures et des PR.')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('init', 'config', 'idle', 'ready'))
    args = parser.parse_args(argv)
    try:
        {'init': initialize, 'config': configure, 'idle': ensure_idle, 'ready': check_ready}[args.action]()
    except (ValueError, OSError, sqlite3.Error) as exc:
        # Do not include arbitrary library/OS messages that might contain secret values.
        print(str(exc) if isinstance(exc, ValueError) else 'Opération impossible ; vérifier les chemins et permissions.', file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\nOpération annulée ; aucun nouvel appel IA.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
