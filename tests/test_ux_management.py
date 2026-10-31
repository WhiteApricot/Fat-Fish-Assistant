"""UX orchestration failure boundaries and interrupted-install ownership proof."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import controller as c

class UX(unittest.TestCase):
    def test_stuck_owned_login_restarts_only_after_confirmation(self):
        record={'chat_python':sys.executable,'components':{'napcat':{'installed':True,'runtime':'node','ownership':'managed','path':'services/node/napcat'}}}
        stuck={'loginPhase':'reconnecting','isLogin':False,'qrLoginAccepted':False}
        ready={'loginPhase':'qrcode','isLogin':False,'qrLoginAccepted':False}
        with patch.object(c,'invoke',side_effect=[stuck,stuck,ready]),patch.object(c.time,'sleep'),patch.object(c,'operate',return_value={'operations':[{'id':'napcat'}]}) as operate:
            result=c.recover_napcat_login(Path('synthetic'),record)
        self.assertTrue(result['restarted']);operate.assert_called_once_with(Path('synthetic'),record,'restart',['napcat'])
        for state in [ready,{'loginPhase':'initializing','qrLoginAccepted':True},{'loginPhase':'ready','isLogin':True}]:
            with patch.object(c,'invoke',return_value=state),patch.object(c,'operate') as operate:
                self.assertFalse(c.recover_napcat_login(Path('synthetic'),record)['restarted'])
                operate.assert_not_called()
        with patch.object(c,'invoke',side_effect=[stuck,ready]),patch.object(c.time,'sleep'),patch.object(c,'operate') as operate:
            self.assertFalse(c.recover_napcat_login(Path('synthetic'),record)['restarted']);operate.assert_not_called()
        record['components']['napcat']['ownership']='external-readonly'
        with patch.object(c,'invoke') as probe,self.assertRaises(c.Fault):c.recover_napcat_login(Path('synthetic'),record)
        probe.assert_not_called()

    def test_windows_utf8_stdin_bom(self):
        import io
        with patch.object(sys,'stdin',io.StringIO('\ufeff{"password":"合成口令"}')):
            self.assertEqual(c.read_stdin()['password'],'合成口令')
    def test_low_disk_and_network_failures_are_reported(self):
        import urllib.error
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp,patch.object(c.shutil,'disk_usage',return_value=SimpleNamespace(free=0)),patch.object(c.socket,'create_connection',side_effect=ConnectionRefusedError),patch.object(c.urllib.request,'urlopen',side_effect=urllib.error.URLError('synthetic offline')):
            result=c.inspect_environment(Path(tmp),network=True)
        self.assertFalse(result['ready'])
        self.assertFalse(next(x for x in result['checks'] if x['name']=='disk_space')['ok'])
        self.assertTrue(result['network_tested'])
        self.assertTrue(all(not x['ok'] for x in result['checks'] if x['name'].startswith('network:')))

    @unittest.skipUnless(c.os.name=='nt','Windows runtime inspection')
    def test_missing_runtime_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp,patch('ctypes.WinDLL',side_effect=OSError('synthetic missing runtime')),patch.object(c.socket,'create_connection',side_effect=ConnectionRefusedError):
            result=c.inspect_environment(Path(tmp))
        self.assertFalse(result['ready'])
        missing=next(x for x in result['checks'] if x['name']=='microsoft_runtime')
        self.assertEqual(len(missing['detail']['missing']),3)

    def test_optional_failure_still_starts_core(self):
        record={'components':{'embedding':{'installed':True},'astrbot':{'installed':True}}}
        calls=[]
        def operate(root,record,action,names):
            calls.extend(names)
            if names==['embedding']:raise c.Fault('failed','synthetic model unavailable')
            return {'operations':[{'id':'astrbot'}]}
        with patch.object(c,'verify_runtime_environment'),patch.object(c,'operate',side_effect=operate),patch.object(c,'invoke',return_value={'authenticated_dashboard':'pass'}),patch.object(c,'chat_python'),patch.object(c,'instance'):
            result=c.daily_start(Path('.'),record)
        self.assertEqual(calls,['embedding','astrbot']);self.assertTrue(result['core_ready']);self.assertEqual(len(result['warnings']),1)

    def test_suspended_source_refuses_before_any_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'instance/private').mkdir(parents=True)
            (root/'instance/private/MIGRATED-OUT.json').write_text('{}')
            record={'instance':'instance','components':{'napcat':{'installed':True}}}
            with patch.object(c,'component_status') as status,self.assertRaises(c.Fault):c.operate(root,record,'start')
            status.assert_not_called()

    def test_interrupted_registration_resumes_only_with_matching_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=c.owned_root(Path(tmp)/'installation',True)
            args=c.parser().parse_args(['--root',str(root),'install','--owner','100','--components','chat','--plugins','','--offline'])
            original=c.atomic;init_calls=[]
            def build(release,*a,**kw):
                release.mkdir(parents=True);(release/'plugins').mkdir()
                original(release/'payload-manifest.json',[]);original(release/'release.json',{'vendor':[]})
            def invoke(root,python,script,*argv,**kw):
                if script=='instance.py':
                    stage=Path(argv[argv.index('--destination')+1]);(stage/'private').mkdir(parents=True);(stage/'data').mkdir()
                    original(stage/'private/instance-identity.json',{'owner':'100'});original(stage/'instance.json',{'release':str(argv[argv.index('--release')+1])})
                    original(stage/'data/cmd_config.json',{'dashboard':{'host':'127.0.0.1','port':16185}})
                    init_calls.append(stage)
                return {'checked':True}
            fail=[True]
            def atomic(path,data):
                if Path(path)==root/'installation.json' and data.get('instance') and fail[0]:
                    fail[0]=False;raise OSError('Synthetic interruption after instance activation, before registration')
                original(path,data)
            with patch('instance.protect_boundary'),patch.object(c,'inspect_environment',return_value={'ready':True,'checks':[]}),patch.object(c,'install_environment',return_value=Path(sys.executable)),patch.object(c,'build_release',side_effect=build),patch.object(c,'invoke',side_effect=invoke),patch.object(c,'atomic',side_effect=atomic):
                with self.assertRaises(OSError):c.install(root,args)
                user=root/'instance/user-chat.txt';user.write_text('synthetic newer chat')
                c.install(root,args)
                self.assertEqual(len(init_calls),1);self.assertEqual(user.read_text(),'synthetic newer chat')
                record=c.state(root);record.pop('instance');original(root/'installation.json',record)
                (root/'instance/private/install-origin.json').unlink()
                with self.assertRaises(c.Fault):c.install(root,args)
                self.assertEqual(user.read_text(),'synthetic newer chat')

if __name__=='__main__':unittest.main(verbosity=2)
