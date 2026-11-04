"""Initialize an owned, stopped NapCat reverse-WS client; never logs in or sends."""
import argparse,json,secrets
from pathlib import Path

def configure(root,bot_id):
    root=Path(root).resolve()
    if not bot_id.isascii() or not bot_id.isdecimal():raise ValueError('Invalid Bot identity')
    def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))
    marker=read(root/'.ffa-managed.json');record=read(root/'installation.json')
    if marker.get('project')!='Fat-Fish-Assistant':raise ValueError('Unmanaged root')
    info=record.get('components',{}).get('napcat',{})
    if not info.get('installed') or info.get('ownership')!='managed':raise ValueError('NapCat must be owned')
    service=(root/info['path']).resolve();service.relative_to(root)
    if (service/'.feiyu-process.json').exists():raise ValueError('Stop owned NapCat before configuration')
    instance=(root/record['instance']).resolve();instance.relative_to(root)
    identity=instance/'private/bot-identity.json'
    if identity.exists() and read(identity).get('bot_id')!=bot_id:raise ValueError('Existing Bot identity differs; no automatic replacement')
    cfg=read(instance/'data/cmd_config.json');platform=next(x for x in cfg['platform'] if x.get('id')=='feiyu-onebot')
    if platform['ws_reverse_host']!='127.0.0.1':raise ValueError('OneBot must use loopback')
    token=(instance/'private/onebot-token.txt').read_text().strip()
    if not token or platform['ws_reverse_token']!=token:raise ValueError('OneBot credential mismatch')
    value={'network':{'httpServers':[],'httpSseServers':[],'httpClients':[],'websocketServers':[],'websocketClients':[{'name':'Fat-Fish-Assistant','enable':True,'messagePostFormat':'array','token':token,'url':f"ws://127.0.0.1:{platform['ws_reverse_port']}/ws",'reportSelfMessage':False,'reconnectInterval':3000,'heartInterval':30000}],'plugins':[]},'musicSignUrl':'','enableLocalFile2Url':False,'parseMultMsg':False,'imageDownloadProxy':''}
    destination=service/'config'/('onebot11_'+bot_id+'.json');destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists() and read(destination)!=value:raise ValueError('Existing OneBot config differs; use native configuration UI')
    if not destination.exists():
        temp=destination.with_suffix('.pending-'+secrets.token_hex(4));temp.write_text(json.dumps(value),encoding='utf-8');temp.replace(destination)
    if not identity.exists():identity.write_text(json.dumps({'bot_id':bot_id}),encoding='utf-8')
    return {'configured':True,'loopback_only':True,'credentials_printed':False,'login_automated':False,'manual_step':'Open the owned NapCat WebUI and scan its QQ login code.'}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--bot-id',required=True);a=p.parse_args();print(json.dumps(configure(a.root,a.bot_id)))
