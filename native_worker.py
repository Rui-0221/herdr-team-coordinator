"""Run the installed Windows engines using their native turn interfaces."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from common import DomainError
from config_store import MODEL
import host_native
from platform_runtime import request
from role_observability import evidence_path
from windows_isolation import execute, grant, identity

HERE = Path(__file__).resolve().parent
ENV_BY_PROVIDER = {'codex': ('OPENAI_API_KEY',), 'dsh': ('DEEPSEEK_API_KEY',), 'antigravity': ()}
PROTOCOL_ACTIONS = frozenset(('status', 'history', 'requests', 'issue', 'dispatch', 'cancel-task',
    'pause', 'resume', 'change-goal', 'resolve-revision', 'review-start', 'ask', 'answer',
    'say', 'amend-task', 'ack-task', 'accept', 'blocked', 'request-write', 'yield', 'handoff', 'review-submit'))


def dsh_model_override(model, effort):
    selection = {}
    if model != 'default':
        provider, _, name = model.partition('/')
        if not provider or not name or not MODEL.fullmatch(model) or not provider[0].isalnum():
            raise ValueError('DSH 模型需使用 provider/model 格式')
        selection.update(provider=provider, model=name)
    if effort != 'default':
        selection['reasoningEffort'] = effort
    return selection


def engine_arguments(provider, mode, *, workspace_write=False, model='default', effort='default'):
    if mode not in ('approval', 'full_auto'):
        raise ValueError('执行权限模式无效')
    full = mode == 'full_auto'
    if provider == 'codex':
        argv = ['codex', 'exec', '--json', '--skip-git-repo-check', '--ignore-user-config',
                '--color', 'never']
        if full:
            argv.append('--dangerously-bypass-approvals-and-sandbox')
        else:
            argv += ['--sandbox', 'workspace-write' if workspace_write else 'read-only',
                     '-c', 'approval_policy="on-request"']
        if model != 'default':
            argv += ['--model', model]
        if effort != 'default':
            argv += ['-c', 'model_reasoning_effort=' + json.dumps(effort)]
        return argv + ['-']
    if provider == 'dsh':
        dsh_model_override(model, effort)
        return ['dsh', '--profile', 'headless', '--json', '--permission-mode',
                'danger-full-access' if full else 'workspace-write',
                '--approval-policy', 'never' if full else 'ask', '-']
    if provider == 'antigravity':
        return ['agy', 'acp', '--include-default-customizations=false',
                '--dangerously-skip-permissions' if full else '--sandbox']
    raise ValueError('未知原生引擎')


def installed_engine(provider, executable):
    if not executable:
        raise DomainError(f'未找到本机 {provider} CLI')
    path = Path(executable)
    if provider == 'codex' and path.suffix.lower() != '.exe':
        packages = path.parent / 'node_modules/@openai/codex/node_modules'
        candidates = list(packages.glob('@openai/codex-win32-x64/vendor/*/bin/codex.exe'))
        if len(candidates) == 1:
            return [str(candidates[0])], [candidates[0].parent.parent]
    if provider == 'dsh':
        if path.stem.lower() == 'dsh-tui':
            local = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local')
            path = local / 'dsh-cli/dsh.cmd'
        bundled_node = path.with_name('node.exe')
        node = str(bundled_node) if bundled_node.is_file() else shutil.which('node.exe')
        script = path.parent / 'node_modules/@deepseek-ai/dsh/lib/bin.js'
        if path.is_file() and node and script.is_file():
            return [node, str(script)], [Path(node).parent, path.parent / 'node_modules']
        raise DomainError('请配置独立 Node 版 dsh-tui.cmd 或 dsh-cli/dsh.cmd')
    if path.suffix.lower() == '.exe':
        return [str(path)], [path.parent]
    raise DomainError(f'无法验证 {provider} 的 Windows 原生可执行入口')


def copy_runtime(source, target):
    # There are often tens of thousands of npm files. Inherit the package policy
    # as each file is created. The copy excludes junction traversal and pruning.
    copied = subprocess.run(['robocopy', str(source), str(target), '/E', '/XJ', '/SL', '/MT:24',
                             '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NP'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if copied.returncode >= 8:
        raise OSError(f'无法准备 Windows 运行时，robocopy 退出码 {copied.returncode}')


def prepare(root, config, binding):
    provider = config['provider']
    prefix, vendors = installed_engine(provider, config['executable'])
    argv = engine_arguments(provider, config['mode'], workspace_write=config['write'],
                            model=config['model'], effort=config['effort'])
    if provider in ('dsh', 'antigravity'):
        selection = dsh_model_override(config['model'], config['effort']) if provider == 'dsh' else None
        return host_native.prepare(root, config, binding, prefix, vendors, argv, copy_runtime, selection)
    package = identity('HerdrTeamCoordinator.' + binding)
    directory = Path(config['journal']) / 'native'
    runtime = directory / 'runtime'
    private = directory / 'home'
    for path in (runtime, private):
        path.mkdir(parents=True, exist_ok=True)
    grant(directory, package)
    grant(private, package, write=True)
    source = Path(sys.base_prefix)
    for part in (Path(sys.executable), *source.glob('python*.dll'), *source.glob('vcruntime*.dll')):
        shutil.copy2(part, runtime / part.name)
    for name in ('DLLs', 'Lib'):
        shutil.copytree(source / name, runtime / name, ignore=shutil.ignore_patterns(
            'site-packages', 'test', '__pycache__', 'tkinter', 'idlelib'), dirs_exist_ok=True)
    shutil.copy2(HERE / 'native_guest.py', runtime / 'coord_runtime.py')
    engine_root = runtime / 'engine'
    engine_root.mkdir(exist_ok=True)
    copy_runtime(vendors[0], engine_root)
    prefix = [str(engine_root / Path(prefix[0]).relative_to(vendors[0]))]
    grant(config['project'], package, write=config['write'])
    system = Path(os.environ['SystemRoot'])
    env = {
        'SystemRoot': str(system), 'WINDIR': str(system), 'ComSpec': str(system / 'System32/cmd.exe'),
        'PATH': os.pathsep.join([str(runtime), str(engine_root), str(system / 'System32'),
                                str(system / 'System32/WindowsPowerShell/v1.0')]),
        'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
        'HERDR_COORD_PROGRAM': str(runtime / 'coord_runtime.py'),
    }
    env.update({key: str(private) for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP')})
    return package, runtime, env, [*prefix, *argv[1:]]


def report(provider, state, evidence=''):
    clean = ''.join(ch for ch in evidence if ch.isprintable())[:500]
    encoded = base64.b64encode(clean.encode('utf-8')).decode('ascii')
    print(f'\x1b]7501;state={state}:app={provider}:msg={encoded}\x1b\\', flush=True)
    if clean:
        print(clean, flush=True)


def run_turn(root, auth, config, isolated, turn):
    if config['provider'] in ('dsh', 'antigravity'):
        return host_native.run_turn(root, auth, config, isolated, turn)
    package, runtime, env, engine = isolated
    env = {**env, **{key: turn['credentials'][key] for key in ENV_BY_PROVIDER[config['provider']]
                    if key in turn['credentials']}}
    executable = str(runtime / Path(sys.executable).name)
    argv = [executable, '-B', '-S', str(runtime / 'coord_runtime.py'), 'worker', config['provider']]
    guest_input, host_output = os.pipe()
    host_input, guest_output = os.pipe()
    outcome = []
    fault = []

    def launch():
        with os.fdopen(guest_input, 'rb') as stdin, os.fdopen(guest_output, 'wb') as stdout, open(os.devnull, 'wb') as stderr:
            try:
                outcome.append(execute(argv, package=package, cwd=config['project'], environment=env,
                                       stdin=stdin, stdout=stdout, stderr=stderr))
            except (OSError, subprocess.SubprocessError) as error:
                fault.append(type(error).__name__)

    runner = threading.Thread(target=launch, daemon=True)
    runner.start()
    receipt = {}
    trace = evidence_path(root / 'control', auth['binding'], turn['id'], turn['attempt'])
    trace.parent.mkdir(parents=True, exist_ok=True)
    with (os.fdopen(host_output, 'w', encoding='utf-8', newline='\n') as sink,
          os.fdopen(host_input, 'r', encoding='utf-8') as source,
          trace.with_suffix('.stdout.jsonl').open('w', encoding='utf-8') as stdout,
          trace.with_suffix('.stderr').open('w', encoding='utf-8') as stderr):
        sink.write(json.dumps({'argv': engine, 'prompt': turn['prompt']}, ensure_ascii=False) + '\n')
        sink.flush()
        for line in source:
            event = json.loads(line)
            if event.get('type') == 'member':
                args = {'action': 'native-relay', 'auth': auth, 'params': event['payload']}
                try:
                    response = request(root, args, channel='member', timeout=45)
                except (OSError, EOFError) as error:
                    response = {'ok': False, 'error': type(error).__name__}
                sink.write(json.dumps(response, ensure_ascii=False) + '\n')
                sink.flush()
            elif event.get('type') == 'result':
                receipt = event
            elif event.get('type') == 'engine-output' and event.get('stream') in ('stdout', 'stderr'):
                stream = stdout if event['stream'] == 'stdout' else stderr
                stream.write(event['line'])
                stream.flush()
    runner.join()
    return bool(outcome == [0] and receipt.get('ok')), str(
        receipt.get('detail') or (fault[0] if fault else 'AppContainer exit ' + str(outcome)))[:500]


def drive(root, auth, config):
    provider = config['provider']
    report(provider, 'working', '准备 Windows 受管运行时')
    try:
        isolated = prepare(root, config, auth['binding'])
    except (DomainError, OSError, ValueError, subprocess.SubprocessError) as error:
        detail = ('无法启动 Windows 隔离角色：' + str(error))[:500]
        try:
            request(root, {'action': 'native-failed', 'auth': auth, 'params': {'evidence': detail}},
                    channel='member', timeout=6)
        except (OSError, EOFError):
            pass
        report(provider, 'error', detail)
        return
    report(provider, 'idle')
    while True:
        try:
            answer = request(root, {'action': 'native-next', 'auth': auth, 'params': {}}, channel='member', timeout=6)
        except (OSError, EOFError) as error:
            report(provider, 'blocked', f'协调器连接中断：{type(error).__name__}')
            time.sleep(2)
            continue
        if not answer['ok']:
            report(provider, 'blocked', answer['error'])
            time.sleep(2)
            continue
        turn = answer['result']
        if turn.get('closed'):
            report(provider, 'done', '协调器已结束此角色。')
            return
        if not turn.get('id'):
            time.sleep(1)
            continue
        report(provider, 'working', f'原生引擎回合 {turn["id"][:12]}')
        try:
            ok, detail = run_turn(root, auth, config, isolated, turn)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            ok, detail = False, type(error).__name__
        try:
            acknowledgement = request(root, {'action': 'native-finish', 'auth': auth, 'params': {
                'id': turn['id'], 'attempt': turn['attempt'], 'ok': ok, 'evidence': detail}},
                channel='member', timeout=6)
        except (OSError, EOFError):
            report(provider, 'blocked', '回合状态提交失败，请核对协调器回执。')
            continue
        if not acknowledgement['ok']:
            report(provider, 'blocked', acknowledgement['error'])
            continue
        report(provider, 'done' if ok else 'error', detail)
        if not ok:
            return


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    root = Path(os.environ['HERDR_COORD_ROOT'])
    credentials = {'binding': os.environ['HERDR_COORD_BINDING'], 'token': os.environ['HERDR_COORD_TOKEN'],
                   'terminal': os.environ['HC_TERMINAL_ID']}
    drive(root, credentials, json.loads(os.environ['HERDR_COORD_NATIVE']))
