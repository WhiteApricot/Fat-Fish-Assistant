"""Show a removed payload in an ancestor is still rejected; synthetic files only."""
import json,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from audit_release import audit

class Audit(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_known_secret_is_not_printed(self):
        value='synthetic-specific-credential-for-negative-test';(self.root/'sample.txt').write_text(value)
        result=audit(self.root,[value],False)
        self.assertFalse(result['passed']);self.assertNotIn(value,json.dumps(result))
    def test_copied_third_party_path_rejected(self):
        target=self.root/'src/p2d/upstream_selfreply';target.mkdir(parents=True);(target/'decision.py').write_text('# synthetic, not upstream code')
        self.assertFalse(audit(self.root,include_git=False)['passed'])
    @unittest.skipUnless(shutil.which('git'),'Git unavailable; history test requires Git')
    def test_ancestor_payload_is_rejected_after_head_rename(self):
        def git(*args):return subprocess.run(['git','-C',str(self.root),'-c','core.hooksPath='+str(self.root/'empty-hooks'),*args],capture_output=True,check=True)
        git('-c','init.templateDir=','init','--quiet');git('config','user.name','Synthetic Test');git('config','user.email','test@example.invalid');git('config','commit.gpgsign','false')
        (self.root/'payload.zip').write_text('synthetic archive marker, no real payload');git('add','.');git('commit','--quiet','-m','fixture payload')
        (self.root/'payload.zip').rename(self.root/'plain.txt');git('add','--all');git('commit','--quiet','-m','head without archive')
        result=audit(self.root)
        self.assertEqual(result['history_commits'],2);self.assertFalse(result['passed'])
        self.assertTrue(any(x['path']=='payload.zip' and x['source'].startswith('history:') for x in result['unresolved_findings']))

if __name__=='__main__':unittest.main(verbosity=2)
