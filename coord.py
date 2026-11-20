"""Herdr 团队协调器。运行 coord help 查看协作命令。"""

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from common import DomainError, data_root, digest, new_id, wire
from platform_runtime import PipeServer, request, service_mutex
from supervisor import start, status, stop


TASK_ACTIONS = ('accept', 'blocked', 'request-write', 'yield', 'handoff', 'review-submit')
TEAM_ACTIONS = ('dispatch', 'cancel-task', 'pause', 'resume', 'change-goal', 'resolve-revision', 'review-start', 'ask', 'answer')
FORMATS = '''
在角色绑定的终端中使用接入指令。以下以 coord 代指接入指令。

  coord status                           查询本轮工作、待办与交接
  coord requests                         查询请求编号及入库结果
  coord accept --task ID --ticket 凭据   使用本轮 status 返回的接单参数
  coord blocked --reason "依赖与证据" [--depends-on-role 角色] [--depends-on-task ID] [--depends-on-resource worktree|refs]
  coord request-write [--path 目录] [--repository-refs]
  coord yield --report "已完成和待完成的工作" --json '{"evidence": {...}}'
  coord handoff --report "成果与验证证据" --paths 文件1 文件2 --json '{"evidence": {...}}'
  coord review-submit --review-id ID --verdict pass --report "核对证据"

主控操作
  coord dispatch --role developer --title "任务" --instructions "范围" --completion "完成条件" --mode read
  coord cancel-task --task-id ID --reason "撤回原因"
  coord pause --reason "暂停原因" [--emergency]
  coord resume [--task-id ID] [--json '{"evidence": {...}}']
  coord change-goal --goal "新目标"
  coord resolve-revision --task-id ID --decision reuse --carried "沿用内容"
  coord review-start --role-id reviewer --paths 文件或目录
  coord ask --purpose integration --text "是否开始联调？"
  coord ask --purpose delete --text "删除确认说明" --json '{"scope":{"target":"对象","extent":"范围","impact":"影响"}}'
  coord answer --question-id ID --response yes

已接单时，助手可在角色凭据目录记录本轮 ID 和票据。只读终端在动作中附上 --task 和 --ticket。
高风险授权（delete / permission / provider）须通过 --json 传入 scope 的 target、extent、impact，并在本地恢复页点击确认。
写入交回证据：{"stopped":true,"terminal":"终端现场","processes":"后台进程","files":"改动","handoff":"交接凭据","external":"外部操作凭据"}。
共享路径和写入资格见 status；只有取得资格才能写入。独立本地授权使用 Herdr 恢复页。
请求中断后运行 coord requests，重试可带 --request-id。每次正常调用都会生成新请求编号；--new-request 强制跳过未完成暂存。
'''


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value.get('ok', True) else 1


def serve(root):
    from coordinator import Coordinator

    with service_mutex(root, 'daemon') as owned:
        if not owned:
            return 0
        app = Coordinator(root)
        servers = []
        try:
            for channel in ('member', 'local'):
                server = PipeServer(root, lambda item, role=channel: app.handle(item, role), channel=channel)
                servers.append(server.start())

            def check_pipes():
                while not app.stop.wait(1):
                    if any(server.failure for server in servers):
                        app.stop.set()
                        app.wake.set()
                        break

            threading.Thread(target=check_pipes, daemon=True, name='coord-listeners').start()
            # Production always uses five minutes. A short idle limit is useful for isolated release exercises.
            idle = 300
            if os.environ.get('HERDR_COORD_TEST_IDLE') and root != data_root():
                idle = max(1, int(os.environ['HERDR_COORD_TEST_IDLE']))
            app.run(idle)
            return int(any(server.failure for server in servers))
        finally:
            for server in reversed(servers):
                server.close()


