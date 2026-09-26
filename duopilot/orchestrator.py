"""Two bounded deliberations; no code execution or GitHub write capability."""
import hashlib
import json
import sqlite3
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo
from flask import current_app
from .db import get_db, now
from . import providers, github, repository, kanban, settings
from .roles import opening_actor_for, roles_for

TOPICS = ('cspilot', 'self')
ACTIONS = {'investigate', 'fix_bug', 'feature', 'improve_process', 'review', 'split', 'none'}

def today():
    return datetime.now(ZoneInfo('Europe/Paris')).date().isoformat()

def enqueue(mode='demo', run_date=None, *, manual_key=None, topics=TOPICS):
    if mode not in ('demo', 'live'):
        raise ValueError('Mode inconnu.')
    run_date = run_date or today()
    date.fromisoformat(run_date)
    topics = tuple(topics)
    if not topics or len(set(topics)) != len(topics) or any(t not in TOPICS for t in topics):
        raise ValueError('Sujets invalides.')
    if manual_key is not None and not re.fullmatch(r'[0-9a-f]{32}', manual_key):
        raise ValueError('Identifiant de lancement manuel invalide.')
    db = get_db()
    with db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM cycles WHERE request_key=?', (manual_key,)).fetchone() if manual_key else db.execute(
            'SELECT * FROM cycles WHERE run_date=? AND mode=? AND sequence=0', (run_date,mode)).fetchone()
        if row:
            if row['mode'] != mode:
                raise ValueError('Ce lancement appartient à un autre mode.')
            return row['id'], False
        sequence = db.execute('SELECT coalesce(max(sequence),0)+1 FROM cycles WHERE run_date=? AND mode=?',
                              (run_date,mode)).fetchone()[0] if manual_key else 0
        cursor = db.execute('''INSERT INTO cycles(run_date,mode,created_at,sequence,request_key,settings_snapshot)
            VALUES(?,?,?,?,?,?)''', (run_date,mode,now(),sequence,manual_key,json.dumps(settings.snapshot())))
        cycle_id = cursor.lastrowid
        for topic in topics:
            opener = opening_actor_for(run_date,topic)
            if sequence % 2:
                opener = 'B' if opener == 'A' else 'A'
            db.execute('INSERT INTO sessions(cycle_id,topic,opening_actor) VALUES(?,?,?)',(cycle_id,topic,opener))
    return cycle_id, True

def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get('action') not in ACTIONS:
        raise ValueError('Plan final absent ou action invalide.')
    result = {'action': plan['action']}
    task_id = plan.get('task_id')
    if task_id is not None and (type(task_id) is not int or not 1 <= task_id <= 2**63 - 1):
        raise ValueError('Référence de carte invalide.')
    result['task_id'] = task_id
    for key, limit in [('title', 200), ('rationale', 2000)]:
        value = plan.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError('Le plan final contient un texte invalide.')
        result[key] = value.strip()
    for key in ('scope', 'acceptance_criteria'):
        value = plan.get(key)
        if (not isinstance(value, list) or not 1 <= len(value) <= 8
                or any(not isinstance(x, str) or not x.strip() or len(x) > 600 for x in value)):
            raise ValueError('Le périmètre et les critères doivent être explicites.')
        result[key] = [x.strip() for x in value]
    children = plan.get('subtasks', [])
    if not isinstance(children,list) or len(children)>6:
        raise ValueError('Six sous-tâches au maximum.')
    if (plan['action']=='split' and len(children)<2) or (plan['action']!='split' and children):
        raise ValueError('Un découpage contient entre deux et six sous-tâches.')
    result['subtasks'] = []
    for index, child in enumerate(children):
        if not isinstance(child,dict) or child.get('action') in ('split','none'):
            raise ValueError('Sous-tâche invalide.')
        dependency = child.get('depends_on',0)
        if type(dependency) is not int or not 0 <= dependency <= index:
            raise ValueError('Une sous-tâche dépend seulement d’une étape précédente, ou de 0 pour aucune.')
        leaf = validate_plan({**child,'task_id':None,'subtasks':[]})
        result['subtasks'].append({k:v for k,v in leaf.items() if k not in ('task_id','subtasks')} | {'depends_on':dependency})
    if len(json.dumps(result,ensure_ascii=False))>30000:
        raise ValueError('Le plan et son découpage dépassent la taille autorisée.')
    return result

