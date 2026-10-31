"""Optional real-model smoke, synthetic context only; no database writes.

Run from the project root: python scripts/smoke_chat_actions.py
Uses the configured model and makes three billable extraction calls. Output is
limited to structural assertions and timings, never credentials/user records.
"""
import asyncio
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v3.chat_actions import handle_actions


async def main():
    client = LLMClient(timeout_seconds=25, retries=0)
    if not client.enabled:
        raise SystemExit("Model not configured; no smoke performed")
    snapshot = {"today":"2026-10-29", "availability":{"timezone":"Asia/Shanghai"}, "applications":[],
                "offerings":[], "requirements":[], "materials":[], "tasks":[], "calendar_events":[]}
    cases = [
        ("帮我添加 CMU 的 MSAII，2027 Fall，2026年12月15日截止，需要SOP、3封推荐信和托福105。再添加这个项目的待办：明天修改SOP，显示在日历。11月2日下午3点到4点见推荐人，新增日程。", "full_create"),
        ("帮我添加 Stanford 的 MSCS 项目", "missing_cycle"),
        ("帮我生成3条全局申请准备todo，不设置日期，不显示在日历", "generated_todos"),
    ]
    for query, name in cases:
        started = time.perf_counter()
        result = await handle_actions(client, {"message":query, "workflow_snapshot":snapshot, "request_id":uuid4().hex})
        assert result, name
        if name == "full_create":
            assert result["completion"]["status"] == "WAITING_APPROVAL", result["completion"]
            commands = result["proposals"][0]["payload"]["commands"]
            assert [c["type"] for c in commands] == ["project.save", "task.create", "calendar_event.create"]
            assert commands[1]["payload"]["due_date"] == "2026-10-30"
            assert commands[1]["payload"]["application_id"].startswith("$ref:")
            assert commands[2]["payload"]["start_at"].startswith("2026-11-02T15:00:00")
        elif name == "missing_cycle":
            assert result["completion"]["status"] == "NEED_USER" and not result["proposals"]
        else:
            assert result["completion"]["status"] == "WAITING_APPROVAL", result["completion"]
            commands = result["proposals"][0]["payload"]["commands"]
            assert len(commands) == 3 and all(c["type"] == "task.create" and c["payload"]["due_date"] is None for c in commands)
        print(json.dumps({"case":name, "passed":True, "seconds":round(time.perf_counter()-started, 2), "model":client.model}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
