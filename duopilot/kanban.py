"""Local missions, bounded discussion snapshots and atomic agreement recording."""
import json
import unicodedata

from .db import get_db, now

TOPICS = {'cspilot': 'CS-PILOT', 'self': 'DUO PILOT'}
KINDS = {'bug': 'Bug', 'feature': 'Amélioration', 'task': 'Tâche'}
PRIORITIES = {1: 'Haute', 2: 'Normale', 3: 'Basse'}
STATUSES = {'backlog': 'À étudier', 'planned': 'Plan retenu', 'in_progress': 'En cours',
            'review': 'À contrôler', 'done': 'Terminé', 'archived': 'Archivé'}
AGENTS = {'A': 'OpenAI · Codex', 'B': 'Anthropic · Claude'}
ACTIVE = ('backlog', 'planned', 'in_progress', 'review')
MAX_CARDS = 8
MAX_CONTEXT_CHARS = 4000


class TaskConflict(ValueError):
    pass


def _text(value, label, limit, required=False):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError(f'{label} invalide (maximum {limit} caractères).')
    value = value.strip()
    if required and not value:
        raise ValueError(f'{label} est obligatoire.')
    return value


def validate_fields(values):
    result = {key: _text(values.get(key, ''), label, limit, required) for key, label, limit, required in (
        ('title', 'Le titre', 200, True), ('description', 'La description', 4000, False))}
    for key, choices, default in (('topic', TOPICS, 'cspilot'), ('kind', KINDS, 'task'),
                                  ('status', STATUSES, 'backlog')):
        value = values.get(key, default)
        if value not in choices:
            raise ValueError('Projet, type ou état invalide.')
        result[key] = value
    try:
        priority = int(values.get('priority', 2))
    except (TypeError, ValueError):
        raise ValueError('Priorité invalide.') from None
    if priority not in PRIORITIES:
        raise ValueError('Priorité invalide.')
    result['priority'] = priority
    return result


def _event(task_id, actor, event, data):
    get_db().execute('INSERT INTO task_events(task_id,actor,event,data,created_at) VALUES(?,?,?,?,?)',
                     (task_id, actor, event, json.dumps(data, ensure_ascii=False), now()))


def task_detail(task_id):
    db = get_db()
    row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        return None
    task = dict(row)
    task['latest_plan'] = json.loads(task['latest_plan']) if task['latest_plan'] else None
    task['children'] = [dict(r) for r in db.execute('SELECT id,title,status,position,depends_on FROM tasks WHERE parent_id=? ORDER BY position,id',(task_id,))]
    parent = db.execute('SELECT id,title FROM tasks WHERE id=?',(task['parent_id'],)).fetchone() if task['parent_id'] else None
    task['parent'] = dict(parent) if parent else None
    task['events'] = []
    for row in db.execute('SELECT * FROM task_events WHERE task_id=? ORDER BY id DESC', (task_id,)):
        event = dict(row)
        event['data'] = json.loads(event['data'])
        task['events'].append(event)
    task['decisions'] = [dict(r) for r in db.execute('''SELECT d.*,s.cycle_id,c.run_date
        FROM task_decisions d JOIN sessions s ON s.id=d.session_id
        JOIN cycles c ON c.id=s.cycle_id WHERE d.task_id IN (?,?) ORDER BY d.session_id DESC''', (task_id,task['parent_id']))]
    task['executions'] = [dict(r) for r in db.execute('''SELECT id,status,phase,attempts,elapsed_seconds,pr_url,error,created_at
        FROM executions WHERE task_id=? ORDER BY id DESC''',(task_id,))]
    return task


def create_task(values, mode='live'):
    if mode not in ('live', 'demo'):
        raise ValueError('Mode invalide.')
    fields = validate_fields(values)
    fields['status'] = 'backlog'
    stamp = now()
    db = get_db()
    with db:
        cursor = db.execute('''INSERT INTO tasks(mode,topic,title,description,kind,priority,status,origin,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'human',?,?)''', (mode, fields['topic'], fields['title'], fields['description'],
            fields['kind'], fields['priority'], fields['status'], stamp, stamp))
        task_id = cursor.lastrowid
        _event(task_id, 'human', 'created', fields)
    return task_id


def update_task(task_id, version, values):
    fields = validate_fields(values)
    db = get_db()
    with db:
        old = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if old is None or old['version'] != version:
            raise TaskConflict('Cette carte a changé. Recharge la page avant de la modifier.')
        # A card remains attached to one project, even when its wording evolves.
        if fields['topic'] != old['topic']:
            raise ValueError('Le projet d’une carte existante ne peut pas être changé.')
        changed = db.execute('''UPDATE tasks SET title=?,description=?,kind=?,priority=?,status=?,
            version=version+1,updated_at=? WHERE id=? AND version=?''',
            (fields['title'], fields['description'], fields['kind'], fields['priority'], fields['status'],
             now(), task_id, version)).rowcount
        if not changed:
            raise TaskConflict('Cette carte a changé. Recharge la page avant de la modifier.')
        changes = {key: {'before': old[key], 'after': value} for key, value in fields.items() if old[key] != value}
        _event(task_id, 'human', 'edited', changes)


