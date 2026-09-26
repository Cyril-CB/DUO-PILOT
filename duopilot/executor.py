"""Journaled execution state machine. No merge, deployment or arbitrary shell."""
from datetime import datetime, timezone
from functools import wraps
import json
from pathlib import Path
import sqlite3
import time
import uuid

from flask import current_app

from . import kanban, settings
from .db import get_db, now
from .execution_io import DockerTests, ExecutionError, GitHubPRs, Workspace, repo_url, valid_branch
from .execution_prompts import CLIExecutionAgent
from .providers import ProviderError

ACTIVE = ('queued','running','paused','blocked','pr_open')
PUBLIC_FIELDS = {'status','phase','task_version','settings_snapshot','attempts','calls','elapsed_seconds',
    'started_at','heartbeat_at','owner','cancel_requested','base_sha','tested_sha','approved_sha','published_sha',
    'test_image_id','checkpoint','test_report','latest_review','review_reads','pr_number','pr_url','error','plan',
    'developer','reviewer'}


def _row(job_id):
    row = get_db().execute('SELECT * FROM executions WHERE id=?',(job_id,)).fetchone()
    if row is None:
        raise ExecutionError('Exécution introuvable.')
    return dict(row)


def _update(job_id, **values):
    if not set(values)<=PUBLIC_FIELDS:
        raise RuntimeError('Unsupported execution field')
    db = get_db()
    with db:
        db.execute('UPDATE executions SET '+','.join(key+'=?' for key in values)+',updated_at=? WHERE id=?',
                   (*values.values(),now(),job_id))


def event(job_id,kind,data,actor=None,status='complete'):
    db = get_db()
    with db:
        cursor = db.execute('INSERT INTO execution_events(execution_id,kind,actor,status,data,created_at) VALUES(?,?,?,?,?,?)',
            (job_id,kind,actor,status,json.dumps(data,ensure_ascii=False),now()))
    return cursor.lastrowid


def detail(job_id):
    job = _row(job_id)
    for key in ('plan','settings_snapshot','checkpoint','test_report','latest_review'):
        job[key] = json.loads(job[key]) if job[key] else None
    job['events'] = []
    for row in get_db().execute('SELECT * FROM execution_events WHERE execution_id=? ORDER BY id',(job_id,)):
        item = dict(row)
        item['data'] = json.loads(item['data'])
        job['events'].append(item)
    return job


def _dependency_ready(task):
    db = get_db()
    seen = set()
    while task:
        if task['id'] in seen:
            return False
        seen.add(task['id'])
        if task['depends_on']:
            dependency = db.execute('SELECT status FROM tasks WHERE id=?',(task['depends_on'],)).fetchone()
            if not dependency or dependency['status']!='done':
                return False
            # A manually completed card with an open PR is not integrated.
            if db.execute("SELECT 1 FROM executions WHERE task_id=? AND status IN ('queued','running','paused','blocked','pr_open')",
                          (task['depends_on'],)).fetchone():
                return False
        # Splitting an already dependent child must not bypass its prerequisite.
        task = db.execute('SELECT * FROM tasks WHERE id=?',(task['parent_id'],)).fetchone() if task['parent_id'] else None
    return True


