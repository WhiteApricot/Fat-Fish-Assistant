"""Pre-wake QQ access boundary, deliberately independent of plugin filters."""
import asyncio
from copy import deepcopy
import hashlib
from datetime import datetime, timezone
import json
import time
from pathlib import Path
from fat_fish.permissions.policy import Policy, FriendDirectory, PendingActions
from fat_fish.participation.participation import Participation
from fat_fish.adapters.group_media import GroupMedia
from fat_fish.gateway.dedup import SeenMessages
from fat_fish.adapters.group_platform import GroupPlatform

ALIASES = {'comfirm':'confirm','remind':'reminder'}

def system_result(text):
    return '【系统】'+text


class Gateway:
    def __init__(self, path):
        self.path = Path(path)
        self.friends = FriendDirectory()
        self.pending = PendingActions()
        self.lock = asyncio.Lock()
        self.notices = {}
        self.command_history = {}
        self.active = {}
        self.participation = Participation()
        self.group_media = GroupMedia()
        self.group_platform=GroupPlatform()
        self.seen = SeenMessages(self.path.with_suffix(".seen.db"))
        self.forward_seen = SeenMessages(self.path.with_suffix('.forwards.db'),ttl=60)

    def epoch(self,cfg,group):
        return cfg.get('epoch',0)

    def barrier(self,cfg,group):
        return cfg.get('barrier_time',0)

    def load(self):
        cfg = json.loads(self.path.read_text(encoding='utf-8-sig'))
        from fat_fish.core.instance_identity import verify_owner
        verify_owner(self.path, cfg['owner'])
        for key in ('admins','blocked','approved_groups'):
            if not isinstance(cfg[key], list) or any(not isinstance(x,str) or not x.isdecimal() for x in cfg[key]):
                raise ValueError('Invalid identities')
        if not isinstance(cfg['groups'],dict) or not set(cfg['groups']).issubset(cfg['approved_groups']):
            raise ValueError('Unapproved group')
        self.participation.salt=cfg.get('identity_salt','')
        return cfg, Policy(cfg['owner'], set(cfg['admins']), set(cfg['blocked']), cfg['groups'])

    def save(self, cfg):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)

    def audit(self,kind,event,reason=''):
        # Hashes and event kinds only: no chat, nickname, QQ ID, token or code.
        try:
            actor=hashlib.sha256((self.participation.salt+'|actor|'+str(event.get_sender_id())).encode()).hexdigest()[:20]
            origin=hashlib.sha256((self.participation.salt+'|origin|'+str(event.get_group_id() or 'private:'+str(event.get_sender_id()))).encode()).hexdigest()[:20]
            now=datetime.now(timezone.utc)
            row={'time_utc':now.isoformat(),'kind':kind,'reason':reason,'actor':actor,'origin':origin,'private':event.is_private_chat()}
            with (self.path.parent/('p2c-audit-'+now.strftime('%Y%m%d')+'.jsonl')).open('a',encoding='utf-8') as out:
                out.write(json.dumps(row,ensure_ascii=False)+'\n')
        except OSError:
            pass

    def record_command(self,event,command,result,args=()):
        group=str(event.get_group_id() or '')
        key=(str(event.get_sender_id()),group)
        now=time.monotonic()
        if command=='new':
            getattr(self,'undo_records',{}).pop((key[0],event_scope(not group,group)),None)
            self.command_history.pop(key,None)
            return
        reset=command=='memory' and result.startswith((
            '本群可用的长期记忆与当前上下文已清除','本私聊的长期记忆已清除',
        ))
        if reset:
            undo=getattr(self,'undo_records',{})
            scope=event_scope(not group,group)
            for previous in tuple(undo):
                if previous==(key[0],scope) or (group and previous[1]==scope):undo.pop(previous,None)
            for previous in tuple(self.command_history):
                if previous==key or (group and previous[1]==group):
                    self.command_history.pop(previous,None)
            args=('reset',)
        if command in {'history','clarify'}:
            return
        from fat_fish.memory.experience import contains_secret
        from fat_fish.commands.intents import ROUTES
        label=command
        if args and args[0] in ROUTES.get(command,{}):
            label+=' '+args[0]
        target=event.get_extra('_ffa_reminder_reference',None) if command=='reminder' else None
        if reset:
            receipt='已清除当前会话的长期记忆和上下文，无法撤销'
        elif command in {'memory','groupmemory'} or (
            command in {'privatepersona','grouppersona','persona','globalpersona'} and (not args or args[0]!='reset')
        ):
            receipt='[已返回回执，记忆正文或自定义人格内容已隐藏]'
        else:
            receipt='[已返回回执，敏感内容已隐藏]' if contains_secret(result) else result[:800]
        if '90秒内发送' in receipt:
            # Current pending state is reconstructed separately from the policy.
            return
        recent=[entry for entry in self.command_history.pop(key,[]) if now-entry[0]<1800]
        self.command_history[key]=[*recent[-7:],(now,label,receipt,target)]
        if len(self.command_history)>512:
            self.command_history.pop(next(iter(self.command_history)))

    def admin_undo_reference(self,event):
        return None

    def command_records(self,event):
        key=(str(event.get_sender_id()),str(event.get_group_id() or ''))
        return [record for record in self.command_history.get(key,()) if time.monotonic()-record[0]<1800]

    def command_receipts(self,event):
        return [f'{command}：{receipt}' for _,command,receipt,_ in self.command_records(event)]

    def recent_commands(self,event):
        return self.command_receipts(event)

    async def reject(self, event, text=None, reason='stopped'):
        self.audit('decision',event,reason)
        event.stop_event()
        if text:
            await event.send(event.plain_result(text))
        return False

    async def process(self,event):
        from fat_fish.adapters.qq_reply_delay import bind_event_delay
        def prepare_recheck():
            before,policy=self.load()
            actor=str(event.get_sender_id());group=str(event.get_group_id() or '')
            epoch=self.epoch(before,group) if group and group in policy.groups else None
            def still_allowed():
                latest,current=self.load()
                if event.get_extra('_ffa_block_notice',False):
                    return actor in current.blocked_for(group) and (not group or (
                        group in current.groups and self.epoch(latest,group)==epoch))
                return actor not in current.blocked_for(group) and (epoch is None or (
                    group in current.groups and self.epoch(latest,group)==epoch))
            return still_allowed
        bind_event_delay(event,prepare_recheck)
        original=event.message_str
        admitted=await self._process(event)
        if admitted and event.get_platform_name()=='aiocqhttp':
            event.set_extra('_ffa_memory_dispatch_checked',True)
            event.set_extra('_ffa_internal_memory',original!=event.message_str and event.message_str.startswith('/记'))
        outcome=event.get_extra('_ffa_command_outcome',None)
        if outcome is not None:
            from fat_fish.persona.persona_reply import send_command_reply
            await send_command_reply(self,event,outcome,command=event.get_extra('_ffa_natural_command',None))
        return admitted

    async def _process(self, event):
        if event.get_platform_name() != 'aiocqhttp':
            return True
        from fat_fish.commands.intents import classify, contains_external_material, IntentUnavailable
        natural=None
        pending_snapshot=None
        undo_snapshot=None
        latest_undo=None
        raw_text=event.message_str.strip()
        actor=str(event.get_sender_id())
        group_id=str(event.get_group_id() or '')
        private_chat=event.is_private_chat()
        mentions={str(part.qq) for part in event.get_messages() if part.__class__.__name__=='At'}
        raw_message=getattr(getattr(event,'message_obj',None),'raw_message',None)
        if isinstance(raw_message,dict) and isinstance(raw_message.get('message'),list):
            for part in raw_message['message']:
                if isinstance(part,dict) and part.get('type')=='at' and isinstance(part.get('data'),dict):
                    mentions.add(str(part['data'].get('qq','')))
        mentions={qq for qq in mentions if qq.isascii() and qq.isdecimal()}
        context=getattr(self,'context',None)
        if (context is not None and 0<len(raw_text)<=2000 and not raw_text.startswith('/')
                and (private_chat or str(event.get_self_id()) in mentions)
                and not contains_external_material(event)):
            try:
                current_cfg, current_policy=self.load()
            except Exception:
                return await self.reject(event)
            if actor!=str(event.get_self_id()) and actor not in current_policy.blocked_for(group_id):
                admitted=(current_policy.level(actor)>=1 or await self.friends.contains(actor,event.bot)) if private_chat else group_id in current_policy.groups
                if admitted and current_cfg.get('naturalcommand',{}).get(group_id and 'group:'+group_id or 'private:'+actor,True):
                    scope=event_scope(private_chat,group_id)
                    pending_snapshot=self.pending.peek(actor,scope)
                    undo_snapshot=self.admin_undo_reference(event)
                    latest_undo=getattr(self,'undo_reference',lambda _:None)(event)
                    store=getattr(self,'reminders',None)
                    reminders=[]
                    if store is not None:
                        from fat_fish.memory.experience import contains_secret
                        reminders=[{'number':row['number'],'body':row['body'][:200]}
                                   for row in store.rows() if row['creator']==actor and row['scope']==group_id
                                   and row['state'] not in {'deleted','done'} and not contains_secret(row['body'])][:20]
                    from fat_fish.persona.persona_settings import persona_catalog
                    settings_key='group:'+group_id if group_id else 'private:'+actor
                    local_note=current_cfg.get('group_persona_notes' if group_id else 'private_persona_notes',{}).get(group_id or actor,'')
                    persona_numbers={'persona':[row[0] for row in persona_catalog(current_cfg,settings_key,local_note)['rows']]}
                    if current_policy.level(actor,False,group_id)>=2.5:
                        persona_numbers['globalpersona']=[
                            row[0] for row in persona_catalog(current_cfg,'global',current_cfg.get('global_persona_notes',''))['rows']]
                    try:
                        natural=await classify(context,event,raw_text,group_id,
                            [qq for qq in mentions if qq!=str(event.get_self_id())],
                            recent=self.recent_commands(event)+(['最新可撤销操作：'+latest_undo[2]] if latest_undo else []),pending=pending_snapshot[1] if pending_snapshot else None,
                            reminders=reminders,persona_numbers=persona_numbers)
                    except IntentUnavailable as exc:
                        self.audit('intent_unavailable',event,str(exc))
                        natural=None
        if natural and natural[0]=='clarify':
            self.audit('natural_fallback_chat',event,'ambiguous_intent')
            natural=None
        async with self.lock:
            try:
                cfg, policy = self.load()
            except Exception:
                return await self.reject(event)
            sender = str(event.get_sender_id())
            private = event.is_private_chat()
            group = str(event.get_group_id()) if not private else ''
            if sender == str(event.get_self_id()):
                return await self.reject(event,reason='self')
            if sender in policy.blocked_for(group):
                # Fixed, rate-limited denial only; never admit this event to plugins/LLM.
                raw=getattr(getattr(event,'message_obj',None),'raw_message',{})
                if (not isinstance(raw,dict) or raw.get('post_type','message')=='message') and (
                        private or (group in policy.groups and str(event.get_self_id()) in mentions)):
                    key=('blocked',sender,group)
                    now=time.monotonic()
                    if now-self.notices.get(key,-1e9)>=60:
                        self.notices[key]=now
                        while len(self.notices)>512:self.notices.pop(next(iter(self.notices)))
                        event.set_extra('_ffa_block_notice',True)
                        return await self.reject(event,system_result(
                            '你已被加入肥鱼助手的黑名单，暂时无法使用。若有疑问，请联系实例主人。'),reason='blocked_notice')
                return await self.reject(event,reason='blocked')
            if not private and group not in policy.groups:
                return await self.reject(event,reason='unapproved_group')
            ident=getattr(getattr(event,'message_obj',None),'message_id',None)
            if ident and not self.seen.claim(('private:'+sender) if private else ('group:'+group),ident):
                return await self.reject(event)
            platform_raw=getattr(getattr(event,'message_obj',None),'raw_message',{})
            if not isinstance(platform_raw,dict):platform_raw={}
            if platform_raw.get('post_type')=='request':
                return await self.reject(event)
            if platform_raw.get('post_type')=='notice' and not (private and platform_raw.get('notice_type')=='friend_recall'):
                if group:
                    self.participation.salt=cfg.get('identity_salt','')
                    notice=await self.group_platform.notice(group,platform_raw,event.bot,policy.blocked_for(group),lambda user:self.participation.label(group,user))
                    if notice:
                        self.participation.capture(group,self.epoch(cfg,group),'__system__',notice)
                        event.set_extra('_ffa_notice_text',notice)
                return await self.reject(event)
            if group and hasattr(policy,'qq_group_admins'):
                self.group_platform.bind_actor(policy,group,sender,platform_raw)
            friend = False
            if sender != policy.owner and (private or sender not in getattr(policy,'group_admins',{}).get(group,policy.admins)):
                friend = await self.friends.contains(sender, event.bot)
            level = policy.level(sender, friend, group)
            event.set_extra('_ffa_level', level)
            event.set_extra('_ffa_blocked_ids', tuple(policy.blocked_for(group)))
            if private and level == 0:
                text = '请先添加肥鱼助手为 QQ 好友，再来私聊吧。请联系此实例的主人说明来意；好友申请由主人审核。主人 QQ：'+policy.owner
                result=await self.reject(event,text,reason='stranger_private')

                return result
            # Drop an entire quoted message when its original author is blocked.
            # Avoid retaining text or media through another user's quote.
            for segment in event.get_messages():
                if not private and self.barrier(cfg,group):
                    if segment.__class__.__name__=='Reply' and float(getattr(segment,'time',0) or 0)<=self.barrier(cfg,group):
                        return await self.reject(event)
                if segment.__class__.__name__ == 'Reply' and str(getattr(segment,'sender_id','')) in policy.blocked_for(group):
                    return await self.reject(event)
            raw = event.message_str.strip()
            import re
            event.set_extra('_ffa_execute_material',bool(re.fullmatch(r'(?:请)?(?:执行|完成)(?:这份|该|这段)?(?:合并转发|转发|聊天记录|记录)(?:里|中|中的|里的)?(?:的)?(?:任务|事项)',raw)))
            if natural and not cfg.get('naturalcommand',{}).get('group:'+group if group else 'private:'+sender,True):
                natural=None
            if natural:
                command, arguments=natural
                if command in {'persona','globalpersona'} and arguments[:1] in (['edit'],['remove']):
                    from fat_fish.persona.persona_settings import persona_catalog
                    if command=='globalpersona':
                        key='global'
                        old_note=current_cfg.get('global_persona_notes','')
                        new_note=cfg.get('global_persona_notes','')
                    else:
                        key='group:'+group if group else 'private:'+sender
                        field='group_persona_notes' if group else 'private_persona_notes'
                        old_note=current_cfg.get(field,{}).get(group or sender,'')
                        new_note=cfg.get(field,{}).get(group or sender,'')
                    if persona_catalog(current_cfg,key,old_note)!=persona_catalog(cfg,key,new_note):
                        return await self.reject(event,system_result('人格条目已变化，请重新查看编号。'))
                if command=='role' and arguments==['undo'] and undo_snapshot!=self.admin_undo_reference(event):
                    return await self.reject(event,system_result('近期授权已变化，请重新核对。'))
                if command in {'confirm','cancel'} or (command in {'memory','groupmemory'} and arguments in (['confirm'],['cancel'])):
                    current=self.pending.peek(sender,event_scope(private,group))
                    if current!=pending_snapshot:
                        return await self.reject(event,system_result('待确认操作已变化，请重新核对。'))
                if natural:
                    event.set_extra('_ffa_natural',True)
                    event.set_extra('_ffa_natural_command',command)
                    raw='/'+command+(' '+' '.join(arguments) if arguments else '')
                    event.message_str=raw
            if raw.startswith('/'):
                event.set_extra('_ffa_reply_is_command',True)
                parts = [natural[0],*natural[1]] if natural else raw[1:].split()
                if not parts:
                    return await self.reject(event)
                cmd = ALIASES.get(parts[0], parts[0].lower())
                args = parts[1:]
                if not natural:
                    if not parts[0].isascii():
                        return await self.reject(event,system_result('请使用 /help 查看英文指令。'))
                    from fat_fish.commands.command_help import HELP, SYNTAX
                    if args==['help'] and cmd in HELP:
                        return await self.reject(event,system_result(self.command(
                            cfg,policy,sender,level,private,group,'help',[cmd],event=event)))
                    if args and cmd in SYNTAX and cmd not in {'help','undo','confirm'}:
                        variants=SYNTAX[cmd]
                        allowed={suffix.split()[0] for suffix in variants if suffix}
                        if allowed and args[0] not in allowed:
                            return await self.reject(event,system_result('请使用 /help '+cmd+' 查看英文指令用法。'))
                    if cmd=='role' and ((args[:1]==['set'] and len(args)>=3 and not args[2].isascii())
                                        or (args[:1]==['global'] and len(args)>=2 and args[1] not in {'list','add','remove'})):
                        return await self.reject(event,system_result('请使用 /help role 查看英文指令用法。'))
                if not natural and cmd in {'persona','globalpersona'} and args:
                    _,_,body=raw[1:].partition(' ')
                    action,_,payload=body.strip().partition(' ')
                    if action in {'set','add'} and payload.strip():
                        args=[action,payload.strip()]
                    elif action=='edit':
                        number,_,content=payload.strip().partition(' ')
                        if content.strip():args=[action,number,content.strip()]
                if cmd=='new':
                    parts[0]='new';event.set_extra('_ffa_personal_native',True);event.message_str='/new'+(' '+' '.join(args) if args else '')
                if cmd=='memory' and args and private and level>=1:
                    operations={'help':'记忆帮助','remember':'记住','search':'记忆查询','update':'记忆更新','delete':'记忆删除','reset':'记忆重置','confirm':'记忆确认','cancel':'记忆取消'}
                    if args[0] not in operations:
                        return await self.reject(event,system_result('/memory help|remember|search|update|delete|reset|confirm|cancel'))
                    event.message_str='/'+operations[args[0]]+(' '+' '.join(args[1:]) if len(args)>1 else '')
                    return True
                if cmd in {'reminder','globaladmin','help','history','clarify','cancel','undo','role','whitelist','blacklist','confirm','groupmode','groupsettings','personal','memory','grouppersona','groupmemory','privatepersona','persona','globalpersona','naturalcommand','webresearch'}:
                    if group and hasattr(policy,'qq_group_admins') and (
                        (cmd=='undo') or (cmd=='role' and args and args[0] in {'list','remove','set'}) or (cmd=='blacklist' and len(args)==2)
                    ):
                        roster_verified=await self.group_platform.bind_roster(policy,group,event.bot,sender)
                        if level==2 and not roster_verified and (cmd=='undo' or (cmd=='blacklist' and args[0]=='add')):
                            text='暂时无法核实目标的QQ群管理身份，请稍后再试'
                            if event.get_extra('_ffa_natural',False):event.set_extra('_ffa_command_outcome',text)
                            return await self.reject(event,system_result(text),reason='group_roster_unavailable')
                    if cmd=='role' and args[:2]==['add','']:
                        text='请明确指定目标，在这条消息中只 @ 一位要授权的成员，并同时 @ 肥鱼助手'
                    else:
                        text=None
                    before=deepcopy(cfg)
                    try:
                        if cmd=='undo':
                            entry=latest_undo if natural else self.undo_reference(event)
                            if entry and (not args or args==[entry[2]]):
                                text=self.restore_recent(cfg,policy,sender,level,private,group,event,entry)
                            else:
                                text='当前没有匹配的可撤销操作，或该操作已过期。'
                        elif cmd=='clarify':
                            text='请明确要执行的操作、目标和范围。'
                        elif cmd=='cancel':
                            scope=event_scope(private,group)
                            item=self.pending.peek(sender,scope)
                            text='当前没有有效的待确认操作。'
                            if item:
                                if item[1]=='memory_reset':
                                    event.message_str='/记忆取消'
                                    if group:event.set_extra('_ffa_group_memory_command',True)
                                    return True
                                self.pending.consume(sender,scope,item[0])
                                text='已取消待确认操作。'
                        elif cmd=='history':
                            receipts=self.command_receipts(event)
                            text=('\n'.join(receipts) if args==['all'] else '上一条指令 /'+receipts[-1]) if receipts else '当前会话没有可查询的近期指令回执'
                        elif text is None:
                            text=self.command(cfg,policy,sender,level,private,group,cmd,args,event=event)
                    except Exception:
                        self.audit('command_error',event,cmd)
                        text='指令执行失败，不能确认是否完整生效；请检查本机日志后再试'
                    if text is None:return True
                    self.audit('command',event,cmd)
                    history_cmd,history_args=event.get_extra('_ffa_confirmed_command',(cmd,args))
                    if cmd not in {'undo','cancel'} and not event.get_extra('_ffa_undo_applied',False) and hasattr(self,'remember_undo'):
                        self.remember_undo(event,level,history_cmd,history_args,before,cfg,text)
                    if cmd!='cancel':self.record_command(event,history_cmd,text,history_args)
                    result=await self.reject(event,system_result(text))
                    if event.get_extra('_ffa_natural',False):event.set_extra('_ffa_command_outcome',text)
                    return result
                # Existing personal-memory commands stay in the P2B handler.
                event.set_extra('_ffa_personal_native',parts[0]=='new')
                personal = parts[0] in {'记住','记忆帮助','记忆查询','记忆更新','记忆删除','记忆重置','记忆确认','记忆取消','new'}
                if not (private and level >= 1 and personal) and level != 3:
                    return await self.reject(event, system_result('这个指令不在你的权限范围内，使用 /help 查看'))
            if not private:
                # Mention mode is strict: / wake prefixes and quoted Bot messages
                # cannot silently enable ordinary group chatting.
                mention = any(x.__class__.__name__ == 'At' and str(getattr(x,'qq','')) == str(event.get_self_id()) for x in event.get_messages())
                self.group_media.observe(event,self.epoch(cfg,group),mentioned=mention)
                # Commands and rejected users never enter the shared window.
                if not raw.startswith('/'):
                    self.participation.salt=cfg.get('identity_salt','')
                    speaker=self.participation.label(group,sender)
                    name=event.get_sender_name() if hasattr(event,'get_sender_name') else ''
                    role=self.group_platform.observe(group,sender,name,platform_raw)
                    self.participation.capture(group,self.epoch(cfg,group),speaker,('昵称：'+name+'；' if name else '')+'QQ群身份：'+role+'；'+raw,raw_text=raw)
                if not mention:
                    if policy.groups[group]=='smart':return 'smart'
                    if getattr(self,'full_group_context',False):return 'passive'
                    return await self.reject(event)
            return True

    def command(self, cfg, policy, sender, level, private, group, cmd, args, event=None):
        if cmd=='help':
            from fat_fish.commands.command_help import HELP
            if args:return HELP.get(ALIASES.get(args[0],args[0].lower()),'没有这个子菜单。')
            names=['help','role']
            if level>=1:names+=['personal','memory','new']
            if level>=2:names+=['whitelist','blacklist','groupmode','groupsettings','confirm']
            return '\n'.join('/'+name for name in names)
        if cmd=='role' and args==['list']:
            return '已授权管理员：'+('、'.join(cfg['admins']) or '暂无') if level==3 else '只有主人可以查看和管理授权名单'
        if cmd == 'role' and not args:
            return {3:'你是肥鱼助手的主人，可以管理聊天配置和其他人的管理权限',
                    2:'你是肥鱼助手的管理员，可以管理聊天行为和已授权群的设置；主人身份由主人保管',
                    1:'你是肥鱼助手的好友，可以私聊，也能在已授权的群里聊天；个人设置请在私聊中调整',
                    0:'你目前可以在已授权的群里与肥鱼助手聊天；想私聊请先联系主人添加好友'}[level]
        if cmd=='role' and args and args[0]=='set':
            roles={'管理员':'add','admin':'add','L2':'add','2':'add',
                   '普通好友':'remove','好友':'remove','friend':'remove','default':'remove','L1':'remove','1':'remove'}
            if len(args)!=3 or not args[1].isdecimal() or args[2] not in roles:
                return '格式：/role set QQ 管理员|普通好友；主人身份不能通过指令转让'
            args=[roles[args[2]],args[1]]
        if cmd in {'personal','memory'}:
            if not policy.authorize(sender, cmd, private=private, friend=level>=1):
                return '个人操作只接受好友在私聊中使用'
            if cmd=='memory':
                from fat_fish.commands.command_help import HELP
                return HELP['memory']
            values={'short':'简短','简短':'简短','normal':'标准','标准':'标准','detailed':'详细','详细':'详细'}
            if not args:return '个人回复长度：'+cfg.get('preferences',{}).get(sender,'标准')+'；/personal short|normal|detailed'
            if len(args)!=1 or args[0] not in values:return '格式：/personal 简短|标准|详细'
            cfg.setdefault('preferences',{})[sender]=values[args[0]];self.save(cfg)
            return '个人回复长度已设为 '+values[args[0]]
        if cmd in {'groupmode','groupsettings'}:
            if not policy.authorize(sender,'group',private=private,group=group):
                return '仅 L2/L3 可在授权群设置本群行为'
            if cmd=='groupsettings':
                if not args:return '本群冷却秒数：'+str(cfg.get('cooldowns',{}).get(group,120))+'；/groupsettings cooldown 120（30至1800秒）'
                if len(args)==2 and args[0] in {'cooldown','冷却'} and args[1].isdigit() and 30<=int(args[1])<=1800:
                    cfg.setdefault('cooldowns',{})[group]=int(args[1]);self.save(cfg);return '已调整本群智能插话冷却'
                return '格式：/groupsettings 冷却 120，范围30至1800秒'
            modes={'mention':'mention','@':'mention','@触发':'mention','smart':'smart','智能':'smart','智能插话':'smart'}
            if not args:return '本群模式：'+policy.groups[group]+'；/groupmode mention|smart'
            if len(args)!=1 or args[0] not in modes:return '格式：/groupmode @触发|智能插话'
            mode=modes[args[0]]
            code=self.pending.issue(sender,event_scope(private,group),'groupmode',(group,mode))
            return '90秒内发送 /confirm '+code+' 确认本群模式变更' 
        if cmd == 'confirm':
            item = self.pending.consume(sender, event_scope(private,group), args[0] if len(args)==1 else '')
            if not item:
                return '确认无效、已过期或不属于当前会话'
            operation, arguments = item
            return self.apply(cfg, policy, sender, level, operation, arguments)
        operation = {'role':'admin','whitelist':'whitelist','blacklist':'blacklist'}.get(cmd)
        if not operation:
            return '指令格式不正确'
        if args==['list'] and operation in {'whitelist','blacklist'}:
            if level<2:return '权限不足'
            values=cfg['groups'] if operation=='whitelist' else cfg['blocked']
            return '当前名单：'+('、'.join(values) or '暂无')
        if not args:
            if level < (3 if operation=='admin' else 2):
                return '权限不足'
            values = cfg[{'admin':'admins','whitelist':'groups','blacklist':'blocked'}[operation]]
            return '当前数量：'+str(len(values))
        if len(args)!=2 or args[0] not in {'add','remove'} or not args[1].isdecimal():
            return '格式：/'+cmd+' add|remove QQ或群号'
        if not policy.authorize(sender,operation,private=private,target=args[1]):
            return '权限不足；主人身份不可通过聊天命令修改'
        # Adding groups beyond the explicitly approved test scope is forbidden.
        if operation=='whitelist' and args[0]=='add' and args[1] not in cfg['approved_groups']:
            return '该群未获本机明确授权，不能扩大开放范围'
        code=self.pending.issue(sender,event_scope(private,group),operation,args)
                    # Exact target/action is shown before the confirmation is accepted.
        labels={'admin':'管理权限','whitelist':'群白名单','blacklist':'用户黑名单'}
        verb='添加到' if args[0]=='add' else '移出'
        return '将 '+args[1]+' '+verb+labels[operation]+'。90秒内发送 /confirm '+code+' 确认' 

    def apply(self,cfg,policy,sender,level,operation,args):
        action,target=args
        if operation=='groupmode':
            if not policy.authorize(sender,'group',private=False,group=action):return '当前权限不足'
            cfg['groups'][action]=target;self.save(cfg);return '本群已切换为 '+target
        if not policy.authorize(sender,operation,private=True,target=target):
            return '当前权限不足，未执行'
        if operation=='admin':
            values=set(cfg['admins'])
            values.add(target) if action=='add' else values.discard(target)
            cfg['admins']=sorted(values)
        elif operation=='whitelist':
            if action=='add':
                if target not in cfg['approved_groups']:return '该群未获授权'
                cfg['groups'][target]='mention'
            else:cfg['groups'].pop(target,None)
        elif operation=='blacklist':
            values=set(cfg['blocked'])
            values.add(target) if action=='add' else values.discard(target)
            cfg['blocked']=sorted(values)
            cfg['epoch']=cfg.get('epoch',0)+1
            cfg['barrier_time']=time.time()
        else:return '操作未开放'
        self.save(cfg)
        if operation=='blacklist':
            # A new group namespace makes every old group history unreachable.
            # Cancel all active group turns, not just the blocked sender's turn:
            # another user's response could contain their previously quoted data.
            current=asyncio.current_task()
            for task,(actor,group_id) in tuple(self.active.items()):
                if task is not current and (actor==target or group_id):task.cancel()
        return '已保存这项设置；好友身份仍以 QQ 实际好友关系为准'

    async def run(self, event, downstream):
        if event.get_platform_name() != 'aiocqhttp':
            return await downstream()
        decision=await self.process(event)
        if not decision:return
        parts=event.get_messages()
        if any(p.__class__.__name__ in {'Image','Forward','Node','Nodes','File','Record','Video'} for p in parts):
            has_text=any(p.__class__.__name__=='Plain' and p.text.strip() for p in parts)
            event.set_extra('_ffa_reply_window',(event.get_extra('_ffa_reply_started',time.monotonic()),not has_text))
        if any(p.__class__.__name__ == 'File' for p in event.get_messages()):
            from fat_fish.integrations.text_files import read_attachments
            from astrbot.api.message_components import Plain
            before,policy=self.load();group=str(event.get_group_id() or '')
            epoch=self.epoch(before,group)
            material=await read_attachments(event.get_messages())
            latest,current=self.load()
            if str(event.get_sender_id()) in current.blocked_for(group) or (group and (
                    group not in current.groups or self.epoch(latest,group)!=epoch)):
                return await self.reject(event)
            # Feed text through the existing collector so a following question joins it.
            event.get_messages()[:]=[p for p in event.get_messages() if p.__class__.__name__!='File']+[Plain(material)]
            event.message_str+='\n'+material
            event.message_obj.message_str=event.message_str
        from fat_fish.integrations.forwards import recover_forward_components
        recover_forward_components(event)
        if any(p.__class__.__name__ in {'Forward','Node','Nodes'} for p in event.get_messages()):
            reader=getattr(self,'forward_reader',None)
            if reader is None or not reader.available():return await self.reject(event,'合并转发阅读插件或消息收集插件未启用')
            cfg,policy=self.load();group=str(event.get_group_id() or '')
            epoch=self.epoch(cfg,group)
            ids=[str(p.id) for p in event.get_messages() if p.__class__.__name__=='Forward']
            claimed={ident for ident in ids if self.forward_seen.claim(event.get_platform_id()+':'+group+':'+str(event.get_sender_id())+':'+str(epoch),ident)}
            if ids and not claimed:
                return await self.reject(event,'这份转发记录刚刚已接收，已避免重复处理；需要再次分析请稍后重新发送')
            if ids:event.get_messages()[:]=[p for p in event.get_messages() if p.__class__.__name__!='Forward' or str(p.id) in claimed]
            material,images=await reader.read(event,policy.blocked_for(group),self.barrier(cfg,group) if group else 0)
            latest,p=self.load()
            if str(event.get_sender_id()) in p.blocked_for(group) or self.epoch(latest,group)!=epoch:return await self.reject(event)
            from astrbot.api.message_components import Plain,Image
            event.get_messages()[:]=[p for p in event.get_messages() if p.__class__.__name__ not in {'Forward','Node','Nodes'}]+[Plain(material)]+[Image(file=ref,url=ref) for ref in images]
            event.message_str+='\n'+material
            event.message_obj.message_str=event.message_str
            event.set_extra('_ffa_material',True)
        if not event.is_private_chat() and decision!='passive':
            cfg,policy=self.load();group=str(event.get_group_id());epoch=self.epoch(cfg,group)
            event.session_id='p2c'+str(epoch)+'_'+group
            def still_allowed():
                latest,current=self.load()
                return (group in current.groups and str(event.get_sender_id()) not in current.blocked_for(group)
                    and self.epoch(latest,group)==epoch
                    and (decision!='smart' or current.groups.get(group)=='smart'))
            changed=await self.group_media.transcribe(event,epoch,getattr(self,'context',None),still_allowed)
            if changed:
                self.participation.capture(group,epoch,self.participation.label(group,str(event.get_sender_id())),event.message_str,raw_text=event.message_str)
        if getattr(self,'full_group_context',False) and not event.is_private_chat():
            await self.prepare_group(event)
        if decision=='passive':
            event.stop_event();return
        if decision=='smart':
            before,policy=self.load()
            group=str(event.get_group_id())
            # Make explicit quote intent visible before read-air. The normal
            # response path below re-resolves it after authorization recheck.
            for part in event.get_messages():
                if part.__class__.__name__=='Reply':
                    from fat_fish.gateway.quote_thread import resolve_quote_thread
                    quoted,_=await resolve_quote_thread(event,part,
                        lambda author,name:'Bot' if str(author)==str(event.get_self_id()) else self.participation.label(group,str(author)))
                    if not quoted:
                        event.stop_event();return
                    event.set_extra('_ffa_smart_quote',quoted)
                    break
            context=await self.participation.choose(group,self.epoch(before,group),before.get('cooldowns',{}).get(group,120),quote_context=event.get_extra('_ffa_smart_quote',''))
            after,current_policy=self.load()
            if context is None or self.epoch(after,group)!=self.epoch(before,group) or current_policy.groups.get(group)!='smart' or str(event.get_sender_id()) in current_policy.blocked_for(group):
                room=self.participation.rooms.get((group,self.epoch(before,group)),{})
                self.audit('decision',event,'smart_'+room.get('reason','revoked'))
                event.stop_event();return
            event.set_extra('_ffa_smart',True)
            event.is_at_or_wake_command=True;event.is_wake=True
        sender=str(event.get_sender_id())
        group=str(event.get_group_id() or '')
        cfg,_=self.load()
        epoch=self.epoch(cfg,group)
        if group:
            # get_group_id() remains the platform group; only conversational
            # history receives this security namespace.
            event.session_id='p2c'+str(epoch)+'_'+group
            self.participation.salt=cfg.get('identity_salt','')
            event.set_extra('_ffa_group_context',event.get_extra('_ffa_durable_group_context',self.participation.context(group,epoch)))
            event.set_extra('_ffa_group_speaker',self.participation.label(group,sender))
            roles=await self.group_platform.summary(group,event.bot,self.load()[1].blocked_for(group),lambda user:self.participation.label(group,user))
            event.set_extra('_ffa_qq_group_roles',roles)
            checked,checked_policy=self.load()
            if sender in checked_policy.blocked_for(group) or self.epoch(checked,group)!=epoch or group not in checked_policy.groups:
                event.stop_event();return
            for part in event.get_messages():
                if part.__class__.__name__=='Reply':
                    from fat_fish.gateway.quote_thread import resolve_quote_thread
                    def formatter(author,name):
                        return 'Bot' if str(author)==str(event.get_self_id()) else name+' ['+self.participation.label(group,str(author))+']'
                    quoted,images=await resolve_quote_thread(event,part,formatter)
                    latest,latest_policy=self.load()
                    if not quoted or sender in latest_policy.blocked_for(group) or self.epoch(latest,group)!=epoch or group not in latest_policy.groups:
                        event.stop_event();return
                    event.set_extra('_ffa_quoted_context',quoted)
                    event.set_extra('_ffa_quoted_images',images)
                    # Route ancestor images through normal media preprocessing.
                    # Passing raw QQ URLs from a request hook skips localization.
                    from astrbot.api.message_components import Image
                    present=set()
                    for component in event.get_messages():
                        candidates=[component]+list(getattr(component,'chain',None) or [])
                        for candidate in candidates:
                            if candidate.__class__.__name__=='Image':
                                present.add(getattr(candidate,'url',None) or getattr(candidate,'file',None))
                    for ref in images:
                        if ref not in present:
                            event.get_messages().append(Image(file=ref,url=ref))
                            present.add(ref)
                    break
        original_send=event.send
        async def checked_send(message):
            if event.get_extra('_ffa_system_sent',False) and not event.get_extra('_ffa_sending_receipt',False):
                return
            current,policy=self.load()
            if sender in policy.blocked_for(group) or (group and (group not in policy.groups or self.epoch(current,group)!=event.get_extra('_ffa_security_epoch',epoch) or (event.get_extra('_ffa_smart',False) and policy.groups.get(group)!='smart'))):
                event.stop_event()
                return
            if event.get_extra('_ffa_personal_native',False) and hasattr(message,'chain'):
                from astrbot.api.message_components import Plain
                message.chain.insert(0,Plain(text='【系统】'))
            from fat_fish.integrations.web_search import clean_message
            error=event.get_extra('_llm_error_message')
            if error and hasattr(message,'get_plain_text') and message.get_plain_text().strip()==str(error).strip():
                message=event.plain_result('这次回复没有生成成功，请稍后再试。')
            elif clean_message(message) and not message.chain:
                if event.get_extra('_ffa_diagnostic_replaced',False):return
                event.set_extra('_ffa_diagnostic_replaced',True)
                message=event.plain_result('刚才没能整理出有效回复，请再试一次。')
            result=await original_send(message)
            if event.get_extra('_ffa_personal_native',False):
                self.record_command(event,'new','')
            self.audit('send',event)
            if event.get_extra('_ffa_natural',False) and event.get_extra('_ffa_personal_native',False) and not event.get_extra('_ffa_new_feedback',False):
                event.set_extra('_ffa_new_feedback',True)
                outcome=message.get_plain_text() if hasattr(message,'get_plain_text') else str(message)
                from fat_fish.persona.persona_reply import send_command_reply
                await send_command_reply(self,event,outcome,send=original_send)
            if group:
                text=message.get_plain_text() if hasattr(message,'get_plain_text') else ''
                room=self.participation.capture(group,epoch,'__bot__',text or '[Bot已回复图片或其他内容]')
                room['reply_to']=self.participation.label(group,sender)
                room['last']=time.monotonic()
                if not event.get_extra('_ffa_smart',False):room['followups']=0
                if getattr(self,'full_group_context',False):await self.after_group_send(event,message)
            return result
        event.set_extra('_ffa_preference',cfg.get('preferences',{}).get(sender,'标准') if not group else '标准')
        event.set_extra('_ffa_security_epoch',epoch)
        event.set_extra('_ffa_access_checked',True)
        self.audit('admitted',event)
        event.send=checked_send
        task=asyncio.current_task()
        self.active[task]=(sender,group)
        try:
            return await downstream()
        finally:
            self.active.pop(task,None)
            event.send=original_send

def event_scope(private, group):
    return 'private' if private else 'group:'+group
