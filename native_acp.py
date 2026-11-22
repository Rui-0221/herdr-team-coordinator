"""Submit one Antigravity ACP turn through the real CLI's stdio transport."""

import json
import os
import subprocess
import threading

from windows_isolation import execute


class AcpError(RuntimeError):
    pass


class Connection:
    def __init__(self, sink, source, evidence, *, full_auto):
        self.sink, self.source, self.evidence = sink, source, evidence
        self.full_auto = full_auto
        self.serial = 0

    def send(self, payload):
        self.sink.write(json.dumps({'jsonrpc': '2.0', **payload}, ensure_ascii=False) + '\n')
        self.sink.flush()

    def call(self, method, params):
        self.serial += 1
        self.send({'id': self.serial, 'method': method, 'params': params})
        for line in self.source:
            self.evidence.write(line)
            self.evidence.flush()
            try:
                message = json.loads(line)
            except ValueError:
                continue  # Preserve vendor diagnostics outside the ACP stream.
            if not isinstance(message, dict):
                continue
            if 'id' in message and 'method' in message:
                self.receive_request(message)
            elif message.get('id') == self.serial:
                if 'error' in message:
                    raise AcpError(f'{method}: {message["error"].get("message", "engine error")}')
                return message.get('result') or {}
        raise AcpError(f'ACP 回合缺少 {method} 回执')

    def receive_request(self, message):
        if message['method'] != 'session/request_permission':
            self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Host capability unavailable'}})
            return
        options = message.get('params', {}).get('options', ())
        allowed = next((item for item in options if item.get('kind') == 'allow_once'), None)
        outcome = ({'outcome': 'selected', 'optionId': allowed['optionId']} if self.full_auto and allowed
                   else {'outcome': 'cancelled'})
        self.send({'id': message['id'], 'result': {'outcome': outcome}})

    def prompt(self, config, prompt):
        initialized = self.call('initialize', {'protocolVersion': 1, 'clientCapabilities': {},
                                'clientInfo': {'name': 'herdr-team-coordinator', 'version': '1'}})
        if initialized.get('protocolVersion') != 1:
            raise AcpError('Antigravity ACP 版本不受支持')
        session = self.call('session/new', {'cwd': config['project'], 'mcpServers': []})
        identifier = session.get('sessionId')
        if not identifier:
            raise AcpError('Antigravity ACP 未返回会话 ID')
        if not self.full_auto:
            self.call('session/set_mode', {'sessionId': identifier,
                      'modeId': 'accept-edits' if config['write'] else 'plan'})
        for key, category in [('model', 'model'), ('effort', 'thought_level')]:
            value = config[key]
            if value == 'default':
                continue
            option = next((row for row in session.get('configOptions', []) if row.get('category') == category), None)
            if not option:
                raise AcpError(f'Antigravity ACP 未声明 {key} 配置')
            self.call('session/set_config_option', {'sessionId': identifier, 'configId': option['id'], 'value': value})
        return self.call('session/prompt', {'sessionId': identifier,
                          'prompt': [{'type': 'text', 'text': prompt}]})


def run(argv, config, env, prompt, stdout_path, stderr_path, observe):
    guest_input, host_output = os.pipe()
    host_input, guest_output = os.pipe()
    exit_codes = []
    errors = []

    def launch():
        with os.fdopen(guest_input, 'rb') as stdin, os.fdopen(guest_output, 'wb') as stdout, stderr_path.open('wb') as stderr:
            try:
                exit_codes.append(execute(argv, package=None, cwd=config['project'], environment=env, stdin=stdin,
                                         stdout=stdout, stderr=stderr, timeout=850, on_start=observe))
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(type(error).__name__)

    worker = threading.Thread(target=launch)
    worker.start()
    receipt = {}
    try:
        with os.fdopen(host_output, 'w', encoding='utf-8', newline='\n') as sink, os.fdopen(host_input, 'r', encoding='utf-8') as source, stdout_path.open('w', encoding='utf-8') as evidence:
            connection = Connection(sink, source, evidence, full_auto=config['mode'] == 'full_auto')
            receipt = connection.prompt(config, prompt)
    except (OSError, AcpError) as error:
        errors.append(str(error))
    finally:
        worker.join()
    reason = receipt.get('stopReason')
    if reason == 'end_turn' and exit_codes == [0] and not errors:
        return True, 'antigravity ACP end_turn (exit 0)'
    return False, f'antigravity ACP {reason or ", ".join(errors) or "missing result"} (exit {exit_codes})'
