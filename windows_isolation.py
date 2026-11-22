"""Launch a Win32 process in an AppContainer with explicit inherited pipes."""

import ctypes as c
from ctypes import wintypes as w
from contextlib import ExitStack, contextmanager
import msvcrt
import os
from pathlib import Path
import subprocess
import threading
import uuid


STATION_LOCK = threading.Lock()
DESKTOP_DENY_SID = 'S-1-5-21-3390279319-962602086-2124725131-1847915627'


class Trustee(c.Structure):
    _fields_ = [('multiple', c.c_void_p), ('operation', c.c_int), ('form', c.c_int),
                ('kind', c.c_int), ('value', c.c_void_p)]


class ExplicitAccess(c.Structure):
    _fields_ = [('rights', w.DWORD), ('mode', c.c_int), ('inherit', w.DWORD), ('trustee', Trustee)]


class Sid(c.Structure):
    _fields_ = [('pointer', c.c_void_p), ('attributes', w.DWORD)]


class Capabilities(c.Structure):
    _fields_ = [('identity', c.c_void_p), ('capabilities', c.POINTER(Sid)),
                ('count', w.DWORD), ('reserved', w.DWORD)]


class StartupInfo(c.Structure):
    _fields_ = [('size', w.DWORD), ('reserved', w.LPWSTR), ('desktop', w.LPWSTR),
                ('title', w.LPWSTR), ('dimensions', w.DWORD * 7), ('flags', w.DWORD),
                ('show', w.WORD), ('reserved_count', w.WORD), ('reserved_bytes', c.c_void_p),
                ('stdin', w.HANDLE), ('stdout', w.HANDLE), ('stderr', w.HANDLE)]


class ExtendedStartup(c.Structure):
    _fields_ = [('info', StartupInfo), ('attributes', c.c_void_p)]


class ProcessInfo(c.Structure):
    _fields_ = [('process', w.HANDLE), ('thread', w.HANDLE), ('pid', w.DWORD), ('tid', w.DWORD)]


class BasicJobLimits(c.Structure):
    _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD),
                ('min_working', c.c_size_t), ('max_working', c.c_size_t), ('active', w.DWORD),
                ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]


class ExtendedJobLimits(c.Structure):
    _fields_ = [('basic', BasicJobLimits), ('io', c.c_uint64 * 6), ('memory', c.c_size_t * 4)]


def _api(dll, name, args, result=w.BOOL):
    method = getattr(c.WinDLL(dll, use_last_error=True), name)
    method.argtypes = args
    method.restype = result
    return method


def _check(success):
    if not success:
        raise c.WinError(c.get_last_error())


def identity(name):
    create = _api('userenv', 'CreateAppContainerProfile',
                  [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, c.c_void_p, w.DWORD, c.POINTER(c.c_void_p)], c.c_long)
    derive = _api('userenv', 'DeriveAppContainerSidFromAppContainerName',
                  [w.LPCWSTR, c.POINTER(c.c_void_p)], c.c_long)
    sid = c.c_void_p()
    status = create(name, name, 'Herdr coordinator role', None, 0, c.byref(sid))
    if status and (status & 0xffffffff) != 0x800700b7:
        raise OSError(f'AppContainer profile HRESULT 0x{status & 0xffffffff:08x}')
    if status and derive(name, c.byref(sid)):
        raise OSError('Cannot derive the existing AppContainer SID')
    convert = _api('advapi32', 'ConvertSidToStringSidW', [c.c_void_p, c.POINTER(w.LPWSTR)])
    free = _api('kernel32', 'LocalFree', [c.c_void_p], c.c_void_p)
    free_sid = _api('advapi32', 'FreeSid', [c.c_void_p], c.c_void_p)
    text = w.LPWSTR()
    try:
        _check(convert(sid, c.byref(text)))
        return text.value
    finally:
        if text:
            free(c.cast(text, c.c_void_p))
        free_sid(sid)


def grant(directory, package, *, write=False):
    """Grant this package access to one explicitly scoped fixture directory."""
    directory = Path(directory).resolve(strict=True)
    rights = 'M' if write else 'RX'
    common = {'check': True, 'capture_output': True, 'timeout': 30,
              'creationflags': subprocess.CREATE_NO_WINDOW}
    subprocess.run(['icacls', str(directory), '/grant:r', f'*{package}:(OI)(CI)({rights})'], **common)
    if write:
        subprocess.run(['icacls', str(directory), '/setintegritylevel', '(OI)(CI)L'], **common)


