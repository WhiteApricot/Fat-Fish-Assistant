"""Evidence-linked projections and deferred analysis in the MemoryService database."""
from contextlib import nullcontext
from dataclasses import asdict
from functools import lru_cache
import hashlib
import json
import re
from uuid import uuid4

from fat_fish.communication.model import identifier
from fat_fish.memory import card, public_experience
from fat_fish.memory.activity import ActivityKind, ActivityState
from fat_fish.memory.experience import sensitive_information


DIMENSIONS = frozenset({"humor", "banter", "formality", "directness", "detail", "warmth"})
SCHEMA = (
    """CREATE TABLE memory_analysis (
        ref INTEGER PRIMARY KEY REFERENCES activities(ref) ON DELETE CASCADE,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        available REAL NOT NULL DEFAULT 0, lease TEXT, error TEXT)""",
    "CREATE INDEX memory_analysis_ready ON memory_analysis(state,available,ref)",
    """CREATE TABLE memory_capacity_status (scope TEXT PRIMARY KEY,
        before_chars INTEGER NOT NULL, after_chars INTEGER NOT NULL, level INTEGER NOT NULL)""",
    """CREATE TABLE memory_social_signal (
        ref INTEGER REFERENCES activities(ref) ON DELETE CASCADE,
        principal TEXT NOT NULL, field TEXT NOT NULL, value REAL NOT NULL,
        confidence REAL NOT NULL, explicit INTEGER NOT NULL,
        PRIMARY KEY(ref,field))""",
    "CREATE INDEX memory_social_principal ON memory_social_signal(principal,field,ref)",
    """CREATE TABLE memory_agent_experience (
        agent TEXT NOT NULL, receipt TEXT NOT NULL,
        ref INTEGER NOT NULL REFERENCES activities(ref) ON DELETE CASCADE,
        visibility TEXT NOT NULL, authority TEXT NOT NULL, PRIMARY KEY(agent,receipt), UNIQUE(agent,ref))""",
    "CREATE INDEX memory_agent_source ON memory_agent_experience(ref)",
    """CREATE TABLE memory_agent_theme (
        agent TEXT NOT NULL, receipt TEXT NOT NULL, theme TEXT NOT NULL,
        kind TEXT NOT NULL, status TEXT NOT NULL, confidence REAL NOT NULL,
        PRIMARY KEY(agent,receipt,kind,theme),
        FOREIGN KEY(agent,receipt) REFERENCES memory_agent_experience(agent,receipt) ON DELETE CASCADE)""",
    """CREATE TABLE memory_projection_epoch (id INTEGER PRIMARY KEY CHECK(id=1),value INTEGER NOT NULL)""",
    "INSERT INTO memory_projection_epoch VALUES (1,0)",
    """CREATE TRIGGER memory_social_removed AFTER DELETE ON memory_social_signal
       BEGIN UPDATE memory_projection_epoch SET value=value+1 WHERE id=1; END""",
    """CREATE TRIGGER memory_agent_removed AFTER DELETE ON memory_agent_experience
       BEGIN UPDATE memory_projection_epoch SET value=value+1 WHERE id=1; END""",
    """CREATE TABLE memory_analysis_conflict (
        ref INTEGER REFERENCES activities(ref) ON DELETE CASCADE,
        fact_key TEXT NOT NULL, PRIMARY KEY(ref,fact_key))""",
    """CREATE TRIGGER memory_unmerge_surviving_evidence BEFORE DELETE ON memory_card
       WHEN OLD.kind='merged' AND OLD.reasons='["model-merge"]'
       BEGIN
        UPDATE memory_card SET hidden=0 WHERE id IN (SELECT value FROM json_each(OLD.merged_from));
       END""",
    """CREATE TRIGGER memory_projection_erasure AFTER UPDATE OF summary,evidence,redacted ON activities
       WHEN NEW.summary<>OLD.summary OR NEW.evidence<>OLD.evidence OR NEW.redacted<>OLD.redacted
       BEGIN
        DELETE FROM memory_social_signal WHERE ref=OLD.ref;
        DELETE FROM memory_agent_experience WHERE ref=OLD.ref;
        DELETE FROM memory_analysis_conflict WHERE ref=OLD.ref;
        UPDATE memory_analysis SET state='discarded',lease=NULL,error=NULL WHERE ref=OLD.ref;
       END""",
)


