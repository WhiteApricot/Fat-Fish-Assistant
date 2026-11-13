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


def platform_help(private, admission, args, *, reminders, registry):
    from fat_fish.core.identity import Role

    menus = {
        "help": "/help [命令]",
        "role": "/role",
        "memory": "/help memory",
        "confirm": "/confirm 确认码",
        "cancel": "/cancel",
        "history": "/history [all]",
        "naturalcommand": "/naturalcommand on|off",
        "webresearch": "/webresearch on|off",
        "persona": "/persona help",
    }
    if reminders:
        menus["remind"] = "/remind help"
        menus["undo"] = "/undo · 撤销最近的提醒操作"
    if private:
        menus.update({
            "new": "/new",
            "personal": "/personal 简短|标准|详细",
            "ffa-invite": "/ffa-invite 平台 实例 账号",
            "ffa-unlink": "/ffa-unlink 平台 实例 账号",
        })
        if admission.role >= Role.ADMIN:
            menus["role"] += " · /role grant 平台 实例 账号 member|admin|blocked"
            menus["globalpersona"] = "/globalpersona help"
        if admission.role >= Role.ADMIN:
            menus["whitelist"] = "/whitelist list · /whitelist add|remove 群号"
    else:
        menus["groupmode"] = "/groupmode"
        if admission.room_moderator:
            menus["groupmode"] += " · /groupmode mention|smart"
            menus["groupsettings"] = "/groupsettings cooldown 秒数"
            menus["blacklist"] = "/blacklist list · /blacklist add|remove 账号"
            menus["groupmemory"] = "/help groupmemory"
            menus["role"] += " · /role list · /role add|remove 账号"
            if admission.role >= Role.ADMIN:
                menus["role"] += " · /role moderator-add|moderator-remove 账号"
        else:
            menus.pop("persona")
            menus.pop("webresearch")
            menus.pop("naturalcommand")
    # Identity commands use provider proof before ordinary chat admission.
    menus = {name: description for name, description in menus.items()
             if name.startswith("ffa-") or registry.find(name) is not None}
    menus.update(registry.help(admission))
    if args:
        return menus.get(args[0].lower().removeprefix("/"), "当前会话没有此命令帮助。") if len(args) == 1 else "用法：/help [命令]"
    return "\n".join(menus.values())
