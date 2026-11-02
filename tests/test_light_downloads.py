"""Synthetic mirror failure, corruption and offline checks; no external service."""
import hashlib,io,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import dependencies as d
import controller as c
class Response(io.BytesIO):
    url='https://official.example/payload'
class Downloads(unittest.TestCase):
    def test_runtime_lock_does_not_disable_code_integrity(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);cache=root/'cache';cache.mkdir();archive=cache/'runtime.zip'
            with zipfile.ZipFile(archive,'w') as z:z.writestr('index.js','pinned code');z.writestr('napcat/napcat.mjs','pinned core')
            spec={'id':'synthetic','file':archive.name,'sha256':d.digest(archive),'format':'zip'}
            target=root/'service'
            c.install_asset(root,'napcat-node',spec,target,cache,True)
            (target/'napcat/.feiyu-process.lock').touch()
            c.install_asset(root,'napcat-node',spec,target,cache,True)
            (target/'index.js').write_text('tampered')
            with self.assertRaises(c.Fault):c.install_asset(root,'napcat-node',spec,target,cache,True)
    def test_fallback(self):
        for failure in [OSError('unavailable'),Response(b'corrupt')]:
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as t:
                spec={'file':'a.zip','sha256':hashlib.sha256(b'correct').hexdigest(),'mirrors':['https://mirror.example/a'],'url':'https://official.example/a'}
                with patch.object(d.urllib.request,'urlopen',side_effect=[failure,Response(b'correct')]) as request:
                    self.assertEqual(d.acquire(spec,Path(t)).read_bytes(),b'correct')
                    self.assertEqual(request.call_count,2)
                self.assertEqual(list(Path(t).glob('*.part-*')),[])
    def test_all_fail(self):
        with tempfile.TemporaryDirectory() as t,patch.object(d.urllib.request,'urlopen',side_effect=OSError('down')):
            with self.assertRaises(RuntimeError):d.acquire({'file':'a','sha256':'0'*64,'url':'https://official.example/a'},t)
            self.assertEqual(list(Path(t).iterdir()),[])
    def test_links(self):
        with tempfile.TemporaryDirectory() as t:
            a=Path(t)/'a';a.write_bytes(b'payload');b=Path(t)/'b';d.link_or_copy(a,b)
            self.assertEqual(a.read_bytes(),b.read_bytes())
if __name__=='__main__':unittest.main(verbosity=2)
