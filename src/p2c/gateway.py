"""Pre-wake QQ access boundary, deliberately independent of plugin filters."""
import asyncio
import hashlib
from datetime import datetime, timezone
import json
import time
from pathlib import Path
from .policy import Policy, FriendDirectory, PendingActions
from p2d.participation import Participation
from p2d.group_media import GroupMedia
from .dedup import SeenMessages
from .group_platform import GroupPlatform

ALIASES = {'帮助':'help','权限':'role','白名单':'whitelist','黑名单':'blacklist',
           '确认':'confirm','群聊模式':'groupmode','群聊设置':'groupsettings',
           '个人设置':'personal','记忆管理':'memory','新对话':'new','comfirm':'confirm','群人格':'grouppersona','群记忆':'groupmemory','私聊人格':'privatepersona'}

class Gateway:
    def __init__(self, path):
        self.path = Path(path)
        self.friends = FriendDirectory()
        self.pending = PendingActions()
        self.lock = asyncio.Lock()
        self.notices = {}
        self.active = {}
        self.participation = Participation()
        self.group_media = GroupMedia()
        self.group_platform=GroupPlatform()
        self.seen = SeenMessages(self.path.with_suffix(".seen.db"))

    def epoch(self,cfg,group):
        return cfg.get('epoch',0)

    def barrier(self,cfg,group):
        return cfg.get('barrier_time',0)

    def load(self):
        cfg = json.loads(self.path.read_text(encoding='utf-8-sig'))
        from .instance_identity import verify_owner
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

    async def reject(self, event, text=None, reason='stopped'):
        self.audit('decision',event,reason)
        event.stop_event()
        if text:
            await event.send(event.plain_result(text))
        return False

    async def process(self, event):
        if event.get_platform_name() != 'aiocqhttp':
            return True
        async with self.lock:
            try:
                cfg, policy = self.load()
            except Exception:
                return await self.reject(event)
            sender = str(event.get_sender_id())
            private = event.is_private_chat()
            group = str(event.get_group_id()) if not private else ''
            if sender in policy.blocked_for(group) or sender == str(event.get_self_id()):
                return await self.reject(event,reason='blocked_or_self')
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
                        event.set_extra('_feiyu_notice_text',notice)
                return await self.reject(event)
            friend = False
            if sender != policy.owner and (private or sender not in getattr(policy,'group_admins',{}).get(group,policy.admins)):
                friend = await self.friends.contains(sender, event.bot)
            level = policy.level(sender, friend, group)
            event.set_extra('_feiyu_level', level)
            event.set_extra('_feiyu_blocked_ids', tuple(policy.blocked_for(group)))
            if private and level == 0:
                text = '请先添加肥鱼助手为 QQ 好友，再来私聊吧。请联系此实例的主人说明来意；好友申请由主人审核'
                return await self.reject(event, text,reason='stranger_private')
            # Drop an entire quoted message when its original author is blocked.
            # Avoid retaining text or media through another user's quote.
            for segment in event.get_messages():
                if not private and self.barrier(cfg,group):
                    if segment.__class__.__name__ in {'Forward','Node','Nodes'}:
                        return await self.reject(event)
                    if segment.__class__.__name__=='Reply' and float(getattr(segment,'time',0) or 0)<=self.barrier(cfg,group):
                        return await self.reject(event)
                if segment.__class__.__name__ == 'Reply' and str(getattr(segment,'sender_id','')) in policy.blocked_for(group):
                    return await self.reject(event)
            raw = event.message_str.strip()
            if raw.startswith('/'):
                parts = raw[1:].split()
                if not parts:
                    return await self.reject(event)
                cmd = ALIASES.get(parts[0], parts[0].lower())
                args = parts[1:]
                if cmd=='new':
                    parts[0]='new';event.message_str='/new'+(' '+' '.join(args) if args else '')
                if cmd=='memory' and args and private and level>=1:
                    operations={'help':'记忆帮助','remember':'记住','search':'记忆查询','update':'记忆更新','delete':'记忆删除','reset':'记忆重置','confirm':'记忆确认','cancel':'记忆取消'}
                    if args[0] not in operations:
                        return await self.reject(event,'/memory help|remember|search|update|delete|reset|confirm|cancel；也可使用中文记忆指令')
                    event.message_str='/'+operations[args[0]]+(' '+' '.join(args[1:]) if len(args)>1 else '')
                    return True
                if cmd in {'help','role','whitelist','blacklist','confirm','groupmode','groupsettings','personal','memory','grouppersona','groupmemory','privatepersona'}:
                    text = self.command(cfg, policy, sender, level, private, group, cmd, args, event=event)
                    if text is None:return True
                    return await self.reject(event, text)
                # Existing personal-memory commands stay in the P2B handler.
                event.set_extra('_feiyu_personal_native',parts[0]=='new')
                personal = parts[0] in {'记住','记忆帮助','记忆查询','记忆更新','记忆删除','记忆重置','记忆确认','记忆取消','new'}
                if not (private and level >= 1 and personal) and level != 3:
                    return await self.reject(event, '这个指令不在你的权限范围内，使用 /help 查看')
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
        if cmd == 'help' and not args:
            lines=['/help 或 /帮助：查看你能使用的指令',
                   '/role 或 /权限：查看你与肥鱼助手的关系']
            if level>=1:
                lines+=['/personal 或 /个人设置：在私聊中设置回复详略',
                        '/memory 或 /记忆管理：在私聊中查看本人记忆操作',
                        '/new 或 /新对话：在私聊中开始新对话']
            if level >= 2:
                lines+=['/whitelist 或 /白名单：查看、加入或移除已授权的群',
                        '/blacklist 或 /黑名单：拉黑或解除拉黑用户',
                        '/groupmode 或 /群聊模式：在本群切换 @触发或智能插话',
                        '/groupsettings 或 /群聊设置：调整本群插话冷却',
                        '/confirm 或 /确认：确认刚才申请的变更']
            return '\n'.join(lines)
        if cmd=='help' and args:
            target=ALIASES.get(args[0],args[0])
            return self.command(cfg,policy,sender,level,private,group,target,['help']) if target in {'role','whitelist','blacklist','groupmode','groupsettings','personal','memory'} else '没有这个子菜单'
        if args==['help'] or args==['帮助']:
            if cmd=='role':
                if level==3:return '/权限：查看自己的身份\n/权限 list：查看已授权的管理员\n/权限 set QQ 管理员：授予管理权限\n/权限 set QQ 普通好友：撤销管理权限，好友身份由 QQ 判断\n/role add|remove QQ：英文写法\n变更需使用 /confirm 或 /确认 再次确认；不能转让主人身份'
                return '用 /权限 查看自己的身份；管理权限由主人授予或撤销'
            if cmd in {'whitelist','blacklist'}:
                if level<2:return '只有主人和管理员可以管理名单'
                root='白名单' if cmd=='whitelist' else '黑名单'
                return '/'+root+'：查看当前数量\n/'+root+' list：查看名单\n/'+root+' add QQ或群号：添加\n/'+root+' remove QQ或群号：移除\n变更需再次确认；群仅限已明确批准的范围'
            if cmd=='groupmode':return '/群聊模式：查看本群模式\n/群聊模式 @触发：只在被 @ 时回复\n/群聊模式 智能插话：允许谨慎接话\n/groupmode mention|smart：英文写法；模式变更需确认'
            if cmd=='groupsettings':return '/群聊设置：查看本群冷却\n/群聊设置 冷却 120：设置30至1800秒\n/groupsettings cooldown 120：英文写法'
            if cmd=='personal':return '/个人设置：查看本人回复详略\n/个人设置 简短|标准|详细：修改本人偏好\n/personal short|normal|detailed：英文写法；仅私聊可修改'
            if cmd=='memory':args=[]
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
                return '格式：/权限 set QQ 管理员|普通好友；主人身份不能通过指令转让'
            args=[roles[args[2]],args[1]]
        if cmd in {'personal','memory'}:
            if not policy.authorize(sender, cmd, private=private, friend=level>=1):
                return '个人操作只接受好友在私聊中使用'
            if cmd=='memory':return '/memory help 或 /记忆帮助：使用说明\n/memory remember 内容 或 /记住 内容：写入本人记忆\n/memory search 关键词 或 /记忆查询 关键词：查询本人记忆\n/memory update 编号 内容 或 /记忆更新 编号 内容：修改本人记忆\n/memory delete 编号 或 /记忆删除 编号：遗忘本人的单条记录\n/memory reset 或 /记忆重置：申请清除本人记忆\n/confirm 或 /确认：确认刚才的清除申请\n/memory cancel 或 /记忆取消：取消清除'
            values={'short':'简短','简短':'简短','normal':'标准','标准':'标准','detailed':'详细','详细':'详细'}
            if not args:return '个人回复长度：'+cfg.get('preferences',{}).get(sender,'标准')+'；/personal short|normal|detailed'
            if len(args)!=1 or args[0] not in values:return '格式：/个人设置 简短|标准|详细'
            cfg.setdefault('preferences',{})[sender]=values[args[0]];self.save(cfg)
            return '个人回复长度已设为 '+values[args[0]]
        if cmd in {'groupmode','groupsettings'}:
            if not policy.authorize(sender,'group',private=private,group=group):
                return '仅 L2/L3 可在授权群设置本群行为'
            if cmd=='groupsettings':
                if not args:return '本群冷却秒数：'+str(cfg.get('cooldowns',{}).get(group,120))+'；/groupsettings cooldown 120（30至1800秒）'
                if len(args)==2 and args[0] in {'cooldown','冷却'} and args[1].isdigit() and 30<=int(args[1])<=1800:
                    cfg.setdefault('cooldowns',{})[group]=int(args[1]);self.save(cfg);return '已调整本群智能插话冷却'
                return '格式：/群聊设置 冷却 120，范围30至1800秒'
            modes={'mention':'mention','@':'mention','@触发':'mention','smart':'smart','智能':'smart','智能插话':'smart'}
            if not args:return '本群模式：'+policy.groups[group]+'；/groupmode mention|smart'
            if len(args)!=1 or args[0] not in modes:return '格式：/群聊模式 @触发|智能插话'
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
                    from .quote_thread import resolve_quote_thread
                    quoted,_=await resolve_quote_thread(event,part,
                        lambda author,name:'Bot' if str(author)==str(event.get_self_id()) else self.participation.label(group,str(author)))
                    if not quoted:
                        event.stop_event();return
                    event.set_extra('_feiyu_smart_quote',quoted)
                    break
            context=await self.participation.choose(group,self.epoch(before,group),before.get('cooldowns',{}).get(group,120),quote_context=event.get_extra('_feiyu_smart_quote',''))
            after,current_policy=self.load()
            if context is None or self.epoch(after,group)!=self.epoch(before,group) or current_policy.groups.get(group)!='smart' or str(event.get_sender_id()) in current_policy.blocked_for(group):
                room=self.participation.rooms.get((group,self.epoch(before,group)),{})
                self.audit('decision',event,'smart_'+room.get('reason','revoked'))
                event.stop_event();return
            event.set_extra('_feiyu_smart',True)
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
            event.set_extra('_feiyu_group_context',event.get_extra('_feiyu_durable_group_context',self.participation.context(group,epoch)))
            event.set_extra('_feiyu_group_speaker',self.participation.label(group,sender))
            roles=await self.group_platform.summary(group,event.bot,self.load()[1].blocked_for(group),lambda user:self.participation.label(group,user))
            event.set_extra('_feiyu_qq_group_roles',roles)
            checked,checked_policy=self.load()
            if sender in checked_policy.blocked_for(group) or self.epoch(checked,group)!=epoch or group not in checked_policy.groups:
                event.stop_event();return
            for part in event.get_messages():
                if part.__class__.__name__=='Reply':
                    from .quote_thread import resolve_quote_thread
                    def formatter(author,name):
                        return 'Bot' if str(author)==str(event.get_self_id()) else name+' ['+self.participation.label(group,str(author))+']'
                    quoted,images=await resolve_quote_thread(event,part,formatter)
                    latest,latest_policy=self.load()
                    if not quoted or sender in latest_policy.blocked_for(group) or self.epoch(latest,group)!=epoch or group not in latest_policy.groups:
                        event.stop_event();return
                    event.set_extra('_feiyu_quoted_context',quoted)
                    event.set_extra('_feiyu_quoted_images',images)
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
            current,policy=self.load()
            if sender in policy.blocked_for(group) or (group and (group not in policy.groups or self.epoch(current,group)!=event.get_extra('_feiyu_security_epoch',epoch) or (event.get_extra('_feiyu_smart',False) and policy.groups.get(group)!='smart'))):
                event.stop_event()
                return
            result=await original_send(message)
            self.audit('send',event)
            if group:
                text=message.get_plain_text() if hasattr(message,'get_plain_text') else ''
                room=self.participation.capture(group,epoch,'__bot__',text or '[Bot已回复图片或其他内容]')
                room['reply_to']=self.participation.label(group,sender)
                room['last']=time.monotonic()
                if not event.get_extra('_feiyu_smart',False):room['followups']=0
                if getattr(self,'full_group_context',False):await self.after_group_send(event,message)
            return result
        event.set_extra('_feiyu_preference',cfg.get('preferences',{}).get(sender,'标准') if not group else '标准')
        event.set_extra('_feiyu_security_epoch',epoch)
        event.set_extra('_feiyu_access_checked',True)
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
