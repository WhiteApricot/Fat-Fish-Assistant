"""Native first-use pack import and actual decorate/respond stages; synthetic sends."""
import asyncio,contextlib,copy,importlib,json,os,sys,tempfile,types
from unittest.mock import AsyncMock
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'scripts'))
from instance import initialize
from configure_experience import configure,native_pack_modules,CATEGORIES,PACK_ID

release=Path(os.environ['FEIYU_TEST_RELEASE']).resolve()
with tempfile.TemporaryDirectory(prefix='experience-') as tmp,contextlib.chdir(Path.cwd()):
    runtime=Path(tmp)/'instance'
    initialize(runtime,'100',release,seed_persona=False)
    permissions=(runtime/'private/p2c-access.json').read_bytes()
    cfg=json.loads((runtime/'data/cmd_config.json').read_text(encoding='utf-8'))
    assert cfg['platform_settings']['segmented_reply']['enable']
    plugin_cfg=json.loads((runtime/'data/config/meme_manager_config.json').read_text(encoding='utf-8'))
    assert plugin_cfg['generation']['emotion']['max_memes_per_message']==1
    assert plugin_cfg['generation']['emotion']['probability']==35
    assert '可以附一张' in plugin_cfg['generation']['prompt']['head']
    native=native_pack_modules(runtime,release)
    pack=next(p for p in native.list_installed_packs() if p['id']==PACK_ID)
    assert pack['image_count']==24 and pack['is_default']
    images=importlib.import_module('ffa_starter_meme.backend.packs.images')
    from PIL import Image
    from ffa_starter_meme.backend.packs.resolver import resolve_pack_paths
    paths=resolve_pack_paths(PACK_ID)
    for mood in CATEGORIES:
        names=images.get_emoji_by_category(mood);assert names
        image_path=Path(paths['memes_dir'])/mood/names[0]
        with Image.open(image_path) as picture:assert picture.size==(512,512) and picture.n_frames==8
        if os.name=='nt':assert image_path.stat().st_ino==(release/'resources/starter-memes/gifs'/names[0]).stat().st_ino
    custom=Path(paths['memes_dir'])/'happy/custom-user.txt';custom.write_text('synthetic user asset')
    configure(runtime,release)
    assert custom.read_text()=='synthetic user asset'
    assert permissions==(runtime/'private/p2c-access.json').read_bytes()
    os.environ.update(FEIYU_RUNTIME_ROOT=str(runtime),FFA_RUNTIME_ROOT=str(runtime),FEIYU_SOURCE_ROOT=str(release/'src'),FEIYU_PERSONA_ROOT=str(runtime/'persona'))
    from astrbot.core.config.default import DEFAULT_CONFIG
    from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
    from astrbot.core.pipeline.respond.stage import RespondStage
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage,MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.message.message_event_result import ResultContentType
    config=copy.deepcopy(DEFAULT_CONFIG);config['platform_settings']['segmented_reply'].update(cfg['platform_settings']['segmented_reply'])
    context=types.SimpleNamespace(astrbot_config=config,plugin_manager=types.SimpleNamespace(context=types.SimpleNamespace(get_using_tts_provider_async=AsyncMock(return_value=None))))
    async def verify():
        decorate=ResultDecorateStage();respond=RespondStage()
        await decorate.initialize(context);await respond.initialize(context)
        respond.interval=[0,0]
        async def check(text,expected):
            obj=AstrBotMessage();obj.type=MessageType.FRIEND_MESSAGE;obj.self_id='999';obj.sender=MessageMember('100');obj.message_id='synthetic';obj.message=[];obj.raw_message={}
            event=AstrMessageEvent('synthetic',obj,PlatformMetadata('aiocqhttp','synthetic','feiyu-onebot'),'100')
            event.set_result(event.plain_result(text).set_result_content_type(ResultContentType.LLM_RESULT))
            sent=[]
            async def send(message):sent.append(message.get_plain_text())
            event.send=send
            async for _ in decorate.process(event):pass
            await respond.process(event)
            assert sent==expected,sent
        await check('第一句\n第二句\n第三句',['第一句','第二句','第三句'])
        await check('说明\n```python\na=1\nb=2\n```\n结束',['说明','```python\na=1\nb=2\n```','结束'])
    asyncio.run(verify())
    print('EXPERIENCE_RESULT=verified whale-girl native pack ready; native separate sends; fenced code intact; permissions and user resources preserved')
