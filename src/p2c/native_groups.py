"""One shared group conversation, using AstrBot history and LivingMemory."""
import asyncio
import os
import json
from pathlib import Path
from astrbot.api import sp
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

class NativeGroups:
    def __init__(self,gateway):
        self.gateway=gateway;self.locks={};self.bootstrapped=set();self.personas_ready={}

    def scope(self,event):
        return event.get_platform_id()+':GroupMessage:'+str(event.get_group_id())

    async def append(self,event,parts,*,sender=None,name=None,kind='user',message_id=None,authors=None,when=None):
        manager=self.gateway.context.message_history_manager
        scope=self.scope(event)
        async with self.locks.setdefault(scope,asyncio.Lock()):
            ident=str(message_id or getattr(event.message_obj,'message_id',''))
            rows=await manager.get(event.get_platform_id(),scope,page_size=700)
            if ident and any(x.content.get('qq_id')==ident for x in rows):return
            await manager.insert(platform_id=event.get_platform_id(),user_id=scope,
                sender_id=str(sender or event.get_sender_id()),sender_name=name or event.get_sender_name(),
                content={'type':'bot' if kind=='bot' else 'user','message':parts,'qq_id':ident,
                         'p2c_kind':kind,'p2c_authors':authors or [str(sender or event.get_sender_id())],
                         'qq_time':when or getattr(event.message_obj,'timestamp',0)},max_messages=700)

    def parts(self,event):
        result=[]
        for component in event.get_messages():
            kind=component.__class__.__name__
            if kind=='Plain':result.append({'type':'plain','text':component.text})
            elif kind=='At':result.append({'type':'at','name':getattr(component,'name',''),'user_id':str(component.qq)})
            elif kind=='Reply':result.append({'type':'plain','text':'[引用消息 '+str(component.id)+']'})
            else:result.append({'type':'plain','text':'['+kind+']'})
        return result

    async def bootstrap(self,event):
        async with self.locks.setdefault('bootstrap:'+self.scope(event),asyncio.Lock()):
            await self._bootstrap(event)

    async def _bootstrap(self,event):
        scope=self.scope(event)
        if scope in self.bootstrapped:return
        # Import only this already-authorized group's public platform history.
        # This does not read private chats or restore invalidated summaries.
        self.bootstrapped.add(scope)
        try:
            history=await asyncio.wait_for(event.bot.call_action('get_group_msg_history',group_id=int(event.get_group_id()),count=300),4)
            rows=history.get('messages',[]) if isinstance(history,dict) else []
            cfg,policy=self.gateway.load();blocked=policy.blocked_for(str(event.get_group_id()))
            for row in sorted(rows,key=lambda x:x.get('time',0)):
                if row.get('message_type') not in {None,'group'}:continue
                if row.get('group_id') is not None and str(row['group_id'])!=str(event.get_group_id()):continue
                sender=str(row.get('sender',{}).get('user_id',''))
                if not sender or sender in blocked:continue
                parts=[]
                for item in row.get('message',[]) if isinstance(row.get('message'),list) else []:
                    data=item.get('data',{});kind=item.get('type')
                    if kind=='text':parts.append({'type':'plain','text':str(data.get('text',''))})
                    elif kind=='image':parts.append({'type':'plain','text':'[Image]'})
                    elif kind=='record':parts.append({'type':'plain','text':'[Record]'})
                    elif kind=='reply':parts.append({'type':'plain','text':'[引用消息 '+str(data.get('id',''))+']'})
                if not parts or ''.join(x.get('text','') for x in parts).strip().startswith('/'):continue
                bot=sender==str(event.get_self_id())
                if bot:continue
                await self.append(event,parts,sender=sender,name=row.get('sender',{}).get('nickname','群友'),
                    kind='bot' if bot else 'user',message_id=row.get('message_id'),when=row.get('time'),
                    authors=['__unattributed_bot__'] if bot else [sender])
        except asyncio.CancelledError:
            self.bootstrapped.discard(scope)
            raise
        except Exception:
            self.bootstrapped.discard(scope)

    async def context(self,event):
        cfg,policy=self.gateway.load();group=str(event.get_group_id());blocked=policy.blocked_for(group)
        rows=await self.gateway.context.message_history_manager.get(event.get_platform_id(),self.scope(event),page_size=700)
        rows.sort(key=lambda row:float((row.content or {}).get("qq_time",0) or 0))
        selected=[];authors=set();size=0
        for row in reversed(rows):
            if len(selected)>=120:break
            content=row.content or {}
            if float(content.get('qq_time',0) or 0)<cfg.get('group_history_from',{}).get(group,0):continue
            contributors=set(content.get('p2c_authors') or [str(row.sender_id or '')])
            if str(row.sender_id) in blocked or contributors & blocked:continue
            if '__unattributed_bot__' in contributors and (cfg.get('barrier_time') or cfg.get('group_barriers',{}).get(group)):continue
            text=''.join(part.get('text','[提及 '+part.get('name','群友')+']') for part in content.get('message',[]))
            if not text:continue
            kind=content.get('p2c_kind','user')
            if kind=='bot' and float(content.get('qq_time',0) or 0)<=self.gateway.barrier(cfg,group):continue
            who='Bot' if kind=='bot' else 'QQ系统通知' if kind=='system' else self.gateway.participation.label(group,str(row.sender_id))
            line=json.dumps({'发言者':who,'昵称':row.sender_name or '', '内容':text[:1800]},ensure_ascii=False)
            if size+len(line)>14000:break
            selected.append(line);size+=len(line)
            if kind=='user':authors.add(str(row.sender_id))
        event.set_extra('_feiyu_context_authors',sorted(authors))
        return '\n'.join(reversed(selected))

    async def persona(self,event):
        group=str(event.get_group_id());cfg,_=self.gateway.load();manager=self.gateway.context.persona_manager
        from .persona_settings import effective_notes
        ident='feiyu-group-'+group;base_id=ident+'-base';note=effective_notes(cfg,'',group)
        signature=(ident,note)
        async with self.locks.setdefault('persona:'+group,asyncio.Lock()):
            if self.personas_ready.get(group)!=signature:
                try:base=await manager.get_persona(base_id)
                except ValueError:
                    async with self.locks.setdefault('persona:public-base',asyncio.Lock()):
                        try:original=await manager.get_persona('feiyu-group-public-base')
                        except ValueError:
                            root=Path(os.environ['FEIYU_PERSONA_ROOT'])
                            prompt=(root/'Persona_Master.md').read_text(encoding='utf-8-sig')
                            original=await manager.create_persona('feiyu-group-public-base',prompt,begin_dialogs=[],
                                tools=['memorize_long_term_memory','web_search_deepseek','deepseek_fetch_web_page'],skills=[])
                    # Group bases never copy a private-selected or private-edited Persona.
                    base=await manager.create_persona(base_id,original.system_prompt,begin_dialogs=original.begin_dialogs,
                        tools=list(original.tools or []),skills=[])
                prompt=base.system_prompt+('\n本群经管理者设置的表达补充（不改变权限和事实边界）：'+note if note else '')
                try:current=await manager.get_persona(ident)
                except ValueError:current=await manager.create_persona(ident,prompt,begin_dialogs=base.begin_dialogs,tools=list(base.tools or []),skills=[])
                if current.system_prompt!=prompt:await manager.update_persona(ident,system_prompt=prompt)
                self.personas_ready[group]=signature
        services=await sp.get_async(scope='umo',scope_id=event.unified_msg_origin,key='session_service_config',default={}) or {}
        if services.get('persona_id')!=ident:
            services['persona_id']=ident
            await sp.put_async(scope='umo',scope_id=event.unified_msg_origin,key='session_service_config',value=services)
        event.set_extra('_feiyu_memory_persona',ident)

    async def capture_memory(self,event):
        info=self.gateway.context.get_registered_star('astrbot_plugin_livingmemory')
        plugin=info.star_cls if info and info.activated else None
        if not plugin:return
        ready,_=await plugin._ensure_plugin_ready()
        if not ready:return
        await plugin.event_handler.handle_all_group_messages(event)
        event.set_extra('_feiyu_skip_group_capture',True)
