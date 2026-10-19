"""Research adapter for the private, confirmed project pool."""
import json
from ..agents.contracts import ResearchResult, ResearchFinding, Evidence
from .schemas import Query
from .service import recommend


async def execute_library(session, request):
    mode = 'project_match' if any(t in request.message.casefold() for t in ['推荐','匹配','recommend','match']) else 'library_qa'
    supplied=getattr(request,'workspace_query',{})
    report = await recommend(session, request.user_id, Query.model_validate(supplied) if supplied else Query(query=request.message))
    result = ResearchResult(task_id=request.request_id, route='rag',
        status='complete' if report['results'] else 'no_results',
        diagnostics={'workspace': True, 'task_type': mode, 'recommendation': report,
            'policy': 'Only user-confirmed private projects. Unknown fields stay unknown. Not official verification or admission probability.'})
    for item in report['results']:
        pid = item['project_id']
        confirmed = dict(item['confirmed_data'])
        confirmed.pop('provenance', None)
        confirmed.pop('field_origins', None)
        for key, req in item['requirements'].items():
            if req['value'] is None:
                confirmed[key] = '待确认，不能作为已核实要求'
        if 'timezone' not in item['confirmations'] and isinstance(confirmed.get('deadlines'),list):
            confirmed['deadlines']=[{**d,'time':'','timezone':'未确认，不推断具体时间'} for d in confirmed['deadlines']]
        statement = {'project_id': pid, 'project_version': item['project_version'], 'confirmed_data': confirmed,
            'matching_experiences': sorted({e['experience_id'] for e in item['evidence']}),
            'scope': '用户确认的项目资料，不代表官网独立核验', 'matching_evidence': item['evidence'][:3]}
        text = json.dumps(statement, ensure_ascii=False)
        ev = Evidence(source_id=pid, title=confirmed['university']+' · '+confirmed['program'],
            excerpt=text, authority='trusted', program_match='exact', relevance_method='user_confirmed_library',
            relevance_passed=True, extraction_provenance={'user_confirmed': True, 'project_version': item['project_version'],
                'source_ids':[e['source_id'] for e in item['evidence'] if e.get('source_id')],
                'workspace_path':'/api/v1/project-library/'+pid})
        result.evidence.append(ev)
        result.findings.append(ResearchFinding(finding_id=pid, program_id=pid, topic=mode,
            statement=text, evidence_ids=[ev.evidence_id]))
    if not result.findings:
        result.missing_items.append({'kind': 'private_library_empty', 'message': '本人项目池中没有匹配资料，请先导入并确认项目。'})
    return result
