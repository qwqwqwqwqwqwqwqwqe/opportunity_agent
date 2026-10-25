"""Pure business rules. No model calls, persistence, wall clock or network."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def day(value):
    return date.fromisoformat(str(value)[:10]) if value else None


def offering_deadline(offering):
    if offering.get("deadline_date"):
        return day(offering["deadline_date"])
    if offering.get("deadline_at"):
        instant = datetime.fromisoformat(str(offering["deadline_at"]).replace("Z", "+00:00"))
        return instant.astimezone(ZoneInfo(offering["timezone"])).date()
    return None


def score_check(criteria, snapshot):
    """True/False/None: alternatives are OR, all conditions per test are AND."""
    alternatives = criteria.get("any_of") or [{"profile_field": criteria.get("profile_field"), "minimum": criteria.get("minimum")}]
    outcomes = []
    for alternative in alternatives:
        field, minimum = alternative.get("profile_field"), alternative.get("minimum")
        actual = snapshot.get("profile", {}).get(field) if field else None
        if type(minimum) not in (int, float) or type(actual) not in (int, float):
            outcomes.append(None)
            continue
        if actual < minimum:
            outcomes.append(False)
            continue
        actual_parts = snapshot.get("profile", {}).get((field or "").replace("_score", "_subscores"), {})
        parts = alternative.get("subscores") or {}
        if any(type(actual_parts.get(key)) not in (int, float) for key in parts):
            outcomes.append(None)
        else:
            outcomes.append(all(actual_parts[key] >= value for key, value in parts.items()))
    until = day(criteria.get("valid_until"))
    if until and until < day(snapshot["today"]):
        return False, "成绩已过期"
    if criteria.get("requires_validity") and until is None:
        return None, "成绩有效期未知"
    if True in outcomes:
        return True, "成绩已达标"
    if None in outcomes:
        return None, "成绩或门槛未知"
    return False, "成绩未达标"


def review(snapshot: dict, application_id: str | None = None) -> dict:
    requirements = [r for r in snapshot["requirements"] if not r.get("archived") and (not application_id or r["application_id"] == application_id)]
    missing, risks, evidence = [], [], []
    unknown = not requirements
    for requirement in requirements:
        if requirement["obligation"] == "optional":
            continue
        sources = [requirement["source_id"]] if requirement.get("source_id") else []
        evidence.extend(sources)
        if not sources:
            risks.append({"requirement_id": requirement["id"], "reason": "要求来源未记录"})
        if requirement["obligation"] == "unknown":
            unknown = True
            risks.append({"requirement_id": requirement["id"], "reason": "是否必需尚未确认"})
            continue
        criteria = requirement.get("criteria") or {}
        if requirement["kind"] == "score":
            satisfied, reason = score_check(criteria, snapshot)
            unknown = unknown or satisfied is None
            if satisfied is not True:
                missing.append({"requirement_id": requirement["id"], "reason": reason, "source_ids": sources})
            continue
        count = requirement.get("required_count")
        if count is None:
            unknown = True
            missing.append({"requirement_id": requirement["id"], "reason": "所需数量未知", "source_ids": sources})
            continue
        required_status = criteria.get("required_status", "submitted")
        accepted = {"ready", "submitted"} if required_status == "ready" else {"submitted"}
        materials = [m for m in snapshot["materials"] if m["requirement_id"] == requirement["id"] and not m.get("archived")]
        fulfilled = sum(m["status"] in accepted for m in materials)
        if fulfilled < count:
            missing.append({"requirement_id": requirement["id"], "required": count, "fulfilled": fulfilled,
                            "missing": count - fulfilled, "source_ids": sources, "reason": "材料尚未满足要求"})
    return {"schema_version": 3, "input_revision": snapshot["revision"],
            "completeness": "incomplete" if missing else "unknown" if unknown else "complete",
            "missing_requirements": missing, "risks": risks, "evidence_refs": sorted(set(evidence)),
            "coverage_note": "仅检查已记录的要求，未验证学校要求是否齐全"}


def schedule(snapshot: dict, application_ids: list[str] | None = None) -> dict:
    today = day(snapshot["today"])
    tasks = [dict(t) for t in snapshot["tasks"] if t["status"] != "cancelled"]
    offerings = {o["id"]: o for o in snapshot["offerings"]}
    applications = {a["id"]: a for a in snapshot["applications"]}
    selected = set(application_ids or applications)
    existing_requirements = {t.get("requirement_id") for t in tasks}
    missing_requirement_ids = {item["requirement_id"] for item in review(snapshot)["missing_requirements"]}
    for requirement in snapshot["requirements"]:
        if requirement.get("archived") or requirement["application_id"] not in selected or requirement["obligation"] != "required":
            continue
        if requirement["id"] not in missing_requirement_ids:
            continue
        if requirement["id"] not in existing_requirements:
            tasks.append({"id": None, "application_id": requirement["application_id"], "requirement_id": requirement["id"],
                          "stable_key": f"requirement:{requirement['id']}", "title": f"准备 {requirement.get('title') or requirement['key']}",
                          "duration_days": 3, "locked": False, "status": "planned", "version": None,
                          "start_date": None, "due_date": None, "date_certainty": "estimated"})
    nodes = {t["id"] or t["stable_key"]: t for t in tasks}
    if not nodes:
        return {"input_revision": snapshot["revision"], "items": [], "conflicts": ["没有可规划的任务或已记录要求"], "assumptions": []}
    incoming = {key: [] for key in nodes}
    outgoing = {key: [] for key in nodes}
    for edge in snapshot["dependencies"]:
        a, b = edge["predecessor_id"], edge["successor_id"]
        if a in nodes and b in nodes:
            incoming[b].append((a, edge["lag_days"]))
            outgoing[a].append((b, edge["lag_days"]))
    ready = sorted(k for k in nodes if not incoming[k])
    order, counts = [], {k: len(v) for k, v in incoming.items()}
    while ready:
        key = ready.pop(0)
        order.append(key)
        for nxt, _ in outgoing[key]:
            counts[nxt] -= 1
            if counts[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    if len(order) != len(nodes):
        return {"input_revision": snapshot["revision"], "items": [], "conflicts": ["任务依赖存在环"], "assumptions": []}
    weekdays = set(snapshot.get("availability", {}).get("weekdays", [0, 1, 2, 3, 4]))
    if not weekdays or not weekdays <= set(range(7)):
        raise ValueError("availability weekdays must be a nonempty subset of 0..6")
    blocked = set()
    for event in snapshot["calendar_events"]:
        if event["type"] not in {"unavailable", "exam"}:
            continue
        start = day(event.get("date"))
        end = start
        if event.get("start_at"):
            zone = ZoneInfo(event["timezone"])
            start = datetime.fromisoformat(event["start_at"]).astimezone(zone).date()
            end = datetime.fromisoformat(event.get("end_at") or event["start_at"]).astimezone(zone).date()
        if start:
            blocked.update(start + timedelta(days=n) for n in range((end - start).days + 1))

    def work(d, direction=1):
        for _ in range(4000):
            if d.weekday() in weekdays and d not in blocked:
                return d
            d += timedelta(days=direction)
        raise ValueError("no available working day within planning horizon")

    def shift(d, amount):
        direction = 1 if amount >= 0 else -1
        d = work(d, direction)
        for _ in range(abs(amount)):
            d = work(d + timedelta(days=direction), direction)
        return d

    conflicts, earliest, latest = [], {}, {}
    # Partial replanning also moves downstream tasks dependent on the changed scope.
    affected = {k for k, t in nodes.items() if not application_ids or t.get("application_id") in selected}
    queue = list(affected)
    while queue:
        for nxt, _ in outgoing[queue.pop()]:
            if nxt not in affected:
                affected.add(nxt)
                queue.append(nxt)
    fixed = {k for k, t in nodes.items() if t.get("locked") or t["status"] == "completed" or k not in affected}
    for key in order:
        task = nodes[key]
        start = today
        for previous, lag in incoming[key]:
            if earliest[previous][1]:
                start = max(start, earliest[previous][1] + timedelta(days=lag + 1))
        if key in fixed:
            a, b = day(task.get("start_date")), day(task.get("due_date"))
            if task.get("locked") and task["status"] != "completed" and b is None:
                conflicts.append(f"锁定任务日期未知：{task['title']}")
            if (a and a < start or b and b < start) and task["status"] != "completed":
                conflicts.append(f"锁定/保留任务依赖冲突：{task['title']}")
            earliest[key] = (a, b)
        else:
            a = work(start)
            earliest[key] = (a, shift(a, task["duration_days"] - 1))
    for key in reversed(order):
        task = nodes[key]
        app = applications.get(task.get("application_id"), {})
        deadline = offering_deadline(offerings.get(app.get("offering_id"), {}))
        limits = [deadline] if deadline else []
        if task.get("locked") and task.get("due_date"):
            limits.append(day(task["due_date"]))
        for nxt, lag in outgoing[key]:
            if latest.get(nxt):
                limits.append(latest[nxt] - timedelta(days=lag + 1))
        if limits:
            end = work(min(limits), -1)
            latest[key] = shift(end, -(task["duration_days"] - 1))
            if earliest[key][1] and earliest[key][1] > end and task["status"] != "completed":
                conflicts.append(f"截止日期前无可行窗口：{task['title']}")
        else:
            latest[key] = None
    items = []
    for key in order:
        task = nodes[key]
        a, b = earliest[key]
        app = applications.get(task.get("application_id"), {})
        if key in affected and task["status"] != "completed" and not offering_deadline(offerings.get(app.get("offering_id"), {})):
            conflicts.append(f"截止日期未知，需确认：{task['title']}")
        snapshot_dates = {"start_date": a.isoformat() if a else None, "due_date": b.isoformat() if b else None,
                          "date_certainty": task.get("date_certainty", "unknown") if key in fixed else "estimated"}
        items.append({"task_id": task.get("id"), "proposal_key": key,
                      "input_task_version": task.get("version"), "task_spec": task,
                      "schedule_snapshot": snapshot_dates})
    return {"input_revision": snapshot["revision"], "items": items, "conflicts": list(dict.fromkeys(conflicts)),
            "assumptions": ["建议工期为预估值；允许不同任务并行，不代表每日容量最优", "仅使用已记录截止日期；未知期限须确认"],
            "dependencies": snapshot["dependencies"]}


def impact(event: dict) -> str:
    if event["type"] == "task.completed":
        return "review"
    if event["type"] in {"plan.activated", "plan.drafted", "plan.rejected", "source.created", "notification.read"}:
        return "no_op"
    fields = set(event["payload"].get("changed_fields", []))
    if event["type"].startswith(("program.", "offering.created")):
        return "no_op"
    if event["type"] == "offering.updated" and fields <= {"gre_policy", "requirements_json"}:
        return "review"
    if event["type"] == "task.updated" and fields <= {"title", "evidence", "show_in_calendar"}:
        return "no_op"
    if event["type"] == "task.updated" and fields <= {"status", "evidence"}:
        return "review"
    if event["type"].startswith("material."):
        return "review"
    if event["type"].startswith(("profile.", "preference.")):
        return "review"
    return "replan"
