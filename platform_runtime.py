"""Windows host integration for the coordinator. No imports start processes or write data."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time


LOG = logging.getLogger(__name__)
APPS = {"codex": ("codex",), "dsh": ("dsh", "dsh-tui", "dst"), "antigravity": ("antigravity", "agy")}
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BREAKAWAY_FROM_JOB = 0x01000000
MAX_FRAME = 1024 * 1024
SAFE_MODEL = re.compile(r"[a-zA-Z0-9._-]{1,120}\Z")
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


def _configured_tools(config):
    return config.get("tools", config) if isinstance(config, dict) else {}


def resolve_tools(config=None):
    """An explicit missing path never falls through to another executable."""
    configured = _configured_tools(config)
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
    candidates = {
        "python": ("python.exe", "python", sys.executable),
        "hc": ("hc.exe", "hc", local / "Programs/herdr-core/hc.exe"),
        "herdr-app": ("herdr-app.exe", "herdr-app", local / "Programs/herdr-app/herdr-app.exe"),
        "codex": ("codex.cmd", "codex.exe", "codex", roaming / "npm/codex.cmd"),
        "dsh": ("dst.exe", "dst.cmd", "dsh.exe", "dsh.cmd", roaming / "npm/dst.cmd"),
        "antigravity": ("agy.exe", "agy", "antigravity.exe", local / "agy/bin/agy.exe"),
    }
    tools = {}
    for key, names in candidates.items():
        explicit = configured.get(key)
        for name in (explicit,) if explicit else names:
            path = Path(os.path.expandvars(str(name))).expanduser()
            found = str(path.resolve()) if path.is_file() else shutil.which(str(path))
            if found:
                tools[key] = str(Path(found).resolve())
                break
        else:
            tools[key] = None
    return tools


def _run(args, *, timeout=8, env=None):
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                          encoding="utf-8", errors="replace", env=env,
                          close_fds=True, creationflags=CREATE_NO_WINDOW)


def _node_codex(executable):
    path = Path(executable)
    if path.suffix.lower() not in (".cmd", ".bat", ".ps1"):
        return [str(path)]
    npm_cli = path.parent / "node_modules/@openai/codex/bin/codex.js"
    node = path.with_name("node.exe")
    node = str(node) if node.is_file() else shutil.which("node.exe")
    if node and npm_cli.is_file() and path.stem.lower() == "codex":
        return [node, str(npm_cli)]
    raise ValueError("Codex 批处理入口无法安全传递初始提示，请配置原生可执行文件或标准 npm 安装。")


def model_catalog(agent, tools=None):
    """Return models advertised by the local installation. Availability still depends on login."""
    if agent == "codex":
        try:
            path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "models_cache.json"
            models = json.loads(path.read_text(encoding="utf-8-sig")).get("models", [])
            return [dict(id=item["slug"], label=item.get("display_name", item["slug"]),
                         efforts=[level["effort"] for level in item.get("supported_reasoning_levels", [])
                                  if level.get("effort") in EFFORTS])
                    for item in models if item.get("visibility") == "list"
                    and SAFE_MODEL.fullmatch(item.get("slug", ""))]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return []
    if agent == "antigravity":
        executable = (tools or resolve_tools()).get(agent)
        if executable and Path(executable).suffix.lower() == ".exe":
            return list(_antigravity_models(executable))
    return []


_AGY_MODELS_CACHE = {}


def _antigravity_models(executable):
    cached = _AGY_MODELS_CACHE.get(executable)
    if cached:
        return cached
    try:
        output = _run([executable, "models"], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if output.returncode:
        return ()
    raw_models = []
    grouped = {}
    effort_order = ("low", "medium", "high", "xhigh", "max")
    for line in output.stdout.splitlines():
        match = re.match(r"^\s*([a-z][\w.-]+-[\w.-]+)(?:\t| {2,})(.+)$", line)
        if not match:
            continue
        slug, label = match[1], match[2].strip()
        raw_models.append(dict(id=slug, label=label, efforts=[]))
        effort_match = re.match(r"^(.+)-(low|medium|high|xhigh|max)$", slug)
        if effort_match:
            base_id, level = effort_match[1], effort_match[2]
            base_label = re.sub(r"\s*\((?:Low|Medium|High|XHigh|Max)\)\s*$", "", label).strip()
            entry = grouped.setdefault(base_id, {"id": base_id, "label": base_label or base_id, "efforts": []})
            if level not in entry["efforts"]:
                entry["efforts"].append(level)
    base_models = []
    for entry in grouped.values():
        entry["efforts"].sort(key=lambda item: effort_order.index(item) if item in effort_order else 99)
        base_models.append(entry)
    seen = {item["id"] for item in base_models}
    result = tuple(base_models + [item for item in raw_models if item["id"] not in seen])
    if result:
        _AGY_MODELS_CACHE[executable] = result
    return result


_antigravity_models.cache_clear = _AGY_MODELS_CACHE.clear


@lru_cache(maxsize=32)
def _help(argv):
    result = _run([*argv, "--help"])
    if result.returncode:
        raise RuntimeError("CLI 能力检查未通过。")
    return result.stdout + result.stderr


def agent_capabilities(config=None):
    tools = resolve_tools(config)
    profiles = {}
    codex_sandbox_ok = bool(os.name == "nt" and _codex_sandbox_sids())
    for agent in APPS:
        executable = tools[agent]
        managed = False
        if executable and agent != "dsh":
            try:
                argv = _node_codex(executable) if agent == "codex" else [executable]
                helptext = _help(tuple(argv))
                required = ("[PROMPT]", "--sandbox", "--no-daemon", "--ask-for-approval") if agent == "codex" else (
                    "--prompt-interactive", "--sandbox")
                managed = all(flag in helptext for flag in required)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                pass
        profiles[agent] = {
            "path": executable, "available": bool(executable), "managed_launch": managed,
            "models": [{"id": "default", "label": "CLI 默认模型", "efforts": []},
                       *model_catalog(agent, tools)],
            "account_isolation_verified": False, "quota_source_verified": False,
            "sandbox_boundary_verified": bool(agent == "codex" and managed and codex_sandbox_ok),
        }
    return profiles


class HcError(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def launch_hidden(args, *, cwd=None, env=None, stdout=None, stderr=None, breakaway=False):
    """Start without a console or inherited handles. A disallowed breakaway is retried once."""
    flags = CREATE_NO_WINDOW | (BREAKAWAY_FROM_JOB if breakaway and os.name == "nt" else 0)
    options = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL if stdout is None else stdout,
                   stderr=subprocess.DEVNULL if stderr is None else stderr, close_fds=True)
    try:
        return subprocess.Popen(args, creationflags=flags, **options)
    except OSError as exc:
        if not breakaway or getattr(exc, "winerror", None) != 5:
            raise
        return subprocess.Popen(args, creationflags=CREATE_NO_WINDOW, **options)


class Hc:
    _confirm_polls = 6
    _confirm_interval = 0.4

    def __init__(self, executable=None, config=None):
        self.tools = resolve_tools(config)
        self.executable = str(executable) if executable else self.tools["hc"]

    def _command(self, args):
        if not self.executable:
            raise HcError("unavailable", "未发现 hc。请配置本机 Herdr Core 可执行文件。")
        return [self.executable, *args]

    def _call(self, *args, timeout=8):
        result = _run(self._command([args[0], "--json", *args[1:]]), timeout=timeout)
        try:
            value = json.loads(result.stdout or result.stderr)
        except ValueError as exc:
            raise HcError("invalid_response", "Core 未返回有效的 JSON 回执。") from exc
        self._check(value, result.returncode)
        return value

    @staticmethod
    def _check(value, exit_code=0):
        if not isinstance(value, dict):
            raise HcError("invalid_response", "Core 未返回有效的 JSON 对象。")
        error = value.get("error") or {}
        if error or exit_code:
            data = error.get("data") or {}
            raise HcError(error.get("kind", data.get("kind", "unknown")),
                          str(error.get("message", "Core 执行失败。"))[:500])

    def _rpc(self, method, params, timeout=12):
        command = self._command(["connect", "--json"])
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, close_fds=True,
                              creationflags=CREATE_NO_WINDOW) as process:
            reply = Queue(maxsize=1)

            def exchange():
                try:
                    envelope = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                    process.stdin.write(json.dumps(envelope, ensure_ascii=False).encode("utf-8") + b"\n")
                    process.stdin.flush()
                    reply.put(process.stdout.readline(MAX_FRAME + 1))
                except OSError as exc:
                    reply.put(exc)

            exchange_thread = threading.Thread(target=exchange, daemon=True)
            exchange_thread.start()
            try:
                line = reply.get(timeout=timeout)
            except Empty as exc:
                process.kill()
                raise HcError("unknown", "Core 请求结果待核实。") from exc
            finally:
                exchange_thread.join(1)
                if not exchange_thread.is_alive():
                    process.stdin.close()
            if isinstance(line, Exception):
                raise HcError("unknown", "Core 连接中断，结果待核实。") from line
            try:
                response = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise HcError("invalid_response", "Core 请求结果待核实。") from exc
            self._check(response)
            if response.get("id") != 1 or "result" not in response:
                raise HcError("invalid_response", "Core 请求结果待核实。")
            return response["result"]

    @staticmethod
    def normalized(raw):
        status = raw.get("status")
        status = status if isinstance(status, dict) else {"state": status}
        app = status.get("app") or raw.get("agent")
        agent = next((key for key, aliases in APPS.items() if app in aliases), app)
        address = raw.get("address")
        created = raw.get("created_ms")
        pid = raw.get("pid")
        instance = f"{address}/{created}/{pid}" if address and isinstance(created, int) and isinstance(pid, int) else None
        return {**raw, "instance": instance, "agent": agent, "agent_status": status,
                "status": status.get("state"), "labels": raw.get("labels") or {}}

    def terminal(self, address):
        try:
            return self.normalized(self._call("get", address))
        except HcError as exc:
            if exc.kind == "not_found":
                return None
            raise

    def terminals(self):
        address_list = self._call("ls").get("terminals", [])
        return [terminal for item in address_list if isinstance(item.get("address"), str)
                and (terminal := self.terminal(item["address"]))]

    snapshot = terminals

    def capabilities(self):
        version = _run(self._command(["--version"])).stdout.strip()
        schema = self._call("schema")
        methods = {method["name"]: method for method in schema.get("methods", [])}
        needed = {"terminal.get", "terminal.run", "terminal.send", "terminal.read", "events.subscribe"}
        missing = sorted(needed - methods.keys())
        if "env" not in {param["name"] for param in methods.get("terminal.run", {}).get("params", [])}:
            missing.append("terminal.run:env")
        return {"version": version, "supported": not missing, "missing": missing,
                "draft_safe_send": False, "delivery_requires_consent": False,
                "python": sys.version.split()[0]}

    def read(self, address, *, lines=0, timeout=8):
        return self._call("read", "--lines", str(max(0, int(lines))), "--", address, timeout=timeout)

    def launch(self, agent, *, cwd, name, labels, bootstrap, environment=None, parent=None,
               no_notify=False, model="default", effort="default", workspace_write=False, writable_dirs=()):
        tool = self.tools.get(agent)
        if not tool or agent not in APPS or agent == "dsh":
            raise ValueError("当前 CLI 无经验证的初始提示启动接口。请在本地绑定已有会话。")
        if not isinstance(bootstrap, str) or not bootstrap.strip() or len(bootstrap) > 16_000:
            raise ValueError("初始提示不能为空且最多 16000 字符。")
        if not isinstance(model, str) or not SAFE_MODEL.fullmatch(model):
            raise ValueError("模型格式无效。")
        models = {item["id"]: item for item in model_catalog(agent, self.tools)} if model != "default" else {}
        if model != "default" and (models or agent != "antigravity") and model not in models:
            raise ValueError("所选模型不在本机 CLI 目录。")
        if effort != "default":
            if effort not in EFFORTS:
                raise ValueError("推理强度无效。")
            if model != "default":
                supported = models.get(model, {}).get("efforts", [])
                if supported and effort not in supported:
                    raise ValueError("所选模型无法核实该推理强度。")
            if agent == "antigravity" and effort not in {"low", "medium", "high", "xhigh", "max"}:
                raise ValueError("Antigravity 不支持该推理强度。")
        env = dict(environment or {})
        allowed_env = {"HERDR_COORD_ROOT", "HERDR_COORD_BINDING", "HERDR_COORD_TOKEN", "HERDR_COORD_JOURNAL"}
        if any(key not in allowed_env
               or not isinstance(value, str) or "\x00" in value or len(value) > 4000
               for key, value in env.items()):
            raise ValueError("无法把此环境变量传入角色会话。")
        if not Path(cwd).is_dir() or not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
            raise ValueError("工作目录或终端名称无效。")
        extra_dirs = []
        for extra in writable_dirs or ():
            resolved = Path(extra).resolve(strict=True)
            if not resolved.is_dir():
                raise ValueError("附加目录必须是已存在的目录。")
            extra_dirs.append(str(resolved))
        if agent == "codex":
            argv = _node_codex(tool)
            available_help = _help(tuple(argv))
            if not all(flag in available_help for flag in ("[PROMPT]", "--sandbox", "--no-daemon", "--ask-for-approval")):
                raise ValueError("本机 Codex 缺少团队启动需要的参数。")
            argv += ["--no-daemon", "-c", "check_for_update_on_startup=false",
                     "--sandbox", "workspace-write" if workspace_write else "read-only",
                     "--ask-for-approval", "on-request"]
            if workspace_write and "--add-dir" in available_help:
                for extra in extra_dirs:
                    argv += ["--add-dir", extra]
            if model != "default":
                argv += ["--model", model]
            if effort != "default":
                argv += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
            argv += ["--", bootstrap]
        else:
            argv = [tool]
            available_help = _help(tuple(argv))
            if not all(flag in available_help for flag in ("--prompt-interactive", "--sandbox")):
                raise ValueError("本机 Antigravity 缺少团队启动需要的参数。")
            argv += ["--sandbox"]
            if "--mode" in available_help:
                argv += ["--mode", "accept-edits" if workspace_write else "plan"]
            if workspace_write and "--add-dir" in available_help:
                for extra in extra_dirs:
                    argv += ["--add-dir", extra]
            if model != "default":
                argv += ["--model", model]
            if effort != "default":
                argv += ["--effort", effort]
            argv += ["--prompt-interactive=" + bootstrap]
        params = {"argv": argv, "cwd": str(Path(cwd).resolve()), "env": env,
                  "name": name, "labels": labels, "no_parent": not parent,
                  "outlive_parent": bool(parent), "no_notify": bool(parent and no_notify)}
        if parent:
            params["parent"] = parent
        result = self._rpc("terminal.run", params)
        if not isinstance(result, dict) or not result.get("address"):
            raise HcError("unknown", "Core 未返回终端编号，需按启动标签核实。")
        found = self.terminal(result["address"])
        if not found or not found["instance"]:
            raise HcError("unknown", "Core 尚未返回可核验的终端实例，需按启动标签核实。")
        return found

    def keys(self, address, *key_names, timeout=15):
        return self._call("keys", "--", address, *key_names, timeout=timeout)

    @staticmethod
    def _prompt_holds_marker(screen, marker):
        """Return True only when the active bottom prompt line holds our unsubmitted marker."""
        if not marker or not isinstance(screen, dict) or not screen.get("complete", True):
            return False
        raw = screen.get("text")
        if not isinstance(raw, str) or not raw.strip():
            return False
        tail = [line.rstrip() for line in raw.splitlines() if line.strip()][-10:]
        tail_lower = "\n".join(tail).lower()
        if any(tok in tail_lower for tok in ("enter select", "enter continue", "enter confirm", "esc back", "esc to cancel")):
            return False
        prompts = [idx for idx, line in enumerate(tail) if line.lstrip().startswith(("›", ">"))]
        if not prompts:
            return False
        last_idx = prompts[-1]
        if any(line.lstrip().startswith(("•", "Ran ")) for line in tail[last_idx + 1:]):
            return False
        compact_prompt = re.sub(r"\s+", "", "".join(tail[last_idx:]))
        return re.sub(r"\s+", "", marker) in compact_prompt

    def _poll_turn_start(self, address, expected, turn_at_send, polls):
        after = None
        for idx in range(max(0, polls) + 1):
            if idx and self._confirm_interval > 0:
                time.sleep(self._confirm_interval)
            try:
                after = self.terminal(address)
            except (OSError, HcError, subprocess.TimeoutExpired):
                after = None
                continue
            if not after or after["instance"] != expected or after["state"] != "live":
                return False, after
            if after.get("status") == "working" or (type(turn_at_send) is int and after.get("turn", -1) > turn_at_send):
                return True, after
        return False, after

    def send(self, address, text, *, expected=None, safe_states=("idle", "done"),
             no_notify=False, timeout=75, consent=False, allow_unreported=False, confirm_marker=None):
        """Make one scoped attempt. Checks SendResult.started and terminal instance."""
        if not consent:
            return {"status": "blocked", "evidence": "投递未获授权，请核对草稿后重试。"}
        if not isinstance(text, str) or not text.strip() or len(text) > 16_000 or "\x00" in text:
            return {"status": "not_sent", "evidence": "投递内容无效。"}
        try:
            before = self.terminal(address)
        except (OSError, HcError, subprocess.TimeoutExpired):
            return {"status": "not_sent", "evidence": "无法核对终端实例。"}
        if not expected or not before or before["instance"] != expected or before["state"] != "live":
            return {"status": "not_sent", "evidence": "目标终端实例已变化。"}
        status = before["status"]
        if status is None:
            if not allow_unreported:
                return {"status": "not_sent", "evidence": "终端未报告工作状态，不能自动投递。"}
        elif status not in safe_states or status not in ("idle", "done", "working"):
            return {"status": "not_sent", "evidence": f"终端状态为 {status}，未处于允许投递状态。"}
        try:
            if status in ("idle", "working"):
                answer = self._call("send", "--if", status, "--", address, text, timeout=timeout)
            else:
                answer = self._call("send", "--", address, text, timeout=timeout)
        except HcError as exc:
            if exc.kind in {"status_mismatch", "blocked", "finished", "not_found"}:
                return {"status": "not_sent", "evidence": exc.kind}
            return {"status": "unknown", "evidence": exc.kind}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"status": "unknown", "evidence": type(exc).__name__}
        if not isinstance(answer, dict):
            return {"status": "unknown", "evidence": "Core 未返回有效的发送回执。"}
        started = answer.get("started")
        turn_at_send = answer.get("turn")
        if started is True:
            try:
                after = self.terminal(address)
            except (OSError, HcError, subprocess.TimeoutExpired):
                after = None
            if not after or after["instance"] != expected or after["state"] != "live":
                return {"status": "unknown", "evidence": {"receipt": answer, "reason": "发送后终端实例已变化或退出。"}}
            if type(turn_at_send) is not int or after.get("turn", -1) < turn_at_send:
                return {"status": "unknown", "evidence": {"receipt": answer, "reason": "发送轮次与当前终端不匹配。"}}
            return {"status": "delivered", "evidence": answer}
        confirmed, after = self._poll_turn_start(address, expected, turn_at_send, self._confirm_polls)
        if confirmed:
            return {"status": "delivered", "evidence": {**answer, "confirmed_status": after.get("status"), "confirmed_turn": after.get("turn")}}
        # If ConPTY bracketed paste left our exact message unsubmitted in the prompt box, press Enter once.
        if started is False and confirm_marker and after and after.get("status") in ("idle", "done"):
            try:
                screen = self.read(address, lines=0)
            except (OSError, HcError, subprocess.TimeoutExpired):
                screen = None
            if self._prompt_holds_marker(screen, confirm_marker):
                try:
                    self.keys(address, "enter")
                except (OSError, HcError, subprocess.TimeoutExpired):
                    pass
                else:
                    confirmed_key, after_key = self._poll_turn_start(address, expected, turn_at_send, max(4, self._confirm_polls))
                    if confirmed_key:
                        return {"status": "delivered", "evidence": {**answer, "confirmed_via": "keys_enter",
                                                                    "confirmed_status": after_key.get("status"),
                                                                    "confirmed_turn": after_key.get("turn")}}
                    after = after_key
        if started is False:
            return {"status": "unknown", "evidence": {**answer, "reason": "消息可能停留在输入框，未观察到 Agent 开始处理。"}}
        if not after or after["instance"] != expected or after["state"] != "live":
            return {"status": "unknown", "evidence": {"receipt": answer, "reason": "发送后终端实例已变化或退出。"}}
        if type(turn_at_send) is not int or after.get("turn", -1) < turn_at_send:
            return {"status": "unknown", "evidence": {"receipt": answer, "reason": "发送轮次与当前终端不匹配。"}}
        if type(before.get("turn")) is int and after.get("turn", -1) > before["turn"]:
            return {"status": "delivered", "evidence": answer}
        return {"status": "unknown", "evidence": {"receipt": answer, "reason": "终端未报告开始处理状态，投递结果待核实。"}}

    def events(self, wake, stop, *, labels=None, terminal=None):
        return EventWatcher(self, wake, stop, labels, terminal)

    def interrupt(self, address, *, expected):
        """Issue one explicitly requested stop key and report observable terminal evidence."""
        try:
            before = self.terminal(address)
        except (OSError, HcError, subprocess.TimeoutExpired):
            return {"status": "not_sent", "evidence": "无法核对打断目标的终端实例。"}
        if not expected or not before or before["instance"] != expected or before["state"] != "live":
            return {"status": "not_sent", "evidence": "打断目标的终端实例已变化。"}
        try:
            outcome = self._call("interrupt", "--timeout", "6", "--", address, timeout=12)
            after = self.terminal(address)
        except (OSError, HcError, subprocess.TimeoutExpired) as exc:
            return {"status": "unknown", "evidence": type(exc).__name__}
        if not after or after["instance"] != expected:
            return {"status": "unknown", "evidence": {"receipt": outcome, "reason": "终端实例已变化。"}}
        return {"status": "observed" if outcome.get("outcome") in ("stopped", "exited") else "unknown",
                "evidence": outcome}


class EventWatcher:
    def __init__(self, hc, wake, stop, labels, terminal):
        args = ["events", "--json"]
        for key, value in (labels or {}).items():
            args.extend(("--label", f"{key}={value}"))
        if terminal:
            args.extend(("--terminal", terminal))
        self.stop = stop
        self.closed = threading.Event()
        self.process = None
        self.thread = threading.Thread(target=self._watch, args=(hc, args, wake), daemon=True)
        self.thread.start()

    def _watch(self, hc, args, wake):
        while not (self.stop.is_set() or self.closed.is_set()):
            try:
                with launch_hidden(hc._command(args), stdout=subprocess.PIPE) as process:
                    self.process = process
                    if self.stop.is_set() or self.closed.is_set():
                        process.terminate()
                    for line in process.stdout:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(event, dict) or event.get("type", event.get("method")) != "heartbeat":
                            wake.set()
                        if self.stop.is_set() or self.closed.is_set():
                            process.terminate()
                            break
            except (OSError, HcError):
                pass
            finally:
                self.process = None
            self.closed.wait(3)

    def close(self):
        self.closed.set()
        process = self.process
        if process is not None:
            try:
                process.terminate()
            except OSError:
                pass
        self.thread.join(timeout=4)


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


@lru_cache(maxsize=1)
def _kernel():
    if os.name != "nt":
        raise OSError("此入口需要 Windows 命名管道。")
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.GetCurrentProcess.argtypes = []
    dll.GetCurrentProcess.restype = wintypes.HANDLE
    dll.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.POINTER(_SecurityAttributes)]
    dll.CreateNamedPipeW.restype = wintypes.HANDLE
    dll.CreateMutexW.argtypes = [ctypes.POINTER(_SecurityAttributes), wintypes.BOOL, wintypes.LPCWSTR]
    dll.CreateMutexW.restype = wintypes.HANDLE
    dll.ReleaseMutex.argtypes = [wintypes.HANDLE]
    dll.ReleaseMutex.restype = wintypes.BOOL
    dll.LocalFree.argtypes = [ctypes.c_void_p]
    dll.LocalFree.restype = ctypes.c_void_p
    dll.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    dll.IsProcessInJob.restype = wintypes.BOOL
    return dll


@lru_cache(maxsize=1)
def _advapi():
    dll = ctypes.WinDLL("advapi32", use_last_error=True)
    dll.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    dll.OpenProcessToken.restype = wintypes.BOOL
    dll.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    dll.GetTokenInformation.restype = wintypes.BOOL
    dll.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    dll.ConvertSidToStringSidW.restype = wintypes.BOOL
    dll.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                                        ctypes.POINTER(ctypes.c_void_p),
                                                                        ctypes.POINTER(wintypes.DWORD)]
    dll.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    dll.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                              ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    dll.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    dll.GetSecurityDescriptorSacl.argtypes = dll.GetSecurityDescriptorDacl.argtypes
    dll.GetSecurityDescriptorSacl.restype = wintypes.BOOL
    dll.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_uint, wintypes.DWORD, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    dll.SetNamedSecurityInfoW.restype = wintypes.DWORD
    dll.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_uint, wintypes.DWORD,
                                          ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(ctypes.c_void_p)]
    dll.GetNamedSecurityInfoW.restype = wintypes.DWORD
    dll.LookupAccountNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                       ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                       ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    dll.LookupAccountNameW.restype = wintypes.BOOL
    return dll


@lru_cache(maxsize=8)
def _lookup_account_sid(name):
    if os.name != "nt":
        return None
    api = _advapi()
    sid_size = wintypes.DWORD(0)
    dom_size = wintypes.DWORD(0)
    use = wintypes.DWORD(0)
    api.LookupAccountNameW(None, name, None, ctypes.byref(sid_size), None, ctypes.byref(dom_size), ctypes.byref(use))
    if not sid_size.value:
        return None
    sid_buf = ctypes.create_string_buffer(sid_size.value)
    dom_buf = ctypes.create_unicode_buffer(max(1, dom_size.value))
    if not api.LookupAccountNameW(None, name, sid_buf, ctypes.byref(sid_size), dom_buf, ctypes.byref(dom_size), ctypes.byref(use)):
        return None
    sid_str = wintypes.LPWSTR()
    if not api.ConvertSidToStringSidW(sid_buf, ctypes.byref(sid_str)):
        return None
    try:
        return sid_str.value
    finally:
        _kernel().LocalFree(sid_str)


@lru_cache(maxsize=1)
def _codex_sandbox_sids():
    sids = []
    for account in ("CodexSandboxUsers", "CodexSandboxOffline", "CodexSandboxOnline"):
        sid = _lookup_account_sid(account)
        if sid and sid not in sids:
            sids.append(sid)
    return tuple(sids)


def _root_owner_sid(root):
    if os.name != "nt":
        return _user_sid()
    path = Path(root).expanduser().resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    api = _advapi()
    sid_ptr = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    error = api.GetNamedSecurityInfoW(str(path), 1, 1, ctypes.byref(sid_ptr), None, None, None, ctypes.byref(descriptor))
    if error or not sid_ptr.value:
        return _user_sid()
    try:
        sid = wintypes.LPWSTR()
        if not api.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid)):
            return _user_sid()
        try:
            return sid.value
        finally:
            _kernel().LocalFree(sid)
    finally:
        _kernel().LocalFree(descriptor)


@lru_cache(maxsize=1)
def _user_sid():
    import _winapi

    api = _advapi()
    token = wintypes.HANDLE()
    if not api.OpenProcessToken(_kernel().GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        api.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        data = ctypes.create_string_buffer(size.value)
        if not api.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_ptr = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        sid = wintypes.LPWSTR()
        if not api.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return sid.value
        finally:
            _kernel().LocalFree(sid)
    finally:
        _winapi.CloseHandle(token.value)


def _identity_key(root, *, channel="local"):
    normalized = os.path.normcase(str(Path(root).expanduser().resolve()))
    sid = _root_owner_sid(root) if channel == "member" else _user_sid()
    return hashlib.sha256(f"{sid}\0{normalized}".encode("utf-8")).hexdigest()[:28]


def protect_control_directory(root):
    """Keep state and SQLite sidecars above low-integrity member processes."""
    root = Path(root).resolve()
    control = root / "control"
    try:
        info = control.lstat()
    except FileNotFoundError:
        pass
    else:
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("协调器的 control 目录不能是重解析路径。")
    control.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        return control
    api = _advapi()
    # Control data includes role secrets. Deny read-up as well as write-up, and
    # have WAL/SHM files and any future control state inherit the same label.
    sddl = f"D:P(A;OICI;FA;;;{_user_sid()})S:(ML;OICI;NWNR;;;ME)"
    descriptor = ctypes.c_void_p()
    if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        sacl = ctypes.c_void_p()
        if not api.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present:
            raise ctypes.WinError(ctypes.get_last_error())
        if not api.GetSecurityDescriptorSacl(descriptor, ctypes.byref(present), ctypes.byref(sacl), ctypes.byref(defaulted)) or not present:
            raise ctypes.WinError(ctypes.get_last_error())
        # SE_FILE_OBJECT, protected DACL, DACL and mandatory label information.
        error = api.SetNamedSecurityInfoW(str(control), 1, 0x80000014, None, None, dacl, sacl)
        if error:
            raise ctypes.WinError(error)
    finally:
        _kernel().LocalFree(descriptor)
    sandbox_sids = _codex_sandbox_sids()
    if sandbox_sids:
        # Grant non-inheritable read/execute on the root directory itself so
        # sandboxed Codex members can query root owner SID for member pipe routing
        # without gaining access to requests/ or control/ subdirectories.
        subprocess.run(
            ["icacls", str(root), "/grant", f"*{sandbox_sids[0]}:(RX)"],
            check=False,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    return control


@contextmanager
def _security(*, low_integrity=False):
    # The subscriber's sandbox still has to allow this named pipe. The low-integrity
    # label and sandbox SIDs only apply to the member IPC endpoint, never to local/state.
    label = "LW" if low_integrity else "ME"
    aces = [f"(A;;GA;;;{_user_sid()})"]
    if low_integrity:
        for sid in _codex_sandbox_sids():
            aces.append(f"(A;;GA;;;{sid})")
    sddl = f"D:P{''.join(aces)}S:(ML;;NW;;;{label})"
    descriptor = ctypes.c_void_p()
    if not _advapi().ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
    finally:
        _kernel().LocalFree(descriptor)


@contextmanager
def service_mutex(root, role):
    """Yield True exactly when this process owns the per-user, per-data-root mutex."""
    import _winapi

    name = f"Global\\HerdrTeamCoordinator-{_identity_key(root, channel='local')}-{role}"
    with _security() as attributes:
        mutex = _kernel().CreateMutexW(ctypes.byref(attributes), False, name)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    owned = _winapi.WaitForSingleObject(mutex, 0) in (0, 0x80)  # WAIT_OBJECT_0, WAIT_ABANDONED
    try:
        yield owned
    finally:
        if owned:
            _kernel().ReleaseMutex(mutex)
        _winapi.CloseHandle(mutex)


def in_job():
    result = wintypes.BOOL()
    if _kernel().IsProcessInJob(_kernel().GetCurrentProcess(), None, ctypes.byref(result)):
        return bool(result.value)
    return None


def _pipe_name(root, channel):
    if channel not in {"member", "local", "supervisor"}:
        raise ValueError("未知协调器通道。")
    return rf"\\.\pipe\HerdrTeamCoordinator-{_identity_key(root, channel=channel)}-{channel}"


def _new_pipe(name, *, first, member, instances):
    import _winapi

    flags = _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED
    if first:
        flags |= _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE
    with _security(low_integrity=member) as attrs:
        # Byte mode, blocking underlying pipe, overlapped operations, reject remote clients.
        handle = _kernel().CreateNamedPipeW(name, flags, 0x00000008, instances, 65536,
                                            65536, 1000, ctypes.byref(attrs))
        if handle == _winapi.INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _finish_io(operation, deadline, stop=None):
    import _winapi

    waited = False
    while time.monotonic() < deadline and (not stop or not stop.is_set()):
        wait_ms = max(1, int(min(200, (deadline - time.monotonic()) * 1000)))
        if _winapi.WaitForSingleObject(operation.event, wait_ms) == 0:
            waited = True
            break
    if not waited:
        operation.cancel()
    count, error = operation.GetOverlappedResult(True)
    if not waited and error:
        raise TimeoutError("等待协调器命名管道超时。")
    if error:
        raise ctypes.WinError(error)
    return count


def _read(pipe, size, deadline, stop=None):
    import _winapi

    result = bytearray()
    while len(result) < size:
        operation, error = _winapi.ReadFile(pipe, min(size - len(result), 65536), overlapped=True)
        count = _finish_io(operation, deadline, stop)
        if not count:
            raise EOFError("命名管道读入结束。")
        result.extend(operation.getbuffer()[:count])
    return result


def _write(pipe, payload, deadline, stop=None):
    import _winapi

    view = memoryview(payload)
    while view:
        operation, error = _winapi.WriteFile(pipe, view[:65536], overlapped=True)
        count = _finish_io(operation, deadline, stop)
        if not count:
            raise EOFError("命名管道写入结束。")
        view = view[count:]


def _receive(pipe, deadline, stop=None):
    size, = struct.unpack("<I", _read(pipe, 4, deadline, stop))
    if not 0 < size <= MAX_FRAME:
        raise ValueError("协调器消息长度无效。")
    value = json.loads(_read(pipe, size, deadline, stop))
    if not isinstance(value, dict):
        raise ValueError("协调器消息必须是 JSON 对象。")
    return value


def _send(pipe, payload, deadline, stop=None):
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if not 0 < len(body) <= MAX_FRAME:
        raise ValueError("协调器消息超过 1 MiB。")
    _write(pipe, struct.pack("<I", len(body)) + body, deadline, stop)


class PipeServer:
    def __init__(self, data_root, handler, *, channel="member", workers=32):
        self.name = _pipe_name(data_root, channel)
        self.handler = handler
        self.channel = channel
        self.workers = workers
        self.worker_slots = threading.Semaphore(workers)
        self.stop = threading.Event()
        self.acceptor = None
        self.connections = set()
        self.connections_lock = threading.Lock()
        self.failure = None

    def start(self):
        if self.acceptor is not None:
            raise RuntimeError("管道监听器只能启动一次。")
        first = self._new(first=True)
        self.acceptor = threading.Thread(target=self._accept, args=(first,), daemon=True)
        self.acceptor.start()
        return self

    def _new(self, first=False):
        return _new_pipe(self.name, first=first, member=self.channel == "member", instances=min(254, self.workers + 2))

    def _accept(self, pipe):
        import _winapi

        try:
            while not self.stop.is_set():
                try:
                    operation = _winapi.ConnectNamedPipe(pipe, overlapped=True)
                    if operation is not None:
                        _finish_io(operation, float("inf"), self.stop)
                except OSError as exc:
                    if getattr(exc, "winerror", None) not in (232, 233):
                        raise
                    self._disconnect(pipe)
                    continue
                if self.stop.is_set():
                    break
                acquired = self.worker_slots.acquire(timeout=2.0)
                if not acquired:
                    try:
                        _receive(pipe, time.monotonic() + 1, self.stop)
                        _send(pipe, {"ok": False, "error": "协调器当前并发繁忙，请稍后重试。"}, time.monotonic() + 1)
                    except (ValueError, EOFError, OSError):
                        pass
                    self._disconnect(pipe)
                    continue
                with self.connections_lock:
                    ready = self._new()
                    worker = threading.Thread(target=self._handle, args=(pipe,), daemon=True)
                    self.connections.add(worker)
                    try:
                        worker.start()
                    except RuntimeError:
                        self.connections.discard(worker)
                        self.worker_slots.release()
                        _winapi.CloseHandle(ready)
                        raise
                pipe = ready
        except (OSError, RuntimeError) as exc:
            if not self.stop.is_set():
                self.failure = exc
                LOG.exception("协调器的 %s 管道监听中断", self.channel)
        finally:
            _winapi.CloseHandle(pipe)

    @staticmethod
    def _disconnect(pipe):
        import _winapi

        try:
            _winapi.DisconnectNamedPipe(pipe)
        except OSError as exc:
            if getattr(exc, "winerror", None) != 233:
                raise

    def _handle(self, pipe):
        import _winapi

        try:
            payload = _receive(pipe, time.monotonic() + 5, self.stop)
            try:
                answer = self.handler(payload)
            except Exception:
                LOG.exception("协调器的 %s 请求处理失败", self.channel)
                answer = {"ok": False, "error": "协调器处理失败，请查看后台状态。"}
            # Wait briefly for the client's ACK before closing, so bytes are not discarded.
            _send(pipe, answer, time.monotonic() + 5)
            _read(pipe, 1, time.monotonic() + 2)
        except (ValueError, EOFError, OSError):
            pass
        finally:
            _winapi.CloseHandle(pipe)
            with self.connections_lock:
                self.connections.discard(threading.current_thread())
            self.worker_slots.release()

    def close(self):
        self.stop.set()
        if self.acceptor is not None:
            self.acceptor.join(2)
        with self.connections_lock:
            connections = list(self.connections)
        deadline = time.monotonic() + 8
        for worker in connections:
            worker.join(max(0, deadline - time.monotonic()))


def request(data_root, payload, *, channel="member", timeout=4, connect_timeout=None):
    import _winapi

    name = _pipe_name(data_root, channel)
    deadline = time.monotonic() + timeout
    connect_deadline = deadline if connect_timeout is None else min(deadline, time.monotonic() + connect_timeout)
    while True:
        try:
            # FILE_READ_DATA + FILE_WRITE_DATA, with security identification level.
            pipe = _winapi.CreateFile(name, 0x0001 | 0x0002, 0, _winapi.NULL, _winapi.OPEN_EXISTING,
                                      _winapi.FILE_FLAG_OVERLAPPED | 0x00110000, _winapi.NULL)
            break
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (2, 231) or time.monotonic() >= connect_deadline:
                raise
            time.sleep(min(0.08, max(0, connect_deadline - time.monotonic())))
    try:
        _send(pipe, payload, deadline)
        answer = _receive(pipe, deadline)
        try:
            _write(pipe, b"\x06", deadline)
        except OSError:
            pass
        return answer
    finally:
        _winapi.CloseHandle(pipe)


_NOTIFICATION_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Information
$icon.Visible = $true
$icon.Text = 'Herdr Team Coordinator'
$icon.add_BalloonTipClicked({
  if ($env:HERDR_COORD_NOTIFY_APP) {
    Start-Process -FilePath $env:HERDR_COORD_NOTIFY_APP -ArgumentList @('do','mod.open','--name','herdr-team-coordinator') -WindowStyle Hidden
  }
  [System.Windows.Forms.Application]::Exit()
})
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 25000
$timer.add_Tick({ [System.Windows.Forms.Application]::Exit() })
try {
  $icon.ShowBalloonTip(15000, $env:HERDR_COORD_NOTIFY_TITLE, $env:HERDR_COORD_NOTIFY_BODY, 'Warning')
  $timer.Start()
  [System.Windows.Forms.Application]::Run()
} finally {
  $timer.Dispose()
  $icon.Dispose()
}
"""


def notify(title, body, *, app=None):
    """Return whether the native notification helper could start, without inferring visibility."""
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return False
    env = {**os.environ, "HERDR_COORD_NOTIFY_TITLE": title[:63],
           "HERDR_COORD_NOTIFY_BODY": (body[:150] + "\n恢复：herdr-app do mod.open --name herdr-team-coordinator"),
           "HERDR_COORD_NOTIFY_APP": str(app or resolve_tools()["herdr-app"] or "")}
    encoded = base64.b64encode(_NOTIFICATION_SCRIPT.encode("utf-16-le")).decode("ascii")
    try:
        launch_hidden([powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
                       "-EncodedCommand", encoded], env=env)
        return True
    except OSError:
        return False
