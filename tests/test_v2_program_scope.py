"""Programme scope: a user-named programme stays the research target end to end.

Regression for a run where an unregistered programme ("Computational Science
and Engineering") was dropped by parsing, replaced by catalogue programmes of
the same school, searched with a polluted query, and its sub-pages never found.
All rules here are generic; no school, URL or programme is special-cased.
"""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from opportunity_agent.official_research import classify_program_page
from opportunity_agent.v2.agents.contracts import ProgramResult, ResearchResult
from opportunity_agent.v2.research.extraction import AdmissionExtraction, AdmissionFact, same_program
from opportunity_agent.v2.research.search_scope import initial_query
from opportunity_agent.v2.research.service import ResearchService
from opportunity_agent.v2.research.task import parse_task
from opportunity_agent.v2.research.tool_ledger import RedisToolLedger
from opportunity_agent.v2.research.web import TavilyMCP


def parse(message):
    return parse_task(SimpleNamespace(message=message, success_criteria=None, missing_task=None))


# --- parsing --------------------------------------------------------------

@pytest.mark.parametrize("message,program", [
    ("查询Georgia Tech Computational Science and Engineering 2027秋季的信息", "Computational Science and Engineering"),
    ("CMU Master of Science in Robotics 2027 Fall 截止日期和GRE", "Master of Science in Robotics"),
    ("Brown Applied Mathematics 2027 Fall deadline", "Applied Mathematics"),
])
def test_unregistered_programme_name_is_kept_unverified(message, program):
    task = parse(message)
    assert task.entities.program == program
    assert task.routing_diagnostics["program_source"] == "query_span_unverified"


def test_model_parse_targets_stay_typed():
    from opportunity_agent.v2.research.task import ResearchEntities, ResearchTarget, SemanticParse
    class Parser:
        enabled = True
        def generate_structured(self, schema, **kwargs):
            return SemanticParse(entities=ResearchEntities(targets=[ResearchTarget(
                university="georgia institute of technology", program="Computational Science and Engineering")]))
    task = parse_task(SimpleNamespace(message="查询Georgia Tech Computational Science and Engineering 2027秋季的信息",
                                      success_criteria=None, missing_task=None), llm=Parser())
    assert all(isinstance(t, ResearchTarget) for t in task.entities.targets)


def test_request_words_never_become_a_programme_name():
    for message in ("Brown 2027 Fall GRE 要求", "Brown 2027 Fall application deadline requirements"):
        assert parse(message).entities.program == ""


def test_programme_information_defaults_to_admission_fields():
    task = parse("查询Georgia Tech Computational Science and Engineering 2027秋季的信息")
    assert task.requested_fields == ["deadline", "gre_policy"] and not task.semantic_questions


def test_advisory_question_keeps_semantic_route():
    task = parse("CMU MSCS 适合我吗")
    assert task.requested_fields == ["research"] and task.semantic_questions


@pytest.mark.parametrize("message,degree", [("Brown Applied Mathematics 硕士 2027 Fall", "masters"),
    ("Brown Applied Mathematics PhD 2027 Fall", "doctoral"), ("Brown Applied Mathematics 2027 Fall", "")])
def test_degree_only_when_user_states_it(message, degree):
    assert parse(message).entities.degree == degree


# --- target scope ---------------------------------------------------------

def catalogue(*programs, school="Georgia Institute of Technology"):
    return [ProgramResult(program_id=f"p{i}", university=school, program=p, intake="2027 Fall")
            for i, p in enumerate(programs)]


def test_named_programme_is_not_replaced_by_catalogue_siblings():
    task = parse("查询Georgia Tech Computational Science and Engineering 2027秋季的信息")
    rows = catalogue("Master of Science in Computer Science", "Master of Science in Electrical and Computer Engineering")
    targets = ResearchService(None, llm=SimpleNamespace(enabled=False))._web_targets(task, ResearchResult(), rows)
    assert [t.program for t in targets] == ["Computational Science and Engineering"]


