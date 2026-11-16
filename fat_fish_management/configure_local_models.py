"""Initialize independent local model credentials and endpoints on a stopped instance."""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import secrets
from pathlib import Path
from fat_fish_management.lifecycle_lock import exclusive

p=argparse.ArgumentParser();p.add_argument('--instance',type=Path,required=True);p.add_argument('--embedding-port',type=int,default=6203);p.add_argument('--components',default='embedding');a=p.parse_args();root=a.instance.resolve()
selected=set(a.components.split(','))
if not selected or selected-{'embedding'}:raise ValueError('Unknown local model component')
if not 1<=a.embedding_port<=65535:raise ValueError('Invalid local port')
with exclusive(root):
    if any((root/'private'/name).exists() for name in ['RUNNING','DIRTY','upgrade-journal.json']):raise ValueError('Resolve active or interrupted maintenance first')
    path=root/'data/cmd_config.json';before=path.read_bytes();cfg=json.loads(before.decode('utf-8-sig'))
    providers=cfg.setdefault('provider',[])
    embedding=next((x for x in providers if x.get('id')=='feiyu-local-embedding'),None)
    if 'embedding' in selected:
        key_path=root/'private/local-embedding-key.txt'
        if not key_path.exists():key_path.write_text(secrets.token_hex(32))
        key=key_path.read_text().strip()
        if embedding is None:
            embedding={'id':'feiyu-local-embedding','type':'openai_embedding','provider_type':'embedding','enable':True,
                       'embedding_api_key':key,'embedding_model':'feiyu-bge-small-zh','embedding_dimensions':512,'embedding_dimensions_mode':'never','timeout':30}
            providers.append(embedding)
        elif embedding.get('type')!='openai_embedding' or embedding.get('embedding_api_key')!=key:raise ValueError('Existing embedding identity/key mismatch; refusing overwrite')
        embedding['embedding_api_base']=f'http://127.0.0.1:{a.embedding_port}/v1'
    if cfg==json.loads(before.decode('utf-8-sig')):
        print('Local provider configuration already matches; no files changed')
        raise SystemExit(0)
    history=root/'private/config-history';history.mkdir(exist_ok=True)
    (history/(secrets.token_hex(8)+'.json')).write_bytes(before)
    pending=path.with_suffix('.pending');pending.write_text(json.dumps(cfg,ensure_ascii=False),encoding='utf-8');pending.replace(path)
print('Independent local providers configured; cloud LLM credentials and QQ login remain user-supplied')
