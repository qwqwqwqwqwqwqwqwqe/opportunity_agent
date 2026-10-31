"""Build complete, revision-fenced project updates from explicit chat patches.

The existing project.save service accepts a complete form and archives omitted
requirements. Chat patches therefore merge against the owned snapshot first.
"""
from __future__ import annotations

import re
from datetime import date as CalendarDate, datetime
from typing import Literal

from pydantic import Field
from .contracts import Contract, RequirementCriteria, ScoreAlternative
from .workspace import ProjectForm


class CriteriaPatch(Contract):
    profile_field: Literal['toefl_score','ielts_score','gre_score','gpa'] | None = None
    minimum: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    required_status: Literal['ready','submitted'] | None = None
    valid_until: CalendarDate | None = None
    requires_validity: bool | None = None
    notes: str | None = None
    any_of: list[ScoreAlternative] | None = Field(default=None, max_length=3)


class MaterialPatch(Contract):
    id: str | None = None
    slot_key: str | None = Field(default=None, min_length=1, max_length=160)
    status: Literal['missing','invited','draft','ready','submitted'] | None = None


class RequirementPatch(Contract):
    id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    kind: Literal['document','recommendation','score','exam','other'] | None = None
    obligation: Literal['required','optional','unknown'] | None = None
    required_count: int | None = Field(default=None, ge=1, le=20, strict=True)
    criteria: CriteriaPatch | None = None
    status: Literal['missing','invited','draft','ready','submitted'] | None = None
    materials: list[MaterialPatch] = Field(default_factory=list, max_length=20)


