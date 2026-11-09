"""English command names and one entry per line for the QQ help menus."""

SYNTAX = {
    'help': ['[command]'],
    'role': ['', 'list [group]', 'add QQ [group]', 'remove QQ [group|all]', 'set QQ 1|2 [group]',
             'global list', 'global add QQ', 'global remove QQ', 'undo'],
    'whitelist': ['list', 'add group', 'remove group'],
    'blacklist': ['list', 'add QQ', 'remove QQ'],
    'groupmode': ['', 'mention', 'smart'],
    'groupsettings': ['', 'cooldown seconds'],
    'personal': ['', 'short', 'normal', 'detailed'],
    'memory': ['remember content', 'search query', 'update id content', 'delete id', 'reset', 'confirm', 'cancel'],
    'groupmemory': ['remember content', 'search query', 'update id content', 'delete id', 'reset', 'cancel'],
    'reminder': ['add 时间提醒我事项', 'addto QQ 时间提醒事项', 'list', 'show id', 'time id 时间',
                 'text id 事项', 'pause id', 'resume id', 'delete id', 'select 明天或每周X [新时间]', 'audio'],
    'privatepersona': ['', 'set content', 'reset', 'undo'],
    'grouppersona': ['', 'set content', 'reset'],
    'persona': ['list', 'set content', 'add content', 'edit id content', 'remove id', 'reset', 'undo'],
    'globalpersona': ['list', 'set content', 'add content', 'edit id content', 'remove id', 'reset', 'undo'],
    'naturalcommand': ['', 'on', 'off'],
    'webresearch': ['', 'on', 'off'],
    'history': ['', 'all'],
    'confirm': ['[code]'],
    'cancel': [''],
    'undo': ['[command]'],
    'new': [''],
    'globaladmin': ['list', 'add QQ', 'remove QQ'],
}


# The parser continues to use SYNTAX. Presentation adds descriptions only.
DESCRIPTIONS = {
    'help': '查看指令或指定类别的帮助',
    'role': '查看身份或管理群授权',
    'whitelist': '查看或调整已授权的群',
    'blacklist': '查看或调整当前范围的黑名单',
    'groupmode': '查看或切换本群回复模式',
    'groupsettings': '查看或设置本群插话冷却',
    'personal': '查看或调整私聊回复长度',
    'memory': '管理本人私聊记忆',
    'groupmemory': '管理本群共享记忆',
    'reminder': '创建和管理当前会话的提醒',
    'privatepersona': '管理本人私聊表达补充',
    'grouppersona': '管理本群表达补充',
    'persona': '管理当前会话人格',
    'globalpersona': '管理全局人格',
    'naturalcommand': '查看或切换自然语言指令',
    'webresearch': '查看或切换当前会话联网搜索',
    'history': '查看近期操作',
    'confirm': '确认当前会话待办，可提供确认码',
    'cancel': '取消当前会话待办',
    'undo': '尝试撤销本会话近期操作',
    'new': '开启新的私聊会话',
    'globaladmin': '查看或管理全局管理员',
}

DETAILS = {
    'list': '查看列表', 'add': '添加', 'remove': '移除', 'set': '设置',
    'undo': '尝试撤销近期修改', 'reset': '恢复默认设置', 'edit': '修改指定编号',
    'mention': '仅在被提及时回复', 'smart': '启用智能插话',
    'cooldown': '设置插话冷却，30 至 1800 秒',
    'short': '使用简短回复', 'normal': '使用标准回复', 'detailed': '使用详细回复',
    'remember': '写入记忆', 'search': '按关键词查询记忆', 'update': '更新指定记忆',
    'delete': '删除指定记录', 'confirm': '确认本人记忆清除', 'cancel': '取消待办',
    'addto': '为指定群成员创建提醒', 'show': '查看指定提醒',
    'time': '修改提醒时间', 'text': '修改提醒事项', 'pause': '暂停提醒',
    'resume': '恢复提醒', 'select': '修改唯一匹配的提醒，省略新时间则删除',
    'audio': '查看自定义提醒音频暂未开放的说明',
    'on': '开启自然语言指令', 'off': '关闭自然语言指令',
    'all': '查看近期操作列表',
}

def help_line(name, syntax, summary=False):
    operation = syntax.split()[0] if syntax else ''
    description = DETAILS.get(operation, DESCRIPTIONS[name])
    if name == 'role':
        description = {
            '': '查看本人身份', 'list': '查看管理员', 'add': '授予群管理权限，需确认',
            'remove': '撤销群管理权限，可指定 all', 'set': '设为普通好友或群管理员，授权需确认',
            'global list': '查看全局管理员', 'global add': '授予全局管理权限，需确认',
            'global remove': '撤销全局管理权限，需确认', 'undo': '尝试取消或撤销近期授权',
        }.get(syntax.removesuffix(' QQ') if operation == 'global' else operation, description)
    elif name == 'globaladmin' and operation in {'add', 'remove'}:
        description = ('授予' if operation == 'add' else '撤销')+'全局管理权限，需确认'
    elif name in {'memory', 'groupmemory'} and operation == 'reset':
        description = '申请清除当前范围的记忆，需确认'
    elif name == 'reminder' and operation == 'add':
        description = '创建自己的提醒'
    elif name == 'webresearch' and operation in {'on','off'}:
        description = ('开启' if operation=='on' else '关闭')+'当前会话联网搜索'
    elif name == 'history' and not syntax:
        description = '查看最近一项操作'
    if summary and '需确认' not in description:
        description = DESCRIPTIONS[name]
    return f"/{name}{' '+syntax if syntax else ''} — {description}"

HELP = {name: '\n'.join(help_line(name, syntax) for syntax in variants)
        for name, variants in SYNTAX.items()}

def scoped_help(policy, sender, level, private, group, target=None, *, reminders=False):
    menus = {name: SYNTAX[name] for name in ('help','role','history','confirm','cancel','undo')}
    if reminders:
        menus['reminder'] = [s for s in SYNTAX['reminder']
                             if not s.startswith('addto ') or not private]
    personal = policy.authorize(sender,'personal',private=private,group=group,friend=level>=1)
    group_admin = policy.authorize(sender,'group',private=private,group=group)
    if personal:
        menus.update({name: SYNTAX[name] for name in ('memory','personal','privatepersona','new')})
    if group_admin:
        menus.update({name: SYNTAX[name] for name in ('groupmemory','groupmode','groupsettings','grouppersona')})
    if personal or group_admin:
        menus.update({name: SYNTAX[name] for name in ('persona','naturalcommand','webresearch')})
    if policy.authorize(sender,'blacklist',private=private,group=group,target='0'):
        menus['blacklist'] = SYNTAX['blacklist']
    if level>=2.5:
        menus.update({name: SYNTAX[name] for name in ('globaladmin','globalpersona','whitelist')})
        if level!=3:
            menus['globaladmin'] = ['list']
            menus['role'] = [s for s in menus['role'] if s not in {'global add QQ','global remove QQ'}]
        if private:
            menus['role'] = [s.replace(' [group]', ' group').replace('[group|all]', 'group|all')
                             if s.startswith(('add ', 'remove ', 'set ')) else s for s in menus['role']]
    else:
        menus['role'] = ['', 'list', 'undo'] if group_admin else ['', 'undo']
    if target is not None:
        variants = menus.get(target)
        if variants is None:
            return '当前场景没有这个可用子菜单。'
        return '\n'.join(help_line(target, syntax) for syntax in variants)
    return '\n'.join(help_line(name, variants[0], summary=True) for name, variants in menus.items())
