"""Synthetic SQLite-only recall under bounded concurrent writes."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

if sys.platform == "win32":
    import psutil
else:
    import resource

from fat_fish.memory.embedding import LocalEmbeddingProvider
from fat_fish.memory.erasure import ErasureLedger
from fat_fish.memory.policy import Audience, ScopeKind, ScopeRef
from fat_fish.memory.records import EventRecord, EvidenceRecord, ItemRecord, SourceRecord
from fat_fish.memory.repository import MemoryRepository
from fat_fish.memory.retrieval import CognitiveRetrieval
from fat_fish.memory.search_text import tokens


def percentiles(values):
    ordered = sorted(values)
    return {f"p{rate}": round(ordered[math.ceil(len(ordered) * rate / 100) - 1], 2)
            for rate in (50, 95, 99)}


def identifier(index):
    return "".join(chr(ord("a") + (index // (26 ** digit)) % 26) for digit in range(4))


def rss_bytes():
    if sys.platform == "win32":
        return psutil.Process().memory_info().rss
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def benchmark(root, size, samples, seed_batch, embedding=None):
    ledger = ErasureLedger(root / "erasure.jsonl", "benchmark", {"test": b"b" * 32}, "test")
    repository = MemoryRepository(root / "memory.sqlite3", ledger)
    repository.open(ledger.create())
    scope = ScopeRef("fish", ScopeKind.PRIVATE, "synthetic-principal")
    audience = Audience("fish", scope.key, scope, 1)
    now = 1_730_000_000_000
    peak_rss = rss_bytes()
    started = time.perf_counter()
    cpu_start = time.process_time()

    # Batched seeding measures fixture generation separately from the read/write run.
    for first in range(0, size, seed_batch):
        with repository.write() as db:
            scope_id = repository.scope_id(db, scope)
            for i in range(first, min(first + seed_batch, size)):
                source = db.execute("""INSERT INTO source(scope_id,source_key,kind,author_principal,
                    producer,observed_at,ingested_at,trust_class,receipt_digest)
                    VALUES (?,?,'sample',?,'sim',?,?,'direct_self',?) RETURNING id""",
                                    (scope_id, str(i), scope.key, now, now, str(i))).fetchone()[0]
                text = f"station-{i % 127} series-{i % 631} instrument-{identifier(i)} report-{i % 53}"
                item = ItemRecord(scope, "fish", scope.key, "fact", str(i), "sample", text, {},
                                  (EvidenceRecord(source),), "direct_self", .9, .5)
                repository._put_item(db, item, None, now)
        peak_rss = max(peak_rss, rss_bytes())
    seed_seconds = time.perf_counter() - started
    seed_cpu = time.process_time() - cpu_start

    def read(i):
        query = f"station-{i % 127} instrument-{identifier(i)}"
        terms = tuple(tokens(query))
        t0 = time.perf_counter()
        try:
            rows = repository.lexical_candidates(audience, now, terms, 12)
            return (time.perf_counter() - t0) * 1000, len(rows), any(f"instrument-{identifier(i)} " in r["body"] for r in rows)
        except sqlite3.OperationalError:
            return (time.perf_counter() - t0) * 1000, None, False

    def write():
        latency = []
        busy = 0
        for i in range(min(samples, 100)):
            t0 = time.perf_counter()
            try:
                key = f"concurrent-{i}"
                source = SourceRecord(scope, key, "platform_message", "sim", "direct_self", key, now, scope.key)
                repository.record_event(EventRecord(source, key, "inbound", "observed", "user", now, key, actor_ref=scope.key),
                                        now, extract=True)
            except sqlite3.OperationalError:
                busy += 1
            latency.append((time.perf_counter() - t0) * 1000)
        return percentiles(latency), busy

    queries = [(i * 7919) % size for i in range(samples)]
    cpu_start = time.process_time()
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=5) as pool:
        writer = pool.submit(write)
        results = []
        for result in pool.map(read, queries):
            results.append(result)
            peak_rss = max(peak_rss, rss_bytes())
        write_latency, write_busy = writer.result()

    summary = {"memory_count": size, "samples": samples, "read_workers": 4, "writers": 1, "seed_batch": seed_batch,
            "lexical_latency_ms": percentiles([row[0] for row in results]),
            "raw_capture_latency_ms": write_latency, "write_errors": write_busy,
            "read_errors": sum(row[1] is None for row in results),
            "exact_target_hits": sum(row[2] for row in results),
            "mean_candidates": round(sum(row[1] or 0 for row in results) / samples, 2),
            "database_bytes": sum(path.stat().st_size for path in root.glob("memory.sqlite3*")),
            "sampled_peak_rss_bytes": peak_rss, "concurrent_cpu_s": round(time.process_time() - cpu_start, 3),
            "concurrent_wall_s": round(time.perf_counter() - started, 3),
            "seed_wall_s": round(seed_seconds, 3), "seed_cpu_s": round(seed_cpu, 3)}
    if embedding:
        summary["embedding_sample"] = asyncio.run(embedding_sample(repository, audience, now, embedding))
    return summary


async def embedding_sample(repository, audience, now, provider):
    # Vectorize a small selected subset. Keep query inference uncached.
    space = provider.space("structured-memory-benchmark")
    rows = repository.authorized_documents(audience, now, limit=24)
    for row in rows:
        vector = await provider.get_embedding(row["body"])
        if not repository.store_embedding(row["id"], row["rev"], row["content_hash"], space, vector, now):
            raise RuntimeError("Could not publish benchmark vector")
    retrieval = CognitiveRetrieval(repository, provider, space=space)
    started = provider.lane.snapshot()
    latencies = []
    failures = 0
    hit_count = 0
    for row in rows:
        target = row["body"].split()[2]
        t0 = time.perf_counter()
        result = await retrieval.recall(audience, f"Where is the report for {target}?", now, deep=True)
        latencies.append((time.perf_counter() - t0) * 1000)
        failures += int("semantic_unavailable" in result.reasons)
        hit_count += int(any(hit.item_id == row["id"] for hit in result.hits))
    final = provider.lane.snapshot()
    return {"count": len(rows), "latency_ms": percentiles(latencies), "semantic_unavailable": failures,
            "target_hits": hit_count, "model_requests": final["submitted"] - started["submitted"],
            "model_success": final["success"] - started["success"],
            "model_time_ms": final["elapsed_ms"] - started["elapsed_ms"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 10000, 50000])
    parser.add_argument("--samples", type=int, default=120)
    parser.add_argument("--seed-batch", type=int, default=250)
    parser.add_argument("--embedding-url", help="Optional loopback llama.cpp OpenAI /v1 endpoint")
    parser.add_argument("--embedding-model", default="feiyu-bge-small-zh")
    args = parser.parse_args()
    if args.samples < 1 or args.seed_batch < 1 or any(not 1 <= size <= 26 ** 4 for size in args.sizes):
        parser.error("sizes must be within 1–456976 and samples and seed batch must be positive")
    embedding = (LocalEmbeddingProvider(args.embedding_url, args.embedding_model, "bench", 512, "")
                 if args.embedding_url else None)
    for size in args.sizes:
        with tempfile.TemporaryDirectory(prefix="ffa-memory-benchmark-") as work:
            result = benchmark(Path(work), size, args.samples, args.seed_batch, embedding)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

