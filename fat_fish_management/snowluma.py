"""Acquire and guide the independent official SnowLuma installation."""
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import time
import zipfile

from fat_fish_management import windows
from fat_fish_management.components import communication_health
from fat_fish_management.dependencies import acquire, digest, external, load, unpack
from fat_fish_management.errors import Fault
from fat_fish_management.onebot_config import listener

LOCK = 'snowluma/windows-x64.json'
WEBUI_PORT = 5099
PROGRAM_FILES = ('launcher.bat', 'release.json', 'index.mjs', 'node.exe',
                 'check-node-version.cjs', 'package.json', 'EULA.md', 'PRIVACY.md')


def program_hashes(directory):
    native = directory / 'native'
    if not native.is_dir() or native.is_symlink() or native.is_junction():
        raise ValueError('SnowLuma native payload directory is missing or redirected.')
    paths = [directory / name for name in PROGRAM_FILES]
    for path in native.rglob('*'):
        if path.is_symlink() or path.is_junction():
            raise ValueError('SnowLuma native payload contains a redirected path.')
        if path.is_file():
            paths.append(path)
    if len(paths) == len(PROGRAM_FILES):
        raise ValueError('SnowLuma native payload is missing.')
    if any(not path.is_file() or path.is_symlink() for path in paths):
        raise ValueError('SnowLuma program files are missing or redirected.')
    return {path.relative_to(directory).as_posix(): digest(path) for path in sorted(paths)}


def release_path(root, destination):
    path = external(destination)
    if path == root or path.is_relative_to(root) or root.is_relative_to(path):
        raise Fault('read_only', '请选择与 FFA 安装目录独立的 SnowLuma 目录。')
    return path


