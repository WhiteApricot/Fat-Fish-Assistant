import os,sys,tempfile,json
from pathlib import Path
RELEASE=Path(os.environ['FEIYU_TEST_RELEASE'])
sys.path[:0]=[str(RELEASE/'src'),str(RELEASE/'astrbot'),str(RELEASE/'plugins')]
_OriginalTemp=tempfile.TemporaryDirectory
class IdentityTemp(_OriginalTemp):
 def __enter__(self):
  folder=super().__enter__()
  (Path(folder)/'instance-identity.json').write_text(json.dumps({'owner':'100'}))
  return folder
tempfile.TemporaryDirectory=IdentityTemp
_bootstrap=_OriginalTemp(ignore_cleanup_errors=True)
_runtime=Path(_bootstrap.name)
(_runtime/'private').mkdir()
(_runtime/'private/instance-identity.json').write_text(json.dumps({'owner':'100'}))
(_runtime/'private/p2c-access.json').write_text(json.dumps({'owner':'100','admins':[],'blocked':[],'approved_groups':[],'groups':{}}))
(_runtime/'persona').mkdir()
(_runtime/'persona/Persona_Master.md').write_text('Synthetic public test persona')
(_runtime/'persona/Persona_Examples.json').write_text('[]')
os.environ.update(FEIYU_SOURCE_ROOT=str(RELEASE/'src'),FEIYU_RUNTIME_ROOT=str(_runtime),FEIYU_PERSONA_ROOT=str(_runtime/'persona'),ASTRBOT_ROOT=str(_runtime))
import asyncio,sys,types,unittest
sys.path[:0]=[str(RELEASE/'src'),str(RELEASE/'astrbot')]
from p2c.quote_thread import resolve_quote_thread
from astrbot.api.message_components import Reply,Plain

class Checks(unittest.IsolatedAsyncioTestCase):
 async def test_group_parent_and_boundary(self):
  async def action(name,**kwargs):return {'message_type':'group','group_id':900,'sender':{'user_id':999,'nickname':'Bot'},'message':[{'type':'text','data':{'text':'最初群聊话题：潮汐'}}]}
  e=types.SimpleNamespace(get_extra=lambda k,d=None:d,is_private_chat=lambda:False,get_group_id=lambda:'900',get_self_id=lambda:'999',bot=types.SimpleNamespace(call_action=action))
  root=Reply(id='20',sender_id='300',sender_nickname='甲',chain=[Reply(id='10'),Plain(text='细说')])
  text,_=await resolve_quote_thread(e,root,lambda user,name:name+'[stable-label]')
  self.assertIn('最初群聊话题：潮汐',text);self.assertIn('stable-label',text)
  async def foreign(name,**kwargs):return {'message_type':'group','group_id':901,'sender':{'user_id':999},'message':[{'type':'text','data':{'text':'other-group-secret'}}]}
  e.bot.call_action=foreign;text,_=await resolve_quote_thread(e,root)
  self.assertNotIn('other-group-secret',text);self.assertIn('未完整取得',text)

unittest.main(verbosity=2)
