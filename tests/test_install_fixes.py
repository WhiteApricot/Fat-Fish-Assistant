"""Installed STT enablement and sanitized audio fallback; synthetic events only."""
import fixture_runtime
import asyncio,json,os,subprocess,sys,tempfile,types,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock,patch
PROJECT=Path(__file__).resolve().parents[1]
RELEASE=Path(os.environ['FEIYU_TEST_RELEASE'])
sys.path[:0]=[str(PROJECT/'src'),str(RELEASE/'astrbot')]
from astrbot.api.message_components import Record,Plain,Reply
from fat_fish_guard.main import FatFishGuard
from astrbot.core.pipeline.preprocess_stage.stage import PreProcessStage

class Fixes(unittest.IsolatedAsyncioTestCase):
 async def test_fresh_install_has_no_chat_or_memory_history(self):
  import sqlite3
  sys.path.insert(0,str(PROJECT/'scripts'))
  from instance import initialize
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)/'instance'
   initialize(root,'100',RELEASE)
   with closing(sqlite3.connect(root/'data/data_v4.db')) as db:
    self.assertEqual(db.execute('select count(*) from conversations').fetchone()[0],0)
    dialogs=db.execute("select begin_dialogs from personas where persona_id='feiyu-v1'").fetchone()[0]
    self.assertEqual(json.loads(dialogs or '[]'),[])
   self.assertFalse((root/'data/plugin_data/astrbot_plugin_livingmemory').exists())

 async def run_voice(self,parts,enabled=False,provider=None,authorized=True):
  stage=PreProcessStage()
  ctx=types.SimpleNamespace(get_using_stt_provider_async=AsyncMock(return_value=provider))
  await stage.initialize(types.SimpleNamespace(astrbot_config={'provider_stt_settings':{'enable':enabled}},plugin_manager=types.SimpleNamespace(context=ctx)))
  event=types.SimpleNamespace(get_platform_name=lambda:'aiocqhttp',get_messages=lambda:parts,unified_msg_origin='synthetic',message_str='',message_obj=types.SimpleNamespace(message_str=''),get_extra=lambda k,d=None:authorized if k=='_feiyu_access_checked' else d,plain_result=lambda t:t)
  event.stopped=False
  def stop():event.stopped=True
  event.stop_event=stop
  with patch.object(Record,'convert_to_file_path',new=AsyncMock(return_value='synthetic.wav')),patch('astrbot.core.pipeline.preprocess_stage.stage.ensure_wav',new=AsyncMock(return_value='synthetic.wav')),patch.object(stage,'_track_temp_media'):
   await stage.process(event)
  replies=[x async for x in FatFishGuard(ctx).unavailable_voice(event)]
  return event,replies
 async def test_uninstalled_audio_stops_with_plain_notice(self):
  e,replies=await self.run_voice([Record(file='synthetic.wav')])
  self.assertTrue(e.stopped);self.assertIn('尚未安装',replies[0]);self.assertNotIn('422',replies[0])
 async def test_service_failure_never_sends_exception(self):
  provider=types.SimpleNamespace(get_text=AsyncMock(side_effect=RuntimeError('private diagnostic must stay local')))
  e,replies=await self.run_voice([Record(file='synthetic.wav')],True,provider)
  self.assertTrue(e.stopped);self.assertNotIn('private diagnostic',replies[0])
 async def test_missing_provider_and_quoted_audio(self):
  e,replies=await self.run_voice([Reply(id=1,chain=[Record(file='synthetic.wav')])],True)
  self.assertTrue(e.stopped);self.assertEqual(len(replies),1)
 async def test_successful_transcript_continues(self):
  e,replies=await self.run_voice([Record(file='synthetic.wav')],True,types.SimpleNamespace(get_text=AsyncMock(return_value='合成转写')))
  self.assertFalse(e.stopped);self.assertEqual(replies,[]);self.assertIsInstance(e.get_messages()[0],Plain)
 async def test_text_image_path_unchanged(self):
  e,replies=await self.run_voice([Plain('synthetic text')])
  self.assertFalse(e.stopped);self.assertFalse(replies)
 async def test_configure_explicitly_enables_and_retains_credentials(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'data').mkdir();(root/'private').mkdir()
   (root/'data/cmd_config.json').write_text('{"provider_stt_settings":{"enable":false}}')
   command=[sys.executable,'-B',str(PROJECT/'scripts/configure_local_models.py'),'--instance',str(root),'--components','speech']
   subprocess.run(command,check=True,capture_output=True)
   cfg=json.loads((root/'data/cmd_config.json').read_text());self.assertTrue(cfg['provider_stt_settings']['enable'])
   before=(root/'data/cmd_config.json').read_bytes()
   subprocess.run(command,check=True,capture_output=True)
   self.assertEqual(before,(root/'data/cmd_config.json').read_bytes())

if __name__=='__main__':unittest.main()
