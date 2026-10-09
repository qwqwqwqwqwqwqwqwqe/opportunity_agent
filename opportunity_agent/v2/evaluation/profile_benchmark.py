"""Resumable value-level benchmark for ProfileExtractionPipeline.

Use this with a reviewed Gold JSONL fixture in production.  The supplied fixture
is intentionally marked silver until people review and sign off its labels.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from ...llm_client import LLMClient
from ..agents.profile_extraction import ProfileExtractionPipeline


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _identities(items: Iterable[dict], *, preference: bool = False) -> set[str]:
    key_name = "key" if preference else "field"
    result: set[str] = set()
    for item in items:
        value = item.get("value")
        # Lists are append-only profile values.  Score every listed value, not
        # the entire list, so a missing experience has a visible recall cost.
        values = value if isinstance(value, list) else [value]
        for entry in values:
            result.add(f"{item[key_name]}={_canonical(entry)}")
    return result


def _counts(predicted: set[str], expected: set[str]) -> Counter:
    return Counter(tp=len(predicted & expected), fp=len(predicted - expected), fn=len(expected - predicted))


def _score(counts: Counter) -> dict[str, float | int]:
    precision = counts["tp"] / max(1, counts["tp"] + counts["fp"])
    recall = counts["tp"] / max(1, counts["tp"] + counts["fn"])
    return {"tp": counts["tp"], "fp": counts["fp"], "fn": counts["fn"],
            "precision": round(precision, 6), "recall": round(recall, 6),
            "f1": round(2 * precision * recall / max(1e-12, precision + recall), 6)}


def _execute(case: dict, mode: str, timeout: int) -> dict:
    started = time.perf_counter()
    try:
        # Independent clients make worker threads safe; retries are disabled so
        # an outage does not silently inflate spending or benchmark latency.
        outcome = ProfileExtractionPipeline(LLMClient(timeout_seconds=timeout, retries=0)).extract(case["input"], mode=mode)
        facts = [{"field": fact.field, "value": fact.value} for fact in outcome.accepted_facts]
        preferences = [{"key": pref.key, "value": pref.value} for pref in outcome.preferences]
        return {"id": case["id"], "mode": mode, "category": case["category"], "tags": case.get("tags", []),
                "expected_route": case["expected_route"], "actual_route": outcome.route_path,
                "route_reason": outcome.route_reason,
                "facts": facts, "preferences": preferences, "errors": outcome.errors,
                "actual_mode": outcome.mode, "latency_ms": int((time.perf_counter() - started) * 1000)}
    except Exception as exc:  # preserve failed calls as scored empty output
        return {"id": case["id"], "mode": mode, "category": case["category"], "tags": case.get("tags", []),
                "expected_route": case["expected_route"], "actual_route": "failed", "route_reason": "executor_failure",
                "facts": [], "preferences": [], "errors": [f"executor_error:{type(exc).__name__}"],
                "actual_mode": "failed", "latency_ms": int((time.perf_counter() - started) * 1000)}


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _summarize(cases: list[dict], records: list[dict], mode: str) -> dict:
    by_id = {case["id"]: case for case in cases}
    facts, preferences = Counter(), Counter()
    categories: dict[str, Counter] = defaultdict(Counter)
    invalid_total = invalid_rejected = failures = model_calls = 0
    latencies: list[int] = []
    route_correct = 0
    route_confusion: dict[str, Counter] = defaultdict(Counter)
    for record in records:
        case = by_id[record["id"]]
        fact_count = _counts(_identities(record["facts"]), _identities(case["expected_facts"]))
        pref_count = _counts(_identities(record["preferences"], preference=True),
                             _identities(case["expected_preferences"], preference=True))
        facts.update(fact_count)
        preferences.update(pref_count)
        categories[case["category"]].update(fact_count)
        if "invalid_value" in case.get("tags", []):
            invalid_total += 1
            invalid_rejected += not record["facts"] and not record["preferences"]
        failures += record["actual_mode"] == "failed" or any(error.startswith("llm_unavailable") or error.startswith("executor_error")
                                                                for error in record["errors"])
        route = record.get("actual_route", "unknown")
        route_confusion[case["expected_route"]][route] += 1
        route_correct += route == case["expected_route"]
        # Forced ablations intentionally call the model regardless of the
        # route gate.  Auto mode spends one model-call unit only for routed
        # LLM/hybrid cases, including requests that later time out.
        model_calls += mode in {"llm_only", "hybrid"} or (mode == "auto" and route in {"llm_only", "hybrid"})
        latencies.append(record["latency_ms"])
    return {
        "mode": mode, "cases": len(records), "fact_metrics": _score(facts),
        "preference_metrics": _score(preferences),
        "fact_metrics_by_category": {name: _score(value) for name, value in sorted(categories.items())},
        "invalid_fact_rejection_rate": round(invalid_rejected / max(1, invalid_total), 6),
        "failed_or_unavailable_calls": failures,
        "latency_ms": {"mean": round(sum(latencies) / max(1, len(latencies)), 2), "max": max(latencies, default=0)},
        "model_calls": model_calls,
        "model_call_rate": round(model_calls / max(1, len(records)), 6),
        "relative_model_cost_vs_forced_hybrid": round(model_calls / max(1, len(records)), 6),
        "model_call_cost_reduction_vs_forced_hybrid": round(1 - model_calls / max(1, len(records)), 6),
        "route_accuracy": round(route_correct / max(1, len(records)), 6) if mode == "auto" else None,
        "route_confusion": {expected: dict(actuals) for expected, actuals in sorted(route_confusion.items())} if mode == "auto" else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("tests/fixtures/profile_extraction_silver_360.jsonl"))
    parser.add_argument("--output", type=Path, required=True, help="JSON report path; a .records.jsonl sibling is created")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--reuse-relabelled-records", action="store_true",
                        help="reuse cached predictions by id after a label-only review; never use this if input text changed")
    parser.add_argument("--refresh-modes", nargs="*", default=[],
                        help="re-execute named modes and append replacement records; useful after routing code changes")
    parser.add_argument("--modes", nargs="+", default=["rule_only", "llm_only", "hybrid", "auto"],
                        choices=["rule_only", "llm_only", "hybrid", "auto"])
    arguments = parser.parse_args()
    cases = _load_jsonl(arguments.dataset)
    if not 300 <= len(cases) <= 500:
        raise SystemExit(f"dataset must contain 300--500 cases, got {len(cases)}")
    digest = hashlib.sha256(arguments.dataset.read_bytes()).hexdigest()
    records_path = arguments.output.with_suffix(".records.jsonl")
    cached: dict[tuple[str, str], dict] = {}
    if records_path.exists():
        for line in records_path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item.get("dataset_sha256") == digest or arguments.reuse_relabelled_records:
                cached[(item["mode"], item["id"])] = item
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    modes: dict[str, list[dict]] = {}
    for mode in arguments.modes:
        missing = [case for case in cases if mode in arguments.refresh_modes or (mode, case["id"]) not in cached]
        print(f"{mode}: {len(cases) - len(missing)}/{len(cases)} cached; {len(missing)} to run", flush=True)
        with records_path.open("a", encoding="utf-8") as handle:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, arguments.workers)) as pool:
                futures = {pool.submit(_execute, case, mode, arguments.timeout): case for case in missing}
                for completed, future in enumerate(concurrent.futures.as_completed(futures), 1):
                    record = future.result() | {"dataset_sha256": digest}
                    cached[(mode, record["id"])] = record
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                    handle.flush()
                    if completed % 10 == 0 or completed == len(missing):
                        print(f"{mode}: completed {completed}/{len(missing)}", flush=True)
        modes[mode] = [cached[(mode, case["id"])] for case in cases]
    report = {
        "dataset": str(arguments.dataset), "dataset_sha256": digest, "label_source": "programmatic_template_annotation",
        "human_review": "pending", "warning": "This is a silver regression benchmark, not a human Gold Dataset.",
        "generated_at_epoch": time.time(), "results": {mode: _summarize(cases, records, mode) for mode, records in modes.items()},
    }
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["results"], ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
