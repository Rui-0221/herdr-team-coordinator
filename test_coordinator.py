"""Exercise the durable protocol and delivery races with a controlled Core adapter."""

import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from common import new_id, now
from coordinator import Coordinator


class Core:
    def __init__(self, project):
        self.terminals = {address: {'address': address, 'instance': f'{address}/1/2', 'state': 'live',
                                    'agent': 'codex', 'cwd': str(project), 'status': 'idle', 'labels': {}}
                          for address in ('lead-term', 'developer-term', 'reviewer-term', 'new-lead', 'new-dev')}
        self.attempts = []
        self.launched = []
        self.during_send = lambda: None
        self.send_outcome = {'status': 'delivered', 'evidence': 'Core receipt'}
        self.screens = {}

    def terminal(self, address):
        return self.terminals.get(address)

    def snapshot(self):
        return list(self.terminals.values())

    def read(self, address, *, lines=0):
        return self.screens.get(address, {'complete': True, 'how': 'screen', 'text': '› Ask Codex to do anything'})

    def launch(self, agent, *, cwd, name, labels, bootstrap, environment=None, parent=None,
               no_notify=False, model='default', effort='default', workspace_write=False, writable_dirs=()):
        address = f'launched-{len(self.launched) + 1}'
        term = {'address': address, 'instance': f'{address}/1/2', 'state': 'live', 'agent': agent,
                'cwd': str(cwd), 'status': 'idle', 'labels': labels}
        self.terminals[address] = term
        self.launched.append({'address': address, 'agent': agent, 'parent': parent, 'no_notify': no_notify,
                              'workspace_write': workspace_write, 'writable_dirs': tuple(writable_dirs),
                              'environment': environment})
        return term

    def send(self, address, text, *, consent, expected, safe_states=('idle', 'done'), allow_unreported=False, **_extra):
        self.attempts.append((address, consent, expected, text))
        self.during_send()
        return self.send_outcome


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        fixture = Path(tempfile.mkdtemp(prefix='Herdr 协调器 '))
        self.addCleanup(shutil.rmtree, fixture, True)
        self.project = fixture / '中文 项目'
        self.project.mkdir()
        self.data = fixture / '运行 数据'
        self.core = Core(self.project)
        self.app = Coordinator(self.data, self.core, confirm=lambda *_: True, notifier=lambda *_: True)
        self.addCleanup(self.app.workers.shutdown)
        choices = {role: {'terminal': terminal, 'instance': self.core.terminal(terminal)['instance']}
                   for role, terminal in [('lead', 'lead-term'), ('developer', 'developer-term'), ('reviewer', 'reviewer-term')]}
        team = self.app.dashboard()['config']['teams'][0]
        roles = [member['id'] for member in team['members']]
        self.dev = next(member['id'] for member in team['members'] if member['kind'] == 'developer')
        self.reviewer = next(member['id'] for member in team['members'] if member['kind'] == 'reviewer')
        members = {role: choices['lead' if role == 'lead' else 'developer' if role == self.dev else 'reviewer'] for role in roles}
        self.run = self.app.start_run({'team_id': team['id'], 'goal': '原目标', 'project': str(self.project), 'members': members})['id']
        self.app.maintenance()
        with self.app.tx(False) as db:
            self.bindings = {row['role_id']: dict(row) for row in db.execute('SELECT * FROM bindings')}

    def message(self, action, params=None, role='lead', identifier=None):
        binding = self.bindings[role]
        return {'action': action, 'params': params or {}, 'request_id': identifier or new_id(),
                'auth': {'binding': binding['id'], 'terminal': binding['terminal'], 'token': binding['token']}}

    def member(self, action, params=None, role='lead'):
        return self.app.handle(self.message(action, params, role), 'member')

    def dispatch_params(self):
        return {'role': self.dev, 'title': '测试任务', 'instructions': '先核对版本', 'completion': '保存证据', 'mode': 'read'}

    def dispatch(self):
        answer = self.member('dispatch', self.dispatch_params())
        self.assertTrue(answer['ok'], answer)
        task_id = answer['result']['task_id']
        with self.app.tx(False) as db:
            delivery = db.execute('SELECT * FROM outbox WHERE task_id=?', (task_id,)).fetchone()
        return task_id, delivery['id']

    def task_ticket(self, task):
        candidates = self.member('status', role=self.dev)['result']['candidates']
        return next({'task': item['id'], 'ticket': item['ticket']} for item in candidates if item['id'] == task)

    def outbox(self, identifier):
        with self.app.tx(False) as db:
            return dict(db.execute('SELECT * FROM outbox WHERE id=?', (identifier,)).fetchone())

    def test_lost_dispatch_reply_is_recovered_from_same_request(self):
        params = self.dispatch_params()
        issued = self.member('issue', {'action': 'dispatch', 'params': params})
        identifier = issued['result']['request_id']
        # Unrecorded request reuses same ID before execution.
        reissued = self.member('issue', {'action': 'dispatch', 'params': params})
        self.assertEqual(reissued['result'], {'request_id': identifier, 'recorded': False})
        call = self.message('dispatch', params, identifier=identifier)
        first = self.app.handle(call, 'member')
        self.assertTrue(first['ok'], first)
        # Replaying the exact same request_id returns stored result without duplicate task.
        self.assertEqual(self.app.handle(call, 'member'), first)
        self.assertFalse(self.app.handle({**call, 'params': {**params, 'title': 'changed'}}, 'member')['ok'])
        # A brand new issue call after recording gets a fresh request_id (never replays completed intents).
        second_issue = self.member('issue', {'action': 'dispatch', 'params': params})
        self.assertNotEqual(second_issue['result']['request_id'], identifier)
        self.assertFalse(second_issue['result']['recorded'])
        with self.app.tx(False) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

    def test_changed_goal_during_send_keeps_stale_evidence(self):
        task, identifier = self.dispatch()
        old_ticket = self.task_ticket(task)
        self.app.maintenance(inspect=False)
        self.assertEqual(self.core.attempts, [])
        self.app.approve_delivery({'run_id': self.run, 'delivery_id': identifier})
        self.core.during_send = lambda: self.assertTrue(self.member('change-goal', {'goal': '新版目标'})['ok'])
        self.app.deliver(identifier)
        self.assertEqual(len(self.core.attempts), 1)
        self.assertEqual(self.outbox(identifier)['state'], 'stale_delivery')
        self.assertFalse(self.member('accept', old_ticket, self.dev)['ok'])
        with self.app.tx(False) as db:
            self.assertEqual(self.app.get_run(db, self.run)['revision'], 2)

    def test_unknown_send_lease_never_requeues(self):
        _, identifier = self.dispatch()
        self.app.approve_delivery({'run_id': self.run, 'delivery_id': identifier})
        self.core.send_outcome = {'status': 'unknown', 'evidence': 'Connection lost'}
        self.app.deliver(identifier)
        with self.app.tx() as db:
            db.execute('UPDATE outbox SET lease_until=? WHERE id=?', (now() - 1, identifier))
        self.app.maintenance(inspect=False)
        self.assertEqual(len(self.core.attempts), 1)
        self.assertEqual(self.outbox(identifier)['state'], 'unknown')

    def test_repeated_accept_uses_member_receipt(self):
        task, identifier = self.dispatch()
        call = self.message('accept', self.task_ticket(task), self.dev)
        first = self.app.handle(call, 'member')
        self.assertTrue(first['ok'], first)
        self.assertEqual(self.app.handle(call, 'member'), first)
        self.assertEqual(self.outbox(identifier)['state'], 'delivered')

    def test_changed_terminal_denies_old_token_and_local_actions(self):
        self.assertFalse(self.member('decide', role=self.dev)['ok'])
        self.core.terminals['developer-term']['instance'] = 'developer-term/2/3'
        self.assertFalse(self.member('status', role=self.dev)['ok'])
        self.core.terminals['lead-term']['state'] = 'finished'
        self.app.maintenance()
        self.assertFalse(self.member('dispatch', self.dispatch_params())['ok'])
        self.app.rebind({'run_id': self.run, 'terminal': 'new-lead', 'instance': 'new-lead/1/2'})
        self.assertFalse(self.member('status')['ok'])
        with self.app.tx(False) as db:
            self.assertEqual(self.app.get_run(db, self.run)['master_state'], 'available')

    def test_stalled_terminal_does_not_delay_other_delivery(self):
        _, first = self.dispatch()
        with self.app.tx() as db:
            second = self.app.enqueue(db, self.app.get_run(db, self.run), self.bindings[self.reviewer]['id'], 'report', 'Other member')
        first_in_core, allow_finish = threading.Event(), threading.Event()

        def stall_first(address, text, **options):
            if address == 'developer-term':
                first_in_core.set()
                if not allow_finish.wait(5):
                    return {'status': 'unknown'}
            return {'status': 'delivered'}

        self.core.send = stall_first
        for item in (first, second):
            self.app.approve_delivery({'run_id': self.run, 'delivery_id': item})
        try:
            self.app.maintenance(inspect=False)
            self.assertTrue(first_in_core.wait(2))
            self.app.jobs[self.bindings[self.reviewer]['id']].result(2)
            self.assertEqual(self.outbox(second)['state'], 'delivered')
            self.assertEqual(self.outbox(first)['state'], 'sending')
        finally:
            allow_finish.set()

    def test_approve_delivery_ignores_notice_at_update(self):
        _, identifier = self.dispatch()
        with self.app.tx() as db:
            db.execute('UPDATE outbox SET notice_at=NULL WHERE id=?', (identifier,))
        orig_confirm = self.app.confirm

        def mutate_notice_during_confirm(title, body):
            with self.app.tx() as db:
                db.execute('UPDATE outbox SET notice_at=? WHERE id=?', (now(), identifier))
            return orig_confirm(title, body)

        self.app.confirm = mutate_notice_during_confirm
        res = self.app.approve_delivery({'run_id': self.run, 'delivery_id': identifier})
        self.assertEqual(res['delivery_id'], identifier)

    def test_plugin_created_terminals_auto_deliver_with_no_notify_and_screen_guard(self):
        cfg = self.app.dashboard()['config']
        cfg['teams'][0]['members'][1]['write_allowed'] = True
        self.app.local('save_config', {'config': cfg})
        run2 = self.app.start_run({'team_id': cfg['teams'][0]['id'], 'goal': '全自动测试', 'project': str(self.project)})['id']
        # Launch master first, then children.
        self.app.maintenance()
        for fut in list(self.app.jobs.values()):
            fut.result(2)
        self.app.maintenance()
        for fut in list(self.app.jobs.values()):
            fut.result(2)
        with self.app.tx(False) as db:
            b2 = {row['role_id']: dict(row) for row in db.execute('SELECT * FROM bindings WHERE run_id=?', (run2,))}
        self.assertEqual(len(self.core.launched), 3)
        lead_launch = next(item for item in self.core.launched if item['environment']['HERDR_COORD_BINDING'] == b2['lead']['id'])
        dev_launch = next(item for item in self.core.launched if item['environment']['HERDR_COORD_BINDING'] == b2[self.dev]['id'])
        self.assertFalse(lead_launch['no_notify'])
        self.assertTrue(dev_launch['no_notify'])
        self.assertTrue(dev_launch['workspace_write'])
        self.assertTrue(dev_launch['writable_dirs'])
        lead_msg = {'action': 'dispatch', 'params': self.dispatch_params(), 'request_id': new_id(),
                    'auth': {'binding': b2['lead']['id'], 'terminal': b2['lead']['terminal'], 'token': b2['lead']['token']}}
        res = self.app.handle(lead_msg, 'member')
        self.assertTrue(res['ok'], res)
        # Auto-delivery should pause when screen shows picker or folder-trust menu, then deliver once screen clears.
        self.core.screens[b2[self.dev]['terminal']] = {'complete': True, 'how': 'screen',
                                                        'text': 'Trust this folder?\nenter continue · esc back'}
        self.app.maintenance(inspect=False)
        for fut in list(self.app.jobs.values()):
            fut.result(2)
        with self.app.tx(False) as db:
            out = db.execute('SELECT * FROM outbox WHERE task_id=?', (res['result']['task_id'],)).fetchone()
            self.assertEqual(out['state'], 'queued')
            db.execute('UPDATE outbox SET not_before=0 WHERE id=?', (out['id'],))
        self.core.screens[b2[self.dev]['terminal']] = {'complete': True, 'how': 'screen', 'text': '› Ask Codex'}
        self.app.maintenance(inspect=False)
        for fut in list(self.app.jobs.values()):
            fut.result(2)
        with self.app.tx(False) as db:
            out = db.execute('SELECT * FROM outbox WHERE task_id=?', (res['result']['task_id'],)).fetchone()
            self.assertEqual(out['state'], 'delivered')
        # Antigravity empty-box mode placeholder must NOT block auto-delivery, while a real draft must block.
        from coordinator import screen_blocker_reason
        self.assertIsNone(screen_blocker_reason(
            {'complete': True, 'text': '──────────────\n> Accept-edits mode: file edits auto-approved\n──────────────'},
            'antigravity',
        ))
        self.assertIsNotNone(screen_blocker_reason(
            {'complete': True, 'text': '──────────────\n> please finish this later\n──────────────'},
            'antigravity',
        ))
        self.assertIsNone(screen_blocker_reason(
            {'complete': True, 'text': '› prior submitted prompt\n• Done.\n› Ask Codex to do anything\n  GPT-6.2-Sol medium'},
            'codex',
        ))
        self.assertIsNotNone(screen_blocker_reason(
            {'complete': True, 'text': '• Done.\n› [Herdr团队协调器 492853d8 v1] unsubmitted pasted text\n  GPT-6.2-Sol medium'},
            'codex',
        ))
        # Long outbox report sends a compact ConPTY wake nudge while full text remains in inbox.
        with self.app.tx() as db:
            long_id = self.app.enqueue(db, self.app.get_run(db, run2), b2['lead']['id'], 'report', 'X' * 350)
        self.app.deliver(long_id)
        self.assertIn('coord status', self.core.attempts[-1][3])
        self.assertLessEqual(len(self.core.attempts[-1][3].encode('utf-8')), 95)

    def test_dsh_awaiting_bind_and_member_health_monitoring_and_reuse_after_accept(self):
        cfg = self.app.dashboard()['config']
        cfg['teams'][0]['members'][1]['agent'] = 'dsh'
        self.app.local('save_config', {'config': cfg})
        # Close run1 via acceptance so terminals are freed
        (self.project / 'deliver.txt').write_text('ok', encoding='utf-8')
        t_id, _ = self.dispatch()
        self.assertTrue(self.member('accept', self.task_ticket(t_id), self.dev)['ok'])
        self.assertTrue(self.member('handoff', {**self.task_ticket(t_id), 'report': 'done', 'paths': ['deliver.txt']}, self.dev)['ok'])
        rev = self.member('review-start', {'role_id': self.reviewer, 'paths': ['deliver.txt']})['result']
        rev_cand = next({'task': i['id'], 'ticket': i['ticket']} for i in self.member('status', role=self.reviewer)['result']['candidates'] if i['id'] == rev['task_id'])
        self.assertTrue(self.member('accept', rev_cand, self.reviewer)['ok'])
        self.assertTrue(self.member('review-submit', {**rev_cand, 'review_id': rev['review_id'], 'verdict': 'pass', 'report': 'good'}, self.reviewer)['ok'])
        q = self.member('ask', {'purpose': 'acceptance', 'text': 'accept?'})['result']
        self.assertTrue(self.member('answer', {'question_id': q['question_id'], 'response': 'yes'})['ok'])

        # Same terminals can now start Round 2 with DSH awaiting_bind!
        run2 = self.app.start_run({
            'team_id': cfg['teams'][0]['id'], 'goal': '第二轮运行', 'project': str(self.project),
            'members': {'lead': {'terminal': 'lead-term', 'instance': 'lead-term/1/2'},
                        self.reviewer: {'terminal': 'reviewer-term', 'instance': 'reviewer-term/1/2'}}
        })['id']
        self.app.maintenance()
        with self.app.tx(False) as db:
            dsh_b = db.execute('SELECT * FROM bindings WHERE run_id=? AND role_id=?', (run2, self.dev)).fetchone()
            self.assertEqual(dsh_b['launch_state'], 'awaiting_bind')

        # Rebind crashed non-master member without write grant
        self.core.terminals['dsh-term'] = {'address': 'dsh-term', 'instance': 'dsh-term/1/2', 'state': 'live',
                                           'agent': 'dsh', 'cwd': str(self.project), 'status': None, 'labels': {}}
        self.app.rebind({'run_id': run2, 'binding_id': dsh_b['id'], 'terminal': 'dsh-term', 'instance': 'dsh-term/1/2'}, master_only=False)
        self.app.maintenance()
        with self.app.tx(False) as db:
            b2 = {row['role_id']: dict(row) for row in db.execute('SELECT * FROM bindings WHERE run_id=? AND active=1', (run2,))}
        d2 = self.app.handle({'action': 'dispatch', 'params': self.dispatch_params(), 'request_id': new_id(),
                              'auth': {'binding': b2['lead']['id'], 'terminal': 'lead-term', 'token': b2['lead']['token']}}, 'member')
        self.assertTrue(d2['ok'], d2)
        self.core.terminals['dsh-term']['state'] = 'finished'
        self.app.maintenance()
        with self.app.tx(False) as db:
            self.assertEqual(db.execute('SELECT health FROM bindings WHERE id=?', (b2[self.dev]['id'],)).fetchone()[0], 'lost')
        self.core.terminals['dsh-term-2'] = {'address': 'dsh-term-2', 'instance': 'dsh-term-2/1/2', 'state': 'live',
                                             'agent': 'dsh', 'cwd': str(self.project), 'status': None, 'labels': {}}
        rebound = self.app.rebind({'run_id': run2, 'binding_id': b2[self.dev]['id'], 'terminal': 'dsh-term-2', 'instance': 'dsh-term-2/1/2'}, master_only=False)
        self.assertEqual(rebound['state'], 'bound')

    def test_real_pipe_coord_cli_repeated_actions_blocked_deps_and_selected_cleanup(self):
        import os, subprocess, sys
        from platform_runtime import PipeServer
        cfg = self.app.dashboard()['config']
        # Add a second developer role so we can test write-lock queue via real coord.py CLI
        cfg['roles'].append({'id': 'dev2', 'kind': 'developer', 'name': '开发2', 'duties': '辅助'})
        cfg['teams'][0]['members'][1]['write_allowed'] = True
        cfg['teams'][0]['members'].append({'id': 'dev2', 'role': 'dev2', 'agent': 'codex', 'model': 'default', 'write_allowed': True})
        self.app.local('save_config', {'config': cfg})
        for addr in ('lead-p', 'dev1-p', 'dev2-p', 'rev-p'):
            self.core.terminals[addr] = {'address': addr, 'instance': f'{addr}/1/2', 'state': 'live',
                                         'agent': 'codex', 'cwd': str(self.project), 'status': 'idle', 'labels': {}}
        run_id = self.app.start_run({
            'team_id': cfg['teams'][0]['id'], 'goal': '管道端到端测试', 'project': str(self.project),
            'members': {
                'lead': {'terminal': 'lead-p', 'instance': 'lead-p/1/2'},
                self.dev: {'terminal': 'dev1-p', 'instance': 'dev1-p/1/2'},
                'dev2': {'terminal': 'dev2-p', 'instance': 'dev2-p/1/2'},
                self.reviewer: {'terminal': 'rev-p', 'instance': 'rev-p/1/2'},
            }
        })['id']
        with self.app.tx(False) as db:
            b = {row['role_id']: dict(row) for row in db.execute('SELECT * FROM bindings WHERE run_id=? AND active=1', (run_id,))}
        srv = PipeServer(self.data, lambda item: self.app.handle(item, 'member'), channel='member').start()
        self.addCleanup(srv.close)
        coord_py = str(Path(__file__).resolve().parent / 'coord.py')

        def cli(role, *argv, writable_journal=True):
            env = {**os.environ, 'HC_TERMINAL_ID': b[role]['terminal'],
                   'HERDR_COORD_ROOT': str(self.data), 'HERDR_COORD_BINDING': b[role]['id'],
                   'HERDR_COORD_TOKEN': b[role]['token']}
            if writable_journal:
                jdir = self.data / 'requests' / b[role]['id']
                jdir.mkdir(parents=True, exist_ok=True)
                env['HERDR_COORD_JOURNAL'] = str(jdir)
            else:
                env.pop('HERDR_COORD_JOURNAL', None)
            proc = subprocess.run([sys.executable, '-B', '-X', 'utf8', coord_py, *argv],
                                  capture_output=True, text=True, encoding='utf-8', env=env, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return __import__('json').loads(proc.stdout)

        # Lead dispatches dev1 and dev2 (read-only journal -> uses server issue)
        d1 = cli('lead', 'dispatch', '--role', self.dev, '--title', 'A', '--instructions', 'do A', '--completion', 'done A', '--mode', 'write', writable_journal=False)['result']['task_id']
        d2 = cli('lead', 'dispatch', '--role', 'dev2', '--title', 'B', '--instructions', 'do B', '--completion', 'done B', '--mode', 'write', writable_journal=False)['result']['task_id']
        t1 = next(c['ticket'] for c in cli(self.dev, 'status')['result']['candidates'] if c['id'] == d1)
        t2 = next(c['ticket'] for c in cli('dev2', 'status')['result']['candidates'] if c['id'] == d2)
        cli(self.dev, 'accept', '--task', d1, '--ticket', t1)
        cli('dev2', 'accept', '--task', d2, '--ticket', t2)
        # dev1 gets write lock; dev2 requests write -> waiting: true; dev2 also logs structured blocked
        self.assertIn('grant_id', cli(self.dev, 'request-write')['result'])
        self.assertTrue(cli('dev2', 'request-write')['result']['waiting'])
        blk = cli('dev2', 'blocked', '--reason', '等待 dev1', '--depends-on-role', self.dev, '--depends-on-resource', 'worktree')
        self.assertEqual(blk['result']['depends_on_role'], self.dev)
        # dev1 hands off -> selected.json cleared & dev2 block resolved!
        (self.project / 'out.txt').write_text('v1', encoding='utf-8')
        stopped = __import__('json').dumps({'evidence': {'stopped': True, 'processes': 'none', 'files': 'out.txt', 'external': 'none'}})
        cli(self.dev, 'handoff', '--report', 'finished A', '--paths', 'out.txt', '--json', stopped)
        self.assertFalse((self.data / 'requests' / b[self.dev]['id'] / 'selected.json').exists())
        # dev2 runs exact same `request-write` command WITHOUT --new-request -> MUST NOT replay waiting: true!
        got_lock = cli('dev2', 'request-write')
        self.assertIn('grant_id', got_lock['result'])
        cli('dev2', 'handoff', '--report', 'finished B', '--paths', 'out.txt', '--json', stopped)
        # Review fails, then rework, then review-start AGAIN with exact same args -> MUST create new review_id!
        r1 = cli('lead', 'review-start', '--role-id', self.reviewer, '--paths', 'out.txt', writable_journal=False)['result']
        rt1 = next(c['ticket'] for c in cli(self.reviewer, 'status', writable_journal=False)['result']['candidates'] if c['id'] == r1['task_id'])
        cli(self.reviewer, 'accept', '--task', r1['task_id'], '--ticket', rt1, writable_journal=False)
        cli(self.reviewer, 'review-submit', '--review-id', r1['review_id'], '--verdict', 'fail', '--report', 'fix', '--task', r1['task_id'], '--ticket', rt1, writable_journal=False)
        d3 = cli('lead', 'dispatch', '--role', self.dev, '--title', 'A2', '--instructions', 'fix A', '--completion', 'fixed', '--mode', 'write', writable_journal=False)['result']['task_id']
        t3 = next(c['ticket'] for c in cli(self.dev, 'status')['result']['candidates'] if c['id'] == d3)
        cli(self.dev, 'accept', '--task', d3, '--ticket', t3)
        cli(self.dev, 'request-write')
        (self.project / 'out.txt').write_text('v2', encoding='utf-8')
        cli(self.dev, 'handoff', '--report', 'fixed A', '--paths', 'out.txt', '--json', stopped)
        r2 = cli('lead', 'review-start', '--role-id', self.reviewer, '--paths', 'out.txt', writable_journal=False)['result']
        self.assertNotEqual(r1['review_id'], r2['review_id'])


if __name__ == '__main__':
    unittest.main()
