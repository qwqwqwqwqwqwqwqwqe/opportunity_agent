from __future__ import annotations

import asyncio
import argparse
from datetime import date, datetime, timezone
from pathlib import Path
import sys

# Support both `python -m opportunity_agent.main` and an IDE's direct
# `python opportunity_agent/main.py` command. Direct execution has no package
# context, so add the project root before importing through the package name.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from opportunity_agent.lifecycle_agent import LifecycleAgent
    from opportunity_agent.models import ExternalEvent
else:
    from .lifecycle_agent import LifecycleAgent
    from .models import ExternalEvent


def run_interactive(input_fn=input, output_fn=print) -> LifecycleAgent:
    """Collect the minimum high-value profile fields through normal dialogue."""
    agent = LifecycleAgent("student_001")
    output_fn("你好，我是留学路径规划 Agent。先告诉我：你目前的情况和出国目标是什么？")
    while agent.roadmap is None:
        try:
            message = input_fn("你：").strip()
        except EOFError:
            output_fn("未收到输入，已结束 onboarding。")
            break
        if message.lower() in {"/quit", "/exit", "退出"}:
            output_fn("已结束 onboarding。")
            break
        if not message:
            output_fn("请用一句话补充你的情况；也可以输入 /quit 退出。")
            continue
        reply = agent.on_user_message(message)
        output_fn(f"Agent：{reply}")

    if agent.roadmap is not None:
        output_fn("\n已生成你的路线图：")
        for milestone in agent.roadmap.milestones:
            output_fn(f"- {milestone.title}")
            for task in milestone.tasks:
                output_fn(f"  - {task.title}（原因：{task.reason}；来源：{task.source}；置信度：{task.confidence:.0%}）")
        if agent.job_recommendations:
            output_fn("\n岗位推荐：")
            for item in agent.job_recommendations:
                output_fn(f"- {item.company} · {item.title}：{item.score:.0%}（{item.reason}；来源：{item.source}；置信度：{item.confidence:.0%}）")
    return agent


def run_demo() -> None:
    """Repeatable acceptance demo: it intentionally supplies complete onboarding data."""
    agent = LifecycleAgent("student_001")
    print("[1] Progressive profiling")
    print(agent.on_user_message("我是大一 CS，想申请美国 AI 硕士。"))
    print(agent.on_user_message("GPA 3.9，排名 5/120"))
    print(agent.on_user_message("我还没开始准备托福，也没有科研经历。"))
    print(agent.on_user_message("2029"))
    print("[2] User state derived:", agent.state.model_dump())
    roadmap = agent.roadmap
    assert roadmap is not None
    print("[3] Roadmap generated:", roadmap.goal)
    print("[4] External deadline event detected")
    decision = agent.on_external_event(ExternalEvent(
        event_id="evt_cmu_deadline", type="program_deadline_changed", title="CMU MSAI deadline updated",
        source_url="https://example.edu/official", published_at=datetime.now(timezone.utc),
        program_name="CMU MSAI", old_deadline=date(2029, 12, 15), new_deadline=date(2029, 11, 1),
    ), today=date(2029, 8, 15))
    print("[5] Replanning completed")
    submit = next(task for milestone in agent.roadmap.milestones for task in milestone.tasks if task.task_id == "submit_application")
    print(f"Notification: {decision.action.upper()} ({decision.score:.0%}) — {decision.reason}")
    print("Updated application deadline:", submit.due_date)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Personal Opportunity Awareness Agent V1")
    parser.add_argument("--demo", action="store_true", help="运行固定的 deadline 重规划验收演示")
    args = parser.parse_args()
    if args.demo:
        run_demo()
    else:
        run_interactive()


if __name__ == "__main__":
    asyncio.run(main())