def acquire_release(root, destination, cache, archive, offline, notify, write_json):
    if os.name != 'nt':
        raise Fault('incompatible', '此发行锁用于 Windows x64。')
    lock = load(LOCK)
    destination = release_path(root, destination)
    receipt_path = root / 'snowluma-release.json'
    if destination.exists():
        receipt = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.is_file() else {}
        if not matching_release(destination, receipt, lock):
            raise Fault('read_only', '目标目录已存在且没有匹配的安装记录。请选择新的目录。')
        return destination

    notify('1/3', '校验 SnowLuma 官方发行资产')
    if archive is None:
        try:
            archive = acquire(lock, cache, offline, notify, official_release=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise Fault('failed', str(exc), '请检查官方发行页，或提供同版本的官方 ZIP 通过 --archive 离线安装。') from exc
    if not archive.is_file() or archive.stat().st_size != lock['size'] or digest(archive) != lock['sha256']:
        raise Fault('integrity', 'SnowLuma ZIP 的大小或 SHA256 与官方发行锁不符。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < lock['size']:
        raise Fault('needs_input', 'SnowLuma 目标磁盘空间不足，无法暂存官方 ZIP。')
    # Copy the official archive into a unique stage and verify it again before
    # unpacking, since an externally selected ZIP can change after selection.
    with tempfile.TemporaryDirectory(prefix='.ffa-snowluma-stage-', dir=destination.parent) as work:
        frozen = Path(work) / 'verified.zip'
        with archive.open('rb') as source, frozen.open('xb') as target:
            shutil.copyfileobj(source, target)
        if (frozen.stat().st_size != lock['size'] or digest(frozen) != lock['sha256']
                or not zipfile.is_zipfile(frozen)):
            raise Fault('integrity', 'SnowLuma ZIP 在校验期间变化或格式不正确，请重试。')
        with zipfile.ZipFile(frozen) as package:
            expanded = sum(entry.file_size for entry in package.infolist())
        if shutil.disk_usage(destination.parent).free < expanded:
            raise Fault('needs_input', 'SnowLuma 目标磁盘空间不足，无法解压官方 ZIP。')
        notify('2/3', '将官方原始文件解压到独立暂存目录')
        stage = Path(work) / 'release'
        unpack(frozen, stage)
        if not windows.snowluma_launcher(stage, lock['version']):
            raise Fault('integrity', '发行包缺少正确的 Windows x64 Launcher 或发行元数据。')
        program = program_hashes(stage)
        notify('3/3', '激活已验证的独立官方目录')
        # Record provenance before activation so a completed rename is recoverable.
        write_json(receipt_path, {'path': str(destination), 'version': lock['version'],
                                  'sha256': lock['sha256'], 'program': program})
        if destination.exists():
            raise Fault('read_only', 'SnowLuma 目标目录在安装过程中出现，已停止。')
        stage.rename(destination)
    return destination


def matching_release(directory, receipt, lock):
    if (receipt.get('path') != str(directory) or receipt.get('version') != lock['version']
            or receipt.get('sha256') != lock['sha256']
            or not windows.snowluma_launcher(directory, lock['version'])):
        return False
    try:
        return program_hashes(directory) == receipt.get('program')
    except (OSError, ValueError):
        return False


def check_port(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise Fault('invalid', 'SnowLuma WebUI 端口必须在 1024 至 65535 之间。')
    return port


def webui_state(port):
    """Only check the public, loopback homepage. No login or private API calls."""
    check_port(port)
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
    try:
        connection.request('GET', '/')
        response = connection.getresponse()
        body = response.read(65536)
        if response.status == 200 and b'snowluma' in body.lower():
            return 'reachable'
        return 'unexpected_http_service'
    except (OSError, http.client.HTTPException):
        # On Windows, filtered loopback can time out instead of refusing a
        # connection even on a free port. Verify ownership with an exclusive bind.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(('127.0.0.1', port))
        except OSError:
            return 'unverified'
        return 'not_listening'
    finally:
        connection.close()


def webui_url(port):
    return f'http://127.0.0.1:{check_port(port)}/'


def connection_fields(instance):
    if instance is None:
        raise Fault('needs_input', '请先初始化 FFA 的 AstrBot 实例。')
    config = json.loads((instance / 'data/cmd_config.json').read_text(encoding='utf-8-sig'))
    platform = listener(config)
    if not platform or not (instance / 'private/bot-identity.json').is_file():
        raise Fault('needs_input', '请先配置 FFA 的 OneBot 监听及机器人 QQ 号码。')
    token = (instance / 'private/onebot-token.txt').read_text(encoding='utf-8').strip()
    if platform['ws_reverse_token'] != token:
        raise Fault('integrity', 'FFA 保存的 OneBot 令牌与 AstrBot 监听配置不一致。')
    return f"ws://127.0.0.1:{platform['ws_reverse_port']}/ws", token


def guidance(instance, port):
    try:
        endpoint, _ = connection_fields(instance)
    except (Fault, OSError, ValueError):
        endpoint = None
    return {'state': 'waiting_for_user', 'webui_url': webui_url(port),
            'reverse_websocket_url': endpoint, 'role': 'Universal', 'message_format': 'array',
            'reconnect_ms': 5000, 'report_self_message': False,
            'steps': [
                '首次启动请在官方界面阅读并自行确认协议。官方 Launcher 窗口提供 WebUI 初始登录信息。',
                '从腾讯官方桌面 QQ 打开登录窗口，使用机器人账号扫码并完成设备确认。',
                '进入 SnowLuma 官方 WebUI，在对应机器人账号的实例页面点击加载，确认账号在线。',
                '在该账号的 OneBot 设置新增反向 WebSocket 客户端，粘贴 FFA 提供的 URL 和令牌。',
                '选择 Universal 和 array，重连间隔填写 5000 毫秒。关闭自身消息上报与状态指令，然后保存并启用。',
                '回到 FFA 检查连接状态。当账号匹配且 OneBot 就绪后，从 QQ 发送测试消息确认回复。',
            ]}


def launch(root, info, port, write_json):
    check_port(port)
    if not info.get('installed'):
        raise Fault('needs_input', '请先获取并登记 SnowLuma 官方发行包。')
    path = release_path(root, Path(info['path']))
    lock = load(LOCK)
    receipt_path = root / 'snowluma-release.json'
    receipt = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.is_file() else {}
    if not matching_release(path, receipt, lock):
        raise Fault('needs_input', '请从已校验的发行包安装，或在现有目录中双击官方 Launcher。')
    record_path = root / 'snowluma-launch.json'
    previous = launcher_record(root)
    if tracked_launcher(previous):
        raise Fault('busy', '上次打开的 SnowLuma Launcher 仍在运行。请完成官方界面操作或关闭旧窗口。')
    if webui_state(port) != 'not_listening':
        raise Fault('busy', 'SnowLuma WebUI 端口已被占用或无法确认。请先检查官方窗口和端口。')
    proc = windows.launch_snowluma(path, lock['launcher'], port)
    created = windows.running_process_creation(proc.pid)
    write_json(record_path, {'pid': proc.pid, 'created': created, 'port': port, 'path': str(path)})
    return {'state': 'waiting_for_user', 'webui_url': webui_url(port),
            'instruction': '请保留官方 Launcher 窗口，在其中查看启动、首次设置或错误提示。'}


def tracked_launcher(record):
    pid, created = record.get('pid'), record.get('created')
    return (type(pid) is int and pid > 0 and type(created) is int
            and windows.running_process_creation(pid) == created)


def launcher_record(root):
    marker = root / 'snowluma-launch.json'
    return json.loads(marker.read_text(encoding='utf-8')) if marker.is_file() else {}


def status(root, port, protocol):
    launch_record = launcher_record(root)
    webui = webui_state(port)
    hints = {
        'not_listening': '查看官方 Launcher 的协议确认和启动信息，或核对 WebUI 端口。',
        'unexpected_http_service': '此端口响应与 SnowLuma WebUI 不符，请检查端口占用。',
        'unverified': '暂时无法验证 WebUI，请检查官方窗口及回环设置。',
    }
    return {'webui': webui, 'webui_url': webui_url(port),
            'tracked_launcher_running': bool(tracked_launcher(launch_record)) if launch_record else None,
            'connection': communication_health(protocol), 'hint': hints.get(webui)}


def wait_for_connection(read_protocol, timeout):
    if type(timeout) is not int or not 1 <= timeout <= 180:
        raise Fault('invalid', '连接检查等待时间必须在 1 至 180 秒之间。')
    deadline = time.monotonic() + timeout
    while True:
        evidence = read_protocol()
        remaining = deadline - time.monotonic()
        if evidence.get('connected') is True or remaining <= 0:
            return evidence
        time.sleep(min(2, remaining))


def open_webui(port, timeout=0):
    url = webui_url(port)
    deadline = time.monotonic() + timeout
    while True:
        state = webui_state(port)
        if state == 'reachable':
            windows.open_snowluma_webui(url)
            return {'opened': True, 'webui_url': url}
        if state == 'unexpected_http_service' or time.monotonic() >= deadline:
            break
        time.sleep(.5)
    raise Fault('needs_input', '尚未确认 SnowLuma 官方 WebUI 已就绪。',
                f'查看官方 Launcher 窗口。官方界面地址为 {url}。')
