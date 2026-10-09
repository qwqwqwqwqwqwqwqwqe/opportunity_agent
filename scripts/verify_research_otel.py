"""Run real catalogue reads, export OTLP, and verify the resulting Jaeger trace."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
from opentelemetry import trace
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://localhost:4318"
os.environ["OTEL_SERVICE_NAME"] = "opportunity-research-verification"
os.environ["RESEARCH_WEB_ENABLED"] = "0"

from opportunity_agent.v2.core.telemetry import span, trace_carrier
from opportunity_agent.v2.agents.a2a import DomainA2ARequest
from opportunity_agent.v2.research.service import ResearchService


async def main():
    engine = create_async_engine("postgresql+asyncpg://opportunity:opportunity_dev_only@localhost:5432/opportunity_research_eval_20261004")
    try:
        with span("orchestrator.research.smoke", run_id="research-verification") as parent:
            parent_id = format(parent.get_span_context().trace_id, "032x")
            carrier = trace_carrier()
            with span("domain.execute", carrier=carrier, agent="research", run_id="research-verification"):
                async with async_sessionmaker(engine)() as session:
                    request = DomainA2ARequest(agent="research", user_id="verification", conversation_id="verification",
                        run_id="research-verification", request_id="research-verification", message="截止日期和 GRE")
                    await ResearchService(session, llm=SimpleNamespace(enabled=False)).execute(request)
        trace.get_tracer_provider().force_flush(timeout_millis=10000)
        async with httpx.AsyncClient(timeout=10) as client:
            for _ in range(20):
                response = await client.get("http://localhost:16686/api/traces/" + parent_id)
                response.raise_for_status()
                data = response.json().get("data", [])
                if data:
                    break
                await asyncio.sleep(.25)
        spans = data[0]["spans"] if data else []
        names = {s["operationName"] for s in spans}
        required = {"orchestrator.research.smoke", "domain.execute", "research.execute", "parse_task", "route", "sql.retrieve"}
        assert required <= names, names
        assert all(s["traceID"] == parent_id for s in spans)
        report = {"trace_id": parent_id, "span_count": len(spans), "operations": sorted(names), "w3c_linked": True}
        Path("deliverables/research/otel-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
    finally:
        await engine.dispose()
        trace.get_tracer_provider().shutdown()


if __name__ == "__main__":
    asyncio.run(main())
