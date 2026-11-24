"""Dispatch frozen Chinese fixtures through a disposable SQLite repository."""

import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import tempfile
import time

from fat_fish.memory.embedding import LocalEmbeddingProvider
from fat_fish.memory.erasure import ErasureLedger
from fat_fish.memory.event_retrieval import EventRetrieval
from fat_fish.memory.policy import Audience, ScopeKind, ScopeRef
from fat_fish.memory.records import EventRecord, EvidenceRecord, ItemRecord, SourceRecord
from fat_fish.memory.repository import MemoryRepository
from fat_fish.memory.retrieval import CognitiveRetrieval
from fat_fish.memory.semantic import unit_vector

DATASET = Path(__file__).with_name("cognitive-retrieval-zh-v1.json")
BASELINE_SPACE = "cognitive-retrieval-zh-v1"
STATUS = {"completed": "succeeded"}


def millis(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def seed(repository, scope, doc):
    event_time = millis(doc["occurred_at"])
    source = SourceRecord(scope, doc["evidence_id"], "public_attestation", "benchmark",
                          "verified_connector", doc["evidence_id"], event_time)
    event = EventRecord(source, doc["id"], doc["action"], STATUS.get(doc["status"], doc["status"]),
                        doc["subject"], event_time, doc["text"])
    event_id = repository.record_event(event, millis(doc["recorded_at"]))
    with repository.write() as db:
        source_id = db.execute("SELECT source_id FROM event WHERE id=?", (event_id,)).fetchone()[0]
    item = ItemRecord(scope, "fish", doc["subject"], "experience", doc["id"], "main", doc["text"],
                      {}, (EvidenceRecord(source_id, event_id),), "verified_connector", 1, .7)
    item_id, rev = repository.put_item(item, None, 1)
    with repository.write() as db:
        content_hash = db.execute(
            "SELECT content_hash FROM revision WHERE item_id=? AND rev=?", (item_id, rev)
        ).fetchone()[0]
    return item_id, rev, content_hash


class CachedModel:
    def __init__(self, vectors):
        self.vectors = vectors
        self.calls = 0

    async def get_embedding(self, text):
        self.calls += 1
        return self.vectors[text]


async def prepare_vectors(dataset, model):
    terms = dict.fromkeys(
        text for g in dataset["groups"]
        for text in [*(d["text"] for d in g["documents"]), *(q["query"] for q in g["queries"])]
    )
    latencies = []
    for text in terms:
        started = time.perf_counter()
        terms[text] = await model.get_embedding(text)
        latencies.append((time.perf_counter() - started) * 1000)
    return terms, latencies


def percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * percentile) - 1)], 3)


def summarize(rows):
    positives = [r for r in rows if r["gold"]]
    negative = [r for r in rows if not r["gold"] and not r["clarification"]]
    ambiguous = [r for r in rows if r["clarification"]]
    rate = lambda n, d: round(n / d, 4) if d else None
    recalls = {}
    for k in (1, 3, 5, 10):
        recalls[f"recall@{k}"] = rate(sum(len(set(r["ranking"][:k]) & set(r["gold"])) /
                                         len(r["gold"]) for r in positives), len(positives))
    mrr = sum(next((1 / (i + 1) for i, item in enumerate(r["ranking"]) if item in r["gold"]), 0)
              for r in positives)
    ndcg = sum(
        sum(1 / math.log2(i + 2) for i, item in enumerate(r["ranking"][:5]) if item in r["gold"])
        / sum(1 / math.log2(i + 2) for i in range(min(5, len(r["gold"])))) for r in positives)
    answers = [r for r in rows if r["decision"] == "answer"]
    correct_answers = sum(set(r["returned"]) == set(r["gold"]) for r in answers)
    correct_decisions = correct_answers + sum(r["decision"] == "clarify" for r in ambiguous) + sum(
        r["decision"] == "abstain" for r in negative)
    latencies = [r["elapsed_ms"] for r in rows]
    sqlite = [r["sqlite_ms"] for r in rows if r.get("sqlite_ms") is not None]
    return {**recalls, "mrr": rate(mrr, len(positives)), "ndcg@5": rate(ndcg, len(positives)),
            "precision@3": rate(sum(len(set(r["ranking"][:3]) & set(r["gold"])) for r in positives),
                                len(positives) * 3),
            "decision_accuracy": rate(correct_decisions, len(rows)),
            "answer_evidence_accuracy": rate(correct_answers, len(answers)),
            "abstention_precision": rate(sum(r["decision"] == "abstain" and r in negative for r in rows),
                                         sum(r["decision"] == "abstain" for r in rows)),
            "abstention_recall": rate(sum(r["decision"] == "abstain" for r in negative), len(negative)),
            "hard_negative_fpr": rate(sum(r["decision"] == "answer" for r in negative), len(negative)),
            "clarification_accuracy": rate(sum(r["decision"] == "clarify" for r in ambiguous), len(ambiguous)),
            "constraint_violation_rate": rate(sum(bool(set(r["returned"]) & set(r["hard_negatives"]))
                                                   for r in answers), len(answers)),
            "retrieval_p50_ms": percentile(latencies, .5), "retrieval_p95_ms": percentile(latencies, .95),
            "sqlite_p95_ms": percentile(sqlite, .95) if sqlite else None,
            "count": len(rows)}


