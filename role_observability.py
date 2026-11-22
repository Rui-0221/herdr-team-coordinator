"""Role-scoped replay of native turns and bounded projections of their output."""

import json
import re

from common import DomainError


SECRET_VALUE = re.compile(r'(?i)\bbearer\s+[^\s"\']+|\b[0-9a-f]{32,}\b|\b(?:sk-|ghp_|AIza)[\w-]{16,}')
SECRET_FIELD = re.compile(r'(?i)(["\']?(?:token|api[_-]?key|password|authorization|ticket|credential)["\']?\s*[:=]\s*["\']?)[^\s",\'}]+')


def safe_text(value, limit=3500):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    value = SECRET_FIELD.sub(r'\1[已隐藏]', value)
    value = SECRET_VALUE.sub('[已隐藏]', value)
    return ''.join(c for c in value if c.isprintable() or c in '\n\t')[:limit]


def install(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS role_turns(
            seq INTEGER PRIMARY KEY, attempt TEXT NOT NULL UNIQUE, delivery_id TEXT NOT NULL REFERENCES outbox(id),
            binding_id TEXT NOT NULL REFERENCES bindings(id), prompt TEXT NOT NULL, created REAL NOT NULL,
            outcome TEXT, receipt TEXT
        );
        CREATE INDEX IF NOT EXISTS role_turn_history ON role_turns(binding_id,seq);
    ''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(role_turns)')}
    for column in ('outcome', 'receipt'):
        if column not in columns:
            db.execute(f'ALTER TABLE role_turns ADD COLUMN {column} TEXT')


def record(db, row, attempt, prompt, created):
    db.execute('INSERT INTO role_turns(attempt,delivery_id,binding_id,prompt,created) VALUES(?,?,?,?,?)',
               (attempt, row['id'], row['binding_id'], prompt, created))


def attempts(db, run_id, role, attempt=None):
    clauses = '''WITH history AS (
        SELECT o.id AS delivery_id,r.binding_id,r.attempt,r.prompt,o.kind,o.task_id,o.revision,o.task_version,r.created,
               CASE WHEN o.attempt=r.attempt THEN o.state ELSE COALESCE(r.outcome,'historic') END state,
               CASE WHEN o.attempt=r.attempt THEN o.evidence ELSE COALESCE(r.receipt,'') END evidence
        FROM role_turns r JOIN outbox o ON o.id=r.delivery_id JOIN bindings b ON b.id=r.binding_id
        WHERE b.run_id=? AND b.role_id=?
        UNION ALL
        SELECT o.id,o.binding_id,o.attempt,NULL,o.kind,o.task_id,o.revision,o.task_version,o.created,o.state,o.evidence
        FROM outbox o JOIN bindings b ON b.id=o.binding_id WHERE o.run_id=? AND b.role_id=?
        AND b.execution_mode!='legacy' AND o.attempt IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM role_turns r WHERE r.attempt=o.attempt)
    )'''
    keys = (run_id, role, run_id, role)
    if attempt is not None:
        return db.execute(clauses + ' SELECT * FROM history WHERE attempt=?', (*keys, attempt)).fetchone()
    return [dict(row) for row in db.execute(clauses + ''' SELECT delivery_id,binding_id,attempt,state,evidence,
        kind,task_id,revision,task_version,created FROM history ORDER BY created DESC,attempt LIMIT 32''', keys)]


def evidence_path(control, binding_id, delivery_id, attempt):
    if not all(re.fullmatch('[0-9a-f]{32}', value or '') for value in (binding_id, delivery_id, attempt)):
        raise DomainError('回合标识无效')
    return control / 'native-evidence' / binding_id / (delivery_id + '-' + attempt)


def event_view(line):
    try:
        row = json.loads(line)
    except ValueError:
        return {'kind': 'output', 'text': safe_text(line)}
    if not isinstance(row, dict):
        return None
    params = row.get('params')
    update = params.get('update') if row.get('method') == 'session/update' and isinstance(params, dict) else None
    if isinstance(update, dict):
        kind = update.get('sessionUpdate')
        if kind not in ('agent_message_chunk', 'tool_call', 'tool_call_update', 'plan'):
            return None
        content = update.get('content')
        if isinstance(content, dict) and content.get('type') == 'text':
            content = content.get('text', '')
        return {'kind': kind, 'name': safe_text(update.get('name') or update.get('title') or '', 160),
                'text': safe_text(content or update.get('args') or update.get('status') or '')}
    kind = row.get('type')
    if kind in ('thinking', 'agent_thought_chunk'):
        return None
    if kind in ('item.started', 'item.updated', 'item.completed'):
        item = row.get('item') or {}
        if not isinstance(item, dict) or item.get('type') == 'reasoning':
            return None
        return {'kind': kind, 'name': safe_text(item.get('type', ''), 100),
                'text': safe_text(item.get('text') or item.get('command') or item.get('aggregated_output') or item)}
    if kind in ('session', 'status', 'text', 'final', 'tool_call', 'tool_result', 'turn.completed', 'turn.failed', 'error'):
        return {'kind': kind, 'name': safe_text(row.get('tool') or '', 120),
                'text': safe_text(row.get('text') or row.get('result') or row.get('input') or row.get('reason') or row.get('phase') or row)}
    result = row.get('result')
    if isinstance(result, dict) and result.get('stopReason'):
        return {'kind': 'turn.completed', 'text': safe_text(result['stopReason'])}
    return None


def output_page(path, offset, *, stderr=False, complete=False, byte_limit=65536, line_limit=48):
    if offset is not None and (type(offset) is not int or offset < 0):
        raise DomainError('无效的输出游标')
    try:
        with path.open('rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - byte_limit) if offset is None else min(offset, size)
            handle.seek(start)
            if start:
                handle.seek(start - 1)
                if handle.read(1) != b'\n':
                    skipped = handle.readline(byte_limit)
                    if not skipped.endswith(b'\n'):
                        return {'events': [], 'next': handle.tell()}
            events = []
            stop = min(handle.tell() + byte_limit, size)
            while handle.tell() < stop:
                cursor = handle.tell()
                line = handle.readline(min(size - cursor, byte_limit))
                if not complete and not line.endswith(b'\n') and handle.tell() == size:
                    handle.seek(cursor)
                    break  # Allow an in-progress writer to finish the line.
                if len(line) >= byte_limit and not line.endswith(b'\n'):
                    events.append({'kind': 'output', 'text': '已跳过过大的原始事件', 'offset': cursor})
                    break
                value = line.decode('utf-8', errors='replace').rstrip('\r\n')
                event = {'kind': 'stderr', 'text': safe_text(value)} if stderr else event_view(value)
                if event:
                    events.append({'offset': cursor, **event})
                    if offset is not None and len(events) >= line_limit:
                        break
            return {'events': events[-line_limit:], 'next': handle.tell()}
    except FileNotFoundError:
        return {'events': [], 'next': 0}


def summary(control, turns):
    for turn in turns[:3]:
        path = evidence_path(control, turn['binding_id'], turn['delivery_id'], turn['attempt'])
        rows = output_page(path.with_suffix('.stdout.jsonl'), None, byte_limit=20000)['events']
        text = ''.join(row['text'] for row in rows if row['kind'] in ('agent_message_chunk', 'text'))
        if text:
            return text[-1400:]
    return ''
