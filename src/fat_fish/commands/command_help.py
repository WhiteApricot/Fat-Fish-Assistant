"""Platform-neutral help generated from one catalog for both command spellings."""

from fat_fish.core.identity import Role


def render_help(name, actions):
    return "\n".join(f"/{name}{' ' + syntax if syntax else ''} — {description}"
                     for syntax, description in actions)


PERSONA = (
    ("list", "查看当前范围的人格条目及编号"),
    ("set [内容]", "用一条表达要求替换现有条目"),
    ("add [内容]", "追加表达要求"),
    ("edit [编号] [内容]", "修改 list 所示的指定条目"),
    ("remove [编号]", "删除 list 所示的指定条目"),
    ("reset", "清除当前范围的自定义表达要求"),
    ("undo", "撤销最近一次当前范围的人格变更"),
)
MEMORY_PRIVATE = (
    ("remember [记忆键] [内容]", "保存一条记忆，记忆键为自定的无空格标识"),
    ("search [关键词]", "查询当前记忆"),
    ("archive [关键词]", "查询记忆，包含已归档内容"),
    ("history [关键词]", "查看近期记忆经历，可省略关键词"),
    ("update [记忆键] [修订号] [内容]", "修改记忆，修订号取自检索结果的 v 后数字"),
    ("delete [记忆键] [修订号]", "删除指定修订的记忆"),
)
MEMORY_READ = (
    ("card [关键词]", "查看记忆卡，可省略关键词"),
    ("card-archive [关键词]", "查看归档记忆卡，可省略关键词"),
)
MEMORY_EDIT = (
    ("pin [卡号]", "置顶记忆卡"),
    ("unpin [卡号]", "取消记忆卡置顶"),
    ("merge [卡号] [卡号] [| 标题]", "合并多张卡；标题可省略，填写时在前面加竖线"),
    ("meet [卡号] [更多卡号] [| 标题]", "整理相关记忆卡；更多卡号和标题可省略"),
    ("auto-meet", "自动选择相关记忆卡整理"),
    ("rename [卡号] | [标题]", "用竖线分隔卡号和新标题"),
    ("erase-card [卡号]", "移除指定记忆卡"),
)
MEMORY_SWITCH = (
    ("status", "查看当前范围是否启用长期记忆"),
    ("on", "启用当前范围长期记忆"),
    ("off", "暂停当前范围长期记忆"),
)
MEMORY_RESET = ("reset", "申请清除当前范围记忆，需在同一会话确认")
REMIND = (
    ("add [描述]", "用自然语言填写时间、循环和事项，由模型解析"),
    ("add-exact [时间] [循环] [事项]", "时间填 +30s 或台北时间 YYYY-MM-DDTHH:MM[:SS]；循环填 once、daily、weekly、monthly 或 every:30m"),
    ("list", "列出当前会话可管理的提醒"),
    ("show [编号]", "查看提醒详情和投递记录，编号取自 list"),
    ("time [编号] [时间]", "修改提醒时间"),
    ("text [编号] [内容]", "修改提醒事项"),
    ("pause [编号]", "暂停循环提醒"),
    ("resume [编号]", "恢复循环提醒"),
    ("delete [编号]", "删除指定提醒"),
)
BLOCKLIST = (
    ("list", "列出当前范围的黑名单"),
    ("add [账号]", "限制指定账号，群内可改为 @ 一名成员"),
    ("remove [账号]", "解除指定账号的限制，群内可改为 @ 一名成员"),
)
DESCRIPTIONS = {
    "help": "查看指令或指定类别的帮助", "role": "查看身份或管理权限",
    "memory": "管理当前范围的记忆和卡片", "groupmemory": "管理本群共享记忆",
    "confirm": "确认当前会话最新的待办", "cancel": "取消当前会话待办",
    "history": "查看近期操作", "undo": "撤销近期操作",
    "naturalcommand": "查看或切换自然语言指令", "websearch": "查看或切换联网搜索",
    "persona": "管理当前会话人格", "globalpersona": "管理全局人格",
    "privatepersona": "管理本人私聊表达补充", "grouppersona": "管理本群表达补充",
    "groupsettings": "查看或设置本群插话冷却", "groupmode": "查看或切换本群回复模式",
    "whitelist": "查看或调整已授权的群", "blacklist": "查看或调整当前范围的黑名单",
    "globaladmin": "查看或管理全局管理员", "new": "开启新的私聊会话",
    "personal": "查看或调整私聊回复长度", "remind": "创建和管理当前会话的提醒",
    "invite": "生成跨平台绑定邀请码", "link": "绑定到发起邀请的身份", "unlink": "申请解除平台绑定",
    "owner": "验证主人身份",
}


def platform_memory_help(*, private, preferences, can_change=True, owner=False):
    actions = MEMORY_PRIVATE if private else ()
    if private or preferences:
        actions += MEMORY_READ
        if can_change:
            actions += MEMORY_EDIT
    if preferences:
        actions += MEMORY_SWITCH if can_change else MEMORY_SWITCH[:1]
        if can_change and not private:
            actions += (("card-sharing [on|off]", "开启或关闭本群卡片向已授权的私聊投影"),)
    if can_change:
        actions += (MEMORY_RESET, ("analyze", "安排后台分析当前来源中已回复的对话"),
                    ("analysis-status", "查看当前来源的记忆分析队列"))
    if private and can_change and owner:
        actions += (("pin-public [公开内容 ID]", "置顶一条已验证的 Bot 公开经历"),
                    ("forget-public [公开内容 ID]", "删除一条 Bot 公开经历及其索引"),
                    ("focus [confirm|activate|complete|abandon] [主题]", "更新已由公开经历支持的关注"))
    return render_help("memory", actions)


