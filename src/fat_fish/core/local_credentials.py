"""Windows current-user DPAPI storage for the local AstrBot dashboard only."""
import ctypes
import json
import logging
import os
import re
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


def save_password(root, password):
    if not isinstance(password, str) or not password:
        raise ValueError('Empty dashboard password')
    path = Path(root)/'private/dashboard-credential.dpapi'
    # DPAPI binds to the Windows account; no machine-wide or plaintext fallback.
    raw = _crypt(json.dumps({'username': 'astrbot', 'password': password}).encode())
    pending = path.with_suffix('.pending')
    pending.write_bytes(raw)
    pending.replace(path)


def load_password(root):
    root = Path(root)
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
            record.msg = message[:match.start(1)]+'[saved with Windows DPAPI]'+message[match.end(1):]
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
