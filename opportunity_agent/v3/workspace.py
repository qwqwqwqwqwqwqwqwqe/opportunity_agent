"""Application form orchestration and read-only preparation projections."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
from typing import Literal
from pydantic import Field, model_validator
from sqlalchemy import select
from . import models as m
from ..v2.db.models import Application
from .contracts import Contract, Command, OfferingData, RequirementCriteria
from .engine import offering_deadline, score_check
from .service import WorkflowError, row_out


class MaterialEntry(Contract):
    id: str | None = None
    slot_key: str = Field(min_length=1, max_length=160)
    status: Literal["missing", "invited", "draft", "ready", "submitted"] = "missing"


class RequirementEntry(Contract):
    id: str | None = None
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["document", "recommendation", "score", "exam", "other"] = "document"
    obligation: Literal["required", "optional", "unknown"] = "required"
    required_count: int | None = Field(default=1, ge=1, le=20, strict=True)
    criteria: RequirementCriteria = Field(default_factory=RequirementCriteria)
    materials: list[MaterialEntry] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def slots(self):
        if len({item.slot_key for item in self.materials}) != len(self.materials):
            raise ValueError("同一要求中的材料名称不能重复")
        if self.required_count is not None and len(self.materials) > self.required_count:
            raise ValueError("材料条数不能超过填写的所需数量")
        return self


class ProjectForm(Contract):
    request_id: str = Field(min_length=1, max_length=140)
    expected_revision: int | None = Field(default=None, ge=0)
    university: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    degree: str = Field(default="", max_length=80)
    home_unit: str = Field(default="", max_length=200)
    cycle: str = Field(min_length=1, max_length=40)
    status: Literal["considering", "preparing", "submitted", "offered", "rejected", "withdrawn"] = "preparing"
    deadline_date: str | None = None
    deadline_at: str | None = None
    timezone: str = "Asia/Shanghai"
    date_precision: Literal["unknown", "date", "instant"] = "unknown"
    gre_policy: Literal["unknown", "required", "optional", "not_required", "not_accepted"] = "unknown"
    requirements: list[RequirementEntry] = Field(default_factory=list, max_length=30)
    source_url: str = Field(default="", max_length=2048)
    notes: str = Field(default="", max_length=10000)

    @model_validator(mode="after")
    def validate_form(self):
        if any(not value.strip() for value in (self.university, self.name, self.cycle)):
            raise ValueError("学校、项目和申请周期不能为空")
        ids = [r.id for r in self.requirements if r.id]
        if len(ids) != len(set(ids)):
            raise ValueError("材料要求不能重复引用")
        if self.source_url and not self.source_url.startswith(("https://", "http://")):
            raise ValueError("来源链接需以 https:// 或 http:// 开头")
        OfferingData.model_validate({"program_id": "validation", **self.offering_payload()})
        return self

    def offering_payload(self):
        return {key: getattr(self, key) for key in ("cycle", "deadline_date", "deadline_at", "timezone", "date_precision", "gre_policy")}


async def save_project(service, user_id, command, state, source, run_id):
    form = ProjectForm.model_validate(command.payload)
    application = None
    offering = program = None
    if command.target_id:
        application = await service.owned(Application, command.target_id, user_id)
        if form.expected_revision != state.planning_revision:
            raise WorkflowError("项目数据已变化，请刷新后重新确认", 409)
        if application.offering_id:
            offering = await service.owned(m.Offering, application.offering_id, user_id)
            program = await service.owned(m.Program, offering.program_id, user_id)
    elif any(r.id or any(slot.id for slot in r.materials) for r in form.requirements):
        raise WorkflowError("新增项目不能引用其他项目的材料")
    if program is None:
        programs = (await service.session.scalars(select(m.Program).where(m.Program.user_id == user_id))).all()
        identity = lambda p: (p.university.strip().casefold(), p.name.strip().casefold(), p.degree.strip().casefold(), p.home_unit.strip().casefold())
        program = next((p for p in programs if identity(p) == tuple(v.strip().casefold() for v in (form.university, form.name, form.degree, form.home_unit))), None)
        offering = await service.session.scalar(select(m.Offering).where(m.Offering.user_id == user_id,
            m.Offering.program_id == program.id, m.Offering.cycle == form.cycle)) if program else None
        duplicate = await service.session.scalar(select(Application.id).where(Application.user_id == user_id,
                       Application.offering_id == offering.id)) if offering else None
        if duplicate and (not application or duplicate != application.id):
            raise WorkflowError("该项目和申请周期已经添加", 409)

    async def apply(kind, payload, row=None, refs=None):
        return await service._apply(user_id, Command(type=f"{kind}.{'update' if row else 'create'}", target_id=row.id if row else None,
            expected_version=row.version if row else None, payload=payload, evidence_refs=refs or []), state, source, run_id)

    source_id = None
    if form.source_url or form.notes:
        result = await service._apply(user_id, Command(type="source.create", payload={"kind": "form",
            "excerpt": "\n".join(filter(None, [form.source_url, form.notes]))}), state, source, run_id)
        source_id = result["id"]
    refs = [source_id] if source_id else []
    result = await apply("program", {"university": form.university.strip(), "name": form.name.strip(),
                                    "degree": form.degree, "home_unit": form.home_unit}, program)
    program = await service.owned(m.Program, result["id"], user_id)
    result = await apply("offering", {"program_id": program.id, **form.offering_payload(),
        "requirements_json": {**(offering.requirements_json if offering else {}), "source_url": form.source_url, "notes": form.notes}}, offering)
    offering = await service.owned(m.Offering, result["id"], user_id)
    result = await apply("application", {"offering_id": offering.id, "status": form.status}, application)
    application = await service.owned(Application, result["id"], user_id)
    retained = set()
    for entry in form.requirements:
        requirement = await service.owned(m.Requirement, entry.id, user_id) if entry.id else None
        if requirement and requirement.application_id != application.id:
            raise WorkflowError("材料要求不属于当前项目", 404)
        result = await apply("requirement", {"application_id": application.id, "key": requirement.key if requirement else "req:" + uuid4().hex,
            "title": entry.title.strip(), "kind": entry.kind, "obligation": entry.obligation, "required_count": entry.required_count,
            "criteria": entry.criteria.model_dump(mode="json"), "archived": False,
            "source_id": source_id or (requirement.source_id if requirement else None)}, requirement, refs)
        requirement = await service.owned(m.Requirement, result["id"], user_id)
        retained.add(requirement.id)
        slots = set()
        for slot in entry.materials:
            material = await service.owned(m.Material, slot.id, user_id) if slot.id else None
            if material is None:
                material = await service.session.scalar(select(m.Material).where(m.Material.user_id == user_id,
                    m.Material.requirement_id == requirement.id, m.Material.slot_key == slot.slot_key.strip()))
            if material and material.requirement_id != requirement.id:
                raise WorkflowError("材料不属于当前要求", 404)
            result = await apply("material", {"requirement_id": requirement.id,
                "slot_key": slot.slot_key.strip(), "status": slot.status,
                "archived": False, "evidence_ref": source_id or (material.evidence_ref if material else None)}, material, refs)
            slots.add(result["id"])
        previous = (await service.session.scalars(select(m.Material).where(m.Material.requirement_id == requirement.id, m.Material.user_id == user_id))).all()
        for material in previous:
            if material.id not in slots and not material.archived:
                await apply("material", {"archived": True}, material)
    previous = (await service.session.scalars(select(m.Requirement).where(m.Requirement.application_id == application.id,
                                    m.Requirement.user_id == user_id))).all()
    for requirement in previous:
        if requirement.id not in retained and not requirement.archived:
            await apply("requirement", {"archived": True}, requirement)
    await service.session.flush()
    return {"type": "project", "id": application.id, "application_id": application.id, "version": application.version}


def project_cards(snapshot):
    offerings = {o["id"]: o for o in snapshot["offerings"]}
    programs = {p["id"]: p for p in snapshot["programs"]}
    cards = []
    for app in snapshot["applications"]:
        offering = offerings.get(app.get("offering_id"), {})
        program = programs.get(offering.get("program_id"), {})
        rows, known, complete, unknown = [], 0, 0, 0
        for req in snapshot["requirements"]:
            if req["application_id"] != app["id"] or req.get("archived") or req["obligation"] == "optional":
                continue
            count = req.get("required_count")
            materials = [r for r in snapshot["materials"] if r["requirement_id"] == req["id"] and not r.get("archived")]
            prepared = sum(r["status"] in {"ready", "submitted"} for r in materials)
            uncertain = req["obligation"] == "unknown" or (req["kind"] != "score" and count is None)
            done = not uncertain and ((req["kind"] == "score" and score_check(req.get("criteria") or {}, snapshot)[0] is True)
                                     or count is not None and prepared >= count)
            if uncertain:
                unknown += 1
            else:
                known += 1
                complete += int(done)
            rows.append({"id": req["id"], "title": req.get("title") or req["key"], "kind": req["kind"],
                         "complete": bool(done), "unknown": uncertain, "prepared": prepared, "required": count})
        deadline = offering_deadline(offering)
        cards.append({"id": app["id"], "university": program.get("university", app["university"]),
            "name": program.get("name", app["program"]), "cycle": offering.get("cycle") or app["intake"],
            "status": app["status"], "deadline": str(deadline) if deadline else None, "requirements": rows,
            "progress": {"completed": complete, "total": known, "unknown": unknown,
                         "percent": round(100 * complete / known) if known else 0,
                         "fully_prepared": bool(known) and complete == known and unknown == 0}})
    return sorted(cards, key=lambda row: (row["deadline"] or "9999-12-31", row["university"], row["name"], row["id"]))


async def project_form(service, user_id, application_id):
    app = await service.owned(Application, application_id, user_id)
    offering = await service.owned(m.Offering, app.offering_id, user_id) if app.offering_id else None
    program = await service.owned(m.Program, offering.program_id, user_id) if offering else None
    state = await service.state(user_id)
    requirements = []
    for req in (await service.session.scalars(select(m.Requirement).where(m.Requirement.application_id == app.id,
                   m.Requirement.user_id == user_id, m.Requirement.archived.is_(False)).order_by(m.Requirement.created_at, m.Requirement.id))).all():
        materials = (await service.session.scalars(select(m.Material).where(m.Material.requirement_id == req.id,
            m.Material.user_id == user_id, m.Material.archived.is_(False)).order_by(m.Material.created_at, m.Material.id))).all()
        requirements.append({"id": req.id, "title": req.title or req.key, "kind": req.kind,
            "obligation": req.obligation, "required_count": req.required_count, "criteria": req.criteria,
            "materials": [{"id": item.id, "slot_key": item.slot_key, "status": item.status} for item in materials]})
    meta = offering.requirements_json if offering else {}
    return {"application_id": app.id, "expected_revision": state.planning_revision if state else 0,
        "university": program.university if program else app.university, "name": program.name if program else app.program,
        "degree": program.degree if program else "", "home_unit": program.home_unit if program else "", "cycle": app.intake,
        "status": app.status, **({key: row_out(offering)[key] for key in ("deadline_date", "deadline_at", "timezone", "date_precision", "gre_policy")} if offering else {}),
        "source_url": meta.get("source_url", ""), "notes": meta.get("notes", ""), "requirements": requirements}
