"""Load and stop both plugins in the real native lifecycle, without a QQ adapter."""
import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock,patch

PROJECT=Path(__file__).resolve().parents[1]
RELEASE=Path(os.environ['FEIYU_TEST_RELEASE']).resolve()
with tempfile.TemporaryDirectory(prefix='p2f-native-load-') as temp,contextlib.chdir(temp):
    root=Path(temp);(root/'private').mkdir();(root/'persona').mkdir();(root/'data/config').mkdir(parents=True)
    shutil.copytree(RELEASE/'plugins',root/'data/plugins')
    for file in (PROJECT/'examples/config/plugins').glob('*.json'):shutil.copy2(file,root/'data/config'/file.name)
    (root/'data/cmd_config.json').write_text(json.dumps({'platform':[],'provider':[],'admins_id':['100'],'dashboard':{'host':'127.0.0.1','port':16186}}))
    (root/'private/p2c-access.json').write_text(json.dumps({'owner':'100','global_admins':[],'admins':[],'blocked':[],'approved_groups':[],'groups':{}}))
    (root/'private/instance-identity.json').write_text('{"owner":"100"}')
    os.environ.update(ASTRBOT_ROOT=str(root),FEIYU_RUNTIME_ROOT=str(root),FFA_RUNTIME_ROOT=str(root),FEIYU_SOURCE_ROOT=str(RELEASE/'src'),FEIYU_PERSONA_ROOT=str(root/'persona'))
    sys.path[:0]=[str(root),str(RELEASE/'src'),str(RELEASE/'astrbot')]
    from astrbot.core import db_helper,LogBroker
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    from astrbot.core.feiyu_p2c import gateway
    async def run():
        app=AstrBotCoreLifecycle(LogBroker(),db_helper)
        with patch('astrbot.core.core_lifecycle.update_llm_metadata',new=AsyncMock()),patch('astrbot.core.utils.t2i.network_strategy.NetworkRenderStrategy.get_official_endpoints',new=AsyncMock()),patch('astrbot.core.star.star_manager._install_requirements_with_precheck',new=AsyncMock(side_effect=AssertionError('Dynamic installation forbidden in P2F tests'))):
            await app.initialize()
        try:
            for name in ['fat_fish_forwards','fat_fish_reminders']:
                md=app.star_context.get_registered_star(name)
                assert md and md.activated and md.star_cls,name+' not loaded'
            assert getattr(gateway,'forward_reader',None)
            assert getattr(gateway,'reminders',None)
            assert not app.cron_manager.scheduler.running,'scheduler started before native platform initialization'
            assert all(p.meta().name!='aiocqhttp' for p in app.platform_manager.platform_insts)
            await app.cron_manager.start(app.star_context)
            assert app.cron_manager.scheduler.get_job('ffa-p2f-reminders')
            md=app.star_context.get_registered_star('fat_fish_reminders')
            await md.star_cls.terminate()
            assert gateway.reminders is None
            assert app.cron_manager.scheduler.get_job('ffa-p2f-reminders') is None
            assert gateway.forward_reader is not None
        finally:await app.stop()
        print('Native load, platform-free initialization, shared scheduler start and independent stop: PASS')
    asyncio.run(run())
