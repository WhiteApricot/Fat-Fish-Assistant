"""Explicit cold rollback of the verified optional extension; never remove core."""
import argparse
from pathlib import Path
import controller as c
from dependencies import rollback_optional_environment
p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--confirm-optional-features-disabled',action='store_true');a=p.parse_args()
if not a.confirm_optional_features_disabled:raise ValueError('Confirm optional adapters and document tools are no longer configured before rollback')
root=c.owned_root(a.root);record=c.state(root)
with c.installation_lock(root):
 c.operate(root,record,'stop')
 rollback_optional_environment(c.chat_python(root,record))
 record['dependency_profile']='core';c.atomic(root/'installation.json',record)
print('{"optional_rollback":"completed","core_preserved":true}')
