"""Supply-chain and patch rejection checks; no models, QQ or data migration."""
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
import importlib.metadata
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import dependencies as dep

class Dependencies(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_corrupt_cache_is_not_replaced(self):
        cache=self.root/'cache';cache.mkdir();payload=cache/'source.zip';payload.write_bytes(b'wrong')
        spec={'file':payload.name,'sha256':hashlib.sha256(b'correct').hexdigest(),'url':'https://example.invalid/source.zip'}
        with patch.object(dep.urllib.request,'urlopen') as network:
            with self.assertRaises(ValueError):dep.acquire(spec,cache)
            network.assert_not_called()
        self.assertEqual(payload.read_bytes(),b'wrong')
    def test_orphan_build_blocks_partial_environment_rename(self):
        target=self.root/'env';target.mkdir()
        marker=target/'ffa-environment-building.json'
        marker.write_text(json.dumps({'lock_sha256':dep.profile_fingerprint('chat')}))
        with patch.object(dep,'ensure_build_quiet',side_effect=RuntimeError('synthetic active builder')):
            with self.assertRaises(RuntimeError):dep.install_environment(Path(sys.executable),target,self.root/'cache','chat',True)
        self.assertTrue(marker.exists());self.assertEqual(list(self.root.glob('env.failed-*')),[])
    @unittest.skipUnless(os.name=='nt','Windows build writer probe')
    def test_actual_build_writer_probe(self):
        destination=self.root/'interrupted-env';destination.mkdir()
        child=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-c','import time;time.sleep(120)',str(destination)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            time.sleep(.3)
            with self.assertRaises(RuntimeError):dep.ensure_build_quiet(destination)
        finally:
            child.terminate();child.wait(timeout=15)
        dep.ensure_build_quiet(destination)
    def test_offline_missing_never_connects(self):
        with patch.object(dep.urllib.request,'urlopen') as network:
            with self.assertRaises(FileNotFoundError):dep.acquire({'file':'missing.zip','sha256':'0'*64},self.root/'cache',True)
            network.assert_not_called()
    def test_repository_payload_refused(self):
        with self.assertRaises(ValueError):dep.external(dep.PROJECT/'cache')
    def test_actual_installed_version_drift_is_refused(self):
        log=self.root/'versions.log'
        with self.assertRaises(RuntimeError):dep.verify_environment_versions(sys.executable,[{'name':'pip','version':'0.synthetic-mismatch'}],log)
        self.assertIn('Pinned dependency version mismatch',log.read_text())
    def test_normalized_installed_package_name(self):
        log=self.root/'versions.log'
        dep.verify_environment_versions(sys.executable,[{'name':'PIP','version':importlib.metadata.version('pip')}],log)
        self.assertIn('All pinned dependency versions match',log.read_text())
    def test_path_escape_and_windows_ads(self):
        for value in ['../outside','C:/outside','/outside','file:secret','..\\outside']:
            with self.subTest(value=value),self.assertRaises(ValueError):dep.safe_path(self.root,value)
    def test_archive_traversal(self):
        archive=self.root/'bad.zip'
        with zipfile.ZipFile(archive,'w') as z:z.writestr('../outside','not allowed')
        with self.assertRaises(ValueError):dep.unpack(archive,self.root/'output')
        self.assertFalse((self.root/'outside').exists())
    def test_real_patch_before_after_and_mismatch(self):
        target=self.root/'target';target.mkdir();file=target/'code.py';file.write_bytes(b'a\nb\n')
        changes=[{'path':'code.py','before':dep.digest(file),'after':hashlib.sha256(b'a\nc\n').hexdigest(),'edits':[{'start':1,'end':2,'old':['b\n'],'new':['c\n']}]}]
        project=self.root/'project';project.mkdir();manifest=project/'changes.json';manifest.write_text(json.dumps(changes))
        spec={'patches':'changes.json','patch_sha256':dep.digest(manifest)}
        with patch.object(dep,'PROJECT',project):
            dep.apply_patches(target,spec);self.assertEqual(file.read_bytes(),b'a\nc\n')
            with self.assertRaises(ValueError):dep.apply_patches(target,spec)
            self.assertEqual(file.read_bytes(),b'a\nc\n')
    def test_patch_result_mismatch_does_not_write(self):
        target=self.root/'target';target.mkdir();file=target/'code.py';file.write_bytes(b'a\n')
        manifest=self.root/'bad.json';manifest.write_text(json.dumps([{'path':'code.py','before':dep.digest(file),'after':'0'*64,'edits':[{'start':0,'end':1,'old':['a\n'],'new':['b\n']}]}]))
        with patch.object(dep,'PROJECT',self.root),self.assertRaises(ValueError):dep.apply_patches(target,{'patches':'bad.json','patch_sha256':dep.digest(manifest)})
        self.assertEqual(file.read_bytes(),b'a\n')

if __name__=='__main__':unittest.main(verbosity=2)
