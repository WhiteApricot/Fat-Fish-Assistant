"""Independent authenticated model protocol probes, never semantic quality claims."""
import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

def probe(instance,component):
    cfg=json.loads((instance/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
    providers=cfg.get('provider',[])
    if component=='embedding':
        row=next(x for x in providers if x.get('id')=='feiyu-local-embedding')
        base=row['embedding_api_base'].rstrip('/');key=row['embedding_api_key'];endpoint='/models'
    else:
        row=next(x for x in providers if x.get('id')=='feiyu-local-stt')
        base=row['api_base'].rstrip('/');key=row['api_key'];base=base[:-3] if base.endswith('/v1') else base;endpoint='/health'
    parsed=urllib.parse.urlsplit(base)
    if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','localhost'}:raise ValueError('Only configured loopback model services may be probed')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(path,token,payload=None):
        req=urllib.request.Request(base+path,data=json.dumps(payload).encode() if payload is not None else None,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with opener.open(req,timeout=20) as response:return json.load(response)
    metadata=call(endpoint,key)
    if component=='embedding':
        assert any(x['id']=='feiyu-bge-small-zh' for x in metadata['data'])
        result=call('/embeddings',key,{'model':'feiyu-bge-small-zh','input':'合成健康检查'})
        assert len(result['data'][0]['embedding'])==512
    else:assert metadata['model']=='sensevoice-int8-20250909'
    try:call(endpoint,'synthetic-invalid-credential')
    except urllib.error.HTTPError as exc:assert exc.code in {401,403}
    else:raise AssertionError('Invalid credential accepted')
    return {'component':component,'authenticated_identity':'pass','invalid_credentials_rejected':'pass','embedding_dimensions':512 if component=='embedding' else None,'limits':'Protocol and identity only; no semantic recall or speech accuracy claim'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--instance',required=True,type=Path);p.add_argument('--component',required=True,choices=['embedding','speech']);a=p.parse_args()
    print(json.dumps(probe(a.instance,a.component)))
