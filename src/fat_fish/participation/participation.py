"""Single event-driven read-air path; all sends remain in the access gateway."""
import asyncio
import hashlib
import json
import time
import re
from collections import deque
from fat_fish.participation.decision_adapter import DecisionAdapter


class Participation:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.rooms = {}
        self.provider = None
        self.salt = ''
        self.decision = DecisionAdapter()

    def label(self, group, sender):
        return '群友' + hashlib.sha256((self.salt+'|'+group+'|'+sender).encode()).hexdigest()[:8]

    def capture(self, group, epoch, sender, text, *, raw_text=None):
        key = (group, epoch)
        for old in list(self.rooms):
            if old[0] == group and old != key:
                self.rooms.pop(old)
        room = self.rooms.setdefault(key, dict(messages=deque(maxlen=30), version=0,
            last=-1e9, judged=-1e9, sent=deque(), busy=False, followups=0))
        now = self.clock()
        room['messages'].append((now, sender, text[:500]))
        room['version'] += 1
        if sender not in {'__bot__', '__system__'}:
            room['latest_user_text'] = text if raw_text is None else raw_text
        if sender == '__bot__':
            room['bot_at'] = now
        return room

    def context(self, group, epoch):
        room = self.rooms.get((group, epoch), {})
        return '\n'.join(json.dumps({'发言者': sender, '内容': text}, ensure_ascii=False)
                         for stamp, sender, text in room.get('messages', [])
                         if self.clock()-stamp <= 300)

    async def choose(self, group, epoch, cooldown=120, *, quote_context=''):
        room = self.rooms.get((group, epoch))
        if not room:
            return None
        def skip(reason):
            room['reason'] = reason
            return None
        def valid():
            return self.rooms.get((group, epoch)) is room
        def gate():
            now = self.clock()
            while room['sent'] and now-room['sent'][0] >= 3600:
                room['sent'].popleft()
            followup = now-room.get('bot_at', -1e9) <= 180 and room['followups'] < 4
            if room['busy']: return 'busy'
            if len(room['sent']) >= 12: return 'hourly_limit'
            if now-room['last'] < (2 if followup else cooldown): return 'cooldown'
            if now-room['judged'] < (4 if followup else 20): return 'judge_interval'
            return ''
        if self.provider is None:
            return skip('provider_unavailable')
        if room['busy']:
            return skip('busy')
        version = room['version']
        # Check cooldown after quiet time: a follow-up received immediately
        # after the final split must not be discarded before it has waited.
        await asyncio.sleep(0.8)
        if not valid() or room['version'] != version:
            return skip('superseded')
        reason = gate()
        if reason:
            return skip(reason)
        now = self.clock()
        recent = [x for x in room['messages'] if now-x[0] <= 300]
        if not recent or recent[-1][1] in {'__bot__', '__system__'}:
            return skip('no_user_message')
        # Explicit role address is a deterministic veto. The model may still
        # weigh indirect addressees; it must not override this clear request.
        body = room.get('latest_user_text', '').strip()
        if (not re.search(r'肥鱼|Bot', body, re.I) and re.match(
            r'^(?:(?:我(?:想)?问|问|请问|让|麻烦)\s*(?:群主|管理员)|(?:群主|管理员)\s*[,，:：、])', body)):
            return skip('other_addressee')
        # Stable anonymous labels also protect against raw caller IDs in tests.
        aliases = {}
        records = []
        for stamp, sender, text in recent:
            if sender not in {'__bot__', '__system__'} and not sender.startswith('群友'):
                aliases.setdefault(sender, '群友'+str(len(aliases)+1))
                sender = aliases[sender]
            records.append((stamp, sender, text))
        if quote_context and records:
            stamp,sender,text=records[-1]
            records[-1]=(stamp,sender,'[本条实际引用资料]'+quote_context[-1000:]+'\n[本条追问]'+text)
        followup = now-room.get('bot_at', -1e9) <= 180 and room['followups'] < 4
        # A verified quote of Bot, or the same recipient asking about their
        # immediately preceding question, is directed conversation in smart
        # mode. Keep all program budgets; do not require another @ or LLM veto.
        directed = followup and (
            '<quoted_message sender="Bot">' in quote_context or
            (recent[-1][1] == room.get('reply_to') and bool(re.search(
                r'我(?:刚才|最初|之前).*(?:问|引用).*(?:什么|啥|哪)|(?:刚才|最初).*(?:问题|引用).*(?:什么|啥|哪)', body)))
        ) and not re.search(r'别(?:回|说)|不用回|不要回', body)
        room['busy'] = True
        room['judged'] = now
        try:
            result = ({'should_reply': True} if directed else
                await self.decision.evaluate(self.provider, records,
                    bot_age=now-room.get('bot_at', -1e9), followup=followup))
            if not valid() or room['version'] != version:
                return skip('superseded')
            if result.get('should_reply') is not True:
                return skip('judge_no')
            # Count every admitted round, including follow-ups and failed sends.
            # Conservative accounting prevents retries from bypassing the cap.
            room['last'] = self.clock()
            room['sent'].append(room['last'])
            room['followups'] = room['followups']+1 if followup else 0
            room['choice_followup'] = followup
            room['reason'] = 'judge_yes_v2'
            return self.context(group, epoch)
        except Exception:
            return skip('judge_error')
        finally:
            room['busy'] = False