def enqueue_task(task_id):
    config = settings.effective_config()
    db = get_db()
    with db:
        db.execute('BEGIN IMMEDIATE')
        task = db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not task or not task['latest_plan'] or task['status'] in ('done','archived','backlog'):
            raise ExecutionError('Choisir une mission avec un plan accepté et encore ouverte.')
        old = db.execute("SELECT id FROM executions WHERE task_id=? AND status IN ('queued','running','paused','blocked','pr_open')",(task_id,)).fetchone()
        if old:
            return old['id'],False
        if db.execute('SELECT 1 FROM tasks WHERE parent_id=?',(task_id,)).fetchone():
            raise ExecutionError('Cette mission est découpée : lancer une de ses sous-tâches.')
        if not _dependency_ready(task):
            raise ExecutionError('La sous-tâche précédente doit être terminée et sa PR intégrée avant ce travail.')
        plan = json.loads(task['latest_plan'])
        if plan['action'] in ('none','split') or task['developer'] not in ('A','B') or task['reviewer'] not in ('A','B') or task['developer']==task['reviewer']:
            raise ExecutionError('Plan ou répartition des rôles incomplet.')
        prefix = 'CSPILOT' if task['topic']=='cspilot' else 'SELF'
        repo = config.get(prefix+'_REPO','')
        base = config[prefix+'_BASE_BRANCH']
        if task['mode']=='live':
            if not config['EXECUTION_ENABLED']:
                raise ExecutionError('Activer le moteur dans Configuration après avoir préparé Git et les tests.')
            repo_url(repo)
            if not valid_branch(base):
                raise ExecutionError('Branche de départ invalide.')
        else:
            repo = 'demo/'+task['topic']
        stamp = now()
        branch = f'duo/task-{task_id}-'+uuid.uuid4().hex[:12]
        cursor = db.execute('''INSERT INTO executions(task_id,mode,topic,repo,base_branch,branch,plan,task_version,
            developer,reviewer,settings_snapshot,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (task_id,task['mode'],task['topic'],repo,base,branch,task['latest_plan'],task['version'],
             task['developer'],task['reviewer'],json.dumps(settings.snapshot(config)),stamp,stamp))
        job_id = cursor.lastrowid
    event(job_id,'queued',{'message':'Mission mise en attente du worker.'})
    return job_id,True


def schedule_ready_tasks():
    config = settings.effective_config()
    if not config['EXECUTION_ENABLED'] or not config['EXECUTION_AUTO']:
        return 0
    rows = get_db().execute('''SELECT id FROM tasks WHERE mode='live' AND status='planned'
        AND latest_plan IS NOT NULL AND NOT EXISTS(SELECT 1 FROM tasks child WHERE child.parent_id=tasks.id)
        AND NOT EXISTS(SELECT 1 FROM executions e WHERE e.task_id=tasks.id AND e.status IN ('queued','running','paused','blocked','pr_open'))
        ORDER BY priority,id LIMIT 20''').fetchall()
    count = 0
    for row in rows:
        try:
            _,created = enqueue_task(row['id'])
            count += int(created)
        except ExecutionError:
            continue
    return count


class Budget:
    def __init__(self,job,owner):
        self.id,self.owner = job['id'],owner
        self.config = json.loads(job['settings_snapshot'])
        self.used = job['elapsed_seconds']
        self.start,self.last_save = time.monotonic(),0

    def remaining(self):
        self.tick()
        return max(.01,self.config['EXECUTION_MINUTES']*60-self.used-(time.monotonic()-self.start))

    def tick(self):
        elapsed = self.used+time.monotonic()-self.start
        job = _row(self.id)
        if job['owner']!=self.owner or job['status']!='running':
            raise ExecutionError('Exécution arrêtée par un autre processus.')
        if job['cancel_requested']:
            raise ExecutionError('Pause demandée depuis l’application.')
        if job['mode']=='live' and not settings.effective_config()['EXECUTION_ENABLED']:
            raise ExecutionError('Moteur désactivé depuis la configuration.')
        task = get_db().execute('SELECT version FROM tasks WHERE id=?',(job['task_id'],)).fetchone()
        if not task or task['version']!=job['task_version']:
            raise ExecutionError('La carte a changé pendant l’exécution. Relire les modifications avant de reprendre.')
        if elapsed>=self.config['EXECUTION_MINUTES']*60:
            raise ExecutionError('Limite de durée de la mission atteinte.')
        if time.monotonic()-self.last_save>=2:
            _update(self.id,elapsed_seconds=elapsed,heartbeat_at=now())
            self.last_save = time.monotonic()

    def finish(self):
        job = _row(self.id)
        if job['owner']==self.owner:
            _update(self.id,elapsed_seconds=self.used+time.monotonic()-self.start,heartbeat_at=now())


class ReadBudget:
    def __init__(self,seconds=90):
        self.deadline = time.monotonic()+seconds
    def tick(self):
        if time.monotonic()>=self.deadline:
            raise ExecutionError('Délai de synchronisation atteint.')
    def remaining(self):
        self.tick()
        return self.deadline-time.monotonic()


def _task_state(job_id,status):
    db = get_db()
    with db:
        job = _row(job_id)
        changed = db.execute('''UPDATE tasks SET status=?,version=version+1,updated_at=? WHERE id=? AND version=?
            AND status NOT IN ('done','archived')''',(status,now(),job['task_id'],job['task_version'])).rowcount
        if not changed:
            raise ExecutionError('La carte a changé. Aucun état n’a été écrasé.')
        db.execute('UPDATE executions SET task_version=task_version+1,updated_at=? WHERE id=?',(now(),job_id))
        kanban._event(job['task_id'],'agents','execution',{'execution_id':job_id,'status':status})


def _claim(job_id):
    db = get_db()
    owner = uuid.uuid4().hex
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            job = _row(job_id)
            if job['status']!='queued' or db.execute("SELECT 1 FROM executions WHERE repo=? AND status='running'",(job['repo'],)).fetchone():
                return None
            db.execute("UPDATE executions SET status='running',owner=?,cancel_requested=0,started_at=coalesce(started_at,?),heartbeat_at=?,updated_at=? WHERE id=?",
                       (owner,now(),now(),now(),job_id))
        return owner
    except sqlite3.IntegrityError:
        return None


def _capacity(job,remote):
    heads = remote.open_heads()
    if job['branch'] in heads:
        return
    unfinished = {r['branch'] for r in get_db().execute('''SELECT branch FROM executions WHERE repo=? AND id!=?
        AND base_sha IS NOT NULL AND status IN ('queued','running','paused','blocked','pr_open')''',(job['repo'],job['id']))}
    if len(heads)+len(unfinished-set(heads))>=current_app.config['MAX_OPEN_PRS']:
        raise ExecutionError('Trois développements non intégrés sont déjà ouverts. Reprendre après une intégration ou un abandon.')


def _request(job,stage,workspace):
    plan = json.loads(job['plan'])
    requested = []
    # A small set of source requests is reread at the current commit. Long
    # conversations and entire execution logs are never replayed to a model.
    rows = get_db().execute("SELECT data FROM execution_events WHERE execution_id=? AND kind='reads' ORDER BY id DESC LIMIT 3",(job['id'],)).fetchall()
    for row in reversed(rows):
        requested.extend(json.loads(row['data'])['requests'])
    context = workspace.context(requested or None)
    excerpts,used = [],0
    for item in context['excerpts']:
        size = len(json.dumps(item,ensure_ascii=False))
        if used+size>30000:
            break
        excerpts.append(item)
        used+=size
    context['excerpts'] = excerpts
    comments = [json.loads(r['data']) for r in get_db().execute("SELECT data FROM task_events WHERE task_id=? AND event='comment' ORDER BY id DESC LIMIT 2",(job['task_id'],))]
    feedback = get_db().execute("SELECT data FROM execution_events WHERE execution_id=? AND kind='github_feedback' ORDER BY id DESC LIMIT 1",(job['id'],)).fetchone()
    return {'stage':stage,'actor':job['developer'] if stage=='develop' else job['reviewer'],'topic':job['topic'],
            'plan':plan,'code':context,'diff':workspace.diff(job['base_sha']),
            'tests':json.loads(job['test_report']) if job['test_report'] else None,
            'previous_review':json.loads(job['latest_review']) if job['latest_review'] else None,
            'user_comments':comments,'github_feedback':json.loads(feedback['data']) if feedback else [],
            'attempts_used':job['attempts'],'attempts_limit':json.loads(job['settings_snapshot'])['EXECUTION_ATTEMPTS']}


def _call(job,stage,agent,workspace,budget):
    budget.tick()
    config = json.loads(job['settings_snapshot'])
    if stage=='develop' and job['attempts']>=config['EXECUTION_ATTEMPTS']:
        raise ExecutionError('Limite de tentatives de développement atteinte.')
    if job['calls']>=config['EXECUTION_ATTEMPTS']*4:
        raise ExecutionError('Limite totale des appels de développement et de contrôle atteinte.')
    request = _request(job,stage,workspace)
    actor = request['actor']
    db = get_db()
    # Charge before calling: an interrupted or failed CLI is never a free retry.
    with db:
        db.execute('UPDATE executions SET calls=calls+1,attempts=attempts+?,updated_at=? WHERE id=?',
                   (1 if stage=='develop' else 0,now(),job['id']))
        cursor = db.execute('INSERT INTO execution_events(execution_id,kind,actor,status,data,created_at) VALUES(?,?,?,?,?,?)',
            (job['id'],stage,actor,'reserved',json.dumps({'request':request},ensure_ascii=False),now()))
        event_id = cursor.lastrowid
    reply = agent.generate(request,min(config['EXECUTION_CALL_SECONDS'],budget.remaining()),budget.tick)
    from .execution_prompts import validate
    validate(reply.payload,stage)
    with db:
        db.execute("UPDATE execution_events SET status='complete',data=? WHERE id=?",
                   (json.dumps({'request':request,'payload':reply.payload,'model':reply.model,
                                'input_tokens':reply.input_tokens,'output_tokens':reply.output_tokens},ensure_ascii=False),event_id))
    return reply.payload


def _pr_body(job):
    plan = json.loads(job['plan'])
    tests = json.loads(job['test_report'])
    review = json.loads(job['latest_review'])
    body = f"## Besoin\n{plan['rationale']}\n\n## Changements prévus\n"+'\n'.join('- '+x for x in plan['scope'])
    body += '\n\n## Critères\n'+'\n'.join('- '+x for x in plan['acceptance_criteria'])
    body += f"\n\n## Vérification\nTests réussis sur `{job['tested_sha']}`.\nCommande : `{json.dumps(tests['command'],ensure_ascii=False)}`."
    body += f"\nImage de test : `{tests['image']}`.\n\nRevue par {kanban.AGENTS[job['reviewer']]} : {review['message']}"
    body += '\n'+ '\n'.join('- '+x for x in review.get('findings',[]))
    body += f"\n\nDUO PILOT · carte #{job['task_id']} · exécution #{job['id']}.\nDéveloppement : {kanban.AGENTS[job['developer']]}. Aucune fusion ni activation automatique."
    return body.replace('@','@\u200b')[:15000]


def run_execution(job_id, *, agent=None, workspace_factory=Workspace, test_factory=DockerTests, github_factory=GitHubPRs):
    owner = _claim(job_id)
    if not owner:
        return False
    job = _row(job_id)
    budget = Budget(job,owner)
    config = settings.effective_config() | json.loads(job['settings_snapshot'])
    try:
        budget.tick()
        task = get_db().execute('SELECT * FROM tasks WHERE id=?',(job['task_id'],)).fetchone()
        if not _dependency_ready(task):
            raise ExecutionError('Dépendance non intégrée : reprendre après la sous-tâche précédente.')
        if job['mode']=='demo':
            _task_state(job_id,'in_progress')
            for stage,actor in (('develop',job['developer']),('tests',None),('review_code',job['reviewer'])):
                event(job_id,stage,{'message':'Simulation fictive : aucun modèle, test de dépôt ou appel GitHub.'},actor)
            _task_state(job_id,'review')
            _update(job_id,status='demo_completed',phase='done',error=None)
            return True
        if not config['EXECUTION_ENABLED']:
            raise ExecutionError('Le moteur d’exécution est désactivé.')
        remote = github_factory(job['repo'],config,budget)
        tests = test_factory(config,budget)
        workspace = workspace_factory(Path(config['EXECUTION_ROOT'])/str(job_id)/'repo',job,config,budget)
        if job['phase']=='prepare':
            _capacity(job,remote)
            prefix = 'CSPILOT' if job['topic']=='cspilot' else 'SELF'
            image = tests.resolve_image(config[prefix+'_TEST_IMAGE'])
            base = workspace.prepare()
            _update(job_id,base_sha=base,test_image_id=image)
            _task_state(job_id,'in_progress')
            _update(job_id,phase='develop',error=None)
            event(job_id,'prepared',{'base_sha':base,'image':image,'branch':job['branch']})
        else:
            workspace.prepare()
            if not job['test_image_id']:
                prefix = 'CSPILOT' if job['topic']=='cspilot' else 'SELF'
                _update(job_id,test_image_id=tests.resolve_image(config[prefix+'_TEST_IMAGE']))
        while True:
            budget.tick()
            job = _row(job_id)
            stage = job['phase']
            if stage in ('develop','review_code') and agent is None:
                agent = CLIExecutionAgent(config)
            if stage=='develop':
                payload = _call(job,'develop',agent,workspace,budget)
                if payload['status']=='blocked':
                    raise ExecutionError(payload['message'][:1000])
                if payload['status']=='read':
                    event(job_id,'reads',{'requests':payload['read_requests']})
                    continue
                intent = workspace.patch_intent(payload['patch'])
                _update(job_id,checkpoint=json.dumps(intent,ensure_ascii=False),phase='apply',approved_sha=None,tested_sha=None)
            elif stage=='apply':
                sha = workspace.finish_patch(json.loads(job['checkpoint']))
                _update(job_id,phase='tests',review_reads=0)
                event(job_id,'commit',{'sha':sha})
            elif stage=='tests':
                prefix = 'CSPILOT' if job['topic']=='cspilot' else 'SELF'
                report = tests.run(workspace,job['test_image_id'],config[prefix+'_TEST_COMMAND'])
                _update(job_id,test_report=json.dumps(report,ensure_ascii=False),tested_sha=report['sha'] if report['passed'] else None,phase='review_code')
                event(job_id,'tests',report)
            elif stage=='review_code':
                payload = _call(job,'review_code',agent,workspace,budget)
                if payload['verdict']=='needs_context':
                    if job['review_reads']>=2:
                        raise ExecutionError('Limite des lectures complémentaires du contrôleur atteinte.')
                    _update(job_id,review_reads=job['review_reads']+1)
                    event(job_id,'reads',{'requests':payload['read_requests']})
                    continue
                sha = workspace.head()
                _update(job_id,latest_review=json.dumps(payload,ensure_ascii=False))
                if payload['verdict']=='approve' and job['tested_sha']==sha and json.loads(job['test_report'])['passed']:
                    _update(job_id,approved_sha=sha,phase='publish')
                else:
                    _update(job_id,phase='develop',approved_sha=None)
            elif stage=='publish':
                sha = workspace.head()
                if sha!=job['approved_sha'] or sha!=job['tested_sha'] or not workspace.clean():
                    raise ExecutionError('Les tests et la revue ne portent plus sur le code actuel.')
                _capacity(job,remote)
                existing = remote.find(job['branch'])
                if existing and (existing.get('state')!='open' or existing.get('head',{}).get('sha') not in (sha,job['published_sha'])):
                    raise ExecutionError('La PR a changé hors de cette exécution. Actualiser son état avant de poursuivre.')
                budget.tick()
                workspace.push(sha)
                _update(job_id,published_sha=sha)
                pr = remote.publish(_row(job_id),sha,_pr_body(_row(job_id)))
                number = pr.get('number')
                if type(number) is not int or number<1 or pr.get('head',{}).get('sha')!=sha or pr.get('base',{}).get('ref')!=job['base_branch']:
                    raise ExecutionError('Réponse de création de PR incohérente ; reprendre pour vérifier la publication.')
                _update(job_id,pr_number=number,pr_url=f"https://github.com/{job['repo']}/pull/{number}",phase='done',error=None)
                try:
                    _task_state(job_id,'review')
                except ExecutionError:
                    _update(job_id,error='PR publiée ; la carte a changé entre-temps et son état a été conservé.')
                _update(job_id,status='pr_open')
                event(job_id,'published',{'number':number,'sha':sha,'url':_row(job_id)['pr_url']})
                break
            else:
                raise ExecutionError('Phase d’exécution inconnue.')
    except Exception as exc:
        safe = str(exc) if isinstance(exc,(ExecutionError,ProviderError)) else 'Erreur d’exécution. Les traces et le dossier de travail sont conservés.'
        current = _row(job_id)
        if current['owner']==owner:
            _update(job_id,status='paused' if current['cancel_requested'] else 'blocked',error=safe[:1000])
            with get_db():
                get_db().execute("UPDATE execution_events SET status='failed' WHERE execution_id=? AND status='reserved'",(job_id,))
            event(job_id,'stopped',{'message':safe[:1000]})
    finally:
        budget.finish()
    return True


def serialized_action(function):
    @wraps(function)
    def action(*args,**kwargs):
        with get_db():
            get_db().execute('BEGIN IMMEDIATE')
            return function(*args,**kwargs)
    return action


@serialized_action
def pause(job_id):
    job = _row(job_id)
    if job['status']=='running':
        _update(job_id,cancel_requested=1)
    elif job['status']=='queued':
        _update(job_id,status='paused',cancel_requested=1)


@serialized_action
def mark_interrupted(job_id):
    job = _row(job_id)
    if job['status']!='running':
        return False
    if job['heartbeat_at'] and (datetime.now(timezone.utc)-datetime.fromisoformat(job['heartbeat_at'])).total_seconds()<30:
        raise ExecutionError('Le worker donne encore signe de vie. Demande une pause, ou arrête-le avant de marquer l’interruption.')
    _update(job_id,status='paused',owner=None,cancel_requested=1,error='Worker interrompu ; tentatives réservées conservées.')
    with get_db():
        get_db().execute("UPDATE execution_events SET status='failed' WHERE execution_id=? AND status='reserved'",(job_id,))
    return True


@serialized_action
def resume(job_id):
    job = _row(job_id)
    if job['status'] not in ('paused','blocked','pr_open'):
        raise ExecutionError('Cette exécution ne peut pas être reprise dans son état actuel.')
    config = settings.effective_config()
    if job['mode']=='live' and not config['EXECUTION_ENABLED']:
        raise ExecutionError('Réactiver le moteur avant de reprendre.')
    task = get_db().execute('SELECT * FROM tasks WHERE id=?',(job['task_id'],)).fetchone()
    if not task or task['status'] in ('done','archived') or not task['latest_plan'] or not _dependency_ready(task):
        raise ExecutionError('Carte close ou dépendance non intégrée.')
    if get_db().execute('SELECT 1 FROM tasks WHERE parent_id=?',(task['id'],)).fetchone():
        raise ExecutionError('Cette carte a été découpée.')
    if task['developer'] not in ('A','B') or task['reviewer'] not in ('A','B') or task['developer']==task['reviewer']:
        raise ExecutionError('Répartition des rôles invalide.')
    phase = job['phase']
    if task['latest_plan']!=job['plan'] or job['status']=='pr_open':
        phase = 'develop'
    elif phase=='done' and job['pr_number']:
        phase = 'publish'
    old_config = json.loads(job['settings_snapshot'])
    prefix = 'CSPILOT' if job['topic']=='cspilot' else 'SELF'
    reset_tests = any(old_config[key]!=config[key] for key in (prefix+'_TEST_IMAGE',prefix+'_TEST_COMMAND'))
    if reset_tests and phase not in ('prepare','develop','apply'):
        phase = 'tests'
    if (phase=='develop' and job['attempts']>=config['EXECUTION_ATTEMPTS']) or job['elapsed_seconds']>=config['EXECUTION_MINUTES']*60:
        raise ExecutionError('Budget épuisé. Augmenter les limites dans Configuration avant une reprise ; les compteurs restent conservés.')
    _update(job_id,status='queued',phase=phase,owner=None,cancel_requested=0,error=None,
            settings_snapshot=json.dumps(settings.snapshot(config)),task_version=task['version'],plan=task['latest_plan'],
            developer=task['developer'],reviewer=task['reviewer'],
            test_image_id=None if reset_tests else job['test_image_id'])
    event(job_id,'resumed',{'message':'Reprise manuelle avec les réglages actuels, sans remise à zéro des compteurs.'})


@serialized_action
def cancel(job_id):
    job = _row(job_id)
    if job['status']=='running':
        raise ExecutionError('Demander une pause avant d’abandonner cette exécution.')
    if job['status'] not in ('queued','paused','blocked') or job['pr_number'] or job['published_sha']:
        raise ExecutionError('Une branche a peut-être déjà été publiée : reprendre pour retrouver la PR, puis la traiter dans GitHub.')
    _update(job_id,status='cancelled',error='Exécution abandonnée manuellement. Branche et fichiers conservés.')
    db = get_db()
    with db:
        db.execute("UPDATE tasks SET status='backlog',version=version+1,updated_at=? WHERE id=? AND version=? AND status NOT IN ('done','archived')",
                   (now(),job['task_id'],job['task_version']))
    event(job_id,'cancelled',{'message':'Aucun fichier ni branche supprimé.'})


def sync_pr(job_id, *, github_factory=GitHubPRs, feedback=False):
    job = _row(job_id)
    if not job['pr_number'] or job['mode']!='live':
        raise ExecutionError('Aucune PR réelle à synchroniser.')
    if job['status'] in ('merged','closed'):
        return {'state':'closed','merged':job['status']=='merged'}
    if job['status'] in ('queued','running'):
        raise ExecutionError('Attendre la fin du travail avant de synchroniser la PR.')
    remote = github_factory(job['repo'],settings.effective_config(),ReadBudget())
    pr = remote.request('GET',f"pulls/{job['pr_number']}")
    if pr.get('head',{}).get('ref')!=job['branch'] or pr.get('base',{}).get('ref')!=job['base_branch']:
        raise ExecutionError('La PR ne correspond plus à la mission.')
    db = get_db()
    with db:
        db.execute('BEGIN IMMEDIATE')
        current = _row(job_id)
        if current!=job:
            raise ExecutionError('Exécution modifiée pendant la synchronisation ; actualiser plus tard.')
        if pr.get('merged'):
            db.execute("UPDATE executions SET status='merged',phase='done',error=NULL,updated_at=? WHERE id=?",(now(),job_id))
            changed = db.execute("UPDATE tasks SET status='done',version=version+1,updated_at=? WHERE id=? AND version=? AND status NOT IN ('done','archived')",
                       (now(),job['task_id'],job['task_version'])).rowcount
            kanban._event(job['task_id'],'agents','execution',{'execution_id':job_id,'status':'merged',
                'message':'Fusion constatée.' if changed else 'Fusion constatée ; carte modifiée depuis, son état a été conservé.'})
            parent = db.execute('SELECT parent_id FROM tasks WHERE id=?',(job['task_id'],)).fetchone()[0]
            while parent and not db.execute("SELECT 1 FROM tasks WHERE parent_id=? AND status!='done'",(parent,)).fetchone():
                db.execute("UPDATE tasks SET status='done',version=version+1,updated_at=? WHERE id=? AND status NOT IN ('done','archived')",(now(),parent))
                kanban._event(parent,'agents','execution',{'status':'merged','message':'Toutes les sous-tâches sont terminées.'})
                parent = db.execute('SELECT parent_id FROM tasks WHERE id=?',(parent,)).fetchone()[0]
        elif pr.get('state')=='closed':
            db.execute("UPDATE executions SET status='closed',phase='done',error=?,updated_at=? WHERE id=?",('PR fermée sans fusion.',now(),job_id))
    if pr.get('merged'):
        event(job_id,'merged',{'message':'Fusion constatée sur GitHub. Aucune fusion effectuée par DUO PILOT.'})
    elif pr.get('state')=='closed':
        event(job_id,'closed',{'message':'PR fermée sans fusion ; la carte reste à examiner.'})
    elif feedback:
        event(job_id,'github_feedback',remote.feedback(job['pr_number']))
    return pr


def process_sync_request():
    db = get_db()
    with db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM execution_sync_requests ORDER BY created_at LIMIT 1').fetchone()
        if not row:
            return False
        db.execute('DELETE FROM execution_sync_requests WHERE execution_id=?',(row['execution_id'],))
    try:
        sync_pr(row['execution_id'],feedback=bool(row['feedback']))
    except Exception as exc:
        message = str(exc) if isinstance(exc,ExecutionError) else 'Réponse GitHub inexploitable. Actualiser plus tard.'
        event(row['execution_id'],'sync_error',{'message':message})
    return True


def queue_pr_syncs():
    """Read-only polling, called every five minutes by the worker; no AI call."""
    db = get_db()
    with db:
        db.execute('''INSERT OR IGNORE INTO execution_sync_requests(execution_id,feedback,created_at)
            SELECT id,0,? FROM executions WHERE mode='live' AND status='pr_open' ''',(now(),))