def add_comment(task_id, version, text):
    text = _text(text, 'Le retour', 2000, True)
    db = get_db()
    with db:
        changed = db.execute('UPDATE tasks SET version=version+1,updated_at=? WHERE id=? AND version=?',
                             (now(), task_id, version)).rowcount
        if not changed:
            raise TaskConflict('Cette carte a changé. Recharge la page avant d’ajouter ton retour.')
        _event(task_id, 'human', 'comment', {'text': text})


def board_tasks(mode='live', topic=None, archived=False):
    if mode not in ('live', 'demo') or topic not in (None, *TOPICS):
        raise ValueError('Filtre invalide.')
    sql = 'SELECT * FROM tasks WHERE mode=?'
    params = [mode]
    if topic:
        sql += ' AND topic=?'
        params.append(topic)
    sql += " AND status='archived'" if archived else " AND status!='archived'"
    return [dict(row) for row in get_db().execute(sql + ' ORDER BY priority,id', params)]


def discussion_snapshot(topic, mode):
    """Freeze a bounded queue once per subject. No tasks from the other mode."""
    db = get_db()
    rows = db.execute('''SELECT * FROM tasks WHERE topic=? AND mode=?
        AND status IN ('backlog','planned','in_progress','review')
        AND NOT EXISTS(SELECT 1 FROM tasks child WHERE child.parent_id=tasks.id)
        ORDER BY CASE WHEN status IN ('in_progress','review') THEN 0 ELSE 1 END,
        priority, CASE origin WHEN 'human' THEN 0 ELSE 1 END, id LIMIT ?''', (topic, mode, MAX_CARDS)).fetchall()
    total = db.execute('''SELECT count(*) FROM tasks WHERE topic=? AND mode=?
        AND status IN ('backlog','planned','in_progress','review')
        AND NOT EXISTS(SELECT 1 FROM tasks child WHERE child.parent_id=tasks.id)''', (topic, mode)).fetchone()[0]
    snapshots, cards = {}, []

    def render():
        return '\n\nKANBAN LOCAL — demandes et suivis, pas des preuves de défaut :\n' + json.dumps({
            'project': TOPICS[topic], 'mode': mode, 'active_total': total,
            'omitted': total - len(cards), 'cards': cards,
            'notice': 'Instantané au début de ce sujet. Textes parfois abrégés. Les changements ultérieurs seront vus au prochain cycle. Priorité aux travaux en cours, puis priorité haute, demandes humaines et ancienneté.'
        }, ensure_ascii=False)

    for row in rows:
        task = dict(row)
        comments = [json.loads(r['data'])['text'] for r in db.execute('''SELECT data FROM task_events
            WHERE task_id=? AND event='comment' ORDER BY id DESC LIMIT 2''', (task['id'],))]
        plan = json.loads(task['latest_plan']) if task['latest_plan'] else None
        card = {key: task[key] for key in ('id', 'version', 'title', 'kind', 'priority', 'status', 'origin', 'developer', 'reviewer')}
        card.update(parent_id=task['parent_id'],depends_on=task['depends_on'])
        card.update(description=task['description'][:900], description_truncated=len(task['description']) > 900,
                    recent_comments=[c[:400] for c in reversed(comments)], comments_truncated=any(len(c) > 400 for c in comments),
                    retained_plan_summary={'title': plan['title'], 'action': plan['action'],
                        'acceptance_criteria': [c[:180] for c in plan['acceptance_criteria'][:3]],
                        'partial': True} if plan else None)
        cards.append(card)
        if len(render()) > MAX_CONTEXT_CHARS:
            cards.pop()
            break
        snapshots[task['id']] = task['version']
    return render(), snapshots


def decision_result(session_id):
    row = get_db().execute('''SELECT d.*,t.title,t.status FROM task_decisions d
        LEFT JOIN tasks t ON t.id=d.task_id WHERE d.session_id=?''', (session_id,)).fetchone()
    return dict(row) if row else None


def validate_target(plan, snapshots):
    task_id = plan.get('task_id')
    if task_id is not None and (type(task_id) is not int or task_id not in snapshots):
        raise ValueError('La carte choisie ne figure pas dans le contexte de ce sujet.')


