"""Replay synthetic context cases. Offline mode probes routing, not LLM quality.

Run from the project root with `python -m scripts.audit_profile_context`.
--live sends only the synthetic fixture to the configured model provider.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

from opportunity_agent.llm_client import LLMClient
from opportunity_agent.llm_context import conversation_scope
from opportunity_agent.v2.agents.contracts import ExecutionState, RouteDecision
from opportunity_agent.v2.agents.orchestrator import LLMSynthesizer
from opportunity_agent.v2.agents.profile_extraction import ProfileExtractionPipeline
from opportunity_agent.v2.evaluation.profile_benchmark import _counts, _identities, _score
from opportunity_agent.v2.services.conversation_context import profile_summary

DATASET = Path(__file__).resolve().parents[1] / "tests/fixtures/profile_context_multiturn.json"


def load_cases():
    return json.loads(DATASET.read_text(encoding="utf-8"))["cases"]


def case_context(case):
    return {"recent_messages": case.get("history", []), "summary": case.get("summary", ""),
            "profile_summary": profile_summary(case.get("profile", {})),
            "relevant_preferences": case.get("preferences", [])}


def oracle_response(case):
    """Ideal extractor test double: detects whether auto routing ever invokes it."""
    return json.dumps({
        "facts": [{"field": f["field"], "raw_value": f["value"], "evidence": f["evidence"],
                   "confidence": .99, "source": "llm"} for f in case.get("expected_facts", [])],
        "preferences": [dict(p, evidence=case["message"], confidence=.99)
                        for p in case.get("expected_preferences", [])],
    }, ensure_ascii=False)


def evaluate_case(case, mode="auto", live=False, timeout=35):
    started = time.perf_counter()
    client = LLMClient(timeout_seconds=timeout, retries=0,
                       completion_fn=None if live else lambda payload: oracle_response(case))
    context = {} if mode == "no_context" else case_context(case)
    record = {"id": case["id"], "mode": mode, "live": live, "message": case["message"]}
    try:
        with conversation_scope(context):
            if case.get("kind") == "recall":
                state = ExecutionState(user_id="synthetic-audit", conversation_id=case["id"],
                    run_id=case["id"], request_id="probe", message=case["message"],
                    conversation_context=context, route_decision=RouteDecision(mode="direct_reply", reason="audit"))
                answer = asyncio.run(LLMSynthesizer(client).synthesize(state))
                record.update(answer=answer, passed=case["expected_answer_contains"] in answer)
            else:
                outcome = ProfileExtractionPipeline(client).extract(
                    case["message"], context, mode="hybrid" if mode == "no_context" else mode)
                facts = [{"field": f.field, "value": f.value} for f in outcome.accepted_facts]
                prefs = [{"key": p.key, "value": p.value} for p in outcome.preferences]
                fact_score = _score(_counts(_identities(facts), _identities(case.get("expected_facts", []))))
                pref_score = _score(_counts(_identities(prefs, preference=True),
                                           _identities(case.get("expected_preferences", []), preference=True)))
                record.update(route=outcome.route_path, actual_mode=outcome.mode, facts=facts,
                    preferences=prefs, errors=outcome.errors, fact_score=fact_score, preference_score=pref_score,
                    passed=not outcome.semantic_failed and not any(s["fp"] or s["fn"] for s in (fact_score, pref_score)),
                    semantic_failed=outcome.semantic_failed,
                    evidence_valid=all(f.evidence and f.evidence in case["message"] for f in outcome.accepted_facts))
            record["transport_error"] = bool(client.last_error)
    except Exception as exc:
        # Avoid logging network exception strings, which may contain provider URLs.
        record.update(passed=False, error_type=type(exc).__name__)
    record["latency_ms"] = round((time.perf_counter() - started) * 1000)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--cases", nargs="*")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.live and not LLMClient().enabled:
        raise SystemExit("No model credentials configured; use offline mode.")
    cases = [c for c in load_cases() if not args.cases or c["id"] in args.cases]
    jobs = [(c, mode) for c in cases for mode in
            (("hybrid", "no_context") if c.get("kind") == "recall" else ("auto", "hybrid", "no_context"))
            if args.live or (c.get("kind") != "recall" and mode != "no_context")]
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        records = list(pool.map(lambda job: evaluate_case(*job, live=args.live, timeout=args.timeout), jobs))
    summary = {}
    for mode in sorted({r["mode"] for r in records}):
        selected = [r for r in records if r["mode"] == mode]
        summary[mode] = {"cases": len(selected), "passed": sum(r["passed"] for r in selected),
                         "failed_ids": [r["id"] for r in selected if not r["passed"]]}
    report = {"dataset": str(DATASET), "model": LLMClient().model if args.live else "ideal_extractor_test_double",
        "limitations": "Synthetic scripted history; exact value matching; not human Gold, not browser E2E. "
                        "Offline scores only diagnose gates/validators, not model accuracy. no_context is an ablation, not a required pass.",
        "summary": summary, "records": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
