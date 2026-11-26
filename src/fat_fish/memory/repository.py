"""Storage and replay primitives for the new cognitive memory database."""

from contextlib import contextmanager
from collections import Counter
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import struct
from threading import Lock
import time
import uuid

from fat_fish.memory.erasure import Erasure, ErasureLedger, LedgerError, target_digest, Watermark
from fat_fish.memory.policy import Audience, ScopeKind, ScopeRef
from fat_fish.memory.records import EventRecord, EvidenceRecord, ItemRecord
from fat_fish.memory.reply_guard import PolicyFence
from fat_fish.memory.schema import initialize
from fat_fish.memory.search_text import TOKENIZER_VERSION, tokens


# Conjunctive derivations require every source. An independent assertion
# contains only fields supported in full by each of its authorized sources.
SOURCE_INELIGIBLE = """
    src.revoked_at IS NOT NULL OR origin.enabled = 0
    OR (src.expires_at IS NOT NULL AND src.expires_at <= :now)
    OR (e.grant_id IS NULL AND src.scope_id <> i.scope_id)
    OR (e.grant_id IS NOT NULL AND (
        g.revoked_at IS NOT NULL OR g.issued_at > :now
        OR (g.expires_at IS NOT NULL AND g.expires_at <= :now)
        OR g.source_scope <> src.scope_id OR g.target_scope <> i.scope_id
    ))
"""
EVIDENCE_SOURCES = """
    FROM evidence e JOIN source src ON src.id=e.source_id
    JOIN scope origin ON origin.id=src.scope_id
    LEFT JOIN "grant" g ON g.id=e.grant_id
    WHERE e.item_id=i.id AND e.rev=r.rev
"""
VISIBLE_EVIDENCE = f"""
    EXISTS (SELECT 1 {EVIDENCE_SOURCES} AND e.role='support' AND NOT ({SOURCE_INELIGIBLE}))
    AND ((i.variant_key='claim' AND json_extract(r.value_json,'$.evidence_mode')='independent')
         OR NOT EXISTS (SELECT 1 {EVIDENCE_SOURCES} AND ({SOURCE_INELIGIBLE})))
    AND (i.variant_key<>'conjunction' OR (
        (SELECT count(*) FROM evidence_dependency dep WHERE dep.item_id=i.id AND dep.rev=r.rev)>=2
        AND NOT EXISTS (
            SELECT 1 FROM evidence_dependency dep JOIN item parent ON parent.id=dep.parent_item
            JOIN revision parent_version ON parent_version.item_id=parent.id AND parent_version.rev=dep.parent_rev
            WHERE dep.item_id=i.id AND dep.rev=r.rev
              AND (parent.state<>'active' OR parent.current_rev IS NOT dep.parent_rev
                   OR parent_version.valid_from>:now OR parent_version.valid_to<=:now)
        )))
"""


def _temporal_query(now, valid_at, known_at, valid_window):
    if known_at is not None and (type(known_at) is not int or not 0 <= known_at <= now):
        raise ValueError("Invalid knowledge time")
    if valid_at is not None and (type(valid_at) is not int or valid_at < 0):
        raise ValueError("Invalid effective time")
    params = {"now": now, "valid_at": now if valid_at is None else valid_at, "known_at": known_at}
    validity = """({revision}.valid_from IS NULL OR {revision}.valid_from<=:valid_at)
        AND ({revision}.valid_to IS NULL OR {revision}.valid_to>:valid_at)"""
    if valid_window is not None:
        if valid_at is not None or not isinstance(valid_window, tuple) or len(valid_window) != 2:
            raise ValueError("Invalid effective time window")
        start, end = valid_window
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise ValueError("Invalid effective time window")
        params.update(window_start=start, window_end=end)
        validity = """({revision}.valid_from IS NOT NULL OR {revision}.valid_to IS NOT NULL)
            AND ({revision}.valid_from IS NULL OR {revision}.valid_from<:window_end)
            AND ({revision}.valid_to IS NULL OR {revision}.valid_to>:window_start)"""
    revision = "r.rev=i.current_rev"
    if known_at is not None:
        revision = "r.known_from<=:known_at AND (r.known_to IS NULL OR r.known_to>:known_at)"
    elif valid_at is not None or valid_window is not None:
        eligible = validity.format(revision="eligible")
        revision = f"""r.rev=(
            SELECT eligible.rev FROM revision eligible WHERE eligible.item_id=i.id
              AND {eligible}
            ORDER BY eligible.known_from DESC,eligible.rev DESC LIMIT 1
        )"""
    return revision, validity.format(revision="r"), params


