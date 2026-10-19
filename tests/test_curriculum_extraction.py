import asyncio
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from opportunity_agent.v2.workbench.curriculum import Curriculum
from opportunity_agent.v2.workbench.schemas import ProjectData
from opportunity_agent.v2.workbench.importer import Extraction, extract_draft


@pytest.mark.parametrize('name',[
    '15-640 Distributed Systems','Probability and Statistics','Research & Innovation',
    '机器学习与数据挖掘','Introduction to AI (2027)','COMP SCI 540 Introduction to AI'])
def test_single_course_names(name):
    assert len(Curriculum(courses=[{'name':name}]).courses)==1


@pytest.mark.parametrize('row',[
    {'name':'15-640 Distributed Systems; 15-645 Database Systems'},
    {'name':'10-623 Generative AI OR 11-667 Large Language Models'},
    {'name':'AI；NLP；ML'}, {'name':'AI\nNLP'},
    {'name':'At least three elective courses'}, {'name':'A 12-unit course in AI'},
    {'name':'AI','credits':'At least 36 units; three courses'},
    {'name':'AI','extra':'unexpected'}, {'name':['AI','ML']},
])
def test_invalid_rows_rejected(row):
    with pytest.raises(ValidationError):Curriculum(courses=[row])


def test_choice_rule_and_group_credits_preserved():
    c=Curriculum(courses=[{'name':'10-623 Generative AI','group':'Knowledge'},
        {'name':'11-667 Large Language Models','group':'Knowledge'},
        {'name':'15-640 Distributed Systems','group':'Electives'}],course_rules=[
        {'group':'Knowledge','requirement':'Choose one','course_codes':['10-623','11-667'],'choose':1},
        {'group':'Electives','requirement':'At least 36 units','credits':'36 units'}])
    assert len(c.courses)==3 and c.course_rules[0].choose==1
    assert c.courses[2].credits==''
    assert not ProjectData(**c.model_dump()).courses_complete


def test_schema_has_typed_course_items_and_separate_rules():
    schema=Extraction.model_json_schema()
    assert schema['$defs']['AtomicCourse']['properties']['name']['type']=='string'
    assert schema['$defs']['Curriculum']['properties']['courses']['items']['$ref'].endswith('/AtomicCourse')
    assert schema['$defs']['CourseRule']['properties']['course_codes']['items']['type']=='string'
    assert schema['$defs']['AtomicCourse']['additionalProperties'] is False


def test_duplicate_and_invalid_choice():
    with pytest.raises(ValidationError):Curriculum(courses=[{'name':'AI'},{'name':' ai '}])
    with pytest.raises(ValidationError):Curriculum(course_rules=[{'requirement':'Choose','choose':0}])


def test_legacy_draft_readable_but_new_extraction_repairs(monkeypatch):
    old={'name':'15-640 Systems; 15-645 Databases','group':'Electives'}
    assert ProjectData(courses=[old]).courses  # existing stored drafts still open
    class Model:
        enabled=True
        def __init__(self):self.calls=0
        def generate_structured(self, schema, **kw):
            self.calls+=1
            if self.calls==1:
                return Extraction.model_validate({'fields':[{'field':'courses','value':[old]}]})
            if self.calls==2:
                assert kw['context']['validation_errors']
                return Extraction.model_validate({'curriculum':{'courses':[
                    {'name':'15-640 Systems','group':'Electives'},{'name':'15-645 Databases','group':'Electives'}],
                    'course_rules':[{'group':'Electives','requirement':'Choose one','choose':1,'course_codes':['15-640','15-645']}]}})
            return Extraction()
    async def run():
        m=Model();calls=[]
        async def reserve():calls.append(1);return len(calls)<=3
        result=await extract_draft(SimpleNamespace(draft=ProjectData().model_dump()),[],llm=m,reserve_call=reserve)
        assert len(result['courses'])==2 and result['course_rules'][0]['choose']==1
        assert len(calls)==3 and not result['courses_complete']
    asyncio.run(run())


def test_repair_is_bounded_and_invalid_courses_not_saved():
    class Model:
        enabled=True
        def generate_structured(self,*a,**kw):
            return Extraction.model_validate({'fields':[{'field':'courses','value':[{'name':'A; B'}]}]})
    async def run():
        calls=[]
        async def reserve():calls.append(1);return True
        result=await extract_draft(SimpleNamespace(draft=ProjectData().model_dump()),[],llm=Model(),reserve_call=reserve)
        assert not result['courses'] and len(calls)==3 and result['warnings']
    asyncio.run(run())
