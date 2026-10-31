"""Single-intent chat research evaluation: fixture web, real (or scripted) model.

Search results and pages come from synthetic fixtures, so every mode sees the
same web. The supplied model drives intent routing, the single-step summary or
the read-only agent loop. Modes run sequentially because CHAT_AGENT_MODE is a
process-wide setting. No database or business write path is involved; a run is
marked unsafe if any turn returns a proposal or an approval decision.

    python -m opportunity_agent.v3.chat_agent_eval --modes single,loop --out eval.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import statistics
import time
from uuid import uuid4

from .chat_search import ChatSearchService
from .monitor_web import PageError, parse_html
from .profile_assistant import ProfileAssistant

DATASET = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "chat_agent_eval" / "dataset.json"
STOP = {"the", "of", "and", "a", "an", "to", "for", "in", "is", "what", "are", "official"}
MAX_CONFIRMATIONS = 2


def page_html(page):
    body = "".join(f"<p>{p}</p>" for p in page["paragraphs"])
    links = "".join(f'<a href="{u}">{u}</a>' for u in page.get("links", []))
    return f"<html><main><h1>{page['title']}</h1>{body}<div>{links}</div></main></html>"


def words(text):
    return set(re.findall(r"[a-z0-9]+", text.casefold())) - STOP


class FixtureDiscovery:
    """Ranks the case's searchable pages by word overlap with the model's query."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    async def search(self, query, domains):
        self.calls.append(query)
        q = words(query)
        scored = []
        for page in self.pages:
            text = " ".join([page["title"], *page.get("keywords", []), *page["paragraphs"]])
            if score := len(q & words(text)):
                scored.append((score, page))
        scored.sort(key=lambda item: -item[0])
        return {"results":[{"url":p["url"], "title":p["title"], "content":p["paragraphs"][0][:200]} for _, p in scored[:5]]}

    search_advanced = search


class FixtureFetcher:
    def __init__(self, pages):
        self.by_url, self.calls = {p["url"]: p for p in pages}, []

    async def fetch(self, url, hosts, etag=None, modified=None):
        self.calls.append(url)
        page = self.by_url.get(url)
        if page is None:
            raise PageError("HTTP_404")
        return parse_html(page_html(page), url)


class CountingClient:
    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    @property
    def enabled(self):
        return self.inner.enabled

    @property
    def model(self):
        return getattr(self.inner, "model", "test")

    def generate_structured(self, *args, **kwargs):
        self.calls += 1
        return self.inner.generate_structured(*args, **kwargs)

    def generate(self, *args, **kwargs):
        self.calls += 1
        return self.inner.generate(*args, **kwargs)


def snapshot(profile):
    return {"today":"2026-10-31", "profile":profile, "profile_version":1, "preferences":[], "applications":[],
            "offerings":[], "requirements":[], "materials":[], "tasks":[], "calendar_events":[],
            "availability":{"timezone":"Asia/Shanghai"}}


def matches(answer, groups):
    text = answer.casefold()
    return all(any(alt.casefold() in text for alt in group) for group in groups)


def is_web(output):
    return bool({"agent", "search"} & set((output.get("route_decision") or {}).get("agents", [])))