def protect_from_workers(path):
    """Protect an existing trusted tree from reads and writes by low-integrity workers."""
    path = Path(path)
    if getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
        raise OSError(f'不能保护重解析路径 {path}')
    p = c.c_void_p
    convert = _api('advapi32', 'ConvertStringSecurityDescriptorToSecurityDescriptorW',
                   [w.LPCWSTR, w.DWORD, c.POINTER(p), p])
    get_sacl = _api('advapi32', 'GetSecurityDescriptorSacl', [p, c.POINTER(w.BOOL), c.POINTER(p), c.POINTER(w.BOOL)])
    set_label = _api('advapi32', 'SetNamedSecurityInfoW', [w.LPCWSTR, c.c_int, w.DWORD, p, p, p, p], w.DWORD)
    free = _api('kernel32', 'LocalFree', [p], p)
    descriptor, acl = p(), p()
    present, defaulted = w.BOOL(), w.BOOL()
    inheritance = 'OICI' if path.is_dir() else ''
    _check(convert(f'S:(ML;{inheritance};NWNRNX;;;ME)', 1, c.byref(descriptor), None))
    try:
        _check(get_sacl(descriptor, c.byref(present), c.byref(acl), c.byref(defaulted)))
        status = set_label(str(path), 1, 0x10, None, None, None, acl)
        if status:
            raise c.WinError(status)
    finally:
        free(descriptor)


def protect_interactive_station(sid):
    """Keep the host station inaccessible even if a worker calls OpenWindowStation."""
    p = c.c_void_p
    open_station = _api('user32', 'OpenWindowStationW', [w.LPCWSTR, w.BOOL, w.DWORD], w.HANDLE)
    close_station = _api('user32', 'CloseWindowStation', [w.HANDLE])
    access = _api('advapi32', 'GetSecurityInfo', [w.HANDLE, c.c_int, w.DWORD, p, p,
                                                 c.POINTER(p), p, c.POINTER(p)], w.DWORD)
    set_access = _api('advapi32', 'SetSecurityInfo', [w.HANDLE, c.c_int, w.DWORD, p, p, p, p], w.DWORD)
    merge = _api('advapi32', 'SetEntriesInAclW', [w.DWORD, c.POINTER(ExplicitAccess), p, c.POINTER(p)], w.DWORD)
    get_ace = _api('advapi32', 'GetAce', [p, w.DWORD, c.POINTER(p)])
    equal_sid = _api('advapi32', 'EqualSid', [p, p])
    free = _api('kernel32', 'LocalFree', [p], p)
    with STATION_LOCK:
        station = open_station('WinSta0', False, 0x00060000)
        _check(station)
        descriptor, prior, updated = p(), p(), p()
        try:
            error = access(station, 7, 4, None, None, c.byref(prior), None, c.byref(descriptor))
            if error:
                raise c.WinError(error)
            rights = 0x000f037f  # WINSTA_ALL_ACCESS and standard rights.
            if prior:
                for index in range(w.WORD.from_address(prior.value + 4).value):
                    entry = p()
                    _check(get_ace(prior, index, c.byref(entry)))
                    if (c.c_ubyte.from_address(entry.value).value == 1
                            and w.DWORD.from_address(entry.value + 4).value & rights == rights
                            and equal_sid(entry.value + 8, sid)):
                        return
            rule = ExplicitAccess(rights, 3, 0, Trustee(None, 0, 0, 0, sid))
            error = merge(1, c.byref(rule), prior, c.byref(updated))
            if not error:
                error = set_access(station, 7, 4, None, None, updated, None)
            if error:
                raise c.WinError(error)
        finally:
            for buffer in (updated, descriptor):
                if buffer:
                    free(buffer)
            close_station(station)


def low_integrity_token():
    """Retain the interactive logon identity, removing privileges and lowering integrity."""
    p = c.c_void_p
    get_process = _api('kernel32', 'GetCurrentProcess', [], w.HANDLE)
    close = _api('kernel32', 'CloseHandle', [w.HANDLE])
    open_token = _api('advapi32', 'OpenProcessToken', [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)])
    restrict = _api('advapi32', 'CreateRestrictedToken',
                    [w.HANDLE, w.DWORD, w.DWORD, p, w.DWORD, p, w.DWORD, p, c.POINTER(w.HANDLE)])
    convert = _api('advapi32', 'ConvertStringSidToSidW', [w.LPCWSTR, c.POINTER(p)])
    length = _api('advapi32', 'GetLengthSid', [p], w.DWORD)
    set_token = _api('advapi32', 'SetTokenInformation', [w.HANDLE, c.c_int, p, w.DWORD])
    get_token = _api('advapi32', 'GetTokenInformation', [w.HANDLE, c.c_int, p, w.DWORD, c.POINTER(w.DWORD)])
    free = _api('kernel32', 'LocalFree', [p], p)
    original, restricted, sid = w.HANDLE(), w.HANDLE(), p()
    checks = [p() for _ in range(6)]
    _check(open_token(get_process(), 0x008b, c.byref(original)))
    try:
        standard = ('S-1-1-0', 'S-1-5-32-545', 'S-1-5-11', 'S-1-5-4', 'S-1-5-18', DESKTOP_DENY_SID)
        for value, pointer in zip(standard, checks):
            _check(convert(value, c.byref(pointer)))
        protect_interactive_station(checks[-1])
        required = w.DWORD()
        get_token(original, 1, None, 0, c.byref(required))
        user = c.create_string_buffer(required.value)
        _check(get_token(original, 1, user, required, c.byref(required)))
        user_sid = Sid.from_buffer(user).pointer
        restrict_sids = (Sid * (len(checks) + 1))(*(Sid(pointer, 0) for pointer in checks), Sid(user_sid, 0))
        _check(restrict(original, 1, 0, None, 0, None, len(restrict_sids), restrict_sids, c.byref(restricted)))
        _check(convert('S-1-16-4096', c.byref(sid)))
        label = Sid(sid, 0x20)
        _check(set_token(restricted, 25, c.byref(label), c.sizeof(label) + length(sid)))
        return restricted
    except BaseException:
        if restricted:
            close(restricted)
        raise
    finally:
        for pointer in (*checks, sid):
            if pointer:
                free(pointer)
        close(original)


