"""Stage 2/3 exercises using disposable fixture directories and an in-memory database."""

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from artifacts import identify_worktree, path_key, snapshot
from common import DomainError, now
from coordinator import Coordinator, SCHEMA
from workflow import install_workflow_schema, route_workflow, workflow_status


STOPPED = {
    'stopped': True, 'terminal': 'Checked the stored terminal instance.', 'processes': 'All writers were stopped.',
    'files': 'Compared the source and resulting bytes.', 'handoff': 'Retained the partial delivery.', 'external': 'No external write.',
}


def git(directory, *args):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
    return subprocess.run(['git', '-C', str(directory), *args], check=True, capture_output=True,
                          env={**env, 'GIT_OPTIONAL_LOCKS': '0'}, timeout=15)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = Path(tempfile.mkdtemp(prefix='herdr-coord-workflow-'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.fixtures, ignore_errors=True)

    def setUp(self):
        self.home = self.fixtures / self._testMethodName
        self.home.mkdir()
        (self.home / 'sub').mkdir()
        (self.home / 'artifact.txt').write_text('version one', encoding='utf-8')
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA)
        install_workflow_schema(self.db)
        self.service = object.__new__(Coordinator)
        with self.db:
            self.db.execute('INSERT INTO runs VALUES(?,?,?,1,?,?,?,?)', ('run', str(self.home), 'goal', 'active', 'available', 'chat', now()))
            for role, kind, write in [('lead', 'master', 0), ('front', 'member', 1), ('back', 'member', 1), ('review', 'reviewer', 0)]:
                self.db.execute('''INSERT INTO bindings(id,run_id,role_id,kind,terminal,instance,created_by_plugin,write_allowed,provider,model,token)
                                   VALUES(?,?,?,?,?,?,0,?,'codex','test','test')''', (role, 'run', role, kind, f't-{role}', f'i-{role}', write))

    def run_row(self):
        return self.service.get_run(self.db, 'run')

    def task(self, role, mode='write'):
        binding = self.service.get_binding(self.db, role)
        task_id = self.service.offer(self.db, self.run_row(), binding, role, 'finish the deliverable', True, 'record completion', mode)
        self.accept(role, task_id)
        return task_id

    def accept(self, role, task_id):
        self.service.mutate(self.db, 'accept', self.actor(role, task_id), {}, self.run_row())

    def actor(self, role, task_id=None):
        binding = self.service.get_binding(self.db, role) if role != 'local' else None
        return {'channel': 'member' if binding else 'local', 'kind': binding['kind'] if binding else 'local',
                'run_id': 'run', 'role_id': role, 'binding_id': role if binding else None, 'task_id': task_id}

    def call(self, action, role='lead', task=None, **params):
        with self.db:
            return route_workflow(self.db, action, self.actor(role, task), params, self.service.offer)

    def full_handoff(self):
        task = self.task('front')
        self.call('request-write', 'front', task)
        self.call('handoff', 'front', task, report='Tested deliverable.', evidence=STOPPED, paths=['artifact.txt'])

    def review(self):
        review = self.call('review-start', role_id='review', paths=['artifact.txt'])
        self.accept('review', review['task_id'])
        return review

    def pass_review(self, review):
        return self.call('review-submit', 'review', review['task_id'], review_id=review['review_id'], verdict='pass', report='Reviewed bytes.')

    def test_parent_child_wait_and_explicit_release(self):
        first, second = self.task('front'), self.task('back')
        grant = self.call('request-write', 'front', first)
        wait = self.call('request-write', 'back', second, path='sub')
        self.assertTrue(wait['waiting'])
        self.assertEqual(wait['owner'], 'front')
        with self.assertRaises(DomainError):
            self.call('yield', 'front', first, report='Stopped without sufficient evidence.')
        self.assertEqual(workflow_status(self.db, 'run')['write_grants'][0]['id'], grant['grant_id'])
        self.call('yield', 'front', first, report='Paused with partial work.', evidence=STOPPED)
        self.assertEqual(workflow_status(self.db, 'run')['write_waits'][0]['state'], 'ready')
        second_grant = self.call('request-write', 'back', second, path='sub')
        self.assertNotEqual(grant['grant_id'], second_grant['grant_id'])

    def test_pause_retains_unknown_writer_and_recovery_preserves_task(self):
        task = self.task('front')
        grant = self.call('request-write', 'front', task)
        result = self.call('pause', reason='interrupted writer', emergency=True)
        self.assertEqual(result['checkpoint']['holders'][0]['id'], grant['grant_id'])
        with self.assertRaises(DomainError):
            self.call('resume', task_id=task, evidence=STOPPED)
        with self.assertRaises(DomainError):
            self.call('recover-write', grant_ids=[grant['grant_id']], evidence=STOPPED)
        self.call('recover-write', 'local', grant_ids=[grant['grant_id']], evidence=STOPPED)
        resumed = self.call('resume', task_id=task, evidence=STOPPED)
        self.assertEqual(resumed['previous_task'], task)
        current = self.db.execute('SELECT * FROM tasks WHERE id=?', (resumed['task_id'],)).fetchone()
        self.assertEqual(current['round'], 2)
        self.assertIn(STOPPED['handoff'], current['instructions'])

    def test_writer_release_during_pause_preserves_wakeup_for_resume(self):
        first, second = self.task('front'), self.task('back')
        self.call('request-write', 'front', first)
        self.assertTrue(self.call('request-write', 'back', second)['waiting'])
        paused = self.call('pause', reason='wait for operator')
        self.service.apply_notices(self.db, self.run_row(), paused)
        result = self.call('yield', 'front', first, report='Stopped writing.', evidence=STOPPED)
        self.service.apply_notices(self.db, self.run_row(), result)
        notification = self.db.execute("SELECT * FROM outbox WHERE task_id=? AND kind='workflow'", (second,)).fetchone()
        pause_notice = self.db.execute("SELECT * FROM outbox WHERE kind='pause' LIMIT 1").fetchone()
        self.assertIsNotNone(notification)
        self.assertEqual(self.service.delivery_state(self.db, notification), 'held')
        self.call('resume')
        self.assertEqual(self.service.delivery_state(self.db, notification), 'valid')
        self.assertEqual(self.service.delivery_state(self.db, pause_notice), 'invalid')

    def test_old_receipt_can_only_release_original_grant(self):
        task = self.task('front')
        self.call('request-write', 'front', task)
        self.call('change-goal', goal='new goal')
        with self.assertRaises(DomainError):
            self.call('handoff', 'front', task, report='Late.', evidence=STOPPED, paths=['artifact.txt'])
        receipt = self.call('yield', 'front', task, report='Stopped original round.', evidence=STOPPED)
        self.assertTrue(receipt['old_round'])
        current = self.call('resolve-revision', task_id=task, decision='reuse', carried='Artifact retained.')
        self.accept('front', current['task_id'])
        grant = self.call('request-write', 'front', current['task_id'])
        with self.assertRaises(DomainError):
            self.call('yield', 'front', task, report='Duplicate late receipt.', evidence=STOPPED)
        self.assertEqual(workflow_status(self.db, 'run')['write_grants'][0]['id'], grant['grant_id'])

    def test_cancelling_wrong_dispatch_preserves_in_flight_evidence_and_old_grant(self):
        task = self.task('front')
        grant = self.call('request-write', 'front', task)
        self.db.execute("UPDATE outbox SET state='sending' WHERE task_id=?", (task,))
        cancelled = self.call('cancel-task', task_id=task, reason='Incorrect subtask.')
        self.assertEqual(cancelled['grants_retained'], [grant['grant_id']])
        self.assertEqual(self.db.execute('SELECT state FROM outbox WHERE task_id=?', (task,)).fetchone()[0], 'stale_delivery')
        self.assertEqual(self.run_row()['revision'], 1)
        second = self.task('back')
        self.assertTrue(self.call('request-write', 'back', second)['waiting'])
        self.assertTrue(self.call('yield', 'front', task, report='Stopped wrong task.', evidence=STOPPED)['old_round'])
        self.assertIn('grant_id', self.call('request-write', 'back', second))

    def test_goal_change_revokes_unsent_offer_and_lists_unknown_offer(self):
        def offer(role):
            return self.service.offer(self.db, self.run_row(), self.service.get_binding(self.db, role), role,
                                      'finish', True, 'record completion', 'read')

        unsent, unknown = offer('front'), offer('back')
        self.db.execute("UPDATE outbox SET state='unknown' WHERE task_id=?", (unknown,))
        result = self.call('change-goal', goal='new goal')
        self.assertEqual(result['revoked'], [unsent])
        self.assertEqual([task['id'] for task in result['tasks']], [unknown])
        self.assertEqual(self.db.execute('SELECT state FROM outbox WHERE task_id=?', (unsent,)).fetchone()[0], 'obsolete')
        self.assertEqual(self.db.execute('SELECT state FROM outbox WHERE task_id=?', (unknown,)).fetchone()[0], 'unknown')

    def test_integration_answer_is_distinct_from_versioned_acceptance(self):
        self.full_handoff()
        review = self.review()
        self.assertEqual(self.pass_review(review)['state'], 'passed')
        question = self.call('ask', purpose='integration', text='Start integration?')
        self.call('answer', question_id=question['question_id'], response='yes')
        self.assertEqual(self.run_row()['state'], 'active')
        question = self.call('ask', purpose='acceptance', text='Accept this reviewed delivery?')
        decision = self.call('answer', question_id=question['question_id'], response='yes')
        self.assertEqual(decision['digest'], review['digest'])
        self.assertEqual(self.run_row()['state'], 'accepted')
        self.assertEqual(workflow_status(self.db, 'run')['acceptance_record']['channel'], 'master_relay')

    def test_changed_bytes_invalidate_review_and_question(self):
        self.full_handoff()
        review = self.review()
        self.pass_review(review)
        question = self.call('ask', purpose='acceptance', text='Accept?')
        (self.home / 'artifact.txt').write_text('version two', encoding='utf-8')
        result = self.call('answer', question_id=question['question_id'], response='yes')
        self.assertEqual(result['state'], 'invalid')
        self.assertEqual(self.run_row()['state'], 'active')
        self.assertIsNone(workflow_status(self.db, 'run')['question'])

    def test_high_risk_approval_binds_scope_and_local_channel(self):
        scope = {'target': 'file X', 'extent': 'one file', 'impact': 'recorded removal'}
        question = self.call('ask', purpose='delete', text='Delete this exact object?', scope=scope)
        with self.assertRaises(DomainError):
            self.call('answer', question_id=question['question_id'], response='yes')
        with self.assertRaises(DomainError):
            self.call('local-accept', 'local', question_id=question['question_id'], response='yes', revision=1, scope={**scope, 'extent': 'all files'})
        result = self.call('local-accept', 'local', question_id=question['question_id'], response='yes', revision=1, scope=scope)
        self.assertEqual(result['scope'], scope)
        self.assertEqual(result['state'], 'approved')
        self.assertTrue((self.home / 'artifact.txt').exists())

    def test_local_acceptance_and_replaced_question(self):
        self.db.execute("UPDATE runs SET acceptance='local' WHERE id='run'")
        self.full_handoff()
        review = self.review()
        self.pass_review(review)
        earlier = self.call('ask', purpose='integration', text='Start integration?')
        current = self.call('ask', purpose='acceptance', text='Accept this version?', replace=earlier['question_id'])
        with self.assertRaises(DomainError):
            self.call('answer', question_id=earlier['question_id'], response='yes')
        with self.assertRaises(DomainError):
            self.call('answer', question_id=current['question_id'], response='yes')
        result = self.call('local-accept', 'local', question_id=current['question_id'], revision=1, response='yes')
        self.assertEqual(result['run_state'], 'accepted')
        self.assertEqual(workflow_status(self.db, 'run')['acceptance_record']['channel'], 'local')

    def test_independent_reviewer_may_not_reuse_writer_session(self):
        self.full_handoff()
        self.db.execute("UPDATE bindings SET active=0 WHERE id='front'")
        self.db.execute("UPDATE bindings SET terminal='t-front', instance='i-front' WHERE id='review'")
        with self.assertRaises(DomainError):
            self.call('review-start', role_id='review', paths=['artifact.txt'])

    def test_handoff_report_file_path_is_checked_before_releasing_authority(self):
        task = self.task('front')
        self.call('request-write', 'front', task)
        report_file = self.home / 'report.md'
        report_file.write_text('Checked the fixture.', encoding='utf-8')
        with self.assertRaises(DomainError):
            self.call('handoff', 'front', task, report='Unsafe report path.', report_path='../report.md',
                      paths=['artifact.txt'], evidence=STOPPED)
        self.assertTrue(workflow_status(self.db, 'run')['write_grants'])
        self.call('handoff', 'front', task, report='Checked.', report_path='report.md', paths=['artifact.txt'], evidence=STOPPED)
        self.assertEqual(workflow_status(self.db, 'run')['handoffs'][0]['paths'], ['artifact.txt', 'report.md'])

    def test_review_refuses_partial_snapshot(self):
        self.full_handoff()
        result = self.call('review-start', role_id='review', paths=['.', 'artifact.txt'])
        self.assertEqual(result['state'], 'unverified')
        self.assertEqual(result['unverified'][0]['path'], '.')
        self.assertIsNone(workflow_status(self.db, 'run')['review'])

    @unittest.skipUnless(shutil.which('git'), 'requires local Git')
    def test_git_snapshot_covers_staged_unstaged_renamed_new_and_ignored_explicit(self):
        git(self.home, 'init', '-q')
        (self.home / '.gitignore').write_text('ignored.bin\n', encoding='utf-8')
        for name in ('change.txt', 'rename.txt', 'remove.txt'):
            (self.home / 'sub' / name).write_text(name, encoding='utf-8')
        git(self.home, 'add', '.')
        git(self.home, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        git(self.home, 'mv', 'sub/rename.txt', 'sub/renamed.txt')
        (self.home / 'sub' / 'remove.txt').rename(self.home / 'outside-scope.txt')
        change = self.home / 'sub' / 'change.txt'
        change.write_text('index', encoding='utf-8')
        git(self.home, 'add', 'sub/change.txt')
        change.write_text('working copy', encoding='utf-8')
        ignored = self.home / 'sub' / 'ignored.bin'
        ignored.write_bytes(b'ignored but declared')
        (self.home / 'sub' / 'new.txt').write_text('new', encoding='utf-8')
        first = snapshot(self.home, ['sub', 'sub/ignored.bin'])
        self.assertFalse(first['unverified'])
        self.assertEqual(first['head'], git(self.home, 'rev-parse', 'HEAD').stdout.decode().strip())
        self.assertIn(['R100', ['sub/rename.txt', 'sub/renamed.txt']], first['staged'])
        self.assertIn(['M', ['sub/change.txt']], first['staged'])
        self.assertIn(['M', ['sub/change.txt']], first['unstaged'])
        self.assertIn(['D', ['sub/remove.txt']], first['unstaged'])
        self.assertEqual(first['untracked'], ['sub/new.txt'])
        self.assertIn('sub/ignored.bin', first['files'])
        self.assertNotIn('outside-scope.txt', first['files'])
        self.assertEqual(path_key(identify_worktree(self.home / 'sub').root), path_key(self.home.resolve()))
        ignored.write_bytes(b'new ignored contents')
        self.assertNotEqual(first['digest'], snapshot(self.home, ['sub', 'sub/ignored.bin'])['digest'])
        self.assertTrue(snapshot(self.home, ['sub/ignored.bin'], byte_budget=2)['unverified'])

    @unittest.skipUnless(shutil.which('git'), 'requires local Git')
    def test_worktrees_are_independent_but_repository_refs_are_shared(self):
        git(self.home, 'init', '-q')
        git(self.home, 'add', 'artifact.txt')
        git(self.home, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        linked = self.fixtures / f'{self._testMethodName}-linked'
        git(self.home, 'worktree', 'add', '--detach', str(linked), 'HEAD')
        main_tree, linked_tree = identify_worktree(self.home), identify_worktree(linked)
        self.assertNotEqual(path_key(main_tree.root), path_key(linked_tree.root))
        self.assertEqual(path_key(main_tree.common), path_key(linked_tree.common))
        first = self.task('front')
        self.call('request-write', 'front', first)
        self.call('request-write', 'front', first, repository_refs=True)
        with self.db:
            self.db.execute('INSERT INTO runs VALUES(?,?,?,1,?,?,?,?)', ('linked', str(linked), 'linked worktree', 'active', 'available', 'chat', now()))
            self.db.execute("""INSERT INTO bindings(id,run_id,role_id,kind,terminal,instance,created_by_plugin,write_allowed,provider,model,token)
                               VALUES('linked-dev','linked','dev','member','linked-terminal','linked-instance',0,1,'codex','test','test')""")
            run = self.service.get_run(self.db, 'linked')
            binding = self.service.get_binding(self.db, 'linked-dev')
            second = self.service.offer(self.db, run, binding, 'another worktree', 'make changes', True, 'deliver', 'write')
            actor = {'channel': 'member', 'kind': 'member', 'run_id': 'linked', 'role_id': 'dev', 'binding_id': 'linked-dev', 'task_id': second}
            self.service.mutate(self.db, 'accept', actor, {}, run)
            independent = route_workflow(self.db, 'request-write', actor, {}, self.service.offer)
            shared = route_workflow(self.db, 'request-write', actor, {'repository_refs': True}, self.service.offer)
        self.assertEqual(independent['resource'], 'worktree')
        self.assertTrue(shared['waiting'])
        self.assertEqual(shared['owner'], 'front')

        # Sibling worktree holding 'refs' in another run must NOT block review on main_tree after front hands off
        self.call('handoff', 'front', first, report='done', evidence=STOPPED, paths=['artifact.txt'])
        with self.db:
            route_workflow(self.db, 'request-write', actor, {'repository_refs': True}, self.service.offer)
        review = self.review()
        self.assertEqual(review['state'], 'pending')

        # Physically nested worktree MUST conflict!
        nested = self.home / 'sub' / 'nested-wt'
        git(self.home, 'worktree', 'add', '--detach', str(nested), 'HEAD')
        with self.db:
            self.db.execute('INSERT INTO runs VALUES(?,?,?,1,?,?,?,?)', ('nested', str(nested), 'nested worktree', 'active', 'available', 'chat', now()))
            self.db.execute("""INSERT INTO bindings(id,run_id,role_id,kind,terminal,instance,created_by_plugin,write_allowed,provider,model,token)
                               VALUES('nested-dev','nested','dev','member','nested-terminal','nested-instance',0,1,'codex','test','test')""")
            n_run = self.service.get_run(self.db, 'nested')
            n_bind = self.service.get_binding(self.db, 'nested-dev')
            n_task = self.service.offer(self.db, n_run, n_bind, 'nested', 'changes', True, 'deliver', 'write')
            n_actor = {'channel': 'member', 'kind': 'member', 'run_id': 'nested', 'role_id': 'dev', 'binding_id': 'nested-dev', 'task_id': n_task}
            self.service.mutate(self.db, 'accept', n_actor, {}, n_run)
            t3 = self.task('front')
            self.call('request-write', 'front', t3)
            conflict = route_workflow(self.db, 'request-write', n_actor, {}, self.service.offer)
        self.assertTrue(conflict['waiting'])

    def test_fifo_write_wait_zero_task_guard_and_change_goal_reuse(self):
        # Zero tasks cannot start review
        with self.assertRaises(DomainError):
            self.call('review-start', role_id='review', paths=['artifact.txt'])

        # Add a third member for FIFO queue test and a fourth read-only member
        with self.db:
            self.db.execute("""INSERT INTO bindings(id,run_id,role_id,kind,terminal,instance,created_by_plugin,write_allowed,provider,model,token)
                               VALUES('third','run','third','member','t-third','i-third',0,1,'codex','test','test')""")
            self.db.execute("""INSERT INTO bindings(id,run_id,role_id,kind,terminal,instance,created_by_plugin,write_allowed,provider,model,token)
                               VALUES('reader','run','reader','member','t-reader','i-reader',0,0,'codex','test','test')""")
        t1, t2, t3 = self.task('front'), self.task('back'), self.task('third')
        self.call('request-write', 'front', t1)
        self.assertTrue(self.call('request-write', 'back', t2)['waiting'])
        self.call('handoff', 'front', t1, report='front done', evidence=STOPPED, paths=['artifact.txt'])
        # Third member must not jump ahead of ready waiter 'back'
        cut = self.call('request-write', 'third', t3)
        self.assertTrue(cut['waiting'])
        self.assertEqual(cut['owner'], 'back')
        # Read task can be resumed while another holds write lock
        r_read = self.task('reader', mode='read')
        self.call('yield', 'reader', r_read, report='pause read')
        self.assertIn('grant_id', self.call('request-write', 'back', t2))
        self.assertIn('task_id', self.call('resume', task_id=r_read))
        self.call('handoff', 'back', t2, report='back done', evidence=STOPPED, paths=['artifact.txt'])
        self.call('cancel-task', task_id=t3, reason='not needed')
        self.call('cancel-task', task_id=self.db.execute("SELECT id FROM tasks WHERE role_id='reader' AND state='offered'").fetchone()[0], reason='skip')

        # Review fails, then goal changes: historical review task must NOT enter needs_decision,
        # and reuse on completed handoff must NOT re-offer to developer!
        rev = self.review()
        self.call('review-submit', 'review', rev['task_id'], review_id=rev['review_id'], verdict='fail', report='needs v2')
        changed = self.call('change-goal', goal='goal v2')
        decided_roles = {item['role_id'] for item in changed['tasks']}
        self.assertNotIn('review', decided_roles)
        for item in changed['tasks']:
            if item['role_id'] == 'front':
                reused = self.call('resolve-revision', task_id=item['id'], decision='reuse', carried='keep front work')
                self.assertTrue(reused['reused_handoff'])
                self.assertEqual(self.db.execute('SELECT state FROM tasks WHERE id=?', (reused['task_id'],)).fetchone()[0], 'handed_off')
            else:
                self.call('resolve-revision', task_id=item['id'], decision='cancel', carried='drop back work')
        rev2 = self.review()
        self.pass_review(rev2)
        q = self.call('ask', purpose='acceptance', text='Accept v2?')
        accepted = self.call('answer', question_id=q['question_id'], response='yes')
        self.assertEqual(accepted['run_state'], 'accepted')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bindings WHERE active=1').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
