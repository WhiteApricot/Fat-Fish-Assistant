"""Loopback-only SenseVoice adapter for AstrBot's existing STT API client."""
import audioop
import os
import hmac
import io
import json
import threading
import wave
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import sherpa_onnx

ROOT=Path(os.environ['FEIYU_RUNTIME_ROOT']).resolve()
MODEL=ROOT/'models/sensevoice/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09'


def pcm_samples(data):
    with wave.open(io.BytesIO(data)) as source:
        width, channels, rate=source.getsampwidth(),source.getnchannels(),source.getframerate()
        frames=source.getnframes()
        if width != 2 or channels not in (1,2) or rate <= 0 or frames/rate > 120:
            raise ValueError('Expected <=120 seconds of 16-bit mono/stereo PCM WAV')
        pcm=source.readframes(frames)
    if channels==2:pcm=audioop.tomono(pcm,2,0.5,0.5)
    if rate!=16000:pcm,_=audioop.ratecv(pcm,2,1,rate,16000,None)
    return np.frombuffer(pcm,dtype=np.int16).astype(np.float32)/32768


def uploaded_wav(content_type, body):
    if not content_type.lower().startswith('multipart/form-data;'):
        raise ValueError('Expected multipart form data')
    message=BytesParser(policy=policy.default).parsebytes(
        ('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+body)
    files=[part.get_payload(decode=True) for part in message.iter_parts()
           if part.get_param('name',header='content-disposition')=='file']
    if len(files)!=1 or not files[0]:raise ValueError('Expected exactly one nonempty audio file')
    return files[0]


def serve():
    cfg=json.loads((ROOT/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
    selected=next(p for p in cfg['provider'] if p['id']==cfg['provider_stt_settings']['provider_id'])
    key=selected.get('api_key','')
    if not key:raise RuntimeError('Local STT API key is required')
    recognizer=sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(MODEL/'model.int8.onnx'),tokens=str(MODEL/'tokens.txt'),
        num_threads=2,language='zh',use_itn=False,provider='cpu')
    lock=threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self,*args):pass  # No audio, transcripts or tokens in logs.

        def reply(self,status,body):
            data=json.dumps(body,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            return hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+key)

        def do_GET(self):
            if not self.authorized():return self.reply(401,{'error':'Unauthorized'})
            if self.path=='/health':return self.reply(200,{'status':'ok','model':'sensevoice-int8-20250909'})
            return self.reply(404,{'error':'Not found'})

        def do_POST(self):
            if not self.authorized():return self.reply(401,{'error':'Unauthorized'})
            if self.path!='/v1/audio/transcriptions':return self.reply(404,{'error':'Not found'})
            try:length=int(self.headers.get('Content-Length','0'))
            except ValueError:return self.reply(400,{'error':'Invalid length'})
            if not 0<length<=16*1024*1024:return self.reply(413,{'error':'Audio too large or empty'})
            if not lock.acquire(blocking=False):return self.reply(503,{'error':'Speech service busy'})
            try:
                body=self.rfile.read(length)
                if len(body)!=length:raise ValueError('Incomplete upload')
                samples=pcm_samples(uploaded_wav(self.headers.get('Content-Type',''),body))
                stream=recognizer.create_stream()
                stream.accept_waveform(16000,samples)
                recognizer.decode_stream(stream)
                self.reply(200,{'text':stream.result.text})
            except (ValueError,wave.Error,EOFError):
                self.reply(400,{'error':'Invalid PCM WAV audio'})
            except Exception:
                self.reply(500,{'error':'Local speech recognition failed'})
            finally:lock.release()

    port=int(os.environ.get('FEIYU_SPEECH_PORT','6202'))
    if not 1<=port<=65535:raise ValueError('Invalid local speech port')
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads=True
    print('Local SenseVoice ready on loopback',flush=True)
    server.serve_forever()


if __name__=='__main__':serve()
