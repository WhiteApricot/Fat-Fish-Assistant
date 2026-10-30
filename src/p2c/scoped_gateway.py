"""Group-scoped administration and native durable group conversations."""
import asyncio
import json
import time
from .gateway import Gateway, ALIASES, event_scope
from .policy import ScopedPolicy

class ScopedGateway(Gateway):
    full_group_context=True
    def __init__(self,path):
        super().__init__(path);self.context=None;self.native=None
    def epoch(self,cfg,group):return cfg.get('group_epochs',{}).get(str(group),cfg.get('epoch',0))
    def barrier(self,cfg,group):return max(cfg.get('barrier_time',0),cfg.get('group_barriers',{}).get(str(group),0))
    def load(self):
        cfg=json.loads(self.path.read_text(encoding='utf-8-sig'))
        from .instance_identity import verify_owner
        verify_owner(self.path, cfg.get('owner'))
        if cfg.get('admins',[]):raise ValueError('Scoped authority configuration required')
        approved=set(cfg['approved_groups'])
        if not set(cfg['groups']).issubset(approved):raise ValueError('Unapproved group')
        for name in ('group_admins','group_blocked'):
            if not set(cfg.get(name,{})).issubset(approved):raise ValueError('Unapproved authority scope')
            if any(not str(x).isdigit() for users in cfg.get(name,{}).values() for x in users):raise ValueError('Invalid user')
        self.participation.salt=cfg.get('identity_salt','')
        policy=ScopedPolicy(cfg['owner'],cfg['groups'],{g:set(v) for g,v in cfg.get('group_admins',{}).items()},set(cfg['blocked']),{g:set(v) for g,v in cfg.get('group_blocked',{}).items()})
        return cfg,policy
    def request(self,sender,private,group,operation,args,description):
        self.pending.issue(sender,event_scope(private,group),operation,args)
        return description+'。90秒内发送 /confirm 或 /确认，即确认刚才这项操作；重新申请会替换旧申请'
    def command(self,cfg,policy,sender,level,private,group,cmd,args,event=None):
        if cmd=='help':
            if args:
                target=ALIASES.get(args[0],args[0])
                if target=='help':return '使用 /help 查看当前场景可用的功能'
                return self.command(cfg,policy,sender,level,private,group,target,['help'],event)
            lines=['/help 或 /帮助：查看当前场景可用功能','/role 或 /权限：查看本人身份与管理范围']
            if private and level>=1:
                lines+=['/personal 或 /个人设置：调整本人私聊回复详略','/privatepersona 或 /私聊人格：调整本人私聊表达风格','/memory 或 /记忆管理：管理本人私聊记忆','/new 或 /新对话：开始新的私聊对话','/confirm 或 /确认：确认当前私聊待处理操作']
            if not private and level>=2:
                lines+=['/groupmode 或 /群聊模式：切换本群回复模式','/groupsettings 或 /群聊设置：调整本群聊天行为','/grouppersona 或 /群人格：查看和调整本群独立人格','/groupmemory 或 /群记忆：管理本群共享记忆','/blacklist 或 /黑名单：管理本群黑名单','/confirm 或 /确认：确认本群待处理操作']
            if level==3:
                lines+=['/whitelist 或 /白名单：管理已批准群的开放状态']
                if private:lines+=['/blacklist 或 /黑名单：管理全局黑名单']
            return '\n'.join(lines)
        if cmd=='role':
            if not args:
                if level==3:return '你是肥鱼助手的主人，可以管理全局配置和各群授权'
                if not private:return '你是本群的肥鱼助手管理员，管理权限只在这个群有效' if level==2 else '你可以在本群聊天；本群的Bot设置由主人或本群Bot管理员管理'
                scopes=[g for g,users in cfg.get('group_admins',{}).items() if sender in users]
                return '你是肥鱼助手的好友，可以私聊并调整本人设置'+('；你还管理这些群：'+'、'.join(scopes)+'。群管理请进入对应群使用指令' if scopes else '')
            if level!=3:return '授权由主人管理；群管理员不能为任何群授予管理权限'
            if args in (['help'],['帮助']):
                suffix=' 群号' if private else ''
                return '/权限：查看本人身份\n/权限 list'+suffix+'：查看对应群管理员\n/role set QQ 2'+suffix+'：授予对应群管理员\n/role set QQ 1'+suffix+'：撤销对应群管理员\n/权限 set QQ 管理员|普通好友'+suffix+'：中文写法\n/role add|remove QQ'+suffix+'：兼容写法\n授权不转让主人身份；群权限不会扩展到其他群或私聊；好友关系以QQ为准'
            if args[0]=='list':
                target=args[1] if len(args)==2 else group
                if not target:return '\n'.join(g+'：'+('、'.join(v) or '暂无') for g,v in cfg.get('group_admins',{}).items()) or '暂无群管理员'
                return '本群Bot管理员：'+('、'.join(cfg.get('group_admins',{}).get(target,[])) or '暂无')
            roles={'管理员':'add','admin':'add','l2':'add','2':'add','普通好友':'remove','好友':'remove','friend':'remove','default':'remove','l1':'remove','1':'remove'}
            if args[0]=='set' and len(args) in (3,4):
                action=roles.get(args[2].lower());target=args[1];target_group=args[3] if len(args)==4 else group
            elif args[0] in {'add','remove'} and len(args) in (2,3):
                action,target=args[:2];target_group=args[2] if len(args)==3 else group
            else:return '用 /权限 help 查看当前场景的授权格式'
            if not action or not target.isdigit() or target_group not in cfg['approved_groups']:return '请明确指定已批准的群号和有效QQ；主人身份不能转让'
            if not policy.authorize(sender,'admin',private=private,group=target_group,target=target):return '不能修改主人身份'
            return self.request(sender,private,group,'admin',(action,target,target_group),('授予' if action=='add' else '撤销')+target+' 在群 '+target_group+' 的Bot管理权限')
        if cmd=='confirm':
            item=self.pending.consume(sender,event_scope(private,group),args[0]) if len(args)==1 else self.pending.consume_latest(sender,event_scope(private,group)) if not args else None
            if not item:return '当前没有有效的待确认操作，可能已过期、已执行，或属于其他账号/群'
            operation,values=item
            if operation=='memory_reset':
                umo,code,target_group=values
                if not event or target_group!=group or not policy.authorize(sender,'memory' if private else 'group',private=private,group=group,friend=level>=1):return '当前身份或会话不允许确认这项记忆清除'
                event.message_str='/记忆确认 '+code;event.set_extra('_feiyu_expected_memory_scope',umo)
                if not private:event.set_extra('_feiyu_group_memory_command',True)
                return None
            return self.apply(cfg,policy,sender,level,operation,values)
        if cmd in {'personal','memory','new'}:
            if not private:return '这个功能只在私聊可用；本群共享记忆由 /群记忆 管理'
            return super().command(cfg,policy,sender,level,private,group,cmd,args,event)
        if cmd=='privatepersona':
            if not policy.authorize(sender,'personal',private=private,group=group,friend=level>=1):return '私聊人格只接受好友在本人私聊中调整，不能指定其他用户'
            current=cfg.get('private_persona_notes',{}).get(sender,'')
            if not args:return '你与肥鱼助手私聊的表达补充：'+(current or '默认')+'\n用 /私聊人格 help 查看调整方法'
            if args in (['help'],['帮助']):return '/私聊人格：查看本人私聊表达补充\n/私聊人格 set 表达要求：只调整本人私聊，最多300字\n/私聊人格 reset：恢复默认表达\n/privatepersona：英文入口；需 /confirm 确认，不修改群或其他人，不清除记忆'
            note='' if args==['reset'] else ' '.join(args[1:]) if args[0]=='set' else None
            if note is None or len(note)>300 or (not note and args!=['reset']):return '格式：/私聊人格 set 表达要求（1至300字），或 /私聊人格 reset'
            return self.request(sender,private,group,'privatepersona',(sender,note),'更新你本人私聊的表达补充为：'+(note or '默认'))
        if cmd=='groupmemory':
            if private or level<2:return '只有主人或本群Bot管理员可以在本群管理共享记忆'
            if not args or args in (['help'],['帮助']):return '/群记忆 search 关键词：查询本群记忆\n/群记忆 remember 内容：写入本群记忆\n/群记忆 update 编号 内容：更新本群记录\n/群记忆 reset：申请清除本群记忆，需再次确认\n/groupmemory：对应英文入口；不能指定其他群或私人记录'
            operations={'search':'记忆查询','查询':'记忆查询','remember':'记住','写入':'记住','update':'记忆更新','更新':'记忆更新','reset':'记忆重置','重置':'记忆重置','cancel':'记忆取消','取消':'记忆取消'}
            if not event or args[0] not in operations:return '用 /群记忆 help 查看可用操作'
            event.message_str='/'+operations[args[0]]+(' '+' '.join(args[1:]) if len(args)>1 else '')
            event.set_extra('_feiyu_group_memory_command',True)
            return None
        if cmd in {'groupmode','groupsettings','grouppersona'}:
            if private or not policy.authorize(sender,'group',private=False,group=group):return '只有主人或本群Bot管理员可以在本群调整这些设置'
            if cmd=='grouppersona':
                current=cfg.get('group_persona_notes',{}).get(group,'')
                if not args:return '本群独立使用肥鱼人格；表达补充：'+(current or '默认')+'\n用 /群人格 help 查看调整方法'
                if args in (['help'],['帮助']):return '/群人格：查看本群独立人格\n/群人格 set 表达要求：只调整本群，最多300字\n/群人格 reset：恢复本群默认表达\n/grouppersona：英文入口；不调用任何人的私聊调试人格'
                note='' if args==['reset'] else ' '.join(args[1:]) if args[0]=='set' else None
                if note is None or len(note)>300:return '格式：/群人格 set 表达要求（最多300字），或 /群人格 reset'
                return self.request(sender,private,group,'persona',(group,note),'更新本群独立人格的表达补充为：'+(note or '默认'))
            if cmd=='groupmode' and args and args not in (['help'],['帮助']):
                modes={'mention':'mention','@':'mention','@触发':'mention','smart':'smart','智能':'smart','智能插话':'smart'}
                if len(args)!=1 or args[0] not in modes:return '用 /群聊模式 help 查看模式'
                return self.request(sender,private,group,'groupmode',(group,modes[args[0]]),'将本群切换为'+('智能插话' if modes[args[0]]=='smart' else '仅 @ 回复'))
            return super().command(cfg,policy,sender,level,private,group,cmd,args,event)
        if cmd in {'whitelist','blacklist'}:
            operation=cmd
            if cmd=='whitelist' and level!=3:return '群开放范围只由主人管理'
            if cmd=='blacklist' and not policy.authorize(sender,'blacklist',private=private,group=group,target='0'):return '全局黑名单由主人在私聊管理；群黑名单由本群Bot管理员管理'
            values=cfg['groups'] if cmd=='whitelist' else cfg['blocked'] if private else cfg.get('group_blocked',{}).get(group,[])
            title='授权群' if cmd=='whitelist' else '全局黑名单' if private else '本群黑名单'
            if not args:return title+'数量：'+str(len(values))+'\n用 /'+cmd+' help 查看操作'
            if args==['list']:return title+'：'+('、'.join(values) or '暂无')
            if args in (['help'],['帮助']):return '/'+cmd+' list：查看'+title+'\n/'+cmd+' add QQ或群号：申请添加\n/'+cmd+' remove QQ或群号：申请移除\n/confirm：确认当前账号在当前会话申请的操作'
            if len(args)!=2 or args[0] not in {'add','remove'} or not args[1].isdigit():return '格式：/'+cmd+' add|remove QQ或群号'
            action,target=args
            if cmd=='blacklist' and not policy.authorize(sender,'blacklist',private=private,group=group,target=target):return '你不能拉黑主人或本群其他Bot管理员'
            return self.request(sender,private,group,operation,(action,target,'' if private else group),'将 '+target+(' 添加到' if action=='add' else ' 移出')+title)
        return '这个功能不在当前场景的可用菜单中，请用 /help 查看'

    def apply(self,cfg,policy,sender,level,operation,args):
        if operation=='privatepersona':
            actor,note=args
            if actor!=sender or not policy.authorize(sender,'personal',private=True,friend=level>=1):return '只能调整本人的私聊人格'
            cfg.setdefault('private_persona_notes',{})[sender]=note
        elif operation in {'groupmode','persona'}:
            group,value=args
            if not policy.authorize(sender,'group',private=False,group=group):return '你目前无权修改这个群'
            if operation=='groupmode':cfg['groups'][group]=value
            else:cfg.setdefault('group_persona_notes',{})[group]=value
        else:
            action,target,group=args
            if operation=='admin':
                if group not in cfg['approved_groups'] or not policy.authorize(sender,'admin',private=not group,group=group,target=target):return '只有主人可以授权对应群的管理员'
                users=set(cfg.setdefault('group_admins',{}).get(group,[]));users.add(target) if action=='add' else users.discard(target)
                cfg['group_admins'][group]=sorted(users)
            elif operation=='whitelist':
                if sender!=policy.owner:return '只有主人可以修改群开放范围'
                if action=='add':
                    # A fresh owner-only, actor/scope-bound confirmation is
                    # explicit authorization of this exact additional group.
                    if target not in cfg['approved_groups']:cfg['approved_groups'].append(target)
                    cfg['groups'].setdefault(target,'mention')
                else:cfg['groups'].pop(target,None)
            elif operation=='blacklist':
                if not policy.authorize(sender,'blacklist',private=not group,group=group,target=target):return '当前权限不足，未执行'
                values=set(cfg['blocked'] if not group else cfg.setdefault('group_blocked',{}).get(group,[]))
                values.add(target) if action=='add' else values.discard(target)
                if group:cfg['group_blocked'][group]=sorted(values)
                else:cfg['blocked']=sorted(values)
                affected=[group] if group else list(cfg['approved_groups'])
                for item in affected:
                    cfg.setdefault('group_epochs',{})[item]=self.epoch(cfg,item)+1
                    cfg.setdefault('group_barriers',{})[item]=time.time()
                if not group:cfg['barrier_time']=time.time()
            else:return '不支持的操作'
        self.save(cfg)
        if operation=='privatepersona':return '已保存你本人私聊的表达补充' if args[1] else '已恢复你本人私聊的默认表达；原有事实记忆和聊天内容保留'
        if operation=='blacklist':
            current=asyncio.current_task()
            for task,(actor,active_group) in tuple(self.active.items()):
                if task is not current and ((group and active_group==group) or (not group and (actor==target or active_group))):task.cancel()
        return '已保存这项设置，只在指定的管理范围内生效'

    async def process(self,event):
        result=await super().process(event)
        notice=event.get_extra('_feiyu_notice_text','')
        if notice and self.context:
            if self.native is None:
                from .native_groups import NativeGroups
                self.native=NativeGroups(self)
            await self.native.append(event,[{'type':'plain','text':notice}],name='QQ系统通知',kind='system')
        return result
    async def prepare_group(self,event):
        if self.context is None:raise RuntimeError('Native group context unavailable')
        if self.native is None:
            from .native_groups import NativeGroups
            self.native=NativeGroups(self)
        cfg,policy=self.load();group=str(event.get_group_id());epoch=self.epoch(cfg,group)
        event.session_id='p2c'+str(epoch)+'_'+group
        event.set_extra('_feiyu_access_checked',True);event.set_extra('_feiyu_security_epoch',epoch)
        await self.native.persona(event)
        command=event.message_str.lstrip().startswith('/')
        if not command:
            await self.native.bootstrap(event)
            await self.native.append(event,self.native.parts(event))
            current,p=self.load()
            if str(event.get_sender_id()) in p.blocked_for(group) or self.epoch(current,group)!=epoch:raise asyncio.CancelledError()
            await self.native.capture_memory(event)
        event.set_extra('_feiyu_skip_group_capture',True)
        event.set_extra('_feiyu_group_persisted',True)
        event.set_extra('_feiyu_durable_group_context',await self.native.context(event))
    async def after_group_send(self,event,message):
        text=message.get_plain_text() if hasattr(message,'get_plain_text') else ''
        if text:
            await self.native.append(event,[{'type':'plain','text':text}],sender=str(event.get_self_id()),name='肥鱼助手',kind='bot',
                message_id=str(event.message_obj.message_id)+':bot:'+str(time.time_ns()),authors=event.get_extra('_feiyu_context_authors',[]))
    async def run(self,event,downstream):
        task=asyncio.current_task()
        if event.get_platform_name()=='aiocqhttp':self.active[task]=(str(event.get_sender_id()),str(event.get_group_id() or ''))
        try:return await super().run(event,downstream)
        finally:self.active.pop(task,None)
