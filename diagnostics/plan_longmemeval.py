"""Freeze and size a public LongMemEval cohort without inspecting answer labels."""

import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
from random import Random


def cohort(cases, seed, per_stratum):
    grouped = defaultdict(list)
    ids = set()
    for row in cases:
        identifier = row["question_id"]
        if not isinstance(identifier, str) or identifier in ids:
            raise ValueError("LongMemEval question IDs must be unique")
        ids.add(identifier)
        kind = row["question_type"]
        abstention = identifier.endswith("_abs")
        grouped[(kind, abstention)].append(row)
    random = Random(seed)
    selected = []
    for key in sorted(grouped):
        candidates = sorted(grouped[key], key=lambda row: row["question_id"])
        selected.extend(random.sample(candidates, min(per_stratum, len(candidates))))
    return selected


def history_size(row):
    dates, keys, sessions = (row[name] for name in ("haystack_dates", "haystack_session_ids", "haystack_sessions"))
    if not sessions or len(dates) != len(keys) or len(keys) != len(sessions):
        raise ValueError("LongMemEval session metadata does not match its conversation")
    user_turns = 0
    text_bytes = 0
    extraction_bytes = 0
    prior_session_bytes = 0
    for session in sessions:
        for turn in session:
            content_size = len(turn["content"].encode("utf-8"))
            text_bytes += content_size
            if turn["role"] == "user":
                user_turns += 1
                extraction_bytes += min(content_size, 6000) + prior_session_bytes
            prior_session_bytes = min(content_size, 1000)
    return len(sessions), user_turns, text_bytes, extraction_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--per-stratum", required=True, type=int)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--budget", type=int, default=5_000_000)
    args = parser.parse_args()
    if args.per_stratum <= 0 or args.budget <= 0:
        parser.error("cohort and budget must be positive")
    data = args.dataset.read_bytes()
    digest = sha256(data).hexdigest()
    if digest != args.sha256.lower():
        parser.error("dataset digest mismatch")
    cases = json.loads(data)
    selected = cohort(cases, args.seed, args.per_stratum)
    strata = defaultdict(int)
    totals = [0, 0, 0, 0]
    for row in selected:
        for index, value in enumerate(history_size(row)):
            totals[index] += value
        strata[(row["question_type"], row["question_id"].endswith("_abs"))] += 1
    ids = [row["question_id"] for row in selected]
    with args.manifest.open("x", encoding="utf-8") as output:
        json.dump({"dataset_sha256": digest, "seed": args.seed, "question_ids": ids,
                   "per_stratum": args.per_stratum}, output, indent=2, sort_keys=True)
    # Conservative planning estimate, not tokenizer measurements or billing. An
    # extract request includes schema/prompt overhead and may invoke consolidation.
    estimated = 1000 * totals[1] + totals[3] // 2 + 2500 * len(selected)
    report = {"dataset_sha256": digest, "dataset_cases": len(cases), "cohort_cases": len(selected),
              "strata": [{"type": kind, "abstention": abstention, "cases": count}
                         for (kind, abstention), count in sorted(strata.items())],
              "history_sessions": totals[0], "user_turns": totals[1], "total_history_bytes": totals[2],
              "rough_uncached_token_estimate": estimated, "planning_budget": args.budget,
              "within_estimated_budget": estimated <= args.budget,
              "manifest_sha256": sha256(args.manifest.read_bytes()).hexdigest()}
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

