import json

from fat_fish.memory.experience import contains_secret, protect_memory_engine


class MemoryCommands:
    async def handle(self,event,text,umo,sender):
        cmd,_,args=text.partition(" ");args=args.strip()
        group=bool(event and not event.is_private_chat())
        label='本群' if group else '本私聊'
        persona_id=event.get_extra('_ffa_memory_persona','feiyu-v1') if event else 'feiyu-v1'
        if event and event.get_extra('_ffa_expected_memory_scope') and event.get_extra('_ffa_expected_memory_scope')!=umo:
            return '待确认操作所属会话已变化，请重新申请'
        if cmd=='/记忆确认' and not args and event and event.get_extra('_ffa_access_checked',False):
            from astrbot.core.fat_fish_gateway import gateway
            from fat_fish.gateway.gateway import event_scope
            item=gateway.pending.pending.get((sender,event_scope(not group,str(event.get_group_id() or ''))))
            if not item or item[1]!='memory_reset':return '当前没有待确认的记忆清除'
            consumed=gateway.pending.consume_latest(sender,event_scope(not group,str(event.get_group_id() or '')))
            if not consumed:return '确认已过期，没有清除记忆'
            operation,values=consumed
            expected,args,_=values
            if expected!=umo:return '记忆清除所属会话已变化，请重新申请'

        if cmd=="/记忆帮助":return "本人的记忆：/memory remember 内容；/memory search 关键词；/memory update 编号 新内容；/memory delete 编号；/memory reset（需再次确认）；/memory cancel"
        if cmd=="/记忆取消":
            self.confirm.items.pop((umo,sender),None)
            if event and event.get_extra('_ffa_access_checked',False):
                from astrbot.core.fat_fish_gateway import gateway
                from fat_fish.gateway.gateway import event_scope
                key=(sender,event_scope(not group,str(event.get_group_id() or '')))
                item=gateway.pending.pending.get(key)
                if item and item[1]=='memory_reset':gateway.pending.pending.pop(key,None)
            return "已取消，没有清除记忆"
        md=self.context.get_registered_star("astrbot_plugin_livingmemory")
        p=md.star_cls if md and md.activated else None
        if not p:return "长期记忆尚未启用，没有进行操作"
        ready,_=await p._ensure_plugin_ready()
        if not ready:return "长期记忆还没准备好，没有进行操作"
        runtime=getattr(p,"initializer",p)
        eng=runtime.memory_engine
        protect_memory_engine(eng)
        if cmd=="/记忆撤销":
            from astrbot.core.fat_fish_gateway import gateway
            from fat_fish.gateway.gateway import event_scope
            entry=event.get_extra('_ffa_restore_memory',None)
            if not entry or gateway.undo_reference(event) is not entry:
                return '没有可撤销的近期记忆操作。'
            operation,expected,previous=entry[-1]
            ident=int(expected['id'])
            if ident not in {int(row['id']) for row in await self.rows(eng,umo)}:
                return '记忆已清除或移出当前会话，不能撤销。'
            if await eng.get_memory(ident)!=expected:
                return '记忆已变化，未覆盖现有内容。'
            if operation=='remember':
                ok=await eng.archive_memories([ident],audit={'archived_by':'explicit_user','archive_session_id':umo})
            elif operation=='delete':
                ok=await eng.restore_memory(ident)
            else:
                metadata=previous.get('metadata',{})
                if isinstance(metadata,str):metadata=json.loads(metadata)
                ok=await eng.update_memory(ident,{'content':previous['text'],'importance':metadata.get('importance',0.5)})
            if not ok:return '记忆撤销未完成。'
            gateway.undo_records.pop((sender,event_scope(not group,str(event.get_group_id() or ''))),None)
            return '已撤销最近的记忆操作。'
        if cmd=="/记忆重置":
            if args:return '重置只作用于'+label+'，不接受其他目标参数'
            code=self.confirm.issue(umo,sender)
            if event and event.get_extra('_ffa_access_checked',False):
                from astrbot.core.fat_fish_gateway import gateway
                from fat_fish.gateway.gateway import event_scope
                gateway.pending.issue(sender,event_scope(not group,str(event.get_group_id() or '')),'memory_reset',(umo,code,str(event.get_group_id() or '')))
                return '这会清除'+label+'可用的长期记忆和当前上下文，其他会话不动；旧记录、备份和日志仍保留。90秒内发送 /confirm 或「确定执行」才会执行，或用 '+('/groupmemory cancel' if group else '/memory cancel')
            return "这会清除你在本私聊的长期记忆和当前聊天上下文，其他人的不动；备份和既有日志不会一并抹除。90秒内发送 /memory confirm "+code+" 才会执行，或用 /memory cancel"
        if cmd=="/记忆确认":
            if not self.confirm.consume(umo,sender,args):return "确认无效或已过期，没有清除记忆"
            # Invalidate ongoing summarization windows before deleting the user's indexes.
            await runtime.conversation_manager.clear_session(umo)
            rows=await self.rows(eng,umo)
            if len(rows)>1000:return "资料数量超过安全上限，未执行删除，请本机管理员处理"
            ids=[int(x["id"]) for x in rows]
            if ids:await eng.batch_delete_memories(ids)
            if await self.rows(eng,umo):raise RuntimeError("reset incomplete")
            eng._invalidate_search_cache()
            await self.context.conversation_manager.new_conversation(umo)
            if group:
                import time
                from astrbot.core.fat_fish_gateway import gateway
                async with gateway.lock:
                    cfg,_=gateway.load();group_id=str(event.get_group_id())
                    cfg.setdefault('group_history_from',{})[group_id]=time.time()
                    cfg.setdefault('group_epochs',{})[group_id]=gateway.epoch(cfg,group_id)+1
                    gateway.save(cfg)
                    event.set_extra('_ffa_security_epoch',gateway.epoch(cfg,group_id))
                return '本群可用的长期记忆与当前上下文已清除；私人会话和其他群不动，旧记录、备份和日志仍保留'
            return "本私聊的长期记忆已清除，当前聊天也已换成空白上下文；其他会话未修改，备份和旧日志仍按保留策略保存"
        if cmd in {"/记住","/记忆更新"}:
            if not args or len(args)>300:return "请分条提供简短明确的内容，每条最多300字"
            if contains_secret(args):return "不要把密码、Token 或其他凭据写进记忆"
        if cmd=="/记住":
            ident=await eng.add_memory(content=args,session_id=umo,persona_id=persona_id,importance=1.0,metadata={"source":"explicit_user","owner_id":sender})
            if not ident:raise RuntimeError("not written")
            if event:
                saved=await eng.get_memory(int(ident))
                if saved:event.set_extra('_ffa_memory_undo',('remember',saved,None))
            return "已写入"+label+"的长期记忆，编号 "+str(ident)
        if cmd=="/记忆更新":
            ident,_,value=args.partition(" ")
            if not ident.isdigit() or not value:return "格式是 /memory update 编号 新内容，只能修改本人的记录"
            own=await self.rows(eng,umo)
            if int(ident) not in {int(x["id"]) for x in own}:return "找不到"+label+"的这个编号，不能修改其他会话的记忆"
            previous=await eng.get_memory(int(ident))
            if not await eng.update_memory(int(ident),{"content":value,"importance":1.0}):raise RuntimeError("update failed")
            if event and previous:
                cursor=await eng.db_connection.execute(
                    "SELECT id FROM documents WHERE json_extract(metadata,'$.session_id')=? AND json_extract(metadata,'$.previous_id')=? AND text=? LIMIT 2",
                    (umo,int(ident),value))
                candidates=await cursor.fetchall()
                if len(candidates)==1:
                    saved=await eng.get_memory(int(candidates[0]['id']))
                    if saved:event.set_extra('_ffa_memory_undo',('update',saved,previous))
            return '已更新'+label+'的这条记忆，查询时以新内容为准'
        if cmd=="/记忆删除":
            if not args.isdigit():return "格式是 /memory delete 编号，只作用当前会话"
            own=await self.rows(eng,umo)
            if int(args) not in {int(x["id"]) for x in own}:return "找不到"+label+"的这个编号"
            if not await eng.archive_memories([int(args)],audit={"archived_by":"explicit_user","archive_session_id":umo}):raise RuntimeError("archive failed")
            eng._invalidate_search_cache()
            if event:
                saved=await eng.get_memory(int(args))
                if saved:event.set_extra('_ffa_memory_undo',('delete',saved,None))
            return "已遗忘"+label+"的这条记忆；历史记录和备份仍保留"
        if cmd=="/记忆查询":
            if not args:return "请在 /memory search 后加关键词"
            if len(args)>300:return "查询请使用简短关键词，最多300字"
            found=await eng.search_memories(query=args,k=3,session_id=umo,persona_id=persona_id)
            own={int(x["id"]) for x in await self.rows(eng,umo)}
            found=[x for x in found if int(x.doc_id) in own]
            if not found:return label+"的长期记忆里没有找到可靠记录"
            return "\n".join(str(x.doc_id)+"："+x.content[:350] for x in found)
        return "没有执行；用 /memory help 查看本人记忆操作，不接受其他用户的目标参数"
    async def rows(self,eng,umo):
        if eng.db_connection is None:raise RuntimeError("database unavailable")
        cur=await eng.db_connection.execute("SELECT id FROM documents WHERE json_extract(metadata,'$.session_id')=? LIMIT 1001",(umo,))
        return await cur.fetchall()
