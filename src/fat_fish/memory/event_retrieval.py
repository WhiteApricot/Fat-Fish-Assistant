"""Evidence-aware retrieval for explicit event lookbacks."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import json
import re
import sqlite3
from time import perf_counter

from fat_fish.memory.erasure import LedgerError
from fat_fish.memory.event_query import (
    ACTIONS, CONFIRM, EVENT_LIST, FIRST_PERSON_SUBJECT, REFERENT,
    confirmation_objects, event_hours, parse_event_query, transfer_direction,
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


def _episode(row):
    """Resolve connector-owned metadata, scoped to the original evidence event."""
    if row["trust_class"] not in {"verified_connector", "owner_confirmed"}:
        return None, ()
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (ValueError, TypeError):
        return None, ()
    episode = payload.get("episode") if isinstance(payload, dict) else None
    if not isinstance(episode, dict):
        return None, ()
    ident = episode.get("id")
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident):
        return None, ()
    names = episode.get("aliases")
    aliases = tuple(name for name in names[:8] if isinstance(name, str) and 2 <= len(name) <= 40
                    and not sensitive_information(name)) if isinstance(names, list) else ()
    return (row["event_scope_id"], row["producer"], ident), aliases


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
        strict = variant in {"E", "Eprev"}
        expanded = strict
        linked = variant == "E"
        # Allow a question about which public survey happened, without forwarding
        # the sensitive phrase to the model or admitting private address evidence.
        public_survey = strict and bool(re.fullmatch(
            r"(?:昨晚|夜里|夜晚|白天|今天)?(?:你)?(?:检查|核验|查)完的是哪条街的住址编号[？?]?", query
        ))
        if not public_survey and sensitive_information(query):
            return EventResult("abstain", "sensitive_query", (), (), {})
        use_structure = variant != "no_structure"
        if strict and FIRST_PERSON_SUBJECT.match(query):
            return EventResult("abstain", "unsupported_subject", (), (), {})
        use_rerank = strict or variant in {"D", "E0", "no_bm25", "no_time", "no_structure", "no_rrf"}
        evidence_gate = strict or variant in {"E0", "no_bm25", "no_time", "no_structure", "no_rrf", "no_rerank"}
        use_time = use_rerank and variant != "no_time"
        intent = parse_event_query(query)
        action = "验证" if public_survey else intent.action
        referent = strict and bool(REFERENT.search(query))
        open_history = expanded and bool(re.search(
            r"记得.{0,4}(?:如何|怎么)|(?:什么|哪些|有何).{0,8}(?:记录|资料|进展)|"
            r"(?:记录|资料|进展).{0,8}(?:什么|哪些)", query
        ))
        confirming = strict and not referent and not open_history and bool(CONFIRM.search(query.rstrip()))
        direction = transfer_direction(query) if confirming and action == "交付" else None
        history = strict and not confirming and (
            bool(EVENT_LIST.search(query)) or (open_history and not referent and bool(action))
        )
        hours = event_hours(query) if strict else None
        phase = "succeeded" if public_survey else intent.phase
        episode_overview = linked and history and phase is None and "做了什么" in query
        if history and not episode_overview and re.search(r"做(?:过|了)(?:什么|哪些)", query):
            phase = "succeeded"
        if confirming and re.search(r"没有|没|未曾|并未", query):
            return EventResult("abstain", "unsupported_claim_polarity", (), (), {})
        if confirming and phase is None:
            phase = "succeeded"
        if referent and "还是" in query:
            phase = None
        actor = (intent.actor or ("bot" if history or public_survey else None)) if use_structure else None
        event_phase = phase if use_structure else None
        objects = confirmation_objects(query) if confirming else ()
        dates = None
        if strict:
            today = datetime.fromtimestamp(now / 1000, tz=self.clock_zone).date()
            periods = [name for name, pattern in (("yesterday", "昨晚|昨天|昨日"), ("today", "今天"),
                                                  ("last_week", "上周|上星期")) if re.search(pattern, query)]
            if periods == ["yesterday"]:
                dates = (today - timedelta(days=1), today)
            elif periods == ["today"]:
                dates = (today, today + timedelta(days=1))
            elif periods == ["last_week"]:
                monday = today - timedelta(days=today.weekday())
                dates = (monday - timedelta(days=7), monday)
        window = None
        if expanded and dates:
            window = tuple(int(datetime.combine(day, time.min, tzinfo=self.clock_zone).timestamp() * 1000)
                           for day in dates)
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
                if variant != "no_bm25" and not public_survey:
                    lexical = await asyncio.to_thread(self.repository.lexical_candidates, audience, now, terms, 48)
        except (TimeoutError, sqlite3.Error, LedgerError):
            return EventResult("abstain", "storage_unavailable", (), (), {})
        timings["sqlite"] = (perf_counter() - started) * 1000
        if self.provider is not None and self.space and not public_survey:
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
                        public_only=public_survey,
                        occurred_window=window,
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
                           ("event", list(rows_by_item)
                            if intent.structured or referent or history or hours or dates else [])):
            for index, key in enumerate(keys, 1):
                positions[key].append((name, index))
        anchors = _object_terms(query)
        topic = EVENT_LIST.sub(" ", query) if history else ""
        topic = re.sub(r"昨晚|昨天|今天|上周|上星期|那回|那次|这次|你在|肥鱼在|你|肥鱼|关于", " ", topic)
        if expanded and open_history:
            topic = re.sub(r"记得|如何|怎么|有什么|有何|哪些|什么|的|记录|资料|进展|[过吗]", " ", topic)
            if action:
                for alias in ACTIONS[action]:
                    topic = topic.replace(alias, " ")
        topic_terms = tuple(word for word in re.findall(r"[\u3400-\u9fff]{2,}", topic)
                            if word not in {"情况", "经历", "事情", "活动"})
        episode_topic = "".join(topic_terms).removesuffix("上")
        ranked = []
        episode_by_evidence = {}
        completed_events = set()
        for item, sources in rows_by_item.items():
            best = None
            for row in sources:
                if public_survey and (
                    row["event_kind"] != "验证" or "门牌" not in row["body"]
                    or sensitive_information(row["body"]) or re.search(r"\d", row["body"])
                ):
                    continue
                matched, conflicts = [], []
                checks = {
                    "action": (action, row["event_kind"]),
                    "time": (intent.time_of_day if use_time else None,
                             _coarse_time(row["occurred_at"], self.clock_zone)),
                    "subject": (actor, row["actor_kind"]),
                    "state": (event_phase, row["phase"]),
                }
                if direction:
                    checks["direction"] = (direction, transfer_direction(row["body"]))
                if hours or dates:
                    local = datetime.fromtimestamp(row["occurred_at"] / 1000, tz=self.clock_zone)
                    if hours:
                        checks["time"] = (True, local.hour in hours)
                    if dates:
                        checks["date"] = (True, dates[0] <= local.date() < dates[1])
                for name, (wanted, actual) in checks.items():
                    if wanted:
                        (matched if wanted == actual else conflicts).append(name)
                if confirming:
                    if objects and all(obj.casefold() in row["body"].casefold() for obj in objects):
                        matched.append("object")
                elif anchors.intersection(tokens(row["body"])):
                    matched.append("object")
                episode, aliases = _episode(row) if linked and history else (None, ())
                if topic_terms and (all(term in row["body"] for term in topic_terms)
                                    or episode_topic in aliases):
                    matched.append("topic")
                if row["trust_class"] in {"verified_connector", "owner_confirmed", "direct_self"}:
                    matched.append("source")
                channels = tuple(positions[item])
                fusion = (sum(1 / (60 + rank) for _, rank in channels) if variant != "no_rrf"
                          else max((1 / (60 + rank) for _, rank in channels), default=0))
                if not channels:
                    continue
                result = RankedEvent(item, row["rev"], row["source_id"], row["event_id"], row["body"],
                                     channels, tuple(matched), tuple(conflicts), fusion)
                if episode:
                    episode_by_evidence[(result.event_id, result.evidence_id)] = episode
                if row["phase"] == "succeeded":
                    completed_events.add(result.event_id)
                structural = sum(name in matched for name in ("time", "action", "state", "object", "topic"))
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
        if history:
            distinct = {}
            episodes = set()
            for _, hit in (ranked if expanded else ranked[:10]):
                matched = set(hit.matched)
                if "source" in matched and not hit.conflicts and (
                    "topic" in matched or (not topic_terms and {"date", "action"} <= matched)
                    or (expanded and open_history and not topic_terms and "action" in matched)
                ):
                    distinct.setdefault(hit.event_id, hit)
                    episode = episode_by_evidence.get((hit.event_id, hit.evidence_id))
                    if episode and ("topic" in matched or (not topic_terms and "action" in matched)):
                        episodes.add(episode)
            if len(episodes) > 1:
                return EventResult("clarify", "multiple_matching_episodes", candidates, (), timings)
            if episodes:
                distinct = {}
                for _, hit in ranked:
                    if "source" in hit.matched and not hit.conflicts and (
                        episode_by_evidence.get((hit.event_id, hit.evidence_id)) in episodes
                    ):
                        distinct.setdefault(hit.event_id, hit)
            elif episode_overview:
                distinct = {key: hit for key, hit in distinct.items() if key in completed_events}
            if not distinct:
                return EventResult("abstain", "history_topic_unresolved", candidates, (), timings)
            if len(distinct) > 3 or (len(rows) == 48 if expanded else len(ranked) > len(candidates)):
                return EventResult("clarify", "history_needs_narrower_scope", candidates, (), timings)
            return EventResult("answer", "supported_history", candidates, tuple(distinct.values()), timings)
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