def platform_help(private, admission, args, *, reminders, registry, global_features=None, platform=None):
    menus = {
        "help": (("", "列出当前会话的命令"), ("[命令]", "查看指定命令的子菜单")),
        "role": (("", "查看当前角色"),),
        "confirm": (("", "确认同一会话中最新且未过期的操作"),),
        "cancel": (("", "取消同一会话中待确认的操作"),),
        "history": (("", "查看最近一次指令回执"), ("all", "列出近期的指令回执")),
        "undo": (("", "撤销最近可以撤销的操作"), ("[命令]", "指定操作类别，例如 remind、persona 或 websearch")),
    }
    admin = admission.role >= Role.ADMIN
    moderator = admission.room_moderator
    for name in ("naturalcommand", "websearch"):
        if admission.role >= Role.MEMBER and (private or moderator or admin):
            actions = (("", "查看当前会话的功能状态"),)
            if private or moderator:
                actions += (("[on|off]", "开启或关闭当前会话的功能，需要先开启全局功能"),)
            if admin:
                actions += (("global", "查看全局状态"), ("global [on|off]", "开启或关闭全局功能"))
            menus[name] = actions
    if reminders:
        menus["remind"] = REMIND
        if not private and moderator:
            menus["remind"] += (
                ("addto [账号] [描述]", "为指定群成员创建自然语言提醒"),
                ("addto-exact [账号] [时间] [循环] [事项]", "为指定群成员严格创建，时间和循环格式与 add-exact 相同"),
            )
    if private or moderator:
        menus["persona"] = PERSONA
        menus["privatepersona" if private else "grouppersona"] = PERSONA
    if private:
        menus.update({
            "new": (("", "开启新私聊对话"),),
            "personal": (("", "查看当前回复长度"), ("[简短|标准|详细]", "调整私聊回复长度")),
            "invite": (("", "生成五分钟内有效的跨平台邀请码"),),
            "link": (("[邀请码]", "关联发起邀请的同一用户身份"),),
            "unlink": (("[平台] [实例] [账号]", "申请解除指定绑定，随后使用 /confirm"),),
            "owner": (("", "发起主人验证"), ("[验证令牌]", "提交本次验证获得的令牌")),
        })
        if admin:
            menus["globalpersona"] = PERSONA
            menus["blacklist"] = BLOCKLIST
            menus["whitelist"] = (
                ("list", "查看授权群"),
                ("add [群号]", "申请授权当前平台指定群，需确认"),
                ("remove [群号]", "申请撤销当前平台指定群授权，需确认"),
            )
            menus["globaladmin"] = (("list", "列出当前平台的全局管理员"),)
            menus["role"] += (("grant [平台] [实例] [账号] [member|admin|blocked]", "授予成员、管理员或限制角色，管理员任免限主人，需确认"),)
            if admission.role == Role.OWNER:
                menus["globaladmin"] += (("add [账号]", "授予全局管理员，需确认"), ("remove [账号]", "降为普通成员，需确认"))
    else:
        if admin:
            menus["whitelist"] = (("list", "查看授权群"),)
            if admission.role == Role.OWNER:
                menus["whitelist"] += (("add", "在本群 @机器人申请群授权，需确认"), ("remove", "在本群 @机器人申请撤销授权，需确认"))
            if platform != "qq":
                menus["role"] += (
                    ("moderator-add [账号]", "任命本群 Bot 管理员，需确认，也可 @成员"),
                    ("moderator-remove [账号]", "撤销本群 Bot 管理员，需确认，也可 @成员"),
                )
        if moderator:
            menus["role"] += (("list", "查看本群管理权限"),)
            menus["groupmode"] = (("", "查看本群回复模式"), ("[mention|smart]", "选择仅 @回复或智能插话，需确认"))
            menus["groupsettings"] = (("", "查看插话冷却"), ("cooldown [秒数]", "设置 30 至 1800 秒的插话冷却"))
            menus["blacklist"] = BLOCKLIST
            menus["groupmemory"] = (
                ("search [关键词]", "查询本群共享记忆"), ("remember [内容]", "写入本群共享记忆"),
                ("update [记忆键] [内容]", "修改一条本群记忆，记忆键取自查询结果"),
                ("delete [记忆键]", "删除一条本群记忆"), MEMORY_RESET,
                ("analyze", "安排后台分析本人在本群已收到回复的对话"),
                ("analysis-status", "查看本人在本群的分析队列"),
            )
    rendered = {name: render_help(name, actions) for name, actions in menus.items()}
    rendered["memory"] = platform_memory_help(private=private, preferences=True,
                                             can_change=private or moderator, owner=admission.role == Role.OWNER)
    extensions = registry.help(admission)
    if args:
        if len(args) != 1:
            return render_help("help", menus["help"])
        name = args[0].lower().removeprefix("/")
        name = {"reminder": "remind", "webresearch": "websearch", "comfirm": "confirm"}.get(name, name)
        return extensions.get(name) or rendered.get(name) or "当前会话没有此命令帮助。"
    return "\n".join([*(f"/{name} — {DESCRIPTIONS[name]}" for name in rendered), *extensions.values()])
