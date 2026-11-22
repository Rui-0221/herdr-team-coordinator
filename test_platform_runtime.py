import concurrent.futures
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from platform_runtime import Hc, PipeServer, _antigravity_models, model_catalog, request, resolve_tools, service_mutex


class ToolDiscoveryTests(unittest.TestCase):
    def test_dsh_prefers_standalone_launcher_over_desktop_and_dst(self):
        def installed(command):
            return 'C:/fixture/Local/dsh-cli/dsh.cmd' if command.endswith(('dsh-cli\\dsh.cmd', 'dsh-cli/dsh.cmd')) else None

        with patch.dict(os.environ, {'LOCALAPPDATA': 'C:/fixture/Local', 'APPDATA': 'C:/fixture/Roaming'}), \
                patch('platform_runtime.shutil.which', side_effect=installed) as which:
            self.assertEqual(Path(resolve_tools()['dsh']), Path('C:/fixture/Local/dsh-cli/dsh.cmd'))
        queried = {Path(call.args[0]).name for call in which.call_args_list}
        self.assertFalse({'dst.cmd', 'dsh.exe'} & queried)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.hc = Hc("unused")
        self.hc._call = Mock()
        self.hc.terminal = Mock(return_value={"instance": "t1/123/456", "state": "live",
                                             "status": "idle", "turn": 8})

    def test_without_draft_confirmation_core_is_never_called(self):
        result = self.hc.send("t1", "test", expected="t1/123/456")
        self.assertEqual(result["status"], "blocked")
        self.hc.terminal.assert_not_called()
        self.hc._call.assert_not_called()

    def test_terminal_replaced_before_send(self):
        result = self.hc.send("t1", "test", expected="t1/123/999", consent=True)
        self.assertEqual(result["status"], "not_sent")
        self.hc._call.assert_not_called()

    def test_timeout_keeps_outcome_unknown_without_attempting_another_send(self):
        self.hc._call.side_effect = subprocess.TimeoutExpired("hc", 75)
        result = self.hc.send("t1", "test", expected="t1/123/456", consent=True)
        self.assertEqual(result["status"], "unknown")
        self.hc._call.assert_called_once_with("send", "--if", "idle", "--", "t1", "test", timeout=75)

    def test_receipt_requires_started_true_and_matching_instance(self):
        initial = self.hc.terminal.return_value
        self.hc.terminal.side_effect = (initial, {**initial, "instance": "t1/123/999"})
        self.hc._call.return_value = {"turn": 8, "started": True}
        result = self.hc.send("t1", "test", expected="t1/123/456", consent=True)
        self.assertEqual(result["status"], "unknown")
        self.hc._call.assert_called_once()

    def test_started_false_or_null_never_claims_delivered(self):
        self.hc._confirm_polls = 0
        self.hc._call.return_value = {"turn": 8, "started": False}
        self.assertEqual(self.hc.send("t1", "test", expected="t1/123/456", consent=True)["status"], "unknown")
        self.hc._call.return_value = {"turn": 8, "started": None}
        self.assertEqual(self.hc.send("t1", "test", expected="t1/123/456", consent=True)["status"], "unknown")
        self.hc._call.return_value = {"turn": 8, "started": True}
        self.assertEqual(self.hc.send("t1", "test", expected="t1/123/456", consent=True)["status"], "delivered")

    def test_started_false_confirmed_delivered_within_grace_window(self):
        self.hc._confirm_polls = 2
        self.hc._confirm_interval = 0
        self.hc._call.return_value = {"turn": 8, "started": False}
        self.hc.terminal.side_effect = [
            {"instance": "t1/123/456", "state": "live", "status": "idle", "turn": 8},
            {"instance": "t1/123/456", "state": "live", "status": "idle", "turn": 8},
            {"instance": "t1/123/456", "state": "live", "status": "working", "turn": 8},
        ]
        res = self.hc.send("t1", "test", expected="t1/123/456", consent=True)
        self.assertEqual(res["status"], "delivered")
        self.assertEqual(res["evidence"]["confirmed_status"], "working")

        self.hc.terminal.side_effect = None
        self.hc.terminal.return_value = {"instance": "t1/123/456", "state": "live", "status": "idle", "turn": 8}
        self.assertEqual(self.hc.send("t1", "test", expected="t1/123/456", consent=True)["status"], "unknown")

    def test_finished_fast_turn_is_compared_with_pre_send_snapshot(self):
        self.hc._confirm_polls = 0
        self.hc._call.return_value = {"turn": 9, "status": "done", "started": False}
        initial = self.hc.terminal.return_value
        self.hc.terminal.side_effect = [initial, {**initial, "status": "done", "turn": 9}]
        receipt = self.hc.send("t1", "test", expected=initial["instance"], consent=True)
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(receipt["evidence"]["confirmed_turn"], 9)

    def test_started_false_keys_enter_recovers_only_verified_marker(self):
        self.hc._confirm_polls = 1
        self.hc._confirm_interval = 0
        self.hc._call.side_effect = [
            {"turn": 8, "started": False},
            {"ok": True},
        ]
        self.hc.read = Mock(return_value={
            "complete": True,
            "text": "• Done\n» [Herdr团队协调器 abcd12345678 v1] 收到新回报，请运行\n  coord status。\n\n  GPT-6.2-Sol",
        })
        self.hc.terminal.side_effect = [
            {"instance": "t1/123/456", "state": "live", "status": "idle", "turn": 8},
            {"instance": "t1/123/456", "state": "live", "status": "idle", "turn": 8},
            {"instance": "t1/123/456", "state": "live", "status": "idle", "turn": 8},
            {"instance": "t1/123/456", "state": "live", "status": "idle", "turn": 8},
            {"instance": "t1/123/456", "state": "live", "status": "working", "turn": 8},
        ]
        res = self.hc.send("t1", "[Herdr团队协调器 abcd12345678 v1] 收到新回报，请运行 coord status。",
                           expected="t1/123/456", consent=True, confirm_marker="[Herdr团队协调器 abcd12345678 v1]")
        self.assertEqual(res["status"], "delivered")
        self.assertEqual(res["evidence"]["confirmed_via"], "keys_enter")
        self.assertEqual(self.hc._call.call_args_list[1][0], ("keys", "--", "t1", "enter"))

        # Unrelated text in prompt box must NOT trigger keys enter!
        self.hc._call.side_effect = None
        self.hc._call.return_value = {"turn": 8, "started": False}
        self.hc._call.reset_mock()
        self.hc.terminal.side_effect = None
        self.hc.terminal.return_value = {"instance": "t1/123/456", "state": "live", "status": "idle", "turn": 8}
        self.hc.read.return_value = {"complete": True, "text": "› human typing something"}
        bad = self.hc.send("t1", "[Herdr团队协调器 abcd12345678 v1] text",
                           expected="t1/123/456", consent=True, confirm_marker="[Herdr团队协调器 abcd12345678 v1]")
        self.assertEqual(bad["status"], "unknown")
        self.hc._call.assert_called_once()
        self.hc._call.reset_mock()
        self.hc.read.return_value = {
            "complete": True, "text": "» [Herdr团队协调器 abcd12345678 v1] text edited by user",
        }
        self.assertEqual(self.hc.send("t1", "[Herdr团队协调器 abcd12345678 v1] text",
                                    expected="t1/123/456", consent=True,
                                    confirm_marker="[Herdr团队协调器 abcd12345678 v1]")["status"], "unknown")
        self.hc._call.assert_called_once()

    def test_done_status_omits_if_flag_and_accepts_started_true(self):
        self.hc.terminal.return_value = {"instance": "t1/123/456", "state": "live", "status": "done", "turn": 8}
        self.hc._call.return_value = {"turn": 8, "started": True}
        result = self.hc.send("t1", "test", expected="t1/123/456", consent=True)
        self.assertEqual(result["status"], "delivered")
        self.hc._call.assert_called_once_with("send", "--", "t1", "test", timeout=75)

    def test_launch_flags_preserve_approval_add_dir_and_antigravity_modes(self):
        tmp = Path(tempfile.mkdtemp(prefix="hc-launch-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        hc = Hc("unused")
        hc.tools = {"codex": str(tmp / "codex.exe"), "antigravity": str(tmp / "agy.exe"), "hc": "unused"}
        hc._rpc = Mock(return_value={"address": "t9"})
        hc.terminal = Mock(return_value={"address": "t9", "instance": "t9/1/2", "state": "live"})
        with patch("platform_runtime._help", return_value="[PROMPT] --sandbox --no-daemon --ask-for-approval --add-dir --prompt-interactive --mode"), \
             patch("platform_runtime.model_catalog", return_value=[{"id": "gemini-4-hydrogen", "label": "Gemini 4 Hydrogen", "efforts": ["low", "xhigh"]}]), \
             patch("platform_runtime._node_codex", side_effect=lambda p: [p]):
            hc.launch("codex", cwd=tmp, name="c1", labels={}, bootstrap="hello", workspace_write=True, writable_dirs=(tmp,))
            codex_argv = hc._rpc.call_args_list[-1][0][1]["argv"]
            self.assertIn("--ask-for-approval", codex_argv)
            self.assertIn("on-request", codex_argv)
            self.assertNotIn("never", codex_argv)
            self.assertIn("--add-dir", codex_argv)

            hc.launch("antigravity", cwd=tmp, name="a1", labels={}, bootstrap="hello", model="gemini-4-hydrogen", effort="xhigh", workspace_write=False)
            agy_ro = hc._rpc.call_args_list[-1][0][1]["argv"]
            self.assertIn("--mode", agy_ro)
            self.assertIn("plan", agy_ro)

            hc.launch("antigravity", cwd=tmp, name="a2", labels={}, bootstrap="hello", model="default", effort="high", workspace_write=True, writable_dirs=(tmp,))
            agy_rw = hc._rpc.call_args_list[-1][0][1]["argv"]
            self.assertIn("accept-edits", agy_rw)
            self.assertIn("--add-dir", agy_rw)
            with self.assertRaises(ValueError):
                hc.launch("antigravity", cwd=tmp, name="a3", labels={}, bootstrap="hello", model="missing-model")
        with patch("platform_runtime._help", return_value="[PROMPT] --sandbox --no-daemon --ask-for-approval --add-dir --prompt-interactive --mode"), \
             patch("platform_runtime.model_catalog", return_value=[]):
            hc.launch("antigravity", cwd=tmp, name="a4", labels={}, bootstrap="hello", model="gemini-4-hydrogen", effort="medium")
            self.assertIn("gemini-4-hydrogen", hc._rpc.call_args_list[-1][0][1]["argv"])


@unittest.skipUnless(os.name == "nt", "Windows named pipe and mutex")
class WindowsPlatformTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / ("Herdr 协调器测试 " + uuid.uuid4().hex)

    def test_unicode_payload_and_isolated_channels(self):
        payload = {"text": "\u8d44\u6599" * 100_000}
        member = PipeServer(self.root, lambda body: {"channel": "member", "body": body}).start()
        local = PipeServer(self.root, lambda body: {"channel": "local", "body": body}, channel="local").start()
        try:
            self.assertEqual(request(self.root, payload)["body"], payload)
            self.assertEqual(request(self.root, {}, channel="local")["channel"], "local")
            self.assertFalse(self.root.exists())
        finally:
            local.close()
            member.close()
        self.assertIsNone(member.failure)
        self.assertIsNone(local.failure)

    def test_requests_run_in_parallel_with_root_scoped_singleton(self):
        def competing_owner():
            with service_mutex(self.root, "daemon") as owner:
                return owner

        server = PipeServer(self.root, lambda body: body).start()
        try:
            with service_mutex(self.root, "daemon") as first:
                self.assertTrue(first)
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as threads:
                    self.assertFalse(threads.submit(competing_owner).result())
                    numbers = list(threads.map(lambda n: request(self.root, {"n": n})["n"], range(18)))
                self.assertEqual(numbers, list(range(18)))
        finally:
            server.close()
        self.assertIsNone(server.failure)


if __name__ == "__main__":
    unittest.main()
