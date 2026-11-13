"""Scoped persona entries with a projection compatible with legacy commands."""

PERSONA_HELP = (
    '/{command} list | set 内容 | add 内容 | edit 编号 内容 | remove 编号 | reset | undo'
)
PERSONA_ROUTES = {'': {0}, 'help': {1}, 'list': {1}, 'set': {2}, 'add': {2},
                  'edit': {3}, 'remove': {2}, 'reset': {1}}


def effective_notes(cfg, actor, group=''):
    global_note=cfg.get('global_persona_notes','')
    local=cfg.get('group_persona_notes',{}).get(group,'') if group else cfg.get('private_persona_notes',{}).get(actor,'')
    if not global_note:return local
    if not local:return global_note
    return f'全局表达要求：{global_note}\n当前会话表达要求（冲突时优先）：{local}'


def persona_catalog(cfg, scope, note):
    entry=cfg.get('persona_entries',{}).get(scope,{})
    next_id=entry.get('next_id',1)
    if entry.get('note')==note:
        rows=[list(row) for row in entry['rows']]
    else:
        rows=[[next_id,note]] if note else []
        next_id+=bool(note)
    return {'rows':rows,'next_id':next_id,'note':note}


def edit_persona(cfg,scope,note,args):
    catalog=persona_catalog(cfg,scope,note)
    rows=catalog['rows']
    next_id=catalog['next_id']
    action=args[0] if args else 'list'
    if action=='list' and len(args)<=1:
        return None,'\n'.join(f'{number}：{text}' for number,text in rows) or '当前作用域没有自定义人格要求。'
    if action not in PERSONA_ROUTES or len(args) not in PERSONA_ROUTES[action]:
        return None,'参数不完整，请使用 /help 查看人格指令。'
    if action in {'set','add'}:
        if action=='set':rows=[]
        if not args[1].strip():return None,'人格要求不能为空。'
        rows.append([next_id,args[1].strip()])
        next_id+=1
    elif action in {'edit','remove'}:
        if not args[1].isascii() or not args[1].isdecimal():return None,'请提供列表中的人格编号。'
        row=next((row for row in rows if str(row[0])==args[1]),None)
        if row is None:return None,'当前作用域没有此人格编号。'
        if action=='remove':rows.remove(row)
        else:
            if not args[2].strip():return None,'人格要求不能为空。'
            row[1]=args[2].strip()
    elif action=='reset':
        rows=[]
    else:
        return None,'请使用 /help 查看人格指令。'
    text='\n'.join(row[1] for row in rows)
    if len(text)>300:return None,'当前作用域的人格要求合计最多 300 字。'
    return {'next_id':next_id,'rows':rows,'note':text},'已保存当前作用域的人格要求。'
