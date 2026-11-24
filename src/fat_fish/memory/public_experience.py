"""Bot-owned projections of independently verified public content."""
from dataclasses import dataclass
import hashlib
import json
import math
import re

from fat_fish.communication.model import identifier, key
from fat_fish.memory.experience import sensitive_information

DAY = 86400

SCHEMA = (
    """CREATE TABLE memory_group_alias (
        ref INTEGER NOT NULL REFERENCES activities ON DELETE CASCADE,
        alias TEXT NOT NULL, canonical TEXT NOT NULL, PRIMARY KEY(ref,alias))""",
    """CREATE TRIGGER memory_group_alias_erasure AFTER UPDATE OF summary,evidence,redacted ON activities
        WHEN NEW.summary<>OLD.summary OR NEW.evidence<>OLD.evidence OR NEW.redacted<>OLD.redacted
        BEGIN DELETE FROM memory_group_alias WHERE ref=OLD.ref; END""",
    """CREATE INDEX memory_legacy_public_recent ON memory_agent_experience(agent,visibility,ref DESC)""",
    """CREATE TABLE memory_public_focus (
        agent TEXT NOT NULL, topic TEXT NOT NULL, revision INTEGER NOT NULL,
        state TEXT NOT NULL, changed REAL NOT NULL, evidence_digest TEXT NOT NULL,
        PRIMARY KEY(agent,topic,revision))""",
    """CREATE TABLE memory_public_content (
      agent TEXT NOT NULL, content_id TEXT NOT NULL, platform TEXT NOT NULL,
      title TEXT NOT NULL, summary TEXT NOT NULL, topics TEXT NOT NULL,
      occurred REAL NOT NULL, revision INTEGER NOT NULL, confidence REAL NOT NULL,
      importance REAL NOT NULL, pinned INTEGER NOT NULL, evidence_digest TEXT NOT NULL,
      PRIMARY KEY(agent,content_id))""",
    """CREATE INDEX memory_public_recent ON memory_public_content(agent,pinned DESC,occurred DESC,content_id)""",
    """CREATE INDEX memory_public_important ON memory_public_content(agent,importance DESC,occurred DESC,content_id)""",
    """CREATE INDEX memory_public_age ON memory_public_content(agent,occurred,content_id)""",
    """CREATE TABLE memory_public_terms (
      agent TEXT NOT NULL, term TEXT NOT NULL, occurred REAL NOT NULL, content_id TEXT NOT NULL,
      PRIMARY KEY(agent,term,occurred,content_id),
      FOREIGN KEY(agent,content_id) REFERENCES memory_public_content ON DELETE CASCADE)""",
    """CREATE INDEX memory_public_terms_content ON memory_public_terms(agent,content_id)""",
    """CREATE TABLE memory_public_vectors (
      agent TEXT NOT NULL, content_id TEXT NOT NULL, space TEXT NOT NULL, vector TEXT NOT NULL,
      PRIMARY KEY(agent,content_id,space),
      FOREIGN KEY(agent,content_id) REFERENCES memory_public_content ON DELETE CASCADE)""",
)


@dataclass(frozen=True)
class PublicContent:
    """Returned by the trusted host verifier after independently checking the public resource."""

    content_id: str
    platform: str
    title: str
    summary: str
    topics: tuple[str, ...] = ()
    confidence: float = 1.0
    importance: float = 0.5

    def __post_init__(self):
        identifier(self.content_id)
        identifier(self.platform)
        if (not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", self.platform)
                or not self.content_id.startswith(self.platform + ":")
                or not re.fullmatch(r"[a-z0-9-]+:[A-Za-z0-9._/-]{1,160}", self.content_id)):
            raise ValueError("Public content ID must identify its platform")
        if not isinstance(self.topics, tuple) or len(self.topics) > 8:
            raise ValueError("Invalid public topics")
        for text, limit in ((self.title, 180), (self.summary, 720), *((topic, 40) for topic in self.topics)):
            if not isinstance(text, str) or not text.strip() or len(text) > limit or sensitive_information(text):
                raise ValueError("Invalid or sensitive public material")
        for value in (self.confidence, self.importance):
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("Invalid public material weight")