class MemoryProjections:
    def __init__(self, service):
        self.service = service

    def pending(self, limit=32):
        with self.service.transaction() as db:
            return [dict(row) for row in db.execute(
                """SELECT j.ref,a.actor,a.scope,COALESCE(
                   (SELECT e.authority FROM memory_agent_experience e WHERE e.ref=a.ref AND e.visibility='public' LIMIT 1),
                   a.source) AS source FROM memory_analysis j JOIN activities a USING(ref)
                   WHERE j.state IN ('pending','running') AND j.released IS NOT NULL AND j.available<=?
                   ORDER BY j.available,j.ref LIMIT ?""", (self.service.clock(), limit))]

    def ready_batches(self, *, idle=120, threshold=4, target=None):
        now = self.service.clock()
        params = [now]
        scope_filter = ""
        if target:
            target.require_recall()
            target.require(moderate=target.scope.kind == "group")
            scope_filter = " AND a.actor=? AND a.scope=? AND a.source=?"
            params.extend((target.principal, target.scope.storage_key, self.encode_source(target.source)))
        params.extend((threshold, now - idle, bool(target)))
        with self.service.transaction() as db:
            rows = db.execute(f"""WITH eligible AS (
                SELECT j.ref,j.released,a.actor,a.scope,
                    COALESCE(e.authority,a.source) AS source,
                    CASE WHEN e.ref IS NOT NULL OR j.attempts>0 THEN j.ref ELSE 0 END AS domain
                FROM memory_analysis j JOIN activities a USING(ref)
                LEFT JOIN memory_agent_experience e ON e.rowid=(
                    SELECT x.rowid FROM memory_agent_experience x WHERE x.ref=a.ref AND x.visibility='public'
                    ORDER BY x.agent,x.receipt LIMIT 1)
                WHERE j.state IN ('pending','running') AND j.released IS NOT NULL AND j.available<=?
                    AND a.redacted=0 {scope_filter}
            ), readiness AS (
                SELECT *,count(*) OVER room AS turns,max(released) OVER room AS latest
                FROM eligible WINDOW room AS (PARTITION BY actor,scope,source,domain)
            )
            SELECT * FROM readiness WHERE domain<>0 OR turns>=? OR latest<=? OR ? ORDER BY ref LIMIT 72""", params)
            grouped = {}
            for row in rows:
                group = (row["actor"], row["scope"], row["source"], row["domain"])
                grouped.setdefault(group, []).append(dict(row))
        return [rows[offset:offset + 6] for rows in grouped.values() for offset in range(0,len(rows),6)][:12]

    def expire_unanswered(self):
        with self.service.transaction(write=True, timeout=0.1) as db:
            db.execute("""UPDATE memory_analysis SET state='discarded',lease=NULL
                WHERE state IN ('pending','running') AND released IS NULL
                AND ref IN (SELECT ref FROM activities WHERE received<?)""",
                (self.service.clock() - 86400,))

    def queue_status(self, access):
        access.require_recall()
        access.require(moderate=access.scope.kind == "group")
        with self.service.transaction() as db:
            counts = dict(db.execute("""SELECT CASE WHEN j.state='pending' AND j.released IS NULL
                THEN 'awaiting_reply' ELSE j.state END AS phase,count(*)
                FROM memory_analysis j JOIN activities a USING(ref) WHERE a.scope=? AND a.actor=?
                AND a.source=? GROUP BY phase""",
                (access.scope.storage_key, access.principal, self.encode_source(access.source))))
        access.require_recall()
        return counts

    def count_request(self, sources, input_tokens, output_tokens):
        day = int(self.service.clock() // 86400)
        with self.service.transaction(write=True) as db:
            db.execute("""INSERT INTO memory_analysis_usage VALUES (?,1,?,?,?)
              ON CONFLICT(day) DO UPDATE SET calls=calls+1,sources=sources+excluded.sources,
              input_tokens=input_tokens+excluded.input_tokens,output_tokens=output_tokens+excluded.output_tokens""",
              (day,sources,input_tokens,output_tokens))
            db.execute("DELETE FROM memory_analysis_usage WHERE day<?", (day-90,))

    def claim(self, access, ref, source_allowed=None):
        access.require_recall()
        now = self.service.clock()
        lease = uuid4().hex
        with self.service.transaction(write=True) as db:
            row, public = self.evidence(db, access, ref)
            if not db.execute(
                    """UPDATE memory_analysis SET state='running',available=?,lease=?
                       WHERE ref=? AND state IN ('pending','running') AND released IS NOT NULL AND available<=?""",
                    (now + 90, lease, ref, now)).rowcount:
                return None
            facts = [dict(item) for item in db.execute(
                """SELECT f.fact_key AS key,f.revision,v.content,a.source FROM facts f
                   JOIN fact_versions v USING(scope,fact_key,revision) JOIN activities a ON a.ref=v.ref
                   WHERE f.scope=? AND f.actor=? AND f.status='active' AND f.fact_key LIKE 'auto-%'
                   AND a.redacted=0 AND v.content IS NOT NULL AND ? ORDER BY v.occurred DESC LIMIT 16""",
                (access.scope.storage_key, access.principal, access.scope.kind == "private" and not public))
                if not sensitive_information(item["content"])]
        current_source = self.encode_source(access.source)
        facts = [
            {"key": item["key"], "revision": item["revision"], "content": item["content"][:300],
             "source": item["source"]}
            for item in facts if item["source"] == current_source or (source_allowed and source_allowed(
                access.principal, access.scope.storage_key, item["source"]))
        ]
        access.require_recall()
        return lease, row["summary"], facts, public

    @staticmethod
    def _merge_candidates(db, access, ref):
        return [dict(row) for row in db.execute("""SELECT c.id,a.ref,a.summary,NULLIF(c.title,'') AS title
            FROM memory_card c JOIN memory_card_sources s ON s.card=c.id JOIN activities a USING(ref)
            WHERE c.scope=? AND c.principal=? AND c.kind IN ('owner','event') AND c.clarity>0
            AND c.hidden=0 AND NOT (c.pinned OR c.first_meet OR c.initial)
            AND a.actor=c.principal AND a.scope=c.scope AND a.kind='message_received' AND a.redacted=0
            AND a.source=? AND NOT EXISTS (SELECT 1 FROM memory_card_sources other
                WHERE other.card=c.id AND other.ref<>s.ref)
            AND (a.ref=? OR a.ref IN (SELECT ref FROM memory_analysis WHERE state='done'))
            ORDER BY a.ref=? DESC,c.created DESC LIMIT 9""",
            (access.scope.storage_key, access.principal, MemoryProjections.encode_source(access.source), ref, ref))
            if not row['title'] and not sensitive_information(row['summary'])]

    def card_candidates(self, access, ref):
        access.require_recall()
        with self.service.transaction() as db:
            rows = self._merge_candidates(db, access, ref)
        if not rows or rows[0]['ref'] != ref:
            return {}
        return {row['id']: (row['ref'], row['summary']) for row in rows[1:]}

    def _merge_card(self, db, access, ref, selected, candidates):
        rows = self._merge_candidates(db, access, ref)
        if not rows or rows[0]['ref'] != ref or selected not in candidates:
            return
        if any(row['id'] == selected and (row['ref'], row['summary']) == candidates[selected] for row in rows[1:]):
            db.execute('SAVEPOINT analysis_card')
            try:
                card.designate(db, access.scope.storage_key, access.principal, (rows[0]['id'], selected),
                               '', self.service.clock(), automatic=True)
            except (PermissionError, ValueError):
                db.execute('ROLLBACK TO analysis_card')
            finally:
                db.execute('RELEASE analysis_card')

    @staticmethod
    def evidence(db, access, ref):
        row = db.execute("""SELECT * FROM activities WHERE ref=? AND scope=? AND actor=?
            AND redacted=0""",
                         (ref, access.scope.storage_key, access.principal)).fetchone()
        if (row is None or not row["summary"] or row["summary"] == "收到消息"
                or sensitive_information(row["summary"])):
            raise PermissionError("Memory analysis evidence unavailable")
        public = [tuple(item) for item in db.execute(
            """SELECT agent,receipt FROM memory_agent_experience WHERE ref=? AND visibility='public'
               AND authority=?""", (ref, MemoryProjections.encode_source(access.source)))]
        if not public and json.loads(row["source"]) != asdict(access.source):
            raise PermissionError("Memory analysis source changed")
        if not public and (row["producer"], row["kind"], row["state"]) != ("core", "message_received", "observed"):
            raise PermissionError("Unsupported analysis source")
        return row, public

    def defer(self, ref):
        now = self.service.clock()
        with self.service.transaction(write=True, timeout=0.1) as db:
            db.execute("""UPDATE memory_analysis SET available=? WHERE ref=?
                AND state IN ('pending','running') AND available<=?""", (now + 60, ref, now))

    def retry(self, ref, lease, error):
        with self.service.transaction(write=True) as db:
            row = db.execute("SELECT attempts FROM memory_analysis WHERE ref=? AND lease=?",
                             (ref, lease)).fetchone()
            if row:
                attempts = row["attempts"] + 1
                db.execute("""UPDATE memory_analysis SET attempts=?,state=?,available=?,lease=NULL,error=?
                              WHERE ref=? AND lease=?""",
                           (attempts, "pending" if attempts < 8 else "failed",
                            self.service.clock() + min(3600, 30 * 2**attempts), error, ref, lease))

    def apply(self, access, ref, lease, proposals, candidates, cards=None):
        access.require(write=True)
        with self.service.transaction(write=True) as db:
            if not db.execute("SELECT 1 FROM memory_analysis WHERE ref=? AND lease=? AND state='running'",
                              (ref, lease)).fetchone():
                return False
            evidence, public = self.evidence(db, access, ref)
            for proposal in proposals:
                kind = proposal["type"]
                if public:
                    if kind == "theme" and (proposal["status"] != "done" or evidence["state"] == "succeeded"):
                        db.executemany("INSERT OR IGNORE INTO memory_agent_theme VALUES (?,?,?,?,?,?)",
                                       ((agent, receipt, proposal["label"], proposal["kind"],
                                         proposal["status"], proposal["confidence"]) for agent, receipt in public))
                    continue
                if kind == "theme":
                    continue
                if kind == "social":
                    if access.scope.kind == "private":
                        db.execute("INSERT OR IGNORE INTO memory_social_signal VALUES (?,?,?,?,?,?)",
                                   (ref, access.principal, proposal["field"], proposal["value"],
                                    proposal["confidence"], int(proposal["explicit"])))
                elif kind == "card":
                    db.execute("""UPDATE memory_card SET importance=?,emotional=?,reasons='["model-assessed"]'
                        WHERE pinned=0 AND first_meet=0 AND initial=0 AND hidden=0 AND kind<>'merged' AND id IN (
                        SELECT card FROM memory_card_sources WHERE ref=?)""",
                               (round(proposal["importance"] * 100), round(proposal["emotion"] * 24), ref))
                elif kind == "expression_alias":
                    alias, canonical = proposal["alias"], proposal["canonical"]
                    if (access.scope.kind == "group" and alias in evidence["summary"]
                            and canonical in evidence["summary"]):
                        db.execute("INSERT OR IGNORE INTO memory_group_alias VALUES (?,?,?)",
                                   (ref, alias, canonical))
                elif kind == "expression":
                    if access.scope.kind == "group" and proposal["expression"] in evidence["summary"]:
                        db.execute("INSERT OR IGNORE INTO memory_group_expression VALUES (?,?,?)",
                                   (ref, proposal["expression"], proposal["meaning"]))
                elif kind == "card_merge":
                    if cards:
                        self._merge_card(db, access, ref, proposal["card_id"], cards)
                elif access.scope.kind == "private":
                    self._apply_fact(db, access, ref, proposal, candidates)
            access.require(write=True)
            db.execute("UPDATE memory_analysis SET state='done',lease=NULL,error=NULL WHERE ref=?", (ref,))
            if access.scope.kind == "private" and not public:
                card.refresh_initial(db, access.scope.storage_key)
            access.require(write=True)
        return True

    def _apply_fact(self, db, access, ref, proposal, candidates):
        operation = proposal["operation"]
        key = proposal["key"]
        if operation == "add":
            digest = hashlib.sha256((access.principal + "\\0" + key).encode()).hexdigest()
            key = "auto-n01-" + digest
            if db.execute("SELECT 1 FROM facts WHERE scope=? AND fact_key=?",
                          (access.scope.storage_key, key)).fetchone():
                return
        elif key not in candidates:
            return
        if operation == "conflict":
            db.execute("INSERT OR IGNORE INTO memory_analysis_conflict VALUES (?,?)", (ref, key))
            return
        if operation == "reinforce":
            db.execute("""INSERT INTO fact_review(scope,fact_key,revision,last_seen,count)
                SELECT scope,fact_key,revision,?,1 FROM facts
                WHERE scope=? AND fact_key=? AND revision=? AND status='active'
                ON CONFLICT(scope,fact_key,revision) DO UPDATE SET
                count=min(count+1,999),last_seen=excluded.last_seen""",
                       (self.service.clock(), access.scope.storage_key, key, candidates[key]))
            return
        db.execute("SAVEPOINT analysis_fact")
        try:
            self.service.put_fact(access, key, proposal["text"], proposal["importance"], ref,
                                  expected_revision=candidates.get(key), automatic=True, _db=db)
        except (PermissionError, ValueError):
            db.execute("ROLLBACK TO analysis_fact")
            db.execute("INSERT OR IGNORE INTO memory_analysis_conflict VALUES (?,?)", (ref, key))
        finally:
            db.execute("RELEASE analysis_fact")

    @staticmethod
    def encode_source(source):
        return json.dumps(asdict(source), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def publish_experience(self, access, agent, receipt, ref, *, visibility, verify_public=None,
                           _db=None, _origin=None):
        """Called by a trusted receipt producer after the destination policy has been checked."""
        access.require(write=True)
        identifier(agent)
        identifier(receipt)
        if visibility not in {"public", "source"}:
            raise ValueError("Invalid experience visibility")
        with (self.service.transaction(write=True) if _db is None else nullcontext(_db)) as db:
            row = db.execute("SELECT * FROM activities WHERE ref=? AND scope=? AND actor=?",
                             (ref, access.scope.storage_key, access.principal)).fetchone()
            if (not row or row["redacted"] or not row["evidence"] or not row["summary"]
                    or sensitive_information(row["summary"])
                    or row["source"] != self.encode_source(_origin or access.source)
                    or row["kind"] not in {kind.value for kind in (
                        ActivityKind.BROWSE, ActivityKind.READ, ActivityKind.TASK, ActivityKind.TOOL,
                        ActivityKind.COMMENT, ActivityKind.MESSAGE_SENT)}
                    or (row["state"] != ActivityState.SUCCEEDED.value and not (
                        row["state"] == ActivityState.OBSERVED.value
                        and row["kind"] in {"browse", "read", "message_sent"}))):
                raise PermissionError("A confirmed receipt in the authorized source is required")
            if row["kind"] == "message_sent" and (row["producer"] != "core"
                    or not row["evidence"].startswith("provider-send-return:")):
                raise PermissionError("A verified provider submission observation is required")
            if visibility == "public" and (row["kind"] not in {"browse", "read"} or not callable(verify_public)
                                           or verify_public(dict(row)) is not True):
                raise PermissionError("Only verified public content receipts may be shared")
            if db.execute("SELECT 1 FROM memory_erasures WHERE scope=? AND digest=?",
                          (access.scope.storage_key, self.receipt_digest(agent, receipt))).fetchone():
                raise PermissionError("Experience was erased")
            existing = db.execute("SELECT ref,visibility,authority FROM memory_agent_experience WHERE agent=? AND receipt=?",
                                  (agent, receipt)).fetchone()
            authority = self.encode_source(access.source)
            same_activity = db.execute("SELECT receipt FROM memory_agent_experience WHERE agent=? AND ref=?",
                                       (agent, ref)).fetchone()
            if same_activity and same_activity["receipt"] != receipt:
                raise ValueError("Activity already attached to an experience receipt")
            if existing:
                if tuple(existing) != (ref, visibility, authority):
                    raise ValueError("Experience receipt already assigned")
                return False
            db.execute("INSERT INTO memory_agent_experience VALUES (?,?,?,?,?)",
                       (agent, receipt, ref, visibility, authority))
            if visibility == "public":
                db.execute("INSERT OR IGNORE INTO memory_analysis(ref) VALUES (?)", (ref,))
            access.require(write=True)
        return True

    def group_expressions(self, access, query, source_allowed):
        if access.scope.kind != "group":
            return []
        access.require_recall()
        with self.service.transaction() as db:
            epoch = self.epoch(db)
            alias_evidence = db.execute("""SELECT DISTINCT g.alias,g.canonical,a.actor,a.source,
                    CAST(a.occurred/86400 AS INTEGER) AS day
                FROM memory_group_alias g JOIN activities a USING(ref)
                WHERE a.scope=? AND a.redacted=0 AND a.occurred>=? AND instr(?,g.alias)>0""",
                (access.scope.storage_key, self.service.clock() - 60 * 86400, query)).fetchall()
            aliases = {}
            for row in alias_evidence:
                if source_allowed(row["actor"], access.scope.storage_key, row["source"]):
                    aliases.setdefault(row["alias"], {}).setdefault(row["canonical"], []).append(row)
            proven = {}
            for alias, targets in aliases.items():
                if len(targets) == 1:
                    canonical, rows = next(iter(targets.items()))
                    if len({row["actor"] for row in rows}) >= 2 and len({row["day"] for row in rows}) >= 2:
                        proven[alias] = canonical
            evidence = db.execute("""SELECT DISTINCT g.expression,g.meaning,a.actor,a.source,CAST(a.occurred/86400 AS INTEGER) AS day
                FROM memory_group_expression g JOIN activities a USING(ref)
                WHERE a.scope=? AND a.redacted=0 AND a.occurred>=?
                  AND (instr(?,g.expression)>0 OR g.expression IN (SELECT value FROM json_each(?)))
                ORDER BY day DESC""",
                (access.scope.storage_key, self.service.clock() - 60 * 86400, query,
                 json.dumps(sorted(set(proven.values()))))).fetchall()
        candidates = {}
        for row in evidence:
            if source_allowed(row["actor"], access.scope.storage_key, row["source"]):
                candidates.setdefault(row["expression"], {}).setdefault(row["meaning"], []).append(row)
        result = []
        for expression, meanings in candidates.items():
            if len(meanings) != 1:
                continue
            meaning, rows = next(iter(meanings.items()))
            if len({row["actor"] for row in rows}) >= 2 and len({row["day"] for row in rows}) >= 2:
                result.append({"expression": expression, "meaning": meaning})
        meanings = {item["expression"]: item["meaning"] for item in result}
        for alias, canonical in proven.items():
            if alias not in candidates and canonical in meanings:
                result.append({"expression": alias, "meaning": meanings[canonical]})
        access.require_recall()
        with self.service.transaction() as db:
            return result[:4] if self.epoch(db) == epoch else []

    def experience_index_rows(self, agent, model, after=0, limit=48):
        with self.service.transaction(timeout=0.25) as db:
            return db.execute("""SELECT a.ref,a.summary,a.actor,a.scope,e.authority,e.receipt,v.vector
                FROM memory_agent_experience e JOIN activities a USING(ref)
                LEFT JOIN memory_experience_vectors v ON v.agent=e.agent AND v.receipt=e.receipt AND v.model=?
                WHERE e.agent=? AND a.redacted=0 AND a.summary NOT LIKE '通信适配器已返回%' AND a.ref>? ORDER BY a.ref LIMIT ?""",
                (model,agent,after,limit)).fetchall()

    def store_experience_vector(self, agent, model, row, vector):
        with self.service.transaction(write=True, timeout=0.25) as db:
            db.execute("""INSERT INTO memory_experience_vectors
                SELECT e.agent,e.receipt,?,? FROM memory_agent_experience e JOIN activities a USING(ref)
                WHERE e.agent=? AND e.receipt=? AND a.ref=? AND a.summary=? AND a.redacted=0
                ON CONFLICT(agent,receipt,model) DO UPDATE SET vector=excluded.vector""",
                (model,json.dumps(vector),agent,row["receipt"],row["ref"],row["summary"]))

    def public_index_rows(self, agent, space, dimensions, limit=4):
        with self.service.transaction(timeout=0.25) as db:
            active = public_experience.recent(db, agent)
            ids = json.dumps([row["content_id"] for row in active])
            return db.execute("""SELECT p.* FROM memory_public_content p
                LEFT JOIN memory_public_vectors v ON v.agent=p.agent AND v.content_id=p.content_id AND v.space=?
                WHERE p.agent=? AND p.content_id IN (SELECT value FROM json_each(?))
                AND (p.occurred>=? OR p.pinned=1 OR p.importance>=0.75)
                AND (v.vector IS NULL OR CASE WHEN json_valid(v.vector) THEN json_array_length(v.vector)<>? ELSE 1 END)
                ORDER BY p.pinned DESC,p.importance DESC,p.occurred DESC,p.content_id LIMIT ?""",
                (space,agent,ids,self.service.clock()-180*86400,dimensions,limit)).fetchall()

    def store_public_vector(self, row, space, vector):
        with self.service.transaction(write=True, timeout=0.25) as db:
            db.execute("""INSERT INTO memory_public_vectors
                SELECT agent,content_id,?,? FROM memory_public_content
                WHERE agent=? AND content_id=? AND revision=?
                ON CONFLICT(agent,content_id,space) DO UPDATE SET vector=excluded.vector""",
                (space,json.dumps(vector),row["agent"],row["content_id"],row["revision"]))

    def maintain_public(self, agent):
        with self.service.transaction(write=True, timeout=0.25) as db:
            db.execute("""DELETE FROM memory_public_vectors WHERE agent=? AND content_id IN (
                SELECT v.content_id FROM memory_public_content p
                JOIN memory_public_vectors v USING(agent,content_id)
                WHERE p.agent=? AND p.occurred<? AND p.pinned=0 AND p.importance<0.75
                ORDER BY p.occurred LIMIT 32)""", (agent,agent,self.service.clock()-180*86400))

    def set_public_focus(self, agent, topic, action, authorized):
        identifier(agent)
        if not authorized():
            raise PermissionError("Focus feedback requires current owner authority")
        with self.service.transaction(write=True) as db:
            if not authorized():
                raise PermissionError("Focus feedback authorization changed")
            return public_experience.set_focus(db, agent, topic, action, self.service.clock())

    def pin_public(self, agent, content_id, authorized):
        identifier(agent)
        identifier(content_id)
        if not authorized():
            raise PermissionError("Public experience pin requires current owner authority")
        with self.service.transaction(write=True) as db:
            if not authorized():
                raise PermissionError("Public experience pin authorization changed")
            if not db.execute("UPDATE memory_public_content SET pinned=1 WHERE agent=? AND content_id=?",
                              (agent,content_id)).rowcount:
                raise ValueError("Public experience not found")

    def forget_public(self, agent, content_id, authorized):
        identifier(agent)
        identifier(content_id)
        if not authorized():
            raise PermissionError("Public experience deletion requires current owner authority")
        with self.service.transaction(write=True) as db:
            if not authorized():
                raise PermissionError("Public experience deletion authorization changed")
            public_experience.forget(db, agent, content_id)

    def snapshot(self, access, agent, source_allowed, query, *, public_only=False, model=None, vector=None):
        from fat_fish.memory.semantic import stored_vector
        from fat_fish.memory.service import bm25, normalize, terms as tokenize

        access.require_recall()
        verify_source = source_allowed
        source_allowed = lru_cache(maxsize=128)(source_allowed)
        terms = {word for word in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", query.lower())
                 if len(word) > 1 or ord(word) >= 0x3400}
        with self.service.transaction() as db:
            epoch = self.epoch(db)
            public_recent = public_experience.recent(db, agent)
            public_rows = public_experience.search(db, agent, tokenize(query), model, public_recent)
            interests = public_experience.interests(public_recent, self.service.clock())
            for item in interests:
                focus = db.execute("""SELECT state,revision FROM memory_public_focus
                    WHERE agent=? AND topic=? ORDER BY revision DESC LIMIT 1""",
                    (agent,item["topic"].strip().casefold())).fetchone()
                if focus:
                    item["focus_state"] = focus["state"]
                    item["focus_revision"] = focus["revision"]
            projections = db.execute("""WITH candidates AS (
                SELECT ref FROM (
                    SELECT a.ref FROM activities a INDEXED BY activities_scope_time
                    JOIN memory_agent_experience e ON e.ref=a.ref
                    WHERE ? AND a.scope=? AND a.actor=? AND e.agent=? AND e.visibility='source' AND a.redacted=0
                    ORDER BY a.occurred DESC,a.ref DESC LIMIT 96)
                UNION ALL
                SELECT ref FROM (
                    SELECT ref FROM memory_agent_experience
                    WHERE agent=? AND visibility='public' ORDER BY ref DESC LIMIT 96)
                )
                SELECT a.ref,a.scope,a.actor,a.kind,a.state,a.summary,a.occurred,a.source,
                    e.visibility,e.authority,v.vector
                FROM candidates c JOIN activities a USING(ref)
                JOIN memory_agent_experience e ON e.ref=a.ref AND e.agent=?
                LEFT JOIN memory_experience_vectors v ON v.agent=e.agent AND v.receipt=e.receipt AND v.model=?
                WHERE a.redacted=0 AND a.summary NOT LIKE '通信适配器已返回%'
                ORDER BY a.occurred DESC,a.ref DESC""",
                (not public_only, access.scope.storage_key, access.principal, agent, agent, agent, model)).fetchall()
            themes = [dict(row) for row in db.execute("""SELECT t.theme,t.kind,t.status,t.confidence,a.occurred,
                a.actor,a.scope,e.authority FROM memory_agent_theme t
                JOIN memory_agent_experience e USING(agent,receipt) JOIN activities a USING(ref)
                WHERE t.agent=? AND e.visibility='public' AND a.redacted=0 AND a.occurred>=?
                ORDER BY a.occurred DESC,a.ref DESC LIMIT 32""",
                (agent, self.service.clock() - 30 * 86400))]
            signals = [dict(row) for row in db.execute("""SELECT s.*,a.source,a.occurred,a.scope
                FROM memory_social_signal s JOIN activities a USING(ref)
                WHERE s.principal=? AND a.redacted=0 AND a.occurred>=?
                ORDER BY a.occurred DESC,a.ref DESC LIMIT 250""",
                (access.principal, self.service.clock() - 180 * 86400))]
        active = {}
        for row in signals:
            if source_allowed(row["principal"], row["scope"], row["source"]):
                active.setdefault(row["field"], []).append(row)
        profile = {}
        for field, rows in active.items():
            explicit = next((row for row in rows if row["explicit"]), None)
            if explicit:
                profile[field] = explicit["value"]
            elif len(rows) >= 3 and len({int(row["occurred"] // 86400) for row in rows}) >= 2:
                weights = [row["confidence"] * 2 ** (-max(0, self.service.clock() - row["occurred"]) / (45 * 86400))
                           for row in rows]
                if sum(weights) > 0:
                    profile[field] = round(sum(row["value"] * w for row, w in zip(rows, weights)) / sum(weights), 2)
        if active:
            events = {(row["ref"], row["occurred"]) for rows in active.values() for row in rows}
            profile["familiarity"] = min(1, len(events) / 20)
        authorized = [row for row in projections
                      if source_allowed(row["actor"], row["scope"], row["authority"])]
        detached = [dict(row) | {"ref": row["content_id"], "kind": "read", "state": "observed",
                                  "visibility": "public", "summary": row["title"] + "。 " + row["summary"]}
                    for row in public_rows]
        authorized.extend(detached)
        texts = [normalize(row["summary"]) for row in authorized]
        lexical = bm25(tokenize(query), texts)
        experiences = []
        for row, text, lexical_score in zip(authorized, texts, lexical):
            candidate = stored_vector(row["vector"], len(vector)) if vector else None
            similarity = sum(a*b for a,b in zip(vector,candidate)) if candidate else 0.0
            if not lexical_score and similarity < 0.60 and terms:
                continue
            recency = 2 ** (-max(0, self.service.clock()-row["occurred"]) / (90*86400))
            detached = isinstance(row, dict)
            importance = row["importance"] * row["confidence"] if detached else (
                1.0 if row["state"] == "succeeded" else 0.6)
            score = 0.56*max(0, similarity) + 0.30*lexical_score/(1+lexical_score) + 0.08*recency + 0.06*importance
            item = {key: row[key] for key in ("ref", "kind", "state", "summary", "occurred", "visibility")}
            item["platform"] = row["platform"] if detached else json.loads(row["source"])["platform"]
            if detached:
                item["revision"] = row["revision"]
            item["reason"] = ("hybrid" if lexical_score else "semantic") if similarity >= .60 else (
                "lexical-match" if lexical_score else "recent-receipt")
            experiences.append((score, text, item, row))
        experiences.sort(key=lambda result: result[0], reverse=True)
        seen = set()
        results = []
        confirmed = lru_cache(maxsize=16)(verify_source)
        for _, text, item, row in experiences:
            if text not in seen and (isinstance(row, dict) or confirmed(row["actor"], row["scope"], row["authority"])):
                seen.add(text)
                results.append(item)
            if len(results) == 3:
                break
        goals = {}
        for row in themes:
            theme = " ".join(row["theme"].casefold().split())
            if (terms and not any(term in theme for term in terms)) or not source_allowed(
                    row["actor"], row["scope"], row["authority"]):
                continue
            key = (row["kind"], theme)
            item = goals.setdefault(key, {name: row[name] for name in ("theme", "kind", "status", "confidence")}
                                    | {"evidence_count": 0, "salience": 0.0})
            age = max(0, self.service.clock() - row["occurred"])
            weight = row["confidence"] * 2 ** (-age / (14 * 86400))
            item["evidence_count"] += 1
            item["salience"] += -weight if row["status"] == "rejected" else weight
        relevant_goals = [item | {"salience": round(item["salience"], 2)} for item in goals.values()
                          if item["salience"] > 0 and item["status"] != "rejected"]
        for item in relevant_goals:
            if item["status"] == "hypothesis" and item["evidence_count"] >= 2 and item["salience"] >= 1:
                item["status"] = "active"
        relevant_goals.sort(key=lambda item: item["salience"], reverse=True)
        access.require_recall()
        with self.service.transaction() as db:
            if self.epoch(db) != epoch:
                return {}, [], []
        interests = [item for item in interests if item.get("focus_state") != "abandoned"]
        return profile, results, interests + relevant_goals[:max(0,4-len(interests))]

    @staticmethod
    def epoch(db):
        return db.execute("SELECT value FROM memory_projection_epoch WHERE id=1").fetchone()[0]

    @staticmethod
    def receipt_digest(agent, receipt):
        return hashlib.sha256(("agent-experience:" + json.dumps([agent, receipt])).encode()).hexdigest()

    def erase_scope(self, db, scope):
        db.execute("DELETE FROM memory_capacity_status WHERE scope=?", (scope,))
        from fat_fish.memory.service import content_digest

        rows = db.execute("""SELECT e.agent,e.receipt,a.ref,a.summary FROM memory_agent_experience e
            JOIN activities a USING(ref) WHERE a.scope=?""", (scope,)).fetchall()
        for row in rows:
            db.executemany("INSERT OR IGNORE INTO memory_erasures VALUES (?,?)", (
                (scope, self.receipt_digest(row["agent"], row["receipt"])),
                (scope, content_digest(row["summary"])),
            ))
        db.executemany("UPDATE activities SET summary='',evidence='',redacted=1 WHERE ref=?",
                       ((row["ref"],) for row in rows))

    def replay(self, db, scope, erased):
        rows = db.execute("""SELECT e.agent,e.receipt FROM memory_agent_experience e
            JOIN activities a USING(ref) WHERE a.scope=?""", (scope,)).fetchall()
        db.executemany("DELETE FROM memory_agent_experience WHERE agent=? AND receipt=?",
                       (tuple(row) for row in rows if self.receipt_digest(*row) in erased))
