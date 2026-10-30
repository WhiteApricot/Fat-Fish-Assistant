"""P2C policy core. No LLM, platform I/O, or implicit owner promotion."""
import asyncio
import hmac
import secrets
import time
from dataclasses import dataclass, field

@dataclass
class Policy:
    owner: str
    admins: set[str] = field(default_factory=set)
    blocked: set[str] = field(default_factory=set)
    groups: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not self.owner.isdecimal():
            raise ValueError('Exactly one numeric owner is required')
        if any(mode not in {'mention', 'smart'} for mode in self.groups.values()):
            raise ValueError('Invalid group mode')

    def level(self, sender, friend=False, group=""):
        sender = str(sender)
        if sender in self.blocked:
            return -1
        return 3 if sender == self.owner else 2 if sender in self.admins else 1 if friend else 0

    def blocked_for(self, group=""):
        return self.blocked

    def allowed(self, sender, private, group='', friend=False):
        level = self.level(sender, friend)
        return level >= 0 and (level >= 1 if private else str(group) in self.groups)

    def authorize(self, sender, operation, *, private, group='', friend=False, target=''):
        level = self.level(sender, friend)
        if level < 0:
            return False
        if operation in {'owner', 'work'}:
            # Work remains unavailable throughout P2C, including for owner.
            return False
        if operation == 'admin':
            return level == 3 and str(target) != self.owner
        if operation == 'blacklist':
            return level >= 2 and str(target) != self.owner and (level == 3 or str(target) not in self.admins)
        if operation == 'whitelist':
            return level >= 2
        if operation == 'group':
            return level >= 2 and not private and str(group) in self.groups
        if operation in {'personal', 'memory'}:
            return level >= 1 and private
        return False

class FriendDirectory:
    """Only successful OneBot get_friend_list responses establish friendship.

    Cache expires after a short TTL; failures never extend stale positive data.
    """
    def __init__(self, ttl=30, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self.ids, self.until = set(), 0
        self.lock = asyncio.Lock()

    async def contains(self, sender, bot):
        async with self.lock:
            if self.clock() >= self.until:
                self.ids, self.until = set(), 0
                try:
                    rows = await asyncio.wait_for(bot.call_action('get_friend_list'), 5)
                    if not isinstance(rows, list) or any(not isinstance(x, dict) or not str(x.get('user_id', '')).isdecimal() for x in rows):
                        return False
                    self.ids = {str(x['user_id']) for x in rows}
                    self.until = self.clock() + self.ttl
                except Exception:
                    return False
            return str(sender) in self.ids

class PendingActions:
    """Confirmation binds actor, conversation, operation and exact arguments."""
    def __init__(self, clock=time.monotonic):
        self.clock, self.pending = clock, {}

    def issue(self, actor, scope, operation, arguments):
        code = secrets.token_hex(4)
        self.pending[(str(actor), scope)] = (code, operation, tuple(arguments), self.clock()+90)
        return code

    def consume_latest(self, actor, scope):
        item=self.pending.get((str(actor),scope))
        return self.consume(actor,scope,item[0]) if item else None

    def consume(self, actor, scope, code):
        key = (str(actor), scope)
        item = self.pending.get(key)
        if not item:
            return None
        if self.clock() >= item[3]:
            self.pending.pop(key, None)
            return None
        if not hmac.compare_digest(item[0], str(code)):
            return None
        self.pending.pop(key)
        return item[1], item[2]


@dataclass
class ScopedPolicy:
    owner: str
    groups: dict[str,str]
    group_admins: dict[str,set[str]] = field(default_factory=dict)
    blocked: set[str] = field(default_factory=set)
    group_blocked: dict[str,set[str]] = field(default_factory=dict)
    admins: set[str] = field(default_factory=set)  # intentionally empty

    def blocked_for(self,group=''):
        return self.blocked | self.group_blocked.get(str(group),set())

    def level(self,sender,friend=False,group=''):
        sender=str(sender)
        if sender in self.blocked_for(group):return -1
        if sender==self.owner:return 3
        if group and sender in self.group_admins.get(str(group),set()):return 2
        return 1 if friend else 0

    def authorize(self,sender,operation,*,private,group='',friend=False,target=''):
        level=self.level(sender,friend,group)
        if level<0:return False
        if operation in {'work','owner'}:return False
        if operation=='admin':return level==3 and str(target)!=self.owner and bool(group)
        if operation=='whitelist':return level==3
        if operation=='blacklist':
            if str(target)==self.owner:return False
            if private:return level==3
            return group in self.groups and (level==3 or (level==2 and str(target) not in self.group_admins.get(group,set())))
        if operation in {'group','persona'}:return not private and group in self.groups and level>=2
        if operation in {'personal','memory'}:return private and level>=1
        return False
