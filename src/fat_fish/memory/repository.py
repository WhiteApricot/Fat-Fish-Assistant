"""Storage and replay primitives for the new cognitive memory database."""

from contextlib import contextmanager
from collections import Counter
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


# Eligibility is shared by profiles, current recall and historical recall. Every
# original source of a rendered revision must still be usable.
VISIBLE_EVIDENCE = """
    EXISTS (
        SELECT 1 FROM evidence AS e
        WHERE e.item_id = i.id AND e.rev = r.rev AND e.role = 'support'
    )
    AND NOT EXISTS (
        SELECT 1 FROM evidence AS e
        JOIN source AS s ON s.id = e.source_id
        JOIN scope AS origin ON origin.id = s.scope_id
        LEFT JOIN "grant" AS g ON g.id = e.grant_id
        WHERE e.item_id = i.id AND e.rev = r.rev AND (
            s.revoked_at IS NOT NULL OR origin.enabled = 0
            OR (s.expires_at IS NOT NULL AND s.expires_at <= :now)
            OR (e.grant_id IS NULL AND s.scope_id <> i.scope_id)
            OR (e.grant_id IS NOT NULL AND (
                g.revoked_at IS NOT NULL OR g.issued_at > :now
                OR (g.expires_at IS NOT NULL AND g.expires_at <= :now)
                OR g.source_scope <> s.scope_id OR g.target_scope <> i.scope_id
            ))
        )
    )
"""


