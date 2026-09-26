import json

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from . import executor, settings
from .db import get_db, now
from .execution_io import ExecutionError

bp = Blueprint('execution',__name__,url_prefix='/executions')
STATUSES = {'queued':'En attente','running':'En cours','paused':'En pause','blocked':'À débloquer',
            'pr_open':'PR ouverte','merged':'Fusion constatée','closed':'PR fermée','cancelled':'Abandonnée',
            'demo_completed':'Simulation terminée'}
PHASES = {'prepare':'Préparation','develop':'Développement','apply':'Application du patch',
          'tests':'Tests','review_code':'Contrôle','publish':'Publication de la PR','done':'Suivi'}


@bp.app_context_processor
def labels():
    return {'execution_statuses':STATUSES,'execution_phases':PHASES}


@bp.route('')
def index():
    rows = get_db().execute('''SELECT e.*,t.title FROM executions e JOIN tasks t ON t.id=e.task_id ORDER BY e.id DESC LIMIT 100''').fetchall()
    return render_template('executions.html',jobs=[dict(r) for r in rows])


@bp.route('/task/<int:task_id>',methods=['POST'])
def enqueue(task_id):
    try:
        job_id,_ = executor.enqueue_task(task_id)
    except ExecutionError as exc:
        flash(str(exc),'error')
        return redirect(url_for('kanban.detail',task_id=task_id))
    flash('Mission mise en attente du worker.','info')
    return redirect(url_for('execution.show',job_id=job_id))


@bp.route('/<int:job_id>')
def show(job_id):
    try:
        job = executor.detail(job_id)
    except ExecutionError:
        abort(404)
    return render_template('execution.html',job=job)


@bp.route('/<int:job_id>/<action>',methods=['POST'])
def action(job_id,action):
    try:
        if action in ('sync','feedback'):
            job = executor.detail(job_id)
            if not job['pr_number']:
                raise ExecutionError('Aucune PR à actualiser.')
            with get_db():
                get_db().execute('''INSERT INTO execution_sync_requests(execution_id,feedback,created_at) VALUES(?,?,?)
                    ON CONFLICT(execution_id) DO UPDATE SET feedback=max(feedback,excluded.feedback)''',
                    (job_id,1 if action=='feedback' else 0,now()))
            flash('Actualisation mise en attente du worker ; aucun appel IA.','info')
        elif action in ('pause','resume','cancel','mark-interrupted'):
            method = {'pause':executor.pause,'resume':executor.resume,'cancel':executor.cancel,
                      'mark-interrupted':executor.mark_interrupted}[action]
            method(job_id)
            flash('Demande enregistrée. Les compteurs de consommation sont conservés.','info')
        else:
            abort(404)
    except ExecutionError as exc:
        flash(str(exc),'error')
    return redirect(url_for('execution.show',job_id=job_id))


@bp.route('/<int:job_id>/export')
def export(job_id):
    try:
        job = executor.detail(job_id)
    except ExecutionError:
        abort(404)
    response = current_app.response_class(json.dumps(job,ensure_ascii=False,indent=2),mimetype='application/json')
    response.headers['Content-Disposition'] = f'attachment; filename="duo-pilot-execution-{job_id}.json"'
    return response
