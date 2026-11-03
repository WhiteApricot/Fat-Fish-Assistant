"""Synthetic localhost authentication and occupied model endpoints only."""
import json,sys,socket,tempfile,threading,unittest,urllib.parse
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from dashboard_login import login_url
from local_model_ports import repair_port
from lifecycle_lock import exclusive

class Handler(BaseHTTPRequestHandler):
 mode='ok';seen=[]
 def log_message(self,*args):pass
 def response(self,data):
  raw=json.dumps({'status':'ok','data':data}).encode();self.send_response(200);self.end_headers();self.wfile.write(raw)
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  self.seen.append((self.path,body))
  if self.mode=='redirect':self.send_response(302);self.send_header('Location','https://example.invalid/');self.end_headers();return
  if self.mode in {'challenge','wrong'}:
   data={'totp_required':True} if self.mode=='challenge' else {}
   self.send_response(401);self.end_headers()
   self.wfile.write(json.dumps({'status':'error','data':data}).encode());return
  self.response({'token':'synthetic-session','username':'synthetic','totp_required':self.mode=='totp','md5_pwd_hint':self.mode=='upgrade'})
 def do_GET(self):
  self.seen.append((self.path,self.headers.get('Authorization')))
  self.response({'content_path':('https://example.invalid/stolen' if self.mode=='remote-page' else '/api/v1/plugins/fat_fish_health/views/launcher/_t/synthetic-scope/index.html')})

class Fixes(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);(self.root/'data').mkdir();(self.root/'private').mkdir()
  self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();Handler.mode='ok';Handler.seen=[]
  (self.root/'private/dashboard-password.txt').write_text('synthetic-password')
  self.cfg={'dashboard':{'host':'127.0.0.1','port':self.server.server_port,'username':'synthetic'}};self.save()
 def save(self):(self.root/'data/cmd_config.json').write_text(json.dumps(self.cfg))
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
 def test_login_token_is_fragment_only(self):
  url=login_url(self.root);parts=urllib.parse.urlsplit(url)
  self.assertNotIn('synthetic-session',parts.path+parts.query);state=json.loads(urllib.parse.unquote(parts.fragment[10:]));self.assertEqual(state['token'],'synthetic-session')
  self.assertEqual(Handler.seen[0][1]['password'],'synthetic-password');self.assertNotIn('synthetic-password',url)
  self.assertEqual(Handler.seen[0][1]['username'],'synthetic')
 def test_authenticated_health_uses_encrypted_credential(self):
  import subprocess
  self.cfg['platform']=[];self.save()
  result=subprocess.run([sys.executable,'-B',str(Path(__file__).resolve().parents[1]/'scripts/authenticated_health.py'),'--instance',str(self.root)],capture_output=True,text=True,check=True)
  self.assertEqual(json.loads(result.stdout)['authenticated_dashboard'],'pass')
  self.assertFalse((self.root/'private/dashboard-password.txt').exists())
 def test_password_update_uses_stdin_and_reopens_without_password(self):
  import subprocess
  cli=[sys.executable,'-B',str(Path(__file__).resolve().parents[1]/'scripts/dashboard_login.py'),'--instance',str(self.root)]
  updated=subprocess.run(cli+['--update'],input=json.dumps({'password':'new-Synthetic密码'}),capture_output=True,text=True,check=True)
  self.assertTrue(json.loads(updated.stdout)['data']['saved'])
  self.assertNotIn('new-Synthetic',updated.stdout+updated.stderr)
  self.assertNotIn(b'new-Synthetic',(self.root/'private/dashboard-credential.dpapi').read_bytes())
  self.assertEqual(Handler.seen[-2][1]['password'],'new-Synthetic密码')
  next_start=subprocess.run(cli,capture_output=True,text=True,check=True)
  self.assertEqual(json.loads(next_start.stdout)['data']['mode'],'automatic')
  self.assertEqual(Handler.seen[-2][1]['password'],'new-Synthetic密码')
 def test_2fa_preserves_password_verification_without_issuing_session(self):
  import subprocess
  cli=[sys.executable,'-B',str(Path(__file__).resolve().parents[1]/'scripts/dashboard_login.py'),'--instance',str(self.root)]
  Handler.mode='challenge'
  updated=subprocess.run(cli+['--update'],input=json.dumps({'password':'synthetic-2fa'}),capture_output=True,text=True,check=True)
  data=json.loads(updated.stdout)['data'];self.assertTrue(data['saved'])
  self.assertEqual(data['mode'],'two_factor');self.assertNotIn('#',data['url'])
  saved=(self.root/'private/dashboard-credential.dpapi').read_bytes()
  again=subprocess.run(cli,capture_output=True,text=True,check=True)
  self.assertEqual(json.loads(again.stdout)['data']['mode'],'two_factor')
  Handler.mode='wrong'
  wrong=subprocess.run(cli+['--update'],input=json.dumps({'password':'incorrect'}),capture_output=True,text=True)
  self.assertNotEqual(wrong.returncode,0)
  self.assertEqual((self.root/'private/dashboard-credential.dpapi').read_bytes(),saved)
 def test_native_security_upgrade_is_preserved(self):
  Handler.mode='upgrade';url=login_url(self.root);state=json.loads(urllib.parse.unquote(urllib.parse.urlsplit(url).fragment[10:]));self.assertTrue(state['md5_pwd_hint'])
 def test_no_auth_redirect_or_remote_page(self):
  for mode in ['redirect','remote-page','totp']:
   Handler.mode=mode
   with self.assertRaises(ValueError):login_url(self.root)
 def test_remote_dashboard_refused(self):
  self.cfg['dashboard']['host']='0.0.0.0';self.save()
  with self.assertRaises(ValueError):login_url(self.root)
 def test_conflicting_model_port_moves_only_endpoint(self):
  self.cfg.update(provider=[{'id':'feiyu-local-stt','api_base':f'http://127.0.0.1:{self.server.server_port}/v1','api_key':'synthetic-key'}],provider_stt_settings={'enable':False,'provider_id':'feiyu-local-stt'})
  self.save();before=(self.root/'data/cmd_config.json').read_bytes();change=repair_port(self.root,'speech');self.assertNotEqual(change['from'],change['to'])
  after=json.loads((self.root/'data/cmd_config.json').read_text());self.assertEqual(after['provider'][0]['api_key'],'synthetic-key');self.assertFalse(after['provider_stt_settings']['enable'])
  self.assertEqual(list((self.root/'private/config-history').glob('*/cmd_config.json'))[0].read_bytes(),before)
 def test_free_model_port_unchanged(self):
  with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
  self.cfg['provider']=[{'id':'feiyu-local-embedding','embedding_api_base':f'http://127.0.0.1:{port}/v1'}];self.save();before=(self.root/'data/cmd_config.json').read_bytes()
  self.assertIsNone(repair_port(self.root,'embedding'));self.assertEqual(before,(self.root/'data/cmd_config.json').read_bytes())
 def test_live_instance_lock_refuses_config_rewrite(self):
  before=(self.root/'data/cmd_config.json').read_bytes()
  with exclusive(self.root):
   with self.assertRaises(RuntimeError):repair_port(self.root,'speech')
  self.assertEqual(before,(self.root/'data/cmd_config.json').read_bytes())

if __name__=='__main__':unittest.main()
