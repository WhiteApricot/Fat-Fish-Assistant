"""Actual HTTP protocol against synthetic loopback services, no real models."""
import json,sys,tempfile,threading,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from service_health import probe

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def answer(self,value):
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(value).encode())
    def do_GET(self):
        if self.headers.get('Authorization')!='Bearer synthetic-local-key':self.send_error(401);return
        self.answer({'model':'sensevoice-int8-20250909'} if self.path=='/health' else {'data':[{'id':'feiyu-bge-small-zh'}]})
    def do_POST(self):
        if self.headers.get('Authorization')!='Bearer synthetic-local-key':self.send_error(401);return
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));assert body['model']=='feiyu-bge-small-zh'
        self.answer({'data':[{'embedding':[0.0]*512}]})

class Health(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'data').mkdir()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
        base=f'http://127.0.0.1:{self.server.server_port}/v1'
        self.cfg={'provider':[{'id':'feiyu-local-embedding','embedding_api_base':base,'embedding_api_key':'synthetic-local-key'},{'id':'feiyu-local-stt','api_base':base,'api_key':'synthetic-local-key'}]}
        self.save()
    def save(self):(self.root/'data/cmd_config.json').write_text(json.dumps(self.cfg))
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def test_embedding_independent(self):self.assertEqual(probe(self.root,'embedding')['embedding_dimensions'],512)
    def test_speech_independent(self):self.assertEqual(probe(self.root,'speech')['authenticated_identity'],'pass')
    def test_remote_endpoint_refused_before_connect(self):
        self.cfg['provider'][0]['embedding_api_base']='http://example.invalid/v1';self.save()
        with self.assertRaises(ValueError):probe(self.root,'embedding')

if __name__=='__main__':unittest.main(verbosity=2)
