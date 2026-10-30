"""Initialize independent local model credentials and endpoints on a stopped instance."""
import argparse
import json
import secrets
from pathlib import Path
from lifecycle_lock import exclusive

p=argparse.ArgumentParser();p.add_argument('--instance',type=Path,required=True);p.add_argument('--embedding-port',type=int,default=6203);p.add_argument('--speech-port',type=int,default=6202);p.add_argument('--components',default='embedding,speech');a=p.parse_args();root=a.instance.resolve()
selected=set(a.components.split(','))
if not selected or selected-{'embedding','speech'}:raise ValueError('Unknown local model component')
if not all(1<=port<=65535 for port in [a.embedding_port,a.speech_port]) or a.embedding_port==a.speech_port:raise ValueError('Invalid or duplicate local ports')
with exclusive(root):
    if any((root/'private'/name).exists() for name in ['RUNNING','DIRTY','upgrade-journal.json']):raise ValueError('Resolve active or interrupted maintenance first')
    path=root/'data/cmd_config.json';before=path.read_bytes();cfg=json.loads(before.decode('utf-8-sig'))
    providers=cfg.setdefault('provider',[])
    embedding=next((x for x in providers if x.get('id')=='feiyu-local-embedding'),None)
    speech=next((x for x in providers if x.get('id')=='feiyu-local-stt'),None)
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
    if 'speech' in selected:
        if speech is None:
            speech={'id':'feiyu-local-stt','type':'openai_whisper_api','provider_type':'speech_to_text','enable':True,
                    'api_key':secrets.token_hex(32),'model':'whisper-base','timeout':90}
            providers.append(speech)
        elif speech.get('type')!='openai_whisper_api':raise ValueError('Existing speech provider type mismatch')
        speech['api_base']=f'http://127.0.0.1:{a.speech_port}/v1'
        cfg.setdefault('provider_stt_settings',{})['provider_id']=speech['id']
    if cfg==json.loads(before.decode('utf-8-sig')):
        print('Local provider configuration already matches; no files changed')
        raise SystemExit(0)
    history=root/'private/config-history';history.mkdir(exist_ok=True)
    (history/(secrets.token_hex(8)+'.json')).write_bytes(before)
    pending=path.with_suffix('.pending');pending.write_text(json.dumps(cfg,ensure_ascii=False),encoding='utf-8');pending.replace(path)
print('Independent local providers configured; cloud LLM credentials and QQ login remain user-supplied')
