"""Management contract and safety regression. No real component writes or migration."""
import argparse
import contextlib
import io
import json
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import controller as c

class Management(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'managed';c.owned_root(self.root,True)
        target=self.root/'instance';(target/'private').mkdir(parents=True);(target/'data').mkdir()
        c.atomic(target/'private/instance-identity.json',{'owner':'100'})
        c.atomic(target/'private/p2c-access.json',{'owner':'100','approved_groups':[]})
        c.atomic(target/'data/cmd_config.json',{'dashboard':{'host':'127.0.0.1','port':16185},'provider':[]})
        (target/'private/dashboard-password.txt').write_text('synthetic-password-not-real')
        self.record={'schema_version':1,'instance':'instance','components':{},'chat_python':str(Path(sys.executable))}
        c.atomic(self.root/'installation.json',self.record)
    def tearDown(self):self.temp.cleanup()
    def call(self,*args,stdin=None):
        stream=io.StringIO()
        with patch.object(sys,'stdin',io.StringIO(stdin or '')),contextlib.redirect_stdout(stream):code=c.main(['--root',str(self.root),*args])
        value=json.loads(stream.getvalue());self.assertEqual(code,value['code']);self.assertEqual(value['schema_version'],1);return code,value
    def test_json_error_contract(self):
        code,value=self.call('not-a-command');self.assertEqual(code,20);self.assertFalse(value['ok'])
    def test_nonempty_root_is_not_adopted(self):
        other=Path(self.temp.name)/'external';other.mkdir();(other/'file').write_text('keep')
        with self.assertRaises(c.Fault):c.owned_root(other,True)
        self.assertEqual((other/'file').read_text(),'keep');self.assertFalse((other/'.ffa-managed.json').exists())
    def test_unknown_contract_version(self):
        c.atomic(self.root/'.ffa-managed.json',{'schema_version':999,'project':'Fat-Fish-Assistant'})
        with self.assertRaises(c.Fault):c.owned_root(self.root)
    def test_reserved_components_are_not_implemented(self):
        for name in ['dagu','work']:
            with self.assertRaises(c.Fault):c.selection(name)
        code,value=self.call('components');self.assertEqual(code,0)
        future=[x for x in value['data']['components'] if x['id'] in {'dagu','work'}]
        self.assertTrue(all(x['reserved'] and not x['installed'] for x in future))
    def test_owner_and_groups_are_not_generic_settings(self):
        before=(self.root/'instance/private/p2c-access.json').read_bytes()
        code,_=self.call('config','apply',stdin=json.dumps({'owner':'200','approved_groups':['900']}));self.assertEqual(code,20)
        self.assertEqual((self.root/'instance/private/p2c-access.json').read_bytes(),before)
    def test_configuration_output_redacts_secrets(self):
        code,value=self.call('config','show');self.assertEqual(code,0)
        self.assertNotIn('synthetic-password-not-real',json.dumps(value));self.assertTrue(value['data']['identity_valid'])
    def test_configuration_waits_for_native_stop(self):
        (self.root/'instance/private/RUNNING').write_text('synthetic marker')
        code,_=self.call('config','apply',stdin=json.dumps({'dashboard_port':16285}));self.assertEqual(code,24)
        self.assertEqual(c.read(self.root/'instance/data/cmd_config.json')['dashboard']['port'],16185)
    def test_external_lifecycle_refused_before_dispatch(self):
        self.record['components']['napcat']={'installed':True,'ownership':'external-readonly','path':str(self.temp.name),'version':'fixture'}
        with patch.object(c,'shell') as shell,self.assertRaises(c.Fault):c.operate(self.root,self.record,'stop',['napcat'])
        shell.assert_not_called()
    def test_path_traversal_refused(self):
        with self.assertRaises(ValueError):c.resolve(self.root,'../escape')
    def test_restore_wrapper_contract_only(self):
        # Dispatch is mocked: no databases are copied and no migration occurs.
        self.record['release']='releases/fixture';c.atomic(self.root/'installation.json',self.record)
        with patch.object(c,'invoke',return_value={'completed':True}) as invoke:
            code,_=self.call('restore','--snapshot',str(Path(self.temp.name)/'synthetic-snapshot'))
            self.assertEqual(code,0);self.assertIn('restore',invoke.call_args.args)
    def test_health_stopped_is_not_passed(self):
        with patch.object(c,'component_status',return_value={'running':False}):
            code,value=self.call('health');self.assertEqual(code,21);self.assertFalse(value['ok'])
    def test_different_owner_config_detected(self):
        c.atomic(self.root/'instance/private/instance-identity.json',{'owner':'200'})
        code,_=self.call('config','validate');self.assertEqual(code,23)
    def test_install_running_instance_refused_before_dependencies(self):
        (self.root/'instance/private/RUNNING').write_text('synthetic')
        with patch.object(c,'install_environment') as install:
            code,_=self.call('install','--owner','100');self.assertEqual(code,24)
        install.assert_not_called()
    def test_embedding_only_configuration_is_idempotent(self):
        script=Path(c.PROJECT)/'scripts/configure_local_models.py'
        command=[sys.executable,str(script),'--instance',str(self.root/'instance'),'--components','embedding']
        subprocess.run(command,check=True,capture_output=True)
        before=(self.root/'instance/private/local-embedding-key.txt').read_bytes()
        subprocess.run(command,check=True,capture_output=True)
        cfg=c.read(self.root/'instance/data/cmd_config.json')
        self.assertEqual([x['provider_type'] for x in cfg['provider']],['embedding'])
        self.assertNotIn('provider_stt_settings',cfg)
        self.assertEqual(before,(self.root/'instance/private/local-embedding-key.txt').read_bytes())
    def test_asset_failure_retry_receipt_and_tamper(self):
        source=Path(self.temp.name)/'fixture';source.write_bytes(b'synthetic optional component')
        target=self.root/'assets/fixture';spec={'id':'synthetic','format':'zip','sha256':c.digest(source)}
        def failed(archive,stage):
            stage.mkdir();(stage/'partial').write_text('incomplete');raise ValueError('injected extraction failure')
        with patch.object(c,'acquire',return_value=source),patch.object(c,'unpack',side_effect=failed):
            with self.assertRaises(ValueError):c.install_asset(self.root,'fixture',spec,target,self.root/'cache')
        self.assertFalse(target.exists())
        def complete(archive,stage):stage.mkdir();(stage/'payload').write_bytes(archive.read_bytes())
        with patch.object(c,'acquire',return_value=source),patch.object(c,'unpack',side_effect=complete):
            c.install_asset(self.root,'fixture',spec,target,self.root/'cache')
        with patch.object(c,'acquire') as acquire:
            c.install_asset(self.root,'fixture',spec,target,self.root/'cache');acquire.assert_not_called()
        marker=self.root/'asset-receipts/fixture.json';marker.unlink()
        c.install_asset(self.root,'fixture',spec,target,self.root/'cache')
        (target/'payload').write_text('damaged')
        with self.assertRaises(c.Fault):c.install_asset(self.root,'fixture',spec,target,self.root/'cache')

if __name__=='__main__':unittest.main(verbosity=2)
