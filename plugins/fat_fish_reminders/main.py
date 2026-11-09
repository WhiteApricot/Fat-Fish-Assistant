import asyncio
from astrbot.api.star import Star
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
    async def authorize(self,row):
        if self.stopping:return False
        current=next((r for r in self.store.rows() if r['id']==row['id']),None)
        if not current or current['state']!='active' or current['due']!=row['due'] or current['updated']!=row['updated']:return False
        cfg,policy=self.gateway.load();group=row['scope']
        if row['creator'] in policy.blocked_for(group) or row['target'] in policy.blocked_for(group):return False
        if self.gateway.epoch(cfg,group)!=row['epoch']:return False
        adapter=self.context.get_platform_inst(row['platform'])
        if not adapter or adapter.meta().name!='aiocqhttp':return False
        bot=adapter.get_client()
        if group:
            if group not in policy.groups:return False
            members=await self.gateway.group_platform.roster(group,bot,max_age=0)
            if not members or not {row['target'],row['creator']}.issubset(members):return False
        else:
            if row['target']!=row['creator']:return False
            if row['creator']!=policy.owner and row['creator'] not in policy.global_admins:
                if not await self.gateway.friends.contains(row['creator'],bot):return False
        latest,current=self.gateway.load()
        if group and members[row['creator']]['role'] in {'owner','admin'}:
            current.qq_group_admins[group]={row['creator']}
        fresh=next((r for r in self.store.rows() if r['id']==row['id']),None)
        return bool(not self.stopping and fresh and fresh['state']=='active' and fresh['due']==row['due'] and fresh['updated']==row['updated']
            and self.gateway.epoch(latest,group)==row['epoch'] and row['creator'] not in current.blocked_for(group) and row['target'] not in current.blocked_for(group)
            and (not group or (group in current.groups and (row['target']==row['creator'] or current.authorize(row['creator'],'group',private=False,group=group)))))
    async def checked_send(self,row):
        from fat_fish.adapters.qq_reply_delay import new_reply_deadline,wait_before_reply
        deadline=new_reply_deadline()
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Plain,At
        from fat_fish.persona.persona_reply import render
        scope=row['platform']+(':GroupMessage:'+row['scope'] if row['scope'] else ':FriendMessage:'+row['target'])
        cfg,_=self.gateway.load()
        notes=cfg.get('group_persona_notes',{}).get(row['scope'],'') if row['scope'] else cfg.get('private_persona_notes',{}).get(row['target'],'')
        wording=await render(self.context,scope,row['body'],notes=notes)
        await wait_before_reply(deadline)
        if not await self.authorize(row):return False
        chain=MessageChain()
        chain.chain=([At(qq=row['target'])] if row['scope'] else [])+[Plain(wording or '提醒：'+row['body'])]
        if not await self.context.send_message(scope,chain):raise RuntimeError('platform unavailable')
    async def tick(self):
        task=asyncio.current_task();self.active.add(task)
        try:await self.store.tick(self.authorize,self.checked_send)
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
