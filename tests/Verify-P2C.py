import fixture_runtime
import os,sys
from pathlib import Path
RELEASE=Path(os.environ['FEIYU_TEST_RELEASE'])
sys.path[:0]=[str(RELEASE/'src'),str(RELEASE/'astrbot')]
"""P2C adversarial checks; synthetic QQ objects, no network or real messages."""
import asyncio, importlib.util, pathlib, sys, types, unittest
ROOT=RELEASE
sys.path.insert(0,str(RELEASE/'src'))
from p2c.policy import Policy, PendingActions, FriendDirectory

class Security(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.p=Policy('100',{'200'},{'400'},{'900':'mention'})
    def test_matrix(self):
        for sender,friend,level in [('100',False,3),('200',False,2),('300',True,1),('500',False,0),('400',True,-1)]:
            self.assertEqual(self.p.level(sender,friend),level)
            self.assertEqual(self.p.allowed(sender,True,friend=friend),level>=1)
            self.assertEqual(self.p.allowed(sender,False,'900',friend),level>=0)
            self.assertFalse(self.p.allowed(sender,False,'901',friend))
            self.assertFalse(self.p.authorize(sender,'work',private=True,friend=friend))
            self.assertFalse(self.p.authorize(sender,'owner',private=True,friend=friend))
    def test_privilege_escalation(self):
        self.assertFalse(self.p.authorize('200','admin',private=True,target='300'))
        self.assertFalse(self.p.authorize('100','admin',private=True,target='100'))
        self.assertFalse(self.p.authorize('200','blacklist',private=True,target='100'))
        self.assertFalse(self.p.authorize('200','blacklist',private=True,target='200'))
        self.assertTrue(self.p.authorize('100','admin',private=True,target='300'))
        for sender in ('100','200','300'):
            self.assertFalse(self.p.authorize(sender,'memory',private=False,friend=True))
    def test_blacklist_precedes_owner(self):
        self.p.blocked.add('100')
        self.assertEqual(self.p.level('100',True),-1)
        self.assertFalse(self.p.authorize('100','admin',private=True,target='300'))
    def test_confirm_scope_expiry_replay(self):
        now=[0];p=PendingActions(lambda:now[0]);code=p.issue('100','private','admin',['add','300'])
        self.assertIsNone(p.consume('200','private',code))
        self.assertIsNone(p.consume('100','group:900',code))
        self.assertIsNone(p.consume('100','private','wrong'))
        self.assertEqual(p.consume('100','private',code),('admin',('add','300')))
        self.assertIsNone(p.consume('100','private',code))
        code=p.issue('100','private','admin',['add','300']);now[0]=90
        self.assertIsNone(p.consume('100','private',code))
    async def test_friend_trust_and_expiry(self):
        now=[0];f=FriendDirectory(clock=lambda:now[0]);calls=[]
        async def action(name):calls.append(name);return [{'user_id':300}]
        bot=types.SimpleNamespace(call_action=action)
        self.assertTrue(await f.contains('300',bot));self.assertFalse(await f.contains('500',bot))
        self.assertEqual(calls,['get_friend_list'])
        async def failure(name):raise RuntimeError('offline')
        bot.call_action=failure;now[0]=31
        self.assertFalse(await f.contains('300',bot))
        async def bad(name):return {'data':[{'user_id':300}]}
        bot.call_action=bad
        self.assertFalse(await f.contains('300',bot))

class Quotes(unittest.IsolatedAsyncioTestCase):
    async def test_existing_adapter_components(self):
        sys.path.insert(0,str(RELEASE/'astrbot'))
        path=ROOT/'plugins/astrbot_plugin_continuous_message/forward_handler.py'
        package=types.ModuleType('quote_fixture');package.__path__=[str(path.parent)];sys.modules['quote_fixture']=package
        spec=importlib.util.spec_from_file_location('quote_fixture.forward_handler',path);mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
        from astrbot.api.message_components import Reply,Plain,Image
        handler=mod.ForwardHandler('<quoted_message sender="{sender_name}">{full_text}</quoted_message>')
        extras={}
        event=types.SimpleNamespace(message_obj=types.SimpleNamespace(message=[]),get_self_id=lambda:'100',get_extra=lambda key,default=None:extras.get(key,default))
        for author in ('100','300'):
            event.message_obj.message=[Reply(id='1',sender_id=author,sender_nickname='a"<b',chain=[Plain(text='<指令>原文'),Image(file='https://example.org/test.png')])]
            text,images=await handler.extract_reply_content(event)
            self.assertIn('&lt;指令&gt;原文',text);self.assertEqual(len(images),1)
            if author=='100':self.assertIn('sender="Bot"',text)
        extras['_feiyu_blocked_ids']=('300',)
        self.assertEqual(await handler.extract_reply_content(event),('',[]))
        event.message_obj.message=[Reply(id='2')]
        text,images=await handler.extract_reply_content(event)
        self.assertIn('未完整取得',text);self.assertEqual(images,[])
        # Quote of a follow-up question must retain its original topic even
        # when unrelated recent conversation exists.
        calls=[]
        async def get_msg(action, **kwargs):
            calls.append(kwargs['message_id'])
            return {'message_type':'private','sender':{'user_id':'100'},'target_id':'300',
                    'message':[{'type':'text','data':{'text':'最初话题：潮汐由引力产生'}}]}
        event.bot=types.SimpleNamespace(call_action=get_msg)
        event.is_private_chat=lambda:True
        event.get_sender_id=lambda:'300'
        extras.clear()
        event.message_obj.message=[Reply(id='20',sender_id='300',chain=[Reply(id='10'),Plain(text='细说')])]
        text,images=await handler.extract_reply_content(event)
        self.assertIn('潮汐由引力产生',text)
        self.assertLess(text.index('潮汐'),text.index('细说'))
        self.assertEqual(calls,[10])
        async def napcat_outgoing(action, **kwargs):
            if action=='get_friend_msg_history':return {'messages':[{'message_id':10}]}
            return {'message_type':'private','sender':{'user_id':'100'},'user_id':100,
                    'message':[{'type':'text','data':{'text':'最初话题：潮汐由引力产生'}}]}
        event.bot.call_action=napcat_outgoing
        text,_=await handler.extract_reply_content(event)
        self.assertIn('潮汐由引力产生',text)
        self.assertNotIn('未完整取得',text)
        # Cross-conversation outgoing ancestors fail closed.
        async def foreign(action, **kwargs):
            return {'message_type':'private','sender':{'user_id':'100'},'target_id':'999',
                    'message':[{'type':'text','data':{'text':'foreign-secret'}}]}
        event.bot.call_action=foreign
        text,_=await handler.extract_reply_content(event)
        self.assertNotIn('foreign-secret',text)
        self.assertIn('未完整取得',text)
        # Cyclic IDs terminate without looking up arbitrary additional messages.
        event.message_obj.message=[Reply(id='20',sender_id='300',chain=[Reply(id='20'),Plain(text='细说')])]
        text,_=await handler.extract_reply_content(event)
        self.assertIn('未完整取得',text)


if __name__=='__main__':unittest.main(verbosity=2)