@contextmanager
def private_desktop():
    """Create an independent, noninteractive window station for a worker tree."""
    p = c.c_void_p
    user = 'user32'
    create_station = _api(user, 'CreateWindowStationW', [w.LPCWSTR, w.DWORD, w.DWORD, p], w.HANDLE)
    get_station = _api(user, 'GetProcessWindowStation', [], w.HANDLE)
    get_object = _api(user, 'GetUserObjectInformationW', [w.HANDLE, c.c_int, p, w.DWORD, c.POINTER(w.DWORD)])
    switch_station = _api(user, 'SetProcessWindowStation', [w.HANDLE])
    close_station = _api(user, 'CloseWindowStation', [w.HANDLE])
    create_desktop = _api(user, 'CreateDesktopW', [w.LPCWSTR, w.LPCWSTR, p, w.DWORD, w.DWORD, p], w.HANDLE)
    close_desktop = _api(user, 'CloseDesktop', [w.HANDLE])
    convert = _api('advapi32', 'ConvertStringSecurityDescriptorToSecurityDescriptorW',
                   [w.LPCWSTR, w.DWORD, c.POINTER(p), p])
    get_sacl = _api('advapi32', 'GetSecurityDescriptorSacl', [p, c.POINTER(w.BOOL), c.POINTER(p), c.POINTER(w.BOOL)])
    set_label = _api('advapi32', 'SetSecurityInfo', [w.HANDLE, c.c_int, w.DWORD, p, p, p, p], w.DWORD)
    free = _api('kernel32', 'LocalFree', [p], p)

    class SecurityAttributes(c.Structure):
        _fields_ = [('size', w.DWORD), ('descriptor', p), ('inherit', w.BOOL)]

    descriptor = p()
    _check(convert('S:(ML;;NW;;;LW)', 1, c.byref(descriptor), None))
    attrs = SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
    station = desktop = None
    try:
        name = 'HerdrWorker-' + uuid.uuid4().hex
        with STATION_LOCK:
            original = get_station()
            _check(original)
            # NULL selects the noninteractive station associated with this logon session.
            station = create_station(None, 0, 0x10000000, None)
            _check(station)
            station_name = c.create_unicode_buffer(260)
            flags = (w.DWORD * 3)()
            needed = w.DWORD()
            _check(get_object(station, 1, flags, c.sizeof(flags), c.byref(needed)))
            _check(get_object(station, 2, station_name, c.sizeof(station_name), c.byref(needed)))
            if flags[2] & 1:
                raise OSError('不能在交互式窗口站启动受管引擎')
            # Vendor command runners may create child desktops inside this separate station.
            present, defaulted, sacl = w.BOOL(), w.BOOL(), p()
            _check(get_sacl(descriptor, c.byref(present), c.byref(sacl), c.byref(defaulted)))
            error = set_label(station, 7, 0x10, None, None, None, sacl)
            if error:
                raise c.WinError(error)
            _check(switch_station(station))
            try:
                desktop = create_desktop(name, None, None, 0, 0x000f01ff, c.byref(attrs))
                _check(desktop)
            finally:
                _check(switch_station(original))
        yield station_name.value + '\\' + name
    finally:
        if desktop:
            close_desktop(desktop)
        if station:
            close_station(station)
        free(descriptor)


