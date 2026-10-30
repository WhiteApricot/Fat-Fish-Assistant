import json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from configure_chat import configure as chat
from configure_onebot import configure

class OneBotConfiguration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.instance=self.root/'instance';self.service=self.root/'services/napcat'
        (self.instance/'private').mkdir(parents=True);(self.instance/'data').mkdir();self.service.mkdir(parents=True)
        (self.root/'.ffa-managed.json').write_text(json.dumps({'project':'Fat-Fish-Assistant'}))
        (self.root/'installation.json').write_text(json.dumps({'instance':'instance','components':{'napcat':{'installed':True,'ownership':'managed','path':'services/napcat'}}}))
        (self.instance/'data/cmd_config.json').write_text('{"provider":[],"platform":[]}');chat(self.instance,16199)
    def tearDown(self):self.tmp.cleanup()
    def test_repeat_retains_shared_token_and_loopback(self):
        configure(self.root,'100');path=self.service/'config/onebot11_100.json';before=path.read_bytes();configure(self.root,'100');self.assertEqual(before,path.read_bytes())
        client=json.loads(before)['network']['websocketClients'][0]
        self.assertEqual(client['url'],'ws://127.0.0.1:16199/ws');self.assertEqual(client['token'],(self.instance/'private/onebot-token.txt').read_text());self.assertFalse(client['reportSelfMessage'])
    def test_identity_and_existing_config_not_overwritten(self):
        configure(self.root,'100')
        with self.assertRaises(ValueError):configure(self.root,'200')
        path=self.service/'config/onebot11_100.json';path.write_text('{}')
        with self.assertRaises(ValueError):configure(self.root,'100')
        self.assertEqual(path.read_text(),'{}')
    def test_active_or_escaped_installation_refused(self):
        marker=self.service/'.feiyu-process.json';marker.write_text('{}')
        with self.assertRaises(ValueError):configure(self.root,'100')
        marker.unlink();(self.root/'installation.json').write_text(json.dumps({'instance':'instance','components':{'napcat':{'installed':True,'ownership':'managed','path':'../escaped'}}}))
        with self.assertRaises(ValueError):configure(self.root,'100')
if __name__=='__main__':unittest.main(verbosity=2)
