import json
from pathlib import Path

def verify_owner(config_path, owner):
    identity = Path(config_path).with_name('instance-identity.json')
    pinned = json.loads(identity.read_text(encoding='utf-8'))['owner']
    if not isinstance(pinned, str) or not pinned.isascii() or not pinned.isdecimal():
        raise ValueError('Invalid pinned instance owner')
    if owner != pinned:
        raise ValueError('Owner identity cannot change')
