"""Component lifecycle state and layered communication health."""
from pathlib import Path

from fat_fish_management import windows
from fat_fish_management.dependencies import load


def lifecycle(component, *, initialized=True):
    if not component['installed']:
        state = 'not_installed'
    elif not component['program_present'] or component.get('dirty') or component.get('health_failed') or component.get('maintenance_pending'):
        state = 'unhealthy'
    elif not initialized:
        state = 'not_initialized'
    elif component['running'] is True:
        state = 'running'
    elif component['running'] is False:
        state = 'stopped'
    else:
        state = 'installed'
    return {**component, 'state': state}


def snowluma_status(record, protocol=None):
    info = record.get('components', {}).get('snowluma', {})
    lock_path = 'snowluma/windows-x64.json'
    lock = load(lock_path)
    path = Path(info['path']) if info.get('path') else None
    present = bool(path and path.is_absolute() and windows.snowluma_launcher(path, info.get('version')))
    protocol = protocol or {}
    # A ready action channel demonstrates a peer is running. It cannot identify
    # an OS process or prove that peer was launched from the registered directory.
    result = lifecycle({
        'id': 'snowluma', 'installed': bool(info.get('installed')),
        'program_present': present if info else None,
        'running': None, 'ownership': 'official-external', 'path': info.get('path'),
        'version': info.get('version'), 'pinned_version': lock['version'], 'depends': [],
        'manifest_refs': ['manifests/components.json', 'manifests/' + lock_path],
    })
    result.update(manual_action=None, onebot_peer_ready=protocol.get('onebot_ready'))
    if not info or (present and not protocol.get('connected')):
        result['state'] = 'waiting_for_user'
        result['manual_action'] = 'Use the official SnowLuma Launcher and WebUI, then verify the reverse connection.'
    return result


def communication_health(protocol):
    return {
        'onebot': {'connected': protocol.get('connected') is True,
                   'reason': protocol.get('reason', 'not_observed')},
        'astrbot_connected': protocol.get('astrbot_connected'),
        'onebot_service_ready': protocol.get('onebot_ready'),
        'qq_login': protocol.get('qq_online'),
        'bot_identity_matches': protocol.get('identity_matches'),
        'core_loaded': protocol.get('core_loaded'),
        'model_call': 'not_checked',
        'business_chat': 'not_checked',
    }
