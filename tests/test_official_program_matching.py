from __future__ import annotations

import json

from opportunity_agent.models import (OfficialRequirement, OfficialSource, StudentProfile,
                                      legacy_target_program_pairs)
from opportunity_agent.official_research import (DynamicDomainCache, OfficialCache, OfficialDomainRegistry,
                                                  OfficialResearchTools, classify_program_page, program_identity)
from opportunity_agent.planning import ModelScopeRoadmapPlanner, _target_pairs


def test_can_resolve_canadian_seed_aliases():
    registry = OfficialDomainRegistry()
    assert registry.resolve("UBC")["verified_domain"] == "ubc.ca"
    assert registry.resolve("UofT")["verified_domain"] == "utoronto.ca"
    assert registry.resolve("Waterloo")["verified_domain"] == "uwaterloo.ca"


def test_mscs_page_match_rejects_business_and_accepts_siebel():
    assert classify_program_page("MSCS", "Gies MBA admissions", "https://gies.illinois.edu/mba", "MBA application")[0] == "rejected"
    match, scope, evidence = classify_program_page(
        "MSCS", "Siebel School graduate admissions", "https://cs.illinois.edu/admissions", "Computer Science Department"
    )
    assert (match, scope) == ("exact", "department")
    assert evidence


def test_mscs_rejects_explicit_msaii_page_even_with_scs_footer():
    match, scope, evidence = classify_program_page(
        "Master of Science in Computer Science",
        "How to Apply | Master of Science in Artificial Intelligence and Innovation",
        "https://msaii.cs.cmu.edu/how-apply",
        "The MSAII application is open. School of Computer Science. CS graduate programmes.",
    )
    assert (match, scope) == ("rejected", "program")
    assert any("different program identity" in item for item in evidence)


def test_mscs_rejects_uoft_civil_and_mineral_engineering_page():
    match, _, evidence = classify_program_page(
        "MSCS", "Application Requirements - Department of Civil & Mineral Engineering",
        "https://civmin.utoronto.ca/graduate/application-requirements",
        "The Department of Civil & Mineral Engineering graduate application requirements.",
    )
    assert match == "rejected"
    assert any("civil" in item and "mineral" in item for item in evidence)

    # A central university page still remains a generic source.  It can support
    # only university-wide facts, never MSCS-specific requirements.
    assert classify_program_page(
        "MSCS", "Graduate admissions", "https://www.utoronto.ca/graduate-admissions", "Apply to graduate studies"
    )[0] == "generic"


def test_msml_identity_rejects_cmu_ece_pages_and_splits_legacy_combined_program_cell():
    assert program_identity("MSAII, MSML")["aliases"][0] == "msml"
    match, _, reason = classify_program_page(
        "MSML", "Graduate Application Guidelines — Electrical and Computer Engineering",
        "https://www.ece.cmu.edu/admissions/graduate-application-guidelines.html", "MS in ECE application"
    )
    assert match == "rejected" and any("ECE" in item or "engineering" in item for item in reason)
    profile = StudentProfile(user_id="legacy", target_program_choices=[{"school": "CMU", "program": "MSAII, MSML"}])
    assert [(item.school, item.program) for item in _target_pairs(profile)] == [("CMU", "MSAII"), ("CMU", "MSML")]


def test_old_ambiguous_target_lists_require_review_but_one_program_is_compatible():
    pairs, review = legacy_target_program_pairs(["CMU", "UIUC"], ["MSCS"])
    assert not review and [(item.school, item.program) for item in pairs] == [("CMU", "MSCS"), ("UIUC", "MSCS")]
    pairs, review = legacy_target_program_pairs(["CMU", "UIUC"], ["MSCS", "MSECE", "MSAI"])
    assert review and all(not item.program for item in pairs)
    profile = StudentProfile(user_id="legacy", target_schools=["CMU", "UIUC"], target_programs=["MSCS", "MSECE", "MSAI"])
    assert profile.target_program_mapping_needs_review


def test_cache_revokes_legacy_business_source_for_mscs(tmp_path):
    cache_path = tmp_path / "official.json"
    source = OfficialSource(source_id="bad", university="UIUC", program="MSCS", title="Gies MBA admissions",
                            url="https://gies.illinois.edu/mba", verified_domain="illinois.edu")
    cache = OfficialCache(cache_path)
    cache.put(source, [OfficialRequirement(field="gre", value="MBA GRE", source_ids=["bad"])])
    result = cache.get("UIUC", "MSCS", "", ["gre"])
    assert not result.sources
    assert result.revoked_sources[0].status == "revoked"
    document = json.loads(cache_path.read_text(encoding="utf-8"))
    assert document["audit"][-1]["source_id"] == "bad"


def test_dynamic_domain_cache_is_reused_without_search(tmp_path, monkeypatch):
    domain_path = tmp_path / "domains.json"
    domain_path.write_text('{"universities": []}', encoding="utf-8")
    dynamic = DynamicDomainCache(tmp_path / "dynamic.json")
    dynamic.put("Example University", {"university": "Example University", "verified_domain": "example.edu",
                                        "domains": ["example.edu"], "domain_source": "dynamic_search"})
    tools = OfficialResearchTools(registry=OfficialDomainRegistry(domain_path), dynamic_cache=dynamic, tavily_key="test")
    monkeypatch.setattr(tools, "_tavily_search", lambda *_: (_ for _ in ()).throw(AssertionError("must use cache")))
    assert tools.resolve_official_domain(type("Args", (), {"university": "Example University"})())["verified_domain"] == "example.edu"


def test_planner_researches_each_explicit_school_program_pair(monkeypatch):
    calls = []

    class EnabledTools:
        enabled = True
        def validate_sources(self, sources, targets): return sources, []

    def fake_research(tools, school, program, intake, questions):
        calls.append((school, program, intake))
        return type("Result", (), {"sources": [], "requirements": [], "unresolved_questions": [], "tool_trace": [], "revoked_sources": []})()

    monkeypatch.setattr("opportunity_agent.planning.deterministic_program_research", fake_research)
    planner = ModelScopeRoadmapPlanner(official_tools=EnabledTools())
    profile = StudentProfile(user_id="u", target_program_choices=[
        {"school": "CMU", "program": "MSCS"}, {"school": "UIUC", "program": "MSECE"},
    ])
    planner._research(profile)
    assert calls == [("CMU", "MSCS", "",), ("UIUC", "MSECE", "",)]
