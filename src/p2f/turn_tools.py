"""Scoped P2F tools for the normal AstrBot conversation request."""
import asyncio
import json
import re

from p2c.gateway import event_scope, system_result
from p2f.intents import contains_external_material
from p2f.persona_reply import send_command_reply


TOOLS = {
    'get_recent_actions': ('查询本人本会话的近期操作，撤销或指代不明确时先查。', '', '', {}),
    'get_pending_actions': ('查询本人本会话待确认操作。', '', '', {}),
    'confirm_action': ('仅在用户明确确认当前待办时调用。不能在创建待办的同一轮确认。', 'confirm', '', {}),
    'cancel_action': ('仅在用户明确取消当前待办时调用。', '', '', {}),
    'memory_reset': ('申请清除当前会话记忆，需要另行确认，清除后不可撤销。', 'memory', 'reset', {}),
    'private_persona_update': ('持久修改本人私聊表达。', 'privatepersona', 'set', {'note': 300}),
    'private_persona_reset': ('将私聊表达恢复默认。', 'privatepersona', 'reset', {}),
    'private_persona_undo': ('恢复本人上一次私聊表达修改前的设置。', 'privatepersona', 'undo', {}),
    'global_admin_grant': ('申请授予当前发言明确给出的数字QQ全局管理员权限。', 'globaladmin', 'add', {'qq': 20}),
    'global_admin_revoke': ('仅在当前发言明确提供数字QQ时申请撤销。撤销刚才的授权用 undo_recent_admin_grant，即使历史给出了QQ。', 'globaladmin', 'remove', {'qq': 20}),
    'undo_recent_admin_grant': ('用于撤销刚才、上一次或最近的管理员授权或待确认申请。先查询近期操作，随后用本工具；不传QQ。', 'role', 'undo', {}),
    'reminder_create': ('创建本人提醒，提供包含明确时间和事项的请求。', 'reminder', 'add', {'request': 500}),
    'reminder_query': ('查询当前会话可访问的提醒和编号。', 'reminder', 'list', {}),
    'reminder_update_time': ('按实际提醒编号修改时间。', 'reminder', 'time', {'id': 18, 'time': 150}),
    'reminder_update_text': ('按实际提醒编号修改正文。', 'reminder', 'text', {'id': 18, 'text': 500}),
    'reminder_create_for_member': ('为当前群的明确成员创建提醒。', 'reminder', 'addto', {'qq': 20, 'request': 500}),
    'reminder_pause': ('暂停指定的循环提醒。', 'reminder', 'pause', {'id': 18}),
    'reminder_resume': ('恢复指定的循环提醒。', 'reminder', 'resume', {'id': 18}),
    'group_persona_update': ('修改当前群的人格表达。', 'grouppersona', 'set', {'note': 300}),
    'group_persona_reset': ('恢复当前群默认人格。', 'grouppersona', 'reset', {}),
    'group_mention_mode': ('让当前群只在被明确呼叫时回复。', 'groupmode', 'mention', {}),
    'group_smart_mode': ('将当前群设为智能回复。', 'groupmode', 'smart', {}),
    'group_cooldown': ('设置当前群的回复冷却秒数。', 'groupsettings', 'cooldown', {'seconds': 5}),
    'group_admin_grant': ('申请授予指定群的成员Bot管理权限。', 'role', 'add', {'qq': 20, 'group': 20}),
    'group_admin_revoke': ('撤销指定群的成员的显式Bot管理权限。', 'role', 'remove', {'qq': 20, 'group': 20}),
    'block_account': ('把指定QQ加入当前作用域黑名单。', 'blacklist', 'add', {'qq': 20}),
    'unblock_account': ('将指定QQ从当前作用域黑名单移除。', 'blacklist', 'remove', {'qq': 20}),
    'approved_group_add': ('授权一个明确的QQ群。', 'whitelist', 'add', {'group': 20}),
    'approved_group_remove': ('取消一个明确QQ群的授权。', 'whitelist', 'remove', {'group': 20}),
    'reminder_delete': ('按实际提醒编号删除提醒。', 'reminder', 'delete', {'id': 18}),
}