def digest(content_id):
    return hashlib.sha256(("public-content:" + content_id).encode()).hexdigest()


def scope(agent):
    return key("agent-public", agent)


def write(db, agent, material, at, receipt):
    from fat_fish.memory.service import terms

    identity = (agent, material.content_id)
    if db.execute("SELECT 1 FROM memory_erasures WHERE scope=? AND digest=?",
                  (scope(agent), digest(material.content_id))).fetchone():
        return
    current = db.execute("SELECT * FROM memory_public_content WHERE agent=? AND content_id=?", identity).fetchone()
    topics = json.dumps(material.topics, ensure_ascii=False)
    content = (material.title, material.summary, topics, material.confidence, material.importance)
    if current and at <= current["occurred"]:
        return
    changed = not current or content != tuple(current[key] for key in (
        "title", "summary", "topics", "confidence", "importance"))
    revision = (current["revision"] if current else 0) + int(changed)
    db.execute("""INSERT INTO memory_public_content VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(agent,content_id) DO UPDATE SET title=excluded.title,summary=excluded.summary,
        topics=excluded.topics,occurred=excluded.occurred,revision=excluded.revision,
        confidence=excluded.confidence,importance=excluded.importance,evidence_digest=excluded.evidence_digest""",
        (*identity, material.platform, material.title, material.summary, topics, at, revision,
         material.confidence, material.importance, 0, hashlib.sha256(receipt.encode()).hexdigest()))
    if changed:
        db.execute("DELETE FROM memory_public_vectors WHERE agent=? AND content_id=?", identity)
    db.execute("DELETE FROM memory_public_terms WHERE agent=? AND content_id=?", identity)
    # Use the same normalization and segmentation as query matching. A separate title
    # pass protects short stable identifiers from being crowded out by long summaries.
    postings = set(terms(material.title)) | set(terms(material.summary))
    for topic in material.topics:
        postings.update(terms(topic))
    db.executemany("INSERT INTO memory_public_terms VALUES (?,?,?,?)",
                   ((agent, term, at, material.content_id) for term in postings))
    db.execute("UPDATE memory_projection_epoch SET value=value+1 WHERE id=1")


def forget(db, agent, content_id):
    db.execute("INSERT OR IGNORE INTO memory_erasures VALUES (?,?)", (scope(agent), digest(content_id)))
    removed = db.execute("SELECT topics FROM memory_public_content WHERE agent=? AND content_id=?",
                         (agent,content_id)).fetchone()
    db.execute("DELETE FROM memory_public_content WHERE agent=? AND content_id=?", (agent, content_id))
    if removed:
        drop_unsupported_focus(db, agent, json.loads(removed[0]))
    db.execute("UPDATE memory_projection_epoch SET value=value+1 WHERE id=1")


def replay(db, agent, erased):
    rows = db.execute("SELECT content_id,topics FROM memory_public_content WHERE agent=?", (agent,)).fetchall()
    removed = [row for row in rows if digest(row["content_id"]) in erased]
    db.executemany("DELETE FROM memory_public_content WHERE agent=? AND content_id=?",
                   ((agent, row["content_id"]) for row in removed))
    drop_unsupported_focus(db, agent, [topic for row in removed for topic in json.loads(row["topics"])])
    db.execute("UPDATE memory_projection_epoch SET value=value+1 WHERE id=1")


def drop_unsupported_focus(db, agent, topics):
    if not topics:
        return
    known = set()
    for row in db.execute("SELECT topics FROM memory_public_content WHERE agent=?", (agent,)):
        known.update(topic.casefold().strip() for topic in json.loads(row[0]))
    db.executemany("DELETE FROM memory_public_focus WHERE agent=? AND topic=?",
                   ((agent, topic) for topic in {name.casefold().strip() for name in topics} - known))


