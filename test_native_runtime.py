import io
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
import unittest
from unittest.mock import patch

from config_store import default_config, normalize_config
from host_native import engine_environment
from native_acp import Connection
from native_guest import native_result
from native_worker import dsh_model_override, engine_arguments


class ExecutionModeTests(unittest.TestCase):
    def test_existing_config_keeps_existing_interactive_transport(self):
        members = normalize_config(default_config())['teams'][0]['members']
        self.assertEqual({member['execution_mode'] for member in members}, {'legacy'})

    def test_team_configuration_persists_each_selected_mode(self):
        config = default_config()
        config['teams'][0]['members'][0]['execution_mode'] = 'full_auto'
        config['teams'][0]['members'][1]['execution_mode'] = 'approval'
        members = normalize_config(config)['teams'][0]['members']
        self.assertEqual([m['execution_mode'] for m in members], ['full_auto', 'approval', 'legacy'])

    def test_codex_native_permissions_change_with_selected_mode(self):
        self.assertIn('--dangerously-bypass-approvals-and-sandbox', engine_arguments('codex', 'full_auto'))
        approval = engine_arguments('codex', 'approval')
        self.assertIn('read-only', approval)
        self.assertIn('approval_policy="on-request"', approval)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', approval)
        self.assertIn('workspace-write', engine_arguments('codex', 'approval', workspace_write=True))

    def test_dsh_native_flags_select_policy_without_profile_mutation(self):
        self.assertEqual(engine_arguments('dsh', 'full_auto')[-5:],
                         ['--permission-mode', 'danger-full-access', '--approval-policy', 'never', '-'])
        self.assertEqual(engine_arguments('dsh', 'approval')[-5:],
                         ['--permission-mode', 'workspace-write', '--approval-policy', 'ask', '-'])

    def test_dsh_role_can_select_a_provider_owned_model_without_changing_the_saved_profile(self):
        self.assertEqual(dsh_model_override('default', 'default'), {})
        self.assertEqual(dsh_model_override('magpie/auto/deepseek-v4.1-flash', 'low'),
                         {'provider': 'magpie', 'model': 'auto/deepseek-v4.1-flash', 'reasoningEffort': 'low'})
        with self.assertRaises(ValueError):
            dsh_model_override('unqualified-model', 'default')

    def test_antigravity_native_permissions_preserve_stream_protocol(self):
        full = engine_arguments('antigravity', 'full_auto')
        approval = engine_arguments('antigravity', 'approval', workspace_write=True)
        self.assertEqual(full[1], 'acp')
        self.assertIn('--dangerously-skip-permissions', full)
        self.assertIn('--sandbox', approval)
        self.assertNotIn('--dangerously-skip-permissions', approval)

    def test_acp_approval_mode_is_set_before_prompt(self):
        replies = [{'id': 1, 'result': {'protocolVersion': 1}}, {'id': 2, 'result': {'sessionId': 'test'}},
                   {'id': 3, 'result': {}}, {'id': 4, 'result': {'stopReason': 'end_turn'}}]
        stdin, stdout, evidence = io.StringIO(), io.StringIO(''.join(json.dumps(row) + '\n' for row in replies)), io.StringIO()
        connection = Connection(stdin, stdout, evidence, full_auto=False)
        config = {'project': 'C:/fixture', 'write': True, 'model': 'default', 'effort': 'default'}
        self.assertEqual(connection.prompt(config, 'marker')['stopReason'], 'end_turn')
        mode = json.loads(stdin.getvalue().splitlines()[2])
        self.assertEqual((mode['method'], mode['params']['modeId']), ('session/set_mode', 'accept-edits'))

    def test_host_workers_retain_profile_paths_and_drop_coordinator_credentials(self):
        inputs = {'USERPROFILE': 'C:/real-user', 'HOME': 'C:/real-user', 'APPDATA': 'C:/real-roaming',
                  'LOCALAPPDATA': 'C:/real-local', 'HERDR_COORD_TOKEN': 'must-stay-with-broker'}
        with patch.dict(os.environ, inputs), patch('host_native.shutil.which', return_value=None):
            env = engine_environment(Path('C:/fixture/runtime'), Path('C:/fixture/private'))
        self.assertEqual({key: env[key] for key in list(inputs)[:4]}, {key: inputs[key] for key in list(inputs)[:4]})
        self.assertNotIn('HERDR_COORD_TOKEN', env)
        self.assertEqual(Path(env['TEMP']), Path('C:/fixture/private'))


class NativeReceiptTests(unittest.TestCase):
    def test_dsh_startup_permission_error_preserves_evidence_without_path(self):
        for operation in ('lstat', 'realpath'):
            with self.subTest(operation=operation):
                result = CompletedProcess([], 1, '', f"node:fs:2835\nError: EPERM: operation not permitted, {operation} 'private path'\n")
                ok, evidence = native_result('dsh', result)
                self.assertFalse(ok)
                self.assertIn(f'EPERM {operation}', evidence)
                self.assertNotIn('private path', evidence)

    def test_antigravity_failed_login_has_no_successful_receipt(self):
        result = CompletedProcess([], 1, '', 'Authentication required')
        self.assertEqual(native_result('antigravity', result), (False, '当前 Windows 运行身份无法使用引擎登录'))