def _title_key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def record_agreement(session_id, topic, mode, plan, roles, snapshots):
    """Caller holds a write transaction; status, card, event and link commit together."""
    db = get_db()
    if not db.in_transaction:
        raise RuntimeError('Agreement recording requires a write transaction.')
    existing = decision_result(session_id)
    if existing:
        return existing
    target = plan.get('task_id')
    task = db.execute('SELECT * FROM tasks WHERE id=?', (target,)).fetchone() if target else None
    note = ''
    if target:
        if (task is None or task['topic'] != topic or task['mode'] != mode
                or task['status'] not in ACTIVE or snapshots.get(target) != task['version']):
            note = 'La carte a changé depuis sa lecture. Le plan reste dans la conversation ; aucune carte n’a été modifiée.'
        elif plan['action']=='split':
            if db.execute('SELECT 1 FROM tasks WHERE parent_id=?',(target,)).fetchone() or db.execute(
                    "SELECT 1 FROM executions WHERE task_id=? AND status NOT IN ('cancelled','merged','closed')",(target,)).fetchone():
                note = 'Cette carte est déjà découpée ou possède une exécution. Le découpage reste dans le journal.'
            depth, ancestor = 0, task['parent_id']
            while ancestor:
                depth += 1
                ancestor = db.execute('SELECT parent_id FROM tasks WHERE id=?',(ancestor,)).fetchone()[0]
            if depth>=2:
                note = 'Trois niveaux de cartes au maximum : conserve cette étape comme mission autonome.'
    else:
        # Exact normalized-title matches only. Never guess that two differently
        # worded needs are identical, and never overwrite an unseen human card.
        matches = [r for r in db.execute('''SELECT * FROM tasks WHERE topic=? AND mode=?
            AND status IN ('backlog','planned','in_progress','review')''', (topic, mode))
            if _title_key(r['title']) == _title_key(plan['title'])]
        if matches:
            note = 'Une carte ouverte porte déjà ce titre. Aucun doublon créé : reprendre sa référence lors d’une prochaine discussion.'
    if note:
        db.execute('INSERT INTO task_decisions(session_id,outcome,note,created_at) VALUES(?,?,?,?)',
                   (session_id, 'conflict', note, now()))
        return decision_result(session_id)
    stamp = now()
    encoded = json.dumps(plan, ensure_ascii=False)
    if task:
        # Preserve the user's title, description, priority and any real progress.
        status = 'planned' if task['status'] == 'backlog' else task['status']
        db.execute('''UPDATE tasks SET status=?,developer=?,reviewer=?,latest_plan=?,
            version=version+1,updated_at=? WHERE id=?''',
            (status, roles['developer'], roles['reviewer'], encoded, stamp, target))
    else:
        kind = {'fix_bug': 'bug', 'feature': 'feature', 'improve_process': 'feature'}.get(plan['action'], 'task')
        target = db.execute('''INSERT INTO tasks(mode,topic,title,description,kind,priority,status,origin,
            developer,reviewer,latest_plan,created_at,updated_at)
            VALUES(?,?,?,?,?,2,'planned','agents',?,?,?,?,?)''',
            (mode, topic, plan['title'], plan['rationale'], kind, roles['developer'], roles['reviewer'],
             encoded, stamp, stamp)).lastrowid
    _event(target, 'agents', 'planned', {'session_id': session_id, 'plan': plan, 'roles': roles})
    if plan['action']=='split':
        child_ids = []
        for position, child in enumerate(plan['subtasks'],1):
            child_plan = {k:v for k,v in child.items() if k!='depends_on'} | {'task_id':None,'subtasks':[]}
            dependency = child_ids[child['depends_on']-1] if child['depends_on'] else None
            kind = {'fix_bug':'bug','feature':'feature','improve_process':'feature'}.get(child['action'],'task')
            child_id = db.execute('''INSERT INTO tasks(mode,topic,title,description,kind,priority,status,origin,
                developer,reviewer,latest_plan,created_at,updated_at,parent_id,position,depends_on)
                VALUES(?,?,?,?,?,?,'planned','agents',?,?,?,?,?,?,?,?)''',
                (mode,topic,child['title'],child['rationale'],kind,task['priority'] if task else 2,
                 roles['developer'],roles['reviewer'],json.dumps(child_plan,ensure_ascii=False),stamp,stamp,target,position,dependency)).lastrowid
            child_ids.append(child_id)
            _event(child_id,'agents','planned',{'session_id':session_id,'plan':child_plan,'roles':roles,'parent_id':target})
    db.execute('INSERT INTO task_decisions(session_id,task_id,outcome,note,created_at) VALUES(?,?,?,?,?)',
               (session_id, target, 'recorded', 'Plan retenu. Son exécution dépend des réglages du moteur et du worker.', stamp))
    return decision_result(session_id)