def test_unnamed_programme_still_expands_to_catalogue():
    task = parse("Georgia Tech 2027 Fall GRE")
    rows = catalogue("Master of Science in Computer Science", "Master of Science in Electrical and Computer Engineering")
    targets = ResearchService(None, llm=SimpleNamespace(enabled=False))._web_targets(task, ResearchResult(), rows)
    assert len(targets) == 2


def test_search_query_is_built_from_structured_scope_only():
    task = parse("查询Georgia Tech Computational Science and Engineering 2027秋季的信息")
    target = ProgramResult(university="Georgia Institute of Technology", program=task.entities.program, intake=task.entities.intake)
    query = initial_query(task, target)
    assert query.isascii()  # no raw (Chinese) question appended
    assert "Computational Science and Engineering" in query and "2027 Fall" in query
    assert "computer science" not in query.casefold().replace("computational science", "")


# --- page identity for unregistered names ---------------------------------

NAME = "Computational Science and Engineering"


@pytest.mark.parametrize("title,url,text,expected", [
    ("Computational Science & Engineering | College of Computing", "https://example.edu/cse", "Overview.", "exact"),
    ("Admissions", "https://example.edu/academics/computational-science-and-engineering-ms", "Apply.", "exact"),
    ("Graduate", "https://example.edu/grad", "# MS in Computational Science and Engineering\nDetails", "exact"),
    # A sibling's page that lists this programme in navigation is not its page.
    ("MS Computer Science", "https://example.edu/ms-computer-science",
     "Menu: Computational Science and Engineering, Mechanical Engineering.", "rejected"),
    ("Courses | Professional Education", "https://pe.example.edu/courses", "Safety course.", "generic"),
])
def test_unregistered_programme_page_identity(title, url, text, expected):
    assert classify_program_page(NAME, title, url, text)[0] == expected


def _fact(block, **kw):
    base = dict(field="deadline", value="February 1st", block_id=block["id"], university="Georgia Institute of Technology",
                program=NAME, degree="masters", scope="all", date_text="February 1st", date_year_status="unknown",
                deadline_kind="final")
    return AdmissionFact(**{**base, **kw})


# Page shape of a real programme requirements page: identity declared in the
# section intro, the deadline in a later subsection that only says "MS CSE".
SECTIONED = ("### CSE M.S. Application Requirements\n"
             "Interested in Georgia Tech's Master's degree in Computational Science and Engineering (CSE)?\n"
             "##### Home Units\n"
             "Application deadlines vary across the home units.\n"
             "School of Computational Science and Engineering(MS CSE application deadline: February 1st)\n")


def test_enclosing_section_declares_identity_and_abbreviation_for_subsection():
    from opportunity_agent.v2.research.extraction import blocks, verify_fact
    page = {"url": "https://www.cse.gatech.edu/cse-ms-application-requirements",
            "title": "CSE M.S. Application Requirements | School of Computational Science and Engineering", "text": SECTIONED}
    target = ProgramResult(university="Georgia Institute of Technology", program=NAME, intake="2027 Fall")
    deadline_block = next(b for b in blocks(SECTIONED, page["title"]) if "February 1st" in b["text"])
    fact = _fact(deadline_block, quote="School of Computational Science and Engineering(MS CSE application deadline: February 1st)",
                 program_quote="Master's degree in Computational Science and Engineering (CSE)")
    provenance, reason = verify_fact(fact, target, page, ["deadline"], __import__("datetime").date(2026, 10, 10))
    assert reason == "" and provenance["year_status"] == "unknown"  # retained, never a verified ISO date
    # Date text that is not in the quote is still rejected.
    assert verify_fact(fact.model_copy(update={"value": "Feb 1", "date_text": "Feb 1"}), target, page,
                       ["deadline"], __import__("datetime").date(2026, 10, 10))[1] == "invalid_year_unknown_claim"