def execute(argv, *, package, cwd, environment, stdin, stdout, stderr, timeout=900, on_start=None):
    """Run one process using the package identity, with no inherited ambient handles."""
    kernel = 'kernel32'
    sid_ptr = c.c_void_p()
    convert = _api('advapi32', 'ConvertStringSidToSidW', [w.LPCWSTR, c.POINTER(c.c_void_p)])
    free = _api(kernel, 'LocalFree', [c.c_void_p], c.c_void_p)
    close = _api(kernel, 'CloseHandle', [w.HANDLE])
    init = _api(kernel, 'InitializeProcThreadAttributeList', [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)])
    update = _api(kernel, 'UpdateProcThreadAttribute',
                  [c.c_void_p, w.DWORD, c.c_size_t, c.c_void_p, c.c_size_t, c.c_void_p, c.c_void_p])
    delete = _api(kernel, 'DeleteProcThreadAttributeList', [c.c_void_p], None)
    create = _api(kernel, 'CreateProcessW', [w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD,
                                           c.c_void_p, w.LPCWSTR, c.POINTER(ExtendedStartup), c.POINTER(ProcessInfo)])
    create_user = _api('advapi32', 'CreateProcessAsUserW', [w.HANDLE, *create.argtypes])
    wait = _api(kernel, 'WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD)
    exit_code = _api(kernel, 'GetExitCodeProcess', [w.HANDLE, c.POINTER(w.DWORD)])
    terminate = _api(kernel, 'TerminateProcess', [w.HANDLE, w.UINT])
    new_job = _api(kernel, 'CreateJobObjectW', [c.c_void_p, w.LPCWSTR], w.HANDLE)
    job_info = _api(kernel, 'SetInformationJobObject', [w.HANDLE, c.c_int, c.c_void_p, w.DWORD])

    std_handles = [msvcrt.get_osfhandle(stream.fileno()) for stream in (stdin, stdout, stderr)]
    inherited = {handle: os.get_handle_inheritable(handle) for handle in std_handles}
    handles = (w.HANDLE * len(inherited))(*inherited)
    sids = [sid_ptr, c.c_void_p(), c.c_void_p()]
    size = c.c_size_t()
    count = 2 if package is None else 3
    init(None, count, 0, c.byref(size))
    attributes = c.create_string_buffer(size.value)
    _check(init(attributes, count, 0, c.byref(size)))
    proc = ProcessInfo()
    job = None
    token = None
    contexts = ExitStack()
    try:
        job = new_job(None, None)
        _check(job)
        limits = ExtendedJobLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _check(job_info(job, 9, c.byref(limits), c.sizeof(limits)))
        jobs = (w.HANDLE * 1)(job)
        if package is None:
            token = low_integrity_token()
            desktop_name = contexts.enter_context(private_desktop())
        else:
            for name, pointer in zip((package, 'S-1-15-3-2', 'S-1-15-3-3'), sids):
                _check(convert(name, c.byref(pointer)))
            capabilities = (Sid * 2)(*(Sid(pointer, 4) for pointer in sids[1:]))
            security = Capabilities(sid_ptr, capabilities, 2, 0)
            _check(update(attributes, 0, 0x20009, c.byref(security), c.sizeof(security), None, None))
        _check(update(attributes, 0, 0x20002, handles, c.sizeof(handles), None, None))
        _check(update(attributes, 0, 0x2000d, jobs, c.sizeof(jobs), None, None))
        info = ExtendedStartup()
        info.info.size = c.sizeof(info)
        info.info.flags = 0x100
        info.info.stdin, info.info.stdout, info.info.stderr = std_handles
        if token:
            info.info.desktop = desktop_name
        info.attributes = c.cast(attributes, c.c_void_p)
        values = '\0'.join(f'{key}={value}' for key, value in sorted(environment.items(), key=lambda pair: pair[0].upper()))
        block = c.create_unicode_buffer(values + '\0\0')
        command = c.create_unicode_buffer(subprocess.list2cmdline(argv))
        for handle in inherited:
            os.set_handle_inheritable(handle, True)
        try:
            flags = 0x80000 | 0x400 | subprocess.CREATE_NO_WINDOW
            args = (argv[0], command, None, None, True, flags, block, str(cwd), c.byref(info), c.byref(proc))
            _check(create_user(token, *args) if token else create(*args))
        finally:
            for handle, previous in inherited.items():
                os.set_handle_inheritable(handle, previous)
        if on_start:
            on_start(proc.pid)
        if wait(proc.process, int(timeout * 1000)) == 258:
            _check(terminate(proc.process, 124))
            wait(proc.process, 5000)
            raise subprocess.TimeoutExpired(argv[0], timeout)
        result = w.DWORD()
        _check(exit_code(proc.process, c.byref(result)))
        return result.value
    finally:
        for handle in (proc.thread, proc.process):
            if handle:
                close(handle)
        if job:
            close(job)
        if token:
            close(token)
        contexts.close()
        for pointer in sids:
            if pointer:
                free(pointer)
        delete(attributes)
