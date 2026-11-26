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

    async def test_erasure_between_candidate_recheck_and_selection_stops_dispatch(self):
        body = "北岸气象站的气压表已完成校验"
        origin = self.event("calibration", body)
        query = self.event("query-race", "气压表校验完成了吗？")
        await self.gateway.capture_inbound(origin, "alice", lambda: True)

        class Provider(ProviderStub):
            selection_calls = 0

            async def text_chat(self, *, prompt, **kwargs):
                if "records" in json.loads(prompt):
                    self.selection_calls += 1
                return await super().text_chat(prompt=prompt, **kwargs)

        provider = Provider([claim(body)], ["北岸气象站气压表校验"])

        async def choose(_):
            return provider

        self.assertTrue(await self.gateway.extract_next(choose, lambda _: True))
        from fat_fish.memory.semantic_query import select

        async def erase_before_selection(*args, **kwargs):
            self.repo.erase("source", self.scope, origin.event_id, 0, self.now)
            return await select(*args, **kwargs)

        with patch("fat_fish.memory.cognitive_gateway.select", side_effect=erase_before_selection) as selection:
            result, stamp = await self.gateway.semantic_snapshot(query, "alice", 1, 0, provider)
        selection.assert_awaited_once()
        self.assertEqual(result.decision, "abstain")
        self.assertEqual(stamp.dependencies, ())
        self.assertEqual(provider.selection_calls, 0)

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
        body, entity, episode = source_texts[1]
        supporting = self.event("unlinked-independent", body)
        await self.gateway.capture_inbound(supporting, "alice", lambda: True)
        job = self.repo.claim_extraction(self.now)
        independent = parse_claims(json.dumps({"claims": [
            claim(body, entities=(entity,), episode=episode, state="running")]}), body)
        self.repo.apply_extraction(job, independent, self.now, authorize=lambda: True)
        self.repo.erase("source", self.scope, self.event("graph-1", body).event_id, 0, self.now)
        self.assertTrue(self.repo.authorized_documents(audience, self.now, item_ids=(expected_ids[1],)))
        self.assertEqual(self.repo.graph_candidates(audience, self.now, (expected_ids[0],)), ())

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

    async def test_historical_query_uses_old_revision_and_erasure_blocks_its_stamp(self):
        from datetime import datetime, timezone
        from dataclasses import replace
        from fat_fish.memory.records import ItemRecord, EvidenceRecord

        origin = self.event("history-proof", "潮位传感器读数")
        await self.gateway.capture_inbound(origin, "alice", lambda: True)
        proof = self.repo.claim_extraction(self.now)
        record = ItemRecord(self.scope, "fish", "alice", "fact", "sensor", "main",
                            "潮位传感器读数蓝色", {}, (EvidenceRecord(proof["source_id"]),),
                            "direct_self", .9, .5)
        item, rev = self.repo.put_item(record, None, self.now)
        before = datetime.fromtimestamp(self.now / 1000, timezone.utc).isoformat()
        self.now += 100
        self.repo.put_item(replace(record, body="潮位传感器读数绿色"), rev, self.now)
        request = self.event("history-query", "早先记录的潮位传感器读数是什么")
        provider = ProviderStub([], [{"text": "潮位传感器读数", "valid_at": None, "known_at": before}])
        decision, stamp = await self.gateway.semantic_snapshot(request, "alice", 3, 0, provider)
        self.assertEqual([row["body"] for row in decision.support], ["潮位传感器读数蓝色"])
        guard = self.gateway.protected_reply(request, "alice", lambda: ("alice", 3), lambda: 0,
                                             clock=lambda: self.now)
        self.assertTrue(guard.validator(stamp))
        self.repo.erase("source", self.scope, origin.event_id, 0, self.now)
        self.assertFalse(guard.validator(stamp))

    async def test_effective_window_recall_uses_overlap_and_rechecks_same_bounds(self):
        from dataclasses import replace
        from datetime import datetime, timezone

        from fat_fish.memory.records import EvidenceRecord, ItemRecord

        origin = self.event("window-proof", "潮位传感器读数")
        await self.gateway.capture_inbound(origin, "alice", lambda: True)
        proof = self.repo.claim_extraction(self.now)
        start, end = self.now - 2_000, self.now - 1_000
        original = ItemRecord(self.scope, "fish", "alice", "fact", "sensor", "main",
                              "潮位传感器读数蓝色", {}, (EvidenceRecord(proof["source_id"]),),
                              "direct_self", .9, .5, valid_from=start, valid_to=end)
        _, revision = self.repo.put_item(original, None, self.now)
        self.now += 100
        self.repo.put_item(replace(original, body="潮位传感器读数绿色", valid_from=end,
                                   valid_to=None), revision, self.now)

        def iso(milliseconds):
            return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()

        query = self.event("window-query", "早先那一段的潮位传感器读数是什么")
        model = ProviderStub([], [{"text": "潮位传感器读数", "valid_from": iso(start),
                                   "valid_to": iso(end), "known_at": None}])
        decision, stamp = await self.gateway.semantic_snapshot(query, "alice", 3, 0, model)
        self.assertEqual([row["body"] for row in decision.support], [original.body])
        self.assertEqual(stamp.dependencies[0].valid_window, (start, end))
        guard = self.gateway.protected_reply(query, "alice", lambda: ("alice", 3), lambda: 0,
                                             clock=lambda: self.now)
        self.assertTrue(guard.validator(stamp))
        self.repo.erase("source", self.scope, origin.event_id, 0, self.now)
        self.assertFalse(guard.validator(stamp))

    def test_connector_receipts_share_public_content_without_sharing_user_or_work(self):
        from dataclasses import replace
        from fat_fish.memory.source_ingress import ContentAction, ContentExperience, SourceIngress, WorkPhase, WorkReceipt
        ingress = SourceIngress(self.repo, "fish")
        public = ContentExperience("bilibili", "clip-42", "https://video.example/clip-42", "fixture-publisher",
                                   self.now - 100, self.now, "v1", "仪器检修公开说明", ContentAction.RETRIEVED)
        attested = ingress.content(public, receipt="connector-only", verify=lambda _: True, now=self.now)
        self.assertEqual(attested, ingress.content(public, receipt="connector-only", verify=lambda _: True, now=self.now))
        with self.assertRaises(PermissionError):
            ingress.content(replace(public, revision="v2"), receipt="connector-only", verify=lambda _: True, now=self.now)
        with self.assertRaises(ValueError):
            ingress.content(public, receipt="unsafe", verify=lambda _: True, now=self.now, principal="alice")
        opened = ingress.content(replace(public, action=ContentAction.OPENED), receipt="own-click",
                                 verify=lambda _: True, now=self.now, principal="alice")
        work = WorkReceipt("work-7", "fixture-project", WorkPhase.TOOL_SUCCEEDED, self.now,
                           "工具完成局部诊断", ("artifact-sample",))
        with self.assertRaises(ValueError):
            ingress.work(replace(work, phase=WorkPhase.VERIFIED), principal="alice",
                         producer="local-executor", receipt="bare-verification", verify=lambda _: True, now=self.now)
        stages = [ingress.work(replace(work, phase=phase), principal="alice", producer="local-executor",
                               receipt=phase.value, verify=lambda _: True, now=self.now)
                  for phase in (WorkPhase.PLAN, WorkPhase.STARTED, WorkPhase.SUBMITTED)]
        pending = ingress.work(work, principal="alice", producer="local-executor", receipt="tool",
                               verify=lambda _: True, now=self.now)
        with self.assertRaises(ValueError):
            ingress.content(replace(public, url="https://127.0.0.1/private"), receipt="private-url",
                            verify=lambda _: True, now=self.now)
        event = self.event("source-recall", "回忆检修工作")
        alice = self.gateway.audience(event, "alice", 1)
        bob = self.gateway.audience(event, "bob", 1)
        self.assertEqual({row["id"] for row in self.repo.authorized_documents(bob, self.now)}, {attested.item_id})
        rows = {row["id"]:row for row in self.repo.authorized_documents(alice, self.now)}
        self.assertEqual(set(rows), {attested.item_id, opened.item_id, pending.item_id, *(p.item_id for p in stages)})
        self.assertEqual(json.loads(rows[opened.item_id]["value_json"])["action"], "opened")
        self.assertEqual(json.loads(rows[pending.item_id]["value_json"])["phase"], "tool_succeeded")
        self.assertEqual(self.repo.event_candidates(alice, self.now, phase="succeeded"), ())
        newer = ingress.content(replace(public, revision="v2"), receipt="second-version",
                                verify=lambda _: True, now=self.now)
        self.assertNotEqual(attested.item_id, newer.item_id)
        ingress.withdraw_public_content("bilibili", "clip-42", generation=0, verify=lambda: True, now=self.now)
        self.assertEqual(self.repo.authorized_documents(bob, self.now), ())
        with self.assertRaises(PermissionError):
            ingress.content(replace(public, revision="v3"), receipt="future-version",
                            verify=lambda _: True, now=self.now)

    async def test_joint_conclusion_requires_two_current_components(self):
        from dataclasses import replace
        from fat_fish.memory.records import EvidenceRecord, ItemRecord

        records = []
        for index, body in enumerate(("雾灯运抵北岸", "北岸雾灯完成安装")):
            origin = self.event(f"joint-{index}", body)
            await self.gateway.capture_inbound(origin, "alice", lambda: True)
            proof = self.repo.claim_extraction(self.now)
            record = ItemRecord(self.scope, "fish", "alice", "fact", str(index), "main",
                                body, {}, (EvidenceRecord(proof["source_id"]),), "direct_self", .9, .5)
            records.append((record, self.repo.put_item(record, None, self.now)))
        joined, _ = self.repo.publish_conjunction(self.scope, slot="installed-at-site", body="雾灯已在北岸安装到位",
                            parents=tuple(key for _, key in records), expected_revision=None,
                            now=self.now, authorize=lambda: True)
        audience = self.gateway.audience(origin, "alice", 1)
        self.assertEqual(self.repo.authorized_documents(audience, self.now, item_ids=(joined,))[0]["trust_class"],
                         "inference")
        prior, (_, revision) = records[1]
        self.now += 10
        self.repo.put_item(replace(prior, body="安装完成记录被纠正"), revision, self.now)
        self.assertEqual(self.repo.authorized_documents(audience, self.now, item_ids=(joined,)), ())

    async def test_opted_in_group_turn_commits_input_jobs_and_send_receipts_together(self):
        provider = ProviderId("qq", "group-fixture")
        room = Conversation(provider, "bot", ChatKind.GROUP, "shared-room", "main")
        sender = Account(provider, "alice")
        event = MessageEvent(room, sender, "native-message", self.now / 1000, "更新北岸气象站状态")
        group = self.gateway.audience(event, "alice", 1).conversation_scope
        with self.assertRaises(PermissionError):
            await self.gateway.capture_completed_turn((event,), "alice", lambda: True)
        self.repo.set_group_memory(group, True, authorize=lambda: True)
        first = await self.gateway.capture_completed_turn((event,), "alice", lambda: True,
                                                         deliveries=(("native-send", self.now),))
        self.assertEqual(first, await self.gateway.capture_completed_turn(
            (event,), "alice", lambda: True, deliveries=(("native-send", self.now),)))
        with self.repo.write() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM job").fetchone()[0], 1)
        job = self.repo.claim_extraction(self.now)
        claims = parse_claims(json.dumps({"claims":[claim(event.text)]}), event.text)
        self.assertTrue(self.repo.apply_extraction(job, claims, self.now, authorize=lambda: True))
        audience = self.gateway.audience(event, "alice", 1)
        self.assertEqual(len(self.repo.authorized_documents(audience, self.now)), 1)
        private = self.gateway.audience(self.event("private", event.text), "alice", 1)
        self.assertEqual(self.repo.authorized_documents(private, self.now), ())
        self.repo.set_group_memory(group, False, authorize=lambda: True)
        self.assertEqual(self.repo.authorized_documents(audience, self.now), ())

    async def test_controlled_worker_resumes_committed_job_after_restart(self):
        from fat_fish.memory.recovery import initialize_controls, open_controlled_repository

        root = self.repo.path.parent
        private = root / "instance" / "private"
        private.mkdir(parents=True)
        controls = root / "independent"
        initialize_controls(controls)
        database = private / "ffa.sqlite3"
        first, _ = open_controlled_repository(database, controls)
        first_gateway = CognitiveMemoryGateway("fish", CaptureQueue(first, clock=lambda: self.now))
        incoming = self.event("restarted", "北岸气象站持续维护传感器")
        await first_gateway.capture_completed_turn((incoming,), "alice", lambda: True,
                                                  deliveries=(("verified-send", self.now),))
        restarted, _ = open_controlled_repository(database, controls)
        active = CognitiveMemoryGateway("fish", CaptureQueue(restarted, clock=lambda: self.now))

        async def provider_for(_):
            return ProviderStub([claim(incoming.text)])

        ready = active.start_extraction_worker(provider_for, lambda job: job["author_principal"] == "alice")
        try:
            async with asyncio.timeout(5):
                await ready.wait()
                audience = active.audience(incoming, "alice", 1)
                while not restarted.authorized_documents(audience, self.now):
                    await asyncio.sleep(.01)
        finally:
            await active.stop_extraction_worker()
        self.assertFalse(restarted.extraction_due(self.now))

    async def test_graph_traversal_spans_two_attributable_hops(self):
        texts = ("北岸项目已建立测试项", "北岸项目使用示波仪测试", "示波仪完成校准")
        labels = (("北岸项目",), ("北岸项目", "示波仪"), ("示波仪",))
        starts = []
        for index, text in enumerate(texts):
            self.now += 1
            event = self.event(f"two-hop-{index}", text)
            await self.gateway.capture_inbound(event, "alice", lambda: True)
            job = self.repo.claim_extraction(self.now)
            candidates = self.repo.extraction_candidates(job, self.now)
            data = claim(text, entities=labels[index])
            if index:
                label = labels[index][0]
                data["links"] = [{"kind":"entity", "label":label,
                                  "target":starts[-1], "target_label":label}]
            proposals = parse_claims(json.dumps({"claims":[data]}), text, {r["id"] for r in candidates})
            self.assertTrue(self.repo.apply_extraction(job, proposals, self.now, candidates=candidates,
                                                       authorize=lambda: True))
            audience = self.gateway.audience(event, "alice", 1)
            starts.append(self.repo.authorized_documents(audience, self.now)[0]["id"])
        self.assertEqual([row["id"] for row in self.repo.graph_candidates(audience, self.now, starts[:1])],
                         [starts[1]])
        self.assertEqual([row["id"] for row in self.repo.graph_candidates(audience, self.now, starts[:1], hops=2)],
                         starts[1:])

    def test_graph_schema_upgrade_preserves_existing_repository_identity(self):
        trusted = self.repo.ledger.read()[1]
        with self.repo.write() as db:
            original = self.repo._meta(db, "db_uuid")
            db.execute("DROP TABLE evidence_dependency")
            db.execute("DROP TABLE memory_mention_proof")
            db.execute("DROP TABLE memory_mention")
            db.execute("DROP TABLE memory_node")
            db.execute("PRAGMA user_version=1")
        self.repo.open(trusted)
        with self.repo.write() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
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

