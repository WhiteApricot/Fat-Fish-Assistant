import sys,io,json,tempfile,hashlib,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import dependencies as d
class Response(io.BytesIO):
 url='https://mirror.example/file'
 headers={'Content-Length':'131072'}
@patch.dict(d.os.environ, {"FFA_TRANSFER_PROGRESS":"1"})
class Transfer(unittest.TestCase):
 def test_chunked_progress_and_integrity(self):
  content=b'x'*131072;events=[]
  with tempfile.TemporaryDirectory() as t,patch.object(d.urllib.request,'urlopen',return_value=Response(content)):
   result=d.acquire({'file':'test.whl','url':Response.url,'sha256':hashlib.sha256(content).hexdigest()},Path(t),progress=lambda step,item:events.append((step,item)))
   self.assertEqual(result.read_bytes(),content)
  frames=[json.loads(item) for step,item in events if step=='transfer']
  self.assertEqual(frames[0]['bytes'],0);self.assertEqual(frames[-1]['bytes'],len(content));self.assertTrue(frames[-1]['done'])
  self.assertEqual(frames[-1]['source'],'mirror.example');self.assertEqual(frames[-1]['eta_seconds'],0)
 def test_unknown_size_is_not_fabricated(self):
  response=Response(b'x');response.headers={};events=[]
  with tempfile.TemporaryDirectory() as t,patch.object(d.urllib.request,'urlopen',return_value=response):
   d.acquire({'file':'test','url':Response.url,'sha256':hashlib.sha256(b'x').hexdigest()},Path(t),progress=lambda step,item:events.append((step,item)))
  last=json.loads([item for step,item in events if step=='transfer'][-1]);self.assertEqual(last['total'],0);self.assertIsNone(last['eta_seconds'])
if __name__=='__main__':unittest.main(verbosity=2)
