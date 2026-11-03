import fixture_runtime
import os,sys
from pathlib import Path
RELEASE=Path(os.environ['FEIYU_TEST_RELEASE'])
sys.path[:0]=[str(RELEASE/'src'),str(RELEASE/'astrbot')]
import asyncio,json,pathlib,sys,tempfile,types,unittest
sys.path.insert(0,str(RELEASE/'src'))
from p2c.gateway import Gateway

class Event:
    def __init__(self,sender='300',private=True,text='',group='900',mention=False):
        self.sender,self.private,self.message_str,self.group=sender,private,text,group
        self.extra={};self.sent=[];self.stopped=False;self.session_id=sender if private else group
        self.chain=[types.SimpleNamespace(__class__=None)] if False else []
        if mention:
            self.chain=[type('At',(),{'qq':'999'})()]
        async def action(name):return [{'user_id':300}]
        self.bot=types.SimpleNamespace(call_action=action)
    def get_platform_name(self):return 'aiocqhttp'
    def get_sender_id(self):return self.sender
    def get_self_id(self):return '999'
    def get_group_id(self):return '' if self.private else self.group
    def is_private_chat(self):return self.private
    def get_messages(self):return self.chain
    def set_extra(self,k,v):self.extra[k]=v
    def get_extra(self,k,d=None):return self.extra.get(k,d)
    def stop_event(self):self.stopped=True
    def plain_result(self,t):return t
    async def send(self,m):self.sent.append(m)

class GatewayChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.path=pathlib.Path(self.temp.name)/'access.json'
        self.cfg={'owner':'100','admins':['200'],'blocked':['400'],'groups':{'900':'mention'},'approved_groups':['900'],'epoch':0}
        self.path.write_text(json.dumps(self.cfg))
        self.path.with_name('instance-identity.json').write_text(json.dumps({'owner':'100'}));self.g=Gateway(self.path)
    def tearDown(self):self.temp.cleanup()
    async def test_early_rejection(self):
        for e in [Event('400'),Event('400',False,mention=True),Event('300',False,group='901',mention=True),Event('300',False)]:
            called=[]
            async def downstream():called.append(True)
            await self.g.run(e,downstream)
            self.assertFalse(called);self.assertTrue(e.stopped)
        e=Event('500');self.assertFalse(await self.g.process(e));self.assertIn('好友',e.sent[0])
    async def test_admin_cannot_promote(self):
        e=Event('200',text='/role add 200');await self.g.process(e)
        self.assertIn('权限不足',e.sent[0]);self.assertEqual(self.g.load()[0]['admins'],['200'])
    async def test_confirm_and_revocation(self):
        e=Event('100',text='/role add 300');await self.g.process(e)
        code=e.sent[0].split('/confirm ')[1].split(' ')[0]
        other=Event('200',text='/confirm '+code);await self.g.process(other)
        self.assertNotIn('300',self.g.load()[0]['admins'])
        e=Event('100',text='/confirm '+code);await self.g.process(e)
        self.assertIn('300',self.g.load()[0]['admins'])
        again=Event('100',text='/confirm '+code);await self.g.process(again)
        self.assertIn('无效',again.sent[0])
    async def test_send_recheck_and_group_namespace(self):
        e=Event('300',False,mention=True)
        async def downstream():
            self.assertEqual(e.session_id,'p2c0_900')
            cfg,p=self.g.load();cfg['blocked'].append('300');cfg['epoch']=1;self.g.save(cfg)
            await e.send('must not be sent')
        await self.g.run(e,downstream)
        self.assertEqual(e.sent,[])
    async def test_scope_and_expansion(self):
        e=Event('300',False,text='/memory',mention=True);await self.g.process(e)
        self.assertIn('只接受',e.sent[0])
        e=Event('200',text='/whitelist add 901');await self.g.process(e)
        self.assertIn('未获',e.sent[0]);self.assertNotIn('901',self.g.load()[0]['groups'])
    async def test_help_role_set_and_stranger_messages(self):
        e=Event('100',text='/help');await self.g.process(e)
        self.assertTrue(all(line.startswith('/') and ' ' not in line for line in e.sent[0].removeprefix('【系统】').splitlines()))
        e=Event('300',text='/role');await self.g.process(e)
        self.assertIn('好友',e.sent[0]);self.assertNotIn('L1',e.sent[0])
        e=Event('100',text='/role set 300 2');await self.g.process(e)
        self.assertIn('/confirm',e.sent[0]);self.assertNotIn('300',self.g.load()[0]['admins'])
        e=Event('200',text='/role set 300 2');await self.g.process(e)
        self.assertIn('权限不足',e.sent[0])
        e=Event('100',text='/role set 300 owner');await self.g.process(e)
        self.assertIn('不能',e.sent[0])
        for _ in range(2):
            e=Event('500',text='你好');await self.g.process(e)
            self.assertIn('此实例的主人',e.sent[0]);self.assertIn('主人 QQ：100',e.sent[0])
    async def test_blacklist_cancels_inflight_and_rotates_group_history(self):
        import asyncio
        entered=asyncio.Event()
        async def downstream():entered.set();await asyncio.sleep(60)
        task=asyncio.create_task(self.g.run(Event('300',False,mention=True),downstream))
        await entered.wait()
        cfg,p=self.g.load();self.g.apply(cfg,p,'100',3,'blacklist',('add','300'))
        result=await asyncio.gather(task,return_exceptions=True)
        self.assertIsInstance(result[0],asyncio.CancelledError)
        cfg,p=self.g.load();self.assertEqual(cfg['epoch'],1);self.assertIn('300',cfg['blocked'])
    async def test_smart_mode_revoked_while_deciding(self):
        self.cfg['groups']['900']='smart';self.g.save(self.cfg)
        async def choose(*args,**kwargs):
            cfg,p=self.g.load();cfg['groups']['900']='mention';self.g.save(cfg);return 'group-context'
        self.g.participation.choose=choose
        called=[]
        async def downstream():called.append(True)
        await self.g.run(Event('300',False),downstream)
        self.assertEqual(called,[])
    async def test_mention_includes_nearby_context_and_separates_speakers(self):
        await self.g.process(Event('300',False,text='今天想吃饺子'))
        e=Event('200',False,text='说一下',mention=True)
        async def downstream():
            context=e.get_extra('_feiyu_group_context')
            current=e.get_extra('_feiyu_group_speaker')
            self.assertIn('今天想吃饺子',context);self.assertIn('说一下',context)
            self.assertEqual(current,self.g.participation.label('900','200'))
            self.assertNotEqual(current,self.g.participation.label('900','300'))
            self.assertNotIn('群友300',context)
        await self.g.run(e,downstream)
    async def test_submenus(self):
        e=Event('100',text='/help');await self.g.process(e)
        self.assertNotIn('set QQ',e.sent[0])
        e=Event('100',text='/role help');await self.g.process(e)
        self.assertIn('set QQ 1|2',e.sent[0])
        e=Event('100',text='/help role');await self.g.process(e)
        self.assertIn('set QQ 1|2',e.sent[0])
    async def test_bad_owner_fails_closed(self):
        self.cfg['owner']='200';self.path.write_text(json.dumps(self.cfg))
        self.assertFalse(await self.g.process(Event()))

unittest.main(verbosity=2)