async def run_case(case, data, client, mode):
    os.environ["CHAT_AGENT_MODE"] = mode
    served = [data["pages"][i] for i in case["pages"]]
    hidden = set(case.get("hidden", []))
    relevant = {data["pages"][i]["url"] for i in case["pages"] if i not in case.get("distractors", [])}
    discovery = FixtureDiscovery([data["pages"][i] for i in case["pages"] if i not in hidden])
    fetcher = FixtureFetcher(served)
    counting = CountingClient(client)
    assistant = ProfileAssistant(counting, ChatSearchService(counting, discovery, fetcher))
    profile = data["profiles"][case["profile"]]
    carried = {"chat_search_pending":None, "chat_evidence":None}
    recent, turns, routes = [], [], []
    confirmations, unsafe, error = 0, False, None
    started = time.monotonic()
    for message in case["turns"]:
        queue, first = [message], True
        while queue:
            text = queue.pop(0)
            initial = {"message":text, "workflow_snapshot":snapshot(profile), "request_id":uuid4().hex,
                       "user_id":"eval-user", "conversation_id":"eval-"+case["id"], "run_id":uuid4().hex,
                       "recent_messages":recent[-8:], "chat_action_draft":None, "chat_pending_approval_ids":[],
                       "profile_facts":[], **carried}
            try:
                output = await asyncio.wait_for(assistant.ainvoke(initial), 300)
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
                break
            unsafe |= bool(output.get("proposals") or output.get("approval_required")
                           or output.get("chat_approval_decision") or output.get("chat_supersede_approval_ids"))
            if first:
                routes.append(is_web(output))
                first = False
            answer = output.get("answer", "")
            recent += [{"role":"user", "content":text}, {"role":"assistant", "content":answer[:2000]}]
            carried["chat_search_pending"] = output.get("chat_search_pending")
            carried["chat_evidence"] = output.get("chat_evidence") or carried["chat_evidence"]
            turns.append({"message":text, "handler":(output.get("route_decision") or {}).get("agents", []),
                          "status":(output.get("completion") or {}).get("status"), "answer":answer})
            # Simulated user: confirms the candidate that belongs to the asked programme.
            candidates = (carried["chat_search_pending"] or {}).get("candidates", [])
            index = next((i for i, c in enumerate(candidates, 1) if c["url"] in relevant), None)
            if index and confirmations < MAX_CONFIRMATIONS:
                queue.append(f"确认第{index}个")
                confirmations += 1
        if error:
            break
    expect = case["expect"]
    final = turns[-1]["answer"] if turns else ""
    route_ok = bool(routes) and all(r == expect["web"] for r in routes)
    answered = matches(final, expect.get("contains", []))
    personal = matches(final, expect.get("personal", []))
    forbidden = [f for f in expect.get("forbidden", []) if f.casefold() in final.casefold()]
    return {"id":case["id"], "category":case["category"], "mode":mode,
            "success":route_ok and answered and personal and not forbidden and not unsafe and not error,
            "route_ok":route_ok, "answered":answered, "personal_ok":personal, "forbidden_hits":forbidden,
            "unsafe_write":unsafe, "error":error, "confirmations":confirmations, "model_calls":counting.calls,
            "searches":len(discovery.calls), "fetches":len(fetcher.calls),
            "latency_s":round(time.monotonic()-started, 2), "final_status":turns[-1]["status"] if turns else None,
            "turns":turns}


def summarize(rows):
    def rate(items, key):
        return round(sum(bool(r[key]) for r in items)/len(items), 3) if items else None
    personal = [r for r in rows if r["category"] in {"personal_match", "follow_up"}]
    categories = sorted({r["category"] for r in rows})
    return {"cases":len(rows), "success_rate":rate(rows, "success"), "route_accuracy":rate(rows, "route_ok"),
            "answer_rate":rate(rows, "answered"), "personal_rate":rate(personal, "personal_ok"),
            "forbidden_hits":sum(len(r["forbidden_hits"]) for r in rows),
            "unsafe_writes":sum(r["unsafe_write"] for r in rows), "errors":sum(bool(r["error"]) for r in rows),
            "avg_confirmations":round(statistics.mean(r["confirmations"] for r in rows), 2) if rows else None,
            "avg_model_calls":round(statistics.mean(r["model_calls"] for r in rows), 2) if rows else None,
            "median_latency_s":statistics.median(r["latency_s"] for r in rows) if rows else None,
            "by_category":{c:rate([r for r in rows if r["category"] == c], "success") for c in categories}}


async def evaluate(client, modes=("single", "loop"), dataset=DATASET, case_ids=None):
    data = json.loads(Path(dataset).read_text(encoding="utf-8"))
    cases = [c for c in data["cases"] if not case_ids or c["id"] in case_ids]
    previous = os.environ.get("CHAT_AGENT_MODE")
    report = {"dataset":str(dataset), "synthetic":data.get("synthetic", False), "modes":{}}
    try:
        for mode in modes:
            rows = [await run_case(case, data, client, mode) for case in cases]
            report["modes"][mode] = {"summary":summarize(rows), "cases":rows}
    finally:
        if previous is None:
            os.environ.pop("CHAT_AGENT_MODE", None)
        else:
            os.environ["CHAT_AGENT_MODE"] = previous
    return report


def main():
    from ..llm_client import LLMClient
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--modes", default="single,loop")
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--cases", default="", help="comma-separated case ids; default all")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    client = LLMClient(timeout_seconds=25, retries=0)
    if not client.enabled:
        raise SystemExit("LLM is not configured; set LLM_API_KEY / LLM_API_BASE / LLM_MODEL")
    report = asyncio.run(evaluate(client, tuple(m for m in args.modes.split(",") if m), args.dataset,
                                  {c for c in args.cases.split(",") if c} or None))
    for mode, value in report["modes"].items():
        print(mode, json.dumps(value["summary"], ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
