"""Group-scoped administration and native durable group conversations."""
import asyncio
from copy import deepcopy
import json
import time
from fat_fish.gateway.gateway import Gateway, ALIASES, event_scope
from fat_fish.permissions.policy import ScopedPolicy
from fat_fish.persona.persona_settings import edit_persona, persona_catalog, PERSONA_HELP

class ScopedGateway(Gateway):
    full_group_context=True
    def __init__(self,path):
        super().__init__(path);self.context=None;self.native=None;self.recent_grants={};self.persona_snapshots={};self.undo_records={};self.persona_entry_undo={}
    def epoch(self,cfg,group):return cfg.get('group_epochs',{}).get(str(group),cfg.get('epoch',0))
    def barrier(self,cfg,group):return max(cfg.get('barrier_time',0),cfg.get('group_barriers',{}).get(str(group),0))
    def load(self):
        cfg=json.loads(self.path.read_text(encoding='utf-8-sig'))
        from fat_fish.core.instance_identity import verify_owner
        verify_owner(self.path, cfg.get('owner'))
        if not isinstance(cfg.get('global_admins',[]),list) or any(not isinstance(x,str) or not x.isdecimal() for x in cfg.get('global_admins',[])):raise ValueError('Invalid global administrators')
        if cfg.get('admins',[]):raise ValueError('Scoped authority configuration required')
        approved=set(cfg['approved_groups'])
        if not set(cfg['groups']).issubset(approved):raise ValueError('Unapproved group')
        for name in ('group_admins','group_blocked'):
            if not set(cfg.get(name,{})).issubset(approved):raise ValueError('Unapproved authority scope')
            if any(not str(x).isdigit() for users in cfg.get(name,{}).values() for x in users):raise ValueError('Invalid user')
        self.participation.salt=cfg.get('identity_salt','')
        policy=ScopedPolicy(cfg['owner'],cfg['groups'],{g:set(v) for g,v in cfg.get('group_admins',{}).items()},set(cfg['blocked']),{g:set(v) for g,v in cfg.get('group_blocked',{}).items()},global_admins=set(cfg.get('global_admins',[])))
        return cfg,policy
    def request(self,cfg,policy,sender,private,group,operation,args,description):
        if operation not in {'global_admin','memory_reset'} and not (operation=='admin' and args[0]=='add'):
            return self.apply(cfg,policy,sender,policy.level(sender,True,group),operation,args)
        scope=event_scope(private,group)
        if operation in {'admin','global_admin'}:
            self.recent_grants.pop((sender,scope),None)
        self.pending.issue(sender,scope,operation,args)
        return description+'。90秒内发送 /confirm 或「确定执行」，即确认刚才这项操作；重新申请会替换旧申请'
    @staticmethod
    def retire_style(cfg,sender,note):
        from fat_fish.persona.private_persona import sentence_affix
        affix=sentence_affix(note or '')
        if affix:cfg.setdefault('private_persona_retired_affixes',{})[sender]=list(affix)

    def remember_undo(self,event,level,command,args,before,after,result):
        if not result.startswith(('已保存','已恢复','已调整','已撤销','已创建提醒','已修改提醒','已删除提醒','已暂停提醒')):
            return
        key=(str(event.get_sender_id()),event_scope(event.is_private_chat(),str(event.get_group_id() or '')))
        task=event.get_extra('_ffa_memory_undo',None) or event.get_extra('_ffa_reminder_undo',None)
        fields={'private_persona_notes','group_persona_notes','preferences','cooldowns','groups',
                'approved_groups','group_admins','global_admins','group_blocked','blocked',
                'persona_entries','global_persona_notes','naturalcommand'}
        changed={field for field in fields if before.get(field)!=after.get(field)}
        if not changed and not task:
            return
        self.undo_records.pop(key,None)
        self.undo_records[key]=(time.monotonic()+1800,level,command,tuple(args),before,deepcopy(after),changed,task)
        if len(self.undo_records)>512:
            self.undo_records.pop(next(iter(self.undo_records)))

    def undo_reference(self,event):
        key=(str(event.get_sender_id()),event_scope(event.is_private_chat(),str(event.get_group_id() or '')))
        entry=self.undo_records.get(key)
        return entry if entry and time.monotonic()<entry[0] else None

    def restore_recent(self,cfg,policy,sender,level,private,group,event,expected,*,confirmed=False):
        entry=self.undo_reference(event)
        if not entry or entry is not expected:
            return '近期操作已经变化或过期，请重新查询。'
        _, required, command, args, before, after, fields, task=entry
        if level<required or cfg!=after:
            return '权限或配置已变化，未覆盖现有状态。'
        if command=='blacklist' and len(args)==2 and not policy.authorize(
                sender,'blacklist',private=private,group=group,target=args[1]):
            return '当前权限不足，未撤销。'
        if fields & {'global_admins','group_admins'} and not confirmed:
            self.pending.issue(sender,event_scope(private,group),'restore_recent',(entry,))
            return '撤销这项操作需要再次核对。90秒内发送 /confirm 或「确定执行」。'
        if task and command=='memory':
            if not policy.authorize(sender,'memory' if private else 'group',private=private,group=group,friend=level>=1):
                return '当前无权撤销记忆操作。'
            event.set_extra('_ffa_restore_memory',entry)
            event.message_str='/记忆撤销'
            if group:event.set_extra('_ffa_group_memory_command',True)
            return None
        if task:
            result=self.reminders.restore_recent(self,event,cfg,policy,task)
            if not result.startswith('已撤销'):return result
        else:
            counters={key:value['next_id'] for key,value in cfg.get('persona_entries',{}).items()}
            for field in fields:
                if field in before:cfg[field]=deepcopy(before[field])
                else:cfg.pop(field,None)
            if 'persona_entries' in fields:
                entries=cfg.setdefault('persona_entries',{})
                for key,number in counters.items():
                    if key in entries:entries[key]['next_id']=max(number,entries[key]['next_id'])
                    else:entries[key]={'next_id':number,'rows':[],'note':None}
            if fields & {'groups','approved_groups','group_admins','global_admins','group_blocked','blocked'}:
                affected=set(before.get('approved_groups',()))|set(after.get('approved_groups',()))
                for item in affected:
                    if any(field in fields for field in ('global_admins','blocked')) or any(
                            before.get(field,{}).get(item)!=after.get(field,{}).get(item)
                            for field in ('groups','group_admins','group_blocked')):
                        cfg.setdefault('group_epochs',{})[item]=self.epoch(cfg,item)+1
                if fields & {'global_admins','blocked'}:cfg['epoch']=cfg.get('epoch',0)+1
            if 'private_persona_notes' in fields:
                retired=after.get('private_persona_notes',{}).get(sender)
                self.retire_style(cfg,sender,retired)
                self.persona_snapshots.pop(sender,None)
            self.save(cfg)
            result='已撤销最近的 '+command+' 操作，恢复了此前设置。'
        event.set_extra('_ffa_undo_applied',True)
        self.undo_records.pop((sender,event_scope(private,group)),None)
        return result

    def persona_command(self,cfg,policy,sender,level,private,group,cmd,args,event=None):
        if cmd=='globalpersona':
            if level<2.5:return '只有主人和全局管理员可以管理全局人格。'
            scope='global'
            target=cfg
            field='global_persona_notes'
        else:
            if not policy.authorize(sender,'personal' if private else 'group',
                                    private=private,group=group,friend=level>=1):
                return '只有本人或本群有权的管理员可以管理当前会话人格。'
            scope='private:'+sender if private else 'group:'+group
            target=cfg.setdefault('private_persona_notes' if private else 'group_persona_notes',{})
            field=sender if private else group
        if args==['help']:
            return PERSONA_HELP.format(command=cmd)+'\n全局与会话要求叠加，冲突时会话优先；reset 只清除当前作用域。'
        entries=cfg.setdefault('persona_entries',{})
        note=target.get(field,'')
        prior=deepcopy(entries.get(scope))
        undo_key=(sender,event_scope(private,group),scope)
        if args==['undo']:
            saved=self.persona_entry_undo.get(undo_key)
            if not saved or time.monotonic()>=saved[0] or (prior,note)!=saved[2]:
                if cmd=='persona' and private and (not prior or prior['note'] is None):
                    return self.command(cfg,policy,sender,level,private,group,'privatepersona',['undo'],event)
                if cmd=='persona' and not private and event is not None:
                    entry=self.undo_reference(event)
                    if entry and entry[2]=='grouppersona':
                        return self.restore_recent(cfg,policy,sender,level,private,group,event,entry)
                return '没有匹配且仍可撤销的人格修改。'
            old_entry,old_note=saved[1]
            old_entry={**old_entry,'next_id':max(prior['next_id'],old_entry['next_id'])}
            entries[scope]=old_entry
            target[field]=old_note
            if scope.startswith('private:'):self.retire_style(cfg,sender,note)
            self.save(cfg)
            self.persona_entry_undo.pop(undo_key,None)
            if event is not None:event.set_extra('_ffa_undo_applied',True)
            return '已恢复上一次修改前的人格要求。'
        previous=persona_catalog(cfg,scope,note)
        new,result=edit_persona(cfg,scope,note,args)
        if new is None:return result
        entries[scope]=new
        target[field]=new['note']
        if scope.startswith('private:'):self.retire_style(cfg,sender,note)
        self.save(cfg)
        if (prior,note)!=(new,new['note']):
            self.persona_entry_undo.pop(undo_key,None)
            self.persona_entry_undo[undo_key]=(time.monotonic()+1800,(previous,note),(deepcopy(new),new['note']))
            if len(self.persona_entry_undo)>512:self.persona_entry_undo.pop(next(iter(self.persona_entry_undo)))
        return result

    def command(self,cfg,policy,sender,level,private,group,cmd,args,event=None):
        if sender in policy.blocked_for(group):return '当前权限不足，未执行'
        if cmd in {'persona','globalpersona'}:
            return self.persona_command(cfg,policy,sender,level,private,group,cmd,args,event)
        if cmd=='naturalcommand':
            if not policy.authorize(sender,'personal' if private else 'group',private=private,group=group,friend=level>=1):
                return '只有本人或本群有权的管理员可以切换自然语言指令。'
            key='private:'+sender if private else 'group:'+group
            settings=cfg.setdefault('naturalcommand',{})
            if not args or args==['help']:
                return '当前作用域自然语言指令：'+('开启' if settings.get(key,True) else '关闭')+'。使用 /naturalcommand on|off。'
            if args not in (['on'],['off']):return '用法：/naturalcommand on|off'
            settings[key]=args==['on']
            self.save(cfg)
            return '已保存当前作用域的自然语言指令设置：'+('开启' if settings[key] else '关闭')
        if cmd=='reminder':
            if not event:return '提醒操作需要当前会话'
            store=getattr(self,'reminders',None)
            if store is None:return '提醒插件未启用'
            try:return store.command(self,event,args,access=(cfg,policy))
            except ValueError as exc:return str(exc)
        if cmd=='role' and args and args[0] in {'global','全局管理员'}:
            args=args[1:]
            cmd='globaladmin'
        if cmd=='globaladmin':
            if args==['list'] and level in {3,2.5}:return '全局管理员：'+('、'.join(cfg.get('global_admins',[])) or '暂无')
            if level!=3:return '只有主人可以管理全局管理员'
            if len(args)!=2 or args[0] not in {'add','remove'} or not args[1].isdecimal():return '/role global list|add QQ|remove QQ'
            if not policy.authorize(sender,'global_admin',private=private,group=group,target=args[1]):return '不能修改主人身份'
            return self.request(cfg,policy,sender,private,group,'global_admin',tuple(args),'变更全局管理员 '+args[1])
        if cmd=='help':
            from fat_fish.commands.command_help import HELP
            if args:
                target=ALIASES.get(args[0],args[0].lower())
                return HELP.get(target,'没有这个子菜单。')
            names=['help','role','reminder','history','confirm','cancel','undo']
            if private and level>=1:names+=['memory','personal','privatepersona','new','persona','naturalcommand']
            if not private and level>=2:names+=['groupmemory','groupmode','groupsettings','grouppersona','persona','naturalcommand','blacklist']
            if level>=2.5:
                names+=['globaladmin','globalpersona','whitelist']
                if private:names.append('blacklist')
            return '\n'.join('/'+name for name in names)
        if cmd=='role':
            if args==['undo']:
                return self.undo_admin(cfg,policy,sender,level,private,group)
            if not args:
                if level==2.5:return '你是全局管理员，可以管理各群和常规配置；不能管理全局管理员或操作电脑'
                if level==3:return '你是肥鱼助手的主人，可以管理全局配置和各群授权'
                if not private:return '你是本群的肥鱼助手管理员，管理权限只在这个群有效' if level==2 else '你可以在本群聊天；本群的Bot设置由主人或本群Bot管理员管理'
                scopes=[g for g,users in cfg.get('group_admins',{}).items() if sender in users]
                return '你是肥鱼助手的好友，可以私聊并调整本人设置'+('；你还管理这些群：'+'、'.join(scopes)+'。群管理请进入对应群使用指令' if scopes else '')
            if args==['list'] and group and level>=2:return '本群Bot管理员：'+('、'.join(sorted((policy.administrators(group)|policy.global_admins|{policy.owner})-policy.blocked_for(group))))
            if level not in {3,2.5}:return '授权由主人管理；群管理员不能为任何群授予管理权限'
            if args in (['help'],['帮助']):
                suffix=' 群号' if private else ''
                global_help='\n/role global list：查看全局管理员'
                if level==3:global_help+='\n/role global add|remove QQ：变更全局管理员，需确认'
                return '/role：查看本人身份\n/role list'+suffix+'：查看对应群管理员\n/role set QQ 2'+suffix+'：授予对应群管理员\n/role set QQ 1'+suffix+'：撤销对应群管理员\n/role set QQ 管理员|普通好友'+suffix+'：中文写法\n/role add|remove QQ'+suffix+'：兼容写法\n授权不转让主人身份；群权限不会扩展到其他群或私聊；好友关系以QQ为准'+global_help
            if args[0]=='list':
                target=args[1] if len(args)==2 else group
                if not target:return '全局管理员：'+('、'.join(sorted(policy.global_admins)) or '暂无')+'\n'+'\n'.join(g+'：'+('、'.join(v) or '暂无') for g,v in cfg.get('group_admins',{}).items()) or '暂无群管理员'
                return '该群显式授权的Bot管理员：'+('、'.join(cfg.get('group_admins',{}).get(target,[])) or '暂无')+'。QQ群身份请在对应群内查看'
            if len(args)==3 and args[0]=='remove' and args[2]=='all':
                target=args[1]
                if not target.isdecimal() or not policy.authorize(sender,'admin',private=private,group=group or '0',target=target):
                    return '当前权限不足或目标QQ无效'
                affected=[g for g,users in cfg.get('group_admins',{}).items() if target in users]
                for g in affected:
                    cfg['group_admins'][g].remove(target)
                    cfg.setdefault('group_epochs',{})[g]=self.epoch(cfg,g)+1
                if affected:self.save(cfg)
                return '已撤销该QQ在 '+str(len(affected))+' 个群的Bot管理员身份'+('。该QQ仍是全局管理员' if target in policy.global_admins else '')
            roles={'管理员':'add','admin':'add','l2':'add','2':'add','普通好友':'remove','好友':'remove','friend':'remove','default':'remove','l1':'remove','1':'remove'}
            if args[0]=='set' and len(args) in (3,4):
                action=roles.get(args[2].lower());target=args[1];target_group=args[3] if len(args)==4 else group
            elif args[0] in {'add','remove'} and len(args) in (2,3):
                action,target=args[:2];target_group=args[2] if len(args)==3 else group
            else:return '用 /role help 查看当前场景的授权格式'
            if not action or not target.isdigit() or target_group not in cfg['approved_groups']:return '请明确指定已批准的群号和有效QQ；主人身份不能转让'
            if not policy.authorize(sender,'admin',private=private,group=target_group,target=target):return '不能修改主人身份'
            return self.request(cfg,policy,sender,private,group,'admin',(action,target,target_group),('授予' if action=='add' else '撤销')+target+' 在群 '+target_group+' 的Bot管理权限')
        if cmd=='confirm':
            item=self.pending.consume(sender,event_scope(private,group),args[0]) if len(args)==1 else self.pending.consume_latest(sender,event_scope(private,group)) if not args else None
            if not item:return '当前没有有效的待确认操作，可能已过期、已执行，或属于其他账号/群'
            operation,values=item
            if operation=='restore_recent':
                return self.restore_recent(cfg,policy,sender,level,private,group,event,values[0],confirmed=True)
            if operation=='memory_reset':
                umo,code,target_group=values
                if not event or target_group!=group or not policy.authorize(sender,'memory' if private else 'group',private=private,group=group,friend=level>=1):return '当前身份或会话不允许确认这项记忆清除'
                event.message_str='/记忆确认 '+code;event.set_extra('_ffa_expected_memory_scope',umo)
                if not private:event.set_extra('_ffa_group_memory_command',True)
                return None
            key=(sender,event_scope(private,group))
            admin_change=operation in {'global_admin','admin'}
            target=values[1] if admin_change else ''
            target_group=values[2] if operation=='admin' else ''
            result=self.apply(cfg,policy,sender,level,operation,values)
            if admin_change and result.startswith('已保存'):
                self.recent_grants.pop(key,None)
                if values[0]=='add':
                    self.recent_grants[key]=(operation,target,target_group,self.epoch(cfg,target_group),time.monotonic()+900)
                    if len(self.recent_grants)>512:
                        self.recent_grants.pop(next(iter(self.recent_grants)))
            if admin_change and result.startswith('已保存'):
                title='全局管理员' if operation=='global_admin' else '群 '+target_group+' 的显式Bot管理员'
                if event is not None:
                    event.set_extra('_ffa_confirmed_command',('globaladmin' if operation=='global_admin' else 'role',values))
                result+='。经二次确认，已'+('授予' if values[0]=='add' else '撤销')+' '+target+' 的'+title+'权限'
            return result
        if cmd in {'personal','memory','new'}:
            if not private:return '这个功能只在私聊可用；本群共享记忆由 /groupmemory 管理'
            return super().command(cfg,policy,sender,level,private,group,cmd,args,event)
        if cmd=='privatepersona':
            if not policy.authorize(sender,'personal',private=private,group=group,friend=level>=1):return '私聊人格只接受好友在本人私聊中调整，不能指定其他用户'
            current=cfg.get('private_persona_notes',{}).get(sender,'')
            if args==['undo']:
                snapshot=self.persona_snapshots.pop(sender,None)
                if snapshot is None or time.monotonic()>=snapshot[0] or current!=snapshot[2]:
                    return '没有可撤销的近期私聊人格修改'
                previous=snapshot[1]
                notes=cfg.setdefault('private_persona_notes',{})
                if previous is None:
                    notes.pop(sender,None)
                else:
                    notes[sender]=previous
                if catalog:=cfg.get('persona_entries',{}).get('private:'+sender):catalog['note']=None
                self.retire_style(cfg,sender,current)
                try:
                    self.save(cfg)
                except Exception:
                    self.persona_snapshots[sender]=snapshot
                    raise
                return '已恢复上一次修改前的私聊人格设置'
            if not args:return '你与肥鱼助手私聊的表达补充：'+(current or '默认')+'\n用 /privatepersona help 查看调整方法'
            if args in (['help'],['帮助']):return '/privatepersona：查看本人私聊表达补充\n/privatepersona set 表达要求：只调整本人私聊，最多300字\n/privatepersona reset：恢复默认表达\n/privatepersona undo：恢复上一次私聊表达；直接执行'
            note='' if args==['reset'] else ' '.join(args[1:]) if args[0]=='set' else None
            if note is None or len(note)>300 or (not note and args!=['reset']):return '格式：/privatepersona set 表达要求（1至300字），或 /privatepersona reset'
            return self.request(cfg,policy,sender,private,group,'privatepersona',(sender,note),'更新你本人私聊的表达补充为：'+(note or '默认'))
        if cmd=='groupmemory':
            if private or level<2:return '只有主人或本群Bot管理员可以在本群管理共享记忆'
            if not args or args in (['help'],['帮助']):return '/groupmemory search 关键词：查询本群记忆\n/groupmemory remember 内容：写入本群记忆\n/groupmemory update 编号 内容：更新本群记录\n/groupmemory delete 编号：遗忘本群单条记录\n/groupmemory reset：申请清除本群记忆，需再次确认\n/groupmemory：对应英文入口；不能指定其他群或私人记录'
            operations={'search':'记忆查询','查询':'记忆查询','remember':'记住','写入':'记住','update':'记忆更新','更新':'记忆更新','delete':'记忆删除','删除':'记忆删除','reset':'记忆重置','重置':'记忆重置','cancel':'记忆取消','取消':'记忆取消'}
            if not event or args[0] not in operations:return '用 /groupmemory help 查看可用操作'
            event.message_str='/'+operations[args[0]]+(' '+' '.join(args[1:]) if len(args)>1 else '')
            event.set_extra('_ffa_group_memory_command',True)
            return None
        if cmd in {'groupmode','groupsettings','grouppersona'}:
            if private or not policy.authorize(sender,'group',private=False,group=group):return '只有主人或本群Bot管理员可以在本群调整这些设置'
            if cmd=='grouppersona':
                current=cfg.get('group_persona_notes',{}).get(group,'')
                if not args:return '本群独立使用肥鱼人格；表达补充：'+(current or '默认')+'\n用 /grouppersona help 查看调整方法'
                if args in (['help'],['帮助']):return '/grouppersona：查看本群独立人格\n/grouppersona set 表达要求：只调整本群，最多300字\n/grouppersona reset：恢复本群默认表达\n/grouppersona：英文入口；不调用任何人的私聊调试人格'
                note='' if args==['reset'] else ' '.join(args[1:]) if args[0]=='set' else None
                if note is None or len(note)>300:return '格式：/grouppersona set 表达要求（最多300字），或 /grouppersona reset'
                return self.request(cfg,policy,sender,private,group,'persona',(group,note),'更新本群独立人格的表达补充为：'+(note or '默认'))
            if cmd=='groupmode' and args and args not in (['help'],['帮助']):
                modes={'mention':'mention','@':'mention','@触发':'mention','smart':'smart','智能':'smart','智能插话':'smart'}
                if len(args)!=1 or args[0] not in modes:return '用 /groupmode help 查看模式'
                return self.request(cfg,policy,sender,private,group,'groupmode',(group,modes[args[0]]),'将本群切换为'+('智能插话' if modes[args[0]]=='smart' else '仅 @ 回复'))
            return super().command(cfg,policy,sender,level,private,group,cmd,args,event)
        if cmd in {'whitelist','blacklist'}:
            operation=cmd
            if cmd=='whitelist' and level not in {3,2.5}:return '群开放范围只由主人管理'
            if cmd=='blacklist' and not policy.authorize(sender,'blacklist',private=private,group=group,target='0'):return '全局黑名单由主人在私聊管理；群黑名单由本群Bot管理员管理'
            values=cfg['groups'] if cmd=='whitelist' else cfg['blocked'] if private else cfg.get('group_blocked',{}).get(group,[])
            title='授权群' if cmd=='whitelist' else '全局黑名单' if private else '本群黑名单'
            if not args:return title+'数量：'+str(len(values))+'\n用 /'+cmd+' help 查看操作'
            if args==['list']:return title+'：'+('、'.join(values) or '暂无')
            if args in (['help'],['帮助']):return '/'+cmd+' list：查看'+title+'\n/'+cmd+' add QQ或群号：添加\n/'+cmd+' remove QQ或群号：移除'
            if len(args)!=2 or args[0] not in {'add','remove'} or not args[1].isdigit():return '格式：/'+cmd+' add|remove QQ或群号'
            action,target=args
            if cmd=='blacklist' and not policy.authorize(sender,'blacklist',private=private,group=group,target=target):return '你不能拉黑主人或本群其他Bot管理员'
            return self.request(cfg,policy,sender,private,group,operation,(action,target,'' if private else group),'将 '+target+(' 添加到' if action=='add' else ' 移出')+title)
        return '这个功能不在当前场景的可用菜单中，请用 /help 查看'

    def admin_undo_reference(self,event):
        actor=str(event.get_sender_id())
        scope=event_scope(event.is_private_chat(),str(event.get_group_id() or ''))
        pending=self.pending.peek(actor,scope)
        if pending:
            return pending
        return self.recent_grants.get((actor,scope))

    def recent_commands(self,event):
        recent=super().recent_commands(event)
        actor=str(event.get_sender_id())
        group=str(event.get_group_id() or '')
        scope=event_scope(event.is_private_chat(),group)
        item=self.pending.peek(actor,scope)
        if item and item[1] in {'admin','global_admin'}:
            action,target,*groups=item[2]
            title='全局管理员' if not groups else '群 '+groups[0]+' 的显式Bot管理员'
            recent.append(('授予' if action=='add' else '撤销')+' '+target+' 的'+title+'权限，尚待确认')
        grant=self.recent_grants.get((actor,scope))
        if grant:
            operation,target,target_group,epoch,expires=grant
            cfg,policy=self.load()
            if time.monotonic()<expires and self.epoch(cfg,target_group)==epoch:
                title='全局管理员' if operation=='global_admin' else '群 '+target_group+' 的显式Bot管理员'
                recent.append('已授予 '+target+' 的'+title+'权限')
        return recent

    def undo_admin(self,cfg,policy,sender,level,private,group):
        scope=event_scope(private,group)
        pending=self.pending.peek(sender,scope)
        if pending:
            if pending[1] not in {'admin','global_admin'}:
                return '当前待确认的是其他操作，管理员申请没有变更'
            self.pending.consume_latest(sender,scope)
            return '已取消刚才的管理员申请，权限没有变更'
        key=(sender,scope)
        grant=self.recent_grants.get(key)
        if not grant:
            return '当前会话没有可撤销的近期管理员授权，请明确指定账号和范围'
        operation,target,target_group,epoch,expires=grant
        if time.monotonic()>=expires or self.epoch(cfg,target_group)!=epoch:
            self.recent_grants.pop(key,None)
            return '近期授权已过期或权限配置已有变化，请明确指定账号和范围'
        if not policy.authorize(sender,operation,private=private,group=target_group,target=target):
            return '当前权限不足，未执行'
        if operation=='global_admin':
            return self.request(cfg,policy,sender,private,group,operation,('remove',target),
                                '撤销刚才授予 '+target+' 的全局管理员权限')
        result=self.apply(cfg,policy,sender,level,operation,('remove',target,target_group))
        if result.startswith('已'):
            self.recent_grants.pop(key,None)
            return '已撤销 '+target+' 在群 '+target_group+' 的显式Bot管理员授权'
        return result

    def apply(self,cfg,policy,sender,level,operation,args):
        if operation=='privatepersona':
            actor,note=args
            if actor!=sender or not policy.authorize(sender,'personal',private=True,friend=level>=1):return '只能调整本人的私聊人格'
            notes=cfg.setdefault('private_persona_notes',{})
            previous=notes.get(sender)
            notes[sender]=note
            if catalog:=cfg.get('persona_entries',{}).get('private:'+sender):catalog['note']=None
        elif operation in {'groupmode','persona'}:
            group,value=args
            if not policy.authorize(sender,'group',private=False,group=group):return '你目前无权修改这个群'
            if operation=='groupmode':cfg['groups'][group]=value
            else:
                cfg.setdefault('group_persona_notes',{})[group]=value
                if catalog:=cfg.get('persona_entries',{}).get('group:'+group):catalog['note']=None
        else:
            action,target,group=(*args,'') if operation=='global_admin' else args
            if operation=='global_admin':
                if not policy.authorize(sender,'global_admin',private=True,target=target):return '只有主人可以管理全局管理员'
                if action=='add' and target in policy.blocked:return '不能授予黑名单用户全局管理员身份'
                users=set(cfg.get('global_admins',[]));users.add(target) if action=='add' else users.discard(target)
                cfg['global_admins']=sorted(users)
                cfg['epoch']=cfg.get('epoch',0)+1
                for item in cfg['approved_groups']:cfg.setdefault('group_epochs',{})[item]=self.epoch(cfg,item)+1
            elif operation=='admin':
                if group not in cfg['approved_groups'] or not policy.authorize(sender,'admin',private=not group,group=group,target=target):return '只有主人可以授权对应群的管理员'
                users=set(cfg.setdefault('group_admins',{}).get(group,[]));users.add(target) if action=='add' else users.discard(target)
                cfg['group_admins'][group]=sorted(users)
                cfg.setdefault('group_epochs',{})[group]=self.epoch(cfg,group)+1
            elif operation=='whitelist':
                if not policy.authorize(sender,'whitelist',private=True):return '当前权限不足'
                if action=='add':
                    # A fresh owner-only, actor/scope-bound confirmation is
                    # explicit authorization of this exact additional group.
                    if target not in cfg['approved_groups']:cfg['approved_groups'].append(target)
                    cfg['groups'].setdefault(target,'mention')
                else:cfg['groups'].pop(target,None)
                cfg.setdefault('group_epochs',{})[target]=self.epoch(cfg,target)+1
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
        if operation=='privatepersona' and previous!=note:self.retire_style(cfg,sender,previous)
        self.save(cfg)
        if operation=='privatepersona' and previous!=note:
            self.persona_snapshots.pop(sender,None)
            self.persona_snapshots[sender]=(time.monotonic()+1800,previous,note)
            if len(self.persona_snapshots)>512:
                self.persona_snapshots.pop(next(iter(self.persona_snapshots)))
        if operation=='privatepersona':return '已保存你本人私聊的表达补充' if args[1] else '已恢复你本人私聊的默认表达；原有事实记忆和聊天内容保留'
        if operation=='admin' and action=='remove' and target in policy.qq_group_admins.get(group,set()):
            return '已撤销显式授权。该成员的QQ群管理身份仍赋予本群Bot管理权限'
        if operation=='blacklist':
            current=asyncio.current_task()
            for task,(actor,active_group) in tuple(self.active.items()):
                if task is not current and ((group and active_group==group) or (not group and (actor==target or active_group))):task.cancel()
        return '已保存这项设置，只在指定的管理范围内生效'

    async def process(self,event):
        result=await super().process(event)
        notice=event.get_extra('_ffa_notice_text','')
        if notice and self.context:
            if self.native is None:
                from fat_fish.memory.native_groups import NativeGroups
                self.native=NativeGroups(self)
            await self.native.append(event,[{'type':'plain','text':notice}],name='QQ系统通知',kind='system')
        return result
    async def prepare_group(self,event):
        if self.context is None:raise RuntimeError('Native group context unavailable')
        if self.native is None:
            from fat_fish.memory.native_groups import NativeGroups
            self.native=NativeGroups(self)
        cfg,policy=self.load();group=str(event.get_group_id());epoch=self.epoch(cfg,group)
        event.session_id='p2c'+str(epoch)+'_'+group
        event.set_extra('_ffa_access_checked',True);event.set_extra('_ffa_security_epoch',epoch)
        await self.native.persona(event)
        command=event.message_str.lstrip().startswith('/')
        if not command:
            await self.native.bootstrap(event)
            await self.native.append(event,self.native.parts(event))
            current,p=self.load()
            if str(event.get_sender_id()) in p.blocked_for(group) or self.epoch(current,group)!=epoch:raise asyncio.CancelledError()
            if not event.get_extra('_ffa_material',False):await self.native.capture_memory(event)
        event.set_extra('_ffa_skip_group_capture',True)
        event.set_extra('_ffa_group_persisted',True)
        event.set_extra('_ffa_durable_group_context',await self.native.context(event))
    async def after_group_send(self,event,message):
        text=message.get_plain_text() if hasattr(message,'get_plain_text') else ''
        if text:
            await self.native.append(event,[{'type':'plain','text':text}],sender=str(event.get_self_id()),name='肥鱼助手',kind='bot',
                message_id=str(event.message_obj.message_id)+':bot:'+str(time.time_ns()),authors=event.get_extra('_ffa_context_authors',[]))
    async def run(self,event,downstream):
        task=asyncio.current_task()
        if event.get_platform_name()=='aiocqhttp':self.active[task]=(str(event.get_sender_id()),str(event.get_group_id() or ''))
        try:return await super().run(event,downstream)
        finally:self.active.pop(task,None)