def plan_hash(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()[:20]

def recent_memory(topic, mode):
    rows = get_db().execute('''SELECT c.run_date,s.status,s.decision FROM sessions s
        JOIN cycles c ON c.id=s.cycle_id WHERE s.topic=? AND c.mode=?
        AND s.status IN ('agreed','rejected','no_action')
        ORDER BY c.run_date DESC LIMIT 3''', (topic, mode)).fetchall()
    items = []
    for row in rows:
        plan = json.loads(row['decision']) if row['decision'] else {}
        items.append(f"{row['run_date']} : {row['status']} — {plan.get('title','sans plan')[:200]}")
    return '\n'.join(items) or 'Aucune décision précédente dans ce mode.'

def run_cycle(cycle_id, provider=None, context_builder=None):
    """Atomic claim prevents double execution across web, worker and daily CLI."""
    db = get_db()
    with db:
        changed = db.execute("UPDATE cycles SET status='running' WHERE id=? AND status='queued'",
                             (cycle_id,)).rowcount
    if not changed:
        return False
    cycle = dict(db.execute('SELECT * FROM cycles WHERE id=?', (cycle_id,)).fetchone())
    if cycle['mode'] == 'live' and cycle['run_date'] != today():
        with db:
            db.execute("UPDATE cycles SET status='failed',finished_at=?,error=? WHERE id=?",
                       (now(), 'Cycle expiré : aucun rattrapage automatique des jours précédents.', cycle_id))
            db.execute("UPDATE sessions SET status='failed',error='Cycle expiré.' WHERE cycle_id=?", (cycle_id,))
        return True
    failed = False
    try:
        config = settings.effective_config()
        if cycle.get('settings_snapshot'):
            config.update(json.loads(cycle['settings_snapshot']))
        provider = provider or providers.build_provider(config, cycle['mode'])
        context_builder = context_builder or repository.build_discussion_context
        topics = [r['topic'] for r in db.execute('SELECT topic FROM sessions WHERE cycle_id=? ORDER BY id',(cycle_id,))]
        for topic in topics:
            session = dict(db.execute('SELECT * FROM sessions WHERE cycle_id=? AND topic=?',
                                      (cycle_id, topic)).fetchone())
            try:
                _run_session(cycle, session, provider, context_builder, config)
            except Exception as exc:
                failed = True
                # Never persist raw CLI stderr, network bodies, credentials or exception reprs.
                safe = str(exc) if isinstance(exc, (ValueError, providers.ProviderError)) else 'Échec de la collecte ou du traitement. Vérifier la connexion et la configuration.'
                safe = safe[:400]
                with db:
                    db.execute("UPDATE sessions SET status='failed',error=? WHERE id=?", (safe, session['id']))
                    db.execute("UPDATE messages SET status='failed' WHERE session_id=? AND status='reserved'", (session['id'],))
    except Exception:
        failed = True
        with db:
            db.execute("UPDATE sessions SET status='failed',error=? WHERE cycle_id=? AND status='pending'",
                       ('Impossible de préparer les agents. Exécuter flask doctor.', cycle_id))
    with db:
        db.execute('UPDATE cycles SET status=?,finished_at=? WHERE id=?',
                   ('failed' if failed else 'completed', now(), cycle_id))
    return True

def _run_session(cycle, session, provider, context_builder, config):
    db = get_db()
    topic = session['topic']
    # Freeze before the first intervention, including older queued sessions.
    first = session['opening_actor'] or opening_actor_for(cycle['run_date'], topic)
    roles = roles_for(first)
    if roles is None:
        raise ValueError('Répartition des rôles invalide.')
    second = roles['reviewer']
    task_context, task_snapshot = kanban.discussion_snapshot(topic, cycle['mode'])
    # Share the existing repository context envelope with local tasks. The
    # separate short memory and six-turn message budget remain unchanged.
    context_config = dict(config)
    context_config['MAX_CONTEXT_CHARS'] = max(0, context_config['MAX_CONTEXT_CHARS'] - len(task_context))
    source = context_builder(context_config, topic, cycle['mode'])
    reader = source if isinstance(source, repository.RepositoryContext) else None
    memory = '\n\nMémoire courte (décisions proposées, pas des travaux réalisés) :\n' + recent_memory(topic, cycle['mode'])
    base_context = reader.render() if reader else str(source)[:context_config['MAX_CONTEXT_CHARS']]
    context = base_context + task_context + memory
    saved_reads = 0

    def save_reads(events):
        nonlocal saved_reads
        with db:
            for event in events:
                saved_reads += 1
                db.execute('INSERT INTO source_reads(session_id,number,after_turn,result,created_at) VALUES(?,?,?,?,?)',
                           (session['id'], saved_reads, event['after_turn'],
                            json.dumps(event, ensure_ascii=False), now()))

    if reader:
        save_reads(reader.events)
    with db:
        db.execute("UPDATE sessions SET status='running',context=?,opening_actor=? WHERE id=?",
                   (context, first, session['id']))
    history, proposal, identifier = [], None, None
    for turn in range(1, 7):
        if cycle['mode'] == 'live' and cycle['run_date'] != today():
            raise ValueError('Changement de journée : cycle arrêté pour préserver le quota quotidien.')
        actor = first if turn % 2 else second
        stage = 'propose' if turn == 5 else 'vote' if turn == 6 else 'discuss'
        # Reserve before invoking a CLI. No automatic retry on quota, timeout or invalid JSON.
        with db:
            cursor = db.execute('INSERT INTO messages(session_id,turn,actor,created_at,input_context) VALUES(?,?,?,?,?)',
                                (session['id'], turn, actor, now(), context))
            message_id = cursor.lastrowid
        reply = provider.generate({'actor': actor, 'topic': topic, 'turn': turn,
            'stage': stage, 'context': context, 'history': list(history),
            'proposal': proposal, 'plan_id': identifier, 'roles': dict(roles),
            'reading_available': bool(reader and reader.available)})
        if not isinstance(reply.text, str) or not reply.text.strip() or len(reply.text) > 6000:
            raise ValueError('Réponse vide ou trop longue : échange arrêté.')
        if not isinstance(reply.payload, dict) or len(json.dumps(reply.payload,ensure_ascii=False)) > 38000:
            raise ValueError('Réponse structurée invalide : échange arrêté.')
        with db:
            db.execute('''UPDATE messages SET content=?,payload=?,status='complete',
                input_tokens=?,output_tokens=?,model=? WHERE id=?''',
                (reply.text, json.dumps(reply.payload, ensure_ascii=False), reply.input_tokens,
                 reply.output_tokens, reply.model, message_id))
        history.append({'actor': actor, 'turn': turn, 'content': reply.text})
        if stage == 'discuss':
            requested = reply.payload.get('read_requests', [])
            repository.validate_requests(requested)
            if reader:
                before = len(reader.events)
                reader.request_reads(requested, actor=actor, after_turn=turn)
                save_reads(reader.events[before:])
                context = reader.render() + task_context + memory
            elif requested:
                events = [{'path': item['path'], 'start_line': item['start_line'], 'actor': actor,
                           'after_turn': turn, 'status': 'unavailable',
                           'note': 'Aucune lecture de fichier disponible dans ce contexte.'} for item in requested]
                save_reads(events)
                history[-1]['read_results'] = events
            with db:
                db.execute('UPDATE sessions SET context=? WHERE id=?', (context, session['id']))
        elif stage == 'propose':
            proposal = validate_plan(reply.payload.get('plan'))
            kanban.validate_target(proposal, task_snapshot)
            identifier = plan_hash(proposal)
            with db:
                db.execute('UPDATE sessions SET decision=?,plan_id=? WHERE id=?',
                           (json.dumps(proposal, ensure_ascii=False), identifier, session['id']))
        elif stage == 'vote':
            agrees = reply.payload.get('vote') == 'accept' and reply.payload.get('plan_id') == identifier
            status = ('no_action' if proposal['action'] == 'none' else 'agreed') if agrees else 'rejected'
            with db:
                db.execute('UPDATE sessions SET status=? WHERE id=?', (status, session['id']))
                if status == 'agreed':
                    kanban.record_agreement(session['id'], topic, cycle['mode'], proposal, roles, task_snapshot)

def cycle_detail(cycle_id):
    db = get_db()
    row = db.execute('SELECT * FROM cycles WHERE id=?', (cycle_id,)).fetchone()
    if row is None:
        return None
    sessions = []
    for row in db.execute('SELECT * FROM sessions WHERE cycle_id=? ORDER BY id', (cycle_id,)):
        item = dict(row)
        item['roles'] = roles_for(item['opening_actor'])
        item['decision'] = json.loads(item['decision']) if item['decision'] else None
        item['messages'] = [dict(m) for m in db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY turn', (item['id'],))]
        item['source_reads'] = [json.loads(r['result']) for r in db.execute(
            'SELECT result FROM source_reads WHERE session_id=? ORDER BY number', (item['id'],))]
        item['kanban'] = kanban.decision_result(item['id'])
        sessions.append(item)
    return {'cycle': dict(db.execute('SELECT * FROM cycles WHERE id=?', (cycle_id,)).fetchone()), 'sessions': sessions}

def mark_interrupted(cycle_id):
    """Manual recovery after stopping the worker; never replays already charged turns."""
    db = get_db()
    with db:
        changed = db.execute("UPDATE cycles SET status='failed',finished_at=?,error=? WHERE id=? AND status='running'",
                            (now(), 'Interrompu manuellement ; aucun nouvel essai ce jour.', cycle_id)).rowcount
        if changed:
            db.execute("UPDATE sessions SET status='failed',error='Cycle interrompu.' WHERE cycle_id=? AND status IN ('running','pending')", (cycle_id,))
            db.execute("UPDATE messages SET status='failed' WHERE status='reserved' AND session_id IN (SELECT id FROM sessions WHERE cycle_id=?)", (cycle_id,))
    return bool(changed)
