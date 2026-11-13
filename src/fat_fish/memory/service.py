"""SQLite activity ledger and permission-filtered, versioned recall."""
from contextlib import closing, contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import re
import sqlite3
from threading import Lock
from zoneinfo import ZoneInfo
import time
import unicodedata

from fat_fish.communication.model import identifier
from fat_fish.memory.activity import ActivityKind, ActivityState, TRANSITIONS
from fat_fish.memory import card
from fat_fish.memory.experience import contains_secret, sensitive_information


logger = logging.getLogger(__name__)
GREETING = re.compile(r"^(?:(?:你好|您好|嗨|哈[啰喽]|早[安上好]*|晚安|hi|hello|hey)[呀啊哦\s]*)[！!。.…~～]*$", re.I)


SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    ref INTEGER PRIMARY KEY,
    producer TEXT NOT NULL, event_id TEXT NOT NULL, digest TEXT NOT NULL,
    scope TEXT NOT NULL, actor TEXT NOT NULL, source TEXT NOT NULL,
    kind TEXT NOT NULL, state TEXT NOT NULL, occurred REAL NOT NULL,
    received REAL NOT NULL, summary TEXT NOT NULL, evidence TEXT NOT NULL,
    operation_id TEXT, UNIQUE (producer, event_id)
);
CREATE INDEX IF NOT EXISTS activities_scope_time ON activities(scope, occurred DESC, ref DESC);
CREATE TABLE IF NOT EXISTS operations (
    producer TEXT NOT NULL, operation_id TEXT NOT NULL,
    scope TEXT NOT NULL, actor TEXT NOT NULL, source TEXT NOT NULL,
    kind TEXT NOT NULL, intent TEXT NOT NULL, state TEXT NOT NULL,
    occurred REAL NOT NULL, ref INTEGER NOT NULL REFERENCES activities(ref),
    PRIMARY KEY (producer, operation_id)
);
CREATE TABLE IF NOT EXISTS facts (
    scope TEXT NOT NULL, fact_key TEXT NOT NULL, actor TEXT NOT NULL,
    revision INTEGER NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY (scope, fact_key)
);
CREATE TABLE IF NOT EXISTS fact_versions (
    scope TEXT NOT NULL, fact_key TEXT NOT NULL, revision INTEGER NOT NULL,
    content TEXT, normalized TEXT NOT NULL, importance REAL NOT NULL,
    occurred REAL NOT NULL, ref INTEGER NOT NULL REFERENCES activities(ref),
    PRIMARY KEY (scope, fact_key, revision)
);
CREATE INDEX IF NOT EXISTS fact_versions_evidence ON fact_versions(ref);
CREATE TABLE IF NOT EXISTS legacy_refs (
    scope TEXT NOT NULL, session TEXT NOT NULL, persona TEXT NOT NULL,
    row_id INTEGER NOT NULL, doc_id TEXT NOT NULL,
    PRIMARY KEY (scope, session, persona, row_id)
);
"""


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def terms(query):
    normalized = normalize(query)
    result = set(re.findall(r"[^\W\d_]+|\d+", normalized))
    for block in re.findall(r"[\u3400-\u9fff]+", normalized):
        result.update(block[i:i + 2] for i in range(len(block) - 1))
    return sorted(result, key=lambda word: (-len(word), word))[:24]


def relevance(words, text):
    return sum(min(len(word), 8) for word in words if word in text)


def bm25(words, texts):
    """Score the scoped candidate set using term frequency and document length."""
    if not texts:
        return []
    lengths = [max(1, len(text)) for text in texts]
    average = sum(lengths) / len(lengths)
    counts = [[text.count(word) for word in words] for text in texts]
    frequency = [sum(count > 0 for count in column) for column in zip(*counts)]
    weights = [math.log1p((len(texts) - count + 0.5) / (count + 0.5)) for count in frequency]
    scores = []
    for length, row in zip(lengths, counts):
        length_adjustment = 1.2 * (0.25 + 0.75 * length / average)
        scores.append(sum(weight * count * 2.2 / (count + length_adjustment)
                          for weight, count in zip(weights, row)))
    return scores


def parse_timezone(value):
    if value in (None, "UTC"):
        return timezone.utc
    offset = re.fullmatch(r"([+-])([01][0-9]|2[0-3]):([0-5][0-9])", value)
    if offset:
        sign, hours, minutes = offset.groups()
        return timezone(timedelta(minutes=(1 if sign == "+" else -1) * (60 * int(hours) + int(minutes))))
    return ZoneInfo(value)


def content_digest(text):
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def without_erased_sentences(summary, erased):
    parts = re.findall(r"[^。！？.!?\n]+[。！？.!?]*|\n+", summary)
    return "".join(part for part in parts if not any(value in normalize(part) for value in erased)).strip()


def validate_erasures(records):
    if not isinstance(records, list):
        raise ValueError("Invalid memory erasure records")
    validated = []
    for record in records:
        if not isinstance(record, list) or len(record) != 2:
            raise ValueError("Invalid memory erasure record")
        scope, digest = record
        if not isinstance(scope, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid memory erasure record")
        address = json.loads(scope)
        if not isinstance(address, list) or len(address) != 2 or encode(address) != scope:
            raise ValueError("Invalid erased memory scope")
        kind, subject = address
        if kind not in {"private", "group"} or not isinstance(subject, str) or not subject:
            raise ValueError("Invalid erased memory scope")
        validated.append((scope, digest))
    return validated


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class MemoryService:
    def __init__(self, path, *, clock=time.time, card_max_chars=0):
        self.path = path
        self.clock = clock
        self.card_max_chars = card_max_chars
        self._schema_lock = Lock()
        self._write_lock = Lock()
        self._ready = False

    @contextmanager
    def transaction(self, *, write=False, timeout=10):
        with self._writer(write, timeout), closing(sqlite3.connect(self.path, timeout=timeout)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")
            if not self._ready:
                with self._schema_lock:
                    if not self._ready:
                        self._migrate(db)
                        self._ready = True
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    @contextmanager
    def _writer(self, write, timeout):
        if not write:
            yield
            return
        if not self._write_lock.acquire(timeout=timeout if timeout < 1 else -1):
            raise sqlite3.OperationalError("Memory writer busy")
        try:
            yield
        finally:
            self._write_lock.release()

    @staticmethod
    def _migrate(db):
        db.execute("PRAGMA journal_mode=WAL").fetchone()
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if not 0 <= version <= 14:
            raise ValueError("Unsupported memory database version")
        if version == 0:
            for statement in SCHEMA.strip().split(";"):
                if statement.strip():
                    db.execute(statement)
            version = 1
        if version == 1:
            db.execute("ALTER TABLE activities ADD COLUMN redacted INTEGER NOT NULL DEFAULT 0")
            db.execute("""UPDATE activities SET summary='',evidence='',redacted=1 WHERE ref IN (
                SELECT v.ref FROM fact_versions v JOIN facts f USING(scope,fact_key)
                WHERE f.status='deleted')""")
            db.execute("""CREATE TABLE agent_approvals (
                event_id TEXT PRIMARY KEY, producer TEXT NOT NULL, operation_id TEXT NOT NULL,
                intent TEXT NOT NULL, approver TEXT NOT NULL, approved REAL NOT NULL,
                expires REAL NOT NULL, consumed REAL,
                UNIQUE(producer, operation_id)
            )""")
            version = 2
        if version == 2:
            db.execute("ALTER TABLE activities ADD COLUMN turn_id TEXT")
            db.execute("CREATE INDEX activities_turn ON activities(scope,turn_id) WHERE turn_id IS NOT NULL")
            db.execute("CREATE INDEX activities_author ON activities(scope,actor)")
            db.execute("""CREATE TABLE fact_review (
                scope TEXT NOT NULL, fact_key TEXT NOT NULL, revision INTEGER NOT NULL,
                last_seen REAL NOT NULL, count INTEGER NOT NULL,
                PRIMARY KEY (scope,fact_key,revision),
                FOREIGN KEY (scope,fact_key,revision) REFERENCES fact_versions
            )""")
            db.execute("""CREATE TABLE fact_vectors (
                scope TEXT NOT NULL, fact_key TEXT NOT NULL, revision INTEGER NOT NULL,
                model TEXT NOT NULL, vector TEXT NOT NULL,
                PRIMARY KEY (scope,fact_key,revision),
                FOREIGN KEY (scope,fact_key,revision) REFERENCES fact_versions
            )""")
            version = 3
        if version == 3:
            db.execute("""CREATE TABLE agent_write_claims (
                producer TEXT NOT NULL, operation_id TEXT NOT NULL, source TEXT NOT NULL,
                target TEXT NOT NULL, claimed REAL NOT NULL,
                PRIMARY KEY (producer, operation_id)
            )""")
            db.execute("CREATE INDEX agent_write_time ON agent_write_claims(source,claimed)")
            version = 4
        if version == 4:
            db.execute("""CREATE TABLE fact_vectors_v5 (
                scope TEXT NOT NULL, fact_key TEXT NOT NULL, revision INTEGER NOT NULL,
                model TEXT NOT NULL, vector TEXT NOT NULL,
                PRIMARY KEY (scope,fact_key,revision,model),
                FOREIGN KEY (scope,fact_key,revision) REFERENCES fact_versions
            )""")
            db.execute("INSERT INTO fact_vectors_v5 SELECT scope,fact_key,revision,model,vector FROM fact_vectors")
            db.execute("DROP TABLE fact_vectors")
            db.execute("ALTER TABLE fact_vectors_v5 RENAME TO fact_vectors")
            db.execute("""CREATE TABLE memory_erasures (
                scope TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY (scope,digest)
            )""")
            version = 5
        if version == 5:
            db.execute("""CREATE TABLE memory_maintenance (
                scope TEXT PRIMARY KEY, day INTEGER NOT NULL
            )""")
            version = 6
        if version == 6:
            db.execute("CREATE TABLE memory_erasure_epochs (scope TEXT PRIMARY KEY, epoch INTEGER NOT NULL)")
            version = 7
        if version == 7:
            db.execute("""CREATE TABLE activity_vectors (
                ref INTEGER NOT NULL REFERENCES activities(ref) ON DELETE CASCADE,
                model TEXT NOT NULL, vector TEXT NOT NULL, PRIMARY KEY(ref,model)
            )""")
            db.execute("""CREATE TRIGGER invalidate_activity_vector
                AFTER UPDATE OF summary, redacted ON activities
                WHEN NEW.summary<>OLD.summary OR NEW.redacted<>OLD.redacted
                BEGIN DELETE FROM activity_vectors WHERE ref=OLD.ref; END""")
            version = 8
        if version == 8:
            db.execute("""CREATE TABLE legacy_source_owners (
                session TEXT PRIMARY KEY, scope TEXT
            )""")
            db.execute("""INSERT INTO legacy_source_owners
                SELECT session,CASE WHEN COUNT(DISTINCT scope)=1 THEN MIN(scope) END
                FROM legacy_refs GROUP BY session""")
            db.execute("""CREATE TABLE memory_runtime_status (
                id INTEGER PRIMARY KEY CHECK(id=1), occurred REAL NOT NULL,
                pending TEXT NOT NULL, failure TEXT
            )""")
            version = 9
        if version == 9:
            db.execute("""CREATE TABLE memory_scope_preferences (
                scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                changed_by TEXT NOT NULL, changed_at REAL NOT NULL
            )""")
            version = 10
        if version == 10:
            for statement in card.SCHEMA:
                db.execute(statement)
            version = 11
        if version == 11:
            db.execute("ALTER TABLE memory_card ADD COLUMN inherited_tags TEXT NOT NULL DEFAULT '[]'")
            db.execute("CREATE TABLE memory_card_backfill (id INTEGER PRIMARY KEY CHECK(id=1),ref INTEGER NOT NULL)")
            db.execute("INSERT INTO memory_card_backfill VALUES (1,0)")
            db.execute("CREATE TABLE memory_card_revoked_evidence (ref INTEGER PRIMARY KEY)")
            version = 12
        if version == 12:
            db.execute("CREATE TABLE IF NOT EXISTS memory_card_sharing (scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL)")
            db.execute("DROP TRIGGER IF EXISTS prevent_card_backfill_after_erasure")
            db.execute("DROP TRIGGER erase_card_evidence")
            db.execute("""CREATE TRIGGER erase_card_evidence AFTER UPDATE OF summary,redacted ON activities
                WHEN NEW.summary<>OLD.summary OR NEW.redacted<>OLD.redacted
                BEGIN
                    INSERT OR IGNORE INTO memory_card_revoked_evidence
                    SELECT s.ref FROM memory_card_sources s WHERE s.card IN
                        (SELECT card FROM memory_card_sources WHERE ref=OLD.ref);
                    INSERT OR IGNORE INTO memory_card_revoked_evidence VALUES (OLD.ref);
                    DELETE FROM memory_card WHERE id IN
                        (SELECT card FROM memory_card_sources WHERE ref=OLD.ref);
                END""")
            version = 13
        if version == 13:
            db.execute("DROP TRIGGER erase_card_evidence")
            db.execute("""CREATE TRIGGER erase_card_evidence AFTER UPDATE OF summary,redacted ON activities
                WHEN NEW.summary<>OLD.summary OR NEW.redacted<>OLD.redacted
                BEGIN
                    INSERT OR IGNORE INTO memory_card_revoked_evidence VALUES (OLD.ref);
                    INSERT OR IGNORE INTO memory_card_revoked_evidence
                    SELECT s.ref FROM memory_card_sources s WHERE NEW.redacted=1 AND s.card IN
                        (SELECT card FROM memory_card_sources WHERE ref=OLD.ref);
                    DELETE FROM memory_card WHERE NEW.redacted=1 AND id IN
                        (SELECT card FROM memory_card_sources WHERE ref=OLD.ref);
                END""")
            db.execute("PRAGMA user_version=14")
        db.commit()

    def backfill_cards(self, owner_id, check_enabled, *, limit=200):
        check_enabled()
        with self.transaction(write=True, timeout=0.1) as db:
            count = card.backfill(db, owner_id, limit=limit)
            check_enabled()
            return count

    def card_sharing(self, scope):
        with self.transaction() as db:
            row = db.execute("SELECT enabled FROM memory_card_sharing WHERE scope=?", (scope.storage_key,)).fetchone()
            return bool(row and row["enabled"])

    def set_card_sharing(self, access, enabled):
        access.require(write=True, moderate=True)
        with self.transaction(write=True) as db:
            db.execute("INSERT OR REPLACE INTO memory_card_sharing VALUES (?,?)",
                       (access.scope.storage_key, int(enabled)))
            access.require(write=True, moderate=True)

    def scope_enabled(self, scope):
        with self.transaction() as db:
            row = db.execute("SELECT enabled FROM memory_scope_preferences WHERE scope=?",
                             (scope.storage_key,)).fetchone()
        return scope.kind == "private" if row is None else bool(row["enabled"])

    def set_scope_enabled(self, access, enabled):
        moderate = access.scope.kind == "group"
        access.require(write=True, moderate=moderate)
        with self.transaction(write=True) as db:
            db.execute("INSERT OR REPLACE INTO memory_scope_preferences VALUES (?,?,?,?)",
                       (access.scope.storage_key, int(enabled), access.principal, self.clock()))
            access.require(write=True, moderate=moderate)

    def record(self, access, activity, *, producer="core", source=None, approval=None,
               consume_approval=False, turn_id=None, write_limit=None, card_capture=None):
        access.require(write=True)
        identifier(producer)
        scope = access.scope.storage_key
        origin = source or access.source
        source = encode(asdict(origin))
        if turn_id is not None:
            identifier(turn_id)
        payload = encode([scope, access.principal, source, asdict(activity)])
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.transaction(write=True) as db:
            if (producer == "core" and activity.kind is ActivityKind.MESSAGE_RECEIVED
                    and db.execute("SELECT 1 FROM memory_erasures WHERE scope=? AND digest=?",
                                   (scope, self._retraction_digest(access.principal, activity.event_id))).fetchone()):
                raise PermissionError("Inbound evidence was retracted")
            row = db.execute("SELECT ref,digest FROM activities WHERE producer=? AND event_id=?",
                             (producer, activity.event_id)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise ValueError("Event ID already identifies different data")
                access.require(write=True)
                return row["ref"], False

            operation = None
            if activity.operation_id:
                operation = db.execute("SELECT * FROM operations WHERE producer=? AND operation_id=?",
                                       (producer, activity.operation_id)).fetchone()
                self._validate_transition(operation, access, activity, source)
            if approval is not None:
                event_id, approver, expires = approval
                db.execute("""INSERT INTO agent_approvals
                    VALUES (?,?,?,?,?,?,?,NULL)""",
                           (event_id, producer, activity.operation_id, activity.intent, approver,
                            activity.occurred, expires))
            if consume_approval:
                consumed = db.execute("""UPDATE agent_approvals SET consumed=? WHERE producer=?
                    AND operation_id=? AND intent=? AND consumed IS NULL AND expires>?""",
                    (activity.occurred, producer, activity.operation_id, activity.intent, activity.occurred))
                if consumed.rowcount != 1:
                    raise PermissionError("Approval absent, expired or consumed")
            if write_limit is not None:
                target, maximum, window, cooldown = write_limit
                since = activity.occurred - max(window, cooldown)
                account = encode([origin.platform, origin.provider, origin.account])
                recent = db.execute("SELECT target,claimed FROM agent_write_claims WHERE source=? AND claimed>?",
                                    (account, since)).fetchall()
                if (sum(row["claimed"] > activity.occurred - window for row in recent) >= maximum
                        or any(row["target"] == target and row["claimed"] > activity.occurred - cooldown
                               for row in recent)):
                    raise PermissionError("External write frequency or target cooldown exceeded")
                db.execute("INSERT INTO agent_write_claims VALUES (?,?,?,?,?)",
                           (producer, activity.operation_id, account, target, activity.occurred))
            ref = db.execute(
                """INSERT INTO activities(producer,event_id,digest,scope,actor,source,kind,state,
                       occurred,received,summary,evidence,operation_id,turn_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (producer, activity.event_id, digest, scope, access.principal, source,
                 activity.kind.value, activity.state.value, activity.occurred, self.clock(),
                 activity.summary, activity.evidence, activity.operation_id, turn_id),
            ).lastrowid
            erased = activity.summary and db.execute(
                "SELECT 1 FROM memory_erasures WHERE scope=? AND digest=?",
                (scope, content_digest(activity.summary))).fetchone()
            if erased:
                db.execute("INSERT OR IGNORE INTO memory_card_revoked_evidence VALUES (?)", (ref,))
                db.execute("UPDATE activities SET summary='' WHERE ref=?", (ref,))
            elif card_capture is not None or (activity.state in {ActivityState.OBSERVED, ActivityState.SUCCEEDED, ActivityState.FAILED}
                    and activity.kind in {ActivityKind.BROWSE, ActivityKind.READ, ActivityKind.REMINDER, ActivityKind.COMMENT}):
                owner, direct = card_capture or (False, False)
                card.capture(db, scope, access.principal, ref, turn_id, activity, owner=owner,
                             direct=direct, private=access.scope.kind == "private")
            if activity.operation_id:
                if operation is None:
                    db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (producer, activity.operation_id, scope, access.principal, source,
                                activity.kind.value, activity.intent, activity.state.value, activity.occurred, ref))
                else:
                    db.execute("""UPDATE operations SET state=?,occurred=?,ref=?
                                  WHERE producer=? AND operation_id=?""",
                               (activity.state.value, activity.occurred, ref, producer, activity.operation_id))
            access.require(write=True)
            return ref, True

    @staticmethod
    def _validate_transition(row, access, activity, source):
        current = ActivityState(row["state"]) if row else None
        if row and (row["scope"], row["actor"], row["source"], row["kind"], row["intent"]) != (
            access.scope.storage_key, access.principal, source, activity.kind.value, activity.intent
        ):
            raise PermissionError("Operation provenance or intent changed")
        if activity.state not in TRANSITIONS.get(current, ()) or (row and activity.occurred < row["occurred"]):
            raise ValueError("Illegal or out-of-order activity transition")

    def operation(self, access, producer, operation_id):
        access.require()
        with self.transaction() as db:
            row = db.execute("""SELECT * FROM operations WHERE producer=? AND operation_id=?
                               AND scope=? AND actor=?""",
                             (producer, operation_id, access.scope.storage_key, access.principal)).fetchone()
        access.require()
        return dict(row) if row else None

    def history(self, access, *, query="", limit=40, actual_only=False, behavior_only=False,
                current_evidence_only=False, kind=None, state=None, platform=None,
                since=None, until=None, executor=None, operation_id=None, kinds=()):
        access.require_recall()
        if not 1 <= limit <= 200:
            raise ValueError("Invalid history limit")
        with self.transaction() as db:
            sql = """SELECT activities.*, (
                SELECT approver FROM agent_approvals WHERE producer=activities.producer
                  AND operation_id=activities.operation_id AND approved<=activities.occurred
            ) AS approval_actor FROM activities WHERE scope=? AND redacted=0"""
            params = [access.scope.storage_key]
            filters = (("kind", kind), ("state", state), ("json_extract(source,'$.platform')", platform),
                       ("json_extract(source,'$.account')", executor), ("operation_id", operation_id))
            for field, value in filters:
                if value is not None:
                    sql += f" AND {field}=?"
                    params.append(value)
            if kinds:
                sql += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
                params.extend(kinds)
            if since is not None:
                sql += " AND occurred>=?"
                params.append(since)
            if until is not None:
                sql += " AND occurred<?"
                params.append(until)
            if actual_only:
                sql += " AND state IN ('observed','succeeded','failed')"
            if behavior_only:
                sql += " AND kind NOT IN ('message_received','message_sent')"
            if current_evidence_only:
                sql += """ AND NOT EXISTS (
                    SELECT 1 FROM fact_versions v JOIN facts f USING (scope,fact_key)
                    WHERE v.ref=activities.ref AND (v.revision<>f.revision OR f.status<>'active')
                )"""
            words = terms(query)
            if words:
                sql += " AND (" + " OR ".join("instr(lower(summary), ?) > 0" for _ in words) + ")"
                params.extend(words)
            rows = db.execute(sql + " ORDER BY occurred DESC, ref DESC LIMIT ?", (*params, limit)).fetchall()
        access.require_recall()
        return [self._history_item(row) for row in rows]

    @staticmethod
    def _history_item(row):
        item = dict(row)
        source = json.loads(item["source"])
        executor = source if item["state"] in {"observed", "running", "succeeded", "failed"} else None
        item["source"] = source
        item["owner"] = item["scope"]
        item["requester"] = item["actor"] if item["operation_id"] or item["kind"] in {
            "message_received", "message_sent", "reminder",
        } else None
        item["executor"] = executor
        item["target"] = source["conversation"]
        approval = item.pop("approval_actor", None)
        item["approver"] = json.loads(approval)[0] if approval else None
        return item

    def put_fact(self, access, fact_key, text, importance, evidence_ref, *, expected_revision=None,
                 automatic=False):
        access.require(write=True)
        identifier(fact_key)
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 2048 or contains_secret([fact_key, text]):
            raise ValueError("Invalid or sensitive fact")
        if not isinstance(importance, (int, float)) or not math.isfinite(importance) or not 0 <= importance <= 1:
            raise ValueError("Invalid importance")
        scope = access.scope.storage_key
        normalized = normalize(text)
        with self.transaction(write=True) as db:
            evidence = db.execute("SELECT actor,kind,state,occurred,redacted,turn_id FROM activities WHERE ref=? AND scope=?",
                                  (evidence_ref, scope)).fetchone()
            if (not evidence or evidence["redacted"] or evidence["actor"] != access.principal
                    or evidence["state"] not in {"observed", "succeeded"}):
                raise PermissionError("Fact requires observed or successful evidence in the author's scope")
            if automatic and db.execute(
                    "SELECT 1 FROM memory_erasures WHERE scope=? AND digest=?", (scope, content_digest(text))).fetchone():
                raise PermissionError("Deleted content requires fresh explicit consent")
            if automatic and (evidence["kind"] != "message_received" or not evidence["turn_id"]):
                raise PermissionError("Automatic facts require an authenticated inbound turn")
            current = db.execute("""SELECT f.*,v.normalized,v.ref,v.occurred FROM facts f
                                  JOIN fact_versions v USING(scope,fact_key,revision)
                                  WHERE f.scope=? AND f.fact_key=?""", (scope, fact_key)).fetchone()
            moderated = bool(current and current["actor"] != access.principal)
            if automatic and not current:
                same = db.execute("""SELECT f.revision FROM facts f JOIN fact_versions v USING(scope,fact_key,revision)
                    WHERE f.scope=? AND f.actor=? AND f.fact_key LIKE 'auto-%'
                      AND f.status='active' AND v.normalized=? LIMIT 1""",
                                  (scope, access.principal, normalized)).fetchone()
                if same:
                    access.require(write=True)
                    return same["revision"]
            if current:
                if moderated:
                    access.require(write=True, moderate=True)
                if current["status"] != "deleted" and current["normalized"] == normalized:
                    access.require(write=True, moderate=moderated)
                    return current["revision"]
                if current["status"] == "deleted" and (automatic or evidence_ref <= current["ref"]):
                    raise PermissionError("A deleted fact requires fresh explicit evidence")
                if (not automatic and expected_revision != current["revision"]) or (
                    evidence["occurred"], evidence_ref
                ) <= (current["occurred"], current["ref"]):
                    raise ValueError("Fact revision or evidence is stale")
                revision = current["revision"] + 1
                db.execute("UPDATE facts SET revision=?,status='active' WHERE scope=? AND fact_key=?",
                           (revision, scope, fact_key))
            else:
                if expected_revision is not None:
                    raise ValueError("Fact does not exist")
                revision = 1
                db.execute("INSERT INTO facts VALUES (?,?,?,?,'active')",
                           (scope, fact_key, access.principal, revision))
            db.execute("DELETE FROM fact_vectors WHERE scope=? AND fact_key=?", (scope, fact_key))
            db.execute("INSERT INTO fact_versions VALUES (?,?,?,?,?,?,?,?)",
                       (scope, fact_key, revision, text.strip(), normalized, importance,
                        evidence["occurred"], evidence_ref))
            # A fact revision is typed content on a card backed by the same evidence.
            attached = db.execute("""UPDATE memory_card SET importance=max(importance, ?)
                WHERE id IN (SELECT card FROM memory_card_sources WHERE ref=?)""",
                (round(importance * 100), evidence_ref)).rowcount
            if not attached:
                card.capture_fact(db, scope, access.principal, evidence_ref, evidence["occurred"],
                                  fact_key, revision, importance)
            access.require(write=True, moderate=moderated)
            return revision

    def remember(self, access, category, subject, text, importance, evidence_ref):
        digest = hashlib.sha256(encode([access.principal, category, normalize(subject)]).encode()).hexdigest()
        return self.put_fact(access, "auto-" + digest, text, importance, evidence_ref, automatic=True)

    @staticmethod
    def _retraction_digest(principal, inbound_id):
        return hashlib.sha256(encode(["retraction", principal, inbound_id]).encode()).hexdigest()

    def retract_message(self, scope, principal, event_id, allowed):
        """Remember a verified withdrawal even when the inbound evidence has not arrived."""
        if not allowed():
            raise PermissionError("Retraction authorization revoked")
        inbound_id = "in:" + event_id
        with self.transaction(write=True) as db:
            recorded = db.execute("INSERT OR IGNORE INTO memory_erasures VALUES (?,?)",
                                  (scope.storage_key, self._retraction_digest(principal, inbound_id))).rowcount
            row = db.execute("""SELECT ref,summary FROM activities WHERE producer='core'
                AND scope=? AND actor=? AND event_id=? AND kind='message_received' AND redacted=0""",
                (scope.storage_key, principal, inbound_id)).fetchone()
            if row is not None:
                self._erase_inbound_evidence(db, scope.storage_key, row["ref"])
            if not allowed():
                raise PermissionError("Retraction authorization revoked")
            return bool(recorded or row is not None)

    def _erase_inbound_evidence(self, db, scope, ref):
        affected = {scope}
        affected.update(item[0] for item in db.execute("""SELECT DISTINCT c.scope FROM memory_card c
            JOIN memory_card_sources s ON s.card=c.id WHERE s.ref=?""", (ref,)))
        db.execute("""UPDATE facts SET status='deleted' WHERE (scope,fact_key,revision) IN
            (SELECT scope,fact_key,revision FROM fact_versions WHERE ref=?)""", (ref,))
        for table in ("fact_vectors", "fact_review"):
            db.execute(f"""DELETE FROM {table} WHERE (scope,fact_key,revision) IN
                (SELECT scope,fact_key,revision FROM fact_versions WHERE ref=?)""", (ref,))
        db.execute("UPDATE fact_versions SET content=NULL,normalized='' WHERE ref=?", (ref,))
        db.execute("UPDATE activities SET summary='',evidence='',redacted=1 WHERE ref=?", (ref,))
        for key in affected:
            self._mark_erasure(db, key)
            card.refresh_initial(db, key)

    def delete_fact(self, access, fact_key, expected_revision):
        access.require(write=True)
        scope = access.scope.storage_key
        with self.transaction(write=True) as db:
            row = db.execute("SELECT actor,revision,status FROM facts WHERE scope=? AND fact_key=?",
                             (scope, fact_key)).fetchone()
            if not row:
                raise PermissionError("Fact unavailable")
            moderated = row["actor"] != access.principal
            if moderated:
                access.require(write=True, moderate=True)
            author = row["actor"]
            if row["revision"] != expected_revision:
                raise ValueError("Fact revision is stale")
            if row["status"] == "deleted":
                return False
            versions = db.execute("""SELECT ref, normalized FROM fact_versions
                WHERE scope=? AND fact_key=?""", (scope, fact_key)).fetchall()
            refs = {item["ref"] for item in versions}
            texts = {item["normalized"] for item in versions if item["normalized"]}
            db.executemany("INSERT OR IGNORE INTO memory_erasures VALUES (?,?)",
                           ((scope, content_digest(value)) for value in texts))
            # Clear exact copies stored under other user-selected keys as well.
            copies = db.execute("""SELECT v.fact_key,v.revision,v.ref,v.normalized FROM fact_versions v
                JOIN facts f USING(scope,fact_key) WHERE v.scope=? AND f.actor=? AND v.content IS NOT NULL""",
                                  (scope, author)).fetchall()
            for copy in copies:
                if copy["fact_key"] != fact_key and copy["normalized"] in texts:
                    version = (scope, copy["fact_key"], copy["revision"])
                    refs.add(copy["ref"])
                    db.execute("UPDATE fact_versions SET content=NULL,normalized='' WHERE scope=? AND fact_key=? AND revision=?", version)
                    db.execute("UPDATE facts SET status='deleted' WHERE scope=? AND fact_key=? AND revision=?", version)
                    db.execute("DELETE FROM fact_vectors WHERE scope=? AND fact_key=? AND revision=?", version)
                    db.execute("DELETE FROM fact_review WHERE scope=? AND fact_key=? AND revision=?", version)
            db.execute("DELETE FROM fact_vectors WHERE scope=? AND fact_key=?", (scope, fact_key))
            db.execute("DELETE FROM fact_review WHERE scope=? AND fact_key=?", (scope, fact_key))
            db.execute("UPDATE facts SET status='deleted' WHERE scope=? AND fact_key=?", (scope, fact_key))
            db.execute("UPDATE fact_versions SET content=NULL,normalized='' WHERE scope=? AND fact_key=?",
                       (scope, fact_key))
            # For adjacent unrelated sentences in unlinked chat copies, keep the surviving text.
            for activity in db.execute("""SELECT ref,summary,evidence FROM activities
                    WHERE scope=? AND (actor=? OR ref IN (SELECT ref FROM fact_versions
                        WHERE scope=? AND fact_key=?)) AND redacted=0""",
                                       (scope, author, scope, fact_key)).fetchall():
                body = normalize(activity["summary"] + "\n" + activity["evidence"])
                if activity["ref"] in refs and not self._has_surviving_fact(db, activity["ref"]):
                    db.execute("UPDATE activities SET summary='',evidence='',redacted=1 WHERE ref=?",
                               (activity["ref"],))
                elif activity["ref"] in refs or any(value in body for value in texts):
                    summary = without_erased_sentences(activity["summary"], texts)
                    if any(value in normalize(summary) for value in texts):
                        summary = ""
                    evidence = activity["evidence"]
                    if any(value in normalize(evidence) for value in texts):
                        evidence = ""
                    db.execute("UPDATE activities SET summary=?,evidence=?,redacted=? WHERE ref=?",
                               (summary, evidence, int(not summary and not self._has_surviving_fact(db, activity["ref"])), activity["ref"]))
            self._mark_erasure(db, scope)
            card.refresh_initial(db, scope)
            access.require(write=True, moderate=moderated)
            return True

    @staticmethod
    def _has_surviving_fact(db, ref):
        return db.execute("""SELECT 1 FROM fact_versions v JOIN facts f USING(scope,fact_key,revision)
            WHERE v.ref=? AND v.content IS NOT NULL AND f.status<>'deleted' LIMIT 1""",
                          (ref,)).fetchone() is not None

    @staticmethod
    def _mark_erasure(db, scope):
        db.execute("""INSERT INTO memory_erasure_epochs VALUES (?,1)
            ON CONFLICT(scope) DO UPDATE SET epoch=epoch+1""", (scope,))

    def erasure_epoch(self, access):
        access.require_recall()
        with self.transaction() as db:
            row = db.execute("SELECT epoch FROM memory_erasure_epochs WHERE scope=?",
                             (access.scope.storage_key,)).fetchone()
            return row["epoch"] if row else 0

    def export_erasures(self):
        """Local instance operator boundary. The export contains no fact text."""
        with self.transaction() as db:
            return [list(row) for row in db.execute("SELECT scope,digest FROM memory_erasures ORDER BY scope,digest")]

    def reapply_erasures(self, records, *, db=None):
        """Apply current revocations to an isolated restore staging database."""
        records = validate_erasures(records)
        if not records:
            return
        with (self.transaction(write=True) if db is None else nullcontext(db)) as db:
            db.executemany("INSERT OR IGNORE INTO memory_erasures VALUES (?,?)", records)
            scopes = {scope for scope, _ in records}
            for scope in scopes:
                self._mark_erasure(db, scope)
                erased = {row[0] for row in db.execute("SELECT digest FROM memory_erasures WHERE scope=?", (scope,))}
                inbound = db.execute("""SELECT ref,actor,event_id FROM activities WHERE scope=? AND producer='core'
                    AND kind='message_received' AND redacted=0""", (scope,)).fetchall()
                for activity in inbound:
                    if self._retraction_digest(activity["actor"], activity["event_id"]) in erased:
                        self._erase_inbound_evidence(db, scope, activity["ref"])
                projections = db.execute("""SELECT DISTINCT c.id FROM memory_card c
                    JOIN memory_card_sources s ON s.card=c.id JOIN activities a USING(ref)
                    WHERE c.scope=? AND a.scope<>c.scope""", (scope,)).fetchall()
                for projection in projections:
                    ident = projection["id"]
                    if content_digest("card-projection:" + ident) in erased:
                        db.execute("DELETE FROM memory_card WHERE id=?", (ident,))
                rows = db.execute("""SELECT fact_key,revision,ref,normalized FROM fact_versions
                    WHERE scope=? AND content IS NOT NULL""", (scope,)).fetchall()
                removed = [row for row in rows if content_digest(row["normalized"]) in erased]
                for row in removed:
                    version = scope, row["fact_key"], row["revision"]
                    db.execute("""UPDATE fact_versions SET content=NULL,normalized=''
                        WHERE scope=? AND fact_key=? AND revision=?""", version)
                    db.execute("UPDATE facts SET status='deleted' WHERE scope=? AND fact_key=? AND revision=?", version)
                    db.execute("DELETE FROM fact_vectors WHERE scope=? AND fact_key=? AND revision=?", version)
                    db.execute("DELETE FROM fact_review WHERE scope=? AND fact_key=? AND revision=?", version)
                for row in removed:
                    if not self._has_surviving_fact(db, row["ref"]):
                        db.execute("UPDATE activities SET summary='',evidence='',redacted=1 WHERE ref=?", (row["ref"],))
                    for other in db.execute("""SELECT ref,summary FROM activities WHERE scope=? AND redacted=0
                        AND kind IN ('message_received','message_sent')""", (scope,)).fetchall():
                        if row["normalized"] in normalize(other["summary"]):
                            summary = without_erased_sentences(other["summary"], (row["normalized"],))
                            if row["normalized"] in normalize(summary):
                                summary = ""
                            db.execute("UPDATE activities SET summary=?,evidence='',redacted=? WHERE ref=?",
                                       (summary, int(not summary and not self._has_surviving_fact(db, other["ref"])), other["ref"]))
                for activity in db.execute("SELECT ref,summary FROM activities WHERE scope=? AND redacted=0", (scope,)):
                    if activity["summary"] and content_digest(activity["summary"]) in erased:
                        db.execute("UPDATE activities SET summary='',evidence='',redacted=1 WHERE ref=?",
                                   (activity["ref"],))

    def fact_history(self, access, fact_key):
        access.require_recall()
        identifier(fact_key)
        with self.transaction() as db:
            rows = db.execute("""SELECT v.revision,v.content,v.importance,v.occurred,v.ref,a.source,a.evidence
                FROM fact_versions v JOIN activities a ON a.ref=v.ref
                WHERE v.scope=? AND v.fact_key=? ORDER BY v.revision DESC""",
                              (access.scope.storage_key, fact_key)).fetchall()
        access.require_recall()
        return [{**dict(row), "source": json.loads(row["source"])} for row in rows]

    def command_facts(self, access):
        access.require_recall()
        group = access.scope.kind == "group"
        if group:
            access.require(moderate=True)
        with self.transaction() as db:
            rows = db.execute("""SELECT f.fact_key,f.revision,f.status,v.content,v.importance,
                                        v.ref,a.source,a.evidence
                FROM facts f JOIN fact_versions v USING(scope,fact_key,revision)
                JOIN activities a ON a.ref=v.ref WHERE f.scope=? AND (f.actor=? OR ?)""",
                              (access.scope.storage_key, access.principal, group)).fetchall()
        access.require_recall()
        return [{**dict(row), "source": json.loads(row["source"])} for row in rows]

    def reset_scope(self, access, legacy_digests=()):
        access.require_recall()
        group = access.scope.kind == "group"
        access.require(write=True, moderate=group)
        scope = access.scope.storage_key
        with self.transaction(write=True) as db:
            digests = set(legacy_digests)
            digests.update(content_digest(row[0]) for row in db.execute(
                "SELECT normalized FROM fact_versions WHERE scope=? AND content IS NOT NULL", (scope,)))
            digests.update(content_digest(row[0]) for row in db.execute("""SELECT summary FROM activities
                WHERE scope=? AND redacted=0 AND kind IN ('message_received','message_sent','legacy_group_summary')
                  AND summary NOT IN ('','收到消息','通信适配器已返回回复提交')""", (scope,)))
            db.executemany("INSERT OR IGNORE INTO memory_erasures VALUES (?,?)",
                           ((scope, digest) for digest in digests))
            db.execute("""UPDATE activities SET summary='',evidence='',redacted=1 WHERE scope=?
                AND (kind IN ('message_received','message_sent','legacy_group_summary') OR ref IN (
                    SELECT ref FROM fact_versions WHERE scope=?))""", (scope, scope))
            db.execute("DELETE FROM fact_vectors WHERE scope=?", (scope,))
            db.execute("DELETE FROM fact_review WHERE scope=?", (scope,))
            db.execute("UPDATE fact_versions SET content=NULL,normalized='' WHERE scope=?", (scope,))
            db.execute("UPDATE facts SET status='deleted' WHERE scope=?", (scope,))
            db.execute("DELETE FROM memory_card_settings WHERE scope=?", (scope,))
            db.execute("DELETE FROM memory_card_sharing WHERE scope=?", (scope,))
            projections = db.execute("""SELECT DISTINCT c.id FROM memory_card c
                JOIN memory_card_sources s ON s.card=c.id JOIN activities a USING(ref)
                WHERE c.scope=? AND a.scope<>c.scope""", (scope,)).fetchall()
            db.executemany("INSERT OR IGNORE INTO memory_erasures VALUES (?,?)",
                           ((scope, content_digest("card-projection:" + row["id"])) for row in projections))
            db.executemany("DELETE FROM memory_card WHERE id=?", ((row["id"],) for row in projections))
            self._mark_erasure(db, scope)
            access.require(write=True, moderate=group)

    def search(self, access, query, *, limit=8, include_archive=False, semantic=None):
        access.require_recall()
        if not 1 <= limit <= 40:
            raise ValueError("Invalid search limit")
        words = terms(query)
        if not words:
            return []
        with self.transaction() as db:
            placeholders = " OR ".join("instr(v.normalized, ?) > 0" for _ in words)
            rows = db.execute(f"""
                SELECT f.fact_key,f.revision,f.status,v.content,v.normalized,v.importance,
                       v.occurred,v.ref,a.received,a.source,a.evidence,r.last_seen,r.count
                FROM facts f JOIN fact_versions v USING(scope,fact_key,revision)
                JOIN activities a ON a.ref=v.ref
                LEFT JOIN fact_review r USING(scope,fact_key,revision)
                WHERE f.scope=? AND (f.status='active' OR (? AND f.status='archived'))
                  AND (({placeholders}) OR ?)
            """, (access.scope.storage_key, int(include_archive), *words, int(bool(semantic)))).fetchall()
        access.require_recall()
        hits, seen = [], set()
        now = self.clock()
        def score(row, lexical):
            age = max(0, now - row["occurred"])
            freshness = 1 / (1 + age / (30 * 86400))
            similarity = (semantic or {}).get((row["fact_key"], row["revision"]), 0)
            if not lexical and similarity < 0.55:
                return -1
            return lexical + 5 * similarity + 2 * row["importance"] + freshness + 0.2 * self.clarity(row, now)
        lexical_scores = bm25(words, [row["normalized"] for row in rows])
        ranked = sorted(((score(row, lexical), row["occurred"], row["fact_key"], row)
                         for row, lexical in zip(rows, lexical_scores)), reverse=True)
        for rank, _, _, row in ranked:
            if rank < 0:
                break
            if not include_archive and self.clarity(row, now) == 0:
                continue
            if row["normalized"] not in seen:
                seen.add(row["normalized"])
                hits.append({**dict(row), "source": json.loads(row["source"]), "clarity": self.clarity(row, now)})
            if len(hits) == limit:
                break
        return hits

    @staticmethod
    def clarity(row, now):
        learned = max(row["received"], row["occurred"])
        age = max(0, now - learned) / 86400
        since_review = max(0, now - (row["last_seen"] or learned)) / 86400
        retention = 100 * row["importance"] + min(12, 2 * (row["count"] or 0))
        retention += 18 if age <= 7 else 0
        retention += 8 if since_review <= 2 else 0
        retention -= min(80, max(0, since_review - 7) * 0.35)
        if row["importance"] >= 0.95:
            retention = max(46, retention)
        return sum(retention >= boundary for boundary in (12, 24, 46, 68))

    def review(self, access, facts):
        access.require(write=True)
        now = self.clock()
        with self.transaction(write=True, timeout=0.1) as db:
            for item in facts:
                db.execute("""INSERT INTO fact_review
                    SELECT scope,fact_key,revision,?,1 FROM facts
                    WHERE scope=? AND fact_key=? AND revision=? AND status='active'
                    ON CONFLICT(scope,fact_key,revision) DO UPDATE SET
                        count=MIN(999,count+1),last_seen=excluded.last_seen
                    WHERE excluded.last_seen-last_seen>=86400""",
                    (now, access.scope.storage_key, item["fact_key"], item["revision"]))
            access.require(write=True)

    def maintain(self, access):
        """Archive decayed facts once per UTC day per active scope."""
        access.require_recall()
        access.require(write=True)
        return self._maintain_scope(access.scope.storage_key, lambda: access.require(write=True))

    def maintain_pending(self, check_enabled, *, limit=32):
        """Host maintenance changes archive state without exporting any memory."""
        check_enabled()
        day = int(self.clock() // 86400)
        with self.transaction() as db:
            scopes = db.execute("""SELECT f.scope FROM facts f LEFT JOIN memory_maintenance m USING(scope)
                WHERE f.status='active' AND (m.day IS NULL OR m.day<?)
                UNION SELECT c.scope FROM memory_card c LEFT JOIN memory_card_settings s USING(scope)
                LEFT JOIN memory_card_archive a ON a.card=c.id
                WHERE s.last_day IS NULL OR s.last_day<? OR a.card IS NULL
                ORDER BY scope LIMIT ?""", (day, day, limit)).fetchall()
        return sum(self._maintain_scope(row["scope"], check_enabled) for row in scopes)

    def pending_private_index_sources(self, model, *, activities=False, limit=32):
        with self.transaction() as db:
            rows = db.execute("""SELECT a.scope,a.actor,a.source,MAX(a.ref) AS latest FROM activities a
                WHERE a.kind='message_received' AND a.redacted=0
                  AND json_extract(a.scope,'$[0]')='private'
                  AND json_valid(json_extract(a.source,'$.conversation'))
                  AND (EXISTS (
                    SELECT 1 FROM facts f LEFT JOIN fact_vectors x
                      ON x.scope=f.scope AND x.fact_key=f.fact_key AND x.revision=f.revision AND x.model=?
                    WHERE f.scope=a.scope AND f.status='active' AND x.model IS NULL
                  ) OR (? AND EXISTS (
                    SELECT 1 FROM activities candidate LEFT JOIN activity_vectors x
                      ON x.ref=candidate.ref AND x.model=?
                    WHERE candidate.scope=a.scope AND candidate.redacted=0 AND candidate.turn_id IS NOT NULL
                      AND candidate.kind IN ('message_received','message_sent')
                      AND candidate.summary NOT IN ('','收到消息','通信适配器已返回回复提交')
                      AND x.model IS NULL
                  )))
                GROUP BY a.scope,a.actor,a.source ORDER BY latest DESC LIMIT ?""",
                              (model, int(activities), model, limit)).fetchall()
        return [(row["scope"], row["actor"], json.loads(row["source"])) for row in rows]

    def _maintain_scope(self, scope, check_enabled):
        check_enabled()
        now = self.clock()
        day = int(now // 86400)
        with self.transaction(write=True, timeout=0.1) as db:
            if db.execute("SELECT 1 FROM memory_card WHERE scope=? LIMIT 1", (scope,)).fetchone():
                card.maintain(db, scope, now, self.card_max_chars)
            claimed = db.execute("""INSERT INTO memory_maintenance VALUES (?,?)
                ON CONFLICT(scope) DO UPDATE SET day=excluded.day WHERE excluded.day>day""",
                (scope, day)).rowcount
            if not claimed:
                check_enabled()
                return 0
            rows = db.execute("""SELECT f.fact_key,v.importance,v.occurred,a.received,r.last_seen,r.count
                FROM facts f JOIN fact_versions v USING(scope,fact_key,revision)
                JOIN activities a ON a.ref=v.ref LEFT JOIN fact_review r USING(scope,fact_key,revision)
                WHERE f.scope=? AND f.status='active'""", (scope,)).fetchall()
            decayed = [(scope, row["fact_key"]) for row in rows if not self.clarity(row, now)]
            db.executemany("UPDATE facts SET status='archived' WHERE scope=? AND fact_key=?", decayed)
            check_enabled()
            return len(decayed)

    def status(self, model=None):
        """Local operator diagnostics with aggregate counts and UTC timestamps."""
        with self.transaction() as db:
            facts = dict(db.execute("SELECT status,COUNT(*) FROM facts GROUP BY status"))
            runtime = db.execute("SELECT occurred,pending,failure FROM memory_runtime_status WHERE id=1").fetchone()
            return {
                "facts": {state: facts.get(state, 0) for state in ("active", "archived", "deleted")},
                "active_cards": db.execute("SELECT COUNT(*) FROM memory_card WHERE hidden=0 AND clarity>0").fetchone()[0],
                "archived_cards": db.execute("SELECT COUNT(*) FROM memory_card_archive").fetchone()[0],
                "activities": db.execute("SELECT COUNT(*) FROM activities WHERE redacted=0").fetchone()[0],
                "legacy_references": db.execute("SELECT COUNT(*) FROM legacy_refs").fetchone()[0],
                "indexed_facts": db.execute("""SELECT COUNT(*) FROM fact_vectors
                    JOIN facts USING(scope,fact_key,revision) WHERE status='active' AND model=?""", (model,)).fetchone()[0],
                "indexed_activities": db.execute("SELECT COUNT(*) FROM activity_vectors WHERE model=?", (model,)).fetchone()[0],
                "last_maintenance_day": db.execute("SELECT MAX(day) FROM memory_maintenance").fetchone()[0],
                "last_maintenance_run": ({"occurred": runtime["occurred"], "pending": json.loads(runtime["pending"]),
                                          "failure": runtime["failure"]} if runtime else None),
            }

    def record_maintenance_run(self, pending, failure):
        with self.transaction(write=True, timeout=0.1) as db:
            db.execute("""INSERT INTO memory_runtime_status VALUES (1,?,?,?)
                ON CONFLICT(id) DO UPDATE SET occurred=excluded.occurred,pending=excluded.pending,
                    failure=excluded.failure""", (self.clock(), encode(pending), failure))

    def compact(self, access, *, keep=30):
        """Archive less useful current facts. Evidence and versions remain linked."""
        access.require(write=True)
        if not 0 <= keep <= 1000:
            raise ValueError("Invalid compaction limit")
        scope = access.scope.storage_key
        with self.transaction(write=True) as db:
            rows = db.execute("""SELECT f.fact_key FROM facts f
                JOIN fact_versions v USING(scope,fact_key,revision)
                WHERE f.scope=? AND f.actor=? AND f.status='active'
                ORDER BY v.importance DESC,v.occurred DESC,f.fact_key LIMIT -1 OFFSET ?""",
                              (scope, access.principal, keep)).fetchall()
            db.executemany("UPDATE facts SET status='archived' WHERE scope=? AND fact_key=?",
                           ((scope, row["fact_key"]) for row in rows))
            access.require(write=True)
            return len(rows)

    def card_records(self, access, query="", *, archived=False, exclude_turn=None, limit=12, semantic=None, user_timezone=timezone.utc):
        access.require_recall()
        if not 1 <= limit <= 50:
            raise ValueError("Invalid memory card limit")
        epoch = self.erasure_epoch(access)
        with self.transaction() as db:
            records = card.contents(db, access.scope.storage_key, archived=archived)
            current = set()
            if exclude_turn is not None:
                current = {row[0] for row in db.execute("SELECT ref FROM activities WHERE turn_id=? AND scope=?",
                                                        (exclude_turn, access.scope.storage_key))}
        words = terms(query)
        greeting = bool(GREETING.fullmatch(query.strip()))
        browsing = bool(re.search(r"(?:浏览|看|读)(?:了|过)?什么", query))
        overview = browsing or bool(re.search(r"做了什么|干了什么|最近.{0,6}(行为|做过|活动|经历)|what did you do", query, re.I))
        platform = "bilibili" if re.search(r"b\s*站|bilibili", query, re.I) else None
        relative = re.search(r"前天|昨天|今天", query) if overview else None
        day = None
        if relative:
            day = datetime.fromtimestamp(self.clock(), user_timezone).date() - timedelta(
                days={"前天": 2, "昨天": 1, "今天": 0}[relative[0]])
        selected = []
        for rows in records.values():
            row = rows[0]
            foreign = {item["evidence_scope"] for item in rows if item["evidence_scope"] != access.scope.storage_key}
            if foreign and (access.source_scope_allowed is None or
                            not all(access.source_scope_allowed(scope) for scope in foreign)):
                continue
            if any(item["ref"] in current for item in rows):
                continue
            if greeting and row["importance"] < 65 and not card.protected(row):
                continue
            if overview:
                rows = [item for item in rows if (
                    (item["activity_kind"] in ({"browse", "read"} if browsing else {"browse", "read", "comment", "reminder"}))
                    and (not platform or json.loads(item["source"])["platform"] == platform)
                    and (day is None or datetime.fromtimestamp(item["occurred"], user_timezone).date() == day))]
                if not rows:
                    continue
            lexical = max(relevance(words, normalize(row["title"])),
                          *(relevance(words, normalize(item["summary"])) for item in rows)) if words and not overview else 1
            similarity = max(max((semantic or {}).get(item["ref"], 0),
                                 (semantic or {}).get((item["fact_key"], item["fact_revision"]), 0))
                             for item in rows)
            matched = greeting or lexical > 0 or similarity >= 0.55
            if not matched and (archived or not card.protected(row)):
                continue
            item = card.render(rows, full=archived)
            if item is not None:
                selected.append((not row["first_meet"], not row["pinned"], not matched,
                                 -lexical, -similarity, -row["importance"], foreign, item))
        selected.sort(key=lambda entry: entry[:6])
        access.require_recall()
        if epoch != self.erasure_epoch(access):
            return []
        return [item for *_, foreign, item in selected
                if not foreign or all(access.source_scope_allowed(scope) for scope in foreign)][:limit]

    def edit_card(self, access, operation, ids=(), title=""):
        access.require_recall()
        access.require(write=True, moderate=access.scope.kind == "group")
        scope = access.scope.storage_key
        with self.transaction(write=True) as db:
            result = None
            if operation in {"merge", "meet"}:
                result = card.designate(db, scope, access.principal, ids, title, self.clock(),
                                        meeting=operation == "meet", source_scope_allowed=access.source_scope_allowed)
            elif operation == "auto-meet":
                card.restore_meeting(db, scope, access.principal)
            elif operation == "rename" and len(ids) == 1 and 0 < len(title) <= 80:
                if sensitive_information(title):
                    raise ValueError("Sensitive card title")
                updated = db.execute("""UPDATE memory_card SET title=?
                    WHERE id=? AND scope=? AND principal=? AND hidden=0""",
                    (title, ids[0], scope, access.principal))
                if not updated.rowcount:
                    raise PermissionError("Memory record is unavailable in this scope")
            elif operation in {"pin", "unpin"} and len(ids) == 1 and not title:
                updated = db.execute("""UPDATE memory_card SET pinned=?,pin_override=1,
                    clarity=CASE WHEN ? THEN 4 ELSE clarity END
                    WHERE id=? AND scope=? AND principal=? AND hidden=0 AND clarity>0""",
                    (operation == "pin", operation == "pin", ids[0], scope, access.principal))
                if not updated.rowcount:
                    raise PermissionError("Memory record is unavailable in this scope")
            elif operation == "erase-card" and len(ids) == 1 and not title:
                sources = db.execute("""SELECT a.ref,a.summary FROM memory_card c
                    JOIN memory_card_sources s ON s.card=c.id JOIN activities a USING(ref)
                    WHERE c.id=? AND c.scope=? AND c.principal=? AND a.redacted=0""",
                    (ids[0], scope, access.principal)).fetchall()
                if not sources:
                    raise PermissionError("Memory record is unavailable in this scope")
                foreign = db.execute("""SELECT 1 FROM activities a JOIN memory_card_sources s USING(ref)
                    WHERE s.card=? AND a.scope<>? LIMIT 1""", (ids[0], scope)).fetchone()
                if foreign:
                    db.execute("INSERT OR IGNORE INTO memory_erasures VALUES (?,?)",
                               (scope, content_digest("card-projection:" + ids[0])))
                    db.execute("DELETE FROM memory_card WHERE id=?", (ids[0],))
                    self._mark_erasure(db, scope)
                    access.require(write=True)
                    return None
                digests = {content_digest(row["summary"]) for row in sources if row["summary"]}
                # Include facts derived from any of these activities in the same erasure transaction.
                refs = encode([row["ref"] for row in sources])
                digests.update(content_digest(row[0]) for row in db.execute(
                    "SELECT normalized FROM fact_versions WHERE content IS NOT NULL "
                    "AND ref IN (SELECT value FROM json_each(?))", (refs,)))
                self.reapply_erasures([[scope, digest] for digest in sorted(digests)], db=db)
                db.execute("""UPDATE activities SET summary='',evidence='',redacted=1
                    WHERE ref IN (SELECT value FROM json_each(?))""", (refs,))
                self._mark_erasure(db, scope)
                card.refresh_initial(db, scope)
            else:
                raise ValueError("Invalid memory card operation")
            access.require(write=True, moderate=access.scope.kind == "group")
            return result

    def context(self, access, query, *, budget=2400, token_budget=None, semantic=None,
                exclude_turn=None, user_timezone=timezone.utc):
        access.require_recall()
        if not 256 <= budget <= 8000:
            raise ValueError("Invalid context budget")
        if token_budget is not None and (type(token_budget) is not int or not 256 <= token_budget <= 8000):
            raise ValueError("Invalid memory token budget")
        if not terms(query):
            return ""
        epoch = self.erasure_epoch(access)
        char_limit = min(640, budget) if GREETING.fullmatch(query.strip()) else budget
        items = self.card_records(access, query, exclude_turn=exclude_turn, limit=12, semantic=semantic,
                                  user_timezone=user_timezone)
        lines, recalled, size, bytes_used = [], [], 0, 0
        for item in items:
            line = encode(item)
            if size + len(line) + 1 > char_limit or (
                    token_budget is not None and bytes_used + len(line.encode()) + 1 > token_budget):
                line = encode({**item, "events": [
                    {**event, "text": event["text"][:48]} for event in item["events"][:3]
                ]})
            line_bytes = len(line.encode()) + 1
            if size + len(line) + 1 <= char_limit and (
                    token_budget is None or bytes_used + line_bytes <= token_budget):
                size += len(line) + 1
                bytes_used += line_bytes
                lines.append(line)
                if len(recalled) < 5:
                    recalled.append(item["id"])
        if recalled and access.may_write:
            try:
                access.require(write=True)
                with self.transaction(write=True, timeout=0.1) as db:
                    now = self.clock()
                    db.executemany("""UPDATE memory_card SET last_seen=?, reviews=min(reviews+1,999)
                        WHERE id=? AND scope=? AND hidden=0 AND clarity>0
                        AND (reviews=0 OR last_seen<=?)""",
                        ((now, ident, access.scope.storage_key, now - card.DAY) for ident in recalled))
                    access.require(write=True)
            except (sqlite3.Error, OSError) as exc:
                logger.warning("FFA narrative memory review deferred (%s)", type(exc).__name__)
        access.require_recall()
        return "\n".join(lines) if self.erasure_epoch(access) == epoch else ""

    @staticmethod
    def _date(value):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
