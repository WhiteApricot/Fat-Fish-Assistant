import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest

from fat_fish.memory.erasure import ErasureLedger
from fat_fish.memory.event_query import parse_event_query
from fat_fish.memory.event_retrieval import EventRetrieval
from fat_fish.memory.policy import Audience, ScopeKind, ScopeRef
from fat_fish.memory.records import EventRecord, EvidenceRecord, ItemRecord, SourceRecord
from fat_fish.memory.repository import MemoryRepository


ZONE = timezone(timedelta(hours=8))
NIGHT = int(datetime(2026, 11, 23, 21, tzinfo=ZONE).timestamp() * 1000)
DAY = NIGHT - 8 * 3600 * 1000
NOW = NIGHT + 12 * 3600 * 1000


class EventRetrievalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        ledger = ErasureLedger(root / "erase.jsonl", "test", {"test": b"x" * 32}, "test")
        self.repo = MemoryRepository(root / "test.sqlite3", ledger)
        self.repo.open(ledger.create())
        self.public = ScopeRef("fish", ScopeKind.PUBLIC, "fish")
        self.group = ScopeRef("fish", ScopeKind.GROUP, "lark:room-2")
        self.alice = ScopeRef("fish", ScopeKind.PRIVATE, "alice")
        self.bob = ScopeRef("fish", ScopeKind.PRIVATE, "bob")
        self.caller = Audience("fish", "alice", self.alice, 1)

    def add(self, scope, key, content, *, actor="bot", phase="succeeded", time=NIGHT, action="渡河"):
        kind = "public_attestation" if scope.kind == ScopeKind.PUBLIC else "message"
        source = SourceRecord(scope, key, kind, "fixture", "verified_connector", key, time)
        event = self.repo.record_event(EventRecord(source, key, action, phase, actor, time, content), NOW)
        with self.repo.write() as conn:
            source_id = conn.execute("SELECT source_id FROM event WHERE id=?", (event,)).fetchone()[0]
        return self.repo.put_item(ItemRecord(scope, "fish", actor, "experience", key, "event",
                                            content, {}, (EvidenceRecord(source_id, event),),
                                            "verified_connector", 1, .7), None, NOW)

    def recall(self, query, audience=None):
        return asyncio.run(EventRetrieval(self.repo, clock_zone=ZONE).recall(audience or self.caller, query, NOW))

    def test_time_uses_occurred_at_and_subject_is_bound(self):
        night, _ = self.add(self.public, "night", "搭渡船抵达河岸")
        self.add(self.public, "day", "从旧桥步行到对岸", time=DAY)
        self.add(self.alice, "user", "昨夜乘快艇过河", actor="user")
        result = self.recall("夜里你怎么过河？")
        self.assertEqual(result.decision, "answer")
        self.assertEqual([hit.item_id for hit in result.support], [night])
        self.assertIn("time", result.support[0].matched)
        self.assertEqual(len(result.candidates), 2)
        self.assertNotEqual(result.support[0].event_id, 0)

    def test_cancellation_and_privacy(self):
        self.add(self.public, "cancelled", "更新天气站，后来取消", phase="cancelled", action="修理")
        self.add(self.bob, "private", "天气站更换工作已经完成", action="修理")
        with self.repo.write() as conn:
            room = self.repo.scope_id(conn, self.group)
            conn.execute("UPDATE scope SET enabled=1 WHERE id=?", (room,))
        self.add(self.group, "group", "天气站检修已经完成", action="修理")
        result = self.recall("你已经修好天气站了吗？")
        self.assertEqual(result.decision, "abstain")
        self.assertFalse(result.candidates)

    def test_erased_evidence_cannot_reappear_in_events_or_postings(self):
        self.add(self.public, "withdrawn", "昨晚渡船从码头出发")
        self.assertEqual(self.recall("夜里你如何过河？").decision, "answer")
        self.repo.erase("source", self.public, "withdrawn", 0, NOW)
        self.assertEqual(self.recall("夜里你如何过河？").decision, "abstain")
        self.assertFalse(self.repo.lexical_candidates(self.caller, NOW, ("渡船",), 12))

    def test_intent_fields_can_be_unknown(self):
        intent = parse_event_query("这件事你怎么看？")
        self.assertIsNone(intent.action)
        self.assertIsNone(intent.phase)
        self.assertIsNone(intent.time_of_day)

    def test_withdrawal_while_waiting_for_embedding(self):
        self.add(self.public, "inflight", "昨晚渡船从码头出发")
        repo, scope = self.repo, self.public

        class WithdrawDuringEmbedding:
            async def get_embedding(self, query):
                await asyncio.to_thread(repo.erase, "source", scope, "inflight", 0, NOW)
                return (1.0, 0.0)

        retriever = EventRetrieval(repo, WithdrawDuringEmbedding(), space="test", clock_zone=ZONE)
        result = asyncio.run(retriever.recall(self.caller, "夜里你如何过河？", NOW))
        self.assertEqual(result.decision, "abstain")
        self.assertFalse(result.candidates)
        self.assertFalse(result.support)

    def test_confirmation_requires_same_object_action_and_state(self):
        good, _ = self.add(self.public, "blue", "蓝册已经转交接班人", action="交付")
        self.add(self.public, "green", "绿册原定转交接班人", phase="planned", action="交付")
        self.add(self.public, "yellow", "黄册已登记，由接班人签字", action="登记")
        for query in ("绿册也转交接班人了吗？", "黄册转交接班人了吗？",
                      "蓝册已经登记了吗？", "蓝册和绿册已经转交了吗？"):
            with self.subTest(query=query):
                self.assertEqual(self.recall(query).decision, "abstain")
        result = self.recall("蓝册已经转交接班人了吗？")
        self.assertEqual([hit.item_id for hit in result.support], [good])

    def test_relative_date_excludes_previous_evening(self):
        good, _ = self.add(self.public, "yesterday", "渡船上能看到南塔")
        self.add(self.public, "earlier", "渡船上能看到北塔", time=NIGHT - 86400000)
        result = self.recall("昨晚你过河时看到了什么？")
        self.assertEqual([hit.item_id for hit in result.support], [good])

    def test_user_subject_is_not_replaced_by_bot_event(self):
        self.add(self.public, "bot", "昨晚搭渡船抵达南岸")
        result = self.recall("请问，我昨晚怎么过河的？")
        self.assertEqual(result.decision, "abstain")
        self.assertEqual(result.reason, "unsupported_subject")

    def test_clarification_requires_distinct_visible_events(self):
        self.add(self.public, "north", "巡查了北泉的水口", action="巡查")
        self.add(self.bob, "south", "巡查了南井的水口", action="巡查")
        self.assertNotEqual(self.recall("检查的是哪一处？").decision, "clarify")
        self.add(self.public, "east", "巡查了东沟的水口", action="巡查")
        self.assertEqual(self.recall("检查的是哪一处？").decision, "clarify")

    def test_ranked_lexical_candidate_outside_recent_event_window(self):
        older, _ = self.add(self.public, "old", "蓝册已经转交接班人",
                            time=NIGHT - 86400000, action="交付")
        for index in range(48):
            self.add(self.public, f"recent-{index}", "新箱已入库", action="登记")
        result = self.recall("蓝册已经转交接班人了吗？")
        self.assertEqual([hit.item_id for hit in result.support], [older])
