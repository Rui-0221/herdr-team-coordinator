"""Launch, monitor, and orderly stop the per-root Windows coordinator service."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time

from platform_runtime import PipeServer, in_job, launch_hidden, request, service_mutex


HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = Path.home() / "HerdrTeamCoordinator"
LOG = logging.getLogger(__name__)
RETRY_DELAYS = (1, 2, 4, 8, 16)
HEALTHY_SECONDS = 60
STOP_TIMEOUT = 100


def _query(root, action, channel, timeout=0.35):
    try:
        value = request(root, {"action": action}, channel=channel, timeout=timeout)
    except (OSError, EOFError, ValueError):
        return None
    if value.get("ok") and isinstance(value.get("result"), dict):
        return value["result"]
    return None


def _existing_daemon(root):
    return _query(root, "service-status", "local", 0.25)


def status(root):
    supervisor = _query(root, "status", "supervisor")
    daemon = _existing_daemon(root)
    return {"running": daemon is not None, "supervisor": supervisor is not None,
            "pid": daemon.get("pid") if daemon else None,
            "supervisor_pid": supervisor.get("pid") if supervisor else None,
            "guarded_by_job": supervisor.get("guarded_by_job") if supervisor else None,
            "phase": supervisor.get("phase") if supervisor else "detached" if daemon else "stopped",
            "protocol": daemon.get("protocol") if daemon else None}


def start(root):
    if _query(root, "status", "supervisor", 0.15):
        return {"started": False, **status(root)}
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUTF8": "1"}
    # Members can request through IPC, but a service process must not inherit a role credential.
    for key in ("HERDR_COORD_BINDING", "HERDR_COORD_TOKEN", "HERDR_COORD_JOURNAL", "HC_TERMINAL_ID"):
        env.pop(key, None)
    with (root / "service.log").open("ab", buffering=0) as log:
        process = launch_hidden([sys.executable, "-B", str(HERE / "supervisor.py"), "watch",
                                 "--root", str(root)], cwd=HERE, env=env, stdout=log, stderr=log,
                                breakaway=True)
    deadline = time.monotonic() + 3.0
    outcome = status(root)
    while time.monotonic() < deadline and not outcome["running"] and process.poll() is None:
        time.sleep(0.1)
        outcome = status(root)
    return {"started": process.poll() is None or outcome["running"], **outcome}


def stop(root):
    deadline = time.monotonic() + STOP_TIMEOUT
    accepted = _query(root, "stop", "supervisor", 0.5)
    if accepted is None:
        daemon = _existing_daemon(root)
        if daemon and _query(root, "service-stop", "local", 3) is None:
            return {"stopped": False, "reason": "后台未确认有序停机。", **status(root)}
    while time.monotonic() < deadline:
        current = status(root)
        if not (current["running"] or current["supervisor"]):
            return {"stopped": True, **current}
        time.sleep(0.2)
    return {"stopped": False, "reason": "后台仍在核对未结束的操作。", **status(root)}


class Supervisor:
    def __init__(self, root):
        self.root = root
        self.stop_requested = threading.Event()
        self.phase = "starting"
        self.guarded_by_job = in_job()

    def command(self, payload):
        action = payload.get("action")
        if action == "stop":
            self.stop_requested.set()
        elif action != "status":
            return {"ok": False, "error": "未知监督进程操作。"}
        return {"ok": True, "result": {"pid": os.getpid(), "phase": self.phase,
                                       "guarded_by_job": self.guarded_by_job}}

    def _stop_child(self):
        self.phase = "stopping"
        return _query(self.root, "service-stop", "local", 1) is not None

    def _adopt(self):
        import _winapi

        daemon = _existing_daemon(self.root)
        if not daemon or type(daemon.get("pid")) is not int or daemon["pid"] <= 0:
            return None
        try:
            # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
            handle = _winapi.OpenProcess(0x00101000, False, daemon["pid"])
        except OSError:
            return None
        self.phase = "adopted"
        stop_sent = False
        try:
            while _winapi.WaitForSingleObject(handle, 500) == 258:  # WAIT_TIMEOUT
                if self.stop_requested.is_set() and not stop_sent:
                    stop_sent = self._stop_child()
            return _winapi.GetExitCodeProcess(handle)
        finally:
            _winapi.CloseHandle(handle)

    def run(self):
        listener = PipeServer(self.root, self.command, channel="supervisor").start()
        try:
            adopted = self._adopt()
            if adopted == 0 or self.stop_requested.is_set():
                return 0
            return self._watch(listener)
        finally:
            listener.close()

    def _watch(self, listener):
        failures = 0
        while not self.stop_requested.is_set():
            if listener.failure:
                return 1
            self.phase = "starting"
            beginning = time.monotonic()
            try:
                process = launch_hidden([sys.executable, "-B", str(HERE / "coord.py"), "serve",
                                         "--root", str(self.root)], cwd=HERE,
                                        stdout=sys.stdout, stderr=sys.stderr)
            except OSError:
                LOG.exception("无法启动调度器")
                code = 1
            else:
                stop_sent = False
                self.phase = "running"
                while process.poll() is None:
                    if listener.failure:
                        return 1  # Leave a running daemon available for the next supervisor to adopt.
                    if self.stop_requested.is_set() and not stop_sent:
                        stop_sent = self._stop_child()
                    time.sleep(0.25)
                code = process.returncode
            if code == 0 or self.stop_requested.is_set():
                return 0
            if time.monotonic() - beginning >= HEALTHY_SECONDS:
                failures = 0
            if failures == len(RETRY_DELAYS):
                LOG.error("调度器多次异常退出。下次打开界面或调用助手时可再次启动。")
                return 1
            delay = RETRY_DELAYS[failures]
            failures += 1
            self.phase = "retrying"
            LOG.warning("调度器退出码 %s，将在 %s 秒后重启。", code, delay)
            self.stop_requested.wait(delay)
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "start", "stop", "restart", "watch"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.action == "watch":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        with service_mutex(root, "supervisor") as owned:
            return Supervisor(root).run() if owned else 0
    if args.action == "status":
        result = status(root)
    elif args.action == "start":
        result = start(root)
    else:
        result = stop(root)
        if args.action == "restart" and result["stopped"]:
            result = {**result, **start(root)}
    print(json.dumps(result, ensure_ascii=False))
    return int(result.get("stopped") is False)


if __name__ == "__main__":
    sys.exit(main())
