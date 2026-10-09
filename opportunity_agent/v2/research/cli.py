"""Research-only diagnostics and validated knowledge ingestion."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path

from ..agents.a2a import DomainA2ARequest
from ..agents.contracts import SuccessCriteria
from ..agents.research_agent import ResearchAgent


async def run(args):
    if args.command in {"mcp-check", "mcp-smoke"}:
        from .web import TavilyMCP
        async with TavilyMCP() as web:
            if args.command == "mcp-check":
                return {"connected": True, "tools": sorted(web.tools), "search_calls": 0}
            data = await web.search("Carnegie Mellon MSCS admissions GRE deadline", ["cmu.edu"])
            page = None
            from urllib.parse import urlparse
            for item in data.get("results", []):
                host = (urlparse(item.get("url", "")).hostname or "").casefold()
                if host == "cmu.edu" or host.endswith(".cmu.edu"):
                    page = await web.read(item["url"], ["cmu.edu"])
                    break
            if page is None or not page["text"].strip():
                raise RuntimeError("No readable official page in MCP smoke test")
            extracted = await web._call("tavily_extract", {"urls": [page["url"]], "format": "text", "extract_depth": "basic"})
            return {"connected": True, "tools": sorted(web.tools), "search_calls": web.search_calls,
                "page_calls": web.page_calls, "url": page["url"], "title": page["title"],
                "body_characters": len(page["text"]), "extract_results": len(extracted.get("results", []))}
    if args.command == "ingest":
        from ..db.session import SessionLocal
        from .ingestion import ingest_page
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        async with SessionLocal.begin() as session:
            return await ingest_page(session, payload)
    criteria = SuccessCriteria(required_program_count=args.count,
        gre_policy=args.gre, deadline_after=date.fromisoformat(args.deadline_after) if args.deadline_after else None,
        evidence_required=True, citation_required=True)
    request = DomainA2ARequest(agent="research", user_id="research-cli", conversation_id="research-cli",
        run_id="research-cli", request_id="research-cli", message=args.message, success_criteria=criteria)
    return (await ResearchAgent().aexecute(request)).model_dump(mode="json")


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("command", choices=["query", "ingest", "mcp-check", "mcp-smoke"])
    parser.add_argument("--message")
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--count", type=int)
    parser.add_argument("--gre", choices=["any", "required", "not_required"], default="any")
    parser.add_argument("--deadline-after")
    args = parser.parse_args()
    if args.command == "query" and not args.message:
        parser.error("query requires --message")
    if args.command == "ingest" and not args.input:
        parser.error("ingest requires --input")
    result = asyncio.run(run(args))
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved {output}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
