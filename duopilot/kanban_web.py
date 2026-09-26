"""Local Kanban forms; all POSTs use the application's existing CSRF guard."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from . import kanban
from .db import get_db
from .orchestrator import validate_plan
from .roles import roles_for

bp = Blueprint('kanban', __name__, url_prefix='/kanban')


@bp.app_template_filter('paris_time')
def paris_time(value):
    return datetime.fromisoformat(value).astimezone(ZoneInfo('Europe/Paris')).strftime('%d/%m/%Y à %H:%M')


@bp.context_processor
def labels():
    return {'task_topics': kanban.TOPICS, 'task_kinds': kanban.KINDS,
            'task_priorities': kanban.PRIORITIES, 'task_statuses': kanban.STATUSES,
            'agent_names': kanban.AGENTS}


def _filters():
    mode = request.args.get('mode', 'live')
    topic = request.args.get('topic') or None
    if mode not in ('live', 'demo') or topic not in (None, *kanban.TOPICS):
        abort(400, description='Filtre Kanban invalide.')
    return mode, topic


def _version():
    try:
        version = int(request.form.get('version', ''))
    except ValueError:
        raise ValueError('Version de carte invalide. Recharge la page.') from None
    if not 1 <= version <= 2**63 - 1:
        raise ValueError('Version de carte invalide. Recharge la page.')
    return version


@bp.route('')
def board():
    mode, topic = _filters()
    archived = request.args.get('archived') == '1'
    tasks = kanban.board_tasks(mode, topic, archived)
    columns = {'archived': kanban.STATUSES['archived']} if archived else {k: v for k, v in kanban.STATUSES.items() if k != 'archived'}
    upcoming = set()
    for subject in ([topic] if topic else kanban.TOPICS):
        upcoming.update(kanban.discussion_snapshot(subject, mode)[1])
    return render_template('kanban.html', tasks=tasks, columns=columns, mode=mode, topic=topic,
                           archived=archived, upcoming=upcoming)


@bp.route('/new', methods=['GET', 'POST'])
def new_task():
    mode, topic = _filters()
    values = {'title': '', 'description': '', 'topic': topic or 'cspilot', 'kind': 'task', 'priority': 2}
    error = None
    if request.method == 'POST':
        values.update(request.form)
        try:
            task_id = kanban.create_task(values, mode)
        except ValueError as exc:
            error = str(exc)
        else:
            flash('Carte ajoutée. Les agents pourront l’examiner lors d’un prochain cycle de ce mode.', 'info')
            return redirect(url_for('kanban.detail', task_id=task_id))
    return render_template('task_form.html', values=values, mode=mode, error=error), 400 if error else 200


@bp.route('/<int:task_id>', methods=['GET', 'POST'])
def detail(task_id):
    task = kanban.task_detail(task_id)
    if task is None:
        abort(404)
    values, error, code = task, None, 200
    if request.method == 'POST':
        values = dict(request.form)
        try:
            kanban.update_task(task_id, _version(), values)
        except ValueError as exc:
            error, code = str(exc), 409 if isinstance(exc, kanban.TaskConflict) else 400
        else:
            flash('Carte mise à jour.', 'info')
            return redirect(url_for('kanban.detail', task_id=task_id))
    return render_template('task_detail.html', task=task, values=values, error=error), code


@bp.route('/<int:task_id>/comments', methods=['POST'])
def comment(task_id):
    task = kanban.task_detail(task_id)
    if task is None:
        abort(404)
    try:
        kanban.add_comment(task_id, _version(), request.form.get('comment', ''))
    except ValueError as exc:
        return render_template('task_detail.html', task=task, values=task, error=str(exc),
                               comment_draft=request.form.get('comment', '')), 409 if isinstance(exc, kanban.TaskConflict) else 400
    flash('Retour enregistré pour les prochaines discussions.', 'info')
    return redirect(url_for('kanban.detail', task_id=task_id))


@bp.route('/sessions/<int:session_id>', methods=['POST'])
def import_decision(session_id):
    """Explicitly bring an old agreed plan into the board, without replaying AI."""
    db = get_db()
    with db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('''SELECT s.*,c.mode FROM sessions s JOIN cycles c ON c.id=s.cycle_id
            WHERE s.id=?''', (session_id,)).fetchone()
        if row is None:
            abort(404)
        existing = kanban.decision_result(session_id)
        if existing:
            result = existing
        else:
            if row['status'] != 'agreed':
                abort(400, description='Seul un plan accepté peut être ajouté au Kanban.')
            try:
                plan = validate_plan(json.loads(row['decision']))
            except (ValueError, TypeError):
                abort(400, description='Ce plan ancien ne contient pas les informations nécessaires.')
            roles = roles_for(row['opening_actor'])
            if not roles or plan['action'] == 'none' or plan.get('task_id') is not None:
                abort(400, description='Ce plan ne peut pas être importé automatiquement.')
            result = kanban.record_agreement(session_id, row['topic'], row['mode'], plan, roles, {})
    if result['task_id']:
        return redirect(url_for('kanban.detail', task_id=result['task_id']))
    flash(result['note'], 'warning')
    return redirect(url_for('web.cycle', cycle_id=row['cycle_id']))


@bp.route('/export')
def export_board():
    mode, topic = _filters()
    rows = kanban.board_tasks(mode, topic) + kanban.board_tasks(mode, topic, archived=True)
    payload = {'version': '0.4.1', 'mode': mode, 'topic': topic,
               'tasks': [kanban.task_detail(row['id']) for row in rows]}
    response = current_app.response_class(json.dumps(payload, ensure_ascii=False, indent=2), mimetype='application/json')
    response.headers['Content-Disposition'] = f'attachment; filename="duo-pilot-kanban-{mode}.json"'
    return response