def call(root, message, channel, *, timeout=8):
    try:
        return request(root, message, channel=channel, timeout=timeout, connect_timeout=0.25)
    except (FileNotFoundError, TimeoutError) as original:
        # The command may have reached the server. Only retry transport after an
        # independent status probe confirms the service never opened its listener.
        if not isinstance(original, FileNotFoundError) or status(root)['running']:
            raise
    try:
        start(root)
    except OSError as error:
        raise DomainError('当前环境无法唤醒协调器', '从本地恢复页启动后台，再核对原请求编号') from error
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            opened = request(root, {'action': 'service-status'}, channel='local', timeout=0.25)
            if opened.get('ok'):
                return request(root, message, channel=channel, timeout=timeout)
        except FileNotFoundError:
            time.sleep(0.2)
        except TimeoutError:
            time.sleep(0.2)
    raise DomainError('后台启动尚未就绪', '查看本地协调器的后台状态与 service.log')


def decode_json(raw):
    value = json.loads(raw) if raw else {}
    if not isinstance(value, dict):
        raise DomainError('--json 需要 JSON 对象')
    return value


def local_action(root, action, params):
    if action == 'status':
        return {'ok': True, 'result': status(root)}
    if action == 'start_daemon':
        return {'ok': True, 'result': start(root)}
    if action in ('stop', 'restart'):
        result = stop(root)
        return {'ok': result['stopped'], 'result': {**result, **start(root)} if action == 'restart' and result['stopped'] else result,
                **({'error': result.get('reason', '后台仍在停止')} if not result['stopped'] else {})}
    return call(root, {'action': action, 'params': params}, 'local', timeout=120 if action in
                ('decide', 'approve_delivery', 'save_config', 'start', 'recover_write',
                 'rebind_lead', 'rebind_member', 'bootstrap') else 35)


def journal_read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def journal_new(journal, fingerprint, fresh):
    if journal is None:
        return None, None
    identifier = new_id()
    target = journal / (f'pending-{fingerprint}-{identifier}.json' if fresh else f'pending-{fingerprint}.json')
    if not fresh:
        existing = journal_read(target).get('id')
        if existing:
            return existing, target
    try:
        with target.open('x', encoding='utf-8') as handle:
            handle.write(wire({'id': identifier, 'fingerprint': fingerprint}))
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        existing = journal_read(target).get('id')
        return (existing, target) if existing else (None, None)
    except OSError:
        return None, None
    return identifier, target


