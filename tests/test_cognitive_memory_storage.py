import asyncio
from dataclasses import replace
import json
import shutil
import sqlite3
import tempfile
import unittes
from pathlib import Path

from fat_fish.communication.model import Account, ChatKind, Conversation, MessageEvent, MessageRetraction, ProviderId
from fat_fish.memory.capture import CaptureQueue
from fat_fish.memory.cognitive_gateway import CognitiveMemoryGateway
from fat_fish.memory.erasure import ErasureLedger, LedgerError
from fat_fish.memory.policy import Audience, ScopeKind, ScopeRef
from fat_fish.memory.records import EventRecord, EvidenceRecord, ItemRecord, SourceRecord
from fat_fish.memory.recovery import replay_snapsho
from fat_fish.memory.repository import MemoryRepository
from fat_fish.memory.reply_guard import Delivery
from fat_fish.memory.retrieval import CognitiveRetrieval


class CognitiveMemoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = ErasureLedger(self.root / "erasure.jsonl", "test-install",
                                   {"one": b"1" * 32, "two": b"2" * 32}, "one")
        self.genesis = self.ledger.create()
        self.repo = MemoryRepository(self.root / "memory.sqlite3", self.ledger)
        self.repo.open(self.genesis)
        self.private = ScopeRef("fish", ScopeKind.PRIVATE, "alice")
        self.group = ScopeRef("fish", ScopeKind.GROUP, "qq/bot/group/one")
        self.public = ScopeRef("fish", ScopeKind.PUBLIC, "fish")
        self.private_audience = Audience("fish", "alice", self.private, 1)
        self.group_audience = Audience("fish", "alice", self.group, 1)

    @staticmethod
    def seed(db, scope, source_key, *, public=False):
        private_owner = None if public else "alice"
        source_id = db.execute("""INSERT INTO source(scope_id, source_key, kind, author_principal,
                producer, observed_at, ingested_at, trust_class, receipt_digest)
            VALUES (?, ?, ?, ?, 'test', 10, 10, ?, 'receipt')""",
            (scope, source_key, "public_attestation" if public else "message", private_owner,
             "verified_connector" if public else "direct_self")).lastrowid
        item = db.execute("""INSERT INTO item(scope_id,observer_ref,subject_ref,kind,slot_key,variant_key,
                state, current_rev, created_at, updated_at) VALUES
                (?, 'fish', 'subject', 'fact', ?, 'value', 'active', 1, 10, 10)""",
            (scope, source_key)).lastrowid
        db.execute("""INSERT INTO revision(item_id,rev,body,value_json,known_from,confidence,importance,
            trust_class,content_hash) VALUES (?,1,?,'{}',10,1,.5,'direct_self','digest')""",
            (item, source_key))
        db.execute("""INSERT INTO evidence(item_id,rev,source_id,role,weight,independence_key)
            VALUES (?, 1, ?, 'support', 1, 'test')""", (item, source_id))
        return source_id, item

    def test_audience_and_foreign_keys(self):
        with self.repo.write() as db:
            private_id = self.repo.scope_id(db, self.private)
            group_id = self.repo.scope_id(db, self.group)
            public_id = self.repo.scope_id(db, self.public)
            source_id, _ = self.seed(db, private_id, "private fixture")
            self.seed(db, public_id, "public fixture", public=True)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("""INSERT INTO event(scope_id,source_id,correlation_key,phase,
                    actor_kind,event_kind,occurred_at,known_at,payload_kind)
                    VALUES (?,?,'cross-scope','observed','bot','read',10,10,'test')""",
                    (group_id, source_id))
            with self.assertRaises(sqlite3.IntegrityError):
                self.seed(db, public_id, "not public")
            db.execute("DELETE FROM item WHERE slot_key='not public'")
        self.assertEqual(
            {r["body"] for r in self.repo.authorized_documents(self.private_audience, 12)},
            {"private fixture", "public fixture"})
        self.assertEqual(
            {r["body"] for r in self.repo.authorized_documents(self.group_audience, 12)},
            {"public fixture"})

    def test_backup_replays_erasure_and_rejects_older_ledger(self):
        with self.repo.write() as db:
            private_id = self.repo.scope_id(db, self.private)
            self.seed(db, private_id, "private fixture")
        old_ledger = self.ledger.path.read_bytes()
        old_anchor = self.ledger.anchor_path.read_bytes()
        snapshot = self.root / "snapshot.sqlite3"
        shutil.copyfile(self.repo.path, snapshot)
        checkpoint = self.repo.erase("source", self.private, "private fixture", 0, 15)
        recovered = MemoryRepository(snapshot, self.ledger)
        recovered.open(checkpoint)
        self.assertEqual(recovered.authorized_documents(self.private_audience, 16), ())
        self.ledger.path.write_bytes(old_ledger)
        self.ledger.anchor_path.write_bytes(old_anchor)
        with self.assertRaises(LedgerError):
            recovered.open(checkpoint)
        with self.assertRaises(LedgerError):
            recovered.authorized_documents(self.private_audience, 18)

    def test_publication_revision_and_erased_event_replay(self):
        source = SourceRecord(self.private, "event-1", "message", "qq", "direct_self",
                              "receipt", 10, author="alice")
        event = EventRecord(source, "chat-1", "message", "observed", "user", 10, "old")
        self.assertEqual(self.repo.record_event(event, 11), self.repo.record_event(event, 12))
        with self.repo.write() as db:
            source_id = db.execute("SELECT id FROM source").fetchone()[0]
        item = ItemRecord(self.private, "fish", "alice", "preference", "slot", "main", "old", {},
                          (EvidenceRecord(source_id),), "direct_self", 1, .7)
        item_id, rev = self.repo.put_item(item, None, 13)
        self.assertEqual((item_id, rev + 1), self.repo.put_item(replace(item, body="new"), rev, 14))
        self.assertEqual(self.repo.authorized_documents(self.private_audience, 14)[0]["body"], "new")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.repo.put_item(item, rev, 15)
        self.repo.erase("source", self.private, source.key, 0, 16)
        self.assertEqual(self.repo.authorized_documents(self.private_audience, 17), ())
        with self.assertRaises(PermissionError):
            self.repo.record_event(event, 17)

    def test_independent_recovery_controls_required(self):
        archive = self.root / "archive"
        archive.mkdir()
        database = archive / "private" / "copy.sqlite3"
        database.parent.mkdir()
        with self.repo.write() as db:
            self.seed(db, self.repo.scope_id(db, self.private), "private fixture")
        shutil.copyfile(self.repo.path, database)
        checkpoint = self.repo.erase("source", self.private, "private fixture", 0, 15)
        controls = self.root / "control"
        controls.mkdir()
        shutil.copyfile(self.ledger.path, controls / "erasure.jsonl")
        shutil.copyfile(self.ledger.anchor_path, controls / "erasure.jsonl.watermark")
        shutil.copyfile(self.ledger.anchor_path, controls / "trusted.watermark")
        (controls / "keys.json").write_text(json.dumps({
            "installation": self.ledger.installation, "first_key": "one",
            "keys": {name: key.hex() for name, key in self.ledger.keys.items()},
        }), encoding="utf-8")
        self.assertEqual(replay_snapshot(database, controls, archive), checkpoint)
        loaded = MemoryRepository(database, self.ledger)
        loaded.open(checkpoint)
        self.assertEqual(loaded.authorized_documents(self.private_audience, 17), ())
        (controls / "trusted.watermark").unlink()
        with self.assertRaises(LedgerError):
            replay_snapshot(database, controls, archive)

    def test_rotation_and_missing_key_close_reads(self):
        self.ledger.append("item", self.private.identity, "old", 0, 10)
        self.ledger.rotate("two", 11)
        new = self.ledger.append("item", self.private.identity, "new", 0, 12)
        records, verified, key = self.ledger.read(new)
        self.assertEqual(verified, new)
        self.assertEqual([row.key_id for row in records], ["one", "one", "two"])
        self.assertEqual(key, "two")
        self.ledger.keys.pop("one")
        with self.assertRaises(LedgerError):
            self.repo.open(new)



class CognitiveGatewayIngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_native_id_separate_sources_shared_principal_and_withdrawal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            ledger = ErasureLedger(path / "ledger.jsonl", "fixture", {"key": b"x" * 32}, "key")
            repository = MemoryRepository(path / "fixture.sqlite3", ledger)
            repository.open(ledger.create())
            queue = CaptureQueue(repository, clock=lambda: 40)
            adapter = CognitiveMemoryGateway("fish", queue)
            qq = ProviderId("qq", "q")
            lark = ProviderId("lark", "l")
            alice_qq = MessageEvent(Conversation(qq, "bot", ChatKind.PRIVATE, "a", "main"),
                                    Account(qq, "a"), "same-native-id", .01, "QQ fixture")
            alice_lark = MessageEvent(Conversation(lark, "bot", ChatKind.PRIVATE, "b", "main"),
                                      Account(lark, "b"), "same-native-id", .02, "Feishu fixture")
            self.assertEqual(adapter.audience(alice_qq, "alice", 1),
                             adapter.audience(alice_lark, "alice", 1))
            queue.start()
            self.addAsyncCleanup(queue.stop)
            self.assertTrue(adapter.on_inbound(alice_qq, "alice", lambda: True))
            self.assertTrue(adapter.on_inbound(alice_lark, "alice", lambda: True))
            async with asyncio.timeout(3):
                while queue.counts().get("persisted", 0) < 2:
                    await asyncio.sleep(.01)
            await asyncio.to_thread(adapter.on_retraction, MessageRetraction(
                alice_qq.conversation, alice_qq.sender, alice_qq.message_id), "alice", 0)
            with repository.write() as db:
                rows = db.execute("SELECT snippet FROM event ORDER BY occurred_at").fetchall()
            self.assertEqual([row[0] for row in rows], [None, "Feishu fixture"])
            await queue.stop()


    async def test_gateway_reply_rebuilds_from_live_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = ErasureLedger(root / "ledger.jsonl", "fixture", {"key": b"x" * 32}, "key")
            repository = MemoryRepository(root / "fixture.sqlite3", ledger)
            repository.open(ledger.create())
            adapter = CognitiveMemoryGateway("fish", CaptureQueue(repository))
            qq = ProviderId("qq", "q")
            incoming = MessageEvent(Conversation(qq, "bot", ChatKind.PRIVATE, "a", "main"),
                                    Account(qq, "a"), "reply-case", .01, "original")
            scope = adapter.audience(incoming, "alice", 1).conversation_scope
            with repository.write() as db:
                scope_id = repository.scope_id(db, scope)
                CognitiveMemoryStorageTests.seed(db, scope_id, "removed fragment")
                CognitiveMemoryStorageTests.seed(db, scope_id, "still-current fragment")
            guard = adapter.protected_reply(incoming, "alice", lambda: ("alice", 1), lambda: 1,
                                            clock=lambda: 20)
            network = []
            attempts = []

            async def build(original, *, allow_tools):
                snippets, stamp = adapter.prompt_snapshot(incoming, "alice", 1, 1, 20)
                attempts.append((original, allow_tools))
                return (snippets, allow_tools), stamp

            async def generate(request):
                for snippet in request[0]:
                    yield snippe
                if request[1]:
                    await asyncio.to_thread(repository.erase, "source", scope,
                                            "removed fragment", 0, 15)

            async def submit(draft, permit):
                def send():
                    network.append(draft)
                    return Delivery.SUBMITTED
                return permit(send)

            result = await guard.run(incoming.text, build, generate, submit,
                                     initial_deadline=asyncio.get_running_loop().time() + 4)
            self.assertEqual(result, Delivery.SUBMITTED)
            self.assertEqual(attempts, [("original", True), ("original", False)])
            self.assertEqual(network, ["still-current fragment"])


    async def test_lexical_and_vector_candidates_are_same_space_and_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = ErasureLedger(root / "ledger.jsonl", "fixture", {"key": b"x" * 32}, "key")
            repository = MemoryRepository(root / "fixture.sqlite3", ledger)
            repository.open(ledger.create())
            clock = 90
            current = ScopeRef("fish", ScopeKind.PRIVATE, "alice")
            other = ScopeRef("fish", ScopeKind.PRIVATE, "bob")
            for scope, body in ((current, "观鸟手册默认青绿色"), (other, "观鸟手册默认机密红色")):
                origin = SourceRecord(scope, scope.key, "message", "qq", "direct_self", "receipt", 10)
                event = EventRecord(origin, scope.key, "message", "observed", "user", 10, body)
                event_id = repository.record_event(event, 11)
                with repository.write() as db:
                    source_id = db.execute("SELECT source_id FROM event WHERE id=?", (event_id,)).fetchone()[0]
                item = ItemRecord(scope, "fish", scope.key, "preference", "bird-key", "main", body, {},
                                  (EvidenceRecord(source_id, event_id),), "direct_self", 1, .7)
                item_id, rev = repository.put_item(item, None, 12)
                audience = Audience("fish", scope.key, scope, 1)
                docs = repository.authorized_documents(audience, clock)
                self.assertTrue(repository.store_embedding(item_id, rev, docs[0]["content_hash"],
                                                           "space-a", [1, 0], 13))

            class Embedding:
                calls = 0

                async def get_embedding(self, _):
                    self.calls += 1
                    return [1, 0]

            model = Embedding()
            caller = Audience("fish", "alice", current, 1)
            retrieval = CognitiveRetrieval(repository, model, space="space-a")
            ordinary = await retrieval.recall(caller, "观鸟手册", clock, deep=False)
            self.assertEqual([hit.body for hit in ordinary.hits], ["观鸟手册默认青绿色"])
            self.assertEqual(model.calls, 0)
            self.assertEqual(ordinary.keyword_candidates, 1)
            deep = await retrieval.recall(caller, "初次参观", clock, deep=True)
            self.assertEqual([hit.body for hit in deep.hits], ["观鸟手册默认青绿色"])
            self.assertEqual(deep.hits[0].semantic_score, 1)
            mismatch = await CognitiveRetrieval(repository, model, space="space-b").recall(
                caller, "初次参观", clock, deep=True)
            self.assertEqual(mismatch.hits, ())
