"""Two bounded deliberations; no code execution or GitHub write capability."""
import hashlib
import json
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo
from flask import current_app
from .db import get_db, now
from . import providers, github
from .roles import opening_actor_for, roles_for

TOPICS = ('cspilot', 'self')
ACTIONS = {'investigate', 'fix_bug', 'feature', 'improve_process', 'review', 'none'}

def today():
    return datetime.now(ZoneInfo('Europe/Paris')).date().isoformat()

def enqueue(mode='demo', run_date=None):
    if mode not in ('demo', 'live'):
        raise ValueError('Mode inconnu.')
    run_date = run_date or today()
    date.fromisoformat(run_date)
    db = get_db()
    try:
        with db:
            cursor = db.execute('INSERT INTO cycles(run_date,mode,created_at) VALUES(?,?,?)',
                                (run_date, mode, now()))
            cycle_id = cursor.lastrowid
            db.executemany('INSERT INTO sessions(cycle_id,topic,opening_actor) VALUES(?,?,?)',
                           [(cycle_id, topic, opening_actor_for(run_date, topic)) for topic in TOPICS])
        return cycle_id, True
    except sqlite3.IntegrityError:
        row = db.execute('SELECT id FROM cycles WHERE run_date=? AND mode=?',
                         (run_date, mode)).fetchone()
        if row is None:
            raise
        return row['id'], False

def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get('action') not in ACTIONS:
        raise ValueError('Plan final absent ou action invalide.')
    result = {'action': plan['action']}
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
        provider = provider or providers.build_provider(current_app.config, cycle['mode'])
        context_builder = context_builder or github.build_context
        for topic in TOPICS:
            session = dict(db.execute('SELECT * FROM sessions WHERE cycle_id=? AND topic=?',
                                      (cycle_id, topic)).fetchone())
            try:
                _run_session(cycle, session, provider, context_builder)
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

def _run_session(cycle, session, provider, context_builder):
    db = get_db()
    topic = session['topic']
    # Freeze before the first intervention, including older queued sessions.
    first = session['opening_actor'] or opening_actor_for(cycle['run_date'], topic)
    roles = roles_for(first)
    if roles is None:
        raise ValueError('Répartition des rôles invalide.')
    second = roles['reviewer']
    context = context_builder(current_app.config, topic, cycle['mode'])
    context = context[:current_app.config['MAX_CONTEXT_CHARS']]
    context += '\n\nMémoire courte (décisions proposées, pas des travaux réalisés) :\n' + recent_memory(topic, cycle['mode'])
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
            cursor = db.execute('INSERT INTO messages(session_id,turn,actor,created_at) VALUES(?,?,?,?)',
                                (session['id'], turn, actor, now()))
            message_id = cursor.lastrowid
        reply = provider.generate({'actor': actor, 'topic': topic, 'turn': turn,
            'stage': stage, 'context': context, 'history': list(history),
            'proposal': proposal, 'plan_id': identifier, 'roles': dict(roles)})
        if not isinstance(reply.text, str) or not reply.text.strip() or len(reply.text) > 6000:
            raise ValueError('Réponse vide ou trop longue : échange arrêté.')
        if not isinstance(reply.payload, dict) or len(json.dumps(reply.payload)) > 20000:
            raise ValueError('Réponse structurée invalide : échange arrêté.')
        with db:
            db.execute('''UPDATE messages SET content=?,payload=?,status='complete',
                input_tokens=?,output_tokens=?,model=? WHERE id=?''',
                (reply.text, json.dumps(reply.payload, ensure_ascii=False), reply.input_tokens,
                 reply.output_tokens, reply.model, message_id))
        history.append({'actor': actor, 'turn': turn, 'content': reply.text})
        if stage == 'propose':
            proposal = validate_plan(reply.payload.get('plan'))
            identifier = plan_hash(proposal)
            with db:
                db.execute('UPDATE sessions SET decision=?,plan_id=? WHERE id=?',
                           (json.dumps(proposal, ensure_ascii=False), identifier, session['id']))
        elif stage == 'vote':
            agrees = reply.payload.get('vote') == 'accept' and reply.payload.get('plan_id') == identifier
            status = ('no_action' if proposal['action'] == 'none' else 'agreed') if agrees else 'rejected'
            with db:
                db.execute('UPDATE sessions SET status=? WHERE id=?', (status, session['id']))

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