def test_sibling_section_cannot_lend_its_identity():
    from opportunity_agent.v2.research.extraction import blocks, verify_fact
    text = ("### Master of Science in Robotics\nThe MS in Robotics deadline is December 1, 2026.\n"
            "### Admissions\nApplication deadline: February 1, 2027.\n")
    page = {"url": "https://www.cmu.edu/robotics", "title": "Graduate programmes", "text": text}
    target = ProgramResult(university="Carnegie Mellon University", program="Applied Mathematics", intake="2027 Fall")
    block = next(b for b in blocks(text, page["title"]) if "February 1, 2027" in b["text"])
    fact = AdmissionFact(field="deadline", value="2027-02-01", block_id=block["id"], quote="Application deadline: February 1, 2027.",
        university="Carnegie Mellon University", program="Master of Science in Robotics", program_quote="Master of Science in Robotics",
        degree="masters", scope="all", date_text="February 1, 2027", deadline_kind="final")
    assert verify_fact(fact, target, page, ["deadline"], __import__("datetime").date(2026, 10, 10))[1] == "program_identity_not_grounded"


@pytest.mark.parametrize("requested,full,page,expected", [
    # Page uses the acronym and the full name separately, never "Name (ABBR)".
    ("MSAII", "Master of Science in Artificial Intelligence and Innovation",
     "Master of Science in Artificial Intelligence and Innovation ... why you chose the MSAII program", True),
    ("MS CSE", "Master of Science in Computational Science and Engineering", "The MS CSE program ...", True),
    # Same letters, but the page never uses the acronym: no match.
    ("MSAII", "Master of Science in Artificial Intelligence and Innovation", "Master of Science in AI and Innovation", False),
    # Acronym on page but initials disagree (MSCS vs Computational Science and Engineering).
    ("MSCS", "Master of Science in Computational Science and Engineering", "MSCS and MS CSE", False),
    # Degree prefix must agree: "MS CSE" is not a PhD programme.
    ("MS CSE", "PhD in Computational Science and Engineering", "MS CSE ...", False),
])
def test_page_local_acronym(requested, full, page, expected):
    from opportunity_agent.v2.research.extraction import page_acronym
    assert page_acronym(requested, full, page) is expected


def test_degree_less_request_matches_degree_qualified_page_name():
    scope = "Master of Science in Computational Science and Engineering"
    assert same_program(NAME, scope, scope)
    assert not same_program(NAME, "Master of Science in Computer Science", scope)
    # A degree-qualified request still requires that degree's exact name.
    assert not same_program("MS in Robotics", "PhD in Robotics", "PhD in Robotics")


# --- tool loop: ranking, map discovery, no replay, per-target budget -------

class MemoryLedger(RedisToolLedger):
    records = {}
    async def _change(self, mutate):
        state = copy.deepcopy(self.records.get(self.key, self.initial))
        value = mutate(state)
        self.records[self.key] = copy.deepcopy(state)
        self.state = state
        return value


HOME = "https://www.cc.gatech.edu/degree-programs/master-science-computational-science-and-engineering"
ADMISSIONS = HOME + "/admissions"
DEADLINE = "The application deadline for fall admission is February 1, 2027."
GRE = "The GRE is not required for this programme."
PAGES = {
    HOME: ("Master of Science in Computational Science and Engineering",
           "# Master of Science in Computational Science and Engineering\nAn interdisciplinary programme in modelling."),
    ADMISSIONS: ("Master of Science in Computational Science and Engineering - Admissions",
                 f"# Master of Science in Computational Science and Engineering Admissions\n{DEADLINE}\n{GRE}"),
}
UNRELATED = [{"url": "https://pe.gatech.edu/courses", "title": "Courses | GTPE", "content": "Safety culture course."},
             {"url": "https://pe.gatech.edu/courses/rf", "title": "Basic RF Concepts | GTPE", "content": "Purchase orders."}]


