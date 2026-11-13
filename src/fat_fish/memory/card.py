"""Scoped, SQLite-backed narrative memory.

Scoring and retention rules adapted from SurfCore (MIT), commit
0ce8580048d3dddb21860114edb8b115d5d79d1e. See surfcore.LICENSE.
"""
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import re
from uuid import NAMESPACE_URL, UUID, uuid5

from fat_fish.memory.activity import ActivityKind
from fat_fish.memory.experience import sensitive_information


DAY = 86400
METADATA_ONLY = {"", "收到消息", "通信适配器已返回回复提交", "通信适配器已返回主动推送提交", "通信适配器已返回消息提交"}
STRONG = re.compile(r"喜欢|想念|想你|舍不得|难过|委屈|哭|心疼|感动|害怕|孤单|抱歉|对不起|谢谢|抱抱|亲亲|陪伴|永远|最爱|在乎|重要")
MEDIUM = re.compile(r"开心|高兴|笑死|可爱|温柔|生气|烦|郁闷|害羞|讨厌|夸|凶|累|困|饿|紧张|激动|委屈")
FIRST = re.compile(r"第一次|初次|头一次|最早|最开始|初次见面|认识你|刚认识|纪念日|第一天")
PROMISE = re.compile(r"答应|约定|说好|以后|下次|记得|不要忘|别忘了|发誓|保证|我会")
CONFLICT = re.compile(r"吵架|生气|道歉|对不起|和好|误会|不理你|冷战|凶你|原谅")
ABOUT_SELF = re.compile(r"我(?:喜欢|讨厌|想要|是|会|在|叫)|我的|wo(?:喜欢|讨厌|想要|是|会)", re.I)

SCHEMA = (
    """CREATE TABLE memory_card (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, principal TEXT NOT NULL, kind TEXT NOT NULL,
        created REAL NOT NULL, owner INTEGER NOT NULL, emotional INTEGER NOT NULL,
        importance INTEGER NOT NULL, reasons TEXT NOT NULL, pinned INTEGER NOT NULL,
        clarity INTEGER NOT NULL, initial INTEGER NOT NULL DEFAULT 0, tier TEXT NOT NULL DEFAULT '',
        first_meet INTEGER NOT NULL DEFAULT 0, last_seen REAL NOT NULL, reviews INTEGER NOT NULL DEFAULT 0,
        hidden INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL DEFAULT '',
        pin_override INTEGER NOT NULL DEFAULT 0, merged_from TEXT NOT NULL DEFAULT '[]'
    )""",
    "CREATE INDEX memory_card_scope ON memory_card(scope, hidden, created)",
    """CREATE TABLE memory_card_sources (
        card TEXT NOT NULL REFERENCES memory_card(id) ON DELETE CASCADE,
        ref INTEGER NOT NULL REFERENCES activities(ref) ON DELETE CASCADE,
        PRIMARY KEY(card,ref)
    )""",
    "CREATE INDEX memory_card_evidence ON memory_card_sources(ref,card)",
    """CREATE TABLE memory_card_archive (
        card TEXT PRIMARY KEY REFERENCES memory_card(id) ON DELETE CASCADE,
        archived REAL NOT NULL
    )""",
    """CREATE TABLE memory_card_settings (
        scope TEXT PRIMARY KEY, first_owner_at REAL, manual_meeting TEXT,
        last_day INTEGER
    )""",
    """CREATE TRIGGER erase_card_evidence AFTER UPDATE OF summary,redacted ON activities
        WHEN NEW.summary<>OLD.summary OR NEW.redacted<>OLD.redacted
        BEGIN
            DELETE FROM memory_card WHERE id IN (SELECT card FROM memory_card_sources WHERE ref=OLD.ref);
        END""",
)


def detect_emotion(text):
    return min(24, 8 * len(STRONG.findall(text)) + 3 * len(MEDIUM.findall(text))
               + int("!" in text or "！" in text))


def score(text, *, owner=False, private=False, direct=False):
    emotional = detect_emotion(text)
    first = bool(FIRST.search(text))
    promise = bool(PROMISE.search(text))
    conflict = bool(CONFLICT.search(text))
    signals = (
        (owner, 42 if private else 32, "owner-private" if private else "owner-group"),
        (direct, 8, "direct"), (emotional, emotional, "emotion"), (first, 30, "first"),
        (promise, 16, "promise"), (conflict, 14, "conflict"),
        (ABOUT_SELF.search(text), 5, "about-self"), (len(text) < 6, -12, "short"),
    )
    importance = max(1, min(100, 8 + sum(weight for active, weight, _ in signals if active)))
    pinned = first and (owner or importance >= 60)
    if first:
        kind = "first"
        if owner:
            importance = 100
    elif owner:
        kind = "owner"
    elif emotional >= 14:
        kind = "emotion"
    elif conflict:
        kind = "conflict"
    elif promise:
        kind = "promise"
    else:
        kind = "event"
    return importance, emotional, kind, pinned, [reason for active, _, reason in signals if active]


