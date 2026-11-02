"""Windows credential rotation, candidate filtering and conversational cadence."""
import asyncio
import io
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from ffa_local_credentials import save_password,load_password,InitialPasswordFilter
from fat_fish_guard.experience import contains_secret,protect_memory_engine,protect_memorize_tool,MemeCadence


class Event:
    def __init__(self,text='终于完成啦',scope='fixture:FriendMessage:100',emotions=None):
        self.message_str,self.unified_msg_origin=text,scope
        self.extra={'found_emotions':['happy'] if emotions is None else emotions}
    def get_extra(self,k,d=None):return self.extra.get(k,d)
    def set_extra(self,k,v):self.extra[k]=v


class Repairs(unittest.IsolatedAsyncioTestCase):
    async def test_current_user_dpapi_rotation_and_legacy_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'private').mkdir()
            legacy=root/'private/dashboard-password.txt';legacy.write_text('OldSynthetic123')
            self.assertEqual(load_password(root),'OldSynthetic123');self.assertFalse(legacy.exists())
            save_password(root,'NewSynthetic456')
            self.assertEqual(load_password(root),'NewSynthetic456')
            raw=(root/'private/dashboard-credential.dpapi').read_bytes()
            self.assertNotIn(b'OldSynthetic123',raw);self.assertNotIn(b'NewSynthetic456',raw)

    async def test_initial_log_is_captured_and_redacted(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'private').mkdir()
            record=logging.LogRecord('astrbot',20,'',0,'Initial password: %s',('GeneratedSynthetic123',),None)
            self.assertTrue(InitialPasswordFilter(root).filter(record))
            self.assertTrue(InitialPasswordFilter(root).filter(record))
            self.assertNotIn('GeneratedSynthetic123',record.getMessage())
            self.assertEqual(load_password(root),'GeneratedSynthetic123')

    async def test_memorize_without_explicit_command_and_reject_chatter(self):
        tool=types.SimpleNamespace(call=AsyncMock(return_value='{"memorized":true}'))
        original=tool.call;protect_memorize_tool(tool)
        for text,allowed in [('我一直喜欢无糖红茶',True),('我每周六都会去游泳',True),('我姐姐叫小雨',True),('我计划未来两年开发一个项目',True),('记住 暗号是鲸鱼',True),('今天午饭吃了面',False),('哈哈哈',False),('假如我喜欢咖啡呢',False),('不要记住我喜欢红茶',False),('你喜欢喝茶吗？',False),('记住 我的密码是 Abcd1234',False),('我的Token: synthetic-private-value',False)]:
            context=types.SimpleNamespace(context=types.SimpleNamespace(event=Event(text)))
            result=json.loads(await tool.call(context,'本人长期喜欢无糖红茶'))
            self.assertEqual(result['memorized'],allowed,text)
        self.assertEqual(original.await_count,5)

    async def test_all_engine_writes_reject_credentials_and_low_value_reflection(self):
        original=AsyncMock(return_value=1)
        engine=types.SimpleNamespace(add_memory=original,update_memory=AsyncMock(return_value=True))
        protect_memory_engine(engine)
        for value in ['我的密码是 Abcd1234','Token: synthetic-value','token synthetic-value','验证码123456','sk-'+('a'*24)]:
            with self.assertRaises(ValueError):await engine.add_memory(value,importance=1)
            with self.assertRaises(ValueError):await engine.update_memory(1,{'content':value})
        with self.assertRaises(ValueError):await engine.add_memory('今天吃面',importance=.3)
        with self.assertRaises(ValueError):await engine.add_memory('喜欢红茶',importance=1,source_messages=[{'text':'password=synthetic'}])
        self.assertTrue(contains_secret({'token':'synthetic'}))
        await engine.add_memory('我长期喜欢红茶',session_id='private-a',importance=.8)
        self.assertEqual(original.await_args.kwargs['session_id'],'private-a')
        await engine.update_memory(1,{'content':'本人现在喜欢绿茶'})
        update=engine.update_memory.__wrapped__
        self.assertEqual(update.await_args.args[1]['metadata']['persona_summary'],'本人现在喜欢绿茶')
        self.assertEqual(update.await_args.args[1]['metadata']['key_facts'],[])

    async def test_meme_cooldown_does_not_affect_other_sessions(self):
        now=[100.];cadence=MemeCadence(lambda:now[0])
        first=Event();cadence.apply(first);self.assertEqual(first.extra['found_emotions'],['happy'])
        second=Event();cadence.apply(second);self.assertEqual(second.extra['found_emotions'],[])
        other=Event(scope='fixture:FriendMessage:200');cadence.apply(other);self.assertEqual(other.extra['found_emotions'],['happy'])
        cadence.apply(Event(emotions=[]));now[0]+=46
        third=Event();cadence.apply(third);self.assertEqual(third.extra['found_emotions'],['happy'])
        explicit=Event('发一个表情包');cadence.apply(explicit);self.assertEqual(explicit.extra['found_emotions'],['happy'])
        denied=Event('不要发表情包');cadence.apply(denied);self.assertEqual(denied.extra['found_emotions'],[])


if __name__=='__main__':unittest.main()
