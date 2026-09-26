"""Human-owned runtime settings. Agent plans never modify these values."""
import json
import re

from flask import current_app
from .db import get_db, now

DEFAULTS = {
    'CODEX_MODEL': '', 'CLAUDE_MODEL': '', 'CLI_TIMEOUT': 180,
    'EXECUTION_ENABLED': False, 'EXECUTION_AUTO': True,
    'EXECUTION_MINUTES': 30, 'EXECUTION_ATTEMPTS': 6,
    'EXECUTION_CALL_SECONDS': 600, 'EXECUTION_TEST_SECONDS': 180,
    'CSPILOT_TEST_IMAGE': 'cspilot-tests:local', 'SELF_TEST_IMAGE': 'duopilot-tests:local',
    'CSPILOT_TEST_COMMAND': ['python', '-m', 'pytest', '-q'],
    'SELF_TEST_COMMAND': ['python', '-m', 'pytest', '-q'],
}
BOUNDS = {'CLI_TIMEOUT': (10,600), 'EXECUTION_MINUTES': (1,240), 'EXECUTION_ATTEMPTS': (1,30),
          'EXECUTION_CALL_SECONDS': (10,1800), 'EXECUTION_TEST_SECONDS': (10,1800)}


def saved():
    row = get_db().execute('SELECT * FROM app_settings WHERE id=1').fetchone()
    return (json.loads(row['data']), row['version']) if row else ({}, 0)


def effective_config():
    values = dict(current_app.config)
    for key, default in DEFAULTS.items():
        values.setdefault(key, default)
    values.update(saved()[0])
    return values


def snapshot(config=None):
    config = config or effective_config()
    # Only non-secret, explicitly supported settings are journaled.
    return {key: config[key] for key in DEFAULTS}


def validate(values):
    result = {}
    for key in ('CODEX_MODEL','CLAUDE_MODEL'):
        value = str(values.get(key, '')).strip()
        if value and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:+/\-]{0,127}', value):
            raise ValueError('Identifiant de modèle invalide.')
        result[key] = value
    for key, (low, high) in BOUNDS.items():
        try:
            value = int(values.get(key, DEFAULTS[key]))
        except (ValueError,TypeError):
            raise ValueError(f'{key} doit être un nombre entier.') from None
        if not low <= value <= high:
            raise ValueError(f'{key} doit être compris entre {low} et {high}.')
        result[key] = value
    for key in ('EXECUTION_ENABLED','EXECUTION_AUTO'):
        result[key] = values.get(key) in (True,'true','1','on')
    for prefix in ('CSPILOT','SELF'):
        key = prefix+'_TEST_IMAGE'
        value = str(values.get(key, DEFAULTS[key])).strip()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:@\-]{0,199}', value):
            raise ValueError('Nom d’image Docker invalide.')
        result[key] = value
        key = prefix+'_TEST_COMMAND'
        value = values.get(key, DEFAULTS[key])
        try:
            value = json.loads(value) if isinstance(value,str) else value
        except ValueError:
            raise ValueError('La commande de test doit être un tableau JSON d’arguments.') from None
        if (not isinstance(value,list) or not 1 <= len(value) <= 30
                or any(not isinstance(v,str) or not v or len(v)>500 or '\x00' in v for v in value)):
            raise ValueError('Commande de test invalide.')
        result[key] = value
    return result


def save(values, version):
    values = validate(values)
    db = get_db()
    with db:
        db.execute('BEGIN IMMEDIATE')
        _, current = saved()
        if current != version:
            raise ValueError('Les réglages ont changé. Recharge la page.')
        db.execute('''INSERT INTO app_settings(id,data,version,updated_at) VALUES(1,?,?,?)
            ON CONFLICT(id) DO UPDATE SET data=excluded.data,version=excluded.version,updated_at=excluded.updated_at''',
            (json.dumps(values,ensure_ascii=False), current+1, now()))
    return values
