import asyncio
from astrbot.api.star import Star
from fat_fish.reminders.dispatch import ReminderDispatch
from fat_fish.reminders.reminders import ReminderStore

class FatFishReminders(Star):
    async def initialize(self):
        from astrbot.core.fat_fish_gateway import gateway
        self.gateway=gateway;self.stopping=False;self.active=set()
        path=gateway.path.parent.parent/'data/plugin_data/fat_fish_reminders'
        path.mkdir(parents=True,exist_ok=True)
        self.store=ReminderStore(path/'reminders.sqlite3')
        # Native lifecycle starts the shared scheduler only after platforms are ready.
        # Starting it during plugin loading could revoke recovered tasks before
        # the platform manager has installed any adapters.
        self.job=self.context.cron_manager.scheduler.add_job(self.tick,'interval',seconds=5,id='ffa-p2f-reminders',replace_existing=True,max_instances=1,coalesce=True,misfire_grace_time=60)
        gateway.reminders=self.store
    async def tick(self):
        task=asyncio.current_task();self.active.add(task)
        try:
            await ReminderDispatch(self.store,self.gateway.reminder_runtime,self.gateway.render_reminder).tick()
        finally:self.active.discard(task)
    async def terminate(self):
        self.stopping=True
        if hasattr(self,'gateway') and getattr(self.gateway,'reminders',None) is getattr(self,'store',None):self.gateway.reminders=None
        if getattr(self,'job',None):
            try:self.job.remove()
            except LookupError:pass
            self.job=None
        for task in tuple(getattr(self,'active',())):task.cancel()
        if getattr(self,'active',None):await asyncio.gather(*self.active,return_exceptions=True)
