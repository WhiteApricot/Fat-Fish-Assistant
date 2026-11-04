"""Explicit cold rollback of the verified optional extension; never remove core."""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
from pathlib import Path
from fat_fish_management import controller as c
from fat_fish_management.dependencies import rollback_optional_environment
p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--confirm-optional-features-disabled',action='store_true');a=p.parse_args()
if not a.confirm_optional_features_disabled:raise ValueError('Confirm optional adapters and document tools are no longer configured before rollback')
root=c.owned_root(a.root);record=c.state(root)
with c.installation_lock(root):
 c.operate(root,record,'stop')
 rollback_optional_environment(c.chat_python(root,record))
 record['dependency_profile']='core';c.atomic(root/'installation.json',record)
print('{"optional_rollback":"completed","core_preserved":true}')