def member_action(args, root, params):
    terminal = os.environ.get('HC_TERMINAL_ID')
    binding = args.binding or os.environ.get('HERDR_COORD_BINDING')
    token = args.token or os.environ.get('HERDR_COORD_TOKEN')
    if not terminal or not binding or not token:
        raise DomainError('没有找到当前终端的协调器角色凭据', '从 Herdr 恢复页复制本角色的接入指令，并在该终端执行')
    raw_journal = args.journal or os.environ.get('HERDR_COORD_JOURNAL')
    expected = (root / 'requests' / binding).resolve()
    journal = expected if raw_journal and Path(raw_journal).resolve() == expected else None
    auth = {'terminal': terminal, 'binding': binding, 'token': token}
    action = args.action
    if action in ('status', 'requests'):
        answer = call(root, {'action': action, 'auth': auth}, 'member')
        if action == 'status' and journal and answer.get('ok'):
            selected = journal_read(journal / 'selected.json')
            if selected.get('task'):
                live_ids = {item['id'] for item in answer.get('result', {}).get('candidates', [])
                            if item.get('state') in ('offered', 'accepted', 'blocked', 'yielded')}
                if selected['task'] not in live_ids:
                    try:
                        (journal / 'selected.json').unlink(missing_ok=True)
                    except OSError:
                        pass
        return answer
    if action in TASK_ACTIONS and not (params.get('task') or params.get('ticket')) and journal and action != 'accept':
        selected = journal_read(journal / 'selected.json')
        if selected.get('task') and selected.get('ticket'):
            params = {**params, 'task': selected['task'], 'ticket': selected['ticket']}
    pending_path = None
    if args.request_id:
        identifier = args.request_id
    else:
        identifier, pending_path = journal_new(journal, digest([action, params]), args.new_request)
    if identifier is None:
        issued = call(root, {'action': 'issue', 'auth': auth,
                              'params': {'action': action, 'params': params, 'new_request': args.new_request}}, 'member')
        if not issued.get('ok'):
            return issued
        identifier = issued['result']['request_id']
    try:
        answer = call(root, {'action': action, 'params': params, 'auth': auth, 'request_id': identifier}, 'member', timeout=45)
    except (OSError, EOFError) as error:
        raise DomainError(f'请求 {identifier} 的返回结果待核实', f'运行 coord requests 查询，重试带 --request-id {identifier}') from error
    if pending_path is not None:
        try:
            pending_path.unlink(missing_ok=True)
        except OSError:
            pass
    if action == 'accept' and journal and answer.get('ok'):
        try:
            with (journal / 'selected.json').open('w', encoding='utf-8') as handle:
                handle.write(wire({'task': params['task'], 'ticket': params['ticket']}))
        except OSError:
            pass
    elif action in ('handoff', 'review-submit') and journal and answer.get('ok'):
        try:
            (journal / 'selected.json').unlink(missing_ok=True)
        except OSError:
            pass
    return {**answer, 'request_id': identifier}


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    for flag in ('root', 'binding', 'token', 'journal'):
        cli.add_argument('--' + flag)
    actions = cli.add_subparsers(dest='action')
    actions.add_parser('help')
    actions.add_parser('serve').add_argument('--root', default=argparse.SUPPRESS)
    local = actions.add_parser('local')
    local.add_argument('local_action')
    local.add_argument('--root', default=argparse.SUPPRESS)
    local.add_argument('--json')
    for action in ('status', 'requests', *TASK_ACTIONS, *TEAM_ACTIONS):
        command = actions.add_parser(action)
        command.add_argument('--json')
        command.add_argument('--request-id')
        command.add_argument('--new-request', action='store_true')
        options = {'dispatch': ('role', 'title', 'instructions', 'completion', 'mode'),
                   'blocked': ('reason', 'depends_on_role', 'depends_on_task', 'depends_on_resource'),
                   'request-write': ('path',), 'yield': ('report',), 'handoff': ('report',),
                   'review-submit': ('review_id', 'verdict', 'report'), 'pause': ('reason',), 'cancel-task': ('task_id', 'reason'),
                   'resume': ('task_id',), 'change-goal': ('goal',),
                   'resolve-revision': ('task_id', 'decision', 'carried', 'instructions', 'role_id'),
                   'review-start': ('role_id',), 'ask': ('purpose', 'text', 'replace'),
                   'answer': ('question_id', 'response')}.get(action, ())
        for option in (*options, *(('task', 'ticket') if action in TASK_ACTIONS else ())):
            command.add_argument('--' + option.replace('_', '-'))
        if action in ('handoff', 'review-start'):
            command.add_argument('--paths', nargs='+')
        for switch in {'dispatch': ('optional',), 'pause': ('emergency',), 'request-write': ('repository_refs',)}.get(action, ()):
            command.add_argument('--' + switch.replace('_', '-'), action='store_true', default=None)
    return cli


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = parser().parse_args()
    if args.action in (None, 'help'):
        print(FORMATS)
        return 0
    root = Path(args.root).expanduser().resolve() if args.root else data_root()
    if args.action == 'serve':
        return serve(root)
    try:
        params = decode_json(args.json)
        if args.action == 'local':
            return output(local_action(root, args.local_action, params))
        protocol = {'root', 'token', 'binding', 'journal', 'action', 'json', 'request_id', 'new_request', 'optional'}
        params.update({key: value for key, value in vars(args).items() if key not in protocol and value is not None})
        if getattr(args, 'optional', False):
            params['required'] = False
        return output(member_action(args, root, params))
    except (DomainError, ValueError, OSError, EOFError) as error:
        return output({'ok': False, 'error': str(error), 'next': getattr(error, 'next_step', '查看协调器状态后核对请求')})


if __name__ == '__main__':
    raise SystemExit(main())
