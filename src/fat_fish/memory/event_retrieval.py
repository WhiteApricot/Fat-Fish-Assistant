"""Evidence-aware retrieval for explicit event lookbacks."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import sqlite3
from time import perf_counter

from fat_fish.memory.erasure import LedgerError
from fat_fish.memory.event_query import (
    ACTIONS, CONFIRM, FIRST_PERSON_SUBJECT, REFERENT, confirmation_objects, parse_event_query,
)
from fat_fish.memory.experience import sensitive_information
from fat_fish.memory.retrieval import _lexical_score
from fat_fish.memory.search_text import tokens
from fat_fish.memory.semantic import unit_vector


@dataclass(frozen=True)
class RankedEvent:
    item_id: int
    revision: int
    evidence_id: int
    event_id: int
    body: str
    channels: tuple[tuple[str, int], ...]
    matched: tuple[str, ...]
    conflicts: tuple[str, ...]
    fusion: float


@dataclass(frozen=True)
class EventResult:
    decision: str
    reason: str
    candidates: tuple[RankedEvent, ...]
    support: tuple[RankedEvent, ...]
    timings_ms: dict[str, float]


def _coarse_time(event_ms, clock_zone):
    hour = datetime.fromtimestamp(event_ms / 1000, tz=clock_zone).hour
    return "night" if hour >= 18 or hour < 6 else "day"


def _object_terms(query):
    # Do not let a shared question frame or event verb satisfy object confirmation.
    query = re.sub("后来|已经|确实|实际|完成|什么|了吗|是否|以前|当时|昨晚|白天|晚上|夜里|是吗|没有|原来",
                   " ", query)
    for aliases in ACTIONS.values():
        for alias in aliases:
            query = query.replace(alias, " ")
    return {word for word in tokens(query) if len(word) >= 2}


class EventRetrieval:
    """Caller uses this path for authorized, explicit lookbacks only."""

    def __init__(self, repository, provider=None, *, space=None, clock_zone=timezone.utc,
                 semantic_min=.60):
        self.repository = repository
        self.provider = provider
        self.space = space
        self.clock_zone = clock_zone
        self.semantic_min = semantic_min

    async def recall(self, audience, query: str, now: int, variant="E"):
        if sensitive_information(query):
            return EventResult("abstain", "sensitive_query", (), (), {})
        use_structure = variant != "no_structure"
        strict = variant == "E"
        if strict and FIRST_PERSON_SUBJECT.match(query):
            return EventResult("abstain", "unsupported_subject", (), (), {})
        use_rerank = variant in {"D", "E", "E0", "no_bm25", "no_time", "no_structure", "no_rrf"}
        evidence_gate = variant in {"E", "E0", "no_bm25", "no_time", "no_structure", "no_rrf", "no_rerank"}
        use_time = use_rerank and variant != "no_time"
        intent = parse_event_query(query)
        referent = strict and bool(REFERENT.search(query))
        confirming = strict and not referent and bool(CONFIRM.search(query.rstrip()))
        phase = intent.phase
        if confirming and phase is None:
            phase = "succeeded"
        if referent and "还是" in query:
            phase = None
        actor = intent.actor if use_structure else None
        event_phase = phase if use_structure else None
        objects = confirmation_objects(query) if confirming else ()
        date = None
        if strict and ("昨" in query or "今天" in query):
            today = datetime.fromtimestamp(now / 1000, tz=self.clock_zone).date()
            if re.search("昨晚|昨天|昨日", query) and "今天" not in query:
                date = today - timedelta(days=1)
            elif "今天" in query and not re.search("昨晚|昨天|昨日", query):
                date = today
        counts = tokens(query[:300])
        terms = tuple(sorted(counts, key=lambda word: (-len(word), -counts[word], word))[:12])
        timings = {}
        rows, lexical, semantic = (), (), ()
        started = perf_counter()
        try:
            async with asyncio.timeout(.12):
                if not strict:
                    rows = await asyncio.to_thread(self.repository.event_candidates, audience, now,
                                                   actor=actor, phase=event_phase)
                if variant != "no_bm25":
                    lexical = await asyncio.to_thread(self.repository.lexical_candidates, audience, now, terms, 48)
        except (TimeoutError, sqlite3.Error, LedgerError):
            return EventResult("abstain", "storage_unavailable", (), (), {})
        timings["sqlite"] = (perf_counter() - started) * 1000
        if self.provider is not None and self.space:
            try:
                async with asyncio.timeout(.32):
                    start = perf_counter()
                    async with asyncio.timeout(.25):
                        vector = unit_vector(await self.provider.get_embedding(query))
                    timings["embedding"] = (perf_counter() - start) * 1000
                    start = perf_counter()
                    semantic = await asyncio.to_thread(
                        self.repository.semantic_candidates, audience, now, self.space, vector
                    )
                    timings["sqlite"] += (perf_counter() - start) * 1000
            except (TimeoutError, ValueError, OSError, sqlite3.Error, LedgerError):
                pass
        lexical_scores = _lexical_score(terms, lexical)
        lex_rank = sorted((k for k, score in lexical_scores.items() if score > 0),
                          key=lambda k: (-lexical_scores[k], k))
        sem_scores = {row["id"]: score for row, score in semantic}
        sem_rank = list(sem_scores)
        if strict or rows:
            start = perf_counter()
            try:
                async with asyncio.timeout(.065):
                    fresh = await asyncio.to_thread(
                        self.repository.event_candidates, audience, now,
                        actor=actor, phase=event_phase,
                        preferred_ids=(*lex_rank[:8], *sem_rank[:8]) if strict else (),
                    )
            except (TimeoutError, sqlite3.Error, LedgerError):
                return EventResult("abstain", "evidence_recheck_unavailable", (), (), timings)
            if strict:
                rows = fresh
            else:
                authorized = {(row["id"], row["rev"], row["event_id"], row["source_id"]) for row in fresh}
                rows = tuple(row for row in rows
                             if (row["id"], row["rev"], row["event_id"], row["source_id"]) in authorized)
            timings["sqlite"] += (perf_counter() - start) * 1000
        start = perf_counter()
        rows_by_item = defaultdict(list)
        for row in rows:
            rows_by_item[row["id"]].append(row)
        positions = defaultdict(list)
        for name, keys in (("bm25", lex_rank), ("bge", sem_rank),
                           ("event", list(rows_by_item) if intent.structured or referent else [])):
            for index, key in enumerate(keys, 1):
                positions[key].append((name, index))
        anchors = _object_terms(query)
        ranked = []
        for item, sources in rows_by_item.items():
            best = None
            for row in sources:
                matched, conflicts = [], []
                checks = {
                    "action": (intent.action, row["event_kind"]),
                    "time": (intent.time_of_day if use_time else None,
                             _coarse_time(row["occurred_at"], self.clock_zone)),
                    "subject": (intent.actor if use_structure else None, row["actor_kind"]),
                    "state": (event_phase, row["phase"]),
                }
                if date is not None:
                    local_date = datetime.fromtimestamp(row["occurred_at"] / 1000, tz=self.clock_zone).date()
                    checks["date"] = (date, local_date)
                for name, (wanted, actual) in checks.items():
                    if wanted:
                        (matched if wanted == actual else conflicts).append(name)
                if confirming:
                    if objects and all(obj.casefold() in row["body"].casefold() for obj in objects):
                        matched.append("object")
                elif anchors.intersection(tokens(row["body"])):
                    matched.append("object")
                if row["trust_class"] in {"verified_connector", "owner_confirmed", "direct_self"}:
                    matched.append("source")
                channels = tuple(positions[item])
                fusion = (sum(1 / (60 + rank) for _, rank in channels) if variant != "no_rrf"
                          else max((1 / (60 + rank) for _, rank in channels), default=0))
                if not channels:
                    continue
                result = RankedEvent(item, row["rev"], row["source_id"], row["event_id"], row["body"],
                                     channels, tuple(matched), tuple(conflicts), fusion)
                structural = sum(name in matched for name in ("time", "action", "state", "object"))
                score = (-len(conflicts), structural, row["occurred_at"] if intent.recent else 0,
                         fusion) if use_rerank else (fusion, structural)
                if best is None or score > best[0]:
                    best = score, result
            if best:
                ranked.append(best)
        ranked.sort(key=lambda item: item[0], reverse=True)
        candidates = tuple(result for _, result in ranked[:10])
        timings["ranking"] = (perf_counter() - start) * 1000
        eligible = [hit for hit in candidates if not (use_rerank and hit.conflicts)]
        if evidence_gate:
            eligible = [hit for hit in eligible if "source" in hit.matched and (
                ("object" in hit.matched and not hit.conflicts)
                or (not intent.confirmation and ("time" in hit.matched or
                     ("action" in hit.matched and "state" in hit.matched)))
                or (not intent.confirmation and not hit.conflicts
                    and sem_scores.get(hit.item_id, -1) >= self.semantic_min
                    and lexical_scores.get(hit.item_id, 0) > 0)
            )]
        if confirming:
            eligible = [hit for hit in eligible if {"action", "object", "source"} <= set(hit.matched)]
        if referent:
            plausible = [hit for hit in candidates if "source" in hit.matched and not hit.conflicts
                         and (not intent.action or "action" in hit.matched)]
            if len({hit.event_id for hit in plausible}) > 1:
                return EventResult("clarify", "multiple_event_referents", candidates, (), timings)
        elif not strict and intent.alternatives and evidence_gate and len(candidates) > 1:
            return EventResult("clarify", "multiple_event_referents", candidates, (), timings)
        if not eligible:
            return EventResult("abstain", "insufficient_evidence", candidates, (), timings)
        support = tuple(eligible[:3 if intent.multiple else 1])
        return EventResult("answer", "supported_event", candidates, support, timings)
