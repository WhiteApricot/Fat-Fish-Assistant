"""FFA-N01 evidence, authorization, migration and model boundary regressions."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fat_fish.communication.model import key
from fat_fish.memory.activity import Access, Activity, ActivityKind, ActivityState, Scope, Source
from fat_fish.memory.reasoner import analyze, analyze_batch, parse
from fat_fish.memory.service import MemoryService


def access(principal, platform, room=None, allowed=lambda: True):
    conversation = key(platform, platform + "-1", "bot", "group" if room else "private",
                       room or principal, room or principal)
    scope = Scope("group", conversation) if room else Scope("private", principal)
    return Access(principal, scope, Source(platform, platform + "-1", "bot", principal, conversation),
                  allowed, moderate=allowed, authenticated_chat=True)


def signal(**changes):
    return dict(type="social", field="banter", value=0.8, confidence=0.9, explicit=False) | changes


def fact(**changes):
    return dict(type="fact", operation="add", key="tone", text="偏好正式回答", importance=0.75, confidence=0.9) | changes


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "memory.sqlite3"
        self.now = 1_780_000_000
        self.memory = MemoryService(self.path, clock=lambda: self.now)
        self.social = self.memory.projections
        self.qq = access("principal-a", "qq")
        self.lark = access("principal-a", "lark")
        self.room = access("principal-a", "lark", "room-a")
        self.other = access("principal-b", "lark")

    def inbound(self, label, origin=None):
        origin = origin or self.qq
        event = Activity("in:" + label, ActivityKind.MESSAGE_RECEIVED, ActivityState.OBSERVED, self.now,
                         "来自该轮互动的私聊文本", "native-message:" + label)
        return self.memory.record(origin, event, turn_id=label, analyze=True)[0]

    def complete(self, ref, proposals, origin=None):
        origin = origin or self.qq
        lease, _, facts, _ = self.social.claim(origin, ref)
        return self.social.apply(origin, ref, lease, proposals, {item["key"]: item["revision"] for item in facts})

    def test_analysis_subcommands_are_registered_with_lookup_distinction(self):
        from fat_fish.commands.registry import CommandRegistry, ROUTES, is_lookup

        registry = CommandRegistry(dict.fromkeys(ROUTES))
        for name in ("memory", "groupmemory"):
            self.assertFalse(is_lookup(registry.proposal(name, ["analyze"])))
            self.assertTrue(is_lookup(registry.proposal(name, ["analysis-status"])))
            self.assertIsNone(registry.proposal(name, ["analyze", "other-scope"]))

    def test_write_lock_wait_is_bounded(self):
        self.memory.status()
        condition = threading.Event()
        release = threading.Event()

        def hold():
            with self.memory.transaction(write=True):
                condition.set()
                release.wait(3)

        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(condition.wait(2))
            started = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError):
                with self.memory.transaction(write=True, timeout=0.06):
                    self.fail("writer was acquired during contention")
            self.assertLess(time.monotonic()-started, 0.75)
        finally:
            release.set()
            thread.join()
        self.assertTrue(self.inbound("post-contention"))

    def test_experience_rank_scope_model_and_erasure(self):
        def publish(origin, ident, text, values):
            activity = Activity(ident, ActivityKind.READ, ActivityState.OBSERVED,
                                self.now, text, "verified:" + ident)
            ref, _ = self.memory.record(origin, activity, producer="fixture")
            self.social.publish_experience(origin, "feiyu", ident, ref, visibility="source")
            row = next(row for row in self.social.experience_index_rows("feiyu", "model-v1") if row["ref"] == ref)
            self.social.store_experience_vector("feiyu", "model-v1", row, values)
            return ref

        good = publish(self.qq, "rain", "冒着暴雨前往研究楼参加会议", [1, 0])
        publish(self.other, "unrelated", "陌生人的私人路线", [1, 0])
        publish(self.room, "group", "仅在本群记录的私人路线", [1, 0])
        publish(self.qq, "lexical", "降水 那趟是虚构的天气用例", [0, 1])
        recall = self.social.snapshot
        _, hits, _ = recall(self.lark, "feiyu", lambda *_: True, "降水时去了哪",
                            model="model-v1", vector=[1, 0])
        self.assertEqual(hits[0]["ref"], good)
        self.assertEqual(hits[0]["reason"], "semantic")
        self.assertEqual(len(hits), 2)
        for model, vector in (("model-v2", [1, 0]), (None, None)):
            self.assertFalse(any(item["ref"] == good for item in recall(
                self.lark, "feiyu", lambda *_: True, "降水时去了哪", model=model, vector=vector)[1]))
        self.assertEqual(recall(self.lark, "feiyu", lambda *_: False, "降水", model="model-v1", vector=[1, 0])[1], [])
        self.memory.reset_scope(self.qq)
        self.assertEqual(self.memory.status()["experience_index"], {"model-v1": 2})
        self.assertEqual(recall(self.lark, "feiyu", lambda *_: True, "降水", model="model-v1", vector=[1, 0])[1], [])

    def test_unreleased_running_job_recovers_upgrading_v16(self):
        ref = self.inbound("old-running")
        with self.memory.transaction(write=True) as db:
            db.execute("UPDATE memory_analysis SET state='running',released=NULL,lease='old' WHERE ref=?", (ref,))
            db.execute("DROP TRIGGER memory_experience_changed")
            db.execute("DROP TABLE memory_experience_vectors")
            db.execute("PRAGMA user_version=16")
        restored = MemoryService(self.path, clock=lambda: self.now)
        self.assertEqual(restored.projections.queue_status(self.qq), {"awaiting_reply": 1})
        self.now += 86401
        restored.projections.expire_unanswered()
        with restored.transaction() as db:
            self.assertEqual(tuple(db.execute("SELECT state,lease FROM memory_analysis").fetchone()),
                             ("discarded", None))

    def test_reply_gate_batches_idle_threshold_and_manual_scope(self):
        def queue(ident, source, *, reply):
            event = Activity("in:" + ident, ActivityKind.MESSAGE_RECEIVED, ActivityState.OBSERVED,
                             self.now, "一条需要分析的偏好")
            ref, _ = self.memory.record(source, event, turn_id=ident, analyze=True, after_reply=True)
            if reply:
                outbound = Activity("out:" + ident, ActivityKind.MESSAGE_SENT, ActivityState.OBSERVED,
                                    self.now, "收到", "native-send-return:" + ident)
                self.memory.record(source, outbound, turn_id=ident)
            return ref

        held = queue("held", self.qq, reply=False)
        refs = [queue("qq" + str(i), self.qq, reply=True) for i in range(3)]
        lark = queue("lark", self.lark, reply=True)
        self.assertEqual(self.social.ready_batches(), [])
        self.assertEqual(self.social.queue_status(self.qq), {"awaiting_reply": 1, "pending": 3})
        self.assertIsNone(self.social.claim(self.qq, held))
        self.assertEqual([r["ref"] for r in self.social.ready_batches(target=self.qq)[0]], refs)
        refs.append(queue("fourth", self.qq, reply=True))
        self.assertEqual([r["ref"] for r in self.social.ready_batches()[0]], refs)
        self.now += 121
        restarted = MemoryService(self.path, clock=lambda: self.now)
        self.assertEqual([[item["ref"] for item in batch] for batch in restarted.projections.ready_batches()],
                         [refs, [lark]])

    def test_group_expression_old_conflict_remains_visible(self):
        older = self.inbound("old-dispute", self.room)
        self.now += 86400
        other = access("principal-b", "lark", "room-a")
        second = self.inbound("second-author", other)
        self.now += 86400
        newer = self.inbound("new-author", self.room)
        with self.memory.transaction(write=True) as db:
            db.execute("INSERT INTO memory_group_expression VALUES (?,?,?)", (older, "青灯", "另一个解释"))
            db.executemany("INSERT INTO memory_group_expression VALUES (?,?,?)",
                           ((second, "青灯", "同意的意思"), (newer, "青灯", "同意的意思")))
            for i in range(82):
                previous = second if i % 2 else newer
                source = db.execute("SELECT * FROM activities WHERE ref=?", (previous,)).fetchone()
                ref = db.execute("""INSERT INTO activities
                    (producer,event_id,digest,scope,actor,source,kind,state,occurred,received,summary,evidence)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source["producer"], f"duplicate-{i}", source["digest"], source["scope"], source["actor"],
                     source["source"], source["kind"], source["state"], source["occurred"], source["received"],
                     source["summary"], source["evidence"])).lastrowid
                db.execute("INSERT INTO memory_group_expression VALUES (?,?,?)", (ref, "青灯", "同意的意思"))
        self.assertEqual(self.social.group_expressions(self.room, "青灯", lambda *_: True), [])
        self.memory.retract_message(self.room.scope, self.room.principal, "old-dispute", lambda: True)
        self.assertEqual(self.social.group_expressions(self.room, "青灯", lambda *_: True),
                         [{"expression": "青灯", "meaning": "同意的意思"}])

    def test_group_expression_requires_multiple_members_and_days_and_revokes(self):
        second = access("principal-b", "lark", "room-a")
        expression = dict(type="expression", expression="青灯",
                          meaning="发布前的集体验证", confidence=0.97)
        for ident, origin in (("first", self.room), ("second", second)):
            activity = Activity("in:" + ident, ActivityKind.MESSAGE_RECEIVED, ActivityState.OBSERVED,
                                self.now, "准备进行青灯验证", "native-message:" + ident)
            ref, _ = self.memory.record(origin, activity, turn_id=ident, analyze=True)
            self.complete(ref, [expression], origin)
            self.now += 86401
        lookup = self.social.group_expressions
        self.assertEqual(lookup(self.room, "青灯", lambda *_: True),
                         [{"expression": "青灯", "meaning": "发布前的集体验证"}])
        self.assertEqual(lookup(self.room, "绿灯", lambda *_: True), [])
        self.assertEqual(lookup(access("principal-a", "qq", "room-a"), "青灯", lambda *_: True), [])
        self.memory.retract_message(second.scope, second.principal, "second", lambda: True)
        self.assertEqual(lookup(self.room, "青灯", lambda *_: True), [])

    def test_social_signals_are_typed_shared_and_revocable(self):
        for i, origin in enumerate((self.qq, self.lark, self.qq)):
            self.now += 86400
            ref = self.inbound(str(i), origin)
            self.assertTrue(self.complete(ref, [signal()], origin))
            profile, _, _ = self.social.snapshot(self.room, "agent", lambda *_: True, "你好")
            self.assertEqual("banter" in profile, i == 2)
        self.assertEqual(self.social.snapshot(self.other, "agent", lambda *_: True, "你好")[0], {})
        self.assertEqual(self.social.snapshot(self.room, "agent", lambda *_: False, "你好")[0], {})
        ref = self.inbound("correction")
        self.complete(ref, [signal(value=0.1, explicit=True)])
        self.assertEqual(self.social.snapshot(self.room, "agent", lambda *_: True, "你好")[0]["banter"], 0.1)
        self.memory.retract_message(self.qq.scope, self.qq.principal, "correction", lambda: True)
        self.assertAlmostEqual(self.social.snapshot(self.room, "agent", lambda *_: True, "你好")[0]["banter"], 0.8)

    def test_fact_correction_conflict_and_stale_revision(self):
        self.complete(self.inbound("first"), [fact()])
        original = self.memory.search(self.qq, "正式")[0]
        current_key = original["key"] if "key" in original else original["fact_key"]
        self.now += 1
        second = self.inbound("second")
        self.complete(second, [fact(operation="supersede", key=current_key, text="偏好随意回答")])
        self.assertEqual([row["revision"] for row in self.memory.fact_history(self.qq, current_key)], [2, 1])
        self.now += 1
        ref = self.inbound("conflicting")
        self.complete(ref, [fact(operation="conflict", key=current_key, text="")])
        with self.memory.transaction() as db:
            self.assertEqual(db.execute("SELECT fact_key FROM memory_analysis_conflict").fetchone()[0], current_key)
        self.assertEqual(len(self.memory.fact_history(self.qq, current_key)), 2)
        self.now += 1
        older = self.inbound("older")
        lease, _, candidates, _ = self.social.claim(self.qq, older)
        self.now += 1
        newer = self.inbound("newer")
        self.complete(newer, [fact(operation="update", key=current_key, text="偏好标准回答")])
        self.social.apply(self.qq, older, lease, [fact(operation="update", key=current_key, text="偏好简短")],
                          {item["key"]: item["revision"] for item in candidates})
        self.assertEqual(self.memory.fact_history(self.qq, current_key)[0]["content"], "偏好标准回答")
        self.memory.retract_message(self.qq.scope, self.qq.principal, "second", lambda: True)
        self.assertEqual(self.memory.search(self.qq, "随意"), [])

    def test_reasoner_candidates_require_current_source_authorization(self):
        self.complete(self.inbound("preference"), [fact()])
        ref = self.inbound("other-platform", self.lark)
        lease, _, candidates, _ = self.social.claim(self.lark, ref, lambda *_: False)
        self.assertEqual(candidates, [])
        self.social.retry(ref, lease, "test retry")
        self.now += 100
        _, _, candidates, _ = self.social.claim(self.lark, ref, lambda *_: True)
        self.assertEqual([candidate["content"] for candidate in candidates], ["偏好正式回答"])

    def test_retraction_and_retry_survive_restart(self):
        ref = self.inbound("queue")
        lease, *_ = self.social.claim(self.qq, ref)
        self.social.retry(ref, lease, "TimeoutError")
        self.memory = MemoryService(self.path, clock=lambda: self.now)
        self.social = self.memory.projections
        self.assertEqual(self.social.pending(), [])
        self.now += 61
        self.assertEqual(self.social.pending()[0]["ref"], ref)
        new_lease, *_ = self.social.claim(self.qq, ref)
        self.assertNotEqual(lease, new_lease)
        self.assertFalse(self.social.apply(self.qq, ref, lease, [signal()], {}))
        self.memory.retract_message(self.qq.scope, self.qq.principal, "queue", lambda: True)
        self.assertFalse(self.social.apply(self.qq, ref, new_lease, [signal()], {}))

    def test_public_experience_cross_platform_and_restore_erasure(self):
        activity = Activity("receipt", ActivityKind.READ, ActivityState.OBSERVED, self.now,
                            "看过公开的 Minecraft 教程", "verified-public-receipt")
        ref = self.memory.record(self.qq, activity, producer="plugin:reader")[0]
        with self.assertRaises(PermissionError):
            self.social.publish_experience(self.qq, "agent", "read:1", ref, visibility="public")
        opts = dict(visibility="public", verify_public=lambda row: row["evidence"] == "verified-public-receipt")
        self.assertTrue(self.social.publish_experience(self.qq, "agent", "read:1", ref, **opts))
        self.assertFalse(self.social.publish_experience(self.qq, "agent", "read:1", ref, **opts))
        self.assertEqual(len(self.social.snapshot(self.room, "agent", lambda *_: True, "Minecraft")[1]), 1)
        self.assertEqual(len(self.social.snapshot(self.lark, "agent", lambda *_: True, "Minecraft")[1]), 1)
        self.assertFalse(self.social.snapshot(self.other, "agent", lambda *_: False, "Minecraft")[1])
        lease, _, _, published = self.social.claim(self.qq, ref)
        self.assertTrue(published)
        theme = dict(type="theme", kind="interest", label="Minecraft", status="hypothesis", confidence=0.9)
        self.social.apply(self.qq, ref, lease, [theme], {})
        self.assertEqual(self.social.snapshot(self.room, "agent", lambda *_: True, "Minecraft")[2][0]["theme"],
                         "Minecraft")
        snapshot = self.path.with_name("snapshot.sqlite3")
        with closing(sqlite3.connect(self.path)) as original, closing(sqlite3.connect(snapshot)) as backup:
            original.backup(backup)
        self.memory.reset_scope(self.qq)
        recovered = MemoryService(snapshot).projections
        recovered.service.reapply_erasures(self.memory.export_erasures())
        self.assertEqual(recovered.snapshot(self.room, "agent", lambda *_: True, "Minecraft"), ({}, [], []))

    def test_private_tasks_and_group_facts_never_promote(self):
        group_ref = self.inbound("group", self.room)
        self.complete(group_ref, [fact(), signal()], self.room)
        self.assertEqual(self.memory.search(self.room, "正式"), [])
        read = Activity("private-read", ActivityKind.READ, ActivityState.OBSERVED, self.now,
                        "已读取私有文件", "internal-receipt")
        ref = self.memory.record(self.qq, read, producer="plugin:reader")[0]
        self.social.publish_experience(self.qq, "agent", "read:private", ref, visibility="source")
        self.assertFalse(self.social.snapshot(self.room, "agent", lambda *_: True, "私有")[1])
        self.assertFalse(self.social.snapshot(self.other, "agent", lambda *_: True, "私有")[1])
        self.assertTrue(self.social.snapshot(self.lark, "agent", lambda *_: True, "私有")[1])

    def test_external_receipt_uses_chat_authority_without_rewriting_provenance(self):
        external = Source("bilibili", "fixture", "external-bot", "external-bot", "public-video")
        activity = Activity("read-external", ActivityKind.READ, ActivityState.OBSERVED, self.now,
                            "公开 Minecraft 教程", "verified-public")
        ref, created = self.memory.record(
            self.qq, activity, producer="plugin:reader", source=external,
            experience=("agent", "public", lambda row: row["evidence"] == "verified-public"),
        )
        self.assertTrue(created)
        job = self.social.pending()[0]
        self.assertEqual(json.loads(job["source"])["platform"], "qq")
        self.assertEqual(self.memory.history(self.qq)[0]["source"]["platform"], "bilibili")
        self.assertTrue(self.social.claim(self.qq, ref)[3])
        self.assertEqual(len(self.social.snapshot(self.lark, "agent", lambda *_: True, "Minecraft")[1]), 1)

    def test_cancelled_operation_cannot_be_published_as_completed_experience(self):
        operation = "fixture-task"
        for state, evidence in ((ActivityState.PLANNED, ""), (ActivityState.CANCELED, "cancel-receipt")):
            activity = Activity(state.value, ActivityKind.TASK, state, self.now, "任务状态", evidence,
                                operation, "stable-intent")
            ref = self.memory.record(self.qq, activity, producer="plugin:tasks")[0]
        self.assertEqual(self.memory.operation(self.qq, "plugin:tasks", operation)["state"], "canceled")
        with self.assertRaises(PermissionError):
            self.social.publish_experience(self.qq, "agent", "cancelled", ref, visibility="source")

    def test_capacity_levels_follow_rendered_size_and_preserve_pins(self):
        from fat_fish.memory import card

        inbound = Activity("in:capacity", ActivityKind.MESSAGE_RECEIVED, ActivityState.OBSERVED, self.now,
                           "记录一份明确的重要约定。" * 35, "capacity-fixture")
        self.memory.record(self.qq, inbound, turn_id="capacity", card_capture=(True, True))
        scope = self.qq.scope.storage_key
        with self.memory.transaction(write=True) as db:
            card.compact(db, scope, 100_000)
            size = db.execute("SELECT before_chars FROM memory_capacity_status WHERE scope=?", (scope,)).fetchone()[0]
            for multiplier, expected in ((1.2, 80), (1.08, 90), (0.8, 100)):
                card.compact(db, scope, round(size * multiplier))
                actual = db.execute("SELECT level FROM memory_capacity_status WHERE scope=?", (scope,)).fetchone()[0]
                self.assertEqual(actual, expected)
            self.assertEqual(db.execute("SELECT clarity FROM memory_card WHERE first_meet=1").fetchone()[0], 4)

    def test_semantic_card_merge_keeps_original_evidence_and_respects_retraction(self):
        def capture(ident, text, origin=self.room):
            activity = Activity("in:" + ident, ActivityKind.MESSAGE_RECEIVED, ActivityState.OBSERVED,
                                self.now, text, "native-message:" + ident)
            return self.memory.record(origin, activity, turn_id=ident, card_capture=(False, True), analyze=True)[0]

        prior = capture("first-browse", "昨天下午我们一起看了公开的组装教程。")
        self.complete(prior, [], self.room)
        outside = access("principal-a", "qq", "room-a")
        unrelated = capture("other-group", "昨天一起看了组装教程。", outside)
        self.complete(unrelated, [], outside)
        self.now += 1
        ref = capture("duplicate-browse", "说起昨天我们一起看的那份组装教程。")
        candidates = self.social.card_candidates(self.room, ref)
        self.assertEqual([item[0] for item in candidates.values()], [prior])
        ident = next(iter(candidates))
        lease, _, _, _ = self.social.claim(self.room, ref)
        proposal = {"type": "card_merge", "card_id": ident, "confidence": 0.96}
        self.assertTrue(self.social.apply(self.room, ref, lease, [proposal], {}, candidates))
        with self.memory.transaction() as db:
            merged = db.execute("SELECT id,pinned FROM memory_card WHERE kind='merged'").fetchone()
            self.assertFalse(merged['pinned'])
            self.assertEqual([row[0] for row in db.execute(
                "SELECT ref FROM memory_card_sources WHERE card=? ORDER BY ref", (merged['id'],))], [prior, ref])
        self.memory.retract_message(self.room.scope, self.room.principal, "duplicate-browse", lambda: True)
        with self.memory.transaction() as db:
            self.assertFalse(db.execute("SELECT 1 FROM memory_card WHERE kind='merged'").fetchone())
            self.assertTrue(db.execute("SELECT 1 FROM memory_card_archive WHERE card=?", (ident,)).fetchone())
            self.assertEqual(db.execute("SELECT hidden FROM memory_card WHERE id=?", (ident,)).fetchone()[0], 0)

    def test_migration_preserves_existing_data_and_is_idempotent(self):
        from fat_fish.memory.semantic import cache

        ref = self.inbound("historical")
        self.memory.put_fact(self.qq, "historical-note", "第一版事实", 0.7, ref)
        revision = self.inbound("revision")
        self.memory.put_fact(self.qq, "historical-note", "第二版事实", 0.8, revision, expected_revision=1)
        cache(self.memory, self.qq, "fixture-space", {("historical-note", 2): [1.0, 0.0]})
        deleted = self.inbound("retracted")
        self.memory.retract_message(self.qq.scope, self.qq.principal, "retracted", lambda: True)
        ledger = self.memory.export_erasures()
        self.assertTrue(ledger)
        with self.memory.transaction(write=True) as db:
            db.execute("DROP TRIGGER memory_experience_changed")
            db.execute("DROP TABLE memory_experience_vectors")
            for name in ("memory_projection_erasure", "memory_social_removed", "memory_agent_removed",
                         "memory_unmerge_surviving_evidence"):
                db.execute(f'DROP TRIGGER "{name}"')
            for name in ("memory_analysis", "memory_social_signal", "memory_agent_theme",
                         "memory_agent_experience", "memory_analysis_conflict", "memory_projection_epoch",
                         "memory_capacity_status"):
                db.execute(f'DROP TABLE "{name}"')
            db.execute("DROP TRIGGER memory_group_expression_erasure")
            db.execute("DROP TABLE memory_group_expression")
            db.execute("DROP TABLE memory_analysis_usage")
            db.execute("PRAGMA user_version=14")
        for _ in range(2):
            reopened = MemoryService(self.path)
            with reopened.transaction() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 17)
                self.assertEqual(db.execute("SELECT ref FROM activities ORDER BY ref").fetchone()[0], ref)
                self.assertEqual(db.execute("SELECT redacted FROM activities WHERE ref=?", (deleted,)).fetchone()[0], 1)
                self.assertFalse(db.execute("SELECT 1 FROM memory_analysis").fetchone())
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertFalse(db.execute("PRAGMA foreign_key_check").fetchall())
                self.assertEqual(db.execute("SELECT count(*) FROM memory_card_sources").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT count(*) FROM fact_vectors").fetchone()[0], 1)
            self.assertEqual(reopened.export_erasures(), ledger)
            self.assertEqual([item['revision'] for item in reopened.fact_history(self.qq, 'historical-note')], [2, 1])


class ReasonerTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_interface_returns_only_allowed_high_confidence_proposals(self):
        raw = dict(version=1, source_ref=42, proposals=[signal(), signal(confidence=0.1)])
        provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps(raw))))
        self.assertEqual(await analyze(provider, 42, "少一点正式措辞", []), [signal()])
        self.assertIsNone(provider.text_chat.await_args.kwargs["func_tool"])
        for invalid in (raw | {"scope": "public"}, raw | {"source_ref": 5},
                        raw | {"proposals": [signal(field="political_view")]},
                        raw | {"proposals": [signal(value=True)]},
                        raw | {"proposals": [dict(type="theme", kind="goal", label="外发", status="done", confidence=1)]}):
            with self.assertRaises(ValueError):
                parse(json.dumps(invalid), 42)

    async def test_batch_calls_model_once_and_rejects_duplicate_source(self):
        payload = [{"source_ref": ref, "text": "样本文字"} for ref in (41, 42)]
        results = [{"version": 1, "source_ref": ref, "proposals": []} for ref in (41, 42)]
        provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(
            completion_text=json.dumps({"version": 2, "results": results}))))
        meter = AsyncMock()
        self.assertEqual(await analyze_batch(provider, payload, meter=meter), {41: [], 42: []})
        provider.text_chat.assert_awaited_once()
        self.assertEqual(meter.await_args.args[0], 2)
        results[0]["proposals"] = [signal() | {"evidence_quote": "来自另一条消息"}]
        provider.text_chat.return_value.completion_text = json.dumps({"version": 2, "results": results})
        with self.assertRaises(ValueError):
            await analyze_batch(provider, payload)
        results[0]["proposals"][0]["evidence_quote"] = "样本文字"
        provider.text_chat.return_value.completion_text = json.dumps({"version": 2, "results": results})
        self.assertEqual((await analyze_batch(provider, payload))[41], [signal()])
        results[1]["source_ref"] = 41
        provider.text_chat.return_value.completion_text = json.dumps({"version": 2, "results": results})
        with self.assertRaises(ValueError):
            await analyze_batch(provider, payload)

    async def test_unavailable_model_propagates_for_retry(self):
        provider = SimpleNamespace(text_chat=AsyncMock(side_effect=TimeoutError))
        with self.assertRaises(TimeoutError):
            await analyze(provider, 42, "普通输入", [])

class GatewayAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_admitted_qq_turn_is_analyzed_after_return_and_recalled_by_bound_lark_account(self):
        from fat_fish.communication.model import Account, ProviderId
        from fat_fish.adapters.astrbot_communication import convert
        from fat_fish.gateway.platform_gateway import PlatformGateway
        from test_communication import At, NativeEvent, QQ, VerifiedProvider

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        private = Path(folder.name)
        (private / "ffa-memory.json").write_text('{"embedding_space":null}', encoding="utf-8")
        gateway = PlatformGateway(private=private)
        runtime = gateway.communication()
        bridge = gateway.memory_bridge
        lark = ProviderId("lark", "office")
        for address in (QQ, lark):
            runtime.register(VerifiedProvider(address))
            await runtime.enable(address)
        self.addAsyncCleanup(runtime.stop_all)
        self.addAsyncCleanup(bridge.stop_maintenance)
        owner = Account(QQ, "31")
        runtime.identities.confirm_owner_challenge(owner, runtime.identities.issue_owner_challenge(owner))
        runtime.identities.confirm_platform_link(Account(lark, "32"), runtime.identities.issue_platform_link(owner))

        async def complete(**kwargs):
            data = json.loads(kwargs["prompt"])
            return SimpleNamespace(completion_text=json.dumps(
                dict(version=1, source_ref=data["source_ref"], proposals=[fact(), signal(field="formality", explicit=True), signal(explicit=True)])))

        model = SimpleNamespace(text_chat=AsyncMock(side_effect=complete))
        gateway.context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=model))
        event = NativeEvent("aiocqhttp", QQ.instance, sender="31", ident="n01-input",
                            text="请以后和我用正式的表达方式")
        bridge.memory().status()
        event.message_obj.timestamp = bridge.memory().clock()

        async def reply():
            await event.send("收到")

        await gateway.run(event, reply)
        model.text_chat.assert_not_awaited()
        self.assertEqual(event.sent, ["收到"])
        await bridge._capture_queue.join()
        await bridge._run_analysis(bridge.memory(), target=event.get_extra("_ffa_memory_access"))
        self.assertEqual(model.text_chat.await_count, 1, bridge.memory().status()["analysis"])

        remote = NativeEvent("lark", lark.instance, sender="32", ident="n01-lark", text="什么是正式回答")

        async def recall():
            request = SimpleNamespace(contexts=[], prompt=remote.message_str, system_prompt="")
            await bridge.augment(remote, request)
            remote.request = request

        await gateway.run(remote, recall)
        remote_access = remote.get_extra("_ffa_memory_access")
        self.assertEqual(bridge.memory().search(remote_access, "正式")[0]["content"], "偏好正式回答")
        self.assertIn("interaction_preferences", remote.request.prompt)

        group = NativeEvent("aiocqhttp", QQ.instance, sender="31", group="701", ident="n01-group",
                            text="群内普通提问", parts=[At("900")])
        runtime.identities.set_room(owner, convert(group).conversation, "mention")

        async def recall_group():
            request = SimpleNamespace(contexts=[], prompt=group.message_str, system_prompt="")
            await bridge.augment(group, request)
            group.request = request

        await gateway.run(group, recall_group)
        self.assertIsNone(group.get_extra("_ffa_memory_access"))
        self.assertIn('"formality":0.8', group.request.prompt)
        self.assertIn('"banter_when_invited":0.8', group.request.prompt)
        self.assertNotIn('"banter":', group.request.prompt)
        self.assertIn("正常提问、严肃讨论、拒绝或意思不明时按普通语气回答", group.request.system_prompt)
        self.assertNotIn("偏好正式回答", group.request.prompt)