class Site:
    def __init__(self, results, *, mappable=True):
        self.results, self.search_calls, self.page_calls, self.reads, self.maps = results, 0, 0, [], []
        self.search_limit = self.page_limit = 60
        self.tools = {"tavily_extract": {}, **({"tavily_map": {}} if mappable else {})}
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def search(self, query, domains):
        self.search_calls += 1
        return {"results": self.results}
    async def map(self, url, domains):
        self.maps.append(url)
        return [ADMISSIONS, HOME + "/news/2026", "https://www.cc.gatech.edu/people/someone"]
    async def validate_url(self, url, domains):
        return await TavilyMCP.validate_url(self, url, domains)
    async def _read_direct(self, url, domains, **kwargs):
        self.page_calls += 1
        self.reads.append(url)
        title, text = PAGES[url]
        return {"url": url, "title": title, "text": text}
    async def extract(self, url, domains, **kwargs):
        return await self._read_direct(url, domains)


class GroundedModel:
    """Returns facts only for quotes present in the supplied blocks."""
    enabled = True
    def __init__(self): self.calls = 0
    def generate_structured(self, schema, **kwargs):
        self.calls += 1
        context, facts = kwargs["context"], []
        for block in context["blocks"]:
            common = dict(block_id=block["id"], university=context["university"],
                          program=PAGES[ADMISSIONS][0].removesuffix(" - Admissions"),
                          program_quote=PAGES[ADMISSIONS][0].removesuffix(" - Admissions"),
                          degree="masters", scope="all")
            if DEADLINE in block["text"] and "deadline" in context["fields"]:
                facts.append(AdmissionFact(field="deadline", value="2027-02-01", quote=DEADLINE,
                    date_text="February 1, 2027", deadline_kind="final", **common))
            if GRE in block["text"] and "gre_policy" in context["fields"]:
                facts.append(AdmissionFact(field="gre_policy", value="not_required", quote=GRE,
                    gre_required=False, gre_considered=None, **common))
        return AdmissionExtraction(facts=facts)


class FailingPlanner:
    """Repair planner outage (the run's HTTP 503): only deterministic repair may act."""
    def __init__(self): self.calls = 0
    async def decide(self, context, remaining):
        self.calls += 1
        raise TimeoutError("planner unavailable")


@pytest.fixture(autouse=True)
def config(monkeypatch):
    MemoryLedger.records.clear()
    for name, value in {"RESEARCH_TOOL_REPAIR_ENABLED": "1", "RESEARCH_PERSIST_FACTS": "0",
                        "RESEARCH_PROGRESS_ENABLED": "0", "RESEARCH_QUEUE_INGEST": "0"}.items():
        monkeypatch.setenv(name, value)


async def run_web(site, message="查询Georgia Tech Computational Science and Engineering 2027秋季的信息", targets=None):
    model = GroundedModel()
    service = ResearchService(None, llm=model, web_factory=lambda: site)
    service.tool_ledger_factory = MemoryLedger
    service.repair_planner = FailingPlanner()
    service._request = SimpleNamespace(user_id="u", run_id="r", request_id="q", message=message, research_tool_state={})
    service._deadline = asyncio.get_running_loop().time() + 120
    task = parse(message)
    task.as_of = __import__("datetime").date(2026, 10, 10)
    result = ResearchResult()
    await service._web(task, result, targets or [])
    return service, model, result


def test_irrelevant_results_skipped_and_subpages_found_by_map():
    async def run():
        site = Site(UNRELATED + [{"url": HOME, "title": PAGES[HOME][0], "content": "Interdisciplinary programme."}])
        service, model, result = await run_web(site)
        # Unrelated pages named nowhere near the programme are never read.
        assert not any("pe.gatech.edu" in u for u in site.reads)
        # The programme home lacks the facts; Map finds its admissions page.
        assert site.maps == [HOME] and ADMISSIONS in site.reads
        assert HOME + "/news/2026" not in site.reads
        program = result.programs[0]
        assert program.program == NAME
        assert str(program.deadline) == "2027-02-01" and program.gre_policy == "not_required"
        calls = result.diagnostics["tool_execution"]["tools_used"]
        assert calls <= 6
    asyncio.run(run())


