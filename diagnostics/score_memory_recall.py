"""Score a frozen memory run without emitting any case, answer or evidence IDs."""

import argparse
from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path


DECISIONS = {"answer", "clarify", "abstain"}
RANKS = (1, 3, 5, 10)


def read_rows(path, expected_hash=None):
    data = path.read_bytes()
    digest = sha256(data).hexdigest()
    if expected_hash is not None and digest != expected_hash:
        raise ValueError("The frozen input digest does not match")
    rows = json.loads(data)
    if not isinstance(rows, list) or not rows:
        raise ValueError("Expected a nonempty JSON array")
    indexed = {}
    for row in rows:
        ident = row.get("id") if isinstance(row, dict) else None
        if not isinstance(ident, str) or not ident or ident in indexed:
            raise ValueError("Missing or duplicate evaluation ID")
        indexed[ident] = row
    return indexed, digest


def ids(row, field):
    value = row.get(field)
    if (not isinstance(value, list) or any(not isinstance(ident, str) or not ident for ident in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"Invalid evidence list field {field}")
    return value


def evaluate(labels, predictions):
    if labels.keys() != predictions.keys():
        raise ValueError("Run rows do not match the frozen collection")
    counts = Counter()
    sums = Counter()
    failures = Counter()
    for key, label in labels.items():
        prediction = predictions[key]
        expected = label.get("decision")
        verdict = prediction.get("decision")
        if not isinstance(expected, str) or expected not in DECISIONS or verdict not in DECISIONS:
            raise ValueError("Invalid evidence decision")
        gold = set(ids(label, "gold"))
        negatives = set(ids(label, "hard_negatives"))
        pool = set(ids(prediction, "candidate_pool"))
        ranked = ids(prediction, "ranked")
        support = set(ids(prediction, "support"))
        if (bool(gold) != (expected == "answer") or gold & negatives
                or not (set(ranked) | support) <= pool or bool(support) != (verdict == "answer")):
            raise ValueError("Inconsistent evidence labels or predictions")
        counts[expected] += 1
        sums[f"correct_{expected}"] += int(verdict == expected)
        counts["with_hard_negatives"] += bool(negatives)
        sums["hard_negative_selection"] += bool(negatives & support)
        if expected != "answer":
            failures["decision"] += int(verdict != expected)
            continue

        for k in RANKS:
            relevant = len(set(ranked[:k]) & gold)
            sums[f"recall_at_{k}"] += relevant / len(gold)
            sums[f"precision_at_{k}"] += relevant / k
            sums[f"hit_at_{k}"] += bool(relevant)
        ideal = sum(1 / math.log2(i + 2) for i in range(min(5, len(gold))))
        dcg = sum(1 / math.log2(i + 2) for i, item in enumerate(ranked[:5]) if item in gold)
        sums["ndcg_at_5"] += dcg / ideal
        sums["mrr"] += next((1 / (i + 1) for i, item in enumerate(ranked) if item in gold), 0)
        sums["evidence_completeness"] += len(gold & support) / len(gold)
        sums["evidence_exactness"] += gold == support

        if not gold <= pool:
            failures["candidate_recall"] += 1
        elif not gold <= set(ranked):
            failures["ranking"] += 1
        elif verdict != expected:
            failures["decision"] += 1
        elif support != gold:
            failures["evidence_selection"] += 1

    def ratio(numerator, denominator):
        return round(numerator / denominator, 6) if denominator else None

    metrics = {f"{metric}_at_{k}": ratio(sums[f"{metric}_at_{k}"], counts["answer"])
               for metric in ("recall", "precision", "hit") for k in RANKS}
    metrics.update({metric: ratio(sums[metric], counts["answer"])
                    for metric in ("mrr", "ndcg_at_5", "evidence_completeness", "evidence_exactness")})
    metrics.update({f"correct_{verdict}_rate": ratio(sums[f"correct_{verdict}"], counts[verdict])
                    for verdict in DECISIONS})
    metrics["decision_accuracy"] = ratio(sum(sums[f"correct_{verdict}"] for verdict in DECISIONS), len(labels))
    metrics["hard_negative_false_acceptance"] = ratio(sums["hard_negative_selection"],
                                                      counts["with_hard_negatives"])
    return {"questions": len(labels), "counts": dict(sorted(counts.items())), "metrics": metrics,
            "first_failure_counts": dict(sorted(failures.items()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--labels-sha256", required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    args = parser.parse_args()
    try:
        labels, label_digest = read_rows(args.labels, args.labels_sha256)
        predictions, prediction_digest = read_rows(args.predictions)
        report = evaluate(labels, predictions)
    except (OSError, ValueError, TypeError):
        parser.exit(2, "Invalid or unreadable frozen evaluation inputs. Case details withheld.\n")
    report.update(labels_sha256=label_digest, predictions_sha256=prediction_digest)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

