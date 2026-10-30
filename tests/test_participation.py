import fixture_runtime
import os,sys
from pathlib import Path
RELEASE=Path(os.environ['FEIYU_TEST_RELEASE'])
sys.path[:0]=[str(RELEASE/'src'),str(RELEASE/'astrbot')]
import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(RELEASE/'src'), str(RELEASE/'astrbot')]
from p2d.participation import Participation


class Checks(unittest.IsolatedAsyncioTestCase):
    def setup_room(self):
        clock = [1000]
        p = Participation(lambda: clock[0])
        p.provider = object()
        calls = []
        async def evaluate(*args, **kwargs):
            calls.append(kwargs)
            return {'should_reply': True}
        p.decision = types.SimpleNamespace(evaluate=evaluate)
        return p, clock, calls

    async def test_immediate_followup_survives_final_split_cooldown(self):
        p, clock, calls = self.setup_room()
        room = p.capture('g', 0, '__bot__', '路线建议')
        room['last'] = clock[0]
        clock[0] += 1
        p.capture('g', 0, '123', '下雨怎么办')
        async def pause(_): clock[0] += 4
        with patch('p2d.participation.asyncio.sleep', pause):
            self.assertIsNotNone(await p.choose('g', 0))
        self.assertTrue(calls[0]['followup'])
        self.assertEqual(len(room['sent']), 1)

    async def test_epoch_revoked_while_judging(self):
        p, _, _ = self.setup_room()
        p.capture('g', 0, '123', '邀请肥鱼')
        async def evaluate(*args, **kwargs):
            p.capture('g', 1, '456', '新权限区')
            return {'should_reply': True}
        p.decision.evaluate = evaluate
        async def pause(_): pass
        with patch('p2d.participation.asyncio.sleep', pause):
            self.assertIsNone(await p.choose('g', 0))

    async def test_all_followups_obey_hourly_budget(self):
        p, clock, calls = self.setup_room()
        room = p.capture('g', 0, '__bot__', '回答')
        room['sent'].extend([clock[0]]*12)
        p.capture('g', 0, '123', '再说呢')
        async def pause(_): clock[0] += 4
        with patch('p2d.participation.asyncio.sleep', pause):
            self.assertIsNone(await p.choose('g', 0))
        self.assertFalse(calls)

    async def test_latest_arrival_and_cancellation(self):
        p, _, _ = self.setup_room()
        p.capture('g', 0, '123', '旧消息')
        async def pause(_): p.capture('g', 0, '456', '新消息')
        with patch('p2d.participation.asyncio.sleep', pause):
            self.assertIsNone(await p.choose('g', 0))
        async def cancelled(*args, **kwargs): raise asyncio.CancelledError
        p.decision.evaluate = cancelled
        async def quiet(_): pass
        with patch('p2d.participation.asyncio.sleep', quiet):
            with self.assertRaises(asyncio.CancelledError): await p.choose('g', 0)
        self.assertFalse(p.rooms[('g', 0)]['busy'])

    async def test_explicit_other_role_never_calls_judge(self):
        for body in ('群主，告诉我6*6是多少', '我问群主5+8得几', '问群主吧，4+4等于几'):
            p, _, calls = self.setup_room()
            p.capture('g', 0, '123', '昵称：测试；QQ群身份：群成员；'+body, raw_text=body)
            async def quiet(_): pass
            with patch('p2d.participation.asyncio.sleep', quiet):
                self.assertIsNone(await p.choose('g', 0))
            self.assertEqual(p.rooms[('g', 0)]['reason'], 'other_addressee')
            self.assertFalse(calls)

    async def test_same_recipient_reference_followup_needs_no_mention(self):
        p, clock, calls = self.setup_room()
        room=p.capture('g',0,'群友A','潮汐是什么')
        p.capture('g',0,'__bot__','潮汐解释')
        room['reply_to']='群友A'
        clock[0]+=10
        p.capture('g',0,'群友A','我最初引用的问题是啥')
        async def quiet(_): pass
        with patch('p2d.participation.asyncio.sleep',quiet):
            self.assertIsNotNone(await p.choose('g',0))
        self.assertFalse(calls)
        self.assertEqual(len(room['sent']),1)


if __name__ == '__main__': unittest.main(verbosity=2)
