"""Worktree identity and bounded, scoped review snapshots."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time


class ScopeError(ValueError):
    pass


@dataclass(frozen=True)
class Worktree:
    root: Path
    common: Path | None = None


def path_key(path):
    return os.path.normcase(str(path))


def overlaps(left, right):
    left, right = Path(path_key(left)), Path(path_key(right))
    return left.is_relative_to(right) or right.is_relative_to(left)


def _git(directory, *args, missing_code=None, deadline=None):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', LC_ALL='C')
    timeout = 12 if deadline is None else min(12, deadline - time.monotonic())
    if timeout <= 0:
        raise ScopeError('Git 状态核对超过时间预算。')
    try:
        proc = subprocess.run(
            ['git', '-C', str(directory), '-c', 'core.fsmonitor=false', *args],
            capture_output=True, timeout=timeout, env=env,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScopeError(f'无法读取 Git 状态：{exc}') from exc
    if missing_code is not None and proc.returncode == missing_code:
        return None
    if proc.returncode:
        raise ScopeError(proc.stderr.decode('utf-8', 'replace').strip() or 'Git 无法读取当前工作树。')
    if len(proc.stdout) > 16 * 1024 * 1024:
        raise ScopeError('Git 变更清单超过 16 MiB。')
    return proc.stdout


def identify_worktree(directory, *, deadline=None):
    try:
        directory = Path(directory).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ScopeError('工作目录不存在或无法解析。') from exc
    if not directory.is_dir():
        raise ScopeError('工作路径必须是目录。')
    try:
        top = _git(directory, 'rev-parse', '--show-toplevel', deadline=deadline)
    except ScopeError as exc:
        if any((path / '.git').exists() for path in (directory, *directory.parents)):
            raise
        if 'not a git repository' in str(exc) or isinstance(exc.__cause__, FileNotFoundError):
            return Worktree(directory)
        raise
    git_root = Path(os.fsdecode(top.removesuffix(b'\n').removesuffix(b'\r'))).resolve(strict=True)
    common = _git(git_root, 'rev-parse', '--path-format=absolute', '--git-common-dir', deadline=deadline)
    return Worktree(git_root, Path(os.fsdecode(common.removesuffix(b'\n').removesuffix(b'\r'))).resolve(strict=True))


def _relative(root, value):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ScopeError('交付路径必须是非空文本。')
    raw = Path(value)
    if '..' in raw.parts or (raw.drive and not raw.is_absolute()):
        raise ScopeError(f'交付路径不能使用相对跳转：{value}')
    try:
        relative = (raw if raw.is_absolute() else root / raw).relative_to(root)
    except ValueError as exc:
        raise ScopeError(f'交付路径位于项目范围之外：{value}') from exc
    if any(part.lower() == '.git' or (os.name == 'nt' and ':' in part) for part in relative.parts):
        raise ScopeError(f'交付路径包含 Git 内部目录或不支持的文件名：{value}')
    return relative.as_posix()


def _assert_inside(root, path, *, include_leaf):
    candidate = path if include_leaf else path.parent
    if path == root:
        candidate = root
    try:
        candidate.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ScopeError(f'交付路径经链接离开项目范围：{path}') from exc


def normalize_scope(project, paths):
    root = Path(project).resolve(strict=True)
    if not isinstance(paths, list) or not paths or len(paths) > 1024:
        raise ScopeError('请声明 1 到 1024 条交付路径。')
    result = set()
    for value in paths:
        relative = _relative(root, value)
        target = root / relative
        _assert_inside(root, target, include_leaf=target.is_dir())
        result.add(relative)
    return sorted(result, key=path_key)


def scope_covers(project, scope, required):
    root = Path(project).resolve()
    target = Path(path_key(_relative(root, required)))
    for entry in scope:
        candidate = Path(path_key(entry))
        if target == candidate or (root / entry).is_dir() and target.is_relative_to(candidate):
            return True
    return False


def _parse_diff(data):
    fields = iter(data.rstrip(b'\0').split(b'\0') if data else ())
    for status in fields:
        paths = [os.fsdecode(next(fields))]
        if status.startswith((b'R', b'C')):
            paths.append(os.fsdecode(next(fields)))
        yield status.decode('ascii'), paths


def _index_entries(data):
    entries = {}
    for record in data.split(b'\0'):
        if record:
            metadata, path = record.split(b'\t', 1)
            entries.setdefault(os.fsdecode(path), []).append(metadata.decode('ascii'))
    return entries


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def _stat_fields(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _hash_file(root, name, remaining, deadline):
    lexical = root / name
    _assert_inside(root, lexical, include_leaf=False)
    try:
        info = lexical.lstat()
    except FileNotFoundError:
        return {'kind': 'missing'}, remaining, None
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(lexical)
        if _stat_fields(info) != _stat_fields(lexical.lstat()):
            raise ScopeError('读取期间链接发生变化。')
        return {'kind': 'symlink', 'digest': _digest(target)}, remaining, _stat_fields(info)
    if not stat.S_ISREG(info.st_mode):
        raise ScopeError('交付对象不是普通文件或符号链接。')
    if info.st_size > remaining:
        raise ScopeError('文件内容超过剩余处理预算。')
    _assert_inside(root, lexical, include_leaf=True)
    digest = hashlib.sha256()
    consumed = 0
    with lexical.open('rb') as stream:
        opened = _stat_fields(os.fstat(stream.fileno()))
        # Windows exposes different ctime semantics for path and handle queries.
        if _stat_fields(info)[:-1] != opened[:-1]:
            raise ScopeError('打开文件时交付对象发生变化。')
        while chunk := stream.read(1024 * 1024):
            consumed += len(chunk)
            if consumed > remaining or time.monotonic() >= deadline:
                raise ScopeError('文件读取超过处理预算。')
            digest.update(chunk)
        if opened != _stat_fields(os.fstat(stream.fileno())) or _stat_fields(info) != _stat_fields(lexical.lstat()):
            raise ScopeError('读取期间文件发生变化。')
    state = {'kind': 'file', 'size': consumed, 'mode': stat.S_IMODE(info.st_mode), 'digest': digest.hexdigest()}
    return state, remaining - consumed, _stat_fields(info)


def _unchanged(root, name, stamp):
    path = root / name
    _assert_inside(root, path, include_leaf=False)
    try:
        return _stat_fields(path.lstat()) == stamp
    except FileNotFoundError:
        return stamp is None


def _git_chunks(tree_root, base_args, pathspecs, *, deadline):
    if not pathspecs:
        yield _git(tree_root, *base_args, '--', deadline=deadline)
        return
    for index in range(0, len(pathspecs), 150):
        chunk = pathspecs[index:index + 150]
        yield _git(tree_root, *base_args, '--', *chunk, deadline=deadline)


def snapshot(project, paths, *, byte_budget=256 * 1024 * 1024, time_budget=30):
    """Return the content fingerprint and explicit reasons for incomplete verification."""
    deadline = time.monotonic() + time_budget
    tree = identify_worktree(project, deadline=deadline)
    project = Path(project).resolve(strict=True)
    scope = normalize_scope(project, paths)
    directories = {name for name in scope if (project / name).is_dir() and not (project / name).is_symlink()}
    explicit = set(scope) - directories
    names = {(project / name).relative_to(tree.root).as_posix() for name in explicit}
    filters = [Path(path_key((project / name).relative_to(tree.root).as_posix())) for name in directories]
    explicit_keys = {path_key(name) for name in names}
    rel_scope = sorted({(project / name).relative_to(tree.root).as_posix() for name in scope}, key=path_key)
    pathspecs = [] if '.' in rel_scope else rel_scope
    manifest = {'scope': scope, 'head': None, 'staged': [], 'unstaged': [], 'untracked': [], 'files': {}, 'unverified': []}
    if tree.common:
        def in_scope(name):
            return path_key(name) in explicit_keys or any(Path(path_key(name)).is_relative_to(base) for base in filters)

        diff_args = ['--name-status', '-z', '--no-ext-diff', '--no-textconv', '--ignore-submodules=none', '-M']
        head = _git(tree.root, 'rev-parse', '--verify', '--quiet', 'HEAD', missing_code=1, deadline=deadline)
        manifest['head'] = head.decode('ascii').strip() if head else '(unborn)'
        for label, flags in (('staged', ['--cached']), ('unstaged', [])):
            seen_diff = set()
            for diff in _git_chunks(tree.root, ['diff', *flags, *diff_args], pathspecs, deadline=deadline):
                for status, changed in _parse_diff(diff):
                    included = [name for name in changed if in_scope(name)]
                    entry_key = (status, tuple(changed))
                    if included and entry_key not in seen_diff:
                        seen_diff.add(entry_key)
                        manifest[label].append([status, changed])
                        names.update(included)
        untracked_set = set()
        for untracked in _git_chunks(tree.root, ['ls-files', '--others', '--exclude-standard', '-z'], pathspecs, deadline=deadline):
            for part in untracked.split(b'\0'):
                if part and in_scope(name := os.fsdecode(part)):
                    untracked_set.add(name)
        manifest['untracked'] = sorted(untracked_set, key=path_key)
        names.update(untracked_set)
        index = {}
        for staged_chunk in _git_chunks(tree.root, ['ls-files', '--stage', '-z'], pathspecs, deadline=deadline):
            for path, metas in _index_entries(staged_chunk).items():
                index.setdefault(path, []).extend(metas)
    else:
        index = {}
        for directory in sorted(directories):
            manifest['unverified'].append({'path': directory, 'reason': '非 Git 项目须逐个声明交付文件。'})
    stamps = {}
    for name in sorted(names, key=path_key):
        try:
            if time.monotonic() >= deadline:
                raise ScopeError('核对超过时间预算。')
            stages = index.get(name, [])
            if any(stage.split()[-1] != '0' for stage in stages):
                raise ScopeError('Git 索引存在未解决的冲突。')
            if any(stage.startswith('160000 ') for stage in stages):
                raise ScopeError('子模块须作为独立工作树审查。')
            state, byte_budget, stamps[name] = _hash_file(tree.root, name, byte_budget, deadline)
            manifest['files'][name] = {'working': state, 'index': stages}
        except (OSError, ScopeError) as exc:
            manifest['unverified'].append({'path': name, 'reason': str(exc)})
    for name, stamp in stamps.items():
        try:
            if time.monotonic() >= deadline:
                raise ScopeError('核对超过时间预算。')
            if not _unchanged(tree.root, name, stamp):
                raise ScopeError('生成成果清单期间文件发生变化。')
        except (OSError, ScopeError) as exc:
            manifest['unverified'].append({'path': name, 'reason': str(exc)})
    if tree.common and not manifest['unverified']:
        try:
            final_head = _git(tree.root, 'rev-parse', '--verify', '--quiet', 'HEAD', missing_code=1, deadline=deadline)
            if final_head != head:
                raise ScopeError('生成成果清单期间 HEAD 发生变化。')
        except ScopeError as exc:
            manifest['unverified'].append({'path': 'HEAD', 'reason': str(exc)})
    return {'digest': _digest(manifest), **manifest}
