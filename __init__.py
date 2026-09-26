import os
import secrets
from pathlib import Path
from flask import Flask
from dotenv import load_dotenv
from .db import init_db, close_db

def create_app(test_config=None):
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / '.env', override=False)
    app = Flask(__name__, instance_path=str(root / 'instance'), instance_relative_config=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.config.from_mapping(
        DATABASE=str(root / 'instance' / 'duopilot.sqlite3'),
        SECRET_KEY=os.environ.get('SECRET_KEY') or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=8192, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
        TRUSTED_HOSTS=['localhost', '127.0.0.1', '[::1]'],
        USE_SUBSCRIPTION_CLI=os.environ.get('USE_SUBSCRIPTION_CLI', '').lower() == 'true',
        CODEX_BIN=os.environ.get('CODEX_BIN', 'codex'), CLAUDE_BIN=os.environ.get('CLAUDE_BIN', 'claude'),
        CODEX_MODEL=os.environ.get('CODEX_MODEL', ''), CLAUDE_MODEL=os.environ.get('CLAUDE_MODEL', ''),
        CLI_TIMEOUT=int(os.environ.get('CLI_TIMEOUT', '180')),
        CSPILOT_REPO=os.environ.get('CSPILOT_REPO', ''), SELF_REPO=os.environ.get('SELF_REPO', ''),
        CSPILOT_BASE_BRANCH=os.environ.get('CSPILOT_BASE_BRANCH', 'dev'),
        SELF_BASE_BRANCH=os.environ.get('SELF_BASE_BRANCH', 'main'),
        GITHUB_TOKEN=os.environ.get('GITHUB_TOKEN', ''), MAX_OPEN_PRS=3, MAX_CONTEXT_CHARS=12000,
    )
    if test_config:
        app.config.update(test_config)
    if not 10 <= app.config['CLI_TIMEOUT'] <= 600:
        raise ValueError('CLI_TIMEOUT doit être compris entre 10 et 600 secondes.')
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
    from .web import bp
    from .cli import register_commands
    app.register_blueprint(bp)
    register_commands(app)
    return app