def test_www_and_bare_host_are_one_page():
    async def run():
        bare = HOME.replace("https://www.", "https://")
        PAGES[bare] = PAGES[HOME]
        try:
            site = Site([{"url": HOME, "title": PAGES[HOME][0], "content": "x"},
                         {"url": bare, "title": PAGES[HOME][0], "content": "x"}], mappable=False)
            await run_web(site)
            assert len([u for u in site.reads if u in {HOME, bare}]) == 1
        finally:
            PAGES.pop(bare)
    asyncio.run(run())


def test_delegated_facts_on_programme_page_lead_to_map_not_to_rejection():
    """A verified programme page whose facts fail grounding still maps its sub-pages."""
    class StrictModel(GroundedModel):
        def generate_structured(self, schema, **kwargs):
            parsed = super().generate_structured(schema, **kwargs)
            if not any(DEADLINE in b["text"] for b in kwargs["context"]["blocks"]):
                # On the home page the model names a sibling identity -> PROGRAM_MISMATCH.
                block = kwargs["context"]["blocks"][0]
                parsed.facts.append(AdmissionFact(field="deadline", value="2027-02-01", block_id=block["id"],
                    quote=block["text"][:20], university=kwargs["context"]["university"], program="Other Programme",
                    program_quote="Other Programme", degree="masters", scope="all", date_text="February 1, 2027",
                    deadline_kind="final"))
            return parsed
    async def run():
        PAGES[HOME] = (PAGES[HOME][0], PAGES[HOME][1] + "\nApplication deadlines vary across the home units.")
        try:
            site = Site([{"url": HOME, "title": PAGES[HOME][0], "content": "x"}])
            model = StrictModel()
            service = ResearchService(None, llm=model, web_factory=lambda: site)
            service.tool_ledger_factory = MemoryLedger
            service.repair_planner = FailingPlanner()
            message = "查询Georgia Tech Computational Science and Engineering 2027秋季的信息"
            service._request = SimpleNamespace(user_id="u", run_id="r2", request_id="q", message=message, research_tool_state={})
            service._deadline = asyncio.get_running_loop().time() + 120
            task = parse(message)
            task.as_of = __import__("datetime").date(2026, 10, 10)
            result = ResearchResult()
            await service._web(task, result, [])
            assert site.maps == [HOME] and ADMISSIONS in site.reads
            assert str(result.programs[0].deadline) == "2027-02-01"
        finally:
            PAGES[HOME] = (PAGES[HOME][0], PAGES[HOME][1].replace("\nApplication deadlines vary across the home units.", ""))
    asyncio.run(run())


def test_research_without_map_does_not_replay_searches_that_return_read_pages():
    async def run():
        site = Site([{"url": HOME, "title": PAGES[HOME][0], "content": "Interdisciplinary programme."}], mappable=False)
        service, model, result = await run_web(site)
        codes = [e["code"] for e in result.errors]
        assert "NO_NEW_RESULTS" in codes
        assert site.search_calls == 2  # initial search + one field search, then stop
        assert site.reads == [HOME]
        assert not result.programs or result.programs[0].deadline is None  # nothing invented
    asyncio.run(run())


def test_sibling_programmes_at_one_school_have_separate_budgets():
    async def run():
        site = Site([{"url": HOME, "title": PAGES[HOME][0], "content": "x"}], mappable=False)
        targets = [ProgramResult(university="Georgia Institute of Technology", program=p, intake="2027 Fall")
                   for p in ("Computational Science and Engineering", "Applied Mathematics")]
        service, model, result = await run_web(site, "Georgia Tech 2027 Fall GRE deadline", targets)
        schools = result.diagnostics["tool_execution"]["schools"]
        assert {k for k in schools if k.startswith("georgia")} == {
            "georgia institute of technology|computational science and engineering",
            "georgia institute of technology|applied mathematics"}
        assert all(v["tools"] <= 6 for v in schools.values())
        assert "TOOL_BUDGET_EXHAUSTED" not in [e["code"] for e in result.errors][:2]
    asyncio.run(run())
