from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import asyncio
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_communication import At, ControlledProvider, NativeEvent, QQ
from fat_fish.agents.bili_whale import BiliWhaleObservations, MANIFEST, comment_result
from fat_fish.agents.output import OutputAuthorization
from fat_fish.agents.registry import AgentRegistry, ApprovalEvent, Manifest, Risk, Tool, ToolResult, WriteLimits
from fat_fish.adapters.astrbot_communication import convert
from fat_fish.communication.model import Account, ChatKind, Conversation, NotSubmitted, Outgoing, ProviderId, key
from fat_fish.core.identity import IdentityDirectory, Role
from fat_fish.memory.activity import Access, Activity, ActivityKind, ActivityState, Scope, Source
from fat_fish.memory.card import DAY, detect_emotion, score
from fat_fish.memory.embedding import LocalEmbeddingProvider
from fat_fish.memory.experience import native_history_read_only
from fat_fish.memory.semantic import IndexMaintenance, candidates, recall, reset
from fat_fish.memory.service import MemoryService, SCHEMA, parse_timezone

Message = SimpleNamespace

OBS = ActivityState.OBSERVED


class NarrativeCardTests(unittest.TestCase):
    def test_cross_scope_merge_requires_current_source_permission_and_keeps_group_card(self):
        with tempfile.TemporaryDirectory() as directory:
            service = MemoryService(Path(directory) / "memory.sqlite3")
            shared = True
            private = permit()
            group = permit(scope=Scope("group", "g"))
            access = replace(private, source_scope_allowed=lambda key: shared and key == group.scope.storage_key)
            for actor, text in ((private, "私聊看到海浪"), (group, "群里提到蓝鲸")):
                service.record(actor, Activity(text, ActivityKind.MESSAGE_RECEIVED, OBS, 100, text),
                               card_capture=(False, True))
            first = service.card_records(private)[0]["id"]
            second = service.card_records(group)[0]["id"]
            with self.assertRaises(PermissionError):
                service.edit_card(private, "merge", [first, second])
            result = service.edit_card(access, "merge", [first, second], "海边经历")
            self.assertEqual(service.card_records(group)[0]["id"], second)
            self.assertEqual(len(next(row for row in service.card_records(access) if row["id"] == result)["events"]), 2)
            shared = False
            self.assertNotIn(result, [row["id"] for row in service.card_records(access)])
            self.assertNotIn("蓝鲸", service.context(access, "海边经历"))
            shared = True
            service.edit_card(access, "erase-card", [result])
            with self.assertRaises(PermissionError):
                service.edit_card(access, "merge", [first, second], "海边经历")
            self.assertEqual(service.card_records(group)[0]["id"], second)

    def test_backfill_only_uuid_rows_and_never_recreates_erased_card(self):
        from uuid import uuid4

        with tempfile.TemporaryDirectory() as directory:
            service = MemoryService(Path(directory) / "memory.sqlite3")
            access = permit(str(uuid4()))
            for actor in (access, permit("owner")):
                for kind, text in ((ActivityKind.MESSAGE_RECEIVED, "我们一起看到了蓝鲸"),
                                   (ActivityKind.MESSAGE_SENT, "我会记得那头蓝鲸")):
                    service.record(actor, Activity(actor.principal + kind.value, kind, OBS, 100, text),
                                   turn_id="whale")
            service.backfill_cards(access.principal, allowed, limit=2)
            service.backfill_cards(access.principal, allowed, limit=2)
            records = service.card_records(access, "蓝鲸")
            self.assertEqual(len(records), 1)
            self.assertEqual(len(records[0]["events"]), 2)
            self.assertEqual(service.card_records(permit("owner")), [])
            service.edit_card(access, "erase-card", [records[0]["id"]])
            self.assertEqual(service.backfill_cards(access.principal, allowed), 0)
            self.assertFalse(service.card_records(access, "蓝鲸"))

    def test_scoring_matches_reference_signals(self):
        self.assertEqual(detect_emotion("委屈！！！"), 12)
        self.assertEqual(detect_emotion("喜欢喜欢喜欢！！！"), 24)
        self.assertEqual(score("第一次见面，我很开心！", owner=True, private=True, direct=True)[:4],
                         (100, 4, "first", True))

    def test_archive_decay_merge_review_and_erasure(self):
        with tempfile.TemporaryDirectory() as directory:
            now = 100 * DAY
            service = MemoryService(Path(directory) / "memory.sqlite3", clock=lambda: now)
            access = permit()

            def capture(ident, body, *, owner=False, occurred=1):
                return service.record(access, Activity(ident, ActivityKind.MESSAGE_RECEIVED, OBS, occurred, body),
                                      turn_id=ident, card_capture=(owner, True))[0]

            ref = capture("one", "相遇那天看到蓝鲸", owner=True)
            capture("two", "后来一起观察海豚", owner=True, occurred=2)
            capture("three", "普通互动观察三号", occurred=3)
            capture("four", "普通互动观察四号", occurred=4)
            active = service.card_records(access)
            meeting = next(row for row in active if row["section"] == "初次相遇")
            second = next(row for row in active if "海豚" in row["events"][0]["text"])
            self.assertEqual(meeting["events"][0]["text"], "相遇那天看到蓝鲸")
            merged = service.edit_card(access, "meet", [meeting["id"], second["id"]], "第一次看海")
            self.assertEqual(len(next(row for row in service.card_records(access) if row["id"] == merged)["events"]), 2)
            self.assertEqual(len(service.card_records(access, archived=True)), 5)
            service.edit_card(access, "auto-meet")
            self.assertEqual(next(row["id"] for row in service.card_records(access)
                                  if row["section"] == "初次相遇"), meeting["id"])
            ordinary = [row["id"] for row in service.card_records(access)
                        if row["events"][0]["text"].startswith("普通")]
            result = service.edit_card(access, "merge", ordinary)
            self.assertIn("永久锚点", next(row["section"] for row in service.card_records(access)
                                       if row["id"] == result))
            service.edit_card(access, "unpin", [result])
            low = capture("five", "没有显著事件", occurred=5)
            service.maintain(access)
            service.maintain(access)
            with service.transaction() as db:
                clarity = db.execute("""SELECT clarity FROM memory_card JOIN memory_card_sources
                    ON card=id WHERE ref=?""", (low,)).fetchone()[0]
            self.assertEqual(clarity, 2)
            self.assertIn("初次相遇", service.context(access, "蓝鲸", budget=1200))
            with service.transaction() as db:
                self.assertEqual(db.execute("SELECT reviews FROM memory_card WHERE id=?", (meeting["id"],)).fetchone()[0], 1)
            service.edit_card(access, "pin", [result])
            service.card_max_chars = 400
            now += DAY
            service.maintain(access)
            with service.transaction() as db:
                self.assertEqual(db.execute("SELECT clarity FROM memory_card WHERE id=?", (result,)).fetchone()[0], 4)
                self.assertEqual(db.execute("SELECT count(*) FROM memory_card_archive WHERE card=?",
                                            (result,)).fetchone()[0], 1)
            self.assertEqual(service.card_records(permit("other"), "蓝鲸", archived=True), [])
            service.put_fact(access, "whale", "相遇那天看到蓝鲸", .9, ref)
            service.delete_fact(access, "whale", 1)
            self.assertEqual(service.card_records(access, "蓝鲸", archived=True), [])
            service.edit_card(access, "erase-card", [second["id"]])
            self.assertEqual(service.card_records(access, "海豚", archived=True), [])
            self.assertFalse(service.history(access, query="海豚"))


