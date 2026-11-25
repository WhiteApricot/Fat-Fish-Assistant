import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from threading import Event
import unittest
from unittest.mock import patch

from fat_fish.communication.model import Account, ChatKind, Conversation, MessageEvent, ProviderId
from fat_fish.memory.capture import CaptureQueue
from fat_fish.memory.cognitive_gateway import CognitiveMemoryGateway
from fat_fish.memory.erasure import ErasureLedger, LedgerError
from fat_fish.memory.policy import ScopeKind, ScopeRef
from fat_fish.memory.reply_guard import Delivery
from fat_fish.memory.repository import MemoryRepository
from fat_fish.memory.structured_memory import parse_claims


def claim(body, *, state="asserted", entities=(), episode=None, occurred_at=None):
    return {"kind": "fact" if state == "asserted" else "event", "body": body, "quote": body,
            "confidence": .96, "state": state, "entities": list(entities), "episode": episode,
            "occurred_at": occurred_at, "valid_from": None, "valid_to": None}


class ProviderStub:
    def __init__(self, claims, targets=()):
        self.claims = claims
        self.targets = targets

    async def text_chat(self, *, prompt, **kwargs):
        packet = json.loads(prompt)
        if "source" in packet:
            return SimpleNamespace(completion_text=json.dumps({"claims": self.claims}, ensure_ascii=False))
        if "records" not in packet:
            return SimpleNamespace(completion_text=json.dumps({"route": "deep", "targets": self.targets}))
        support = [[ids[0]] for ids in packet["targets"].values()]
        return SimpleNamespace(completion_text=json.dumps({"decision": "answer", "support": support}))


class StructuredMemoryFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ledger = ErasureLedger(root / "erasure.jsonl", "test", {"one": b"a" * 32}, "one")
        self.repo = MemoryRepository(root / "memory.sqlite3", ledger)
        self.repo.open(ledger.create())
        self.now = 1_730_000_000_000
        self.gateway = CognitiveMemoryGateway("fish", CaptureQueue(self.repo, clock=lambda: self.now))
        self.scope = ScopeRef("fish", ScopeKind.PRIVATE, "alice")

    def event(self, event_id, text, platform="qq"):
        provider = ProviderId(platform, f"{platform}-test")
        room = Conversation(provider, "bot", ChatKind.PRIVATE, "alice", "main")
        return MessageEvent(room, Account(provider, "alice"), event_id, self.now / 1000, text)

    async def test_capture_retries_and_recall_regenerates_after_revocation(self):
        content = "北岸气象站的气压表校验已经结束"
        row = claim(content, state="succeeded", entities=("北岸气象站",),
                    episode="气压表校验", occurred_at="2024-10-25")
        origin = self.event("evidence", content)
        authorized = lambda: True
        event_id = await self.gateway.capture_inbound(origin, "alice", authorized)
        self.assertEqual(event_id, await self.gateway.capture_inbound(origin, "alice", authorized))
        with self.repo.write() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM job").fetchone()[0], 1)

        async def unavailable(_):
            raise OSError("fixture failure")

        self.assertTrue(await self.gateway.extract_next(unavailable, lambda _: True))
        self.assertFalse(await self.gateway.extract_next(unavailable, lambda _: True))
        self.now += 3_000
        model = ProviderStub([row], ["北岸气象站气压表校验"])

        async def provider_for(_):
            return model

        self.assertTrue(await self.gateway.extract_next(provider_for, lambda _: True))
        self.assertFalse(await self.gateway.extract_next(provider_for, lambda _: True))
        inquiry = self.event("query", "那台仪器最终怎样了？", "lark")
        args = (inquiry, "alice", 2, 0, model)
        result, stamp = await self.gateway.semantic_snapshot(*args)
        self.assertEqual((result.decision, [row["body"] for row in result.support]), ("answer", [content]))
        other, _ = await self.gateway.semantic_snapshot(inquiry, "bob", 2, 0, model)
        self.assertEqual(other.support, ())

        guard = self.gateway.protected_reply(inquiry, "alice", lambda: ("alice", 2), lambda: 0,
                                             clock=lambda: self.now)
        sent = []
        snapshots = []

        async def build(_, *, allow_tools):
            decision, current = await self.gateway.semantic_snapshot(*args)
            snapshots.append(allow_tools)
            return decision, current

        async def generate(decision):
            if decision.support:
                self.repo.erase("source", self.scope, origin.event_id, 0, self.now)
                yield f"unsafe draft: {content}"
            else:
                yield "没有找到可用的校验记录。"

        async def send(text, consume):
            def submit():
                sent.append(text)
                return Delivery.SUBMITTED
            return consume(submit)

        verdict = await guard.run(inquiry, build, generate, send,
                                  initial_deadline=asyncio.get_running_loop().time() + 5)
        self.assertEqual(verdict, Delivery.SUBMITTED)
        self.assertEqual(sent, ["没有找到可用的校验记录。"])
        self.assertEqual(snapshots, [True, False])
        self.assertIsNotNone(stamp)

    async def test_expired_worker_cannot_publish_erased_source(self):
        body = "我计划检查风速仪"
        event = self.event("removed", body)
        await self.gateway.capture_inbound(event, "alice", lambda: True)
        job = self.repo.claim_extraction(self.now)
        claims = parse_claims(json.dumps({"claims": [claim(body, state="planned")]}), body)
        self.repo.erase("source", self.scope, event.event_id, 0, self.now)
        self.assertFalse(self.repo.apply_extraction(job, claims, self.now, authorize=lambda: True))
        self.assertEqual(self.repo.authorized_documents(self.gateway.audience(event, "alice", 1), self.now), ())

    async def test_wal_snapshot_overlapping_erase_is_discarded(self):
        text = "我负责北岸气象站的设备维护"
        origin = self.event("concurrent-source", text)
        await self.gateway.capture_inbound(origin, "alice", lambda: True)
        job = self.repo.claim_extraction(self.now)
        proposals = parse_claims(json.dumps({"claims": [claim(text)]}), text)
        self.assertTrue(self.repo.apply_extraction(job, proposals, self.now, authorize=lambda: True))
        audience = self.gateway.audience(origin, "alice", 1)
        self.assertEqual(self.repo.authorized_documents(audience, self.now)[0]["body"], text)
        finished_sql = Event()
        resume = Event()
        connect = self.repo._connect

        @contextmanager
        def pause_reader(*, read_only=False):
            with connect(read_only=read_only) as db:
                yield db
            if read_only:
                finished_sql.set()
                if not resume.wait(timeout=10):
                    raise RuntimeError("Timed out waiting for synthetic erasure")

        with patch.object(self.repo, "_connect", pause_reader):
            reader = asyncio.create_task(asyncio.to_thread(self.repo.authorized_documents, audience, self.now))
            try:
                self.assertTrue(await asyncio.to_thread(finished_sql.wait, 10))
                await asyncio.to_thread(self.repo.erase, "source", self.scope, origin.event_id, 0, self.now)
            finally:
                resume.set()
            with self.assertRaisesRegex(LedgerError, "overlapped"):
                await reader
        self.assertEqual(self.repo.authorized_documents(audience, self.now), ())

    def test_unverified_entity_and_actual_time_on_plan_are_rejected(self):
        body = "我们准备检查风速仪"
        row = claim(body, state="planned", entities=("气压表",))
        with self.assertRaisesRegex(ValueError, "source evidence"):
            parse_claims(json.dumps({"claims": [row]}), body)
        row.update(entities=["风速仪"], occurred_at="2024-10-25")
        with self.assertRaisesRegex(ValueError, "completed event"):
            parse_claims(json.dumps({"claims": [row]}), body)

