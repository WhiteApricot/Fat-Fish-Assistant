import json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from configure_chat import configure

class ChatConfiguration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'private').mkdir();(self.root/'data').mkdir()
        (self.root/'data/cmd_config.json').write_text(json.dumps({'platform':[],'provider':[]}))
        (self.root/'private/p2c-access.json').write_text(json.dumps({'owner':'100','approved_groups':[]}))
    def tearDown(self):self.tmp.cleanup()
    def test_repeat_preserves_token_and_permissions(self):
        before=(self.root/'private/p2c-access.json').read_bytes();configure(self.root);token=(self.root/'private/onebot-token.txt').read_bytes();configure(self.root)
        cfg=json.loads((self.root/'data/cmd_config.json').read_text());self.assertEqual(len(cfg['provider']),1);self.assertEqual(len(cfg['platform']),1)
        self.assertEqual(cfg['platform'][0]['ws_reverse_host'],'127.0.0.1');self.assertEqual(token,(self.root/'private/onebot-token.txt').read_bytes());self.assertEqual(before,(self.root/'private/p2c-access.json').read_bytes())
        self.assertEqual(cfg['provider'][0]['key'],['$DEEPSEEK_API_KEY'])
    def test_running_and_incompatible_configuration_refused(self):
        marker=self.root/'private/RUNNING';marker.write_text('fixture')
        with self.assertRaises(ValueError):configure(self.root)
        marker.unlink();configure(self.root);before=(self.root/'data/cmd_config.json').read_bytes()
        with self.assertRaises(ValueError):configure(self.root,6200)
        self.assertEqual(before,(self.root/'data/cmd_config.json').read_bytes())
    def test_invalid_port_writes_nothing(self):
        before=(self.root/'data/cmd_config.json').read_bytes()
        with self.assertRaises(ValueError):configure(self.root,80)
        self.assertEqual(before,(self.root/'data/cmd_config.json').read_bytes());self.assertFalse((self.root/'private/onebot-token.txt').exists())
if __name__=='__main__':unittest.main(verbosity=2)
