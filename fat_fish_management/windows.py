"""Windows platform boundary for the existing FFA workers and SnowLuma discovery."""
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import uuid

from fat_fish_management.dependencies import PROJECT, clean_environment, load
from fat_fish_management.errors import Fault

SCRIPTS = {
    'Install-Runtime.ps1': 'scripts/build/Install-Runtime.ps1',
    'Invoke-LocalModels.ps1': 'scripts/services/Invoke-LocalModels.ps1',
    'Check-Environment.ps1': 'scripts/diagnostics/Check-Environment.ps1',
}


def environment_checks():
    architecture_ok = os.name == 'nt' and platform.machine().lower() in {'amd64', 'x86_64'}
    yield dict(name='windows_x64', ok=architecture_ok, severity='error', detail=platform.platform(),
               hint='Use Windows x64 for this release.')
    if os.name != 'nt':
        return
    version = sys.getwindowsversion()
    yield dict(name='windows_version', ok=version.major >= 10, severity='error',
               detail=str(version), hint='Use Windows 10 or 11 x64.')
    import ctypes
    missing = []
    for name in ('VCRUNTIME140.dll', 'VCRUNTIME140_1.dll', 'MSVCP140.dll'):
        try:
            ctypes.WinDLL(str(Path(os.environ['SystemRoot']) / 'System32' / name))
        except OSError:
            missing.append(name)
    yield dict(name='microsoft_runtime', ok=not missing, severity='error', detail={'missing': missing},
               hint='See the official Microsoft x64 runtime in manifests/assets.json.')


def snowluma_launcher(directory, version):
    """Inspect the pinned official layout without reading user or runtime data."""
    if os.name != 'nt':
        return None
    lock = load('snowluma/windows-x64.json')
    if version != lock['version']:
        return None
    launcher = directory / lock['launcher']
    metadata = directory / 'release.json'
    if not launcher.is_file() or launcher.is_symlink() or not metadata.is_file() or metadata.is_symlink():
        return None
    try:
        release = json.loads(metadata.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(release, dict) or release.get('version') != version or release.get('target') != 'win32-x64':
        return None
    return launcher


def powershell_path():
    if os.name != 'nt':
        raise Fault('incompatible', 'Windows service management requires Windows')
    return Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'


def run_script(root, script, *args, json_output=False):
    executable = powershell_path()
    env = clean_environment()
    env['PSMODULEPATH'] = str(executable.parent / 'Modules')
    # Detached workers can inherit output handles. Wait for the script, not pipe EOF.
    log = root / 'logs' / (script + '-' + uuid.uuid4().hex + '.log')
    log.parent.mkdir(exist_ok=True)
    with log.open('wb') as stream:
        proc = subprocess.run(
            [str(executable), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
             str(PROJECT / SCRIPTS[script]), *map(str, args)],
            stdout=stream, stderr=subprocess.STDOUT, env=env, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    if proc.returncode:
        raise Fault('failed', script + ' failed', f'Inspect the private log: {log}')
    if json_output:
        output = log.read_text(encoding='utf-8-sig', errors='replace').strip()
        if output in {'True', 'False'}:
            return output == 'True'
        return json.loads(output)
    return {'completed': True}


def install_runtime(root, installer):
    return run_script(root, 'Install-Runtime.ps1', '-Installer', installer)


def model_service(root, action, instance, name, *, port=None, python=None):
    options = []
    if port is not None:
        options.extend(('-Port', port))
    if python is not None:
        options.extend(('-SpeechPython', python))
    return run_script(root, 'Invoke-LocalModels.ps1', '-Action', action.title(),
                      '-Runtime', instance, '-Component', name.title(), *options,
                      json_output=action == 'status')


def verify_environment(root, chat_python, speech_python=None):
    options = ['-ChatPython', chat_python]
    if speech_python is not None:
        options.extend(('-SpeechPython', speech_python))
    return run_script(root, 'Check-Environment.ps1', *options)


def protect_private(path):
    executable = powershell_path()
    env = dict(os.environ, PSMODULEPATH=str(executable.parent / 'Modules'))
    proc = subprocess.run(
        [str(executable), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         str(PROJECT / 'scripts/process/Protect-Private.ps1'), '-Path', str(path)],
        env=env, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if proc.returncode:
        raise RuntimeError('Private boundary protection failed: ' + proc.stderr.strip())


def ensure_no_build_writers(destination):
    executable = powershell_path()
    proc = subprocess.run(
        [str(executable), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         str(PROJECT / 'scripts/build/Assert-Build-Writers.ps1'),
         '-Destination', str(destination), '-ControllerPid', str(os.getpid())],
        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if proc.returncode:
        raise RuntimeError('Cannot verify interrupted build writers; partial environment retained')
    if json.loads(proc.stdout.removeprefix('\ufeff'))['active_count']:
        raise RuntimeError('An interrupted build still has active Python workers; wait for it to exit before retry; nothing was moved')
