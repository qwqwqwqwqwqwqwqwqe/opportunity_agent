from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import random
import statistics


@dataclass(frozen=True)
class RetrievalMetrics:
    recall_at_5: float
    mrr: float
    citation_precision: float | None
    citation_recall: float | None
    precision_at_5: float = 0.0
    hit_at_5: float = 0.0
    candidate_recall_at_50: float = 0.0
    answerable_count: int = 0
    unanswerable_count: int = 0
    abstention_accuracy: float | None = None


def fact_f1(predicted: Iterable[str], expected: Iterable[str]) -> float:
    predicted_set, expected_set = set(predicted), set(expected)
    if not predicted_set and not expected_set:
        return 1.0
    precision = len(predicted_set & expected_set) / max(1, len(predicted_set))
    recall = len(predicted_set & expected_set) / max(1, len(expected_set))
    return 2 * precision * recall / max(1e-9, precision + recall)


def retrieval_metrics(ranked_ids: list[list[str]], relevant_ids: list[set[str]], cited_ids: list[set[str]] | None = None) -> RetrievalMetrics:
    """Macro retrieval metrics; ID-only citation overlap is legacy, not claim support."""
    if len(ranked_ids) != len(relevant_ids) or (cited_ids is not None and len(cited_ids) != len(relevant_ids)):
        raise ValueError("Aligned query arrays are required")
    ranked_ids = [list(dict.fromkeys(ids)) for ids in ranked_ids]
    cases = [(ranked, gold) for ranked, gold in zip(ranked_ids, relevant_ids) if gold]
    count = max(1, len(cases))
    recall = sum(len(set(result[:5]) & relevant) / len(relevant) for result, relevant in cases) / count
    precision = sum(len(set(result[:5]) & relevant) / 5 for result, relevant in cases) / count
    hit = sum(bool(set(result[:5]) & relevant) for result, relevant in cases) / count
    candidate = sum(len(set(result[:50]) & relevant) / len(relevant) for result, relevant in cases) / count
    reciprocal = sum(next((1 / rank for rank, identifier in enumerate(result[:50], 1) if identifier in relevant), 0.0)
                     for result, relevant in cases) / count
    empty = [not result for result, relevant in zip(ranked_ids, relevant_ids) if not relevant]
    citation_labels_provided = cited_ids is not None
    cited_ids = cited_ids or [set() for _ in relevant_ids]
    cited = sum(len(citations) for citations in cited_ids)
    correct = sum(len(citations & relevant) for citations, relevant in zip(cited_ids, relevant_ids))
    total_relevant = sum(len(relevant) for relevant in relevant_ids)
    return RetrievalMetrics(recall, reciprocal, correct / cited if cited else None,
        correct / total_relevant if total_relevant and citation_labels_provided else None, precision, hit, candidate,
        len(cases), len(empty), sum(empty) / len(empty) if empty else None)


def set_metrics(predicted, expected):
    predicted, expected = set(predicted), set(expected)
    overlap = len(predicted & expected)
    return {"precision": overlap / len(predicted) if predicted else None,
            "recall": overlap / len(expected) if expected else None}


def answer_metrics(*, claims, required_claims, correct_claims, supported_claims, citation_pairs, valid_pairs):
    """All support/correctness labels come from a gold annotator, never the online Checker."""
    claims, required, correct = set(claims), set(required_claims), set(correct_claims)
    pairs, valid = set(map(tuple, citation_pairs)), set(map(tuple, valid_pairs))
    supported_citations = pairs & valid
    covered = {claim for claim, _ in supported_citations}
    return {"fact_precision": len(claims & correct) / len(claims) if claims else None,
            "fact_recall": len(claims & correct & required) / len(required) if required else None,
            "citation_precision": len(supported_citations) / len(pairs) if pairs else None,
            "citation_coverage": len(covered & required) / len(required) if required else None,
            "answer_faithfulness": len(claims & set(supported_claims)) / len(claims) if claims else None,
            "missing_citations": len(required - covered)}


def paired_bootstrap(before, after, *, samples=2000, seed=1729):
    if len(before) != len(after) or not before:
        raise ValueError("Nonempty paired observations required")
    differences = [a - b for b, a in zip(before, after)]
    randomizer = random.Random(seed)
    means = sorted(statistics.mean(randomizer.choices(differences, k=len(differences))) for _ in range(samples))
    baseline = statistics.mean(before)
    delta = statistics.mean(differences)
    return {"delta": delta, "percentage_points": 100 * delta,
            "relative_change": delta / baseline if baseline else None,
            "ci95": [means[int(samples * .025)], means[min(samples - 1, int(samples * .975))]]}


def calibrate_threshold(records, *, target_precision=.9):
    if not records or any(r.get("split") != "dev" for r in records):
        raise ValueError("Calibration requires dev-only labelled query/passage pairs")
    total_positive = sum(r["label"] == 2 for r in records)
    eligible = []
    for threshold in sorted({float(r["score"]) for r in records}):
        accepted = [r for r in records if r["score"] >= threshold]
        positive = sum(r["label"] == 2 for r in accepted)
        precision = positive / len(accepted)
        recall = positive / total_positive if total_positive else 0
        if precision >= target_precision:
            eligible.append({"threshold": threshold, "precision": precision, "recall": recall})
    return max(eligible, key=lambda r: (r["recall"], r["precision"], -r["threshold"])) if eligible else {
        "threshold": None, "reason": "target_precision_not_achieved"}
