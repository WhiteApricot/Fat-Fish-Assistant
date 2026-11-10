"""Bind a stopped FFA instance to its expected OneBot account."""
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
from pathlib import Path

from fat_fish_management.controller import atomic
from fat_fish_management.lifecycle_lock import exclusive
from fat_fish_management.onebot_config import listener


def configure(instance, bot_id):
    if not isinstance(bot_id, str) or not bot_id.isascii() or not bot_id.isdecimal() or int(bot_id) == 0:
        raise ValueError('Invalid bot identity')
    with exclusive(instance, '.start-lock'), exclusive(instance):
        if any((instance / 'private' / name).exists() for name in ('RUNNING', 'DIRTY', 'upgrade-journal.json')):
            raise ValueError('Stop FFA cleanly before binding its OneBot identity')
        cfg = json.loads((instance / 'data/cmd_config.json').read_text(encoding='utf-8-sig'))
        platform = listener(cfg)
        if platform is None:
            raise ValueError('Initialize the managed AstrBot OneBot listener first')
        port = platform['ws_reverse_port']
        token = (instance / 'private/onebot-token.txt').read_text(encoding='utf-8').strip()
        if not token or platform.get('ws_reverse_token') != token:
            raise ValueError('OneBot credential mismatch')
        identity = instance / 'private/bot-identity.json'
        if identity.exists():
            if json.loads(identity.read_text(encoding='utf-8')).get('bot_id') != bot_id:
                raise ValueError('Existing bot identity differs; no automatic replacement')
        else:
            atomic(identity, {'bot_id': bot_id})
    return {'configured': True, 'reverse_ws_url': f'ws://127.0.0.1:{port}/ws',
            'credentials_printed': False, 'manual_step': 'Configure the reverse client in the official SnowLuma WebUI.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True, type=Path)
    parser.add_argument('--bot-id', required=True)
    args = parser.parse_args()
    print(json.dumps(configure(args.instance, args.bot_id)))
