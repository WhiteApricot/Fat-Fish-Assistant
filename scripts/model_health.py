"""Authenticated local model identity checks using synthetic input only."""
import argparse
import io
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
import wave
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--instance',type=Path,required=True);a=p.parse_args();root=a.instance
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
cfg=json.loads((root/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
stt=next(x for x in cfg['provider'] if x['id']==cfg['provider_stt_settings']['provider_id'])
embedding=next(x for x in cfg['provider'] if x.get('type')=='openai_embedding' and x.get('enable',True))
embedding_key=embedding['embedding_api_key']
embedding_base=embedding['embedding_api_base'].rstrip('/')
speech_base=stt['api_base'].rstrip('/')
for base in [embedding_base,speech_base]:
    parsed=urlparse(base)
    if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','localhost'}:raise ValueError('Local model diagnostics require loopback HTTP endpoints')
parsed=urlparse(speech_base)
speech_origin=parsed.scheme+'://'+parsed.netloc
def request(url,key,body=None,content_type='application/json'):
    req=urllib.request.Request(url,data=body,headers={'Authorization':'Bearer '+key,'Content-Type':content_type})
    with opener.open(req,timeout=30) as response:return json.load(response)
for attempt in range(30):
    try:
        embedding=request(embedding_base+'/models',embedding_key)
        speech=request(speech_origin+'/health',stt['api_key'])
        break
    except (OSError,urllib.error.URLError):
        if attempt==29:raise
        time.sleep(1)
assert any(x['id']=='feiyu-bge-small-zh' for x in embedding['data'])
assert speech['model']=='sensevoice-int8-20250909'
vectors=request(embedding_base+'/embeddings',embedding_key,json.dumps({'model':'feiyu-bge-small-zh','input':'合成测试海洋'}).encode())
assert len(vectors['data'][0]['embedding'])==512
for url in [embedding_base+'/models',speech_origin+'/health']:
    try:request(url,'invalid-fixture-key')
    except urllib.error.HTTPError as exc:assert exc.code in {401,403}
    else:raise AssertionError('Unauthenticated request accepted')
buf=io.BytesIO()
with wave.open(buf,'wb') as audio:
    audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000);audio.writeframes(b'\0'*32000)
boundary='feiyu-synthetic-audio-boundary'
body=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="fixture.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode()+buf.getvalue()+f'\r\n--{boundary}--\r\n'.encode()
transcription=request(speech_base+'/audio/transcriptions',stt['api_key'],body,'multipart/form-data; boundary='+boundary)
assert isinstance(transcription['text'],str)
print(json.dumps({'embedding_identity':'pass','embedding_dimensions':512,'speech_identity':'pass','unauthenticated_rejected':'pass','synthetic_pcm_request':'pass','limits':'Silence fixture tests protocol, not real speech accuracy'}))
