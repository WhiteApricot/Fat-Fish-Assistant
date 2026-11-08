"""Initialize native DeepSeek and loopback OneBot settings on a stopped instance.

Does not import another instance's configuration, grant groups, log in or send.
"""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse,json,secrets,shutil
from pathlib import Path
from fat_fish_management.lifecycle_lock import exclusive

def configure(root,port=6199,model='deepseek-chat',onebot_only=False):
    root=Path(root).resolve()
    if type(port) is not int or not 1024<=port<=65535:raise ValueError('Invalid loopback OneBot port')
    if model not in {'deepseek-chat','deepseek-reasoner'}:raise ValueError('Unsupported model preset')
    with exclusive(root):
        if any((root/'private'/name).exists() for name in ['RUNNING','DIRTY','upgrade-journal.json']):raise ValueError('Stop cleanly before configuration')
        path=root/'data/cmd_config.json';before=path.read_bytes();cfg=json.loads(before.decode('utf-8-sig'))
        providers=cfg.setdefault('provider',[]);platforms=cfg.setdefault('platform',[])
        provider={'id':'fat-fish-chat','provider':'deepseek','type':'openai_chat_completion','provider_type':'chat_completion','enable':True,'key':['$DEEPSEEK_API_KEY'],'api_base':'https://api.deepseek.com/v1','model':model,'timeout':120}
        old=next((x for x in providers if x.get('id')==provider['id']),None)
        if not onebot_only and old is not None and old!=provider:raise ValueError('Existing provider differs; use native configuration UI')
        if not onebot_only and any(x.get('id')!='fat-fish-chat' and x.get('provider_type')=='chat_completion' for x in providers):raise ValueError('Existing Chat provider requires explicit native configuration')
        token_path=root/'private/onebot-token.txt'
        token=token_path.read_text().strip() if token_path.exists() else secrets.token_hex(32)
        platform={'id':'feiyu-onebot','type':'aiocqhttp','enable':True,'ws_reverse_host':'127.0.0.1','ws_reverse_port':port,'ws_reverse_token':token}
        old_platform=next((x for x in platforms if x.get('id')==platform['id']),None)
        if old_platform is not None and old_platform!=platform:raise ValueError('Existing OneBot settings differ; no overwrite')
        if not onebot_only and old is None:providers.append(provider)
        if old_platform is None:platforms.append(platform)
        if not onebot_only:cfg.setdefault('agent_runner',{}).setdefault('config',{}).setdefault('model',{})['provider_id']='fat-fish-chat'
        history=root/'private/config-history'/('chat-'+secrets.token_hex(6));history.mkdir(parents=True)
        (history/'cmd_config.json').write_bytes(before)
        if not token_path.exists():token_path.write_text(token,encoding='utf-8')
        pending=path.with_suffix('.pending');pending.write_text(json.dumps(cfg,ensure_ascii=False),encoding='utf-8');pending.replace(path)
    return {'configured':True,'secret_values_printed':False,'group_permissions_changed':False,'onebot_loopback_port':port,'api_key_required':'private/deepseek-key.txt','manual_step':'Configure NapCat reverse WebSocket to this port using private/onebot-token.txt; login manually.'}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--instance',type=Path,required=True);p.add_argument('--onebot-port',type=int,default=6199);p.add_argument('--model',default='deepseek-chat');p.add_argument('--onebot-only',action='store_true');args=p.parse_args()
    print(json.dumps(configure(args.instance,args.onebot_port,args.model,args.onebot_only)))