class MemoryRepository:
    def __init__(self, path: Path, ledger: ErasureLedger):
        self.path = path
        self.ledger = ledger
        self.fence = PolicyFence()
        self._gate = Lock()
        self._ready = False
        self._read_epoch = 0

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
        with self.fence.mutation(), self._write() as db:
            yield db

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
        source = event.source
        if not all((event.key, source.key, source.producer, source.receipt_digest)):
            raise ValueError("An event requires stable source and receipt identifiers")
        if source.scope.kind == ScopeKind.PUBLIC and (
            source.kind != "public_attestation" or source.trust != "verified_connector"
            or source.author is not None or event.actor_ref is not None
        ):
            raise PermissionError("Public activity requires an independent attestation")
        payload = json.dumps(event.payload, ensure_ascii=False, sort_keys=True) if event.payload else None
        with self.write() as db:
            if not authorize():
                raise PermissionError("Memory capture authorization changed")
            scope = self.scope_id(db, source.scope)
            if not db.execute("SELECT enabled FROM scope WHERE id=?", (scope,)).fetchone()[0]:
                raise PermissionError("Memory capture scope is disabled")
            if self._blocked(db, source.scope, "source", source.key, event.time):
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
                if (extract and event.snippet and source.scope.kind == ScopeKind.PRIVATE
                        and source.author == source.scope.key and source.trust == "direct_self"):
                    self._queue_extraction(db, scope, registered["id"], row[0], now)
                return row[0]
            old = db.execute("""SELECT id, actor_kind, actor_ref, event_kind, occurred_at, snippet, payload_json
                FROM event WHERE scope_id=? AND source_id=? AND correlation_key=? AND phase=?""",
                             (scope, registered["id"], event.key, event.phase)).fetchone()
            if tuple(old)[1:] != (event.actor_kind, event.actor_ref, event.kind, event.time, event.snippet, payload):
                raise ValueError("Conflicting replay of event")
            return old[0]

    def put_item(self, item: ItemRecord, expected_revision: int | None, now: int) -> tuple[int, int]:
        with self.write() as db:
            return self._put_item(db, item, expected_revision, now)

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
            db.execute("""UPDATE revision SET known_to=? WHERE item_id=? AND rev=?""",
                       (now, item_id, expected_revision))
            db.execute("DELETE FROM search_doc WHERE item_id=? AND rev=?", (item_id, expected_revision))
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
        return db.execute("""SELECT j.*, sc.agent, sc.scope_key, src.author_principal, src.producer,
                src.source_key, src.id AS source_id, ev.id AS event_id, ev.snippet, ev.occurred_at
            FROM job j JOIN scope sc ON sc.id=j.scope_id
            JOIN job_input ji ON ji.job_id=j.id
            JOIN source src ON src.id=ji.source_id AND src.scope_id=sc.id
            JOIN event ev ON ev.id=ji.event_id AND ev.source_id=src.id
            WHERE j.id=? AND j.kind='extract' AND j.status='leased'
              AND j.lease_nonce=? AND j.lease_until>?
              AND sc.enabled=1 AND sc.kind='private' AND sc.generation=j.scope_gen
              AND src.author_principal=sc.scope_key AND src.trust_class='direct_self'
              AND src.revoked_at IS NULL AND (src.expires_at IS NULL OR src.expires_at>?)
              AND ev.snippet IS NOT NULL""", (job_id, nonce, now, now)).fetchone()

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

    def retry_extraction(self, job, now):
        with self.write() as db:
            db.execute("""UPDATE job SET status=CASE WHEN attempts>=4 THEN 'dead-letter' ELSE 'retry' END,
                eligible_at=?,updated_at=?,lease_nonce=NULL,lease_until=NULL
                WHERE id=? AND kind='extract' AND status='leased' AND lease_nonce=?""",
                       (now + 1_000 * 2 ** job["attempts"], now, job["id"], job["lease_nonce"]))

    def apply_extraction(self, job, claims, now, *, authorize):
        with self.write() as db:
            source = self._extraction_source(db, job["id"], now, job["lease_nonce"])
            if source is None or not authorize():
                return False
            scope = ScopeRef(source["agent"], ScopeKind.PRIVATE, source["scope_key"])
            proof = EvidenceRecord(source["source_id"], source["event_id"],
                                   independence_key=str(source["source_id"]))
            for claim in claims:
                if claim.quote not in source["snippet"]:
                    raise ValueError("Extraction quote is outside the claimed source")
                slot = claim.fingerprint
                if self._blocked(db, scope, "item", slot, now):
                    continue
                current = db.execute("""SELECT current_rev,state FROM item
                    WHERE scope_id=? AND observer_ref=? AND subject_ref=? AND kind=? AND slot_key=?
                    AND variant_key='claim'""",
                                     (source["scope_id"], scope.agent, scope.key, claim.kind, slot)).fetchone()
                if current and current["state"] in {"active", "deleted"}:
                    continue
                value = {"quote": claim.quote, "state": claim.state, "occurred_at": claim.occurred_at,
                         "entities": claim.entities, "episode": claim.episode,
                         "provenance": "user_report"}
                item = ItemRecord(scope, scope.agent, scope.key, claim.kind, slot, "claim", claim.body,
                                  value, (proof,), "direct_self", claim.confidence, .5,
                                  claim.valid_from, claim.valid_to)
                self._put_item(db, item, current["current_rev"] if current else None, now)
            if not authorize():
                raise PermissionError("Extraction authorization changed during publication")
            db.execute("""UPDATE job SET status='done',lease_nonce=NULL,lease_until=NULL,updated_at=?
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
                break

    @staticmethod
    def _matches(digest, key, installation, kind, *parts):
        return target_digest(key, installation, kind, *parts) == digest

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
            for source_id in matches:
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
        affected = db.execute(
            f"SELECT DISTINCT item_id FROM evidence WHERE source_id IN ({ids})", params
        ).fetchall()
        self._detach_items(db, (row[0] for row in affected))
        db.execute(f"UPDATE source SET revoked_at=? WHERE {criteria}", (now, *params))
        db.execute(f"UPDATE event SET snippet=NULL, payload_json=NULL WHERE source_id IN ({ids})", params)
        db.execute(f"""UPDATE job SET status='cancelled', lease_nonce=NULL, lease_until=NULL
            WHERE status NOT IN ('done','cancelled','dead-letter')
              AND id IN (SELECT job_id FROM job_input WHERE source_id IN ({ids}))""", params)
        db.execute(f"""UPDATE candidate SET validated_json=NULL, status='expired'
            WHERE job_id IN (SELECT job_id FROM job_input WHERE source_id IN ({ids}))""", params)
        db.execute(f"DELETE FROM job_input WHERE source_id IN ({ids})", params)

    @staticmethod
    def _detach_items(db, items, *, deleted=False):
        for item_id in list(items):
            db.execute("UPDATE item SET state=?, current_rev=NULL WHERE id=?",
                       ("deleted" if deleted else "suspended", item_id))
            db.execute("DELETE FROM revision WHERE item_id=?", (item_id,))

    def authorized_documents(self, audience: Audience, now: int, *, limit: int = 12, item_ids=None, deadline=None):
        """Read a bounded snapshot. The gateway must revalidate its stamp on send."""
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
            params = {"now": now, "limit": limit}
            for i, ref in enumerate(allowed):
                params.update({f"agent{i}": ref.agent, f"kind{i}": ref.kind.value, f"key{i}": ref.key})
            item_filter = ""
            if item_ids is not None:
                keys = ", ".join(f":item{i}" for i in range(len(item_ids)))
                item_filter = f"AND i.id IN ({keys})"
                params.update({f"item{i}": item_id for i, item_id in enumerate(item_ids)})
            rows = db.execute(f"""WITH allowed(agent, kind, scope_key) AS (VALUES {clauses})
                SELECT i.id, r.rev, r.body, r.content_hash, i.scope_id, s.generation,
                    i.kind, i.subject_ref, r.value_json, r.trust_class,
                    r.known_from, r.valid_from, r.valid_to,
                    CAST((SELECT value FROM meta WHERE key='ledger_applied_seq') AS INTEGER) AS ledger_seq
                FROM allowed a
                JOIN scope s USING(agent, kind, scope_key)
                JOIN item i ON i.scope_id=s.id
                JOIN revision r ON r.item_id=i.id AND r.rev=i.current_rev
                WHERE s.enabled=1 AND i.state='active'
                  AND (r.valid_from IS NULL OR r.valid_from <= :now)
                  AND (r.valid_to IS NULL OR r.valid_to > :now)
                  AND {VISIBLE_EVIDENCE}
                  {item_filter}
                ORDER BY i.updated_at DESC, i.id DESC LIMIT :limit""",
                              params).fetchall()
        if epoch != self._read_epoch or not self._ready:
            raise LedgerError("Erasure overlapped memory recall")
        return tuple(dict(row) for row in rows)



    def lexical_candidates(self, audience: Audience, now: int, query: tuple[str, ...], limit: int):
        """Return bounded term hits with scope and active-evidence filtering in SQL."""
        if not query or not 1 <= limit <= 48:
            return ()
        deadline = time.monotonic() + 0.10
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
                            JOIN item i ON i.id=d.item_id AND i.current_rev=d.rev
                            JOIN revision r ON r.item_id=i.id AND r.rev=d.rev
                            WHERE s.agent=:agent AND s.kind=:kind AND s.scope_key=:scope
                              AND s.enabled=1 AND i.state='active'
                              AND (r.valid_from IS NULL OR r.valid_from<=:now)
                              AND (r.valid_to IS NULL OR r.valid_to>:now)
                              AND {VISIBLE_EVIDENCE}
                            ORDER BY p.doc_id DESC LIMIT 12""",
                                          {"agent": ref.agent, "kind": ref.kind.value,
                                           "scope": ref.key, "now": now, "term": term})
                    rows = rows.fetchall()
                    # An exact rare term should survive common-term posting caps.
                    for row in rows:
                        candidates[row[0]] += 1 / len(rows)
        return self.authorized_documents(audience, now, limit=limit,
                                         item_ids=tuple(item_id for item_id, _ in candidates.most_common(limit)),
                                         deadline=deadline)



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
                      {time_clause}
                      AND (:actor IS NULL OR ev.actor_kind=:actor)
                      AND (:phase IS NULL OR ev.phase=:phase)
                      AND {VISIBLE_EVIDENCE}
                    ORDER BY {priority}, ev.id DESC, i.id DESC LIMIT :limit""",
                                  binds).fetchall()
                return tuple(dict(row) for row in rows)
        finally:
            self._gate.release()