def protected(row):
    return row["pinned"] or row["first_meet"] or row["initial"] or (row["owner"] and row["importance"] >= 65)


def target_clarity(row, now):
    if row["pinned"] or row["first_meet"]:
        return 4
    age = max(0, (now - row["created"]) / DAY)
    since_seen = max(0, (now - row["last_seen"]) / DAY)
    preserve = protected(row)
    if age > 60 and row["importance"] < 50 and not preserve:
        return 0
    retention = row["importance"] + min(12, row["reviews"] * 2)
    retention += 18 if age <= 7 else 0
    retention += 8 if since_seen <= 2 else 0
    retention -= min(80, max(0, age - 7) * (0.12 if preserve else 0.35))
    retention = max(retention, 55 if preserve else 0, 50 if age <= 7 else 0)
    return sum(retention >= threshold for threshold in (12, 24, 46, 68))


def refresh_initial(db, scope):
    db.execute("INSERT OR IGNORE INTO memory_card_settings(scope) VALUES (?)", (scope,))
    setting = db.execute("SELECT * FROM memory_card_settings WHERE scope=?", (scope,)).fetchone()
    owners = db.execute("""SELECT * FROM memory_card WHERE scope=? AND owner=1 AND hidden=0 AND clarity>0
                           ORDER BY created,id""", (scope,)).fetchall()
    if not owners:
        return
    first_at = owners[0]["created"] if setting["first_owner_at"] is None else min(
        owners[0]["created"], setting["first_owner_at"])
    ids = {row["id"] for row in owners}
    meeting = setting["manual_meeting"] if setting["manual_meeting"] in ids else owners[0]["id"]
    db.execute("UPDATE memory_card_settings SET first_owner_at=? WHERE scope=?", (first_at, scope))
    for row in owners:
        elapsed = max(0, (row["created"] - first_at) // DAY)
        tier, probability = next((tier, chance) for limit, tier, chance in (
            (10, "first10", .90), (20, "mid10", .55), (30, "late10", .30),
            (float("inf"), "after", .03),
        ) if elapsed < limit)
        chance = int(hashlib.sha256((row["id"] + ":initial").encode()).hexdigest()[:8], 16) / 2**32
        first_meet = row["id"] == meeting
        db.execute("""UPDATE memory_card SET
            pinned=CASE WHEN ? AND first_meet=0 AND pin_override=0 THEN 1 ELSE pinned END,
            first_meet=?,initial=?,tier=?,
            clarity=CASE WHEN ? THEN 4 ELSE clarity END WHERE id=?""",
                   (first_meet, first_meet, first_meet or chance < probability,
                    "first-meet" if first_meet else tier, first_meet, row["id"]))


def capture(db, scope, principal, ref, turn, activity, *, owner, direct, private, update_meeting=True):
    kind = activity.kind.value
    if kind not in {"message_received", "message_sent", "browse", "read", "reminder", "comment"} or sensitive_information(activity.summary):
        return
    if activity.summary in METADATA_ONLY or activity.summary.lstrip().startswith("/"):
        return
    ident = str(uuid5(NAMESPACE_URL, json.dumps([scope, principal, turn or activity.event_id])))
    importance, emotional, category, pinned, reasons = score(
        activity.summary, owner=owner and kind == "message_received", private=private, direct=direct)
    created = db.execute("""INSERT OR IGNORE INTO memory_card
        (id,scope,principal,kind,created,owner,emotional,importance,reasons,pinned,clarity,last_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ident, scope, principal, category, activity.occurred, owner and kind == "message_received",
         emotional, importance, json.dumps(reasons), pinned, 4 if pinned else 3, activity.occurred)).rowcount
    db.execute("INSERT OR IGNORE INTO memory_card_sources VALUES (?,?)", (ident, ref))
    if owner and kind == "message_received":
        if not created:
            db.execute("""UPDATE memory_card SET owner=1,kind=?,created=min(created,?),emotional=max(emotional,?),
                importance=max(importance,?),reasons=? WHERE id=? AND owner=0""",
                (category, activity.occurred, emotional, importance, json.dumps(reasons), ident))
        if update_meeting:
            refresh_initial(db, scope)


def capture_fact(db, scope, principal, ref, occurred, fact_key, revision, importance):
    ident = str(uuid5(NAMESPACE_URL, json.dumps([scope, principal, fact_key, revision])))
    db.execute("""INSERT INTO memory_card
        (id,scope,principal,kind,created,owner,emotional,importance,reasons,pinned,clarity,last_seen)
        VALUES (?,?,?,'fact',?,0,0,?,'["explicit-or-extracted"]',0,3,?)""",
        (ident, scope, principal, occurred, round(importance * 100), occurred))
    db.execute("INSERT INTO memory_card_sources VALUES (?,?)", (ident, ref))


@dataclass(frozen=True)
class SavedActivity:
    event_id: str
    kind: ActivityKind
    occurred: float
    summary: str


def backfill(db, owner_id, *, limit=200):
    cursor = db.execute("SELECT ref FROM memory_card_backfill WHERE id=1").fetchone()
    rows = db.execute("""SELECT a.ref,event_id,kind,occurred,summary,actor,scope,turn_id,
        redacted OR EXISTS (SELECT 1 FROM memory_card_revoked_evidence r WHERE r.ref=a.ref)
        OR EXISTS (SELECT 1 FROM memory_card_sources s WHERE s.ref=a.ref) AS skip
        FROM activities a WHERE a.ref>? ORDER BY a.ref LIMIT ?""", (cursor[0], limit)).fetchall()
    owner_scopes = set()
    for row in rows:
        if row["skip"]:
            continue
        try:
            if str(UUID(row["actor"])) != row["actor"]:
                continue
        except ValueError:
            continue
        kind, subject = json.loads(row["scope"])
        if kind not in {"private", "group"} or (kind == "private" and subject != row["actor"]):
            continue
        if row["kind"] not in {"message_received", "message_sent"} or row["summary"] in METADATA_ONLY:
            continue
        activity = SavedActivity(row["event_id"], ActivityKind(row["kind"]), row["occurred"], row["summary"])
        capture(db, row["scope"], row["actor"], row["ref"], row["turn_id"], activity,
                owner=row["actor"] == owner_id, direct=kind == "private", private=kind == "private",
                update_meeting=False)
        if row["actor"] == owner_id and activity.kind == ActivityKind.MESSAGE_RECEIVED:
            owner_scopes.add(row["scope"])
    for scope in owner_scopes:
        refresh_initial(db, scope)
    if rows:
        db.execute("UPDATE memory_card_backfill SET ref=? WHERE id=1", (rows[-1]["ref"],))
    return len(rows)


def archive(db, scope, now):
    db.execute("""INSERT OR IGNORE INTO memory_card_archive
        SELECT id,? FROM memory_card WHERE scope=?""", (now, scope))


def contents(db, scope, *, archived=False):
    active = "" if archived else """AND c.hidden=0 AND c.clarity>0 AND (
        current_fact.fact_key IS NOT NULL OR NOT EXISTS (
            SELECT 1 FROM fact_versions v WHERE v.ref=a.ref))"""
    summary = "a.summary" if archived else "COALESCE(current_fact.content,a.summary)"
    facts = "SELECT NULL AS fact_key,NULL AS revision,NULL AS ref,NULL AS content WHERE 0" if archived else """SELECT f.fact_key,f.revision,v.ref,v.content
        FROM facts f JOIN fact_versions v USING(scope,fact_key,revision)
        WHERE f.status='active' AND v.content IS NOT NULL"""
    records = defaultdict(list)
    for row in db.execute(f"""WITH current_facts AS ({facts})
        SELECT c.*,a.ref,{summary} AS summary,a.source,a.occurred,a.state,a.kind AS activity_kind,
        current_fact.fact_key, current_fact.revision AS fact_revision,
        a.scope AS evidence_scope,a.actor AS evidence_principal,a.kind AS evidence_kind
        FROM memory_card c JOIN memory_card_sources s ON s.card=c.id
        JOIN activities a USING(ref) LEFT JOIN current_facts current_fact ON current_fact.ref=a.ref
        WHERE c.scope=? AND a.redacted=0 {active}
        ORDER BY c.created DESC,c.id,a.occurred,a.ref,current_fact.fact_key""", (scope,)):
        records[row["id"]].append(row)
    return records


def render(rows, *, full=False):
    row = rows[0]
    if row["first_meet"]:
        section = "初次相遇"
    elif row["pinned"]:
        section = "永久锚点"
    elif row["initial"]:
        section = "初期记忆"
    elif row["owner"] and row["importance"] >= 65:
        section = "主人重要记忆"
    else:
        section = ("互动", "记忆痕迹", "模糊记忆", "近期互动", "清晰互动")[row["clarity"]]
    length = (0, 28, 90, 240, 1500)[row["clarity"]]
    events = []
    for item in rows:
        if sensitive_information(item["summary"]):
            continue
        source = json.loads(item["source"])
        event = {"text": item["summary"] if full else item["summary"][:length],
                 "platform": source["platform"], "at": item["occurred"], "evidence": item["ref"],
                 "speaker": "bot" if item["activity_kind"] == "message_sent" else "user",
                 "state": item["state"]}
        if item["activity_kind"] != "message_sent":
            if json.loads(row["scope"])[0] == "group":
                event["speaker"] = "群友" + hashlib.sha256(
                    (row["scope"] + "|" + item["evidence_principal"]).encode()).hexdigest()[:8]
            if row["owner"] and item["evidence_principal"] == row["principal"]:
                event["role"] = "owner"
        if full:
            event["source"] = source
            event["scope"] = item["evidence_scope"]
            event["principal"] = item["evidence_principal"]
        events.append(event)
    if not events:
        return None
    result = {"type": "memory_card", "section": section, "id": row["id"],
              "title": row["title"], "events": events}
    if full:
        result["merged_from"] = json.loads(row["merged_from"])
        result["tags"] = tags(row)
        result["importance"] = 100 if row["first_meet"] else row["importance"]
        result["emotional"] = row["emotional"]
    return result


def tags(row):
    values = json.loads(row["inherited_tags"])
    flags = ((row["owner"], "owner"), (row["emotional"] >= 8, "emotion"),
             (row["kind"] == "first" or row["first_meet"], "first"),
             (row["initial"], "initial"), (row["kind"] == "merged", "merged"))
    return list(dict.fromkeys([*(tag for active, tag in flags if active), *values]))[:12]


def maintain(db, scope, now, max_chars=0):
    refresh_initial(db, scope)
    archive(db, scope, now)
    db.execute("INSERT OR IGNORE INTO memory_card_settings(scope) VALUES (?)", (scope,))
    day = int(now // DAY)
    claimed = db.execute("""UPDATE memory_card_settings SET last_day=?
        WHERE scope=? AND (last_day IS NULL OR last_day<?)""", (day, scope, day)).rowcount
    if not claimed:
        return
    for row in db.execute("SELECT * FROM memory_card WHERE scope=? AND hidden=0 AND clarity>0", (scope,)):
        current = row["clarity"]
        target = target_clarity(row, now)
        level = current + (target > current) - (target < current)
        if level != current:
            db.execute("UPDATE memory_card SET clarity=? WHERE id=?", (level, row["id"]))
    if max_chars:
        compact(db, scope, max_chars)


def compact(db, scope, max_chars):
    records = contents(db, scope)
    def size():
        return sum(len(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) + 1
                   for rows in records.values()
                   if (item := render(rows)) is not None)
    if size() <= max_chars:
        return
    candidates = sorted((rows for rows in records.values() if not protected(rows[0])),
                        key=lambda rows: (rows[0]["importance"], rows[0]["created"], rows[0]["clarity"]))
    for rows in candidates:
        row = rows[0]
        ident = row["id"]
        for level in range(row["clarity"] - 1, -1, -1):
            db.execute("UPDATE memory_card SET clarity=? WHERE id=?", (level, ident))
            if level:
                records[ident] = [{**item, "clarity": level} for item in rows]
            else:
                del records[ident]
            if size() <= max(400, int(max_chars * .9)):
                return


def designate(db, scope, principal, ids, title, now, *, meeting=False, source_scope_allowed=None):
    ids = tuple(dict.fromkeys(ids))
    if not 1 <= len(ids) <= 20 or (len(ids) < 2 and not meeting):
        raise ValueError("Invalid number of memory records")
    if len(title) > 80 or sensitive_information(title):
        raise ValueError("Invalid memory title")
    records = contents(db, scope)
    origins = {scope}
    if source_scope_allowed is not None and not meeting:
        for ident in ids:
            if ident in records:
                continue
            row = db.execute("SELECT scope,principal FROM memory_card WHERE id=?", (ident,)).fetchone()
            if row and row["principal"] == principal and source_scope_allowed(row["scope"]):
                match = [item for item in contents(db, row["scope"]).get(ident, ())
                         if item["evidence_kind"] == "message_received"]
                if match:
                    records[ident] = match
                    origins.add(row["scope"])
    if any(ident not in records or records[ident][0]["principal"] != principal for ident in ids):
        raise PermissionError("Memory record is unavailable in this scope")
    selected = sorted((records[ident][0] for ident in ids), key=lambda row: (row["created"], row["id"]))
    if meeting and not all(row["owner"] for row in selected):
        raise ValueError("First meeting requires owner interactions")
    if not meeting and any(row["first_meet"] for row in selected):
        raise ValueError("First meeting cannot enter a regular merge")
    cross_scope = len(origins) > 1 or any(evidence["evidence_scope"] != scope
                                         for ident in ids for evidence in records[ident])
    if cross_scope:
        if json.loads(scope)[0] != "private" or any(
            evidence["evidence_principal"] != principal
            or (evidence["evidence_scope"] != scope and (source_scope_allowed is None
                or not source_scope_allowed(evidence["evidence_scope"])
                or evidence["evidence_kind"] != "message_received"))
            for ident in ids for evidence in records[ident]
        ):
            raise PermissionError("No common authorized memory audience")
    for origin in origins:
        archive(db, origin, now)
    if meeting:
        restore_meeting(db, scope, principal)
    ident = ids[0]
    if len(ids) > 1:
        ident = str(uuid5(NAMESPACE_URL, json.dumps([scope, principal, meeting, sorted(ids)])))
        erased = hashlib.sha256(("card-projection:" + ident).encode()).hexdigest()
        if db.execute("SELECT 1 FROM memory_erasures WHERE scope=? AND digest=?", (scope, erased)).fetchone():
            raise PermissionError("This memory projection was erased")
        db.execute("""INSERT INTO memory_card
            (id,scope,principal,kind,created,owner,emotional,importance,reasons,pinned,clarity,last_seen,title,merged_from)
            VALUES (?,?,?,?,?,?,?,?,?,1,4,?,?,?)
            ON CONFLICT(id) DO UPDATE SET hidden=0,title=excluded.title,pinned=1,clarity=4""",
            (ident, scope, principal, "merged", selected[0]["created"], all(row["owner"] for row in selected),
             max(row["emotional"] for row in selected),
             max(80, *(row["importance"] for row in selected)), '["merge"]',
             max(row["last_seen"] for row in selected), title or "合并事件", json.dumps(ids)))
        inherited = sorted({tag for row in selected for tag in tags(row)} - {"owner", "initial", "merged"})
        db.execute("UPDATE memory_card SET inherited_tags=? WHERE id=?", (json.dumps(inherited[:12]), ident))
        db.executemany("INSERT OR IGNORE INTO memory_card_sources VALUES (?,?)",
                       ((ident, row["ref"]) for source in ids for row in records[source]))
        if not cross_scope:
            db.executemany("UPDATE memory_card SET hidden=1 WHERE id=?", ((source,) for source in ids))
    if meeting:
        db.execute("INSERT OR IGNORE INTO memory_card_settings(scope) VALUES (?)", (scope,))
        db.execute("UPDATE memory_card_settings SET manual_meeting=? WHERE scope=?", (ident, scope))
        db.execute("""UPDATE memory_card SET pinned=CASE WHEN pin_override=0 THEN 1 ELSE pinned END,
                      clarity=4 WHERE id=?""", (ident,))
    refresh_initial(db, scope)
    return ident


def restore_meeting(db, scope, principal):
    row = db.execute("""SELECT c.id,c.kind,c.merged_from,c.principal FROM memory_card_settings s
        JOIN memory_card c ON c.id=s.manual_meeting
        WHERE s.scope=?""", (scope,)).fetchone()
    if row and row["principal"] != principal:
        raise PermissionError("First meeting belongs to a different principal")
    if row and row["kind"] == "merged":
        db.execute("""UPDATE memory_card SET hidden=0 WHERE scope=? AND principal=?
            AND id IN (SELECT value FROM json_each(?))""", (scope, principal, row["merged_from"]))
        db.execute("UPDATE memory_card SET hidden=1,first_meet=0 WHERE id=?", (row["id"],))
    db.execute("UPDATE memory_card_settings SET manual_meeting=NULL WHERE scope=?", (scope,))
    refresh_initial(db, scope)
