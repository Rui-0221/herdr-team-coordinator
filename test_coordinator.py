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

    def test_message_replay_queue_and_stale_task_version(self):
        panel = {'run_id': self.run, 'message_id': new_id(), 'text': '检查界面'}
        self.assertEqual(self.app.local('say', panel), self.app.local('say', panel))
        action = {**self.dispatch_params(), 'source': panel['message_id']}
        task = self.member('dispatch', action)['result']['task_id']
        self.assertEqual(task, self.member('dispatch', action)['result']['task_id'])
        self.assertFalse(self.member('dispatch', {**action, 'title': '另一个任务'})['ok'])
        queued = self.member('dispatch', self.dispatch_params())['result']
        self.assertEqual(queued['state'], 'queued')
        self.assertFalse(self.member('accept', self.task_ticket(queued['task_id']), self.dev)['ok'])
        ticket = self.task_ticket(task)
        self.assertTrue(self.member('accept', ticket, self.dev)['ok'])
        change = {'task_id': task, 'version': 1, 'completion': '可按键盘导出'}
        self.assertFalse(self.member('amend-task', change, self.reviewer)['ok'])
        self.assertTrue(self.member('amend-task', change, self.dev)['ok'])
        self.assertFalse(self.member('amend-task', change)['ok'])
        handoff = {**ticket, 'report': '核对完毕', 'version': 2}
        self.assertFalse(self.member('handoff', handoff, self.dev)['ok'])
        self.assertTrue(self.member('ack-task', {**ticket, 'version': 2}, self.dev)['ok'])
        self.assertFalse(self.member('handoff', {**handoff, 'version': 1}, self.dev)['ok'])
        self.assertTrue(self.member('handoff', handoff, self.dev)['ok'])
        self.app.maintenance(inspect=False)
        self.assertTrue(self.member('accept', self.task_ticket(queued['task_id']), self.dev)['ok'])

    def test_forwarded_message_keeps_task_context_and_reply_history(self):
        initial = {'run_id':self.run, 'to':self.dev, 'message_id':new_id(), 'text':'帮我协调一个新任务'}
        self.app.local('say', initial)
        forward = {'source':initial['message_id'], 'to':'lead', 'text':'请主控选择成员'}
        forwarded = self.member('say', forward, self.dev)['result']['message_id']
        self.assertFalse(self.member('say', {'source':forwarded,'text':'错用消息'}, self.dev)['ok'])
        params = {**self.dispatch_params(), 'source':forwarded}
        result = self.member('dispatch', params)['result']
        task = result['task_id']
        history = self.member('history', {'task_id':task})['result']['conversation']
        self.assertEqual([m['id'] for m in history], [initial['message_id'], forwarded])
        reply = {'run_id':self.run, 'message_id':new_id(), 'reply_to':initial['message_id'], 'text':'刚才那个再加键盘'}
        self.app.local('say', reply)
        pending = self.member('status')['result']['pending_messages']
        self.assertEqual([(m['id'],m['task_id']) for m in pending], [(reply['message_id'],task)])
        self.member('amend-task', {'version':1, 'task_id':task, 'completion':'键盘', 'source':reply['message_id']})
        old = self.member('history', {'task_id':task,'version':1})['result']['requirements']
        current = self.member('history', {'task_id':task})['result']['requirements']
        self.assertEqual(old['completion'], self.dispatch_params()['completion'])
        self.assertIn('键盘', current['completion'])
        self.assertFalse(self.member('history', {'task_id':task,'version':4})['ok'])

    def test_reopen_handoff_invalidates_acceptance_and_uses_new_round(self):
        task, _ = self.dispatch()
        ticket = self.task_ticket(task)
        self.assertTrue(self.member('accept', ticket, self.dev)['ok'])
        (self.project / 'out.txt').write_text('result', encoding='utf8')
        self.assertTrue(self.member('handoff', {**ticket, 'paths':['out.txt'], 'report':'一轮'}, self.dev)['ok'])
        review = self.member('review-start', {'role_id':self.reviewer, 'paths':['out.txt']})['result']
        reviewer = self.member('status', role=self.reviewer)['result']['candidates'][0]
        r_ticket = {'task':review['task_id'], 'ticket':reviewer['ticket']}
        submission = {**r_ticket, 'review_id':review['review_id'], 'verdict':'pass', 'report':'通过'}
        self.assertIn('coord accept', reviewer['instructions'])
        premature = self.member('review-submit', submission, self.reviewer)
        self.assertFalse(premature['ok'])
        self.assertIn('coord accept', premature['next'])
        self.member('accept', r_ticket, self.reviewer)
        self.assertTrue(self.member('review-submit', submission, self.reviewer)['ok'])
        question = self.member('ask', {'purpose':'acceptance','text':'验收？'})['result']
        change = self.member('amend-task', {'task_id':task, 'instructions':'支持键盘', 'version':1})['result']
        self.assertEqual((change['task_id'], change['state']), (task, 'offered'))
        self.assertFalse(self.member('answer', {'question_id':question['question_id'], 'response':'yes'})['ok'])
        self.assertFalse(self.member('accept', ticket, self.dev)['ok'])
        new_ticket = self.task_ticket(task)
        self.member('accept', new_ticket, self.dev)
        self.member('ack-task', {**new_ticket,'version':2}, self.dev)
        self.assertTrue(self.member('handoff', {**new_ticket, 'version':2, 'paths':['out.txt'], 'report':'二轮'}, self.dev)['ok'])
        self.member('review-start', {'role_id':self.reviewer, 'paths':['out.txt']})
        status = self.member('status')['result']
        self.assertEqual(len(status['review']['handoffs']), 1)
        self.assertEqual(len([h for h in status['handoffs'] if h['task_id'] == task]), 2)

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

    def test_native_relay_cannot_change_target_role_or_call_local_actions(self):
        with self.app.tx() as db:
            db.execute("UPDATE bindings SET execution_mode='full_auto' WHERE id=?", (self.bindings[self.dev]['id'],))
        direct = self.message('status', role=self.dev)
        self.assertFalse(self.app.handle(direct, 'member')['ok'])
        forged = {'action': 'status', 'params': {}, 'auth': self.message('status')['auth']}
        relay = self.message('native-relay', forged, role=self.dev)
        reply = self.app.handle(relay, 'member')
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['result']['role'], self.dev)
        relay['params'] = {'action': 'service-stop', 'params': {}}
        self.assertFalse(self.app.handle(relay, 'member')['ok'])
        self.assertFalse(self.app.stop.is_set())

    def test_native_worker_claims_outbox_without_calling_hc_send(self):
        with self.app.tx() as db:
            db.execute("UPDATE bindings SET execution_mode='full_auto' WHERE id=?", (self.bindings[self.dev]['id'],))
        task, identifier = self.dispatch()
        self.app.maintenance(inspect=False)
        self.assertFalse(self.core.attempts)
        outcome = self.member('native-next', role=self.dev)
        self.assertTrue(outcome['ok'], outcome)
        claim = outcome['result']
        self.assertEqual(claim['id'], identifier)
        self.assertEqual(self.outbox(identifier)['state'], 'sending')
        self.assertEqual(self.member('native-next', role=self.dev)['result'], {})
        self.assertFalse(self.app.handle(self.message('native-finish', {
            'id': identifier, 'attempt': 'wrong', 'ok': True, 'evidence': 'wrong attempt',
        }, self.dev), 'member')['ok'])
        finished = self.member('native-finish', {
            'id': identifier, 'attempt': claim['attempt'], 'ok': True, 'evidence': 'native done',
        }, self.dev)
        self.assertTrue(finished['ok'], finished)
        self.assertEqual(self.outbox(identifier)['state'], 'delivered')
        self.assertFalse(self.core.attempts)

    def test_direct_user_directive_versions_task_and_survives_lead_replacement(self):
        task, _ = self.dispatch()
        chat = {'run_id': self.run, 'message_id': 'panel-lead', 'text': '请整理团队验收'}
        self.app.local('say', chat)
        direct = {'run_id': self.run, 'message_id': 'panel-worker', 'text': '页脚保留用户条款',
                  'to': self.dev, 'task_id': task, 'version': 1}
        updated = self.app.local('steer-task', direct)
        self.assertEqual(self.app.local('steer-task', direct), updated)
        self.assertEqual(updated['task_version'], 2)
        self.assertFalse(self.member('amend-task', {'task_id': task, 'version': 1, 'instructions': '过期替换'})['ok'])
        self.assertTrue(self.member('amend-task', {'task_id': task, 'version': 2, 'edit': 'replace',
                                                   'instructions': '新版主控执行说明', 'source': 'panel-lead'})['ok'])
        inspector = self.app.local('role-inspector', {'run_id': self.run, 'role_id': self.dev})
        self.assertEqual(inspector['task']['instructions'], '新版主控执行说明')
        self.assertEqual(inspector['task']['task_version'], 3)
        self.assertEqual(inspector['task']['user_directives'][0]['text'], '页脚保留用户条款')
        self.assertEqual([row['id'] for row in inspector['conversation']], ['panel-worker'])
        snapshot = self.app.local('role-inspector', {'run_id': self.run, 'role_id': self.dev, 'task_id': task, 'version': 1})
        self.assertEqual(snapshot['task']['instructions'], '先核对版本')
        self.assertEqual(snapshot['task']['user_directives'], [])
        status = self.member('status')['result']
        self.assertIn('panel-worker', [row['id'] for row in status['conversation']])

    def test_role_output_is_persisted_incrementally_and_keeps_role_scope(self):
        with self.app.tx() as db:
            db.execute("UPDATE bindings SET execution_mode='full_auto' WHERE id=?", (self.bindings[self.dev]['id'],))
        _, identifier = self.dispatch()
        turn = self.member('native-next', role=self.dev)['result']
        trace = self.app.control / 'native-evidence' / self.bindings[self.dev]['id'] / f'{identifier}-{turn["attempt"]}.stdout.jsonl'
        trace.parent.mkdir(parents=True)
        trace.write_bytes(b'{"type":"text","text":"first"}\n{"type":"thinking","text":"hidden"}\n{"type":"tool')
        params = {'run_id': self.run, 'role_id': self.dev, 'attempt': turn['attempt'], 'include_prompt': True}
        first = self.app.local('role-output', params)
        self.assertEqual([item['text'] for item in first['stdout']['events']], ['first'])
        self.assertIn('先核对版本', first['prompt'])
        with trace.open('ab') as sink:
            sink.write(b'_result","result":"ticket=abcdef123456"}\n')
        second = self.app.local('role-output', {**params, 'stdout_after': first['stdout']['next']})
        self.assertEqual([item['text'] for item in second['stdout']['events']], ['ticket=[已隐藏]'])
        forbidden = {**params, 'role_id': self.reviewer}
        self.assertFalse(self.app.handle({'action': 'role-output', 'params': forbidden}, 'local')['ok'])
        self.assertFalse(self.member('role-output', params, self.dev)['ok'])

    def test_native_goal_change_settles_old_attempt_and_requeues_pending_role_chat(self):
        with self.app.tx() as db:
            db.execute("UPDATE bindings SET execution_mode='full_auto' WHERE id=?", (self.bindings['lead']['id'],))
        self.app.local('say', {'run_id': self.run, 'message_id': 'goal-message', 'text': '更换团队目标'})
        old = self.member('native-next')['result']
        self.app.local('say', {'run_id': self.run, 'message_id': 'second-message', 'text': '仍要核对'})
        relay = self.message('native-relay', {'action': 'change-goal', 'request_id': new_id(),
                            'params': {'goal': '新目标', 'source': 'goal-message'}})
        self.assertTrue(self.app.handle(relay, 'member')['ok'])
        self.assertEqual(self.member('native-next')['result'], {})
        receipt = {'id': old['id'], 'attempt': old['attempt'], 'ok': True, 'evidence': 'finished'}
        self.assertEqual(self.member('native-finish', receipt)['result']['state'], 'obsolete')
        self.assertIn('仍要核对', self.member('native-next')['result']['prompt'])

    def test_status_recovers_unknown_chat_without_marking_business_action_complete(self):
        msg = {'run_id': self.run, 'message_id': new_id(), 'text': '请核对要求'}
        self.app.local('say', msg)
        with self.app.tx(False) as db:
            row = db.execute('SELECT id FROM outbox WHERE chat_id=?', (msg['message_id'],)).fetchone()
        identifier = row['id']
        self.app.approve_delivery({'run_id': self.run, 'delivery_id': identifier})
        self.core.send_outcome = {'status': 'unknown', 'evidence': 'Connection lost'}
        self.app.deliver(identifier)
        self.assertEqual(self.outbox(identifier)['state'], 'unknown')
        result = self.member('status')['result']
        self.assertIn(msg['message_id'], [message['id'] for message in result['pending_messages']])
        self.assertEqual(self.outbox(identifier)['state'], 'observed')
        self.assertTrue(self.member('say', {'source': msg['message_id'], 'text': '收到'})['ok'])

    def test_paused_run_holds_chat_and_version_ack_consumes_only_update(self):
        task, offer = self.dispatch()
        ticket = self.task_ticket(task)
        self.assertTrue(self.member('accept', ticket, self.dev)['ok'])
        self.assertTrue(self.member('amend-task', {'task_id': task, 'version': 1, 'completion': '更多要求'})['ok'])
        with self.app.tx(False) as db:
            update = db.execute('SELECT id FROM outbox WHERE task_id=? AND task_version=2', (task,)).fetchone()['id']
        self.assertTrue(self.member('ack-task', {**ticket, 'version': 2}, self.dev)['ok'])
        self.assertEqual(self.outbox(update)['state'], 'observed')
        self.assertEqual(self.outbox(offer)['state'], 'delivered')
        self.app.local('pause', {'run_id': self.run, 'reason': '暂停'})
        self.app.local('say', {'run_id': self.run, 'message_id': new_id(), 'text': '恢复后处理'})
        with self.app.tx(False) as db:
            chat = db.execute("SELECT id FROM outbox WHERE kind='chat'").fetchone()['id']
        self.app.maintenance(inspect=False)
        self.assertEqual(self.outbox(chat)['state'], 'queued')
        self.assertFalse(self.core.attempts)

    def test_managed_restart_rekeys_failed_session_and_retains_task(self):
        task, _ = self.dispatch()
        ticket = self.task_ticket(task)
        self.assertTrue(self.member('accept', ticket, self.dev)['ok'])
        old = self.bindings[self.dev]
        self.core.terminals[old['terminal']]['state'] = 'finished'
        result = self.app.local('restart_managed', {'run_id': self.run, 'binding_id': old['id']})
        self.assertEqual(result['state'], 'pending')
        self.assertFalse(self.member('status', role=self.dev)['ok'])
        self.app.launch_binding(result['id'])
        with self.app.tx(False) as db:
            self.bindings[self.dev] = dict(self.app.get_binding(db, result['id']))
        self.assertTrue(self.bindings[self.dev]['created_by_plugin'])
        self.assertNotEqual(old['token'], self.bindings[self.dev]['token'])
        replacement = self.task_ticket(task)
        self.assertNotEqual(ticket, replacement)
        self.assertTrue(self.member('accept', replacement, self.dev)['ok'])

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
        # A member and the lead amend the same live writer through the real CLI.
        direct = cli(self.dev, 'say', '--to', self.dev, '--text', '也要支持键盘', '--task-id', d1)['result']['message_id']
        cli(self.dev, 'amend-task', '--source', direct, '--task-id', d1, '--version', '1', '--completion', '键盘可操作')
        message = {'run_id': run_id, 'message_id': new_id(), 'reply_to': direct, 'text': '刚才那个还要显示焦点'}
        self.app.local('say', message)
        status = cli('lead', 'status')['result']
        self.assertTrue(next(m for m in status['members'] if m['role_id'] == 'dev2')['duties'])
        self.assertEqual(status['pending_messages'][0]['task_id'], d1)
        cli('lead', 'amend-task', '--source', message['message_id'], '--task-id', d1, '--version', '2', '--completion', '焦点可见')
        cli(self.dev, 'ack-task', '--version', '3')
        status = cli('lead', 'status')['result']
        self.assertEqual(status['run']['revision'], 1)
        self.assertEqual(next(t for t in status['tasks'] if t['id'] == d2)['task_version'], 1)
        self.assertEqual(next(t for t in status['tasks'] if t['id'] == d1)['ack_version'], 3)
        self.assertEqual(len(status['write_grants']), 1)
        self.assertEqual(len(cli('lead', 'history')['result']['conversation']), 2)
        # dev1 hands off -> selected.json cleared & dev2 block resolved!
        (self.project / 'out.txt').write_text('v1', encoding='utf-8')
        stopped = __import__('json').dumps({'evidence': {'stopped': True, 'processes': 'none', 'files': 'out.txt', 'external': 'none'}})
        cli(self.dev, 'handoff', '--version', '3', '--report', 'finished A', '--paths', 'out.txt', '--json', stopped)
        self.assertFalse((self.data / 'requests' / b[self.dev]['id'] / 'selected.json').exists())
        # dev2 runs exact same `request-write` command WITHOUT --new-request -> MUST NOT replay waiting: true!
        got_lock = cli('dev2', 'request-write')
        self.assertIn('grant_id', got_lock['result'])
        cli('dev2', 'handoff', '--report', 'finished B', '--paths', 'out.txt', '--json', stopped)
        # Review fails, then rework, then review-start AGAIN with exact same args -> MUST create new review_id!
        r1 = cli('lead', 'review-start', '--role-id', self.reviewer, '--paths', 'out.txt', writable_journal=False)['result']
        rt1 = next(c['ticket'] for c in cli(self.reviewer, 'status', writable_journal=False)['result']['candidates'] if c['id'] == r1['task_id'])
        reviewed = next(c for c in cli(self.reviewer, 'status')['result']['candidates'] if c['id'] == r1['task_id'])
        self.assertIn('键盘可操作', reviewed['instructions'])
        self.assertIn('焦点可见', reviewed['instructions'])
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
