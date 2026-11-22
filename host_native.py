"""Run host-installed Windows engines at low integrity with scoped coordinator IPC."""

from functools import partial
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading

from common import DomainError
import native_acp
from native_guest import native_result
from platform_runtime import request
from windows_isolation import execute, protect_from_workers

HERE = Path(__file__).resolve().parent


def protected_paths(root):
    home = Path.home()
    production = home / 'HerdrTeamCoordinator'
    installed = home / '.config/herdr-app/mods/herdr-team-coordinator'
    paths = [HERE, installed]
    for state_root in {Path(root), production}:
        paths.extend(state_root / item for item in ('control', 'requests', 'service.log'))
    return tuple(dict.fromkeys(path for path in paths if path.exists()))


def allow_workspace_writes(path):
    subprocess.run(['icacls.exe', str(path), '/setintegritylevel', '(OI)(CI)L'], check=True,
                   capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)


def engine_environment(runtime, private):
    keys = ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'OS', 'SYSTEMDRIVE', 'USERPROFILE', 'HOME', 'APPDATA',
            'LOCALAPPDATA', 'HOMEDRIVE', 'HOMEPATH', 'USERNAME', 'USERDOMAIN', 'PROGRAMFILES',
            'PROGRAMFILES(X86)', 'PROGRAMW6432', 'PROGRAMDATA', 'PATHEXT', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY')
    env = {key: os.environ[key] for key in keys if key in os.environ}
    system = Path(os.environ['SystemRoot'])
    directories = [Path(sys.executable).parent]
    git = shutil.which('git.exe')
    if git:
        git_bin = Path(git).parent.parent / 'bin'
        if (git_bin / 'bash.exe').is_file():
            directories.append(git_bin)
        directories.append(Path(git).parent)
    directories += [system / 'System32', system / 'System32/WindowsPowerShell/v1.0']
    for tool in ('node.exe', 'pwsh.exe'):
        path = shutil.which(tool)
        if path:
            directories.append(Path(path).parent)
    env.update(PATH=os.pathsep.join(map(str, dict.fromkeys(directories))), TEMP=str(private), TMP=str(private),
               PYTHONUTF8='1', PYTHONIOENCODING='utf-8', HERDR_COORD_PROGRAM=str(runtime / 'coord_runtime.py'))
    env.setdefault('PATHEXT', '.COM;.EXE;.BAT;.CMD')
    return env


def prepare(root, config, binding, prefix, vendors, args, copy_runtime, selection):
    for path in protected_paths(root):
        protect_from_workers(path)
    runtime = Path(root) / 'worker-runtime' / binding
    private = runtime / 'state'
    evidence = Path(root) / 'control' / 'native-evidence' / binding
    private.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE / 'native_guest.py', runtime / 'coord_runtime.py')
    env = engine_environment(runtime, private)
    if config['provider'] == 'dsh':
        if len(prefix) != 2:
            raise DomainError('Windows 受管 DSH 需要本机 Node 版 dsh CLI')
        # Keep node.exe on its installed path. A read-only snapshot of the installed
        # JS dependency tree avoids volume and AppData loader restrictions.
        modules = runtime / 'node_modules'
        copy_runtime(vendors[-1], modules)
        prefix = [prefix[0], str(modules / '@deepseek-ai/dsh/lib/bin.js')]
        env['DSH_HOME'] = str(private)
        user_home = Path(os.environ.get('DSH_HOME') or Path.home() / '.dsh').resolve()
        credentials = user_home / '.credentials.yaml'
        profile = user_home / 'profiles' / 'dsh-tui' / 'cordis.patch.yml'
        overrides = ['--patch', str(profile)] if profile.is_file() else []
        auth_patch = runtime / 'stored-auth.yml'
        overlay = [{'id': 'credentials', 'disabled': False, 'config': {'path': str(credentials)}},
                   {'id': 'authorization', 'disabled': False}, {'id': 'deepseek-account', 'disabled': False}]
        if selection:
            overlay.append({'id': 'agent-default-model', 'config': selection})
        auth_patch.write_text(json.dumps(overlay, ensure_ascii=False, indent=2), encoding='utf-8')
        args = [*args[:3], *overrides, '--patch', str(auth_patch), *args[3:]]
    else:
        args = [*args, '--gemini-dir', str(private), '--log-file', str(private / 'agy.log')]
    allow_workspace_writes(private)
    if config['write']:
        allow_workspace_writes(config['project'])
    return runtime, evidence, env, [*prefix, *args[1:]]


class MemberHandler(BaseHTTPRequestHandler):
    def __init__(self, access, relay, *args):
        self.access, self.relay = access, relay
        super().__init__(*args)

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            if (self.path != '/member' or not compare_digest(self.headers.get('Authorization', ''), self.access)
                    or not 0 < length <= 1024 * 1024):
                self.send_error(403)
                return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                self.send_error(400)
                return
            answer = self.relay(payload)
            response = json.dumps(answer, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        except (OSError, ValueError):
            self.close_connection = True

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass


def run_turn(root, auth, config, prepared, turn):
    runtime, evidence, env, argv = prepared
    access = secrets.token_hex(32)

    def relay(payload):
        msg = {'action': 'native-relay', 'auth': auth, 'params': payload}
        try:
            return request(root, msg, channel='member', timeout=25)
        except (OSError, EOFError) as error:
            return {'ok': False, 'error': type(error).__name__}

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(MemberHandler, access, relay))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        env = {**env, 'HERDR_COORD_ENDPOINT': f'http://127.0.0.1:{server.server_port}',
               'HERDR_COORD_ACCESS': access}
        prompt = turn['prompt']
        trace = evidence / (turn['id'] + '-' + turn['attempt'])
        prompt_path = trace.with_suffix('.stdin')
        stdout_path = trace.with_suffix('.stdout.jsonl')
        stderr_path = trace.with_suffix('.stderr')
        prompt_path.write_text(prompt, encoding='utf-8')

        def observe(pid):
            trace.with_suffix('.process.json').write_text(json.dumps(
                {'pid': pid, 'argv': argv, 'provider': config['provider'], 'execution_mode': config['mode'],
                 'launcher': config.get('executable'), 'cwd': config['project']}, ensure_ascii=False), encoding='utf-8')

        if config['provider'] == 'antigravity':
            return native_acp.run(argv, config, env, prompt, stdout_path, stderr_path, observe)

        with prompt_path.open('rb') as stdin, stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
            status = execute(argv, package=None, cwd=config['project'], environment=env, stdin=stdin,
                             stdout=stdout, stderr=stderr, timeout=850, on_start=observe)
        result = subprocess.CompletedProcess(argv, status, stdout_path.read_text(encoding='utf-8', errors='replace'),
                                             stderr_path.read_text(encoding='utf-8', errors='replace'))
        ok, summary = native_result(config['provider'], result)
        return ok, f'{config["provider"]} {summary} (exit {status})'
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()
