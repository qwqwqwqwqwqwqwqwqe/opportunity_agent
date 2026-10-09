"""Opt-in HTTP acceptance against an isolated REAL-model deployment.

Requires 40 human-reviewed cases (8 per category); never labels drafts as gold.
Does not compute RAG quality or treat Checker PASS as factual correctness.
"""
import argparse
import asyncio
import json
import secrets
import time
from pathlib import Path

import httpx


def reviewed_cases(data):
    categories = {"smalltalk", "profile", "research", "planning", "mixed"}
    cases = data.get("cases", [])
    if len(cases) != 40 or len({c["id"] for c in cases}) != 40:
        raise ValueError("Exactly 40 uniquely identified cases are required")
    if any(sum(c["category"] == category for c in cases) != 8 for category in categories):
        raise ValueError("Require 8 cases per category")
    for case in cases:
        if case.get("annotation_status") != "human_reviewed":
            raise ValueError("Human review is required before live acceptance")
        if not set(case["expected_agents"]) <= {"profile", "research", "planning"}:
            raise ValueError("Invalid expected agent")
    return cases


async def execute(args):
    if not args.confirm_isolated_environment:
        raise ValueError("Refusing to create test accounts without --confirm-isolated-environment")
    data = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    cases = reviewed_cases(data)
    results = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as client:
        for case in cases:
            # Case isolation prevents prior explicit preferences changing another case.
            registered = await client.post("/api/v1/auth/register", json={
                "email": f"live-{secrets.token_hex(10)}@example.test", "password": secrets.token_urlsafe(24)})
            registered.raise_for_status()
            profile = (await client.get("/api/v1/profile")).json()
            initial = await client.patch("/api/v1/profile", json={"request_id": secrets.token_hex(16),
                "expected_version": profile["version"], "payload": {"major": "Computer Science",
                "onboarding_completed": True, "target_countries": ["US"], "target_degree": "MS",
                "target_fields": ["AI"], "graduation_year": 2027, "planned_enrollment_year": 2028}})
            initial.raise_for_status()
            accepted = await client.post(f"/api/v1/approvals/{initial.json()['approval_id']}/accept", json={})
            accepted.raise_for_status()
            for repeat in range(3):
                started_at = time.monotonic()
                conversation = (await client.post("/api/v1/conversations", json={"title": case["id"]})).json()
                record = {"case_id": case["id"], "repeat": repeat, "expected_agents": case["expected_agents"]}
                try:
                    response = await client.post(f"/api/v1/conversations/{conversation['id']}/runs", json={
                        "message": case["message"], "request_id": secrets.token_hex(16)})
                    response.raise_for_status()
                    run_id = response.json()["run_id"]
                    run = {}
                    while time.monotonic()-started_at < 240:
                        response = await client.get(f"/api/v1/runs/{run_id}")
                        response.raise_for_status()
                        run = response.json()
                        if run["status"] in {"completed", "failed"}:
                            break
                        await asyncio.sleep(.5)
                    events = await client.get(f"/api/v1/runs/{run_id}/events") if run.get("status") in {"completed", "failed"} else None
                    route = None
                    if events:
                        event_name = None
                        for line in events.text.splitlines():
                            if line.startswith("event: "):
                                event_name = line[7:]
                            if line.startswith("data: "):
                                event = json.loads(line[6:])
                                if event_name == "route_selected":
                                    route = event["payload"]
                    actual = route.get("agents", []) if route else None
                    record.update(run_id=run_id, run_status=run.get("status"), actual_agents=actual,
                        route_match=actual is not None and set(actual) == set(case["expected_agents"]),
                        completion=run.get("completion"), answer=run.get("answer", ""),
                        research_route=(run.get("research_result") or {}).get("route"), error=run.get("error"))
                    record["contract_valid"] = run.get("status") == "completed" and route is not None and bool(run.get("answer"))
                except Exception as exc:
                    record.update(contract_valid=False, route_match=False, error=type(exc).__name__)
                record["seconds"] = round(time.monotonic()-started_at, 3)
                results.append(record)
    routing = sum(r["route_match"] for r in results)/len(results)
    report = {"model_label": args.model_label, "scope": "real-model HTTP routing/connectivity; NOT RAG gold quality",
              "records": results, "route_match_rate": routing,
              "contract_valid_count": sum(r["contract_valid"] for r in results),
              "automatic_gate_passed": routing >= .95 and all(r["contract_valid"] for r in results),
              "human_answer_and_safety_review": "pending"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # Saving an operator-selected report is part of the explicit CLI workflow.
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--model-label", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--confirm-isolated-environment", action="store_true")
    asyncio.run(execute(parser.parse_args()))