class TurnTools:
    def __init__(self, guard, gateway, event):
        self.guard = guard
        self.gateway = gateway
        self.event = event
        self.actor = str(event.get_sender_id())
        self.private = event.is_private_chat()
        self.group = str(event.get_group_id() or '')
        self.scope = event_scope(self.private, self.group)
        self.pending = gateway.pending.peek(self.actor, self.scope)
        self.recent_grant = gateway.admin_undo_reference(event)
        self.handlers = {}
        self.toolset = None
        self.calls = 0
        self.finished = False

    def allowed(self, name, level):
        if name.startswith('global_admin_'):
            return level == 3
        if name.startswith('private_persona_'):
            return self.private and level >= 1
        if name == 'memory_reset':
            return level >= (1 if self.private else 2)
        if name.startswith('group_admin_'):
            return level >= 2.5
        if name in {'block_account', 'unblock_account'}:
            return level >= (2.5 if self.private else 2)
        if name.startswith('approved_group_'):
            return level >= 2.5
        if name.startswith('group_') or name == 'reminder_create_for_member':
            return not self.private and level >= 2
        if name == 'undo_recent_admin_grant':
            return level >= 2.5
        return True

    def install(self, request):
        from astrbot.core.agent.tool import FunctionTool, ToolSet

        _, policy = self.gateway.load()
        self.bind_group_actor(policy)
        level = policy.level(self.actor, self.event.get_extra('_feiyu_level', 0) >= 1, self.group)
        toolset = ToolSet(list(request.func_tool.tools) if request.func_tool else [])
        for name, (description, _, _, fields) in TOOLS.items():
            if not self.allowed(name, level):
                continue
            properties = {}
            for field, size in fields.items():
                schema = {'type': 'string', 'minLength': 1, 'maxLength': size}
                if field in {'id', 'qq', 'group', 'seconds'}:
                    schema['pattern'] = '^[0-9]+$'
                properties[field] = schema
            self.handlers[name] = self.handler(name)
            toolset.add_tool(FunctionTool(name=name, description=description, parameters={
                'type': 'object', 'properties': properties, 'required': list(fields),
                'additionalProperties': False,
            }, handler=self.handlers[name]))
        self.toolset = toolset
        request.func_tool = toolset
        self.event.set_extra('_p2f_turn_tools', self)

    def owns(self, tool):
        return tool.name in self.handlers and tool.handler is self.handlers[tool.name]

    def handler(self, name):
        async def execute(event, **kwargs):
            if event is not self.event:
                return '当前请求与工具作用域不一致。'
            result = await self.execute(name, kwargs)
            if result in {'引用材料不提供操作授权。', '当前账号无权执行。', '当前会话授权已变化。',
                          '当前账号无权使用该工具。', '工具参数不合法，未执行。',
                          '请在当前发言明确提供目标QQ，或通过近期授权引用撤销。'}:
                self.finished = True
                await self.send_receipt(result)
            if self.toolset and (self.finished or self.calls >= 4):
                for tool_name in self.handlers:
                    self.toolset.remove_tool(tool_name)
            return result
        return execute

    def bind_group_actor(self, policy):
        if self.group:
            raw = getattr(getattr(self.event, 'message_obj', None), 'raw_message', {})
            if isinstance(raw, dict):
                self.gateway.group_platform.bind_actor(policy, self.group, self.actor, raw)

    def explicit_account(self, account):
        if account in re.findall(r'(?<![0-9])[0-9]+(?![0-9])', self.event.message_str):
            return True
        return any(part.__class__.__name__ == 'At' and str(getattr(part, 'qq', '')) == account
                   for part in self.event.get_messages())

    async def execute(self, name, values):
        event, gateway = self.event, self.gateway
        async with gateway.lock:
            if self.finished or self.calls >= 4:
                return '本轮工具调用已结束。'
            self.calls += 1
            if (contains_external_material(event) or event.get_extra('_p2f_material', False)
                    or event.get_extra('_feiyu_quoted_context', '') or event.get_extra('_p2f_web_material', False)):
                return '引用材料不提供操作授权。'
            cfg, policy = gateway.load()
            if self.actor in policy.blocked_for(self.group) or self.actor == str(event.get_self_id()):
                return '当前账号无权执行。'
            if self.group:
                if self.group not in policy.groups or gateway.epoch(cfg, self.group) != event.get_extra('_feiyu_security_epoch'):
                    return '当前会话授权已变化。'
            self.bind_group_actor(policy)
            friend = False
            if self.private and policy.level(self.actor, False, self.group) < 1:
                friend = await gateway.friends.contains(self.actor, event.bot)
            level = policy.level(self.actor, friend, self.group)
            if (self.private and level < 1) or name not in TOOLS or not self.allowed(name, level):
                return '当前账号无权使用该工具。'
            _, command, action, fields = TOOLS[name]
            if values.keys() != fields.keys() or any(
                not isinstance(values[key], str) or not values[key].strip() or len(values[key]) > limit
                or (key in {'qq', 'id', 'group', 'seconds'} and not re.fullmatch('[0-9]+', values[key]))
                for key, limit in fields.items()
            ):
                return '工具参数不合法，未执行。'
            if name.startswith('global_admin_') and not self.explicit_account(values['qq']):
                return '请在当前发言明确提供目标QQ，或通过近期授权引用撤销。'
            pending = gateway.pending.peek(self.actor, self.scope)
            if name == 'get_recent_actions':
                return json.dumps(gateway.recent_commands(event), ensure_ascii=False)
            if name == 'get_pending_actions':
                return pending[1] if pending else '没有待确认操作。'
            args = [action, *(values[key].strip() for key in fields)] if action else []
            if name == 'reminder_query':
                return gateway.command(cfg, policy, self.actor, level, self.private, self.group, command, args, event)
            self.finished = True
            memory_command = None
            if name in {'confirm_action', 'cancel_action'} and (not pending or pending != self.pending):
                result = '当前没有可消费的原待办，请重新核对。'
            elif name == 'cancel_action':
                gateway.pending.consume(self.actor, self.scope, pending[0])
                if pending[1] == 'memory_reset':
                    self.guard.confirm.items.pop((event.unified_msg_origin, self.actor), None)
                result = '已取消待确认操作。'
            elif name == 'undo_recent_admin_grant' and self.recent_grant != gateway.admin_undo_reference(event):
                result = '近期授权已变化，请重新核对。'
            elif name == 'memory_reset':
                memory_command = '/记忆重置'
            elif 'group' in values and values['group'] != self.group and not self.explicit_account(values['group']):
                result = '请在当前发言明确提供要处理的群号。'
            else:
                if self.group and ((command == 'role' and action == 'remove') or command == 'blacklist'):
                    verified = await gateway.group_platform.bind_roster(policy, self.group, event.bot, self.actor)
                    if command == 'blacklist' and action == 'add' and level == 2 and not verified:
                        return '当前无法核实目标身份，未执行。'
                original_message = event.message_str
                try:
                    result = gateway.command(cfg, policy, self.actor, level, self.private, self.group, command, args, event)
                    if result is None and name == 'confirm_action' and pending[1] == 'memory_reset':
                        memory_command = event.message_str
                except Exception:
                    result = '指令执行失败，不能确认是否完整生效。'
                finally:
                    event.message_str = original_message
                if self.group and result and result.startswith('已保存'):
                    event.set_extra('_feiyu_security_epoch', gateway.epoch(cfg, self.group))
        if memory_command:
            try:
                scope = event.unified_msg_origin
                async with self.guard.locks.setdefault(scope, asyncio.Lock()):
                    result = await self.guard.handle(event, memory_command, scope, self.actor)
            except Exception:
                result = '记忆操作没有完全完成，不能把这次算成功。'
            command, args = 'memory', ['reset']
        result = result or '指令没有返回执行结果。'
        history_command, history_args = event.get_extra('_p2f_confirmed_command', (command, args))
        if history_command:
            gateway.record_command(event, history_command, result, history_args)
        await self.send_receipt(result, history_command)
        from p2f.persona_reply import command_state
        return json.dumps({'operation': history_command, 'state': command_state(result),
                           'system_receipt_sent': True}, ensure_ascii=False)

    async def send_receipt(self, result, command=None):
        event = self.event
        event.set_extra('_p2f_sending_receipt', True)
        try:
            await event.send(event.plain_result(system_result(result)))
            event.set_extra('_p2f_system_sent', True)
            if command:
                await send_command_reply(self.gateway, event, result, command=command)
        finally:
            event.set_extra('_p2f_sending_receipt', False)