class ProjectPatch(Contract):
    university: str | None = Field(default=None, min_length=1, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    degree: str | None = Field(default=None, max_length=80)
    home_unit: str | None = Field(default=None, max_length=200)
    cycle: str | None = Field(default=None, min_length=1, max_length=40)
    status: Literal['considering','preparing','submitted','offered','rejected','withdrawn'] | None = None
    deadline_date: CalendarDate | None = None
    deadline_at: datetime | None = None
    deadline_text: str | None = Field(default=None, max_length=100)
    clear_deadline: bool = False
    timezone: str | None = None
    gre_policy: Literal['unknown','required','optional','not_required','not_accepted'] | None = None
    source_url: str | None = Field(default=None, max_length=2048)
    notes: str | None = Field(default=None, max_length=10000)
    requirements: list[RequirementPatch] | None = Field(default=None, max_length=30)
    requirements_mode: Literal['merge','replace'] = 'merge'


class NeedsProjectInput(ValueError):
    pass


def title_key(value):
    value = re.sub(r'[\s_\-·（）()]', '', value or '').casefold()
    aliases = {
        'sop':{'sop','sop最终版','个人陈述','statementofpurpose'},
        'cv':{'cv','简历','个人简历','resume','résumé','简历cv','cv简历','简历resume'},
        'transcript':{'transcript','transcripts','成绩单'},
        'recommendation':{'推荐信','推荐函','recommendation','recommendationletters','lettersofrecommendation'},
        'toefl':{'toefl','托福','托福成绩'}, 'ielts':{'ielts','雅思','雅思成绩'},
        'gre':{'gre','gre成绩'},
    }
    return next((key for key, names in aliases.items() if value in names), value)


def _target(project, snapshot):
    apps = snapshot.get('applications', [])
    programs = {p['id']:p for p in snapshot.get('programs', [])}
    offerings = {o['id']:o for o in snapshot.get('offerings', [])}
    def matches(app):
        program = programs.get(offerings.get(app.get('offering_id'), {}).get('program_id'), {})
        return ((not project.university or project.university.strip().casefold() == app['university'].strip().casefold())
                and (not project.name or project.name.strip().casefold() in
                     [str(x).strip().casefold() for x in [app['program'], *program.get('aliases', [])]])
                and (not project.cycle or project.cycle.strip().casefold() == app.get('intake','').strip().casefold()))
    if project.application_id:
        app = next((a for a in apps if a['id'] == project.application_id), None)
        if not app:
            raise ValueError('找不到属于你的目标申请项目')
        if project.cycle and re.sub(r'\s+','',project.cycle).casefold() != re.sub(r'\s+','',app.get('intake','')).casefold():
            raise NeedsProjectInput('所选项目与申请周期不一致，请确认要修改哪一个周期。')
        siblings = [a for a in apps if a['university'].casefold() == app['university'].casefold()
                    and a['program'].casefold() == app['program'].casefold()]
        if len(siblings) > 1 and not project.cycle:
            raise NeedsProjectInput('同一个项目有多个申请周期，请说明要修改哪一个周期。')
        return app
    if not (project.university or project.name):
        raise NeedsProjectInput('请说明要修改的学校、项目名称和申请周期。')
    candidates = [a for a in apps if matches(a)]
    if len(candidates) != 1:
        raise NeedsProjectInput('没有唯一匹配的已有项目，请说明学校、项目和申请周期；不会新建替代项目。')
    return candidates[0]


def _form(snapshot, app):
    offering = next((o for o in snapshot.get('offerings', []) if o['id'] == app.get('offering_id')), {})
    program = next((p for p in snapshot.get('programs', []) if p['id'] == offering.get('program_id')), {})
    meta = offering.get('requirements_json') or {}
    requirements = []
    for req in snapshot.get('requirements', []):
        if req['application_id'] != app['id'] or req.get('archived'):
            continue
        requirements.append({k:req.get(k) for k in ('id','title','kind','obligation','required_count','criteria')})
        requirements[-1]['title'] = req.get('title') or req['key']
        requirements[-1]['materials'] = [{k:item[k] for k in ('id','slot_key','status')}
            for item in snapshot.get('materials', []) if item['requirement_id'] == req['id'] and not item.get('archived')]
    return {'request_id':'chat-update', 'expected_revision':snapshot['revision'],
            'university':program.get('university', app['university']), 'name':program.get('name', app['program']),
            'degree':program.get('degree',''), 'home_unit':program.get('home_unit',''), 'cycle':offering.get('cycle') or app.get('intake',''),
            'status':app['status'], 'deadline_date':offering.get('deadline_date'), 'deadline_at':offering.get('deadline_at'),
            'timezone':offering.get('timezone','Asia/Shanghai'), 'date_precision':offering.get('date_precision','unknown'),
            'gre_policy':offering.get('gre_policy','unknown'), 'source_url':meta.get('source_url',''), 'notes':meta.get('notes',''),
            'requirements':requirements}


def _requirement(patch, existing, snapshot, app_id):
    from copy import deepcopy
    if patch.id:
        found = [r for r in existing if r['id'] == patch.id]
        if not found:
            raise ValueError('材料要求不属于当前项目或已失效')
    else:
        if not patch.title:
            raise NeedsProjectInput('请说明要修改哪项材料要求。')
        key = title_key(patch.title)
        field = {'toefl':'toefl_score','ielts':'ielts_score','gre':'gre_score'}.get(key)
        found = [r for r in existing if title_key(r['title']) == key or field and
            (r['criteria'].get('profile_field') == field or any(a.get('profile_field') == field for a in r['criteria'].get('any_of',[])))]
    if len(found) > 1:
        raise NeedsProjectInput(f'存在多项同名或相关要求“{patch.title}”，请明确要修改哪一项。')
    row = deepcopy(found[0]) if found else {'id':None, 'title':patch.title, 'kind':'document','obligation':'required',
                                          'required_count':1,'criteria':{},'materials':[]}
    for field in ('title','kind','obligation','required_count'):
        if getattr(patch, field) is not None:
            row[field] = getattr(patch, field)
    if patch.criteria is not None:
        row['criteria'] = {**row['criteria'], **patch.criteria.model_dump(mode='json', exclude_none=True)}
    row['criteria'] = RequirementCriteria.model_validate(row['criteria']).model_dump(mode='json')
    materials = row['materials']
    count = row['required_count']
    if count is not None and count < len(materials):
        raise NeedsProjectInput(f'“{row["title"]}”已有 {len(materials)} 份材料，数量减少前请明确应保留哪些记录。')
    if count is not None and (patch.status is not None or materials or patch.materials):
        while len(materials) < count:
            used = {s['slot_key'] for s in materials}
            index = 1
            while f'材料{index}' in used:
                index += 1
            materials.append({'id':None, 'slot_key':f'材料{index}', 'status':'missing'})
    if patch.status is not None:
        if count is None:
            raise NeedsProjectInput(f'请先说明“{row["title"]}”需要几份，才能设置全部材料的状态。')
        for material in materials:
            # A readiness statement does not retract an earlier submission.
            if not (patch.status == 'ready' and material['status'] == 'submitted'):
                material['status'] = patch.status
    for update in patch.materials:
        if update.id:
            matched = next((s for s in materials if s.get('id') == update.id), None)
            if not matched:
                raise ValueError('材料记录不属于当前要求')
        else:
            matched = next((s for s in materials if s['slot_key'] == update.slot_key), None)
            if not matched:
                if not update.slot_key:
                    raise NeedsProjectInput('请说明具体材料或推荐人名称。')
                placeholder = next((s for s in materials if s.get('id') is None and re.fullmatch(r'材料\d+', s['slot_key'])), None)
                if placeholder:
                    placeholder['slot_key'] = update.slot_key
                    matched = placeholder
                elif count is not None and len(materials) >= count:
                    raise NeedsProjectInput(f'“{row["title"]}”的材料槽位已满，请说明要更新哪一份。')
                else:
                    matched = {'id':None,'slot_key':update.slot_key,'status':'missing'}
                    materials.append(matched)
        if update.status is not None:
            matched['status'] = update.status
    return row


def build_project_update(project, snapshot, request_id):
    app = _target(project, snapshot)
    if project.changes is None:
        raise NeedsProjectInput('请说明这个项目要修改的字段、材料要求或材料状态。')
    patch = project.changes
    if patch.deadline_text and not (patch.deadline_date or patch.deadline_at):
        raise NeedsProjectInput(f'截止日期“{patch.deadline_text}”是哪一年？')
    form = _form(snapshot, app)
    fields = patch.model_dump(mode='json', exclude_none=True, exclude={'requirements','requirements_mode','clear_deadline','deadline_text'})
    if not fields and patch.requirements is None and not patch.clear_deadline:
        raise NeedsProjectInput('没有识别到具体修改内容，请说明要更新什么。')
    form.update(fields)
    form['request_id'] = request_id
    if patch.clear_deadline:
        if patch.deadline_date or patch.deadline_at:
            raise ValueError('清除截止日期不能同时设置新日期')
        form.update(deadline_date=None, deadline_at=None, date_precision='unknown')
    elif patch.deadline_date:
        form.update(deadline_date=str(patch.deadline_date), deadline_at=None, date_precision='date')
    elif patch.deadline_at:
        form.update(deadline_at=patch.deadline_at.isoformat(), deadline_date=None, date_precision='instant')
    if patch.requirements is not None:
        existing = form['requirements']
        merged = list(existing) if patch.requirements_mode == 'merge' else []
        touched = set()
        for entry in patch.requirements:
            row = _requirement(entry, existing, snapshot, app['id'])
            identity = row['id'] or title_key(row['title'])
            if identity in touched:
                raise ValueError('同一项材料要求不能重复修改')
            touched.add(identity)
            position = next((i for i,r in enumerate(merged) if row['id'] and r['id'] == row['id']), None)
            if position is None:
                merged.append(row)
            else:
                merged[position] = row
        form['requirements'] = merged
    if patch.gre_policy == 'optional':
        for row in form['requirements']:
            if title_key(row['title']) == 'gre' or row['criteria'].get('profile_field') == 'gre_score':
                row['obligation'] = 'optional'
    validated = ProjectForm.model_validate(form)
    lines = [f'- 修改项目：{validated.university} · {validated.name}（{validated.cycle}）']
    names = {'university':'学校','name':'项目名','degree':'学位','home_unit':'院系','cycle':'周期','status':'申请状态',
             'deadline_date':'截止日期','deadline_at':'截止时刻','timezone':'时区','gre_policy':'GRE 政策','source_url':'来源链接','notes':'备注'}
    for field, value in fields.items():
        lines.append(f'  {names.get(field, field)}：{value}')
    if patch.clear_deadline:
        lines.append('  清除截止日期')
    status_names = {'ready':'已准备','submitted':'已提交','missing':'未准备','draft':'草稿','invited':'已邀请'}
    if patch.requirements is not None:
        lines.append('  材料要求：' + ('仅保留本次清单，其他要求将归档' if patch.requirements_mode=='replace' else '合并修改，未提及的要求保留'))
        for row in validated.requirements:
            state_counts = {}
            for slot in row.materials:
                name = status_names[slot.status]
                state_counts[name] = state_counts.get(name,0) + 1
            states = '、'.join(f'{name} {n} 份' for name,n in state_counts.items()) or '状态未记录'
            lines.append(f'  - {row.title}｜{"可选" if row.obligation=="optional" else "必需" if row.obligation=="required" else "是否必需待确认"}｜{row.required_count or "数量未知"} 份｜{states}')
            if row.kind == 'score':
                score_names = {'toefl_score':'托福','ielts_score':'雅思','gre_score':'GRE','gpa':'GPA'}
                criteria = row.criteria
                scores = criteria.any_of or ([criteria] if criteria.profile_field else [])
                thresholds = [f'{score_names[s.profile_field]} ≥ {s.minimum}' for s in scores if s.minimum is not None]
                lines.append('    最低分：' + (' 或 '.join(thresholds) if thresholds else '未记录，不能据此判断成绩达标'))
            if len(row.materials) > 1:
                lines.append('    ' + '；'.join(f'{s.slot_key}：{status_names[s.status]}' for s in row.materials))
        if patch.requirements_mode == 'replace':
            retained = {r.id for r in validated.requirements if r.id}
            removed = [r['title'] for r in existing if r['id'] not in retained]
            if removed:
                lines.append('  将归档的要求：' + '、'.join(removed))
        lines.append('  成绩材料已准备不代表已核验最低分要求；不会修改个人成绩或待办完成状态。')
    return {'type':'project.save','ref':project.ref,'target_id':app['id'],'payload':validated.model_dump(mode='json')}, lines
