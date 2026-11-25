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
from fat_fish.memory.structured_memory import parse_claims, parse_observations


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

    async def test_independent_evidence_survives_one_source_erasure(self):
        text = "我目前负责实验室仪表维护"
        rows = []
        source_keys = []
        for native_id, platform in (("src-qq", "qq"), ("src-lark", "lark")):
            self.now += 10
            incoming = self.event(native_id, text, platform)
            source_keys.append(incoming.event_id)
            await self.gateway.capture_inbound(incoming, "alice", lambda: True)
            job = self.repo.claim_extraction(self.now)
            claims = parse_claims(json.dumps({"claims": [claim(text)]}), text)
            self.assertTrue(self.repo.apply_extraction(job, claims, self.now, authorize=lambda: True))
            audience = self.gateway.audience(incoming, "alice", 1)
            rows.append(self.repo.authorized_documents(audience, self.now)[0])
        self.assertEqual(rows[0]["id"], rows[1]["id"])
        self.assertGreater(rows[1]["rev"], rows[0]["rev"])
        historical = self.repo.authorized_documents(audience, self.now, known_at=rows[0]["known_from"])
        self.assertEqual(historical[0]["rev"], rows[0]["rev"])
        self.now += 10
        self.repo.erase("source", self.scope, source_keys[0], 0, self.now)
        survivors = self.repo.authorized_documents(audience, self.now)
        self.assertEqual([row["body"] for row in survivors], [text])
        self.assertGreater(survivors[0]["rev"], rows[1]["rev"])
        self.assertEqual(self.repo.authorized_documents(audience, self.now, known_at=rows[0]["known_from"]), ())
        self.repo.erase("source", self.scope, source_keys[1], 0, self.now)
        self.assertEqual(self.repo.authorized_documents(audience, self.now), ())

    async def test_semantic_equivalence_is_scoped_to_current_candidate_revision(self):
        first = "我负责北岸气象站的仪表维护"
        second = "北岸气象站的仪表保养由我负责"
        for index, text in enumerate((first, second)):
            self.now += 10
            incoming = self.event(f"equivalent-{index}", text)
            await self.gateway.capture_inbound(incoming, "alice", lambda: True)
            job = self.repo.claim_extraction(self.now)
            candidates = self.repo.extraction_candidates(job, self.now)
            row = claim(text)
            if index:
                self.assertEqual([candidate["body"] for candidate in candidates], [first])
                row["equivalent_to"] = candidates[0]["id"]
                with self.assertRaisesRegex(ValueError, "outside"):
                    parse_claims(json.dumps({"claims": [row]}), text)
            proposals = parse_claims(json.dumps({"claims": [row]}), text, {r["id"] for r in candidates})
            self.assertTrue(self.repo.apply_extraction(job, proposals, self.now, authorize=lambda: True,
                                                       candidates=candidates))
        audience = self.gateway.audience(incoming, "alice", 1)
        self.assertEqual([row["body"] for row in self.repo.authorized_documents(audience, self.now)], [first])

    async def test_model_links_explicit_alias_and_episode_across_distinct_claims(self):
        source_texts = (("北岸气象站的春季仪器复核已经开始", "北岸气象站", "春季仪器复核"),
                        ("北岸站的春季仪器复核记录已经提交", "北岸站", "春季仪器复核"),
                        ("北岸气象站的秋季仪器复核已经开始", "北岸气象站", "秋季仪器复核"))
        expected_ids = []
        for index, (body, entity, episode) in enumerate(source_texts):
            self.now += 10
            event = self.event(f"graph-{index}", body)
            await self.gateway.capture_inbound(event, "alice", lambda: True)
            job = self.repo.claim_extraction(self.now)
            candidates = self.repo.extraction_candidates(job, self.now)
            row = claim(body, entities=(entity,), episode=episode, state="running")
            if index == 1:
                row["links"] = [
                    {"kind": "entity", "label": entity, "target": expected_ids[0],
                     "target_label": "北岸气象站"},
                    {"kind": "episode", "label": episode, "target": expected_ids[0],
                     "target_label": episode},
                ]
            parsed = parse_claims(json.dumps({"claims": [row]}), body, {r["id"] for r in candidates})
            self.assertTrue(self.repo.apply_extraction(job, parsed, self.now, candidates=candidates,
                                                       authorize=lambda: True))
            audience = self.gateway.audience(event, "alice", 1)
            expected_ids.append(self.repo.authorized_documents(audience, self.now)[0]["id"])
        self.assertEqual([row["id"] for row in self.repo.graph_candidates(
            audience, self.now, (expected_ids[0],))], [expected_ids[1]])
        stranger = self.gateway.audience(event, "bob", 1)
        self.assertEqual(self.repo.graph_candidates(stranger, self.now, (expected_ids[0],)), ())

    async def test_completed_turn_rolls_back_if_an_input_was_withdrawn(self):
        first = self.event("turn-first", "先记录仪表")
        second = self.event("turn-withdrawn", "递交仪表记录")
        self.repo.erase("source", self.scope, second.event_id, 0, self.now)
        with self.assertRaises(PermissionError):
            await self.gateway.capture_completed_turn((first, second), "alice", lambda: True)
        with self.repo.write() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM job").fetchone()[0], 0)
        third = self.event("turn-next", "补充复核结果")
        captured = await self.gateway.capture_completed_turn((first, third), "alice", lambda: True)
        self.assertEqual(captured, await self.gateway.capture_completed_turn((first, third), "alice", lambda: True))
        with self.repo.write() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM job").fetchone()[0], 2)

    async def test_observation_tracks_refutation_and_recomputes_after_withdrawal(self):
        candidates_by_body = {}
        origins = []
        for index, text in enumerate(("我常在周一复核仪表", "上周一我复核了仪表",
                                      "我通常在周五才复核仪表", "这周一我也复核了仪表")):
            self.now += 10
            event = self.event(f"observation-{index}", text)
            origins.append(event.event_id)
            await self.gateway.capture_inbound(event, "alice", lambda: True)
            job = self.repo.claim_extraction(self.now)
            audience = self.gateway.audience(event, "alice", 1)
            claims = parse_claims(json.dumps({"claims":[claim(text)]}), text)
            candidates = self.repo.extraction_candidates(job, self.now)
            observations = ()
            if index == 3:
                observation = {"axis":"behavior", "key":"monday_check",
                               "body":"有在周一复核仪表的重复行为。", "quote":text,
                               "support":[candidates_by_body["我常在周一复核仪表"],
                                          candidates_by_body["上周一我复核了仪表"]],
                               "refute":[candidates_by_body["我通常在周五才复核仪表"]]}
                encoded = json.dumps({"observations":[observation]})
                observations = parse_observations("```json\n"+encoded+"\n```",
                                                  text, {r["id"] for r in candidates})
                self.assertEqual(observations[0].key, "monday-check")
            self.assertTrue(self.repo.apply_extraction(job, claims, self.now, authorize=lambda: True,
                                                       candidates=candidates, observations=observations))
            candidates_by_body.update((r["body"], r["id"]) for r in self.repo.authorized_documents(audience, self.now))
        def current_observations():
            return [r for r in self.repo.authorized_documents(audience, self.now) if r["kind"] == "observation"]
        row, = current_observations()
        self.assertEqual(row["trust_class"], "inference")
        self.assertAlmostEqual(row["confidence"], .6667)
        self.repo.erase("source", self.scope, origins[0], 0, self.now)
        updated, = current_observations()
        self.assertGreater(updated["rev"], row["rev"])
        self.assertAlmostEqual(updated["confidence"], .6)
        self.repo.erase("source", self.scope, origins[1], 0, self.now)
        self.assertEqual(current_observations(), [])

    def test_graph_schema_upgrade_preserves_existing_repository_identity(self):
        trusted = self.repo.ledger.read()[1]
        with self.repo.write() as db:
            original = self.repo._meta(db, "db_uuid")
            db.execute("DROP TABLE memory_mention")
            db.execute("DROP TABLE memory_node")
            db.execute("PRAGMA user_version=1")
        self.repo.open(trusted)
        with self.repo.write() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(self.repo._meta(db, "db_uuid"), original)
            self.assertEqual(db.execute("SELECT count(*) FROM memory_node").fetchone()[0], 0)

    def test_unverified_entity_and_actual_time_on_plan_are_rejected(self):
        body = "我们准备检查风速仪"
        row = claim(body, state="planned", entities=("气压表",))
        with self.assertRaisesRegex(ValueError, "source evidence"):
            parse_claims(json.dumps({"claims": [row]}), body)
        row.update(entities=["风速仪"], occurred_at="2024-10-25")
        with self.assertRaisesRegex(ValueError, "completed event"):
            parse_claims(json.dumps({"claims": [row]}), body)

