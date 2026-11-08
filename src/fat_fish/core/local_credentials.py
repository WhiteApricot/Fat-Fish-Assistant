"""Platform-specific storage for the local AstrBot dashboard."""
import ctypes
import json
import logging
import os
import re
import stat
import tempfile
from ctypes import wintypes
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(value, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('Dashboard credential storage requires Windows DPAPI')
    buf = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    api = ctypes.WinDLL('crypt32', use_last_error=True)
    function = api.CryptUnprotectData if decrypt else api.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise RuntimeError('Windows could not unlock the dashboard credential')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel = ctypes.WinDLL('kernel32')
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def _posix_credential_path(root):
    private = Path(root) / 'private'
    info = private.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError('Dashboard credential directory must be owned by the service user')
    private.chmod(0o700)
    if private.stat().st_mode & 0o077:
        raise ValueError('Dashboard credential directory cannot be protected')
    return private / 'dashboard-credential.json'

def save_password(root, password):
    if not isinstance(password, str) or not password:
        raise ValueError('Empty dashboard password')
    if os.name != 'nt':
        path = _posix_credential_path(root)
        fd, name = tempfile.mkstemp(prefix='.dashboard-', dir=path.parent)
        pending = Path(name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'username': 'astrbot', 'password': password}, stream)
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)
        return
    path = Path(root)/'private/dashboard-credential.dpapi'
    # DPAPI binds to the Windows account; no machine-wide or plaintext fallback.
    raw = _crypt(json.dumps({'username': 'astrbot', 'password': password}).encode())
    pending = path.with_suffix('.pending')
    pending.write_bytes(raw)
    pending.replace(path)


def load_password(root):
    root = Path(root)
    if os.name != 'nt':
        path = _posix_credential_path(root)
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), encoding='utf-8') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise ValueError('Dashboard credential permissions are unsafe')
                return json.load(stream)['password']
        except FileNotFoundError as exc:
            raise ValueError('Update the saved AstrBot dashboard password once') from exc
    path = root/'private/dashboard-credential.dpapi'
    legacy = root/'private/dashboard-password.txt'
    if path.exists():
        return json.loads(_crypt(path.read_bytes(), True))['password']
    if legacy.exists():
        password = legacy.read_text(encoding='utf-8-sig').strip()
        save_password(root, password)
        legacy.unlink()  # Only after a successful atomic encrypted write.
        return password
    raise ValueError('Update the saved AstrBot dashboard password once')


class InitialPasswordFilter(logging.Filter):
    """Capture the upstream startup credential before any logger persists it."""
    def __init__(self, root):
        super().__init__()
        self.root = Path(root)

    def filter(self, record):
        if getattr(record, '_ffa_credential_redacted', False):
            return True
        message = record.getMessage()
        match = re.search(r'Initial password:\s*([^\s]+)', message)
        if match:
            save_password(self.root, match[1])
            record.msg = message[:match.start(1)]+('[saved with Windows DPAPI]' if os.name == 'nt' else '[saved in protected storage]')+message[match.end(1):]
            record.args = ()
            record._ffa_credential_redacted = True
        return True


def install_capture(root):
    capture = InitialPasswordFilter(root)
    logger=logging.getLogger('astrbot')
    for old in list(logger.filters):
        if isinstance(old,InitialPasswordFilter):logger.removeFilter(old)
    logger.addFilter(capture)
    return capture


def capture_generated(root, config):
    # Persona seeding imports AstrBot before its HTTP server starts. Preserve
    # that real upstream-generated initial credential through the same log filter.
    value = getattr(config, '_generated_dashboard_password', None)
    if value:
        logger = logging.getLogger('astrbot')
        record = logger.makeRecord('astrbot', logging.INFO, __file__, 0,
                                   'AstrBot startup - Initial password: %s', (value,), None)
        InitialPasswordFilter(root).filter(record)
        logger.handle(record)
        object.__setattr__(config, '_generated_dashboard_password', None)
