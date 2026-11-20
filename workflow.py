"""Versioned handoffs, write arbitration, and review decisions for the scheduler."""

import json
from pathlib import Path
import time

from artifacts import ScopeError, identify_worktree, normalize_scope, overlaps, path_key, scope_covers, snapshot
from common import DomainError, new_id, now


HIGH_RISK = {'delete', 'permission', 'provider'}
LIVE_TASKS = ('offered', 'accepted', 'blocked', 'yielded', 'handed_off', 'needs_decision')
ROUTES = {
    'pause', 'resume', 'cancel-task', 'change-goal', 'resolve-revision', 'request-write', 'yield', 'handoff',
    'review-start', 'review-submit', 'ask', 'answer', 'local-accept', 'recover-write',
}


def install_workflow_schema(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS workflow_grants (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            task_id TEXT NOT NULL REFERENCES tasks(id), binding_id TEXT NOT NULL REFERENCES bindings(id),
            round INTEGER NOT NULL, role_id TEXT NOT NULL, revision INTEGER NOT NULL,
            resource TEXT NOT NULL, path TEXT NOT NULL, common_dir TEXT,
            created NOT NULL, released, evidence TEXT
        );
        CREATE INDEX IF NOT EXISTS workflow_active_grants ON workflow_grants(resource, path) WHERE released IS NULL;
        CREATE TABLE IF NOT EXISTS workflow_waits (
            task_id TEXT NOT NULL REFERENCES tasks(id), run_id TEXT NOT NULL REFERENCES runs(id),
            resource TEXT NOT NULL, path TEXT NOT NULL, common_dir TEXT,
            role_id TEXT NOT NULL, holder_id TEXT NOT NULL REFERENCES workflow_grants(id),
            state TEXT NOT NULL, reason TEXT NOT NULL, next_check REAL NOT NULL, condition TEXT NOT NULL,
            created NOT NULL, PRIMARY KEY (task_id, resource, path)
        );
        CREATE TABLE IF NOT EXISTS workflow_pauses (
            run_id TEXT PRIMARY KEY REFERENCES runs(id), revision INTEGER NOT NULL,
            reason TEXT NOT NULL, emergency INTEGER NOT NULL, checkpoint TEXT NOT NULL,
            state TEXT NOT NULL, created NOT NULL, recovery TEXT
        );
        CREATE TABLE IF NOT EXISTS workflow_handoffs (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            task_id TEXT NOT NULL REFERENCES tasks(id), round INTEGER NOT NULL,
            revision INTEGER NOT NULL, role_id TEXT NOT NULL, kind TEXT NOT NULL,
            report TEXT NOT NULL, paths TEXT NOT NULL, evidence TEXT, created NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_revision_decisions (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id), run_id TEXT NOT NULL REFERENCES runs(id),
            revision INTEGER NOT NULL, decision TEXT NOT NULL, carried TEXT NOT NULL,
            replacement_task TEXT REFERENCES tasks(id), created NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_reviews (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            revision INTEGER NOT NULL, binding_id TEXT NOT NULL REFERENCES bindings(id),
            task_id TEXT NOT NULL REFERENCES tasks(id), state TEXT NOT NULL, paths TEXT NOT NULL,
            digest TEXT NOT NULL, manifest TEXT NOT NULL, handoffs TEXT NOT NULL,
            report TEXT, invalid_reason TEXT, created NOT NULL, decided
        );
        CREATE UNIQUE INDEX IF NOT EXISTS workflow_active_review ON workflow_reviews(run_id) WHERE state IN ('pending', 'passed');
        CREATE TABLE IF NOT EXISTS workflow_questions (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), revision INTEGER NOT NULL,
            master_binding TEXT REFERENCES bindings(id), purpose TEXT NOT NULL, question TEXT NOT NULL,
            state TEXT NOT NULL, scope TEXT, review_id TEXT REFERENCES workflow_reviews(id),
            digest TEXT, channel TEXT, answer TEXT, reason TEXT, created NOT NULL, decided
        );
        CREATE UNIQUE INDEX IF NOT EXISTS workflow_pending_question ON workflow_questions(run_id) WHERE state='pending';
        CREATE TABLE IF NOT EXISTS workflow_acceptances (
            run_id TEXT PRIMARY KEY REFERENCES runs(id), revision INTEGER NOT NULL,
            review_id TEXT NOT NULL REFERENCES workflow_reviews(id), question_id TEXT NOT NULL REFERENCES workflow_questions(id),
            digest TEXT NOT NULL, channel TEXT NOT NULL, master_binding TEXT REFERENCES bindings(id), created NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_blocks (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            task_id TEXT NOT NULL REFERENCES tasks(id), role_id TEXT NOT NULL,
            reason TEXT NOT NULL, depends_on_role TEXT, depends_on_task TEXT,
            depends_on_resource TEXT, state TEXT NOT NULL DEFAULT 'open',
            created REAL NOT NULL, resolved REAL
        );
    ''')


def _row(db, sql, *values):
    return db.execute(sql, values).fetchone()


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _decoded(row, *fields):
    if row is None:
        return None
    result = dict(row)
    for key in fields:
        if result.get(key) is not None:
            result[key] = json.loads(result[key])
    return result


def _required(params, name, label, maximum=8000):
    value = params.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise DomainError(f'{label}必须是 1 到 {maximum} 字的明确文本。')
    return value.strip()


def _check_scope(root, paths):
    try:
        return normalize_scope(root, paths)
    except (ValueError, OSError, RuntimeError) as exc:
        raise DomainError(str(exc), '使用项目目录内的交付路径。') from exc


def _capture(root, paths):
    try:
        return snapshot(root, paths)
    except (ValueError, OSError, RuntimeError) as exc:
        raise DomainError(f'无法核对成果：{exc}', '核对项目目录和交付范围后重试。') from exc


def _report(params, project, label):
    report = _required(params, 'report', label)
    if 'report_path' not in params:
        return report, None
    relative = _check_scope(project, [_required(params, 'report_path', '报告文件', 1000)])[0]
    root = Path(project).resolve()
    try:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
        if path.suffix.lower() not in ('.md', '.txt', '.json') or not path.is_file() or path.stat().st_size > 512000:
            raise ValueError('报告须为不超过 512000 字节的现有文本文件。')
    except (OSError, RuntimeError, ValueError) as exc:
        raise DomainError('无法核对项目内的报告文件。', '使用项目中的现有 .md、.txt 或 .json 文件。') from exc
    return f'{report}\n报告文件：{relative}', relative


def _binding(db, run_id, role_id, kind=None):
    binding = _row(db, 'SELECT * FROM bindings WHERE run_id=? AND role_id=? AND active=1', run_id, role_id)
    if not binding or not binding['terminal'] or not binding['instance'] or (kind and binding['kind'] != kind):
        raise DomainError('没有匹配的活跃角色绑定。', '从状态中选择已经核验的角色会话。')
    return binding


def _master_id(db, run_id):
    binding = _row(db, "SELECT id FROM bindings WHERE run_id=? AND kind='master' AND active=1", run_id)
    return binding['id'] if binding else None


def invalidate_question(db, run_id, reason):
    db.execute("UPDATE workflow_questions SET state='invalid', reason=?, decided=? WHERE run_id=? AND state='pending'",
               (reason, now(), run_id))


def invalidate_review(db, run_id, reason):
    reviews = db.execute("SELECT id, task_id FROM workflow_reviews WHERE run_id=? AND state IN ('pending','passed')", (run_id,)).fetchall()
    for review in reviews:
        db.execute("UPDATE workflow_reviews SET state='invalid', invalid_reason=?, decided=? WHERE id=?",
                   (reason, now(), review['id']))
        db.execute("UPDATE tasks SET state='cancelled' WHERE id=? AND state IN ('offered','accepted','blocked','yielded')", (review['task_id'],))
        db.execute("UPDATE outbox SET state='obsolete', consent=0 WHERE task_id=? AND state='queued'", (review['task_id'],))
        db.execute("UPDATE outbox SET state='stale_delivery', consent=0, evidence=? WHERE task_id=? AND state='sending'",
                   (reason, review['task_id']))
        db.execute("UPDATE workflow_questions SET state='invalid', reason=?, decided=? WHERE review_id=? AND state='pending'",
                   (reason, now(), review['id']))


def guard_dispatch(db, run, mode):
    if run['state'] == 'accepted':
        raise DomainError('本次运行已经验收。', '建立新运行以安排后续工作。')
    if run['state'] == 'paused' and mode == 'write':
        raise DomainError('本次运行已暂停写入。', '先核查现场并恢复，或安排独立只读工作。')
    if mode == 'write' and _row(db, "SELECT id FROM tasks WHERE run_id=? AND state='needs_decision' LIMIT 1", run['id']):
        raise DomainError('新版目标仍有旧任务待处理。', '逐项决定沿用、调整或取消。')


def guard_accept(db, task):
    run = _row(db, 'SELECT * FROM runs WHERE id=?', task['run_id'])
    if task['revision'] != run['revision']:
        raise DomainError('这条指令来自旧目标版本。', '查询状态，等待主控确认新版任务。')
    guard_dispatch(db, run, task['mode'])


def workflow_status(db, run_id):
    grants = db.execute('SELECT * FROM workflow_grants WHERE run_id=? AND released IS NULL ORDER BY created', (run_id,))
    waits = db.execute('''SELECT w.*, g.run_id AS owner_run, g.role_id AS owner_role, g.task_id AS owner_task
                          FROM workflow_waits w JOIN workflow_grants g ON g.id=w.holder_id
                          WHERE w.run_id=? AND w.state IN ('waiting','ready') ORDER BY w.created''', (run_id,))
    decisions = db.execute('''SELECT d.task_id,d.revision,d.decision,d.carried,d.replacement_task,d.created
                              FROM workflow_revision_decisions d WHERE run_id=? ORDER BY d.created DESC LIMIT 50''', (run_id,))
    handoffs = db.execute('SELECT * FROM workflow_handoffs WHERE run_id=? ORDER BY created DESC LIMIT 30', (run_id,))
    review = _row(db, '''SELECT id,revision,binding_id,task_id,state,paths,digest,handoffs,report,invalid_reason,created,decided
                         FROM workflow_reviews WHERE run_id=? ORDER BY rowid DESC LIMIT 1''', run_id)
    question = _decoded(_row(db, "SELECT * FROM workflow_questions WHERE run_id=? AND state='pending'", run_id), 'scope')
    if question:
        settings = _row(db, 'SELECT acceptance FROM runs WHERE id=?', run_id)
        question.update(text=question['question'], local_required=question['purpose'] in HIGH_RISK or
                        (question['purpose'] == 'acceptance' and settings['acceptance'] == 'local'))
    pause = _row(db, 'SELECT * FROM workflow_pauses WHERE run_id=?', run_id)
    acceptance = _row(db, 'SELECT * FROM workflow_acceptances WHERE run_id=?', run_id)
    undecided = db.execute('''SELECT id,title,role_id,round,revision,mode FROM tasks
                             WHERE run_id=? AND state='needs_decision' ORDER BY created''', (run_id,))
    blocks = db.execute("SELECT * FROM workflow_blocks WHERE run_id=? AND state='open' ORDER BY created", (run_id,))
    return {
        'write_grants': [dict(row, state='active', holder=row['role_id']) for row in grants],
        'write_waits': [dict(row, owner=row['owner_role']) for row in waits],
        'blocks': [dict(row) for row in blocks],
        'pause': _decoded(pause, 'checkpoint', 'recovery'), 'revision_decisions': [dict(row) for row in decisions],
        'undecided': [dict(row) for row in undecided], 'handoffs': [_decoded(row, 'paths', 'evidence') for row in handoffs],
        'review': _decoded(review, 'paths', 'handoffs'), 'question': question,
        'acceptance_record': dict(acceptance) if acceptance else None,
    }


def record_block(db, task, reason, params):
    dep_role = params.get('depends_on_role')
    dep_task = params.get('depends_on_task')
    dep_resource = params.get('depends_on_resource')
    nested = params.get('depends_on')
    if isinstance(nested, dict):
        dep_role = nested.get('role', dep_role)
        dep_task = nested.get('task_id', dep_task)
        dep_resource = nested.get('resource', dep_resource)
    for val, name in ((dep_role, 'depends_on_role'), (dep_task, 'depends_on_task'), (dep_resource, 'depends_on_resource')):
        if val is not None and (not isinstance(val, str) or not val.strip() or len(val) > 100 or '\0' in val):
            raise DomainError(f'{name} 须为有效标识文本。')
    if dep_resource is not None and dep_resource not in ('worktree', 'refs'):
        raise DomainError('depends_on_resource 须为 worktree 或 refs。')
    block_id = new_id()
    db.execute('''INSERT INTO workflow_blocks(id,run_id,task_id,role_id,reason,depends_on_role,depends_on_task,depends_on_resource,state,created)
                  VALUES(?,?,?,?,?,?,?,?,'open',?)''',
               (block_id, task['run_id'], task['id'], task['role_id'], reason,
                dep_role.strip() if dep_role else None, dep_task.strip() if dep_task else None,
                dep_resource, now()))
    return {'block_id': block_id, 'depends_on_role': dep_role, 'depends_on_task': dep_task, 'depends_on_resource': dep_resource}


def _active_releases(db, task):
    return db.execute('''SELECT * FROM workflow_grants WHERE task_id=? AND binding_id=? AND round=? AND released IS NULL''',
                      (task['id'], task['binding_id'], task['round'])).fetchall()


def _unblock_resolved_tasks(db, run_id):
    db.execute('''UPDATE tasks SET state='accepted'
                  WHERE run_id=? AND state='blocked'
                  AND NOT EXISTS(SELECT 1 FROM workflow_blocks b WHERE b.task_id=tasks.id AND b.state='open')''', (run_id,))


def _release_grant(db, grant, evidence):
    db.execute('UPDATE workflow_grants SET released=?, evidence=? WHERE id=? AND released IS NULL', (now(), _dump(evidence), grant['id']))
    ready = db.execute('''SELECT w.*, t.binding_id, r.revision FROM workflow_waits w
                          JOIN tasks t ON t.id=w.task_id JOIN runs r ON r.id=w.run_id
                          JOIN bindings b ON b.id=t.binding_id
                          WHERE w.holder_id=? AND w.state='waiting' AND b.active=1 AND r.state='active'
                          AND t.revision=r.revision AND t.state IN ('accepted','blocked','yielded')
                          ORDER BY w.created''', (grant['id'],)).fetchall()
    db.execute("UPDATE workflow_waits SET state='ready', reason='原资格已核对并释放。', next_check=? WHERE holder_id=? AND state='waiting'",
               (time.time(), grant['id']))
    db.execute('''UPDATE workflow_blocks SET state='resolved', resolved=?
                  WHERE run_id=? AND state='open' AND (depends_on_resource=? OR depends_on_task=?)''',
               (now(), grant['run_id'], grant['resource'], grant['task_id']))
    _unblock_resolved_tasks(db, grant['run_id'])
    return ready


def _release(db, task, evidence):
    ready = []
    for grant in _active_releases(db, task):
        ready.extend(_release_grant(db, grant, evidence))
    db.execute("UPDATE workflow_waits SET state='cancelled' WHERE task_id=? AND state IN ('waiting','ready')", (task['id'],))
    return ready


def _wait_ready_notices(ready_waits):
    seen = set()
    notices = []
    for wait in ready_waits:
        if wait['binding_id'] in seen:
            continue
        seen.add(wait['binding_id'])
        notices.append({
            'binding_id': wait['binding_id'],
            'task_id': wait['task_id'],
            'text': f"写入资格（{wait['resource']}）已释放，请运行 coord status 并重新执行 coord request-write 申请写入。",
        })
    return notices


def _declared_stopped(params, *, recovery=False):
    evidence = params.get('evidence')
    if not isinstance(evidence, dict) or evidence.get('stopped') is not True:
        raise DomainError('缺少停写确认。', '确认写入操作及后台写入均已停止，再提交证据。')
    required = ('terminal', 'processes', 'files', 'handoff', 'external') if recovery else ('processes', 'files', 'external')
    return {'stopped': True, **{key: _required(evidence, key, f'停写证据 {key}', 2000) for key in required}}


def _task_actor(db, actor, run):
    task = _row(db, 'SELECT * FROM tasks WHERE id=? AND run_id=? AND binding_id=?',
                actor.get('task_id'), run['id'], actor.get('binding_id'))
    if task is None:
        raise DomainError('动作缺少本轮执行凭据。', '重新读取你实际收到的指令并查询本轮状态。')
    return task


def _recover_write(db, run, params):
    ids = params.get('grant_ids')
    if not isinstance(ids, list) or not ids or len(ids) > 64 or any(not isinstance(value, str) for value in ids) or len(set(ids)) != len(ids):
        raise DomainError('须明确列出此次要回收的旧资格编号。')
    evidence = _declared_stopped(params, recovery=True)
    grants = [_row(db, 'SELECT * FROM workflow_grants WHERE id=? AND run_id=? AND released IS NULL', grant_id, run['id']) for grant_id in ids]
    if any(grant is None for grant in grants):
        raise DomainError('原资格刚刚变化。', '重新核对实际仍未释放的写入资格。')
    ready_waits = []
    for grant in grants:
        ready_waits.extend(_release_grant(db, grant, {'source': 'local_recovery', **evidence}))
    for task_id in {grant['task_id'] for grant in grants}:
        task = _row(db, 'SELECT * FROM tasks WHERE id=?', task_id)
        if _active_releases(db, task):
            continue
        db.execute("UPDATE tasks SET state='yielded' WHERE id=? AND revision=? AND state IN ('accepted','blocked')", (task_id, run['revision']))
        db.execute("UPDATE workflow_waits SET state='cancelled' WHERE task_id=? AND state IN ('waiting','ready')", (task_id,))
        db.execute('''INSERT INTO workflow_handoffs(id,run_id,task_id,round,revision,role_id,kind,report,paths,evidence,created)
                      VALUES(?,?,?,?,?,?,'recovered',?,'[]',?,?)''',
                   (new_id(), run['id'], task_id, task['round'], task['revision'], task['role_id'], evidence['handoff'], _dump(evidence), now()))
    invalidate_review(db, run['id'], '本地核对后回收了旧会话的写入资格。')
    return {'state': 'recovered', 'grant_ids': ids, 'next': '重新核验角色会话，再恢复原任务或处置旧目标中的任务。',
            '_notices': _wait_ready_notices(ready_waits)}


def _work_request(db, run, actor, params):
    task = _task_actor(db, actor, run)
    guard_accept(db, task)
    binding = _row(db, 'SELECT kind,write_allowed FROM bindings WHERE id=? AND active=1', task['binding_id'])
    if task['state'] != 'accepted' or task['mode'] != 'write' or not binding or not binding['write_allowed'] or binding['kind'] == 'reviewer':
        raise DomainError('当前轮次没有可申请的写入权限。', '正式接单，核对任务及角色的写入范围。')
    raw = params.get('path', run['root'])
    if not isinstance(raw, str) or not raw or not isinstance(params.get('repository_refs', False), bool):
        raise DomainError('写入申请需要有效的工作目录与 Git 引用选项。')
    try:
        root = Path(run['root']).resolve(strict=True)
        directory = (root / raw).resolve(strict=True)
        directory.relative_to(root)
        tree = identify_worktree(directory)
    except (OSError, ValueError, RuntimeError) as exc:
        raise DomainError(f'无法核对项目内的写入目录：{exc}') from exc
    refs = params.get('repository_refs', False)
    if refs and not tree.common:
        raise DomainError('此目录不属于 Git 工作树。')
    resource = 'refs' if refs else 'worktree'
    target = path_key(tree.root)
    common = path_key(tree.common) if tree.common else None
    grants = db.execute('SELECT * FROM workflow_grants WHERE released IS NULL ORDER BY created').fetchall()
    for grant in grants:
        if grant['task_id'] == task['id'] and grant['resource'] == resource and grant['path'] == target:
            return {'grant_id': grant['id'], 'resource': resource, 'path': target, 'round': task['round']}
    for grant in grants:
        if grant['task_id'] == task['id']:
            continue
        if not _scope_conflict(target, common, resource, grant['path'], grant['common_dir'], grant['resource']):
            continue
        reason = f"{grant['role_id']} 仍持有重叠工作范围的写入资格。"
        condition = '原持有者须完成停写核对并提交 yield 或 handoff。收到释放通知或查询状态后重试。'
        db.execute('''INSERT INTO workflow_waits(task_id,run_id,resource,path,common_dir,role_id,holder_id,state,reason,next_check,condition,created)
                       VALUES(?,?,?,?,?,?,?,'waiting',?,?,?,?)
                       ON CONFLICT(task_id,resource,path) DO UPDATE SET holder_id=excluded.holder_id, state='waiting',
                          reason=excluded.reason, next_check=excluded.next_check, condition=excluded.condition''',
                   (task['id'], run['id'], resource, target, common, task['role_id'], grant['id'], reason, time.time()+15, condition, now()))
        return {'waiting': True, 'owner': grant['role_id'], 'owner_task': grant['task_id'], 'reason': reason, 'next': condition}
    existing_wait = _row(db, 'SELECT created FROM workflow_waits WHERE task_id=? AND resource=? AND path=?', task['id'], resource, target)
    my_created = existing_wait['created'] if existing_wait else now()
    ready_queue = db.execute('''SELECT w.*, b.role_id AS waiter_role FROM workflow_waits w
                                JOIN tasks t ON t.id=w.task_id JOIN runs r ON r.id=w.run_id
                                JOIN bindings b ON b.id=t.binding_id
                                WHERE w.state='ready' AND w.task_id!=? AND b.active=1 AND r.state='active'
                                AND t.revision=r.revision AND t.state IN ('accepted','blocked','yielded')
                                ORDER BY w.created''', (task['id'],)).fetchall()
    for ahead in ready_queue:
        if ahead['created'] < my_created and _scope_conflict(target, common, resource, ahead['path'], ahead['common_dir'], ahead['resource']):
            reason = f"{ahead['waiter_role']} 在等待队列更前方待接手写入资格。"
            condition = '先申请的成员优先取得资格；等待队列推进或重新查询状态后重试。'
            db.execute('''INSERT INTO workflow_waits(task_id,run_id,resource,path,common_dir,role_id,holder_id,state,reason,next_check,condition,created)
                           VALUES(?,?,?,?,?,?,?,'waiting',?,?,?,?)
                           ON CONFLICT(task_id,resource,path) DO UPDATE SET holder_id=excluded.holder_id, state='waiting',
                              reason=excluded.reason, next_check=excluded.next_check, condition=excluded.condition''',
                       (task['id'], run['id'], resource, target, common, task['role_id'], ahead['holder_id'], reason, time.time()+15, condition, my_created))
            return {'waiting': True, 'owner': ahead['waiter_role'], 'owner_task': ahead['task_id'], 'reason': reason, 'next': condition}
    _invalidate_overlapping(db, target, common, resource)
    grant_id = new_id()
    db.execute('''INSERT INTO workflow_grants(id,run_id,task_id,binding_id,round,role_id,revision,resource,path,common_dir,created)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
               (grant_id, run['id'], task['id'], task['binding_id'], task['round'], task['role_id'], task['revision'], resource, target, common, now()))
    db.execute("UPDATE workflow_waits SET state='granted' WHERE task_id=? AND resource=? AND path=?", (task['id'], resource, target))
    db.execute("UPDATE workflow_blocks SET state='resolved', resolved=? WHERE task_id=? AND state='open' AND depends_on_resource=?",
               (now(), task['id'], resource))
    return {'grant_id': grant_id, 'resource': resource, 'path': target, 'round': task['round']}


def _scope_conflict(left, left_common, left_resource, right, right_common, right_resource):
    if overlaps(left, right):
        return True
    if left_common and right_common:
        return left_resource == right_resource == 'refs' and left_common == right_common
    return False


def _invalidate_overlapping(db, directory, common, resource):
    runs = db.execute('''SELECT DISTINCT runs.id, runs.root FROM runs JOIN workflow_reviews ON workflow_reviews.run_id=runs.id
                         WHERE workflow_reviews.state IN ('pending','passed')''').fetchall()
    for run in runs:
        try:
            tree = identify_worktree(run['root'])
            conflict = _scope_conflict(directory, common, resource, path_key(tree.root),
                                       path_key(tree.common) if tree.common else None, 'refs')
        except (ScopeError, OSError):
            conflict = True
        if conflict:
            invalidate_review(db, run['id'], '成果范围重新取得写入资格。')


def _handover(db, run, actor, params, kind):
    task = _task_actor(db, actor, run)
    old_round = task['revision'] != run['revision'] or task['state'] in ('superseded', 'needs_decision', 'cancelled')
    grants = _active_releases(db, task)
    if old_round and (kind != 'yield' or not grants):
        raise DomainError('迟到回执只可交还原轮次仍持有的资格。', '核对版本，使用旧执行凭据提交 yield。')
    if not old_round and task['state'] not in ('accepted', 'blocked', 'yielded'):
        raise DomainError('任务还未接单或交接已经登记。', '查询本轮状态。')
    report, report_path = _report(params, run['root'], '交接报告')
    paths = _check_scope(run['root'], params['paths']) if 'paths' in params else []
    if kind == 'handoff' and task['mode'] == 'write' and not paths:
        raise DomainError('写入任务须声明交付路径。', '使用 paths 提供每个交付文件或 Git 范围。')
    if kind == 'handoff' and report_path and report_path not in paths:
        paths.append(report_path)
    if kind == 'handoff' and task['mode'] == 'write':
        history = db.execute('SELECT path FROM workflow_grants WHERE task_id=? AND binding_id=? AND round=?',
                             (task['id'], task['binding_id'], task['round'])).fetchall()
        if not history:
            raise DomainError('这轮写入任务从未获得写入资格。', '记录只读进度，或等待主控安排补齐工作。')
        root = Path(run['root']).resolve()
        for path in paths:
            supplied = Path(path_key((root / path).resolve()))
            if not any(supplied.is_relative_to(Path(grant['path'])) for grant in history):
                raise DomainError(f'交付路径超出本轮写入资格：{path}。', '核对实际工作树和交付范围。')
    evidence = _declared_stopped(params) if grants else None
    ready_waits = _release(db, task, evidence)
    receipt = new_id()
    db.execute('''INSERT INTO workflow_handoffs(id,run_id,task_id,round,revision,role_id,kind,report,paths,evidence,created)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
               (receipt, run['id'], task['id'], task['round'], task['revision'], task['role_id'], kind, report, _dump(paths),
                _dump(evidence) if evidence else None, now()))
    notices = _wait_ready_notices(ready_waits)
    if not old_round:
        state = 'yielded' if kind == 'yield' else 'handed_off'
        db.execute('UPDATE tasks SET state=? WHERE id=?', (state, task['id']))
    if kind == 'handoff':
        invalidate_review(db, run['id'], '收到新的任务交接。')
        resolved_blocks = db.execute('''SELECT b.task_id, t.binding_id FROM workflow_blocks b JOIN tasks t ON t.id=b.task_id
                                        WHERE b.run_id=? AND b.state='open'
                                        AND (b.depends_on_task=? OR b.depends_on_role=?)''',
                                     (run['id'], task['id'], task['role_id'])).fetchall()
        db.execute('''UPDATE workflow_blocks SET state='resolved', resolved=?
                      WHERE run_id=? AND state='open'
                      AND (depends_on_task=? OR depends_on_role=?)''',
                   (now(), run['id'], task['id'], task['role_id']))
        _unblock_resolved_tasks(db, run['id'])
        for blk in resolved_blocks:
            notices.append({'binding_id': blk['binding_id'], 'task_id': blk['task_id'],
                            'text': f"依赖任务 {task['id']}（{task['role_id']}）已交接，请运行 coord status 核对并继续。"})
    label = '交还写入资格' if kind == 'yield' else '提交成果交接'
    return {'handoff_id': receipt, 'task_id': task['id'], 'old_round': old_round,
            '_notify': f"{task['role_id']} 已{label}，任务 {task['id']}，目标版本 {task['revision']}。\n{report}",
            '_notices': notices}


def _pause(db, run, params):
    if run['state'] != 'active':
        raise DomainError('只有活动运行可以暂停。')
    reason = _required(params, 'reason', '暂停原因')
    emergency = params.get('emergency', False)
    if not isinstance(emergency, bool):
        raise DomainError('emergency 必须为布尔值。')
    active = db.execute('''SELECT id,binding_id,role_id,round,state,mode FROM tasks WHERE run_id=? AND revision=?
                           AND state IN ('offered','accepted','blocked','yielded') ORDER BY created''', (run['id'], run['revision'])).fetchall()
    holders = db.execute('SELECT id,task_id,role_id,path FROM workflow_grants WHERE run_id=? AND released IS NULL', (run['id'],)).fetchall()
    last_reports = db.execute('''SELECT id,task_id,kind FROM workflow_handoffs WHERE run_id=? ORDER BY created DESC LIMIT 30''',
                              (run['id'],)).fetchall()
    checkpoint = {'tasks': [dict(row) for row in active], 'holders': [dict(row) for row in holders],
                  'reports': [dict(row) for row in last_reports]}
    db.execute("UPDATE runs SET state='paused' WHERE id=?", (run['id'],))
    db.execute('''INSERT INTO workflow_pauses(run_id,revision,reason,emergency,checkpoint,state,created) VALUES(?,?,?,?,?,'paused',?)
                  ON CONFLICT(run_id) DO UPDATE SET revision=excluded.revision,reason=excluded.reason,emergency=excluded.emergency,
                  checkpoint=excluded.checkpoint,state='paused',created=excluded.created,recovery=NULL''',
               (run['id'], run['revision'], reason, int(emergency), _dump(checkpoint), now()))
    invalidate_review(db, run['id'], '运行暂停。')
    invalidate_question(db, run['id'], '运行暂停。')
    db.execute("UPDATE workflow_waits SET state='cancelled' WHERE run_id=? AND state IN ('waiting','ready')", (run['id'],))
    db.execute('''UPDATE outbox SET consent=0 WHERE run_id=? AND state='queued' AND task_id IN
                  (SELECT id FROM tasks WHERE run_id=? AND mode='write')''', (run['id'], run['id']))
    notices = [{'binding_id': row['binding_id'], 'text': f"运行已暂停。任务 {row['id']}：{reason}\n请停止写入，核对后台进程和外部操作，提交 yield 及进度。"}
               for row in active if row['mode'] == 'write' and row['state'] in ('accepted','blocked')]
    return {'state': 'paused', 'checkpoint': checkpoint, '_notices': notices}


def _resume(db, run, params, offer):
    task_id = params.get('task_id')
    if run['state'] not in ('active', 'paused') or run['state'] == 'active' and not task_id:
        raise DomainError('请恢复暂停的运行或指定一个未完成任务。')
    if _row(db, "SELECT id FROM tasks WHERE run_id=? AND state='needs_decision' LIMIT 1", run['id']):
        raise DomainError('目标变更仍有旧任务待处理。', '先决定旧任务的去向。')
    pause = _row(db, "SELECT * FROM workflow_pauses WHERE run_id=? AND state='paused'", run['id'])
    task = _row(db, 'SELECT * FROM tasks WHERE id=? AND run_id=? AND revision=?', task_id, run['id'], run['revision']) if task_id else None
    if task_id and (not task or task['state'] not in ('offered', 'blocked', 'yielded', 'accepted')):
        raise DomainError('指定任务不属于当前可恢复的执行项。', '查询当前版本的未完成任务。')
    if (run['state'] == 'paused' or (task and task['mode'] == 'write')) and _row(
            db, 'SELECT id FROM workflow_grants WHERE run_id=? AND released IS NULL LIMIT 1', run['id']):
        raise DomainError('仍有原写入资格，暂不能重新启动写入。', '由实际持有者提交本轮停写证据及 yield 或 handoff。')
    evidence = _declared_stopped(params, recovery=True) if pause and pause['emergency'] or task and task['mode'] == 'write' else None
    db.execute("UPDATE runs SET state='active' WHERE id=?", (run['id'],))
    if pause:
        db.execute("UPDATE workflow_pauses SET state='resumed', recovery=? WHERE run_id=?", (_dump(evidence) if evidence else None, run['id']))
    if task:
        binding = _binding(db, run['id'], task['role_id'], 'member')
        context = _row(db, "SELECT report FROM workflow_handoffs WHERE task_id=? ORDER BY rowid DESC LIMIT 1", task['id'])
        db.execute("UPDATE tasks SET state='superseded' WHERE id=?", (task['id'],))
        db.execute("UPDATE outbox SET state='obsolete', consent=0 WHERE task_id=? AND state='queued'", (task['id'],))
        current = _row(db, 'SELECT * FROM runs WHERE id=?', run['id'])
        instructions = f"{task['instructions']}\n\n继续原任务 {task['id']}。"
        if context:
            instructions += f"\n最近进度：{context['report']}"
        if evidence:
            instructions += f'\n恢复前现场核对：{_dump(evidence)}'
        replacement = offer(db, current, binding, task['title'], instructions, task['required'], task['completion'], task['mode'])
        return {'state': 'active', 'previous_task': task['id'], 'task_id': replacement}
    return {'state': 'active'}


def _change_goal(db, run, params):
    if run['state'] not in ('active', 'paused'):
        raise DomainError('已验收的运行不能直接改目标。')
    goal = _required(params, 'goal', '新目标')
    if goal == run['goal']:
        raise DomainError('新目标与当前目标相同。')
    invalidate_review(db, run['id'], '目标版本变更。')
    invalidate_question(db, run['id'], '目标版本变更。')
    # Historical completed review tasks belong to the invalidated review, not member deliverables.
    db.execute('''UPDATE tasks SET state='superseded'
                  WHERE run_id=? AND state='handed_off'
                  AND (binding_id IN (SELECT id FROM bindings WHERE run_id=? AND kind='reviewer')
                       OR id IN (SELECT task_id FROM workflow_reviews WHERE run_id=?))''',
               (run['id'], run['id'], run['id']))
    placeholders = ','.join('?' for _ in LIVE_TASKS)
    old_tasks = db.execute(f'''SELECT id,binding_id,role_id,round,title,state FROM tasks
                               WHERE run_id=? AND state IN ({placeholders})
                               AND binding_id NOT IN (SELECT id FROM bindings WHERE run_id=? AND kind='reviewer')
                               AND id NOT IN (SELECT task_id FROM workflow_reviews WHERE run_id=?)''',
                           (run['id'], *LIVE_TASKS, run['id'], run['id'])).fetchall()
    revoked = []
    decisions = []
    for task in old_tasks:
        if task['state'] == 'offered' and not _row(db, '''SELECT id FROM outbox WHERE task_id=?
                AND state IN ('sending','delivered','unknown','stale_delivery') LIMIT 1''', task['id']):
            db.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (task['id'],))
            revoked.append(task['id'])
        else:
            db.execute("UPDATE tasks SET state='needs_decision' WHERE id=?", (task['id'],))
            decisions.append(dict(task))
    db.execute("UPDATE outbox SET state='obsolete', consent=0 WHERE run_id=? AND state='queued'", (run['id'],))
    db.execute("UPDATE workflow_waits SET state='cancelled' WHERE run_id=? AND state IN ('waiting','ready')", (run['id'],))
    db.execute('UPDATE runs SET goal=?,revision=revision+1 WHERE id=?', (goal, run['id']))
    notices = [{'binding_id': task['binding_id'], 'text': f"目标已变更。旧任务 {task['id']} 暂停，保留现场。仍持有写入资格时请停写并提交旧轮次 yield。"}
               for task in old_tasks if task['state'] in ('accepted','blocked')]
    return {'revision': run['revision']+1, 'tasks': decisions, 'revoked': revoked, '_notices': notices}


def _cancel_task(db, run, params):
    if run['state'] != 'active':
        raise DomainError('仅可纠正活动运行中的当前任务。')
    reason = _required(params, 'reason', '撤回原因')
    task_id = _required(params, 'task_id', '要撤回的任务编号', 100)
    task = _row(db, 'SELECT * FROM tasks WHERE id=? AND run_id=? AND revision=?', task_id, run['id'], run['revision'])
    if not task or task['state'] not in ('offered', 'accepted', 'blocked', 'yielded'):
        raise DomainError('请选择本目标版本尚未完成的任务。')
    grants = _active_releases(db, task)
    invalidate_review(db, run['id'], '当前任务已撤回。')
    db.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (task_id,))
    db.execute("UPDATE outbox SET state='obsolete',consent=0 WHERE task_id=? AND state='queued'", (task_id,))
    db.execute('''UPDATE outbox SET state='stale_delivery',consent=0,evidence='任务撤回时正在发送，旧正文可能已到达'
                  WHERE task_id=? AND state='sending' ''', (task_id,))
    db.execute("UPDATE workflow_waits SET state='cancelled' WHERE task_id=? AND state IN ('waiting','ready')", (task_id,))
    db.execute("UPDATE workflow_blocks SET state='resolved', resolved=? WHERE task_id=? AND state='open'", (now(), task_id))
    notices = []
    if task['state'] != 'offered' or grants:
        body = f'任务 {task_id} 已撤回。原因：{reason}\n请停止本轮工作，保留现场；仍持有写入资格时提交本轮 yield。'
        notices.append({'binding_id': task['binding_id'], 'text': body})
    return {'task_id': task_id, 'state': 'cancelled', 'reason': reason, 'revision': run['revision'],
            'grants_retained': [row['id'] for row in grants], '_notices': notices}


def _resolve_revision(db, run, params, offer):
    prior = _row(db, "SELECT * FROM tasks WHERE id=? AND run_id=? AND state='needs_decision'", params.get('task_id'), run['id'])
    choice = params.get('decision')
    if not prior or choice not in ('reuse', 'adjust', 'cancel'):
        raise DomainError('请选择一个待处理旧任务及沿用、调整或取消。')
    carried = _required(params, 'carried', '旧成果处理说明')
    if choice != 'cancel' and _active_releases(db, prior):
        raise DomainError('旧轮次仍持有写入资格。', '保留进度并联系实际持有人交还。')
    completed_handoff = _row(db, "SELECT * FROM workflow_handoffs WHERE task_id=? AND kind='handoff' ORDER BY rowid DESC LIMIT 1", prior['id'])
    replacement = None
    if choice == 'reuse' and completed_handoff:
        # Preserve the completed deliverable directly in the new revision without forcing redundant rework.
        round_no = db.execute('SELECT COALESCE(MAX(round),0)+1 FROM tasks WHERE run_id=? AND role_id=?',
                              (run['id'], prior['role_id'])).fetchone()[0]
        carried_task = new_id()
        db.execute("UPDATE tasks SET state='superseded' WHERE id=?", (prior['id'],))
        db.execute('''INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (carried_task, run['id'], prior['role_id'], prior['binding_id'], round_no, run['revision'],
                    prior['title'], prior['instructions'], prior['required'], prior['completion'],
                    prior['mode'], 'handed_off', new_id(), now()))
        db.execute('''INSERT INTO workflow_handoffs(id,run_id,task_id,round,revision,role_id,kind,report,paths,evidence,created)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                   (new_id(), run['id'], carried_task, round_no, run['revision'], prior['role_id'], 'handoff',
                    f"{completed_handoff['report']}\n[沿用原任务 {prior['id']} 成果] {carried}",
                    completed_handoff['paths'], completed_handoff['evidence'], now()))
        replacement = carried_task
    elif choice != 'cancel':
        role_id = params.get('role_id', prior['role_id'])
        binding = _binding(db, run['id'], role_id, 'member')
        instructions = _required(params, 'instructions', '调整后的指令') if choice == 'adjust' else prior['instructions']
        instructions += f"\n\n原任务 {prior['id']} 达成的成果及当前接手范围：{carried}。请先核对沿用成果。"
        db.execute("UPDATE tasks SET state='superseded' WHERE id=?", (prior['id'],))
        replacement = offer(db, run, binding, prior['title'], instructions, prior['required'], prior['completion'], prior['mode'])
    else:
        db.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (prior['id'],))
    db.execute('''INSERT INTO workflow_revision_decisions(task_id,run_id,revision,decision,carried,replacement_task,created)
                  VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision, decision=excluded.decision,
                  carried=excluded.carried,replacement_task=excluded.replacement_task,created=excluded.created''',
               (prior['id'], run['id'], run['revision'], choice, carried, replacement, now()))
    return {'decision': choice, 'task_id': replacement, 'previous_task': prior['id'], 'revision': run['revision'],
            'reused_handoff': bool(choice == 'reuse' and completed_handoff)}


def _review_inputs(db, run):
    if run['state'] != 'active':
        raise DomainError('当前运行无法进入审查或验收。', '先恢复运行并处理待定任务。')
    if _row(db, "SELECT id FROM tasks WHERE run_id=? AND state='needs_decision' LIMIT 1", run['id']):
        raise DomainError('旧版本执行项尚未处置。', '逐项决定沿用、调整或取消。')
    try:
        tree = identify_worktree(run['root'])
    except (OSError, RuntimeError, ValueError) as exc:
        raise DomainError(f'无法核对成果工作树：{exc}') from exc
    grants = db.execute('SELECT run_id,path,common_dir,resource,role_id FROM workflow_grants WHERE released IS NULL').fetchall()
    waits = db.execute("SELECT run_id,path,common_dir,resource,role_id FROM workflow_waits WHERE state IN ('waiting','ready')").fetchall()
    common = path_key(tree.common) if tree.common else None
    for holder in (*grants, *waits):
        if holder['run_id'] == run['id'] or _scope_conflict(holder['path'], holder['common_dir'], holder['resource'],
                                                          path_key(tree.root), common, 'worktree'):
            raise DomainError(f"{holder['role_id']} 仍有写入资格或排队请求。", '完成交棒及停写核对后重新审查。')
    missing = db.execute('''SELECT id,title FROM tasks WHERE run_id=? AND revision=? AND required=1
                             AND state NOT IN ('handed_off','superseded','cancelled')''', (run['id'], run['revision'])).fetchall()
    if missing:
        raise DomainError(f"必做任务仍未完成交接：{', '.join(row['title'] for row in missing)}。", '核对完成条件并取得正式交接。')
    handoffs = db.execute('''SELECT h.*, t.binding_id, t.required FROM workflow_handoffs h JOIN tasks t ON t.id=h.task_id
                             WHERE h.run_id=? AND h.revision=? AND h.kind='handoff' AND t.state='handed_off' ORDER BY h.id''',
                          (run['id'], run['revision'])).fetchall()
    if not any(row['required'] for row in handoffs):
        raise DomainError('当前目标版本还没有已交接的必做任务成果。', '先派发并完成至少一项必做任务的正式交接。')
    return handoffs


def _independent(db, run_id, reviewer, handoffs):
    prior = db.execute('SELECT DISTINCT binding_id FROM workflow_grants WHERE run_id=?', (run_id,)).fetchall()
    bindings = {row['binding_id'] for row in (*prior, *handoffs)}
    for binding_id in bindings:
        author = _row(db, 'SELECT role_id,terminal,instance FROM bindings WHERE id=?', binding_id)
        if author and (author['role_id'] == reviewer['role_id'] or
                       author['terminal'] == reviewer['terminal'] and author['instance'] == reviewer['instance']):
            raise DomainError('该审查会话参与过此次成果的实现或交接。', '绑定独立、经核验的审查席位。')


def _review_start(db, run, params, offer):
    handoffs = _review_inputs(db, run)
    role = _required(params, 'role_id', '审查角色', 200)
    reviewer = _binding(db, run['id'], role, 'reviewer')
    _independent(db, run['id'], reviewer, handoffs)
    paths = _check_scope(run['root'], params.get('paths'))
    delivered = [(handoff['id'], path) for handoff in handoffs for path in json.loads(handoff['paths'])]
    if uncovered := [path for _, path in delivered if not scope_covers(run['root'], paths, path)]:
        raise DomainError(f"审查范围漏掉交接成果：{', '.join(uncovered)}。", '纳入这些交接路径后重试。')
    # Handoff files remain explicit even when the reviewer entered a parent directory.
    paths = _check_scope(run['root'], sorted(set(paths).union(path for _, path in delivered)))
    manifest = _capture(run['root'], paths)
    if manifest['unverified']:
        return {'state': 'unverified', 'unverified': manifest['unverified'], 'paths': paths,
                'next': '解决不可核对项目或明确划分审查范围后重新发起。'}
    invalidate_review(db, run['id'], '新审查批次替换了旧批次。')
    review_id = new_id()
    report_lines = [f"{handoff['role_id']} / {handoff['task_id']} / 第 {handoff['round']} 轮 / 交接 {handoff['id']}"
                    for handoff in handoffs]
    instructions = (f"以只读方式审查目标版本 {run['revision']} 的成果。\n审查编号 {review_id}\n"
                    f"工作目录：{run['root']}\n成果清单：{_dump(paths)}\n指纹：{manifest['digest']}\n"
                    f"目标：{run['goal']}\n交接清单：\n" + ('\n'.join(report_lines) or '无'))
    task_id = offer(db, run, reviewer, '独立审查', instructions, 0, '提交可复核的结论和证据。', 'read')
    db.execute('''INSERT INTO workflow_reviews(id,run_id,revision,binding_id,task_id,state,paths,digest,manifest,handoffs,created)
                  VALUES(?,?,?,?,?,'pending',?,?,?,?,?)''',
               (review_id, run['id'], run['revision'], reviewer['id'], task_id, _dump(paths), manifest['digest'],
                _dump(manifest), _dump([row['id'] for row in handoffs]), now()))
    return {'review_id': review_id, 'task_id': task_id, 'digest': manifest['digest'], 'paths': paths, 'state': 'pending'}


def _review_recheck(db, run, review):
    try:
        handoffs = _review_inputs(db, run)
        manifest = _capture(run['root'], json.loads(review['paths']))
    except DomainError as exc:
        return str(exc), []
    if sorted(json.loads(review['handoffs'])) != sorted(row['id'] for row in handoffs):
        return '必做任务或交接记录已变化。', []
    if manifest['unverified']:
        return '成果存在尚未核对项。', manifest['unverified']
    if manifest['digest'] != review['digest']:
        return '成果与已登记的审查指纹不一致。', []
    return None, []


def _review_submit(db, run, actor, params):
    task = _task_actor(db, actor, run)
    review = _row(db, "SELECT * FROM workflow_reviews WHERE id=? AND run_id=? AND state='pending'", params.get('review_id'), run['id'])
    if not review or review['task_id'] != task['id'] or review['binding_id'] != actor['binding_id'] or task['state'] != 'accepted':
        raise DomainError('审查编号与已接单的独立审查轮次不匹配。', '核对目标版本与审查编号。')
    verdict = params.get('verdict')
    if verdict not in ('pass', 'fail'):
        raise DomainError('审查结论须为 pass 或 fail。')
    report, _ = _report(params, run['root'], '审查证据')
    reason, unverified = _review_recheck(db, run, review)
    if reason:
        invalidate_review(db, run['id'], reason)
        return {'state': 'invalid', 'reason': reason, 'unverified': unverified, '_notify': reason}
    state = 'passed' if verdict == 'pass' else 'failed'
    db.execute('UPDATE workflow_reviews SET state=?, report=?, decided=? WHERE id=?', (state, report, now(), review['id']))
    db.execute("UPDATE tasks SET state='handed_off' WHERE id=?", (task['id'],))
    return {'review_id': review['id'], 'state': state, '_notify': f"独立审查 {review['id']} 已提交 {verdict}。\n{report}"}


def _approval_scope(params):
    scope = params.get('scope')
    if not isinstance(scope, dict) or set(scope) != {'target', 'extent', 'impact'}:
        raise DomainError('请提供本次操作的对象、范围和影响。')
    return {key: _required(scope, key, label, 2000) for key, label in [('target', '对象'), ('extent', '范围'), ('impact', '影响')]}


def _ask(db, run, params):
    purpose = params.get('purpose')
    if purpose not in ('integration', 'acceptance') and purpose not in HIGH_RISK:
        raise DomainError('请选择联调、最终验收或一种独立本地授权。')
    if purpose not in HIGH_RISK and run['state'] != 'active':
        raise DomainError('只能对活动运行发起联调或最终验收决定。')
    prompt = _required(params, 'text', '待决定的问题', 3000)
    pending = _row(db, "SELECT id FROM workflow_questions WHERE run_id=? AND state='pending'", run['id'])
    if pending and params.get('replace') != pending['id']:
        raise DomainError(f"运行已有待决问题 {pending['id']}。", '先完成决定，或明确指定要替换的问题编号。')
    manifest = None
    review_id = None
    if purpose == 'acceptance':
        review = _row(db, "SELECT * FROM workflow_reviews WHERE run_id=? AND revision=? AND state='passed'", run['id'], run['revision'])
        if not review:
            raise DomainError('本目标版本缺少通过的有效审查。', '先完成独立审查。')
        reason, unverified = _review_recheck(db, run, review)
        if reason:
            invalidate_review(db, run['id'], reason)
            return {'state': 'invalid', 'reason': reason, 'unverified': unverified}
        manifest = review['digest']
        review_id = review['id']
    scope = _approval_scope(params) if purpose in HIGH_RISK else None
    if pending:
        invalidate_question(db, run['id'], '用户问题被明确替换。')
    question_id = new_id()
    db.execute('''INSERT INTO workflow_questions(id,run_id,revision,master_binding,purpose,question,state,scope,review_id,digest,created)
                  VALUES(?,?,?,?,?,?,'pending',?,?,?,?)''',
               (question_id, run['id'], run['revision'], _master_id(db, run['id']), purpose, prompt,
                _dump(scope) if scope else None, review_id, manifest, now()))
    return {'question_id': question_id, 'revision': run['revision'], 'purpose': purpose,
            'state': 'pending', 'local_required': purpose in HIGH_RISK or purpose == 'acceptance' and run['acceptance'] == 'local'}


def _answer(db, run, params, local):
    question = _row(db, "SELECT * FROM workflow_questions WHERE id=? AND run_id=? AND state='pending'", params.get('question_id'), run['id'])
    if not question:
        raise DomainError('该问题已处理或已被替换。', '查询当前问题并重新说明待决定的动作。')
    if question['revision'] != run['revision'] or question['master_binding'] != _master_id(db, run['id']):
        invalidate_question(db, run['id'], '目标版本或主控会话变更。')
        return {'state': 'invalid', 'reason': '目标或主控变更，请重新向用户说明待决定的动作。'}
    if local and params.get('revision') != question['revision']:
        raise DomainError('本地界面显示的目标版本已经过时。', '刷新问题后再决定。')
    answer = params.get('response')
    if answer not in ('yes', 'no'):
        raise DomainError('答复必须明确指向该动作并登记为 yes 或 no。', '请主控重新说明问题并核实用户答复。')
    purpose = question['purpose']
    if purpose in HIGH_RISK:
        if not local:
            raise DomainError('这项操作需要本地独立批准。', '打开本地恢复页核对并确认对象、范围和影响。')
        if answer == 'yes' and _approval_scope(params) != json.loads(question['scope']):
            raise DomainError('批准范围与原问题不一致。', '刷新并确认当前显示的范围。')
    if purpose == 'acceptance' and answer == 'yes':
        if run['acceptance'] == 'local' and not local:
            raise DomainError('本团队的最终验收需要本地点击。', '在恢复页核对审查版本并作决定。')
        review = _row(db, "SELECT * FROM workflow_reviews WHERE id=? AND run_id=? AND revision=? AND state='passed'",
                      question['review_id'], run['id'], run['revision'])
        if not review or review['digest'] != question['digest']:
            invalidate_question(db, run['id'], '对应的审查已失效。')
            return {'state': 'invalid', 'reason': '对应的审查已失效，请重新审查并提问。'}
        reason, unverified = _review_recheck(db, run, review)
        if reason:
            invalidate_review(db, run['id'], reason)
            return {'state': 'invalid', 'reason': reason, 'unverified': unverified}
        db.execute("UPDATE runs SET state='accepted' WHERE id=?", (run['id'],))
        db.execute("UPDATE bindings SET active=0 WHERE run_id=? AND active=1", (run['id'],))
        db.execute("UPDATE outbox SET state='obsolete', consent=0, evidence='运行已验收结束' WHERE run_id=? AND state='queued'", (run['id'],))
        db.execute("UPDATE workflow_waits SET state='cancelled' WHERE run_id=? AND state IN ('waiting','ready')", (run['id'],))
        db.execute("UPDATE workflow_blocks SET state='resolved', resolved=? WHERE run_id=? AND state='open'", (now(), run['id']))
        db.execute("UPDATE workflow_reviews SET state='accepted' WHERE id=?", (review['id'],))
        db.execute('''INSERT INTO workflow_acceptances(run_id,revision,review_id,question_id,digest,channel,master_binding,created)
                      VALUES(?,?,?,?,?,?,?,?)''',
                   (run['id'], run['revision'], review['id'], question['id'], review['digest'], 'local' if local else 'master_relay',
                    question['master_binding'], now()))
    state = 'approved' if answer == 'yes' else 'declined'
    db.execute('UPDATE workflow_questions SET state=?,channel=?,answer=?,decided=? WHERE id=?',
               (state, 'local' if local else 'master_relay', answer, now(), question['id']))
    result = {'question_id': question['id'], 'state': state, 'purpose': purpose, 'revision': run['revision']}
    if purpose == 'acceptance' and answer == 'yes':
        result.update(run_state='accepted', digest=question['digest'], review_id=question['review_id'],
                      recorded_as='本地验收' if local else '主控转述用户接受了此版本')
    if purpose in HIGH_RISK:
        result['scope'] = json.loads(question['scope'])
    return result


def route_workflow(db, action, actor, params, offer):
    if action not in ROUTES:
        return None
    run = _row(db, 'SELECT * FROM runs WHERE id=?', actor.get('run_id'))
    if not run:
        raise DomainError('找不到这次运行。')
    kind = actor.get('kind')
    local = kind == 'local' and actor.get('channel') == 'local'
    if action == 'review-submit':
        if kind != 'reviewer':
            raise DomainError('仅已绑定的独立审查员可以提交审查。')
        return _review_submit(db, run, actor, params)
    if action in ('request-write', 'yield', 'handoff'):
        if kind != 'member':
            raise DomainError('仅绑定为执行成员的会话可以提交执行轮次动作。')
        if action == 'request-write':
            return _work_request(db, run, actor, params)
        return _handover(db, run, actor, params, action)
    if not local and kind != 'master':
        raise DomainError('此动作需要主控或本地管理入口。')
    if action == 'recover-write':
        if not local:
            raise DomainError('旧资格只能通过独立本地核对回收。')
        return _recover_write(db, run, params)
    if action == 'local-accept':
        if not local:
            raise DomainError('此决定只能从本地恢复页提交。')
        return _answer(db, run, params, local=True)
    if action == 'answer':
        if local:
            raise DomainError('请从本地批准入口确认当前问题。')
        return _answer(db, run, params, local=False)
    if action == 'pause':
        return _pause(db, run, params)
    if action == 'resume':
        return _resume(db, run, params, offer)
    if action == 'cancel-task':
        return _cancel_task(db, run, params)
    if action == 'change-goal':
        return _change_goal(db, run, params)
    if action == 'resolve-revision':
        return _resolve_revision(db, run, params, offer)
    if action == 'review-start':
        return _review_start(db, run, params, offer)
    return _ask(db, run, params)