def recent(db, agent):
    active = db.execute("""SELECT * FROM memory_public_content
        WHERE agent=? ORDER BY pinned DESC,occurred DESC,content_id LIMIT 96""", (agent,)).fetchall()
    important = db.execute("""SELECT * FROM memory_public_content WHERE agent=? AND importance>=0.75
        ORDER BY importance DESC,occurred DESC,content_id LIMIT 24""", (agent,)).fetchall()
    return list({row["content_id"]: row for row in (*active, *important)}.values())


def search(db, agent, query_terms, model, recent_rows):
    ids = {row["content_id"] for row in recent_rows}
    for term in query_terms[:12]:
        ids.update(row[0] for row in db.execute("""SELECT content_id FROM memory_public_terms
            WHERE agent=? AND term=? ORDER BY occurred DESC,content_id DESC LIMIT 12""", (agent,term)))
    return db.execute("""SELECT p.*,v.vector FROM memory_public_content p
        LEFT JOIN memory_public_vectors v ON v.agent=p.agent AND v.content_id=p.content_id AND v.space=?
        WHERE p.agent=? AND p.content_id IN (SELECT value FROM json_each(?))""",
        (model, agent, json.dumps(sorted(ids)))).fetchall()


def set_focus(db, agent, topic, action, now):
    action = {"confirm": "confirmed", "activate": "active", "complete": "completed", "abandon": "abandoned"}.get(action)
    topic = topic.strip().casefold()
    entry = next((item for item in interests(recent(db, agent), now)
                  if item["topic"].strip().casefold() == topic), None)
    if not entry or not action:
        raise ValueError("Focus requires a sustained public interest and a valid action")
    latest = db.execute("""SELECT revision,state FROM memory_public_focus
        WHERE agent=? AND topic=? ORDER BY revision DESC LIMIT 1""", (agent,topic)).fetchone()
    current = latest["state"] if latest else "observed"
    allowed = {"observed": {"confirmed", "abandoned"}, "confirmed": {"active", "abandoned"},
               "active": {"completed", "abandoned"}, "completed": {"confirmed"},
               "abandoned": {"confirmed"}}
    if action == current:
        return current
    if action not in allowed[current]:
        raise ValueError("Focus transition is not allowed")
    proof = hashlib.sha256(json.dumps(entry["evidence"], sort_keys=True).encode()).hexdigest()
    db.execute("INSERT INTO memory_public_focus VALUES (?,?,?,?,?,?)",
               (agent, topic, (latest["revision"] if latest else 0)+1, action,now,proof))
    db.execute("UPDATE memory_projection_epoch SET value=value+1 WHERE id=1")
    return action


def interests(rows, now):
    """An interest is supported by distinct public objects, never repeat reads."""
    groups = {}
    for row in rows:
        if now-row["occurred"] > 180*DAY:
            continue
        topics = {topic.casefold().strip(): topic for topic in json.loads(row["topics"])}
        for normalized, topic in topics.items():
            group = groups.setdefault(normalized, {"topic": topic, "evidence": [], "score": 0.0, "days": set()})
            group["evidence"].append({"content_id": row["content_id"], "revision": row["revision"]})
            group["days"].add(int(row["occurred"] // DAY))
            group["score"] += row["confidence"] * 2 ** (-max(0, now-row["occurred"])/(60*DAY))
    qualified = [row for row in groups.values() if len(row["evidence"]) >= 3
                 and len(row["days"]) >= 2 and row["score"] >= 1.6]
    qualified.sort(key=lambda row: (-row["score"],row["topic"]))
    return [{"topic": row["topic"], "status": "sustained-interest", "evidence": row["evidence"][:4]}
            for row in qualified[:4]]
