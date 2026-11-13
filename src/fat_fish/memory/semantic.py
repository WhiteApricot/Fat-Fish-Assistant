"""Disposable embeddings of authorized facts and conversation evidence."""
import asyncio
from collections import OrderedDict
import json
import math
import logging
import sqlite3
import time

from fat_fish.memory.experience import sensitive_information
from fat_fish.memory.service import encode

logger = logging.getLogger(__name__)


def unit_vector(values):
    vector = [float(value) for value in values]
    if not vector or len(vector) > 4096 or not all(map(math.isfinite, vector)):
        raise ValueError("Invalid embedding")
    norm = math.hypot(*vector)
    if not norm or not math.isfinite(norm):
        raise ValueError("Invalid embedding magnitude")
    return [value / norm for value in vector]


def candidates(service, access, model):
    access.require_recall()
    with service.transaction() as db:
        rows = db.execute("""SELECT f.fact_key,f.revision,v.content,x.vector FROM facts f
            JOIN fact_versions v USING(scope,fact_key,revision)
            LEFT JOIN fact_vectors x ON x.scope=f.scope AND x.fact_key=f.fact_key
                AND x.revision=f.revision AND x.model=?
            WHERE f.scope=? AND f.status='active'
            ORDER BY v.occurred DESC, f.fact_key""", (model, access.scope.storage_key)).fetchall()
    access.require_recall()
    return rows


def cache(service, access, model, encoded):
    access.require_recall()
    access.require(write=True)
    scope = access.scope.storage_key
    with service.transaction(write=True) as db:
        db.executemany("""INSERT INTO fact_vectors
            SELECT scope,fact_key,revision,?,? FROM facts
            WHERE scope=? AND fact_key=? AND revision=? AND status='active'
            ON CONFLICT(scope,fact_key,revision,model) DO UPDATE SET vector=excluded.vector""",
            ((model, encode(vector), scope, key, revision) for (key, revision), vector in encoded.items()))
        access.require(write=True)


def activity_candidates(service, access, model):
    access.require_recall()
    with service.transaction() as db:
        rows = db.execute("""SELECT a.ref,a.summary,x.vector FROM activities a
            LEFT JOIN activity_vectors x ON x.ref=a.ref AND x.model=?
            WHERE a.scope=? AND a.turn_id IS NOT NULL AND a.redacted=0
              AND a.kind IN ('message_received','message_sent')
              AND a.summary NOT IN ('','收到消息','通信适配器已返回回复提交')
              AND NOT EXISTS (SELECT 1 FROM fact_versions v JOIN facts f USING(scope,fact_key)
                  WHERE v.ref=a.ref AND (v.revision<>f.revision OR f.status<>'active'))
            ORDER BY a.occurred DESC,a.ref DESC""", (model, access.scope.storage_key)).fetchall()
    access.require_recall()
    return rows


def cache_activity(service, access, model, ref, summary, vector):
    access.require_recall()
    access.require(write=True)
    with service.transaction(write=True) as db:
        db.execute("""INSERT INTO activity_vectors
            SELECT ref,?,? FROM activities WHERE ref=? AND scope=? AND summary=? AND redacted=0
            ON CONFLICT(ref,model) DO UPDATE SET vector=excluded.vector""",
                   (model, encode(vector), ref, access.scope.storage_key, summary))
        access.require(write=True)


def reset(service, access, model):
    access.require_recall()
    access.require(write=True)
    with service.transaction(write=True) as db:
        count = db.execute("DELETE FROM fact_vectors WHERE scope=? AND model=?",
                           (access.scope.storage_key, model)).rowcount
        count += db.execute("""DELETE FROM activity_vectors WHERE model=?
            AND ref IN (SELECT ref FROM activities WHERE scope=?)""",
                            (model, access.scope.storage_key)).rowcount
        access.require(write=True)
        return count


class IndexMaintenance:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.tasks = {}
        self.backoff = OrderedDict()
        self.last_failure = None

    def schedule(self, service, access, model, provider, *, activities=False):
        key = access.scope.storage_key, model
        if key not in self.tasks:
            task = asyncio.create_task(self.run(service, access, model, provider, activities=activities))
            self.tasks[key] = task
            task.add_done_callback(lambda _: self.tasks.pop(key, None))
        return self.tasks[key]

    async def run(self, service, access, model, provider, *, timeout=10, limit=8, activities=False):
        if access.scope.kind != "private" or not access.may_write:
            return
        try:
            facts = await asyncio.to_thread(candidates, service, access, model)
            rows = [(row, (row["fact_key"], row["revision"]), row["content"]) for row in facts]
            if activities:
                events = await asyncio.to_thread(activity_candidates, service, access, model)
                rows.extend((row, (row["ref"],), row["summary"]) for row in events)
            attempts = 0
            for row, key, text in rows:
                if row["vector"] or sensitive_information(text):
                    continue
                retry_key = access.scope.storage_key, model, *key
                count, next_at = self.backoff.get(retry_key, (0, 0))
                if self.clock() < next_at:
                    continue
                if attempts >= limit:
                    break
                attempts += 1
                access.require_recall()
                try:
                    values = await asyncio.wait_for(provider.get_embedding(text), timeout)
                    vector = unit_vector(values)
                    if len(key) == 1:
                        await asyncio.to_thread(cache_activity, service, access, model, key[0], text, vector)
                    else:
                        await asyncio.to_thread(cache, service, access, model, {key: vector})
                except PermissionError:
                    raise
                except (TimeoutError, ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
                    self.last_failure = type(exc).__name__
                    self.backoff[retry_key] = count + 1, self.clock() + min(300, 2 ** min(count + 1, 9))
                    self.backoff.move_to_end(retry_key)
                    if len(self.backoff) > 512:
                        self.backoff.popitem(last=False)
                    logger.warning("FFA vector indexing deferred (%s)", type(exc).__name__)
                else:
                    self.backoff.pop(retry_key, None)
                    if not self.backoff:
                        self.last_failure = None
        except (PermissionError, OSError, sqlite3.Error) as exc:
            self.last_failure = type(exc).__name__
            logger.warning("FFA vector maintenance unavailable (%s)", type(exc).__name__)


async def recall(service, access, query, model, provider, *, activities=False):
    access.require_recall()
    if access.scope.kind != "private" or sensitive_information(query):
        return {}
    rows = await asyncio.to_thread(candidates, service, access, model)
    cached = [((row["fact_key"], row["revision"]), row["vector"]) for row in rows if row["vector"]]
    if activities:
        events = await asyncio.to_thread(activity_candidates, service, access, model)
        cached.extend((row["ref"], row["vector"]) for row in events if row["vector"])
    if not cached:
        return {}
    access.require_recall()
    query_vector = unit_vector(await provider.get_embedding(query[:1500]))
    scores = {}
    for key, encoded in cached:
        vector = json.loads(encoded)
        if len(vector) == len(query_vector):
            scores[key] = sum(
                left * right for left, right in zip(query_vector, vector))
    access.require_recall()
    return scores
