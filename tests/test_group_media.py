import fixture_runtime
import os,sys
from pathlib import Path
RELEASE=Path(os.environ['FEIYU_TEST_RELEASE'])
sys.path[:0]=[str(RELEASE/'src'),str(RELEASE/'astrbot')]
import sys
import types
import unittest
import asyncio
from unittest.mock import AsyncMock,patch
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(RELEASE/'src'),str(RELEASE/'astrbot')]
from p2d.group_media import GroupMedia
from astrbot.api.message_components import Image, Record, Plain, Reply
from p2c.quote_thread import resolve_quote_thread

def event(sender='a', group='g', parts=None):
    items=parts or []
    extras={}
    return types.SimpleNamespace(get_sender_id=lambda:sender,get_group_id=lambda:group,
        get_messages=lambda:items,set_extra=lambda k,v:extras.__setitem__(k,v),
        get_extra=lambda k,d=None:extras.get(k,d),message_str='',
        message_obj=types.SimpleNamespace(message_str=''),unified_msg_origin='fixture')

class Checks(unittest.TestCase):
    def test_picture_and_audio_attach_once(self):
        for component in (Image(file='fixture.png'),Record(file='fixture.wav')):
            bridge=GroupMedia()
            bridge.observe(event(parts=[component]),0,mentioned=False)
            request=event(parts=[Plain('请理解媒体')])
            bridge.observe(request,0,mentioned=True)
            self.assertEqual(len(request.get_messages()),2)
            self.assertIsNot(request.get_messages()[1],component)
            second=event(parts=[Plain('新问题')])
            bridge.observe(second,0,mentioned=True)
            self.assertEqual(len(second.get_messages()),1)

    def test_sender_group_epoch_and_expiry_isolation(self):
        for sender,group,epoch,elapsed in [('b','g',0,1),('a','h',0,1),('a','g',1,1),('a','g',0,13)]:
            clock=[100];bridge=GroupMedia(lambda:clock[0])
            bridge.observe(event(parts=[Image(file='fixture.png')]),0,mentioned=False)
            clock[0]+=elapsed
            request=event(sender,group,[Plain('问题')])
            bridge.observe(request,epoch,mentioned=True)
            self.assertEqual(len(request.get_messages()),1)

    def test_explicit_quote_does_not_pick_unrelated_picture(self):
        bridge=GroupMedia()
        bridge.observe(event(parts=[Image(file='fixture.png')]),0,mentioned=False)
        request=event(parts=[Reply(id='10'),Plain('引用问题')])
        bridge.observe(request,0,mentioned=True)
        self.assertEqual(len(request.get_messages()),2)

class QuoteChecks(unittest.IsolatedAsyncioTestCase):
    async def test_voice_text_is_available_before_smart_judgment(self):
        bridge=GroupMedia()
        e=event(parts=[Record(file='sample.amr')]);bridge.observe(e,0,mentioned=False)
        stt=types.SimpleNamespace(get_text=AsyncMock(return_value='周末想看海，有推荐吗'))
        context=types.SimpleNamespace(get_using_stt_provider_async=AsyncMock(return_value=stt))
        with patch.object(Record,'convert_to_file_path',new=AsyncMock(return_value='sample.wav')):
            self.assertTrue(await bridge.transcribe(e,0,context,lambda:True))
            at=event(parts=[Plain(' ')]);bridge.observe(at,0,mentioned=True)
            self.assertTrue(await bridge.transcribe(at,0,context,lambda:True))
        self.assertIn('周末想看海',e.message_str)
        self.assertIn('请回应其内容',at.message_str)
        self.assertEqual(stt.get_text.await_count,1)
        self.assertFalse(any(isinstance(p,Record) for p in e.get_messages()))

    async def test_empty_mention_uses_same_sender_transcript_after_media_expiry(self):
        clock=[100];bridge=GroupMedia(lambda:clock[0])
        bridge.speech[('g',0,'a')]=(100,('sample',),'想先喝水再散步')
        clock[0]+=20
        other=event('b',parts=[Plain(' ')]);bridge.observe(other,0,mentioned=True)
        self.assertFalse(await bridge.transcribe(other,0,None,lambda:True))
        own=event(parts=[Plain(' ')]);bridge.observe(own,0,mentioned=True)
        self.assertTrue(await bridge.transcribe(own,0,None,lambda:True))
        self.assertIn('想先喝水',own.message_str)

    async def test_revocation_during_stt_prevents_transcript_capture(self):
        bridge=GroupMedia();e=event(parts=[Record(file='sample.amr')])
        allowed=[True]
        async def stt(**kwargs):allowed[0]=False;return 'must not enter history'
        context=types.SimpleNamespace(get_using_stt_provider_async=AsyncMock(return_value=types.SimpleNamespace(get_text=stt)))
        with patch.object(Record,'convert_to_file_path',new=AsyncMock(return_value='sample.wav')):
            with self.assertRaises(asyncio.CancelledError):
                await bridge.transcribe(e,0,context,lambda:allowed[0])
        self.assertFalse(bridge.speech);self.assertEqual(e.message_str,'')

    async def test_delayed_empty_mention_in_mention_mode_retains_voice(self):
        clock=[100];bridge=GroupMedia(lambda:clock[0])
        original=event(parts=[Record(file='sample.amr')]);bridge.observe(original,0,mentioned=False)
        clock[0]+=25
        at=event(parts=[Plain(' ')]);bridge.observe(at,0,mentioned=True)
        context=types.SimpleNamespace(get_using_stt_provider_async=AsyncMock(
            return_value=types.SimpleNamespace(get_text=AsyncMock(return_value='明天去图书馆'))))
        with patch.object(Record,'convert_to_file_path',new=AsyncMock(return_value='sample.wav')):
            self.assertTrue(await bridge.transcribe(at,0,context,lambda:True))
        self.assertIn('请回应其内容',at.message_str)

    async def test_populated_chain_does_not_bypass_group_proof(self):
        async def foreign(*args, **kwargs):
            return {'message_type':'group','group_id':'other','sender':{'user_id':'b'},
                    'message':[{'type':'text','data':{'text':'FOREIGN_SECRET'}}]}
        e=event(parts=[])
        e.is_private_chat=lambda:False
        e.get_extra=lambda key,default=None:default
        e.get_self_id=lambda:'bot'
        e.bot=types.SimpleNamespace(call_action=foreign)
        quoted,_=await resolve_quote_thread(e,Reply(id='10',sender_id='b',chain=[Plain('FOREIGN_SECRET')]))
        self.assertNotIn('FOREIGN_SECRET',quoted)
        self.assertIn('未完整取得',quoted)

if __name__=='__main__':unittest.main(verbosity=2)
