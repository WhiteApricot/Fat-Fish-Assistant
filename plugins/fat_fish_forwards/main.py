import os

from astrbot.api.event import filter
from astrbot.api.star import Star
from fat_fish.integrations.forwards import ForwardReader,install_collector_bridge,remove_collector_bridge

class FatFishForwards(Star):
    async def initialize(self):
        from astrbot.core.fat_fish_gateway import gateway
        self.gateway=gateway;self.reader=ForwardReader(self.context)
        self.collector=None
        self._connect_collector(required=os.name=='nt')

    @filter.on_astrbot_loaded()
    async def connect_after_plugins_loaded(self):
        if self.collector is None:
            self._connect_collector(required=True)

    def _connect_collector(self, *, required):
        md=self.context.get_registered_star('astrbot_plugin_continuous_message')
        if not md or not md.activated:
            if required:
                raise RuntimeError('合并转发需要启用现有消息收集插件')
            return
        install_collector_bridge(md.star_cls, md.version)
        self.collector=md.star_cls
        self.gateway.forward_reader=self.reader

    async def terminate(self):
        if getattr(self,'collector',None):remove_collector_bridge(self.collector)
        if hasattr(self,'gateway') and getattr(self.gateway,'forward_reader',None) is self.reader:self.gateway.forward_reader=None
