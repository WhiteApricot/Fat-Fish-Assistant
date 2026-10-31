import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'scripts'),str(PROJECT/'src')]
from p2d.connection_health import probe
import connection_health as reader

class Protocol(unittest.IsolatedAsyncioTestCase):
    async def test_only_readonly_actions_and_matching_identity(self):
        calls=[]
        async def action(name,**params):
            calls.append(name)
            return {'online':True,'good':True} if name=='get_status' else {'user_id':555}
        bot=SimpleNamespace(_wsr_api_clients={'555':object()},_wsr_event_clients={object()},call_action=action)
        platform=SimpleNamespace(config={'id':'feiyu-onebot'},bot=bot)
        context=SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda:[platform]))
        self.assertTrue((await probe(context,'555'))['connected'])
        self.assertEqual(calls,['get_status','get_login_info'])
        calls.clear();self.assertFalse((await probe(context,'666'))['connected']);self.assertEqual(calls,[])
        async def wrong(name,**params):return {'online':True,'good':True} if name=='get_status' else {'user_id':666}
        bot.call_action=wrong;self.assertFalse((await probe(context,'555'))['connected'])
        async def failing(*a,**k):raise RuntimeError('synthetic-sensitive-value')
        bot.call_action=failing
        result=await probe(context,'555');self.assertFalse(result['connected']);self.assertNotIn('synthetic-sensitive-value',json.dumps(result))

class Evidence(unittest.TestCase):
    def test_fresh_pid_and_deployment_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'private').mkdir();(root/'instance.json').write_text('{"deployment_id":"synthetic"}')
            record={'schema':1,'pid':123,'time':time.time(),'deployment_id':'synthetic','connected':True,'reason':'ready'}
            path=root/'private/runtime-health.json'
            with patch.object(reader,'process',return_value=SimpleNamespace(pid=123)):
                path.write_text(json.dumps(record));self.assertTrue(reader.read_connection(root)['connected'])
                record['pid']=456;path.write_text(json.dumps(record));self.assertFalse(reader.read_connection(root)['connected'])
                record.update(pid=123,time=time.time()-60);path.write_text(json.dumps(record));self.assertFalse(reader.read_connection(root)['connected'])
                record.update(time=time.time(),deployment_id='another');path.write_text(json.dumps(record));self.assertFalse(reader.read_connection(root)['connected'])

if __name__=='__main__':unittest.main(verbosity=2)
