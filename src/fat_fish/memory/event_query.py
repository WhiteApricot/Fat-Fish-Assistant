"""Conservative Chinese event constraints, independent of authorization."""

from dataclasses import dataclass
import re

NIGHT = re.compile("昨晚|夜间|夜里|夜班|入夜|晚间|晚上|天黑|天色暗|太阳落山")
DAY = re.compile("白天|白日|下午|日间")
RECENT = re.compile("上回|上一次|上次|最近|上一轮")
COMPLETED = re.compile("已[经实际]*.{0,8}(?:完成|做完|读完|收尾|送到|交付|签收|收尾|修好|记录|检查|入账|写进|取得|实际转交)|做完|看完|读完|办好|做成|修好|收尾|确实|实际|完成|已经|已确认")
CANCELLED = re.compile("取消|撤掉|作罢|中止|没看|没有发生|不看的|放弃|暂不|没有开展")
PLANNED = re.compile("计划|准备|打算|提议|待处理|没开始|还没|仍由|待检|列入日程|安排")
RUNNING = re.compile("还在|仍在|正在|执行中")
CONFIRM = re.compile(r"(?:吧|吗|过没有)[？?]?$|是不是|能确认")
ALTERNATIVE = re.compile("还是|哪次|哪一回")
REFERENT = re.compile(r"还是|哪[一]?(?:次|回|款|处|份|批|扇|箱|笔|项)|哪个领域|两.{0,6}都")
FIRST_PERSON_SUBJECT = re.compile(r"^\s*(?:请问[，,：:\s]*)?我(?!记得|想问|想知道|听说)")
LEGACY_CONFIRM = re.compile(r"(?:吧|了吗|过吗|过没有)[？?]?$|是不是")
HISTORY = re.compile("哪些|有什么|发生过什么|有何|进展|做了什么")
EVENT_LIST = re.compile(r"发生(?:过|了)?什么|有哪些|做过什么|做了什么|做了哪些|分别做了什么")
DAY_PARTS = (
    (r"凌晨", 0, 6), (r"清晨|早晨|早上", 6, 9), (r"上午", 6, 12),
    (r"中午|正午", 11, 14), (r"下午", 12, 18), (r"傍晚|黄昏", 17, 20),
    (r"昨晚|昨天晚上|夜晚|夜里|夜间|晚间|晚上|夜班|天黑|天色暗|太阳落山", 18, 6),
    (r"白天|白日|日间", 6, 18),
)


def transfer_direction(text: str) -> str | None:
    incoming = bool(re.search(r"收到|收到了|接到|交给你|转交给你|送给你", text))
    outgoing = bool(re.search(r"送给|送到|转交|交付|交给|交还|归还", text)
                    and not re.search(r"(?:交给|送给)你", text))
    if incoming != outgoing:
        return "incoming" if incoming else "outgoing"
    return None


def event_hours(text: str) -> frozenset[int] | None:
    """An unspecified or mixed time phrase does not impose a time constraint."""
    ranges = [(start, end) for pattern, start, end in DAY_PARTS if re.search(pattern, text)]
    if len(ranges) != 1:
        return None
    start, end = ranges[0]
    return frozenset(hour for hour in range(24)
                     if (start <= hour < end if start < end else hour >= start or hour < end))
ACTIONS = {
    "验证": ("验证", "测定", "测完", "标定", "化验", "核验", "核实", "试验", "检测", "评估"),
    "回看": ("回看", "翻出", "翻旧", "旧记录"),
    "渡河": ("过河", "过水", "跨水", "河道", "水道", "对岸", "另一边", "过江"),
    "巡查": ("巡查", "巡检", "还没检查", "检查", "查过", "出水口"),
    "修理": ("修好", "检修", "修复", "维修", "换新", "更换", "保养", "维护"),
    "交付": ("交付", "交接", "签收", "交还", "还回", "到手", "归还", "转交", "接班", "送到"),
    "登记": ("编号", "入册", "入账", "登记", "录入"),
    "观测": ("观测", "观察", "发现", "拍到", "看到了"),
    "品评": ("品评", "品尝", "试味", "尝到", "喝", "试了", "试茶"),
    "阅读": ("阅读", "查阅", "读完", "看完", "文档", "公示", "指南", "手册"),
}
CONFIRMATION_VERBS = re.compile("|".join(sorted(
    {word for aliases in ACTIONS.values() for word in aliases}
    | {"装好", "收集", "分到", "完成", "打开", "读", "看", "试", "去"},
    key=lambda word: (-len(word), word),
)))


@dataclass(frozen=True)
class EventIntent:
    actor: str | None
    phase: str | None
    action: str | None
    time_of_day: str | None
    recent: bool
    alternatives: bool
    confirmation: bool
    multiple: bool

    @property
    def structured(self) -> bool:
        return bool(self.phase or self.action or self.time_of_day or self.recent)


def parse_event_query(text: str) -> EventIntent:
    has_day, has_night = bool(DAY.search(text)), bool(NIGHT.search(text))
    time_of_day = ("night" if has_night else "day") if has_day != has_night else None
    actor = "bot" if re.search("你|肥鱼", text) and not re.search("替你|给你|告诉你", text) else None
    if CANCELLED.search(text):
        phase = "cancelled"
    elif RUNNING.search(text):
        phase = "running"
    elif PLANNED.search(text):
        phase = "planned"
    elif COMPLETED.search(text):
        phase = "succeeded"
    else:
        phase = None
    matches = {action for action, words in ACTIONS.items() if any(word in text for word in words)}
    action = next(iter(matches)) if len(matches) == 1 else None
    return EventIntent(actor, phase, action, time_of_day, bool(RECENT.search(text)),
                       bool(ALTERNATIVE.search(text)), bool(LEGACY_CONFIRM.search(text)),
                       bool(HISTORY.search(text)))


def confirmation_objects(text: str) -> tuple[str, ...]:
    """Keep complete lexical spans so a shared bigram alone cannot confirm a claim."""
    text = re.sub(r"昨天晚上|昨晚|昨天|今天|夜里|白天|后来|已经|确实|实际|是不是|能确认|没有|"
                  r"肥鱼|你|那款|那件|那次|[了吗吧过也的把给和与]", " ", text)
    text = CONFIRMATION_VERBS.sub(" ", text)
    return tuple(dict.fromkeys(re.findall(r"[\u3400-\u9fff]{2,}|[A-Za-z0-9-]{2,}", text)))
