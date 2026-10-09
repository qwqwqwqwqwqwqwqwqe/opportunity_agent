"""Two real HTTP queries, with immutable pending-review data checked before/after."""
import argparse
import asyncio
import hashlib
import json
import secrets
import time
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def pending_snapshot(url):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            rows = (await connection.execute(text("SELECT id,program_id,source_id,field,value,excerpt,status,program_match "
                "FROM research_requirements WHERE status='pending_review' ORDER BY id"))).all()
            encoded = json.dumps([list(r) for r in rows], ensure_ascii=False, default=str)
            return {"count": len(rows), "sha256": hashlib.sha256(encoded.encode()).hexdigest()}
    finally:
        await engine.dispose()


async def verify(args):
    report = {"runs": [], "pending_before": await pending_snapshot(args.database_url)}
    try:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=20, trust_env=False) as client:
            response = await client.post("/api/v1/auth/register", json={
                "email": "cache-acceptance-" + secrets.token_hex(8) + "@example.test",
                "password": secrets.token_urlsafe(32)})
            response.raise_for_status()
            for index in range(2):
                response = await client.post("/api/v1/conversations", json={"title": f"Cache acceptance {index + 1}"})
                response.raise_for_status()
                conversation = response.json()["id"]
                response = await client.post(f"/api/v1/conversations/{conversation}/runs", json={
                    "message": "帮我查询cmu msaii的截止日期", "request_id": secrets.token_hex(16)})
                response.raise_for_status()
                run_id = response.json()["run_id"]
                print(json.dumps({"started": index + 1, "run_id": run_id}), flush=True)
                started = time.monotonic()
                while True:
                    response = await client.get(f"/api/v1/runs/{run_id}")
                    response.raise_for_status()
                    run = response.json()
                    if run["status"] in {"completed", "failed"}:
                        break
                    if time.monotonic() - started > 240:
                        raise TimeoutError("Live run did not finish within 240 seconds")
                    await asyncio.sleep(1)
                result = run.get("research_result") or {}
                diagnostics = result.get("diagnostics", {})
                record = {"run_id": run_id, "trace_id": run.get("trace_id"), "status": run["status"],
                    "completion": run.get("completion"), "seconds": round(time.monotonic() - started, 3),
                    "answer": run.get("answer"), "error": run.get("error"),
                    "route": result.get("route"), "diagnostics": diagnostics,
                    "programs": result.get("programs", []), "research_errors": result.get("errors", [])}
                report["runs"].append(record)
                print(json.dumps({k: record[k] for k in ["run_id", "status", "seconds", "completion", "diagnostics"]}, ensure_ascii=True), flush=True)
                assert run["status"] == "completed", run.get("error")
                assert (run.get("completion") or {}).get("status") == "PASS", run.get("completion")
                assert result.get("status") == "complete"
                assert run.get("answer") and record["programs"]
                assert any(p.get("deadline") for p in record["programs"])
                if index == 0:
                    assert diagnostics.get("web_attempted") is True, "Expected an initially uncached lookup"
                    assert diagnostics.get("persisted_facts", 0) > 0, diagnostics.get("persist_errors")
                else:
                    assert diagnostics.get("cache_hit") is True
                    assert not diagnostics.get("web_attempted") and not diagnostics.get("search_calls")
                    before = report["runs"][0]["programs"][0]
                    after = record["programs"][0]
                    assert before["program_id"] == after["program_id"] and before["deadline"] == after["deadline"]
                    assert {e["url"] for e in before["evidence"]} & {e["url"] for e in after["evidence"]}
        report["pending_after"] = await pending_snapshot(args.database_url)
        assert report["pending_before"] == report["pending_after"], "Unreviewed observations changed"
        report["passed"] = True
    except Exception as exc:
        report.update(passed=False, failure_type=type(exc).__name__, failure=str(exc))
        raise
    finally:
        report["pending_after"] = await pending_snapshot(args.database_url)
        report["pending_unchanged"] = report["pending_before"] == report["pending_after"]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "report": str(args.output.resolve())}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", type=Path, default=Path("deliverables/synthesis-cache-live.json"))
    asyncio.run(verify(parser.parse_args()))