def allowed():
    return True


def permit(principal="alice", platform="qq", scope=None, verify=allowed):
    source = Source(platform, "fixture", "bot", principal, "fixture-session")
    return Access(principal, scope or Scope("private", principal), source, verify)


def observation(event_id, summary, at=1000):
    return Activity(event_id, ActivityKind.READ, OBS, at, summary, "fixture:" + event_id)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = MemoryService(self.root / "memory.sqlite3", clock=lambda: 20000)
        self.alice = permit()
        self.lark_alice = permit(platform="lark")
        self.bob = permit("bob", "lark")

    def test_card_projects_each_current_fact_on_shared_evidence(self):
        ref, _ = self.store.record(self.alice, observation("shared", "偏好资料"))
        self.store.put_fact(self.alice, "drink", "喜欢桂花茶", .8, ref)
        self.store.put_fact(self.alice, "color", "偏爱湖蓝色", .8, ref)
        result = self.store.card_records(self.alice)
        self.assertEqual(len(result), 1)
        self.assertEqual({event["text"] for event in result[0]["events"]},
                         {"喜欢桂花茶", "偏爱湖蓝色"})
        update, _ = self.store.record(self.alice, observation("updated", "更新资料", 1001))
        self.store.put_fact(self.alice, "drink", "喜欢茉莉茶", .8, update, expected_revision=1)
        current = {event["text"] for row in self.store.card_records(self.alice) for event in row["events"]}
        self.assertEqual(current, {"喜欢茉莉茶", "偏爱湖蓝色"})

    def test_shared_evidence_deletion_survives_backup_replay(self):
        ref, _ = self.store.record(self.alice, observation("shared-delete", "喜欢桂花茶。偏爱湖蓝色。"))
        self.store.put_fact(self.alice, "drink", "喜欢桂花茶", .8, ref)
        self.store.put_fact(self.alice, "color", "偏爱湖蓝色", .8, ref)
        backup = self.root / "shared-before.sqlite3"
        with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        self.store.delete_fact(self.alice, "drink", 1)
        restored = MemoryService(backup)
        restored.reapply_erasures(self.store.export_erasures())
        for service in (self.store, restored):
            rendered = service.context(self.alice, "湖蓝色")
            self.assertIn("偏爱湖蓝色", rendered)
            self.assertNotIn("桂花茶", rendered)
            self.assertEqual(service.search(self.alice, "桂花茶"), [])

    def test_retraction_replay_erases_only_the_target_event_and_its_projections(self):
        refs = {}
        for ident, fact in (("target", "drink"), ("other", "second-drink")):
            activity = Activity("in:" + ident, ActivityKind.MESSAGE_RECEIVED, OBS, 1000,
                                "喜欢桂花茶", "fixture:" + ident)
            ref, _ = self.store.record(self.alice, activity, turn_id=ident, card_capture=(False, True))
            self.store.put_fact(self.alice, fact, "喜欢桂花茶", .8, ref)
            refs[ident] = ref
        with self.store.transaction(write=True) as db:
            db.executemany("INSERT INTO activity_vectors VALUES (?,'mock','[1,0]')",
                           ((ref,) for ref in refs.values()))
            db.executemany("INSERT INTO fact_vectors VALUES (?,?,1,'mock','[1,0]')",
                           ((self.alice.scope.storage_key, fact) for fact in ("drink", "second-drink")))
        backup = self.root / "before-retraction.sqlite3"
        with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        self.assertTrue(self.store.retract_message(self.alice.scope, self.alice.principal, "target", allowed))
        restored = MemoryService(backup)
        for _ in range(2):
            restored.reapply_erasures(self.store.export_erasures())
        for service in (self.store, restored):
            self.assertIsNone(service.fact_history(self.alice, "drink")[0]["content"])
            self.assertEqual(service.fact_history(self.alice, "second-drink")[0]["content"], "喜欢桂花茶")
            self.assertEqual(len(service.card_records(self.alice)), 1)
            with service.transaction() as db:
                self.assertEqual([row[0] for row in db.execute("SELECT ref FROM activity_vectors")], [refs["other"]])
                self.assertEqual([row[0] for row in db.execute("SELECT fact_key FROM fact_vectors")], ["second-drink"])

    def test_greeting_core_card_is_private_bounded_and_filters_sensitive_text(self):
        for index, (content, importance) in enumerate((
            ("长期维护潮汐计", .9), ("花生过敏", 1), ("临时优惠券", .1),
        )):
            ref, _ = self.store.record(self.alice, observation(f"card-{index}", content))
            self.store.put_fact(self.alice, f"card-{index}", content, importance, ref)
        rendered = self.store.context(self.lark_alice, "你好！")
        self.assertIn("长期维护潮汐计", rendered)
        self.assertIn("memory_card", rendered)
        self.assertNotIn("过敏", rendered)
        self.assertNotIn("优惠券", rendered)
        self.assertFalse(self.store.context(self.bob, "你好！"))
        self.assertFalse(self.store.context(permit(scope=Scope("group", "room")), "你好！"))

    def test_idle_maintenance_recovers_scopes_after_restart(self):
        ref, _ = self.store.record(self.alice, observation("idle", "临时优惠券"))
        self.store.put_fact(self.alice, "idle", "临时优惠券", .1, ref)
        restarted = MemoryService(self.store.path, clock=lambda: 400 * 86400)
        self.assertEqual(restarted.maintain_pending(lambda: None), 1)
        self.assertEqual(restarted.maintain_pending(lambda: None), 0)
        self.assertEqual(restarted.status()["facts"]["archived"], 1)

    def test_native_history_is_read_only_only_after_explicit_cutover(self):
        private = self.root / "private"
        private.mkdir()
        config = private / "ffa-memory.json"
        with patch.dict(os.environ, {"FEIYU_RUNTIME_ROOT": str(self.root)}):
            self.assertFalse(native_history_read_only())
            config.write_text('{"enabled":true,"legacy_persona":"fixture"}')
            self.assertTrue(native_history_read_only())
            config.write_text('{"enabled":false,"legacy_persona":"fixture"}')
            self.assertFalse(native_history_read_only())
            memory = MemoryService(private / "ffa-memory.sqlite3")
            scope = self.alice.scope.storage_key
            with memory.transaction(write=True) as db:
                db.execute("INSERT INTO legacy_source_owners VALUES (?,?)", ("test-session", scope))
                db.execute("INSERT INTO memory_erasures VALUES (?,?)", (scope, "0" * 64))
            self.assertTrue(native_history_read_only())
            config.write_text("{")
            self.assertTrue(native_history_read_only())

    def test_embedding_endpoint_enforces_local_revision_and_dimensions(self):
        provider = LocalEmbeddingProvider("http://127.0.0.1:6203/v1", "bge", "a", 2, "secret")
        with self.assertRaises(ValueError):
            replace(provider, base_url="https://remote.example/v1")
        self.assertNotEqual(provider.space("model"), replace(provider, revision="b").space("model"))
        self.assertNotEqual(provider.space("model"), replace(provider, dimensions=3).space("model"))
        with patch("fat_fish.memory.embedding.HTTPConnection") as connector:
            connection = connector.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"model":"bge","data":[{"embedding":[1,2]}]}'
            self.assertEqual(provider._request("离线语义验证"), [1, 2])
            response.read.return_value = b'{"model":"bge","data":[{"embedding":[1]}]}'
            with self.assertRaises(ValueError):
                provider._request("不匹配的向量")
            connection.close.assert_called()

    def test_semantic_episode_evidence_is_scoped_and_invalidated_on_redaction(self):
        first, _ = self.store.record(self.alice, Activity("episode-in", ActivityKind.MESSAGE_RECEIVED,
                                                         OBS, 1001, "约好在周末装潮汐计"), turn_id="turn", card_capture=(False, True))
        self.store.record(self.alice, Activity("episode-out", ActivityKind.MESSAGE_SENT,
                                              OBS, 1002, "我会准备工具"), turn_id="turn", card_capture=(False, True))

        class Provider:
            async def get_embedding(self, text):
                return [1, 0]

        async def check():
            provider = Provider()
            await IndexMaintenance().run(self.store, self.alice, "v1", provider, activities=True)
            scores = await recall(self.store, self.lark_alice, "未来安排", "v1", provider, activities=True)
            self.assertIn(first, scores)
            self.assertIn("潮汐计", self.store.context(self.lark_alice, "未来安排", semantic=scores))
            self.assertEqual(await recall(self.store, self.bob, "未来安排", "v1", provider, activities=True), {})
            with self.store.transaction(write=True) as db:
                db.execute("UPDATE activities SET redacted=1,summary='' WHERE ref=?", (first,))
                self.assertIsNone(db.execute("SELECT 1 FROM activity_vectors WHERE ref=?", (first,)).fetchone())
            self.assertNotIn("潮汐计", self.store.context(self.lark_alice, "未来安排", semantic=scores))

        asyncio.run(check())

    def test_recall_updates_compaction_and_trace(self):
        refs = []
        fixture = [
            ("favorite-coffee", "长期喜欢深焙咖啡", .9),
            ("weekend-project", "周末维护雨量传感器", .8),
            ("short-coffee-note", "咖啡馆曾发过一张优惠券", .1),
            ("allergy", "花生过敏", 1.0),
        ]
        for index, (key, text, importance) in enumerate(fixture):
            ref, _ = self.store.record(self.alice, observation("event" + str(index), text, 1000 + index))
            refs.append(ref)
            self.store.put_fact(self.alice, key, text, importance, ref)
        self.assertEqual(self.store.search(self.lark_alice, "咖啡")[0]["fact_key"], "favorite-coffee")
        self.assertEqual(self.store.search(self.lark_alice, "花生")[0]["content"], "花生过敏")
        ref, _ = self.store.record(self.lark_alice, observation("correct", "偏好现在是浅焙咖啡", 3000))
        self.assertEqual(self.store.put_fact(self.lark_alice, "favorite-coffee", "偏好浅焙咖啡", .95, ref,
                                             expected_revision=1), 2)
        with self.assertRaises(ValueError):
            self.store.put_fact(self.alice, "favorite-coffee", "旧偏好回写", 1, refs[0], expected_revision=2)
        self.assertEqual(self.store.compact(self.alice, keep=2), 2)
        self.assertEqual(self.store.search(self.lark_alice, "传感器"), [])
        self.assertEqual(self.store.search(self.lark_alice, "传感器", include_archive=True)[0]["ref"], refs[1])
        hit = self.store.search(self.alice, "咖啡")[0]
        self.assertEqual((hit["content"], hit["source"]["platform"], hit["ref"]), ("偏好浅焙咖啡", "lark", ref))
        versions = self.store.fact_history(self.alice, "favorite-coffee")
        self.assertEqual([item["ref"] for item in versions], [ref, refs[0]])
        self.assertEqual(self.store.fact_history(self.bob, "favorite-coffee"), [])
        context = self.store.context(self.lark_alice, "咖啡")
        self.assertIn("偏好浅焙咖啡", context)
        self.assertNotIn("长期喜欢深焙咖啡", context)
        history = self.store.history(self.alice, query="深焙")
        self.assertEqual(history[0]["ref"], refs[0])
        self.assertEqual(self.store.search(self.bob, "咖啡", include_archive=True), [])

    def test_exact_scope_author_and_authorization(self):
        team = Scope("group", "provider-a:team")
        other_team = Scope("group", "provider-b:team")
        group_alice = permit(scope=team)
        group_bob = permit("bob", scope=team)
        ref, _ = self.store.record(group_alice, observation("team", "共享项目 Alpha"))
        self.store.put_fact(group_alice, "project", "Alpha", .9, ref)
        self.assertEqual(self.store.search(group_bob, "alpha")[0]["content"], "Alpha")
        with self.assertRaises(PermissionError):
            self.store.put_fact(group_bob, "project", "Beta", .9, ref, expected_revision=1)
        self.assertEqual(self.store.search(permit(scope=other_team), "alpha"), [])
        self.assertEqual(self.store.search(self.alice, "alpha"), [])
        self.assertEqual(self.store.history(self.bob), [])
        revoked = replace(group_alice, verify=lambda: False)
        with self.assertRaises(PermissionError):
            self.store.history(revoked)
        with self.assertRaises(PermissionError):
            self.store.delete_fact(replace(group_alice, may_write=False), "project", 1)

    def test_fact_delete_erases_fact_text_and_preserves_evidence_link(self):
        ref, _ = self.store.record(self.alice, observation("evidence", "明确用户记忆操作"))
        self.store.put_fact(self.alice, "preference", "偏爱茶", 1, ref)
        self.assertTrue(self.store.delete_fact(self.lark_alice, "preference", 1))
        self.assertFalse(self.store.delete_fact(self.alice, "preference", 1))
        with self.assertRaises(PermissionError):
            self.store.put_fact(self.alice, "preference", "偏爱茶", 1, ref, expected_revision=1)
        self.assertEqual(self.store.search(self.alice, "偏爱", include_archive=True), [])
        self.assertEqual(self.store.history(self.alice), [])
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT content FROM fact_versions").fetchone()[0])

    def test_public_recall_and_deleted_copies_are_denied(self):
        text = "偏好试验室的蓝色背包"
        ref, _ = self.store.record(self.alice, observation("in-private", text))
        self.store.record(self.alice, observation("copy", "回复重述：" + text))
        self.store.put_fact(self.alice, "preference", text, .9, ref)
        self.store.put_fact(self.alice, "duplicate", text, .9, ref)
        self.assertTrue(self.store.search(self.lark_alice, "背包"))
        for external in (permit(platform="bilibili"), replace(self.alice, audience="public")):
            for read, args in ((self.store.search, ("背包",)), (self.store.history, ()),
                               (self.store.context, ("背包",)), (self.store.fact_history, ("preference",))):
                with self.subTest(read=read.__name__, platform=external.source.platform):
                    with self.assertRaises(PermissionError):
                        read(external, *args)
        self.assertTrue(self.store.delete_fact(self.alice, "preference", 1))
        self.assertEqual(self.store.history(self.alice), [])
        self.assertEqual(self.store.context(self.alice, "背包"), "")
        self.assertEqual(self.store.search(self.alice, "背包", include_archive=True), [])
        fresh, _ = self.store.record(self.alice, observation("reauthorized", text, 1001))
        self.assertEqual(self.store.put_fact(self.alice, "preference", text, .9, fresh,
                                             expected_revision=1), 2)
        self.assertIsNone(self.store.fact_history(self.alice, "preference")[1]["content"])

    def test_group_membership_does_not_follow_a_bound_alias(self):
        platform = ProviderId("lark", "fixture")
        owner = Account(QQ, "10001")
        first, second = Account(platform, "first"), Account(platform, "second")
        identities = IdentityDirectory(self.root / "identity.sqlite3")
        identities.confirm_owner_challenge(owner, identities.issue_owner_challenge(owner))
        identities.observe(first)
        person = identities.provision(owner, first, Role.MEMBER)
        room = Conversation(platform, "bot", ChatKind.GROUP, "team", "thread")
        identities.set_room(owner, room, "open", [first])
        self.assertEqual(identities.confirm(second, identities.invite(first, first, second)), person)
        self.assertEqual(identities.access(first, room)[2], "open")
        self.assertIsNone(identities.access(second, room)[2])
        identities.unlink(owner, first)
        self.assertIsNone(identities.access(first, room)[2])
        identities.set_room(owner, room, "mention", [second])
        self.assertEqual(identities.access(second, room)[2], "mention")
        identities.provision(owner, second, Role.BLOCKED)
        self.assertIsNone(identities.access(second, room)[2])

    def test_small_ranked_recall_corpus(self):
        cases = (
            ("rain", "我正在维护屋顶雨量仪", ("屋顶雨量仪在哪", "维护哪台雨量仪", "之前说的雨量仪")),
            ("coffee", "我喜欢浅焙咖啡", ("喝哪种咖啡", "我的浅焙偏好", "浅焙咖啡的记忆")),
            ("backpack", "我偏好红色旅行背包", ("红色旅行背包是哪种", "旅行背包偏好", "提过的红色背包")),
            ("notebook", "我习惯周五整理笔记", ("何时整理笔记", "周五的笔记习惯", "哪天整理笔记")),
            ("meter", "我正在开发海洋潮汐计", ("正在开发的潮汐计", "海洋潮汐计项目", "以前提到的潮汐计")),
            ("allergy", "我对花生过敏", ("花生能吃吗", "过敏原是什么", "有没有花生过敏")),
            ("garden", "我长期维护屋顶花园", ("屋顶花园还维护吗", "花园项目是什么", "哪座花园")),
            ("lantern", "我喜欢低饱和纸灯笼", ("纸灯笼什么色", "低饱和的灯笼", "灯笼偏好")),
        )
        for index, (fact_key, content, _) in enumerate(cases):
            ref, _ = self.store.record(self.alice, observation(f"quality-{index}", content))
            self.store.put_fact(self.alice, fact_key, content, .8, ref)
        for fact_key, _, queries in cases:
            for query in queries:
                with self.subTest(query=query):
                    hits = self.store.search(self.lark_alice, query, limit=1)
                    self.assertEqual([hit["fact_key"] for hit in hits], [fact_key])

    def test_version_one_migration_failure_and_retry(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.executescript(SCHEMA + "PRAGMA user_version=1;")
            db.execute("CREATE TABLE fact_vectors (fixture TEXT)")
        with self.assertRaises(sqlite3.OperationalError):
            self.store.history(self.alice)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertNotIn("redacted", [row[1] for row in db.execute("PRAGMA table_info(activities)")])
            db.execute("DROP TABLE fact_vectors")
        self.assertEqual(self.store.history(self.alice), [])
        with self.store.transaction() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 14)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(MemoryService(self.store.path).history(self.alice), [])

    def test_clarity_decay_review_and_history_filters(self):
        now = 20000
        self.store.clock = lambda: now
        ref, _ = self.store.record(self.alice, observation("low", "临时雨量资料"))
        self.store.put_fact(self.alice, "ordinary", "临时雨量资料", .3, ref)
        ref, _ = self.store.record(self.alice, observation("high", "花生过敏"))
        self.store.put_fact(self.alice, "anchor", "花生过敏", .99, ref)
        initial = self.store.search(self.alice, "雨量")[0]["clarity"]
        now += 30 * 86400
        before = self.store.search(self.alice, "雨量")[0]["clarity"]
        self.assertLess(before, initial)
        self.store.review(self.alice, self.store.search(self.alice, "雨量"))
        self.assertGreater(self.store.search(self.alice, "雨量")[0]["clarity"], before)
        now += 300 * 86400
        self.assertEqual(self.store.search(self.alice, "雨量"), [])
        self.assertTrue(self.store.search(self.alice, "雨量", include_archive=True))
        self.assertTrue(self.store.search(self.alice, "花生"))
        self.assertEqual(len(self.store.history(self.alice, platform="qq", kind="read", state="observed",
                                                executor="alice", since=999, until=1001)), 2)
        self.assertEqual(self.store.history(self.alice, platform="lark"), [])

    def test_history_question_filters_platform_day_and_activity_kind(self):
        self.store.clock = lambda: datetime(2026, 11, 12, 12).timestamp()
        tuesday = datetime(2026, 11, 10, 12).timestamp()
        wednesday = datetime(2026, 11, 11, 12).timestamp()
        bili = Source("bilibili", "fixture", "bot", "agent", "public-video")
        for event_id, text, kind, when, source in (
            ("wanted", "看了雨量仪拆解", ActivityKind.BROWSE, tuesday, bili),
            ("wrong-day", "读了潮汐计拆解", ActivityKind.READ, wednesday, bili),
            ("wrong-platform", "看了天文望远镜", ActivityKind.BROWSE, tuesday, self.alice.source),
            ("wrong-kind", "写了旧任务", ActivityKind.TASK, tuesday, bili),
        ):
            self.store.record(self.alice, Activity(event_id, kind, OBS, when, text), source=source)
        result = self.store.context(self.alice, "前天在B站看了什么")
        self.assertIn("雨量仪拆解", result)
        for text in ("潮汐计拆解", "天文望远镜", "旧任务"):
            self.assertNotIn(text, result)


    def test_context_records_card_review(self):
        ref, _ = self.store.record(self.alice, observation("tea", "用户偏爱桂花茶"))
        self.store.put_fact(self.alice, "tea", "用户偏爱桂花茶", .8, ref)
        self.assertIn("桂花茶", self.store.context(self.alice, "桂花茶"))
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT reviews FROM memory_card").fetchone()[0], 1)

    def test_relative_day_uses_explicit_timezone_at_midnight(self):
        now = datetime(2026, 11, 12, 1, tzinfo=timezone.utc).timestamp()
        before = datetime(2026, 11, 11, 18, tzinfo=timezone.utc).timestamp()
        self.store.clock = lambda: now
        source = Source("bilibili", "fixture", "bot", "agent", "public")
        self.store.record(self.alice, Activity("midnight", ActivityKind.BROWSE, OBS, before, "雨量仪"),
                          source=source)
        self.assertIn("雨量仪", self.store.context(self.alice, "昨天在B站看了什么"))
        self.assertNotIn("雨量仪", self.store.context(self.alice, "昨天在B站看了什么",
                                                     user_timezone=parse_timezone("+08:00")))
        self.assertIn("雨量仪", self.store.context(self.alice, "今天在B站看了什么",
                                                   user_timezone=parse_timezone("+08:00")))

    def test_erasure_reapplied_to_predelete_snapshot(self):
        ref, _ = self.store.record(self.alice, observation("remove", "忘记这个已记录的雨量仪设置"))
        self.store.put_fact(self.alice, "remove", "已记录的雨量仪设置", .8, ref)
        ref, _ = self.store.record(self.alice, observation("keep", "不相关的笔记习惯"))
        self.store.put_fact(self.alice, "keep", "不相关的笔记习惯", .8, ref)
        before = self.root / "before.sqlite3"
        with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(before)) as copy:
            source.backup(copy)
        self.store.delete_fact(self.alice, "remove", 1)
        snapshot = MemoryService(before, clock=self.store.clock)
        snapshot.reapply_erasures(self.store.export_erasures())
        snapshot.reapply_erasures(self.store.export_erasures())
        self.assertEqual(snapshot.search(self.alice, "雨量仪"), [])
        self.assertEqual(snapshot.history(self.alice, query="雨量仪"), [])
        self.assertTrue(snapshot.search(self.alice, "笔记习惯"))

    def test_context_drops_text_deleted_after_search_snapshot(self):
        ref, _ = self.store.record(self.alice, observation("erase-race", "遗忘旧旅行地点"))
        self.store.put_fact(self.alice, "location", "遗忘旧旅行地点", .8, ref)
        original = self.store.card_records

        def stale_result(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.delete_fact(self.alice, "location", 1)
            return result

        with patch.object(self.store, "card_records", side_effect=stale_result):
            self.assertEqual(self.store.context(self.alice, "旅行地点"), "")

    def test_duplicate_and_out_of_order_activity(self):
        activity = observation("stable", "观察到内容")
        ref, created = self.store.record(self.alice, activity)
        self.assertTrue(created)
        self.assertEqual(self.store.record(self.alice, activity), (ref, False))
        with self.assertRaises(ValueError):
            self.store.record(self.alice, replace(activity, summary="另一件事"))
        with self.assertRaises(ValueError):
            self.store.record(self.alice, Activity("finish", ActivityKind.TASK, ActivityState.SUCCEEDED,
                                                   2000, "完成", "proof", "job", "intent"))
        with self.assertRaises(ValueError):
            Activity("fake", ActivityKind.TASK, ActivityState.SUCCEEDED, 2000, "完成", "", "job", "intent")
        self.assertEqual(len(self.store.history(self.alice)), 1)

    def test_partial_write_rolls_back_before_retry(self):
        def phase(state, at, evidence=""):
            return Activity(state.value, ActivityKind.TASK, state, at, "索引任务", evidence, "job", "same")

        planned = phase(ActivityState.PLANNED, 100)
        running = phase(ActivityState.RUNNING, 200)
        done = phase(ActivityState.SUCCEEDED, 300, "verified:output")
        self.store.record(self.alice, planned)
        self.store.record(self.alice, running)
        with self.store.transaction(write=True) as db:
            db.execute("""CREATE TRIGGER abort_transition BEFORE UPDATE ON operations
                          BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record(self.alice, done)
        self.assertEqual(self.store.operation(self.alice, "core", "job")["state"], "running")
        self.assertEqual(self.store.history(self.alice, actual_only=True), [])
        with self.store.transaction(write=True) as db:
            db.execute("DROP TRIGGER abort_transition")
        reopened = MemoryService(self.store.path)
        self.assertTrue(reopened.record(self.alice, done)[1])
        self.assertFalse(reopened.record(self.alice, done)[1])
        self.assertEqual(reopened.operation(self.alice, "core", "job")["state"], "succeeded")

    def test_crash_before_commit_recovers_on_restart(self):
        self.store.record(self.alice, observation("before", "保留"))
        code = """
import os, sys
from fat_fish.memory.activity import Access, Activity, ActivityKind, ActivityState, Scope, Source
from fat_fish.memory.service import MemoryService, SCHEMA, parse_timezone
n=0
def verify():
    global n
    n+=1
    if n==2: os._exit(77)
    return True
access=Access('alice',Scope('private','alice'),Source('qq','fixture','bot','alice','fixture-session'),verify)
MemoryService(sys.argv[1]).record(access, Activity('crash',ActivityKind.READ,ActivityState.OBSERVED,1001,'中断'))
"""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        proc = subprocess.run([sys.executable, "-B", "-c", code, str(self.store.path)], env=env,
                              capture_output=True, timeout=20)
        self.assertEqual(proc.returncode, 77, proc.stderr.decode(errors="replace"))
        restarted = MemoryService(self.store.path)
        self.assertEqual([item["event_id"] for item in restarted.history(self.alice)], ["before"])
        self.assertTrue(restarted.record(self.alice, observation("crash", "重试"))[1])

    def test_concurrent_record_and_query_consistency_and_latency(self):
        duration = []
        start = time.perf_counter()
        def worker(index):
            access = self.alice if index % 2 else self.lark_alice
            elapsed = time.perf_counter()
            event = observation("parallel-" + str(index), "并发索引 " + str(index), 1000 + index)
            ref, created = self.store.record(access, event)
            self.assertTrue(created)
            self.assertEqual(self.store.record(access, event), (ref, False))
            self.assertTrue(self.store.history(access, query="并发", limit=1))
            return time.perf_counter() - elapsed

        with ThreadPoolExecutor(max_workers=6) as pool:
            duration.extend(pool.map(worker, range(144)))
        elapsed = time.perf_counter() - start
        p95 = sorted(duration)[int(len(duration) * .95) - 1]
        self.assertEqual(len(self.store.history(self.alice, limit=200)), 144)
        self.assertEqual(self.store.history(self.bob), [])
        self.assertLess(elapsed, 120)
        self.assertLess(p95, 12)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        size = self.store.path.stat().st_size
        self.assertLess(size, 2 * 1024 * 1024)
        print(f"Memory load: 144 events + 144 retries + 144 queries, elapsed={elapsed:.3f}s p95={p95:.3f}s db={size} bytes")


class SemanticMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_progress_retry_and_model_isolation(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memory = MemoryService(Path(tmp.name) / "memory.sqlite3", clock=lambda: 1000)
        access = permit()
        for index, text in enumerate(("slow", "ready")):
            ref, _ = memory.record(access, observation(text, text, 1000 + index))
            memory.put_fact(access, text, text, .8, ref)
        now = 0
        calls = []
        failing = True

        class Provider:
            async def get_embedding(self, text):
                calls.append(text)
                if text == "slow" and failing:
                    await asyncio.sleep(1)
                return [1, 0]

        maintenance = IndexMaintenance(clock=lambda: now)
        provider = Provider()
        await maintenance.run(memory, access, "A", provider, timeout=.01)
        self.assertEqual([row["fact_key"] for row in candidates(memory, access, "A") if row["vector"]], ["ready"])
        await maintenance.run(memory, access, "A", provider, timeout=.01)
        self.assertEqual(calls, ["ready", "slow"])
        now, failing = 5, False
        await maintenance.run(memory, access, "A", provider)
        await maintenance.run(memory, access, "B", provider)
        self.assertEqual(len(await recall(memory, access, "probe", "A", provider)), 2)
        self.assertEqual(reset(memory, access, "B"), 2)
        self.assertFalse(any(row["vector"] for row in candidates(memory, access, "B")))
        self.assertTrue(all(row["vector"] for row in candidates(memory, access, "A")))

    async def test_revocation_during_embedding_does_not_cache_result(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memory = MemoryService(Path(tmp.name) / "memory.sqlite3", clock=lambda: 1000)
        original = permit()
        ref, _ = memory.record(original, observation("revoked-index", "授权配色"))
        memory.put_fact(original, "palette", "授权配色", .8, ref)
        valid = True

        class Provider:
            async def get_embedding(self, _):
                nonlocal valid
                valid = False
                return [1, 0]

        await IndexMaintenance().run(memory, replace(original, verify=lambda: valid), "A", Provider())
        self.assertFalse(any(row["vector"] for row in candidates(memory, original, "A")))


class Connector:
    def __init__(self, result=None, *, failure=False):
        self.result = result or ToolResult(True, "已发布视频评论", "bili:reply:123")
        self.failure = failure
        self.calls = 0
        self.started = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False

    async def healthy(self):
        return self.started and not self.failure

    async def invoke_checked(self, name, arguments, authorize):
        authorize()
        return await self.invoke(name, arguments)

    async def invoke(self, name, arguments):
        self.calls += 1
        await asyncio.sleep(0)
        if self.failure:
            raise ConnectionError("Unknown remote effect")
        return self.result


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "memory.sqlite3"
        self.store = MemoryService(self.path)
        self.access = permit()
        self.source = Source("bilibili", "whale", "bili-bot", "experiment-account", "isolated-diary")
        self.approvals = {}
        self.registry = self.make_registry(self.store)
        self.connector = Connector()
        self.handle = self.registry.register(MANIFEST, self.connector, self.access, self.source,
                                             allowed=("browse", "video-content", "diary", "comment"), approved_manifest=MANIFEST.fingerprint)
        self.assertTrue(await self.registry.enable(self.handle))
        self.args = {"bvid": "BV1xx411c7mD", "text": "谢谢分享"}

    def make_registry(self, memory):
        def authenticate(event, source, tool, operation, intent):
            entry = self.approvals.get(event) if isinstance(event, str) else None
            if entry and entry[:4] == (source, tool, operation, intent):
                return ApprovalEvent(event, entry[5], entry[4])

        return AgentRegistry(memory, environment="dsh", dependency_available=lambda _: True,
                             authenticate_approval=authenticate,
                             authorize_output=lambda source, _, args: OutputAuthorization.attest(
                                 "confirmed_user", source.account, args, "fixture-confirmation"),
                             verify_outcome=lambda source, tool, op, args, result:
                                 result.evidence.startswith("verified:") or (
                                     result.evidence.startswith("bili:{")
                                     and json.loads(result.evidence[5:])["target"] == args["bvid"]
                                     and json.loads(result.evidence[5:])["account"] == source.account
                                 ))

    def approval(self, operation, approver, *, event=None, arguments=None):
        event = event or "trusted:" + operation
        tool = next(tool for tool in MANIFEST.tools if tool.name == "comment")
        intent = self.registry._intent(tool, arguments or self.args, MANIFEST, self.source)
        self.approvals[event] = self.source, "comment", operation, intent, approver, self.registry.clock()
        return event

    async def test_bili_observation_approval_and_actual_recall(self):
        bridge = BiliWhaleObservations(self.registry, self.handle)
        for cmd in ("browse", "video-content", "diary"):
            info = dict(command=cmd, receipt_id="receipt-1", bvid=self.args["bvid"], occurred=3000,
                        title="雨量传感器", evidence="local:fixture:" + cmd)
            self.assertTrue(bridge.submit(**info)[1])
            self.assertFalse(bridge.submit(**info)[1])
        with self.assertRaises(ValueError):
            bridge.submit(command="comment", receipt_id="forged", bvid=self.args["bvid"], occurred=3000,
                          title="伪造成功", evidence="untrusted")
        native_diary = {"at": "2026-11-11T16:00:00Z", "kind": "comment",
                        "target": "BV:" + self.args["bvid"], "note": "声称评论已完成"}
        self.assertTrue(bridge.submit_diary(native_diary, receipt_id="log-offset-17",
                                            evidence="local:diary:17")[1])
        diary = self.store.history(self.access, query="记录日记")[0]
        self.assertNotIn(native_diary["note"], diary["summary"])
        self.assertEqual((diary["kind"], diary["state"]), ("read", "observed"))
        result = {"code": 0, "data": {"rpid_str": "203744978475184128"}}
        details = dict(target=self.args["bvid"], account=self.source.account, checked_at=time.time())
        readback = {"target": details["target"], "account": details["account"], "reply": "203744978475184128"}
        with self.assertRaises(ValueError):
            comment_result(result, go=False, exit_code=0, verified_reply=readback, **details)
        with self.assertRaises(ValueError):
            comment_result(result, go=True, exit_code=0, verified_reply=None, **details)
        with self.assertRaises(ValueError):
            comment_result(result, go=True, exit_code=0, verified_reply={**readback, "account": "other"}, **details)
        self.connector.result = comment_result(result, go=True, exit_code=0, verified_reply=readback, **details)
        self.assertEqual(self.registry.plan(self.handle, "comment", "comment-1", self.args, "计划评论"),
                         ActivityState.AWAITING_APPROVAL)
        self.assertNotIn("计划评论", self.store.context(permit(platform="lark"), "评论"))
        with self.assertRaises(PermissionError):
            await self.registry.execute(self.handle, "comment", "comment-1", self.args)
        with self.assertRaises(PermissionError):
            self.registry.approve(self.handle, "comment", "comment-1", self.args, self.approval("comment-1", permit("bob")))
        with self.assertRaises(PermissionError):
            self.registry.approve(self.handle, "comment", "comment-1", {**self.args, "text": "changed"},
                                  self.approval("comment-1", self.access))
        self.registry.approve(self.handle, "comment", "comment-1", self.args, self.approval("comment-1", permit(platform="lark")))
        results = await asyncio.gather(*(self.registry.execute(self.handle, "comment", "comment-1", self.args)
                                         for _ in range(3)))
        self.assertEqual(results, [ActivityState.SUCCEEDED] * 3)
        self.assertEqual(self.connector.calls, 1)
        context = self.store.context(permit(platform="lark"), "评论")
        self.assertIn("bilibili", context)
        self.assertIn("succeeded", context)
        self.assertIn("已发布视频评论", context)
        self.assertNotIn("计划评论", context)
        self.assertIn("已发布视频评论", self.store.context(self.access, "你最近做了什么"))
        entries = self.store.history(self.access, query="雨量传感器")
        self.assertEqual({item["kind"] for item in entries}, {"browse", "read"})

    async def test_approval_expiry_replay_privacy_and_limits(self):
        self.registry.plugins[MANIFEST.plugin_id].write_limits = WriteLimits("bvid", 2, 600, 300)
        now = time.time()
        self.registry.clock = lambda: now
        for operation in ("expired", "first", "duplicate-target", "reused", "private"):
            self.registry.plan(self.handle, "comment", operation,
                               self.args if operation != "private" else {**self.args, "text": "私人测试背包"}, "评论")
        with self.assertRaises(PermissionError):
            self.registry.approve(self.handle, "comment", "expired", self.args, self.access)
        event = self.approval("expired", self.access)
        self.registry.approve(self.handle, "comment", "expired", self.args, event)
        now += 301
        with self.assertRaises(PermissionError):
            await self.registry.execute(self.handle, "comment", "expired", self.args)
        with self.assertRaises(PermissionError):
            self.registry.approve(self.handle, "comment", "reused", self.args,
                                  self.approval("reused", self.access, event=event))
        ref, _ = self.store.record(self.access, observation("private-ref", "私人测试背包"))
        self.store.put_fact(self.access, "private", "私人测试背包", .8, ref)
        args = {**self.args, "text": "私人测试背包"}
        with self.assertRaises(PermissionError):
            self.registry.approve(self.handle, "comment", "private", args,
                                  self.approval("private", self.access, arguments=args))
        self.connector.result = ToolResult(True, "verified", "verified:comment")
        for operation in ("first", "duplicate-target"):
            self.registry.approve(self.handle, "comment", operation, self.args,
                                  self.approval(operation, self.access))
        self.assertEqual(await self.registry.execute(self.handle, "comment", "first", self.args),
                         ActivityState.SUCCEEDED)
        with self.assertRaises(PermissionError):
            await self.registry.execute(self.handle, "comment", "duplicate-target", self.args)
        self.assertEqual(self.connector.calls, 1)

    async def test_private_provenance_rejects_paraphrase_before_submission(self):
        args = {**self.args, "text": "她正在考虑换工作"}
        operation = "unapproved-provenance"
        self.registry.plan(self.handle, "comment", operation, args, "外发草稿")
        self.registry.authorize_output = lambda source, _, body: OutputAuthorization.attest(
            "private", source.account, body, "memory-source")
        with self.assertRaises(PermissionError):
            self.registry.approve(self.handle, "comment", operation, args,
                                  self.approval(operation, self.access, arguments=args))
        self.assertEqual(self.connector.calls, 0)

    async def test_agent_rechecks_live_approval_after_connector_wait(self):
        entered, resume = asyncio.Event(), asyncio.Event()
        valid = True

        async def dispatch(name, arguments, authorize):
            entered.set()
            await resume.wait()
            authorize()
            return await self.connector.invoke(name, arguments)

        self.connector.invoke_checked = dispatch
        approver = permit(platform="lark", verify=lambda: valid)
        operation = "wait-for-permission"
        self.registry.plan(self.handle, "comment", operation, self.args, "等待评论")
        self.registry.approve(self.handle, "comment", operation, self.args, self.approval(operation, approver))
        task = asyncio.create_task(self.registry.execute(self.handle, "comment", operation, self.args))
        await entered.wait()
        valid = False
        resume.set()
        with self.assertRaises(PermissionError):
            await task
        self.assertEqual(self.connector.calls, 0)

    async def test_unknown_side_effect_requires_reconciliation_after_restart(self):
        self.connector.failure = True
        self.registry.plan(self.handle, "comment", "uncertain", self.args, "等待评论")
        self.registry.approve(self.handle, "comment", "uncertain", self.args, self.approval("uncertain", self.access))
        with self.assertRaises(ConnectionError):
            await self.registry.execute(self.handle, "comment", "uncertain", self.args)
        self.assertEqual(await self.registry.health(self.handle), "unhealthy")
        self.assertEqual(self.store.history(self.access, actual_only=True), [])
        reopened = self.make_registry(MemoryService(self.path))
        other = Connector()
        handle = reopened.register(MANIFEST, other, self.access, self.source, allowed=("comment",), approved_manifest=MANIFEST.fingerprint)
        await reopened.enable(handle)
        self.assertEqual(await reopened.execute(handle, "comment", "uncertain", self.args), ActivityState.RUNNING)
        self.assertEqual(other.calls, 0)
        self.assertEqual(reopened.reconcile(handle, "comment", "uncertain", self.args, self.access,
                                             ToolResult(False, "评论被远端拒绝", "verified:server-denial")),
                         ActivityState.FAILED)
        self.assertIn("failed", self.store.context(self.access, "评论"))
        self.assertNotIn("succeeded", self.store.context(self.access, "评论"))

    async def test_pre_migration_running_write_can_reconcile_without_replay(self):
        slot = self.registry.plugins[MANIFEST.plugin_id]
        tool = next(tool for tool in MANIFEST.tools if tool.name == "comment")
        intent = self.registry._legacy_intent(tool, self.args)
        for state in (ActivityState.PLANNED, ActivityState.AWAITING_APPROVAL,
                      ActivityState.APPROVED, ActivityState.RUNNING):
            self.registry._write(slot, tool, "old-operation", intent, state, "旧记录")
        self.assertEqual(await self.registry.execute(self.handle, "comment", "old-operation", self.args),
                         ActivityState.RUNNING)
        self.assertEqual(self.connector.calls, 0)
        result = ToolResult(False, "远端明确拒绝", "verified:fixture")
        self.assertEqual(self.registry.reconcile(self.handle, "comment", "old-operation",
                                                 self.args, self.access, result), ActivityState.FAILED)

    async def test_plugin_dependencies_grants_stop_and_health_isolation(self):
        bad = Manifest("missing-host", "different-host", ("unknown",), (Tool("read", ActivityKind.READ, Risk.READ),))
        other = Connector()
        handle = self.registry.register(bad, other, self.access, self.source, allowed=("read",))
        self.assertFalse(await self.registry.enable(handle))
        self.assertEqual(await self.registry.health(handle), "dependency_missing")
        self.assertEqual(await self.registry.health(self.handle), "running")
        with self.assertRaises(PermissionError):
            self.registry.observe(object(), "browse", event_id="x", occurred=1, summary="x", evidence="x")
        with self.assertRaises(PermissionError):
            self.registry.observe(self.handle, "comment", event_id="x", occurred=1, summary="x", evidence="x")
        await self.registry.disable(self.handle)
        self.assertFalse(self.connector.started)
        with self.assertRaises(PermissionError):
            self.registry.plan(self.handle, "comment", "x", self.args, "已停用")




class ChatMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from fat_fish.gateway.platform_gateway import PlatformGateway
        from test_communication import VerifiedProvider

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.private = Path(tmp.name)
        self.config = self.private / "ffa-memory.json"
        self.gateway = PlatformGateway(private=self.private)
        self.runtime = self.gateway.communication()
        self.lark = ProviderId("lark", "office")
        for address in (QQ, self.lark):
            self.runtime.register(VerifiedProvider(address))
            await self.runtime.enable(address)
        self.qq_user, self.lark_user = Account(QQ, "32"), Account(self.lark, "ou_friend")
        self.owner = Account(QQ, "31")
        identities = self.runtime.identities
        identities.confirm_owner_challenge(self.owner, identities.issue_owner_challenge(self.owner))
        identities.observe(self.qq_user)
        identities.provision(self.owner, self.qq_user, Role.MEMBER)
        identities.confirm(self.lark_user, identities.invite(self.qq_user, self.qq_user, self.lark_user))

    async def unexpected(self):
        self.fail("Control or unauthorized event reached the chat model")

    async def test_registered_mock_adapter_can_recall_the_same_principal(self):
        provider = ProviderId("synthetic", "office-2")
        self.runtime.register(ControlledProvider(provider))
        await self.runtime.enable(provider)
        target = Account(provider, "verified")
        self.runtime.identities.confirm(target, self.runtime.identities.invite(self.owner, self.qq_user, target))
        event = NativeEvent("synthetic", "office-2", sender="verified", text="/memory remember instrument 在维护潮汐计")
        await self.gateway.run(event, self.unexpected)
        self.assertIn("已保存", event.sent[0])
        help_event = NativeEvent("synthetic", "office-2", sender="verified", ident="help", text="/help memory")
        await self.gateway.run(help_event, self.unexpected)
        self.assertIn("/memory archive query", help_event.sent[0])
        other = NativeEvent(sender=self.lark_user.subject, ident="mock-recall", text="潮汐计")
        request = SimpleNamespace(prompt=other.message_str, system_prompt="")

        async def recall():
            await self.gateway.memory_bridge.augment(other, request)

        await self.gateway.run(other, recall)
        self.assertIn("在维护潮汐计", request.prompt)

    async def test_lark_stream_rechecks_authority_after_card_creation(self):
        event = NativeEvent(sender=self.lark_user.subject, ident="stream-revoked", text="stream")
        submitted_cards = []

        async def create():
            self.runtime.identities.unlink(self.owner, self.lark_user)
            return "card"

        async def send_card(card):
            submitted_cards.append(card)

        async def stream(parts):
            async for _ in parts:
                await event._send_card_message(await event._create_streaming_card())

        async def tokens():
            yield "hello"

        from fat_fish.adapters.astrbot_communication import guard_lark_stream
        self.runtime.connections[self.lark].provider.guard_stream = guard_lark_stream
        event._create_streaming_card = create
        event._send_card_message = send_card
        event.send_streaming = stream
        async def downstream():
            await event.send_streaming(tokens())

        with self.assertRaises(asyncio.CancelledError):
            await self.gateway.run(event, downstream)
        self.assertEqual(submitted_cards, [])
        self.assertEqual(self.runtime.seen.delivery("ingress", convert(event).event_id), "unknown")
        self.assertIs(event._send_card_message, send_card)

    async def test_ingress_retries_before_submit_and_preserves_uncertainty(self):
        def incoming(ident):
            return NativeEvent(sender=self.lark_user.subject, ident=ident, text="你好")

        async def before_submit():
            raise TimeoutError("before dispatch")

        first = incoming("safe-retry")
        await self.gateway.run(first, before_submit)
        replay = incoming("safe-retry")
        async def reply():
            await replay.send("reply")
        await self.gateway.run(replay, reply)
        self.assertEqual(replay.sent, ["reply"])

        uncertain = incoming("unknown-submit")
        async def missing_reply(_):
            raise TimeoutError("provider response lost")
        uncertain.send = missing_reply
        async def dispatch():
            await uncertain.send("reply")

        await self.gateway.run(uncertain, dispatch)
        self.assertEqual(self.runtime.seen.delivery("ingress", convert(uncertain).event_id), "unknown")
        replay = incoming("unknown-submit")
        await self.gateway.run(replay, reply)
        self.assertEqual(replay.sent, [])

    async def test_sensitive_chat_is_metadata_only_and_failed_send_is_not_history(self):
        self.config.write_text('{"schema":1,"enabled":true,"capture_text":true}')
        value = "fake-" + "a" * 20
        event = NativeEvent(sender=self.lark_user.subject, text="password: " + value)
        async def downstream():
            raise ConnectionError("adapter disconnected")
        await self.gateway.run(event, downstream)
        access = event.get_extra("_ffa_memory_access")
        history = self.gateway.memory_bridge.memory().history(access)
        self.assertEqual([row["kind"] for row in history], ["message_received"])
        self.assertNotIn(value, json.dumps(history))

    async def test_outbound_revalidates_after_adapter_wait_and_reconciles_unknown(self):
        provider = self.runtime.connections[self.lark].provider
        room = convert(NativeEvent(sender=self.lark_user.subject)).conversation
        entered, resume = asyncio.Event(), asyncio.Event()

        async def guarded(target, message, allowed):
            entered.set()
            await resume.wait()
            if not allowed():
                raise NotSubmitted("revoked before dispatch")
            await provider.send(target, message)

        provider.send_checked = guarded
        task = asyncio.create_task(self.runtime.send(self.lark_user, room, Outgoing("test"), "revoked"))
        await entered.wait()
        self.runtime.identities.unlink(self.owner, self.lark_user)
        resume.set()
        with self.assertRaises(NotSubmitted):
            await task
        self.assertEqual(provider.deliveries, [])
        scope = "outbound:" + room.storage_key
        self.assertIsNone(self.runtime.seen.delivery(scope, "revoked"))

        ticket = self.runtime.identities.invite(self.owner, self.qq_user, self.lark_user)
        self.runtime.identities.confirm(self.lark_user, ticket)
        provider.fail = True
        with self.assertRaises(ConnectionError):
            await self.runtime.send(self.lark_user, room, Outgoing("test"), "unknown")
        self.assertEqual(self.runtime.seen.delivery(scope, "unknown"), "unknown")
        provider.fail = False
        self.assertFalse(await self.runtime.send(self.lark_user, room, Outgoing("test"), "unknown"))
        with self.assertRaises(ValueError):
            self.runtime.reconcile_send(self.lark_user, room, "unknown", "self-report", verify=lambda *args: None)
        self.assertTrue(self.runtime.reconcile_send(self.lark_user, room, "unknown", "fixture",
                                                    verify=lambda *args: "delivered"))
        self.assertEqual(self.runtime.seen.delivery(scope, "unknown"), "delivered")

    async def test_proactive_send_has_one_receipt_and_failed_send_has_none(self):
        event = NativeEvent(sender=self.lark_user.subject, ident="push-context")
        async def downstream():
            pass
        await self.gateway.run(event, downstream)
        target = convert(event).conversation
        self.assertTrue(await self.runtime.send(self.lark_user, target, Outgoing("推送"), "once", proactive=True))
        self.assertFalse(await self.runtime.send(self.lark_user, target, Outgoing("推送"), "once", proactive=True))
        provider = self.runtime.connections[self.lark].provider
        provider.fail = True
        with self.assertRaises(ConnectionError):
            await self.runtime.send(self.lark_user, target, Outgoing("失败"), "failed", proactive=True)
        access = event.get_extra("_ffa_memory_access")
        receipts = [row for row in self.gateway.memory_bridge.memory().history(access)
                    if row["kind"] == "message_sent"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["source"]["platform"], "lark")
        self.assertIn("主动推送", receipts[0]["summary"])
