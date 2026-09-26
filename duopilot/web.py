import hmac
import json
import secrets
import shutil
import ipaddress
import re
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import SecurityError
from .db import get_db
from .orchestrator import enqueue, run_cycle, cycle_detail, today
from . import settings

bp = Blueprint('web', __name__)

def readiness():
    c = settings.effective_config()
    errors = []
    if not c['USE_SUBSCRIPTION_CLI']:
        errors.append('Activer USE_SUBSCRIPTION_CLI=true dans .env après connexion aux deux outils.')
    for key in ('CODEX_BIN', 'CLAUDE_BIN'):
        if not shutil.which(c[key]):
            errors.append(f"{key} : outil absent ou introuvable.")
    for key in ('CSPILOT_REPO', 'SELF_REPO'):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', c[key]):
            errors.append(f'{key} : renseigner propriétaire/dépôt.')
    if c['USE_SUBSCRIPTION_CLI'] and all(shutil.which(c[key]) for key in ('CODEX_BIN','CLAUDE_BIN')):
        from .providers import validate_live_config, ProviderError
        try:
            validate_live_config(c)
        except ProviderError as exc:
            errors.append(str(exc))
    return errors

def public_config():
    c = settings.effective_config()
    return dict(cspilot_repo=c['CSPILOT_REPO'], self_repo=c['SELF_REPO'],
                codex_model=c['CODEX_MODEL'] or 'Choix par défaut de Codex',
                claude_model=c['CLAUDE_MODEL'] or 'Choix par défaut de Claude Code')

def csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']

@bp.app_context_processor
def helpers():
    return {'csrf_token': csrf_token}

@bp.before_app_request
def local_only():
    # Personal local starter. Do not expose through a reverse proxy.
    try:
        allowed = ipaddress.ip_address(request.remote_addr or '').is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        abort(403, description='Cette version est accessible uniquement en local.')
    if request.method == 'POST':
        expected = session.get('csrf_token', '')
        supplied = request.form.get('csrf_token', '')
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(400, description='Formulaire expiré. Recharge la page puis réessaie.')

@bp.after_app_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
    response.headers['Cache-Control'] = 'no-store'
    return response

@bp.app_errorhandler(400)
@bp.app_errorhandler(403)
@bp.app_errorhandler(404)
@bp.app_errorhandler(413)
def error_page(error):
    if isinstance(error, SecurityError):
        return 'Hôte non autorisé. Utiliser http://127.0.0.1:5055.', 400, {'Content-Type':'text/plain; charset=utf-8'}
    return render_template('error.html', message=error.description, code=error.code), error.code

@bp.route('/')
def index():
    db = get_db()
    page = request.args.get('page', 1, type=int)
    if page < 1:
        abort(400, description='Numéro de page invalide.')
    total_cycles = db.execute('SELECT count(*) FROM cycles').fetchone()[0]
    total_pages = max(1, (total_cycles + 29) // 30)
    if page > total_pages:
        abort(404)
    cycles = [dict(row) for row in db.execute('''SELECT c.*,
      (SELECT count(*) FROM messages m JOIN sessions s ON m.session_id=s.id WHERE s.cycle_id=c.id) AS message_count,
      (SELECT count(*) FROM sessions s WHERE s.cycle_id=c.id AND s.status='agreed') AS agreed_count
      ,(SELECT count(*) FROM sessions s WHERE s.cycle_id=c.id) AS subject_count
      FROM cycles c ORDER BY c.run_date DESC,c.id DESC LIMIT 30 OFFSET ?''', ((page - 1) * 30,))]
    stats = {'cycles': total_cycles,
             'messages': db.execute('SELECT count(*) FROM messages').fetchone()[0],
             'agreed': db.execute("SELECT count(*) FROM sessions WHERE status='agreed'").fetchone()[0]}
    errors = readiness()
    return render_template('dashboard.html', cycles=cycles, today=today(), live_ready=not errors,
                           readiness=errors, config_public=public_config(), stats=stats,
                           page=page, total_pages=total_pages, manual_key=secrets.token_hex(16))

@bp.route('/cycles', methods=['POST'])
def create_cycle():
    mode = request.form.get('mode', 'demo')
    if mode not in ('demo', 'live'):
        abort(400, description='Mode invalide.')
    if mode == 'live' and readiness():
        flash('La connexion aux agents doit être préparée avant un cycle réel.', 'warning')
        return redirect(url_for('web.configuration'))
    manual_key = request.form.get('manual_key') or None
    subject = request.form.get('topic','both')
    if subject not in ('both','cspilot','self'):
        abort(400,description='Sujet invalide.')
    try:
        cycle_id, created = enqueue(mode, manual_key=manual_key,
                                   topics=('cspilot','self') if subject=='both' or not manual_key else (subject,))
    except ValueError as exc:
        abort(400,description=str(exc))
    if created and mode == 'demo':
        run_cycle(cycle_id)
    elif created:
        flash('Cycle en attente. Le worker le traitera dans un terminal séparé.', 'info')
    else:
        flash('Ce cycle existe déjà pour aujourd’hui : aucun message supplémentaire.', 'info')
    return redirect(url_for('web.cycle', cycle_id=cycle_id))

@bp.route('/cycles/<int:cycle_id>')
def cycle(cycle_id):
    detail = cycle_detail(cycle_id)
    if detail is None:
        abort(404)
    return render_template('cycle.html', **detail)

@bp.route('/cycles/<int:cycle_id>/export')
def export_cycle(cycle_id):
    detail = cycle_detail(cycle_id)
    if detail is None:
        abort(404)
    detail['version'] = '0.4.1'
    detail['execution_enabled'] = settings.effective_config()['EXECUTION_ENABLED']
    detail['kanban_enabled'] = True
    response = current_app.response_class(json.dumps(detail, ensure_ascii=False, indent=2), mimetype='application/json')
    response.headers['Content-Disposition'] = f'attachment; filename="duo-pilot-cycle-{cycle_id}.json"'
    return response

@bp.route('/configuration', methods=['GET','POST'])
def configuration():
    error = None
    code = 200
    if request.method == 'POST':
        try:
            settings.save(request.form,int(request.form.get('version','-1')))
        except (ValueError,TypeError) as exc:
            error,code = str(exc),400
        else:
            flash('Réglages enregistrés. Ils s’appliqueront aux nouveaux cycles et nouvelles exécutions.','info')
            return redirect(url_for('web.configuration'))
    errors = readiness()
    config = settings.effective_config()
    execution_readiness = []
    for key,label in (('GIT_BIN','Git'),('DOCKER_BIN','Docker')):
        if not shutil.which(config[key]):
            execution_readiness.append(label+' est absent ou introuvable.')
    if not (config.get('GITHUB_WRITE_TOKEN') or config.get('GITHUB_TOKEN')):
        execution_readiness.append('Renseigner GITHUB_WRITE_TOKEN dans .env pour publier les branches et PR.')
    return render_template('configuration.html', readiness=errors, live_ready=not errors,
                           config_public=public_config(), settings=config, settings_version=settings.saved()[1],
                           error=error,execution_readiness=execution_readiness),code
