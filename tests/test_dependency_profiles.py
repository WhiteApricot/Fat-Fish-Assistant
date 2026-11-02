"""Profile, cache retention and migration boundary regression; synthetic only."""
import sys,json,tempfile,unittest,hashlib
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import dependencies as d
import controller as c
class Profiles(unittest.TestCase):
 def test_interrupted_extension_preserves_rollback_inventory(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);python=root/'env/Scripts/python.exe';python.parent.mkdir(parents=True);python.touch()
   marker=python.parents[1]/'ffa-environment.json';original=json.dumps({'profile':'core','lock_sha256':'core-lock'}).encode();marker.write_bytes(original)
   wheel=root/'optional.whl';wheel.write_bytes(b'synthetic wheel')
   base={'name':'base','version':'1','format':'wheel','file':'base.whl','sha256':'0'*64}
   optional={'name':'optional','version':'1','format':'wheel','file':wheel.name,'sha256':d.digest(wheel)}
   def manifest(name):return [base] if name=='core.json' else [base,optional]
   calls=[]
   def interrupted(args,*a,**k):
    if 'install' in args:raise KeyboardInterrupt('synthetic interruption after writes')
   with patch.object(d,'load',side_effect=manifest),patch.object(d,'profile_fingerprint',side_effect=lambda n:n+'-lock'),patch.object(d,'verify_environment_versions'),patch.object(d,'acquire',return_value=wheel),patch.object(d.subprocess,'run',return_value=SimpleNamespace(stdout='{"base":"1"}')),patch.object(d,'run_logged',side_effect=interrupted):
    with self.assertRaises(KeyboardInterrupt):d.extend_core_environment(python,root/'cache')
   self.assertEqual(marker.read_bytes(),original)
   with patch.object(d,'load',side_effect=manifest),patch.object(d,'profile_fingerprint',side_effect=lambda n:n+'-lock'),patch.object(d,'verify_environment_versions'),patch.object(d,'acquire',return_value=wheel),patch.object(d.subprocess,'run',return_value=SimpleNamespace(stdout='{"base":"1","optional":"1"}')),patch.object(d,'run_logged',side_effect=lambda args,*a,**k:calls.append(args)):
    d.extend_core_environment(python,root/'cache')
    self.assertEqual(json.loads(marker.read_text())['extension']['added'],['optional'])
    d.rollback_optional_environment(python)
   self.assertEqual(marker.read_bytes(),original)
   uninstall=next(args for args in calls if 'uninstall' in args)
   self.assertEqual(uninstall[-1],'optional');self.assertNotIn('base',uninstall)
 def test_core_is_version_locked_subset(self):
  full={r['name']:r for r in d.load('chat.json')};core=d.load('core.json')
  self.assertLess(len(core),len(full))
  for r in core:self.assertEqual(r,full[r['name']])
  self.assertTrue({'faiss-cpu','numpy','pillow','aiocqhttp','imageio-ffmpeg','opentelemetry-sdk'}<={r['name'].lower().replace('_','-') for r in core})
 def test_default_and_optional_selection(self):
  self.assertIsNone(c.parser().parse_args(['install']).dependency_profile)
  self.assertEqual(c.parser().parse_args(['install','--dependency-profile','chat']).dependency_profile,'chat')
 def test_shared_asset_remains_after_cache_eviction(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);cache=root/'cache';cache.mkdir();source=cache/'fixed.zip';source.write_bytes(b'fixed')
   target=root/'resource';d.link_or_copy(source,target)
   spec={'file':'fixed.zip','sha256':hashlib.sha256(b'fixed').hexdigest()}
   (cache/'user.txt').write_text('keep');(cache/'untrusted.zip').write_bytes(b'unknown')
   with patch.object(d,'load',return_value=[spec]):d.prune_download_cache(cache)
   self.assertFalse(source.exists());self.assertEqual(target.read_bytes(),b'fixed');self.assertTrue((cache/'user.txt').exists());self.assertTrue((cache/'untrusted.zip').exists())
 def test_profile_receipt_roles(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);env=root/'env';(env/'Scripts').mkdir(parents=True)
   (env/'ffa-environment.json').write_text(json.dumps({'profile':'speech'}))
   with self.assertRaises(c.Fault):c.environment_profile(root,{'chat_python':'env/Scripts/python.exe'},'chat')
if __name__=='__main__':unittest.main(verbosity=2)
