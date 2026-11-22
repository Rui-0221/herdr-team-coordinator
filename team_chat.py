"""Durable team messages shared by the panel and authenticated agent terminals."""

import json

from common import DomainError, digest, now, wire


def install(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS team_messages(
            seq INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL,
            revision INTEGER NOT NULL, sender TEXT NOT NULL, origin TEXT NOT NULL, recipient TEXT NOT NULL,
            text TEXT NOT NULL, task_id TEXT REFERENCES tasks(id), reply_to TEXT,
            input_hash TEXT NOT NULL, result TEXT, result_hash TEXT, created REAL NOT NULL,
            UNIQUE(run_id,id)
        );
        CREATE INDEX IF NOT EXISTS team_history ON team_messages(run_id,seq);
    """)


def page(db, run_id, *, before=None, limit=16, pending_for=None, revision=None, task_id=None, participant=None):
    query = 'SELECT seq,id,revision,sender,origin,recipient,text,task_id,reply_to,result,created FROM team_messages WHERE run_id=?'
    args = [run_id]
    if task_id is not None:
        query += ' AND task_id=?'
        args.append(task_id)
    if participant is not None:
        query += ' AND (recipient=? OR sender=?)'
        args.extend((participant, participant))
    if before is not None:
        if type(before) is not int:
            raise DomainError('无效的历史游标')
        query += ' AND seq<?'
        args.append(before)
    if pending_for is not None:
        query += ' AND recipient=? AND revision=? AND result IS NULL'
        args.extend([pending_for, revision])
    order = 'ASC' if pending_for is not None else 'DESC'
    rows = [dict(row) for row in db.execute(query + f' ORDER BY seq {order} LIMIT ?', (*args, limit))]
    for row in rows:
        row['result'] = json.loads(row['result']) if row['result'] else None
    return rows if pending_for is not None else rows[::-1]


def lookup(db, run_id, identifier):
    row = db.execute('SELECT * FROM team_messages WHERE run_id=? AND id=?', (run_id, identifier)).fetchone()
    if not row:
        raise DomainError('找不到本次运行中的对话消息')
    return row


def record(db, run, sender, origin, params, enqueue, *, outcome=None):
    identifier, text = params.get('message_id'), params.get('text')
    if not isinstance(identifier, str) or not identifier or len(identifier) > 100:
        raise DomainError('需要有效的消息编号')
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise DomainError('对话内容须为 1 到 4000 字')
    fingerprint = digest([sender, origin, params, outcome])
    previous = db.execute('SELECT input_hash FROM team_messages WHERE run_id=? AND id=?', (run['id'], identifier)).fetchone()
    if previous:
        if previous[0] != fingerprint:
            raise DomainError('消息编号已用于其他内容')
        return {'message_id': identifier}
    if run['state'] == 'accepted':
        raise DomainError('此次运行已经验收')
    role = params.get('to')
    if role:
        target = db.execute('SELECT * FROM bindings WHERE run_id=? AND role_id=? AND active=1', (run['id'], role)).fetchone()
    else:
        target = db.execute("SELECT * FROM bindings WHERE run_id=? AND kind='master' AND active=1", (run['id'],)).fetchone()
    if not target:
        raise DomainError('未找到对话目标角色')
    parent = lookup(db, run['id'], params['reply_to']) if params.get('reply_to') else None
    task_id = params.get('task_id') or (parent['task_id'] if parent else None)
    if task_id and not db.execute('SELECT 1 FROM tasks WHERE id=? AND run_id=?', (task_id, run['id'])).fetchone():
        raise DomainError('关联任务不在当前团队')
    if origin == 'panel' and params.get('to') and task_id and target['kind'] != 'master' and not db.execute(
            'SELECT 1 FROM tasks WHERE id=? AND role_id=?', (task_id, target['role_id'])).fetchone():
        raise DomainError('消息目标与任务角色不匹配')
    db.execute('''INSERT INTO team_messages(run_id,id,revision,sender,origin,recipient,text,task_id,reply_to,input_hash,result,created)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
               (run['id'], identifier, run['revision'], sender, origin, target['role_id'], text.strip(), task_id,
                parent['id'] if parent else None, fingerprint, wire(outcome) if outcome else None, now()))
    if target['role_id'] != sender or origin == 'panel':
        enqueue(db, run, target['id'], 'chat', f'团队对话 {identifier} 有更新，请查看 coord status。', chat_id=identifier)
    return {'message_id': identifier}


def pending_action(db, run, actor, params, action):
    row = lookup(db, run['id'], params['source'])
    if row['recipient'] != actor['role_id'] or row['revision'] != run['revision']:
        raise DomainError('此消息不属于当前角色或目标版本')
    fingerprint = digest([action, {k:v for k,v in params.items() if k != 'ticket'}])
    if row['result'] and row['result_hash'] != fingerprint:
        raise DomainError('这条消息已经处理，请查看对话记录')
    return row, fingerprint


def finish_action(db, source, result, fingerprint):
    task_id = result.get('task_id')
    db.execute('UPDATE team_messages SET result=?,result_hash=?,task_id=COALESCE(?,task_id) WHERE seq=?',
               (wire(result), fingerprint, task_id, source['seq']))
    # A forwarded request keeps its original reply target usable in subsequent turns.
    parent = source
    while task_id and parent['reply_to']:
        parent = lookup(db, source['run_id'], parent['reply_to'])
        if parent['task_id'] or parent['revision'] != source['revision']:
            break
        db.execute('UPDATE team_messages SET task_id=? WHERE seq=?', (task_id, parent['seq']))
