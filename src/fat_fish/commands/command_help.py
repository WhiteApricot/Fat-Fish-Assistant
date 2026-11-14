"""Help for the shared chat commands."""


def platform_memory_help(*, private, preferences, can_change=True):
    variants = []
    if private:
        variants.extend([
            'remember key content', 'search query', 'update key revision content',
            'delete key revision', 'archive query', 'history [query]', 'reset',
        ])
    if private or preferences:
        variants.extend(['card [query]', 'card-archive [query]'])
        if can_change:
            variants.extend(['pin id', 'unpin id', 'merge id id [| title]',
                             'meet id [id] [| title]', 'auto-meet', 'rename id | 标题', 'erase-card id'])
    if preferences:
        if can_change:
            variants.extend(['on', 'off'])
            if not private:
                variants.append('card-sharing on|off')
                variants.append('reset')
        variants.append('status')
    return '\n'.join('/memory ' + syntax for syntax in variants)


DESCRIPTIONS = {
    "help": "查看指令或指定类别的帮助", "role": "查看身份或管理权限",
    "memory": "管理本人私聊记忆", "groupmemory": "管理本群共享记忆",
    "confirm": "确认当前会话最新的待办", "cancel": "取消当前会话待办",
    "history": "查看近期操作", "undo": "尝试撤销近期操作",
    "naturalcommand": "查看或切换自然语言指令", "webresearch": "查看或切换联网搜索",
    "persona": "管理当前会话人格", "globalpersona": "管理全局人格",
    "privatepersona": "管理本人私聊表达补充", "grouppersona": "管理本群表达补充",
    "groupsettings": "查看或设置本群插话冷却", "groupmode": "查看或切换本群回复模式",
    "whitelist": "查看或调整已授权的群", "blacklist": "查看或调整当前范围的黑名单",
    "globaladmin": "查看或管理全局管理员", "new": "开启新的私聊会话",
    "personal": "查看或调整私聊回复长度", "remind": "创建和管理当前会话的提醒",
    "invite": "生成跨平台绑定邀请码", "link": "绑定到发起邀请的身份", "unlink": "申请解除平台绑定",
}


def platform_help(private, admission, args, *, reminders, registry, global_features=None):
    from fat_fish.core.identity import Role

    menus = {
        "help": "/help [命令]",
        "role": "/role",
        "confirm": "/confirm",
        "cancel": "/cancel",
        "history": "/history [all]",
        "persona": "/persona help",
    }
    admin = admission.role >= Role.ADMIN
    for name in ("naturalcommand", "webresearch"):
        local = (private and admission.role >= Role.MEMBER or admission.room_moderator) and (
            (global_features or {}).get(name) is True)
        variants = [f"/{name} on|off"] if local else []
        if admin:
            variants.append(f"/{name} global [on|off]")
        if variants:
            menus[name] = " · ".join(variants)
    if reminders:
        menus["remind"] = "/remind help"
    menus["undo"] = "/undo [命令]"
    if private:
        menus.update({
            "memory": "/help memory",
            "new": "/new",
            "personal": "/personal 简短|标准|详细",
            "privatepersona": "/privatepersona [set 内容|reset|undo]",
            "invite": "/invite",
            "link": "/link 邀请码",
            "unlink": "/unlink 平台 实例 账号",
        })
        if admission.role >= Role.ADMIN:
            menus["role"] += " · /role grant 平台 实例 账号 member|admin|blocked"
            menus["globalpersona"] = "/globalpersona help"
            menus["whitelist"] = "/whitelist list · /whitelist add|remove 群号"
            menus["globaladmin"] = "/globaladmin list"
            menus["blacklist"] = "/blacklist list · /blacklist add|remove 账号"
            if admission.role == Role.OWNER:
                menus["globaladmin"] += " · /globaladmin add|remove 账号"
    else:
        menus["groupmode"] = "/groupmode"
        if admission.room_moderator:
            menus["groupmode"] += " · /groupmode mention|smart"
            menus["groupsettings"] = "/groupsettings cooldown 秒数"
            menus["blacklist"] = "/blacklist list · /blacklist add|remove 账号"
            menus["groupmemory"] = "/help groupmemory"
            menus["grouppersona"] = "/grouppersona [set 内容|reset]"
            menus["role"] += " · /role list"
            if admission.role >= Role.ADMIN:
                menus["role"] += " · /role moderator-add|moderator-remove 账号"
        else:
            menus.pop("persona")
        if admission.role == Role.OWNER:
            menus["whitelist"] = "/whitelist add · /whitelist remove"
    # Identity commands use provider proof before ordinary chat admission.
    menus = {name: description for name, description in menus.items()
             if name in {"invite", "link", "unlink"} or registry.find(name) is not None}
    extensions = registry.help(admission)
    if args:
        if len(args) != 1:
            return "用法：/help [命令]"
        name = args[0].lower().removeprefix("/")
        if name in extensions:
            return extensions[name]
        if name not in menus:
            return "当前会话没有此命令帮助。"
        return "\n".join(f"{syntax} — {DESCRIPTIONS[name]}" for syntax in menus[name].split(" · "))
    return "\n".join([*(f"/{name} — {DESCRIPTIONS[name]}" for name in menus), *extensions.values()])
