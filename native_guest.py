"""Public role client and JSON stdin adapter for the Windows AppContainer."""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
from urllib.request import ProxyHandler, Request, build_opener
import uuid


def client():
    parser = argparse.ArgumentParser(prog='coord')
    parser.add_argument('action')
    parser.add_argument('--json', default='{}')
    parser.add_argument('--request-id')
    flags, remaining = parser.parse_known_args()
    data = sys.stdin.read() if flags.json == '-' else flags.json
    params = json.loads(data)
    if not isinstance(params, dict):
        parser.error('expected a JSON object')
    while remaining:
        name = remaining.pop(0)
        if not name.startswith('--'):
            parser.error('expected --key value')
        values = []
        while remaining and not remaining[0].startswith('--'):
            values.append(remaining.pop(0))
        key = name[2:].replace('-', '_')
        params[key] = (values if key == 'paths' else int(values[0]) if key in ('version', 'before') else
                       values[0] if len(values) == 1 else True if not values else ' '.join(values))
    payload = {'action': flags.action, 'params': params, 'request_id': flags.request_id or uuid.uuid4().hex}
    req = Request(os.environ['HERDR_COORD_ENDPOINT'] + '/member', method='POST',
                  data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json',
                      'Authorization': os.environ['HERDR_COORD_ACCESS']})
    with build_opener(ProxyHandler({})).open(req, timeout=30) as response:
        result = json.load(response)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


def native_result(provider, result):
    events = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
        except ValueError:
            continue
    if 'Authentication required' in result.stderr or 'not authenticated' in result.stderr.casefold():
        return False, '当前 Windows 运行身份无法使用引擎登录'
    if provider == 'antigravity':
        final = next((event.get('result', {}) for event in reversed(events) if event.get('event') == 'result'), {})
        return result.returncode == 0 and final.get('status') == 'SUCCESS', final.get('status', 'missing result')
    if provider == 'codex':
        complete = any(event.get('type') == 'turn.completed' for event in events)
        return result.returncode == 0 and complete, 'turn.completed' if complete else 'missing turn.completed'
    if result.returncode != 0 and not events:
        denied = re.search(r'^Error: EPERM: operation not permitted, (lstat|realpath)\b',
                           result.stderr, re.MULTILINE)
        if denied:
            return False, f'Windows 运行时路径解析被拒绝: EPERM {denied[1]}'
    final = next((event for event in reversed(events) if event.get('type') == 'final'), {})
    reason = final.get('reason') or {}
    return result.returncode == 0 and reason.get('kind') == 'completed', (
        reason.get('kind') or ('missing final' if events else 'no JSONL events'))


def worker(provider):
    body = json.loads(sys.stdin.readline())
    access = secrets.token_hex(32)
    relay = threading.Lock()
    output = threading.Lock()

    def emit(event):
        with output:
            print(json.dumps(event), flush=True)

    class MemberEndpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if (self.path != '/member' or self.headers.get('Authorization') != access
                        or not 0 < size <= 1024 * 1024):
                    self.send_error(403)
                    return
                payload = json.loads(self.rfile.read(size))
                with relay:
                    emit({'type': 'member', 'payload': payload})
                    response = sys.stdin.readline().encode('utf-8')
                if not response:
                    self.send_error(503)
                    return
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

    server = ThreadingHTTPServer(('127.0.0.1', 0), MemberEndpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    environment = {**os.environ, 'HERDR_COORD_ENDPOINT': f'http://127.0.0.1:{server.server_port}',
                   'HERDR_COORD_ACCESS': access}
    text = body['prompt']
    if provider == 'antigravity':
        text = json.dumps({'event': 'user', 'message': {'content': text}}, ensure_ascii=False) + '\n'
    try:
        with subprocess.Popen(body['argv'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding='utf-8', errors='replace', env=environment) as engine:
            collected = {'stdout': [], 'stderr': []}

            def forward(stream, name):
                for line in stream:
                    bucket = collected[name]
                    if len(bucket) == 64:
                        bucket.pop(0)
                    bucket.append(line)
                    emit({'type': 'engine-output', 'stream': name, 'line': line})

            readers = [threading.Thread(target=forward, args=(stream, name), daemon=True)
                       for name, stream in [('stdout', engine.stdout), ('stderr',engine.stderr)]]
            for reader in readers:
                reader.start()
            try:
                with engine.stdin:
                    engine.stdin.write(text)
            except BrokenPipeError:
                pass
            try:
                code = engine.wait(timeout=850)
            except subprocess.TimeoutExpired:
                engine.kill()
                engine.wait()
                raise
            for reader in readers:
                reader.join()
        result = subprocess.CompletedProcess(body['argv'], code, ''.join(collected['stdout']), ''.join(collected['stderr']))
        ok, summary = native_result(provider, result)
        emit({'type': 'result', 'ok': ok, 'detail': f'{provider} {summary} (exit {result.returncode})'})
    except (subprocess.SubprocessError, OSError) as error:
        emit({'type': 'result', 'ok': False, 'detail': f'{provider} {type(error).__name__}'})
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(worker(sys.argv[2]) if sys.argv[1:2] == ['worker'] else client())