class MemoryRepository:
    def __init__(self, path: Path, ledger: ErasureLedger):
        self.path = path
        self.ledger = ledger
        self.fence = PolicyFence()
        self._gate = Lock()
        self._ready = False
        self._read_epoch = 0
        self._write_revision = 0

    @contextmanager
    def _connect(self, *, read_only=False):
        target = self.path.resolve().as_uri() + "?mode=ro" if read_only else self.path
        db = sqlite3.connect(target, uri=read_only, timeout=0.01 if read_only else 0.14)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            db.close()

    @contextmanager
    def keep_wal_open(self):
        """Keep an idle reader while a worker uses short-lived connections."""
        self._require_open()
        with self._connect(read_only=True) as db:
            db.execute("SELECT key FROM meta LIMIT 1").fetchall()
            yield

    def open(self, trusted: Watermark) -> Watermark:
        """Replay a verified ledger before permitting reads of this database."""
        with self.fence.mutation(), self._gate:
            self._ready = False
            self._read_epoch += 1
            records, mark, _ = self.ledger.read(trusted)
            with self._connect() as db:
                if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise sqlite3.OperationalError("Cognitive memory could not enable WAL")
                initialize(db, mark.installation, uuid.uuid4().hex)
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise LedgerError("Memory database has invalid evidence references")
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    self._replay(db, records, mark)
            self._ready = True
            return mark

    @contextmanager
    def write(self):
        with self.fence.mutation():
            try:
                with self._write() as db:
                    yield db
            finally:
                self._write_revision += 1

    def provider_snapshot(self):
        """Track whether local writes overlap an evidence check spanning several reads."""
        self._require_open()
        return self._read_epoch, self._write_revision

    def require_provider_snapshot(self, snapshot):
        verdict = self.fence.validate(lambda: self._ready, lambda: self.provider_snapshot() == snapshot)
        if verdict is not None:
            raise PermissionError("Memory evidence changed before model dispatch")

    @contextmanager
    def _write(self):
        if not self._gate.acquire(timeout=0.14):
            raise sqlite3.OperationalError("Memory writer busy")
        try:
            self._require_open()
            with self._connect() as db, db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            self._gate.release()

    def _require_open(self):
        if not self._ready:
            raise LedgerError("Memory database is closed pending erasure validation")

    @staticmethod
    def scope_id(db, ref: ScopeRef) -> int:
        owner = ref.key if ref.kind == ScopeKind.PRIVATE else None
        db.execute("""INSERT INTO scope(agent, kind, scope_key, owner_principal, enabled)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(agent, kind, scope_key) DO NOTHING""",
                   (ref.agent, ref.kind.value, ref.key, owner, int(ref.kind != ScopeKind.GROUP)))
        return db.execute("SELECT id FROM scope WHERE agent=? AND kind=? AND scope_key=?",
                          (ref.agent, ref.kind.value, ref.key)).fetchone()[0]

    def set_group_memory(self, scope, enabled, *, authorize):
        if scope.kind != ScopeKind.GROUP or type(enabled) is not bool:
            raise ValueError("Group memory requires a group policy decision")
        with self.write() as db:
            if not authorize():
                raise PermissionError("Group memory policy change is unauthorized")
            scope_id = self.scope_id(db, scope)
            db.execute("""UPDATE scope SET enabled=?,generation=generation+1
                WHERE id=? AND enabled<>?""", (enabled, scope_id, enabled))
            if not authorize():
                raise PermissionError("Group memory policy authorization changed")

    def _blocked(self, db, ref, target_kind, target, observed_at):
        for key_id, key in self.ledger.keys.items():
            scope_hmac = target_digest(key, self.ledger.installation, "scope", ref.identity)
            digest = target_digest(key, self.ledger.installation, target_kind, ref.identity, target)
            public_digest = target_digest(key, self.ledger.installation, "public_asset", ref.identity, target)
            if db.execute("""SELECT 1 FROM erasure WHERE scope_hmac=? AND key_id=?
                AND ((target_kind='scope' AND requested_at>=?)
                  OR (target_kind=? AND target_hmac=?)
                  OR (?='source' AND target_kind='public_asset' AND target_hmac=?)) LIMIT 1""",
                          (scope_hmac, key_id, observed_at, target_kind, digest, target_kind,
                           public_digest)).fetchone():
                return True
        return False

    def record_event(self, event: EventRecord, now: int, *, authorize=lambda: True, extract=False) -> int:
        with self.write() as db:
            return self._record_event(db, event, now, authorize, extract)

    def record_completed_turn(self, records, now, *, authorize, deliveries=()):
        """Atomically persist the completed input, send receipts and extraction jobs."""
        records, deliveries = tuple(records), tuple(deliveries)
        if not 1 <= len(records) <= 16 or len(deliveries) > 64:
            raise ValueError("Completed turn exceeds the capture limit")
        scope, author = records[0].source.scope, records[0].source.author
        if (scope.kind not in {ScopeKind.PRIVATE, ScopeKind.GROUP} or not author
                or any(record.source.scope != scope or record.kind != "inbound"
                       or record.source.author != author or record.actor_ref != author for record in records)
                or any(receipt.source.scope != scope or receipt.kind != "delivery"
                       or receipt.phase != "submitted" or receipt.source.trust != "verified_connector"
                       for receipt in deliveries)):
            raise ValueError("Completed capture needs one authorized scope and subject")
        with self.write() as db:
            for receipt in deliveries:
                self._record_event(db, receipt, now, authorize, False)
            return tuple(self._record_event(db, record, now, authorize, True) for record in records)

    def _record_event(self, db, event, now, authorize, extract):
        source = event.source
        if not all((event.key, source.key, source.producer, source.receipt_digest)):
            raise ValueError("An event requires stable source and receipt identifiers")
        if source.scope.kind == ScopeKind.PUBLIC and (
            source.kind != "public_attestation" or source.trust != "verified_connector"
            or source.author is not None or event.actor_ref is not None
        ):
            raise PermissionError("Public activity requires an independent attestation")
        payload = json.dumps(event.payload, ensure_ascii=False, sort_keys=True) if event.payload else None
        if not authorize():
            raise PermissionError("Memory capture authorization changed")
        scope = self.scope_id(db, source.scope)
        if not db.execute("SELECT enabled FROM scope WHERE id=?", (scope,)).fetchone()[0]:
            raise PermissionError("Memory capture scope is disabled")
        if (self._blocked(db, source.scope, "source", source.key, event.time)
                or (source.scope.kind == ScopeKind.PUBLIC and event.kind == "content_experience"
                and self._blocked(db, source.scope, "public_asset",
                                  self.public_asset_key(source.producer, event.key), event.time))):
            raise PermissionError("Event source was erased")
        db.execute("""INSERT INTO source(scope_id, source_key, kind, author_principal, producer,
            observed_at, ingested_at, trust_class, receipt_digest, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_id, producer, source_key) DO NOTHING""",
                   (scope, source.key, source.kind, source.author, source.producer,
                    source.observed_at, now, source.trust, source.receipt_digest, source.expires_at))
        registered = db.execute("""SELECT * FROM source WHERE scope_id=? AND producer=? AND source_key=?""",
                                (scope, source.producer, source.key)).fetchone()
        if (registered["revoked_at"] is not None
                or registered["receipt_digest"] != source.receipt_digest
                or registered["trust_class"] != source.trust
                or registered["kind"] != source.kind
                or registered["author_principal"] != source.author
                or registered["expires_at"] != source.expires_at):
            raise PermissionError("Event cannot replace an existing source")
        row = db.execute("""INSERT INTO event(scope_id,source_id,correlation_key,phase,actor_kind,
            actor_ref,event_kind,occurred_at,known_at,snippet,payload_kind,payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_id,correlation_key,phase,source_id) DO NOTHING RETURNING id""",
                         (scope, registered["id"], event.key, event.phase, event.actor_kind,
                          event.actor_ref, event.kind, event.time, now, event.snippet,
                          event.kind, payload)).fetchone()
        if not authorize():
            raise PermissionError("Memory capture authorization changed")
        if row:
            if (extract and event.snippet and event.kind == "inbound"
                    and source.trust == "direct_self" and source.author == event.actor_ref
                    and (source.scope.kind == ScopeKind.PRIVATE and source.author == source.scope.key
                         or source.scope.kind == ScopeKind.GROUP and source.author is not None)):
                self._queue_extraction(db, scope, registered["id"], row[0], now)
            return row[0]
        old = db.execute("""SELECT id, actor_kind, actor_ref, event_kind, occurred_at, snippet, payload_json
            FROM event WHERE scope_id=? AND source_id=? AND correlation_key=? AND phase=?""",
                         (scope, registered["id"], event.key, event.phase)).fetchone()
        if tuple(old)[1:] != (event.actor_kind, event.actor_ref, event.kind, event.time, event.snippet, payload):
            raise ValueError("Conflicting replay of event")
        return old[0]

    def publish_verified_event(self, event, build_item, now, *, authorize):
        """Commit a connector receipt and its evidence-backed projection together."""
        with self.write() as db:
            event_id = self._record_event(db, event, now, authorize, False)
            origin = db.execute("SELECT scope_id,source_id FROM event WHERE id=?", (event_id,)).fetchone()
            item = build_item(EvidenceRecord(origin["source_id"], event_id,
                                             independence_key=event.source.receipt_digest))
            if item.scope != event.source.scope:
                raise PermissionError("Projection scope differs from its verified receipt")
            prior = db.execute("""SELECT id,current_rev,state FROM item WHERE scope_id=? AND observer_ref=?
                AND subject_ref=? AND kind=? AND slot_key=? AND variant_key=?""",
                (origin["scope_id"], item.observer, item.subject, item.kind, item.slot, item.variant)).fetchone()
            if prior is not None:
                if prior["state"] != "active":
                    raise PermissionError("Verified receipt projection is no longer active")
                return prior["id"], prior["current_rev"]
            if event.kind in {"work_plan", "work_receipt"}:
                prerequisites = self._advance_work(db, event, origin["scope_id"], event_id, now)
                item = replace(item, evidence=(*item.evidence, *prerequisites))
            published = self._put_item(db, item, None, now)
            if not authorize():
                raise PermissionError("Verified receipt authorization changed")
            return published

    def _advance_work(self, db, event, scope_id, event_id, now):
        prior = db.execute(f"""SELECT op.revision,e.payload_json,e.occurred_at,i.id,r.rev FROM operation op
            JOIN event e ON e.id=op.latest_event_id JOIN source s ON s.id=e.source_id
            JOIN evidence own ON own.event_id=e.id
            JOIN item i ON i.id=own.item_id AND i.current_rev=own.rev AND i.state='active'
            JOIN revision r ON r.item_id=i.id AND r.rev=own.rev
            WHERE op.scope_id=:scope AND op.producer=:producer AND op.operation_key=:key
              AND i.slot_key=s.source_key AND s.revoked_at IS NULL AND {VISIBLE_EVIDENCE}""",
            {"scope":scope_id, "producer":event.source.producer, "key":event.key, "now":now}).fetchone()
        phase = event.payload["phase"]
        expected = {"running":"planned", "submitted":"running", "tool_succeeded":"submitted",
                    "verified":"tool_succeeded"}
        previous_payload = json.loads(prior["payload_json"]) if prior else {}
        previous = previous_payload.get("phase")
        if prior and (previous_payload.get("project") != event.payload["project"]
                or (previous_payload.get("approval") is not None
                    and previous_payload["approval"] != event.payload["approval"])):
            raise ValueError("Work receipt changed an operation's project or approval")
        if phase in {"failed", "cancelled"}:
            allowed = previous in {"planned", "running", "submitted", "tool_succeeded"}
        else:
            allowed = previous == expected.get(phase) and (phase in expected or phase == "planned")
        if not allowed or (prior is not None and event.time < prior["occurred_at"]):
            raise ValueError("Work receipt does not follow the recorded operation")
        db.execute("""INSERT INTO operation(scope_id,producer,operation_key,phase,latest_event_id,revision,updated_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(scope_id,producer,operation_key) DO UPDATE SET
                phase=excluded.phase,latest_event_id=excluded.latest_event_id,
                revision=excluded.revision,updated_at=excluded.updated_at""",
            (scope_id, event.source.producer, event.key, event.phase, event_id,
             prior["revision"]+1 if prior else 1, event.time))
        if prior is None:
            return ()
        return self._independent_evidence(db, prior["id"], prior["rev"], now)

    def put_item(self, item: ItemRecord, expected_revision: int | None, now: int) -> tuple[int, int]:
        with self.write() as db:
            return self._put_item(db, item, expected_revision, now)

    def publish_conjunction(self, scope, *, slot, body, parents, expected_revision, now, authorize):
        """Publish a model-inferred conclusion whose components are jointly required."""
        if scope.kind != ScopeKind.PRIVATE or not 2 <= len(parents) <= 8:
            raise ValueError("Joint evidence requires a bounded private claim set")
        if len({item_id for item_id, _ in parents}) != len(parents):
            raise ValueError("Repeated joint evidence component")
        from fat_fish.memory.experience import sensitive_information
        if sensitive_information(body):
            raise ValueError("Sensitive conclusion")
        with self.write() as db:
            if not authorize():
                raise PermissionError("Joint evidence authorization changed")
            scope_id = self.scope_id(db, scope)
            proofs = {}
            confidence = 1.0
            component_roots = []
            for item_id, revision in parents:
                row = db.execute(f"""SELECT r.confidence FROM item i JOIN revision r
                    ON r.item_id=i.id AND r.rev=i.current_rev
                    WHERE i.id=:item AND r.rev=:revision AND i.scope_id=:scope
                      AND i.subject_ref=:subject AND i.kind IN ('fact','event') AND i.state='active'
                      AND (r.valid_from IS NULL OR r.valid_from<=:now)
                      AND (r.valid_to IS NULL OR r.valid_to>:now)
                      AND {VISIBLE_EVIDENCE}""",
                    {"item": item_id, "revision": revision, "scope": scope_id,
                     "subject": scope.key, "now": now}).fetchone()
                roots = self._independent_evidence(db, item_id, revision, now) if row else ()
                if not roots:
                    raise ValueError("Joint evidence component is missing or stale")
                component_roots.append({proof.source_id for proof in roots})
                proofs.update((proof.source_id, proof) for proof in roots)
                confidence = min(confidence, row["confidence"])
            if len(set().union(*component_roots)) < 2:
                raise ValueError("Joint evidence must include independent sources")
            item = ItemRecord(scope, scope.agent, scope.key, "derived_claim", slot, "conjunction",
                              body, {"provenance": "model_inference"}, tuple(proofs.values()),
                              "inference", confidence, .5)
            item_id, revision = self._put_item(db, item, expected_revision, now)
            db.executemany("INSERT INTO evidence_dependency VALUES (?,?,?,?,?)",
                           ((scope_id, item_id, revision, parent, parent_rev) for parent, parent_rev in parents))
            if not authorize():
                raise PermissionError("Joint evidence authorization changed")
            return item_id, revision

    def _put_item(self, db, item, expected_revision, now):
        if not item.evidence or not any(e.role == "support" for e in item.evidence):
            raise ValueError("Every memory revision requires supporting evidence")
        value_json = json.dumps(item.value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        body_hash = hashlib.sha256((item.body + "\0" + value_json).encode()).hexdigest()
        scope = self.scope_id(db, item.scope)
        if self._blocked(db, item.scope, "item", item.slot, now):
            raise PermissionError("Memory item was erased")
        key = (scope, item.observer, item.subject, item.kind, item.slot, item.variant)
        prior = db.execute("""SELECT id,state,current_rev,last_rev FROM item
            WHERE scope_id=? AND observer_ref=? AND subject_ref=? AND kind=? AND slot_key=? AND variant_key=?""",
                           key).fetchone()
        if prior is None:
            if expected_revision is not None:
                raise ValueError("Memory revision no longer exists")
            item_id = db.execute("""INSERT INTO item(scope_id, observer_ref, subject_ref, kind,
                slot_key, variant_key, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'candidate', ?, ?)""", (*key, now, now)).lastrowid
            new = 1
        else:
            if prior["current_rev"] != expected_revision or prior["state"] == "deleted":
                raise ValueError("Memory revision is stale")
            item_id = prior["id"]
            new = max(prior["last_rev"], prior["current_rev"] or 0) + 1
        if expected_revision is not None:
            previous_time = db.execute("SELECT known_from FROM revision WHERE item_id=? AND rev=?",
                                       (item_id, expected_revision)).fetchone()[0]
            now = max(now, previous_time + 1)
            db.execute("UPDATE revision SET known_to=? WHERE item_id=? AND rev=?",
                       (now, item_id, expected_revision))
            db.execute("DELETE FROM embedding WHERE doc_id IN (SELECT id FROM search_doc WHERE item_id=? AND rev=?)",
                       (item_id, expected_revision))
            db.execute("DELETE FROM profile WHERE source_item_id=? AND rev=?", (item_id, expected_revision))
        db.execute("""INSERT INTO revision(item_id,rev,body,value_json,valid_from,valid_to,known_from,
            confidence,importance,trust_class,content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (item_id, new, item.body, value_json, item.valid_from, item.valid_to,
                    now, item.confidence, item.importance, item.trust, body_hash))
        db.executemany("INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       ((item_id, new, e.source_id, e.event_id, e.role, e.grant_id,
                         e.weight, e.independence_key) for e in item.evidence))
        db.execute("UPDATE item SET state='active',current_rev=?,last_rev=?,updated_at=? WHERE id=?",
                   (new, new, now, item_id))
        posting = tokens(item.body)
        doc_id = db.execute("""INSERT INTO search_doc(item_id, rev, scope_id, kind, terms_text,
            norm_version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (item_id, new, scope, item.kind, " ".join(posting), TOKENIZER_VERSION, now)).lastrowid
        db.executemany("INSERT INTO posting VALUES (?, ?, ?, ?)",
                       ((scope, word, doc_id, count) for word, count in posting.items()))
        return item_id, new

    @staticmethod
    def _queue_extraction(db, scope_id, source_id, event_id, now):
        job_id = db.execute("""INSERT INTO job(scope_id,kind,dedup_key,priority,status,
            eligible_at,scope_gen,created_at,updated_at)
            SELECT id,'extract',?,0,'ready',?,generation,?,? FROM scope WHERE id=?
            RETURNING id""", (str(event_id), now, now, now, scope_id)).fetchone()[0]
        db.execute("INSERT INTO job_input VALUES (?,?,?)", (job_id, source_id, event_id))

    @staticmethod
    def _extraction_source(db, job_id, now, nonce):
        return db.execute("""SELECT j.*, sc.agent, sc.scope_key, sc.kind AS scope_kind, src.author_principal, src.producer,
                src.source_key, src.id AS source_id, ev.id AS event_id, ev.snippet, ev.occurred_at, ev.payload_json
            FROM job j JOIN scope sc ON sc.id=j.scope_id
            JOIN job_input ji ON ji.job_id=j.id
            JOIN source src ON src.id=ji.source_id AND src.scope_id=sc.id
            JOIN event ev ON ev.id=ji.event_id AND ev.source_id=src.id
            WHERE j.id=? AND j.kind='extract' AND j.status='leased'
              AND j.lease_nonce=? AND j.lease_until>?
              AND sc.enabled=1 AND sc.generation=j.scope_gen
              AND ((sc.kind='private' AND src.author_principal=sc.scope_key)
                   OR (sc.kind='group' AND src.author_principal=ev.actor_ref))
              AND src.trust_class='direct_self'
              AND src.revoked_at IS NULL AND (src.expires_at IS NULL OR src.expires_at>?)
              AND ev.snippet IS NOT NULL""", (job_id, nonce, now, now)).fetchone()

    def current_extraction(self, job, now):
        """Confirm the leased source still exists before a model call."""
        epoch = self._read_epoch
        self._require_open()
        with self._connect(read_only=True) as db:
            row = self._extraction_source(db, job["id"], now, job["lease_nonce"])
        if epoch != self._read_epoch or not self._ready:
            return None
        return dict(row) if row is not None else None

    def extraction_due(self, now):
        self._require_open()
        with self._connect(read_only=True) as db:
            return db.execute("""SELECT 1 FROM job
                WHERE kind='extract' AND (
                    (status IN ('ready','retry') AND eligible_at<=?)
                    OR (status='leased' AND lease_until<=?)) LIMIT 1""", (now, now)).fetchone() is not None

    def claim_extraction(self, now):
        with self.write() as db:
            db.execute("""UPDATE job SET status='dead-letter',lease_nonce=NULL,lease_until=NULL,updated_at=?
                WHERE kind='extract' AND status='leased' AND lease_until<=? AND attempts>=4""",
                       (now, now))
            nonce = uuid.uuid4().hex
            job = db.execute("""UPDATE job SET status='leased',lease_nonce=?,lease_until=?,
                    attempts=attempts+1,updated_at=?
                WHERE id=(SELECT id FROM job WHERE kind='extract' AND attempts<4 AND
                    ((status IN ('ready','retry') AND eligible_at<=?)
                      OR (status='leased' AND lease_until<=?))
                    ORDER BY eligible_at,id LIMIT 1) RETURNING id""",
                             (nonce, now + 30_000, now, now, now)).fetchone()
            if not job:
                return None
            source = self._extraction_source(db, job[0], now, nonce)
            if not source:
                db.execute("""UPDATE job SET status='cancelled',lease_nonce=NULL,
                    lease_until=NULL WHERE id=?""", (job[0],))
                return None
            return dict(source)

    def retry_extraction(self, job, now, *, reason):
        with self.write() as db:
            db.execute("""UPDATE job SET status=CASE WHEN attempts>=4 THEN 'dead-letter' ELSE 'retry' END,
                eligible_at=?,updated_at=?,last_error=?,lease_nonce=NULL,lease_until=NULL
                WHERE id=? AND kind='extract' AND status='leased' AND lease_nonce=?""",
                       (now + 1_000 * 2 ** job["attempts"], now, reason, job["id"], job["lease_nonce"]))

    def extraction_candidates(self, job, now):
        """Return bounded same-subject revisions for semantic equivalence judgment."""
        scope = ScopeRef(job["agent"], ScopeKind(job["scope_kind"]), job["scope_key"])
        audience = Audience(scope.agent, job["author_principal"], scope, 0)
        ranked = tokens(job["snippet"])
        terms = tuple(sorted(ranked, key=lambda term: (-len(term), -ranked[term], term))[:12])
        lexical = self.lexical_candidates(audience, now, terms, 12)
        recent = self.authorized_documents(audience, now, limit=12)
        selected = {}
        for rows, capacity in ((lexical, 4), (recent, 6)):
            for row in rows:
                if len(selected) >= capacity:
                    break
                if (row["scope_id"] == job["scope_id"] and row["variant_key"] == "claim"
                        and row["subject_ref"] == job["author_principal"]):
                    selected.setdefault(row["id"], row)
        current = self.authorized_documents(audience, now, item_ids=tuple(selected))
        by_id = {row["id"]: row for row in current
                 if row["rev"] == selected[row["id"]]["rev"]
                 and row["content_hash"] == selected[row["id"]]["content_hash"]}
        return tuple(by_id[item] for item in selected if item in by_id)

    @staticmethod
    def _equivalent_candidate(db, candidate, source, kind, now):
        if candidate is None:
            return None
        return db.execute(f"""SELECT i.slot_key,r.body,r.value_json,r.valid_from,r.valid_to
            FROM item i JOIN revision r ON r.item_id=i.id AND r.rev=i.current_rev
            WHERE i.id=:item AND i.scope_id=:scope AND i.subject_ref=:subject
              AND i.variant_key='claim' AND i.kind=:kind AND i.state='active'
              AND r.rev=:revision AND r.content_hash=:digest AND {VISIBLE_EVIDENCE}""",
            {"item": candidate["id"], "scope": source["scope_id"], "subject": source["author_principal"],
             "kind": kind, "revision": candidate["rev"], "digest": candidate["content_hash"],
             "now": now}).fetchone()

    @staticmethod
    def _copy_mentions(db, item_id, previous, revision):
        db.execute("""INSERT OR IGNORE INTO memory_mention(scope_id,node_id,item_id,rev,label,
                    linked_source_id,anchor_item_id,anchor_rev)
            SELECT scope_id,node_id,item_id,?,label,linked_source_id,anchor_item_id,anchor_rev
            FROM memory_mention WHERE item_id=? AND rev=?""",
                   (revision, item_id, previous))
        db.execute("""INSERT OR IGNORE INTO memory_mention_proof
            SELECT item_id,?,node_id,label,source_id,anchor_item,anchor_rev
            FROM memory_mention_proof WHERE item_id=? AND rev=?""", (revision, item_id, previous))

    def _index_mentions(self, db, published, previous, claim, candidates, source, now):
        item_id, revision = published
        if previous is not None:
            self._copy_mentions(db, item_id, previous, revision)
        requests = {(link.kind, link.label): link for link in claim.links}
        labels = [("entity", name) for name in claim.entities]
        if claim.episode:
            labels.append(("episode", claim.episode))
        for kind, label in labels:
            bindings = (source["scope_id"], item_id, revision, label, kind)
            present = db.execute("""SELECT 1 FROM memory_mention m JOIN memory_node n ON n.id=m.node_id
                WHERE m.scope_id=? AND m.item_id=? AND m.rev=? AND m.label=? AND n.kind=?""",
                                 bindings).fetchone()
            target = requests.get((kind, label))
            linked = None
            if target is not None:
                candidate = candidates.get(target.target)
                if candidate and self._equivalent_candidate(db, candidate, source, candidate["kind"], now):
                    linked = db.execute("""SELECT m.node_id FROM memory_mention m
                        JOIN memory_node n ON n.id=m.node_id AND n.scope_id=m.scope_id
                        WHERE m.scope_id=? AND m.item_id=? AND m.rev=? AND m.label=? AND n.kind=?
                          AND m.linked_source_id IS NULL""",
                        (source["scope_id"], target.target, candidate["rev"], target.target_label, kind)).fetchone()
            if not linked and present:
                continue
            node = linked[0] if linked else db.execute(
                "INSERT INTO memory_node(scope_id,kind,created_at) VALUES(?,?,?) RETURNING id",
                (source["scope_id"], kind, now)).fetchone()[0]
            provenance = (source["source_id"], target.target, candidate["rev"]) if linked else (None, None, None)
            db.execute("INSERT OR IGNORE INTO memory_mention VALUES(?,?,?,?,?,?,?,?)",
                       (source["scope_id"], node, item_id, revision, label, *provenance))
            if linked:
                db.execute("INSERT OR IGNORE INTO memory_mention_proof VALUES(?,?,?,?,?,?,?)",
                           (item_id, revision, node, label, *provenance))

    @staticmethod
    def _observation_confidence(evidence):
        positive = {entry.independence_key for entry in evidence if entry.role == "support"}
        negative = {entry.independence_key for entry in evidence if entry.role == "refute"}
        if len(positive) < 2 or positive & negative:
            return None
        return round((len(positive) + 1) / (len(positive) + len(negative) + 2), 4)

    def _apply_observation(self, db, scope, source, proof, observation, offered, now):
        if observation.quote not in source["snippet"]:
            raise ValueError("Observation quote is outside the claimed source")
        evidence = {proof.independence_key: proof}
        for role, ids in (("support", observation.support), ("refute", observation.refute)):
            for item_id in ids:
                candidate = offered.get(item_id)
                if (not candidate or candidate["kind"] not in {"fact", "event"}
                        or not self._equivalent_candidate(db, candidate, source, candidate["kind"], now)):
                    return
                roots = self._independent_evidence(db, item_id, candidate["rev"], now)
                if not roots or any(root.independence_key in evidence
                                   and evidence[root.independence_key].role != role for root in roots):
                    return
                for root in roots:
                    evidence[root.independence_key] = replace(root, role=role)
        confidence = self._observation_confidence(evidence.values())
        if confidence is None:
            return
        slot = f"{observation.axis}:{observation.key}"
        if self._blocked(db, scope, "item", slot, now):
            return
        current = db.execute("""SELECT current_rev,state FROM item WHERE scope_id=? AND observer_ref=?
            AND subject_ref=? AND kind='observation' AND slot_key=? AND variant_key='inductive'""",
            (source["scope_id"], scope.agent, source["author_principal"], slot)).fetchone()
        if current and current["state"] == "deleted":
            return
        item = ItemRecord(scope, scope.agent, source["author_principal"], "observation", slot, "inductive",
                          observation.body, {"axis": observation.axis, "provenance": "model_inference"},
                          tuple(evidence.values()), "inference", confidence, .5)
        self._put_item(db, item, current["current_rev"] if current else None, now)

    def apply_extraction(self, job, claims, now, *, authorize, candidates=(), observations=()):
        with self.write() as db:
            source = self._extraction_source(db, job["id"], now, job["lease_nonce"])
            if source is None or not authorize():
                return False
            scope = ScopeRef(source["agent"], ScopeKind(source["scope_kind"]), source["scope_key"])
            proof = EvidenceRecord(source["source_id"], source["event_id"],
                                   independence_key=str(source["source_id"]))
            offered = {row["id"]: row for row in candidates}
            for observation in observations:
                self._apply_observation(db, scope, source, proof, observation, offered, now)
            for claim in claims:
                if claim.quote not in source["snippet"]:
                    raise ValueError("Extraction quote is outside the claimed source")
                slot, body = claim.fingerprint, claim.body
                value = {"state": claim.state, "occurred_at": claim.occurred_at,
                         "entities": sorted(claim.entities), "episode": claim.episode,
                         "provenance": "user_report", "evidence_mode": "independent"}
                if claim.equivalent_to is not None and (claim.kind != "event" or claim.occurred_at and claim.episode):
                    match = self._equivalent_candidate(db, offered.get(claim.equivalent_to), source, claim.kind, now)
                    if (match and json.loads(match["value_json"]) == value
                            and (match["valid_from"], match["valid_to"]) == (claim.valid_from, claim.valid_to)):
                        slot, body = match["slot_key"], match["body"]
                if self._blocked(db, scope, "item", slot, now):
                    continue
                current = db.execute("""SELECT i.id,i.current_rev,i.state,r.value_json
                    FROM item i LEFT JOIN revision r ON r.item_id=i.id AND r.rev=i.current_rev
                    WHERE i.scope_id=? AND i.observer_ref=? AND i.subject_ref=? AND i.kind=?
                      AND i.slot_key=? AND i.variant_key='claim'""",
                    (source["scope_id"], scope.agent, source["author_principal"], claim.kind, slot)).fetchone()
                if current and current["state"] == "deleted":
                    continue
                evidence = (proof,)
                if current and current["current_rev"] is not None:
                    old_value = json.loads(current["value_json"])
                    old_value.pop("quote", None)
                    old_value["entities"] = sorted(old_value.get("entities", ()))
                    old_value["evidence_mode"] = "independent"
                    if old_value != value:
                        # A matching statement does not corroborate different context.
                        continue
                    prior = self._independent_evidence(db, current["id"], current["current_rev"], now)
                    if any(row.independence_key == proof.independence_key for row in prior):
                        continue
                    evidence = (*prior, proof)
                item = ItemRecord(scope, scope.agent, source["author_principal"], claim.kind, slot, "claim", body,
                                  value, evidence, "direct_self", claim.confidence, .5,
                                  claim.valid_from, claim.valid_to)
                previous = current["current_rev"] if current else None
                published = self._put_item(db, item, previous, now)
                self._index_mentions(db, published, previous, claim, offered, source, now)
            if not authorize():
                raise PermissionError("Extraction authorization changed during publication")
            db.execute("""UPDATE job SET status='done',lease_nonce=NULL,lease_until=NULL,last_error=NULL,updated_at=?
                WHERE id=?""", (now, source["id"]))
            return True

    def erase(self, kind: str, scope: ScopeRef, target: str, generation: int, now: int) -> Watermark:
        """Close reads during erasure. Return only after ledger and SQLite commit."""
        with self.fence.mutation(), self._gate:
            self._require_open()
            self._ready = False
            self._read_epoch += 1
            mark = self.ledger.append(kind, scope.identity, target, generation, now)
            records, _, _ = self.ledger.read(mark)
            with self._connect() as db, db:
                db.execute("BEGIN IMMEDIATE")
                self._replay(db, records, mark)
            self._ready = True
            return mark

    @staticmethod
    def _meta(db, key: str) -> str | None:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _replay(self, db, records: tuple[Erasure, ...], mark: Watermark) -> None:
        previous = int(self._meta(db, "ledger_applied_seq"))
        if not 0 <= previous <= mark.seq:
            raise LedgerError("SQLite erasure watermark disagrees with ledger")
        tail = self._meta(db, "ledger_applied_mac")
        if previous and tail != records[previous - 1].mac:
            raise LedgerError("SQLite erasure watermark is from a different history")
        for record in records[previous:]:
            if record.kind == "rotate":
                continue
            self._apply_erasure(db, record)
            db.execute("INSERT INTO erasure VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (record.seq, record.generation, record.kind, record.key_id,
                        record.target_hmac, record.scope_hmac, record.requested_at,
                        int(time.time() * 1000)))
        db.executemany("""INSERT INTO meta VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                       (("ledger_applied_seq", str(mark.seq)), ("ledger_applied_mac", mark.tail_mac)))

    def _apply_erasure(self, db, record: Erasure) -> None:
        key = self.ledger.keys[record.key_id]
        installation = self.ledger.installation
        for scope in db.execute("SELECT id, agent, kind, scope_key, generation FROM scope"):
            ref = ScopeRef(scope["agent"], ScopeKind(scope["kind"]), scope["scope_key"])
            if self._matches(record.scope_hmac, key, installation, "scope", ref.identity):
                self._erase_scope_target(db, scope, ref, record, key)
                db.execute("""DELETE FROM memory_mention WHERE linked_source_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM memory_mention_proof p WHERE p.item_id=memory_mention.item_id
                     AND p.rev=memory_mention.rev AND p.node_id=memory_mention.node_id
                     AND p.label=memory_mention.label
                )""")
                break

    @staticmethod
    def _matches(digest, key, installation, kind, *parts):
        return target_digest(key, installation, kind, *parts) == digest

    @staticmethod
    def public_asset_key(platform, content_id):
        return json.dumps((platform, content_id), ensure_ascii=False, separators=(",", ":"))

    def _erase_scope_target(self, db, scope, ref, record, key):
        scope_id = scope["id"]
        if record.kind == "scope":
            if scope["generation"] > record.generation:
                return
            db.execute("UPDATE scope SET generation=? WHERE id=?", (record.generation + 1, scope_id))
            items = db.execute("SELECT id FROM item WHERE scope_id=?", (scope_id,)).fetchall()
            self._detach_items(db, (row[0] for row in items))
            self._revoke_sources(db, scope_id, None, record.requested_at)
        elif record.kind in {"source", "public_asset"}:
            query = "SELECT id, source_key FROM source WHERE scope_id=?"
            matches = self._target_rows(db.execute(query, (scope_id,)), ref, record, key)
            if record.kind == "public_asset" and ref.kind == ScopeKind.PUBLIC:
                assets = db.execute("""SELECT DISTINCT src.id,src.producer,ev.correlation_key
                    FROM event ev JOIN source src ON src.id=ev.source_id
                    WHERE ev.scope_id=? AND ev.event_kind='content_experience'""", (scope_id,))
                identities = ((row[0], self.public_asset_key(row[1], row[2])) for row in assets)
                matches.extend(self._target_rows(identities, ref, record, key))
            for source_id in dict.fromkeys(matches):
                self._revoke_sources(db, scope_id, source_id, record.requested_at)
        elif record.kind == "item":
            query = "SELECT id, slot_key FROM item WHERE scope_id=?"
            matches = self._target_rows(db.execute(query, (scope_id,)), ref, record, key)
            self._detach_items(db, matches, deleted=True)
        elif record.kind == "grant":
            query = 'SELECT id, CAST(id AS TEXT) FROM "grant" WHERE target_scope=?'
            for grant_id in self._target_rows(db.execute(query, (scope_id,)), ref, record, key):
                affected = db.execute("SELECT DISTINCT item_id FROM evidence WHERE grant_id=?", (grant_id,))
                self._detach_items(db, (row[0] for row in affected))
                db.execute('UPDATE "grant" SET revoked_at=? WHERE id=? AND revoked_at IS NULL',
                           (record.requested_at, grant_id))

    def _target_rows(self, rows, ref, record, key):
        return [row[0] for row in rows if self._matches(
            record.target_hmac, key, self.ledger.installation, record.kind, ref.identity, row[1]
        )]

    def _revoke_sources(self, db, scope, source_id, now):
        criteria = "scope_id=?" if source_id is None else "scope_id=? AND id=?"
        params = (scope,) if source_id is None else (scope, source_id)
        ids = f"SELECT id FROM source WHERE {criteria}"
        affected = db.execute(f"""SELECT DISTINCT e.item_id FROM evidence e
            WHERE e.source_id IN ({ids})""", params).fetchall()
        db.execute(f"UPDATE source SET revoked_at=? WHERE {criteria} AND revoked_at IS NULL", (now, *params))
        db.execute(f"DELETE FROM memory_mention_proof WHERE source_id IN ({ids})", params)
        for row in affected:
            self._remove_source_provenance(db, row[0], now)
        db.execute(f"DELETE FROM operation WHERE latest_event_id IN (SELECT id FROM event WHERE source_id IN ({ids}))",
                   params)
        db.execute(f"UPDATE event SET snippet=NULL, payload_json=NULL WHERE source_id IN ({ids})", params)
        db.execute(f"""UPDATE job SET status='cancelled', lease_nonce=NULL, lease_until=NULL
            WHERE status NOT IN ('done','cancelled','dead-letter')
              AND id IN (SELECT job_id FROM job_input WHERE source_id IN ({ids}))""", params)
        db.execute(f"""UPDATE candidate SET validated_json=NULL, status='expired'
            WHERE job_id IN (SELECT job_id FROM job_input WHERE source_id IN ({ids}))""", params)
        db.execute(f"DELETE FROM job_input WHERE source_id IN ({ids})", params)

    @staticmethod
    def _independent_evidence(db, item_id, revision, now, *, refutations=False):
        rows = db.execute("""SELECT e.source_id,e.event_id,e.grant_id,e.role,e.independence_key,e.weight
            FROM evidence e JOIN source src ON src.id=e.source_id
            JOIN item i ON i.id=e.item_id AND src.scope_id=i.scope_id
            WHERE e.item_id=? AND e.rev=? AND e.grant_id IS NULL AND (e.role='support' OR ?)
              AND src.revoked_at IS NULL AND (src.expires_at IS NULL OR src.expires_at>?)""",
                          (item_id, revision, refutations, now))
        return tuple(EvidenceRecord(**dict(row)) for row in rows)

    def _remove_source_provenance(self, db, item_id, now):
        row = db.execute("""SELECT i.*,r.body,r.value_json,r.valid_from,r.valid_to,r.confidence,
                r.importance,r.trust_class,sc.agent,sc.kind AS scope_kind,sc.scope_key
            FROM item i JOIN scope sc ON sc.id=i.scope_id
            LEFT JOIN revision r ON r.item_id=i.id AND r.rev=i.current_rev
            WHERE i.id=?""", (item_id,)).fetchone()
        impacted = {entry[0] for entry in db.execute("""SELECT DISTINCT e.rev FROM evidence e
            JOIN source s ON s.id=e.source_id WHERE e.item_id=? AND s.revoked_at IS NOT NULL""", (item_id,))}
        if row["current_rev"] in impacted:
            value = json.loads(row["value_json"])
            support = ()
            confidence = row["confidence"]
            if row["variant_key"] == "claim" and value.get("evidence_mode") == "independent":
                support = self._independent_evidence(db, item_id, row["current_rev"], now)
            elif row["kind"] == "observation" and row["variant_key"] == "inductive":
                support = self._independent_evidence(db, item_id, row["current_rev"], now, refutations=True)
                confidence = self._observation_confidence(support)
            if not support or confidence is None:
                self._detach_items(db, (item_id,))
                return
            ref = ScopeRef(row["agent"], ScopeKind(row["scope_kind"]), row["scope_key"])
            item = ItemRecord(ref, row["observer_ref"], row["subject_ref"], row["kind"], row["slot_key"],
                              row["variant_key"], row["body"], value, support, row["trust_class"],
                              confidence, row["importance"], row["valid_from"], row["valid_to"])
            _, revision = self._put_item(db, item, row["current_rev"], now)
            self._copy_mentions(db, item_id, row["current_rev"], revision)
        db.executemany("DELETE FROM revision WHERE item_id=? AND rev=?",
                       ((item_id, rev) for rev in impacted))

    @staticmethod
    def _detach_items(db, items, *, deleted=False):
        for item_id in list(items):
            db.execute("UPDATE item SET state=?, current_rev=NULL WHERE id=?",
                       ("deleted" if deleted else "suspended", item_id))
            db.execute("DELETE FROM revision WHERE item_id=?", (item_id,))

    def authorized_documents(self, audience: Audience, now: int, *, limit: int = 12, item_ids=None,
                             deadline=None, valid_at=None, known_at=None, valid_window=None):
        """Recall on two time axes while applying permissions and erasure at now."""
        revision_filter, validity, params = _temporal_query(now, valid_at, known_at, valid_window)
        if not 1 <= limit <= 48:
            raise ValueError("Invalid recall budget")
        if item_ids is not None and (not item_ids or len(item_ids) > limit):
            if not item_ids:
                return ()
            raise ValueError("Too many requested memory items")
        deadline = deadline if deadline is not None else time.monotonic() + 0.10
        if deadline <= time.monotonic():
            return ()
        allowed = audience.scopes()
        epoch = self._read_epoch
        self._require_open()
        # WAL readers can use an independent committed snapshot during ordinary
        # writes. An overlapping ledger replay invalidates the whole snapshot.
        with self._connect(read_only=True) as db:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 250)
            clauses = ", ".join(f"(:agent{i}, :kind{i}, :key{i})" for i in range(len(allowed)))
            params["limit"] = limit
            for i, ref in enumerate(allowed):
                params.update({f"agent{i}": ref.agent, f"kind{i}": ref.kind.value, f"key{i}": ref.key})
            item_filter = ""
            if item_ids is not None:
                keys = ", ".join(f":item{i}" for i in range(len(item_ids)))
                item_filter = f"AND i.id IN ({keys})"
                params.update({f"item{i}": item_id for i, item_id in enumerate(item_ids)})
            rows = db.execute(f"""WITH allowed(agent, kind, scope_key) AS (VALUES {clauses})
                SELECT i.id, r.rev, r.body, r.content_hash, i.scope_id, s.generation,
                    i.kind, i.subject_ref, i.variant_key, r.value_json, r.trust_class, r.confidence,
                    r.known_from, r.valid_from, r.valid_to,
                    CAST((SELECT value FROM meta WHERE key='ledger_applied_seq') AS INTEGER) AS ledger_seq
                FROM allowed a
                JOIN scope s USING(agent, kind, scope_key)
                JOIN item i ON i.scope_id=s.id
                JOIN revision r ON r.item_id=i.id AND {revision_filter}
                WHERE s.enabled=1 AND i.state='active'
                  AND {validity}
                  AND {VISIBLE_EVIDENCE}
                  {item_filter}
                ORDER BY i.updated_at DESC, i.id DESC LIMIT :limit""",
                              params).fetchall()
        if epoch != self._read_epoch or not self._ready:
            raise LedgerError("Erasure overlapped memory recall")
        return tuple(dict(row) for row in rows)



    def lexical_candidates(self, audience: Audience, now: int, query: tuple[str, ...], limit: int,
                           *, valid_at=None, known_at=None, valid_window=None):
        """Return bounded term hits with scope and active-evidence filtering in SQL."""
        if not query or not 1 <= limit <= 48:
            return ()
        deadline = time.monotonic() + 0.10
        revision, validity, bounds = _temporal_query(now, valid_at, known_at, valid_window)
        candidates = Counter()
        self._require_open()
        # The posting scan yields only opaque IDs from a committed WAL snapshot.
        # Readable bodies still come from authorized_documents under the live gate.
        with self._connect(read_only=True) as db:
            db.execute("BEGIN")
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 250)
            for ref in audience.scopes():
                for term in query[:12]:
                    rows = db.execute(f"""SELECT i.id FROM scope s
                            JOIN posting p ON p.scope_id=s.id AND p.term=:term
                            JOIN search_doc d ON d.id=p.doc_id AND d.scope_id=s.id
                            JOIN item i ON i.id=d.item_id
                            JOIN revision r ON r.item_id=i.id AND r.rev=d.rev
                            WHERE s.agent=:agent AND s.kind=:kind AND s.scope_key=:scope
                              AND s.enabled=1 AND i.state='active'
                              AND {revision}
                              AND {validity}
                              AND {VISIBLE_EVIDENCE}
                            ORDER BY p.doc_id DESC LIMIT 12""",
                                          {"agent": ref.agent, "kind": ref.kind.value,
                                           "scope": ref.key, "term": term, **bounds})
                    rows = rows.fetchall()
                    # An exact rare term should survive common-term posting caps.
                    for row in rows:
                        candidates[row[0]] += 1 / len(rows)
        return self.authorized_documents(audience, now, limit=limit,
                                         item_ids=tuple(item_id for item_id, _ in candidates.most_common(limit)),
                                         deadline=deadline, valid_at=valid_at, known_at=known_at,
                                         valid_window=valid_window)



    def graph_candidates(self, audience, now, seed_ids, *, limit=12, hops=1):
        """Bounded traversal across currently attributable entities and episodes."""
        if not seed_ids or not 1 <= limit <= 12 or not 1 <= hops <= 2:
            return ()
        epoch = self._read_epoch
        deadline = time.monotonic() + .12
        frontier = tuple(dict.fromkeys(seed_ids))[:12]
        excluded = set(frontier)
        result = {}
        for level in range(hops):
            if not frontier or time.monotonic() >= deadline or len(result) >= limit:
                break
            budget = limit - len(result)
            if level == 0 and hops == 2:
                budget = max(1, (budget + 1) // 2)
            rows = self._graph_hop(audience, now, frontier, limit=min(12, budget + len(excluded)),
                                   deadline=deadline)
            frontier = tuple(row["id"] for row in rows if row["id"] not in excluded)[:budget]
            accepted = set(frontier)
            result.update((row["id"], row) for row in rows if row["id"] in accepted)
            excluded.update(accepted)
        if self._read_epoch != epoch or not self._ready:
            raise LedgerError("Erasure overlapped graph traversal")
        live = self.authorized_documents(audience, now, item_ids=tuple(result), limit=limit, deadline=deadline)
        by_id = {row["id"]: row for row in live
                 if (row["rev"], row["content_hash"]) == (result[row["id"]]["rev"],
                                                          result[row["id"]]["content_hash"])}
        return tuple(by_id[item] for item in result if item in by_id)

    def _graph_hop(self, audience, now, seed_ids, *, limit, deadline):
        seeds = self.authorized_documents(audience, now, item_ids=tuple(dict.fromkeys(seed_ids))[:12],
                                          deadline=deadline)
        if not seeds:
            return ()
        epoch = self._read_epoch
        self._require_open()
        args = [value for row in seeds for value in (row["scope_id"], row["id"], row["rev"])]
        def attributable(mention):
            return f"""({mention}.linked_source_id IS NULL OR EXISTS (
                SELECT 1 FROM memory_mention_proof p JOIN source link_source ON link_source.id=p.source_id
                JOIN item i ON i.id=p.anchor_item
                JOIN revision r ON r.item_id=i.id AND r.rev=p.anchor_rev
                WHERE p.item_id={mention}.item_id AND p.rev={mention}.rev AND p.node_id={mention}.node_id
                  AND p.label={mention}.label AND link_source.scope_id={mention}.scope_id
                  AND link_source.revoked_at IS NULL
                  AND (link_source.expires_at IS NULL OR link_source.expires_at>:now)
                  AND i.scope_id={mention}.scope_id AND i.state='active' AND {VISIBLE_EVIDENCE}
            ))"""

        params = {f"seed{i}": value for i, value in enumerate(args)}
        params.update(now=now, limit=limit)
        values = ",".join(f"(:seed{i},:seed{i+1},:seed{i+2})" for i in range(0, len(args), 3))
        with self._connect(read_only=True) as db:
            cutoff = min(deadline, time.monotonic() + .08)
            db.set_progress_handler(lambda: int(time.monotonic() >= cutoff), 250)
            rows = db.execute(f"""WITH seed(scope_id,item_id,rev) AS (VALUES {values})
                SELECT neighbor.item_id, MAX(n.kind='episode') AS same_episode, count(*) AS shared
                FROM seed
                JOIN item live ON live.id=seed.item_id AND live.current_rev=seed.rev AND live.state='active'
                JOIN memory_mention anchor ON anchor.scope_id=seed.scope_id
                  AND anchor.item_id=seed.item_id AND anchor.rev=seed.rev
                JOIN memory_node n ON n.scope_id=anchor.scope_id AND n.id=anchor.node_id
                JOIN memory_mention neighbor ON neighbor.scope_id=anchor.scope_id AND neighbor.node_id=anchor.node_id
                JOIN item i ON i.id=neighbor.item_id AND i.current_rev=neighbor.rev AND i.state='active'
                WHERE neighbor.item_id NOT IN (SELECT item_id FROM seed)
                  AND {attributable("anchor")} AND {attributable("neighbor")}
                GROUP BY neighbor.item_id
                ORDER BY same_episode DESC,shared DESC,neighbor.item_id DESC LIMIT :limit""", params).fetchall()
        if epoch != self._read_epoch or not self._ready:
            raise LedgerError("Erasure overlapped graph recall")
        ordered = [row["item_id"] for row in rows]
        allowed = self.authorized_documents(audience, now, item_ids=tuple(ordered), limit=limit, deadline=deadline)
        by_id = {row["id"]: row for row in allowed}
        if epoch != self._read_epoch or not self._ready:
            raise LedgerError("Erasure overlapped graph recall")
        return tuple(by_id[item] for item in ordered if item in by_id)

    def store_embedding(self, item_id: int, revision: int, digest: str, space: str, vector, now: int) -> bool:
        if not 1 <= len(vector) <= 4096 or not all(math.isfinite(value) for value in vector):
            raise ValueError("Invalid embedding vector")
        norm = math.hypot(*vector)
        if not norm or not math.isfinite(norm):
            raise ValueError("Empty embedding vector")
        raw = struct.pack(f"<{len(vector)}f", *(value / norm for value in vector))
        with self.write() as db:
            row = db.execute(f"""SELECT d.id FROM search_doc d
                JOIN item i ON i.id=d.item_id AND i.current_rev=d.rev
                JOIN revision r ON r.item_id=i.id AND r.rev=d.rev
                JOIN scope s ON s.id=d.scope_id
                WHERE i.id=:item AND r.rev=:rev AND r.content_hash=:digest
                  AND s.enabled=1 AND i.state='active' AND {VISIBLE_EVIDENCE}""",
                             {"item": item_id, "rev": revision, "digest": digest, "now": now}).fetchone()
            if not row:
                return False
            db.execute("""INSERT INTO embedding VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_id,space) DO UPDATE SET content_hash=excluded.content_hash,
                dimension=excluded.dimension,vector=excluded.vector,generated_at=excluded.generated_at,
                index_generation=excluded.index_generation""",
                       (row[0], space, digest, len(vector), raw, now, int(self._meta(db, "index_generation"))))
            return True

    def semantic_candidates(self, audience: Audience, now: int, space: str, vector):
        """Exact cosine within a bounded, authorized model-space window."""
        if not self._gate.acquire(timeout=0.01):
            raise sqlite3.OperationalError("Memory recall writer-busy budget exhausted")
        started = time.monotonic()
        matches = []
        try:
            self._require_open()
            with self._connect(read_only=True) as db:
                db.set_progress_handler(lambda: int(time.monotonic() - started >= .07), 250)
                scopes = audience.scopes()
                per_scope = 384 // len(scopes)
                for ref in scopes:
                    rows = db.execute(f"""SELECT i.id, r.rev, r.content_hash, r.body, e.vector
                        FROM scope s
                        JOIN item i ON i.scope_id=s.id
                        JOIN revision r ON r.item_id=i.id AND r.rev=i.current_rev
                        JOIN search_doc d ON d.item_id=i.id AND d.rev=r.rev
                        JOIN embedding e ON e.doc_id=d.id AND e.content_hash=r.content_hash
                        WHERE s.agent=:agent AND s.kind=:kind AND s.scope_key=:scope
                          AND s.enabled=1 AND i.state='active'
                          AND (r.valid_from IS NULL OR r.valid_from<=:now)
                          AND (r.valid_to IS NULL OR r.valid_to>:now)
                          AND e.space=:space AND e.dimension=:dimension AND {VISIBLE_EVIDENCE}
                        ORDER BY i.updated_at DESC, i.id DESC LIMIT :limit""",
                                      {"agent": ref.agent, "kind": ref.kind.value, "scope": ref.key,
                                       "now": now, "space": space, "dimension": len(vector), "limit": per_scope})
                    for row in rows:
                        if time.monotonic() - started >= .075:
                            raise TimeoutError("Semantic scoring budget exhausted")
                        parts = struct.unpack(f"<{len(vector)}f", row["vector"])
                        score = sum(a * b for a, b in zip(vector, parts))
                        if math.isfinite(score):
                            matches.append(({k: row[k] for k in ("id", "rev", "content_hash", "body")}, score))
        finally:
            self._gate.release()
        return sorted(matches, key=lambda entry: (-entry[1], entry[0]["id"]))[:48]



    def event_candidates(self, audience: Audience, now: int, *, actor=None, phase=None, limit=48,
                         preferred_ids=(), public_only=False, occurred_window=None):
        """Return only events that support a currently visible revision."""
        if not 1 <= limit <= 48:
            raise ValueError("Invalid event candidate budget")
        allowed = audience.scopes()
        if public_only:
            allowed = tuple(scope for scope in allowed if scope.kind == ScopeKind.PUBLIC)
        if not self._gate.acquire(timeout=.01):
            raise sqlite3.OperationalError("Memory event writer-busy budget exhausted")
        deadline = time.monotonic() + .085
        try:
            self._require_open()
            with self._connect(read_only=True) as db:
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 250)
                clauses = ", ".join(f"(:ag{i}, :ki{i}, :sc{i})" for i in range(len(allowed)))
                binds = {"now": now, "actor": actor, "phase": phase, "limit": limit}
                time_clause = ""
                if occurred_window is not None:
                    binds["earliest"], binds["latest"] = occurred_window
                    time_clause = "AND ev.occurred_at>=:earliest AND ev.occurred_at<:latest"
                preferred = tuple(dict.fromkeys(preferred_ids))[:16]
                priority = "ev.occurred_at DESC"
                if preferred:
                    placeholders = ", ".join(f":preferred{i}" for i in range(len(preferred)))
                    binds.update({f"preferred{i}": value for i, value in enumerate(preferred)})
                    priority = f"(i.id IN ({placeholders})) DESC, {priority}"
                for i, scope in enumerate(allowed):
                    binds.update({f"ag{i}": scope.agent, f"ki{i}": scope.kind.value, f"sc{i}": scope.key})
                rows = db.execute(f"""WITH allowed(agent, kind, scope_key) AS (VALUES {clauses})
                    SELECT i.id, r.rev, r.content_hash, r.body, r.confidence,
                        ev.id AS event_id, ev.event_kind, ev.actor_kind, ev.phase,
                        ev.occurred_at, ev.known_at, se.id AS source_id, se.trust_class,
                        ev.scope_id AS event_scope_id, se.producer, ev.payload_json
                    FROM allowed a JOIN scope s USING(agent, kind, scope_key)
                    JOIN item i ON i.scope_id=s.id
                    JOIN revision r ON r.item_id=i.id AND r.rev=i.current_rev
                    JOIN evidence e ON e.item_id=i.id AND e.rev=r.rev AND e.role='support'
                    JOIN source se ON se.id=e.source_id
                    JOIN event ev ON ev.id=e.event_id AND ev.source_id=e.source_id
                    WHERE s.enabled=1 AND i.state='active'
                      AND (r.valid_from IS NULL OR r.valid_from<=:now)
                      AND (r.valid_to IS NULL OR r.valid_to>:now)
                      AND ev.known_at<=:now AND ev.occurred_at<=:now
                      AND se.revoked_at IS NULL AND (se.expires_at IS NULL OR se.expires_at>:now)
                      {time_clause}
                      AND (:actor IS NULL OR ev.actor_kind=:actor)
                      AND (:phase IS NULL OR ev.phase=:phase)
                      AND {VISIBLE_EVIDENCE}
                    ORDER BY {priority}, ev.id DESC, i.id DESC LIMIT :limit""",
                                  binds).fetchall()
                return tuple(dict(row) for row in rows)
        finally:
            self._gate.release()