async def evaluate(dataset, vectors, variants, workspace):
    now = millis(dataset["query_time"])
    model = CachedModel(vectors)
    rows = {variant: [] for variant in variants}
    for group in dataset["groups"]:
        root = workspace / group["id"]
        root.mkdir()
        ledger = ErasureLedger(root / "erase.jsonl", "fixture", {"fixture": b"f" * 32}, "fixture")
        repository = MemoryRepository(root / "memory.sqlite3", ledger)
        repository.open(ledger.create())
        public = ScopeRef("fish", ScopeKind.PUBLIC, "fish")
        caller = Audience("fish", "fixture", ScopeRef("fish", ScopeKind.PRIVATE, "fixture"), 1)
        names = {}
        for doc in group["documents"]:
            item, rev, content_hash = seed(repository, public, doc)
            names[item] = doc["id"]
            repository.store_embedding(item, rev, content_hash, BASELINE_SPACE,
                                       vectors[doc["text"]], now)
        baseline = CognitiveRetrieval(repository, model, space=BASELINE_SPACE)
        for case in group["queries"]:
            for variant in variants:
                start = time.perf_counter()
                sqlite_ms = None
                trace = None
                if variant == "A":
                    vector = unit_vector(vectors[case["query"]])
                    hits = repository.semantic_candidates(caller, now, BASELINE_SPACE, vector)
                    ranking = [names[doc["id"]] for doc, _ in hits]
                    returned = [names[doc["id"]] for doc, score in hits if score >= .60][:1]
                    decision = "answer" if returned else "abstain"
                elif variant == "B":
                    result = await baseline.recall(caller, case["query"], now, deep=True)
                    ranking = [names[hit.item_id] for hit in result.hits]
                    returned = ranking[:1]
                    decision = "answer" if returned else "abstain"
                else:
                    result = await EventRetrieval(repository, model, space=BASELINE_SPACE,
                                                  clock_zone=datetime.fromisoformat(dataset["query_time"]).tzinfo).recall(
                        caller, case["query"], now, variant)
                    ranking = [names[hit.item_id] for hit in result.candidates]
                    returned = [names[hit.item_id] for hit in result.support]
                    decision = result.decision
                    sqlite_ms = result.timings_ms.get("sqlite")
                    trace = {
                        "reason": result.reason,
                        "evidence": [
                            {"item": names[hit.item_id], "revision": hit.revision,
                             "source": hit.evidence_id, "event": hit.event_id,
                             "channels": hit.channels, "matches": hit.matched,
                             "conflicts": hit.conflicts}
                            for hit in result.support
                        ],
                    }
                rows[variant].append({**{k: case[k] for k in ("id", "gold", "clarification", "hard_negatives")},
                                      "split": group["split"], "ranking": ranking, "returned": returned,
                                      "sqlite_ms": sqlite_ms,
                                      "trace": trace,
                                      "elapsed_ms": (time.perf_counter() - start) * 1000,
                                      "decision": decision})
    return rows


async def main(workspace):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--vectors", required=True, type=Path)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--variants", default="AB")
    args = parser.parse_args()
    dataset_bytes = args.dataset.read_bytes()
    dataset = json.loads(dataset_bytes)
    dataset_hash = hashlib.sha256(dataset_bytes).hexdigest()
    model = LocalEmbeddingProvider("http://127.0.0.1:6387/v1", "feiyu-bge-small-zh",
                                   "bge-small-zh-v1.5-gguf@5bf683e6", 512, "")
    if args.vectors.exists():
        cache = json.loads(args.vectors.read_text(encoding="utf-8"))
        if cache["version"] != dataset["version"] or cache["model"] != model.revision:
            raise ValueError("Evaluation cache does not match the dataset and model")
        if cache.get("dataset_sha256", dataset_hash) != dataset_hash:
            raise ValueError("Evaluation cache does not match the dataset content")
        vectors, latencies = cache["vectors"], cache["embedding_ms"]
    else:
        vectors, latencies = await prepare_vectors(dataset, model)
        args.vectors.write_text(json.dumps({"version": dataset["version"], "model": model.revision,
                                             "dataset_sha256": dataset_hash,
                                             "vectors": vectors, "embedding_ms": latencies}), encoding="utf-8")
    variants = tuple(args.variants.split(",")) if "," in args.variants else tuple(args.variants)
    rows = await evaluate(dataset, vectors, variants, workspace)
    metrics = {name: {split: summarize([r for r in result if r["split"] == split])
                      for split in ("dev", "validation", "frozen")} for name, result in rows.items()}
    args.output.write_text(json.dumps({"metrics": metrics, "cases": rows, "dataset_sha256": dataset_hash,
                                      "model_revision": model.revision, "seed": dataset.get("seed"),
                                      "embedding_p50_ms": percentile(latencies, .5),
                                      "embedding_p95_ms": percentile(latencies, .95)}, ensure_ascii=False,
                                     indent=2), encoding="utf-8")
    for name in metrics:
        print(name, metrics[name]["frozen"])


if __name__ == "__main__":
    # asyncio.run drains outstanding executor work before Windows removes the databases.
    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(main(Path(directory)))
