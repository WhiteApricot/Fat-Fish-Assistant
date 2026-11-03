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
    'history': ['', 'all'],
    'confirm': ['[code]'],
    'cancel': [''],
    'undo': ['[command]'],
    'new': [''],
    'globaladmin': ['list', 'add QQ', 'remove QQ'],
}

HELP = {
    name: '\n'.join('/'+name+(' '+suffix if suffix else '') for suffix in variants)
    for name,variants in SYNTAX.items()
}
