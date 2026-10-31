import asyncio
import json
import os
from pathlib import Path
import time
from astrbot.api.star import Star
from astrbot.api import logger
from p2d.connection_health import probe

class FatFishHealth(Star):
    def __init__(self,context):
        super().__init__(context);self.context=context;self.task=None
        value=os.environ.get('FFA_RUNTIME_ROOT') or os.environ.get('FEIYU_RUNTIME_ROOT')
        self.root=Path(value) if value else None
        self.path=self.root/'private/runtime-health.json' if self.root else None

    async def initialize(self):
        if self.root and (self.root/'instance.json').exists():self.task=asyncio.create_task(self.monitor())

    async def monitor(self):
        failed=False
        while True:
            try:
                identity=self.root/'private/bot-identity.json'
                bot_id=json.loads(identity.read_text())['bot_id'] if identity.exists() else None
                status=await probe(self.context,bot_id)
                deployment=json.loads((self.root/'instance.json').read_text()).get('deployment_id')
                record={'schema':1,'pid':os.getpid(),'time':time.time(),'deployment_id':deployment,**status}
                pending=self.path.with_suffix('.pending');pending.write_text(json.dumps(record));pending.replace(self.path)
                failed=False
            except asyncio.CancelledError:raise
            except Exception as exc:
                if not failed:logger.warning('Managed protocol health unavailable: '+type(exc).__name__)
                failed=True
            await asyncio.sleep(5)

    async def terminate(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
        if self.path:self.path.unlink(missing_ok=True)
