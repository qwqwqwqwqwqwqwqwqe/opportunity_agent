"""Small, explicitly labelled intake dataset and reproducible evaluation CLI.

Seed labels are authored examples, NOT human-reviewed Gold. The acceptance
gate refuses pending labels unless --allow-unreviewed is explicitly selected.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import time
from .orchestrator import IntakeAgent
from ..llm_client import LLMClient


def seed_snapshot():
    return {"revision": 1, "today": "2026-10-19", "availability": {"weekdays": [0,1,2,3,4]},
        "profile": {}, "profile_version": 1, "current_plan_id": None,
        "programs": [{"id":"p1","version":1,"university":"A大学","name":"MSCS","degree":"MS","home_unit":"","aliases":[]}],
        "offerings": [{"id":"o1","version":1,"program_id":"p1","cycle":"2027 Fall","deadline_date":"2026-12-01",
            "deadline_at":None,"timezone":"Asia/Shanghai","date_precision":"date","gre_policy":"unknown","requirements_json":{}}],
        "applications": [{"id":"a1","version":1,"offering_id":"o1","priority":0,"status":"preparing"}],
        "requirements": [{"id":"r1","version":1,"application_id":"a1","key":"推荐信","kind":"recommendation",
            "obligation":"required","required_count":3,"criteria":{},"source_id":None}],
        "materials": [{"id":"m1","version":1,"requirement_id":"r1","slot_key":"张老师","status":"invited","evidence_ref":None}],
        "tasks": [{"id":"t1","version":1,"application_id":"a1","requirement_id":None,"stable_key":"cv","title":"整理简历",
            "status":"planned","start_date":None,"due_date":None,"date_certainty":"unknown","duration_days":1,"locked":False,"evidence":""}],
        "dependencies": [], "calendar_events": []}


def seed_cases():
    profile = [
        ("我的托福是105分", "toefl_score", 105), ("我的雅思是7.5分", "ielts_score", 7.5),
        ("我的GRE是325分", "gre_score", 325), ("我的专业是计算机科学", "major", "计算机科学"),
        ("我2027年毕业", "graduation_year", 2027), ("我的托福改成108分", "toefl_score", 108),
        ("我的雅思改成8分", "ielts_score", 8), ("我的GRE更新为330分", "gre_score", 330),
        ("我2028年毕业", "graduation_year", 2028), ("我的专业是软件工程", "major", "软件工程"),
        ("我的托福是100分", "toefl_score", 100), ("我的雅思是7分", "ielts_score", 7),
        ("我的GRE是320分", "gre_score", 320), ("我的托福是110分", "toefl_score", 110),
        ("我的雅思是6.5分", "ielts_score", 6.5), ("我的GRE是315分", "gre_score", 315),
        ("我的托福改成102分", "toefl_score", 102), ("我的雅思改成7分", "ielts_score", 7),
        ("我2029年毕业", "graduation_year", 2029), ("我的GRE是328分", "gre_score", 328)]
    domain = [
        ("把A大学MSCS 2027 Fall截止日期改为2026年12月10日", "offering.update", "o1", "deadline_date", "2026-12-10"),
        ("记录A大学MSCS 2027 Fall不要求GRE", "offering.update", "o1", "gre_policy", "not_required"),
        ("记录A大学MSCS 2027 Fall的GRE是可选的", "offering.update", "o1", "gre_policy", "optional"),
        ("A大学MSCS 2027 Fall要求GRE，请更新", "offering.update", "o1", "gre_policy", "required"),
        ("把A大学MSCS申请状态改为已提交", "application.update", "a1", "status", "submitted"),
        ("把A大学MSCS申请优先级改为10", "application.update", "a1", "priority", 10),
        ("把A大学MSCS申请改为已撤回", "application.update", "a1", "status", "withdrawn"),
        ("更新：A大学MSCS张老师的推荐信已经提交了", "material.update", "m1", "status", "submitted"),
        ("更新：A大学MSCS张老师的推荐信已经准备好了", "material.update", "m1", "status", "ready"),
        ("更新：A大学MSCS张老师的推荐信仍然缺失", "material.update", "m1", "status", "missing"),
        ("我已经完成了整理简历任务，请记录", "task.update", "t1", "status", "completed"),
        ("把整理简历任务改为进行中", "task.update", "t1", "status", "in_progress"),
        ("把整理简历任务日期改为2026年11月1日", "task.update", "t1", "due_date", "2026-11-01"),
        ("修改整理简历任务，锁定它的日期", "task.update", "t1", "locked", True),
        ("把整理简历的预估工期改为3个工作日", "task.update", "t1", "duration_days", 3),
        ("把A大学MSCS推荐信所需数量改为2份", "requirement.update", "r1", "required_count", 2),
        ("记录A大学MSCS推荐信是可选材料", "requirement.update", "r1", "obligation", "optional"),
        ("把A大学MSCS推荐信要求改为必需", "requirement.update", "r1", "obligation", "required"),
        ("把A大学MSCS项目名称修改为MSc Computer Science", "program.update", "p1", "name", "MSc Computer Science"),
        ("把A大学MSCS的学位更新为MSc", "program.update", "p1", "degree", "MSc")]
    negative = ["你好", "谢谢你", "我朋友托福是110分", "假如我的托福是110分，够吗？", "如果张老师提交了推荐信就好了",
                "请告诉我推荐信提交是什么意思", "材料没交会怎么样？", "别修改任何信息", "整理简历完成了吗？", "听说A大学截止日期会延期，是真的吗？"]
    rows = []
    for message, field, value in profile:
        rows.append({"group":f"profile:{field}","message":message,"expected":[["profile.change",None,field,value]]})
    for message, kind, target, field, value in domain:
        rows.append({"group":f"domain:{kind}:{field}","message":message,"expected":[[kind,target,field,value]]})
    for message in negative:
        rows.append({"group":"non_assertion","message":message,"expected":[]})
    for index, row in enumerate(rows, 1):
        row.update(id=f"intake-{index:03}", human_review="pending", label_source="authored_seed",
                   split="heldout" if int(hashlib.sha256(row["group"].encode()).hexdigest()[:8],16)%4==0 else "development")
    return {"schema_version":3,"label_status":"pending_human_review","snapshot":seed_snapshot(),"cases":rows}


def tokens(result):
    values=[]
    ignored={"date_precision","date_certainty","timezone","source_id","evidence_ref","evidence"}
    for command in result.proposed_commands:
        if command.type=="profile.change":
            values.extend([command.type,None,fact["field"],fact.get("normalized_value",fact.get("raw_value"))]
                          for fact in command.payload.get("facts",[]))
        else:
            values.extend([command.type,command.target_id,key,value] for key,value in command.payload.items() if key not in ignored)
    return {json.dumps(value,sort_keys=True,ensure_ascii=False) for value in values}


def evaluate(dataset, client, split="development", allow_unreviewed=False):
    rows=[row for row in dataset["cases"] if row.get("split","development")==split]
    if not rows:
        raise ValueError("selected split contains no cases")
    reviewed=all(row.get("human_review")=="approved" for row in rows)
    if not reviewed and not allow_unreviewed:
        raise ValueError("labels require independent human approval; use --allow-unreviewed only for development diagnostics")
    agent=IntakeAgent(client)
    true_positive=false_positive=false_negative=errors=0
    details=[]
    started=time.monotonic()
    for row in rows:
        expected={json.dumps(value,sort_keys=True,ensure_ascii=False) for value in row["expected"]}
        try:
            result=agent.execute(row["message"],row.get("snapshot",dataset["snapshot"]),{})
            actual=tokens(result)
            error=None
        except Exception as exc:
            actual=set()
            error=type(exc).__name__
            errors+=1
        true_positive+=len(expected&actual)
        false_positive+=len(actual-expected)
        false_negative+=len(expected-actual)
        details.append({"id":row["id"],"correct":expected==actual and not error,"error":error,
                        "missing":sorted(expected-actual),"unexpected":sorted(actual-expected)})
    precision=true_positive/(true_positive+false_positive) if true_positive+false_positive else 0
    recall=true_positive/(true_positive+false_negative) if true_positive+false_negative else 0
    f1=2*precision*recall/(precision+recall) if precision+recall else 0
    return {"schema_version":3,"model":client.model if client.enabled else "rules_only","split":split,"cases":len(rows),
            "human_reviewed":reviewed,"acceptance_eligible":reviewed and client.enabled,"precision":precision,"recall":recall,"f1":f1,
            "errors":errors,"elapsed_seconds":round(time.monotonic()-started,3),
            "dataset_hash":hashlib.sha256(json.dumps(dataset,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
            "limitations":"无人工审核标签时仅供开发诊断；模拟/规则结果不能证明真实模型质量。费用需结合提供商账单。",
            "details":details}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--dataset",type=Path)
    parser.add_argument("--export-seed",type=Path)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--split",choices=["development","heldout"],default="development")
    parser.add_argument("--mode",choices=["rules","real"],default="rules")
    parser.add_argument("--allow-unreviewed",action="store_true")
    args=parser.parse_args()
    if args.export_seed:
        args.export_seed.write_text(json.dumps(seed_cases(),ensure_ascii=False,indent=2),encoding="utf-8")
        return
    dataset=json.loads(args.dataset.read_text(encoding="utf-8")) if args.dataset else seed_cases()
    client=LLMClient(timeout_seconds=25,retries=0)
    if args.mode=="rules":
        client.api_key=""
    elif not client.enabled:
        parser.error("real mode requires configured model credentials")
    try:
        report=evaluate(dataset,client,args.split,args.allow_unreviewed)
    except ValueError as exc:
        parser.error(str(exc))
    rendered=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        args.output.write_text(rendered,encoding="utf-8")
    else:
        print(rendered)


if __name__=="__main__":
    main()
