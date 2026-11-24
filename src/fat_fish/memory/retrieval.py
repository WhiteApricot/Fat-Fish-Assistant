"""Bounded recall with separately diagnosable lexical and semantic channels."""

import asyncio
from dataclasses import dataclass
import math
import sqlite3
import time

from fat_fish.memory.erasure import LedgerError
from fat_fish.memory.experience import sensitive_information
from fat_fish.memory.search_text import tokens
from fat_fish.memory.semantic import unit_vector

LOOKBACK_WORDS = ("上次", "之前", "以前", "昨晚", "记得", "哪次", "previous", "last time")


@dataclass(frozen=True)
class EvidenceHit:
    item_id: int
    revision: int
    digest: str
    body: str
    keyword_score: float
    semantic_score: float | None
    rrf_score: float


@dataclass(frozen=True)
class RecallResult:
    hits: tuple[EvidenceHit, ...]
    keyword_candidates: int
    semantic_candidates: int
    reasons: tuple[str, ...]


def _lexical_score(query, documents):
    """BM25 on the approved candidate set, using the same tokenization as the index."""
    rows = [tokens(doc["body"]) for doc in documents]
    if not rows:
        return {}
    avg = sum(sum(row.values()) for row in rows) / len(rows) or 1
    idf = {term: math.log1p((len(rows) - n + .5) / (n + .5))
           for term in query for n in (sum(term in row for row in rows),)}
    result = {}
    for doc, row in zip(documents, rows):
        if len(query) > 1 and not any(len(term) > 1 and row[term] for term in query):
            result[doc["id"]] = 0.0
            continue
        length = sum(row.values())
        norm = 1.2 * (.25 + .75 * length / avg)
        result[doc["id"]] = sum(idf[term] * row[term] * 2.2 / (row[term] + norm)
                                for term in query if row[term])
    return result


class CognitiveRetrieval:
    def __init__(self, repository, provider=None, *, space=None, semantic_min=0.60):
        self.repository = repository
        self.provider = provider
        self.space = space
        self.semantic_min = semantic_min

    async def recall(self, audience, query: str, now: int, *, deep=None) -> RecallResult:
        sensitive = sensitive_information(query)
        permitted = deep if deep is not None else any(word in query.casefold() for word in LOOKBACK_WORDS)
        word_counts = tokens(query[:300]) if not sensitive else {}
        words = tuple(sorted(word_counts, key=lambda term: (-len(term), -word_counts[term], term))[:12])
        if not words:
            return RecallResult((), 0, 0, ("no_query_terms",))
        loop = asyncio.get_running_loop()
        reasons = []
        lexical = ()
        semantic = []
        try:
            async with asyncio.timeout(.12):
                lexical = await asyncio.to_thread(self.repository.lexical_candidates,
                                                  audience, now, words, 48 if permitted else 12)
        except (TimeoutError, sqlite3.Error, LedgerError):
            reasons.append("lexical_unavailable")
        lexical_scores = _lexical_score(words, lexical)
        if permitted and self.provider is not None and self.space:
            deadline = loop.time() + .33
            try:
                async with asyncio.timeout_at(deadline):
                    async with asyncio.timeout(.25):
                        vector = unit_vector(await self.provider.get_embedding(query))
                    semantic = await asyncio.to_thread(
                        self.repository.semantic_candidates, audience, now, self.space, vector
                    )
            except (TimeoutError, OSError, ValueError, sqlite3.Error, LedgerError):
                reasons.append("semantic_unavailable")
        elif permitted:
            reasons.append("missing_model_space")

        docs = {doc["id"]: doc for doc in lexical}
        scores = {doc["id"]: similarity for doc, similarity in semantic}
        ranks = {}
        for rank, doc in enumerate(sorted(lexical, key=lambda row: (-lexical_scores[row["id"]], row["id"])), 1):
            if lexical_scores[doc["id"]] > 0:
                ranks[doc["id"]] = 1 / (60 + rank)
        for rank, (doc, similarity) in enumerate(semantic, 1):
            if similarity >= self.semantic_min or doc["id"] in ranks:
                docs[doc["id"]] = doc
                ranks[doc["id"]] = ranks.get(doc["id"], 0) + 1 / (60 + rank)
        if not ranks:
            reasons.append("insufficient_evidence")
        count = 6 if permitted else 3
        ordered = sorted(ranks, key=lambda item: (-ranks[item], item))[:count]
        hits = tuple(EvidenceHit(item, docs[item]["rev"], docs[item]["content_hash"], docs[item]["body"],
                                 lexical_scores.get(item, 0), scores.get(item), ranks[item])
                     for item in ordered)
        return RecallResult(hits, len(lexical), len(semantic), tuple(reasons))
