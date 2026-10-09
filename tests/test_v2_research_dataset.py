from __future__ import annotations

import json
import asyncio
import hashlib
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from opportunity_agent.v2.evaluation.research_annotation import (
    _merge_candidate_pool,
    _apply_accepted_query_edits,
    annotation_state,
    create_app,
    export_gold,
    reviewed_item_state,
    save_judgment,
    save_review,
    weighted_kappa,
)
from opportunity_agent.v2.evaluation.research_dataset import (
    DUKE_GRADUATE_POLICY_PAGES,
    _fact_candidates,
    _graduate_policy_facts,
    _reconstruct_page_text,
    _revalidate_existing_corpus,
    _source_rejection,
    archive_removed_review_items,
    collect_corpus,
    CHUNKER_VERSION,
    generate_draft,
    normalise_document_ids,
    VERSION,
    review_queue,
    validate_draft,
)
from opportunity_agent.v2.rag.ingest import chunk_sectioned_text
from opportunity_agent.v2.evaluation.corpus import repair_corpus_duplicates


class FakeE5Tokenizer:
    is_fast = True

    def __call__(self, text, *, add_special_tokens=False, return_offsets_mapping=False):
        return {"offset_mapping": [match.span() for match in re.finditer(r"\S+", text)]}


def test_real150_draft_has_locked_distribution_and_is_reproducible():
    first, second = generate_draft(1729), generate_draft(1729)
    assert first == second
    report = validate_draft(first)
    assert report["case_count"] == 150
    assert report["splits"] == {"dev": 50, "test": 100}
    assert report["languages"] == {"zh": 90, "mixed": 30, "en": 30}
    assert set(report["schools"].values()) == {15}
    assert all(case["annotation_status"] == "draft_unreviewed" for case in first["cases"])
    assert all(not case["relevant_ids"] and not case["gold_programs"] for case in first["cases"])


def test_real150_projects_do_not_leak_between_splits():
    data = generate_draft()
    owners = {}
    for case in data["cases"]:
        for programme in case["candidate_program_ids"]:
            owners.setdefault(programme, case["split"])
            assert owners[programme] == case["split"]


def test_incremental_candidate_pool_replaces_only_selected_cases():
    previous = {"dataset_version": "v1", "fixture_models": False, "as_of": "2026-10-05",
                "queries": {"cmu-rag": "old CMU query", "duke-rag": "unchanged Duke query"},
                "pairs": [{"case_id": "cmu-rag", "chunk_id": "old-cmu", "ranks": {"A": 1}},
                          {"case_id": "duke-rag", "chunk_id": "duke", "ranks": {"A": 1}}]}
    refreshed = {"dataset_version": "v1", "fixture_models": False, "as_of": "2026-10-06",
                 "queries": {"cmu-rag": "corrected CMU query"},
                 "pairs": [{"case_id": "cmu-rag", "chunk_id": "clean-cmu", "ranks": {"A": 1}}]}

    merged = _merge_candidate_pool(previous, refreshed, {"cmu-rag"})

    assert merged["as_of"] == "2026-10-06"
    assert merged["queries"] == {"cmu-rag": "corrected CMU query", "duke-rag": "unchanged Duke query"}
    assert {(pair["case_id"], pair["chunk_id"]) for pair in merged["pairs"]} == {
        ("cmu-rag", "clean-cmu"), ("duke-rag", "duke")}


def test_review_queue_includes_non_automatic_human_decisions():
    data = generate_draft()
    queue = review_queue(data)
    counts = {stage: sum(item["stage"] == stage for item in queue)
              for stage in {item["stage"] for item in queue}}
    assert counts == {"program": 20, "query": 150, "gold": 150}


def test_fact_review_context_does_not_repeat_the_current_fact_list():
    data = generate_draft()
    data["sources"] = [{"id": "source-a", "program_id": data["programs"][0]["id"], "facts": [
        {"id": "gre-a", "field": "gre_policy", "value": "optional", "quote": "GRE is optional."}
    ]}]
    item = next(row for row in review_queue(data) if row["stage"] == "fact")
    assert item["payload"]["fact"]["id"] == "gre-a"
    assert "facts" not in item["payload"]["source"]


def test_source_review_does_not_show_unreviewed_facts_but_keeps_their_count():
    data = generate_draft()
    data["sources"] = [{"id": "source-a", "program_id": data["programs"][0]["id"], "facts": [
        {"id": "gre-a", "field": "gre_policy", "value": "optional", "quote": "GRE is optional."}
    ]}]
    item = next(row for row in review_queue(data) if row["stage"] == "source")
    assert "facts" not in item["payload"]
    assert item["payload"]["fact_candidate_count"] == 1


def test_weighted_kappa_and_direct_support_guard(tmp_path):
    assert weighted_kappa([(0, 0), (1, 1), (2, 2)]) == 1
    with pytest.raises(ValueError, match="at least one claim"):
        save_judgment(tmp_path, {"case_id": "q", "chunk_id": "c", "annotator": "a", "relevance": 2,
            "supports_claims": [], "program_match": "exact", "intake_match": "yes", "source_reliable": "yes"})


def test_annotation_api_is_local_workflow_and_resumable(tmp_path):
    data = generate_draft()
    (tmp_path / "draft.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "review-queue.json").write_text(json.dumps(review_queue(data)), encoding="utf-8")
    client = TestClient(create_app(tmp_path))
    first = client.get("/api/next", params={"annotator": "alice", "stage": "program"}).json()
    assert first["item"]["stage"] == "program"
    response = client.post("/api/reviews", json={"item_id": first["item"]["id"], "stage": "program",
        "annotator": "alice", "decision": "accept", "payload": {}, "notes": ""})
    assert response.status_code == 200
    following = client.get("/api/next", params={"annotator": "alice", "stage": "program"}).json()
    assert following["completed"] == 1
    assert following["item"]["id"] != first["item"]["id"]
    progress = client.get("/api/progress").json()
    assert "issues" not in progress
    assert progress["issue_count"] > 0
    assert len(progress["issue_sample"]) <= 20


def test_annotation_api_reopens_last_review_and_keeps_revision_history(tmp_path):
    data = generate_draft()
    data["sources"] = [{"id": "source-a", "program_id": data["programs"][0]["id"],
                        "url": "https://example.edu/page", "facts": []}]
    (tmp_path / "draft.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "review-queue.json").write_text(json.dumps(review_queue(data)), encoding="utf-8")
    client = TestClient(create_app(tmp_path))
    item = client.get("/api/next", params={"annotator": "alice", "stage": "source"}).json()["item"]
    body = {"item_id": item["id"], "stage": "source", "annotator": "alice", "decision": "accept",
            "payload": {}, "notes": "first"}
    assert client.post("/api/reviews", json=body).status_code == 200
    reopened = client.get("/api/item", params={"annotator": "alice", "stage": "source", "item_id": item["id"]}).json()
    assert reopened["editing"] is True
    assert reopened["saved_review"]["notes"] == "first"
    body.update({"decision": "reject", "notes": "changed"})
    assert client.post("/api/reviews", json=body).status_code == 200
    from opportunity_agent.v2.evaluation.research_annotation import connect
    connection = connect(tmp_path)
    try:
        assert connection.execute("SELECT count(*) FROM review_history").fetchone()[0] == 1
    finally:
        connection.close()


def test_removed_previous_item_is_archived_and_can_be_reopened(tmp_path):
    data = generate_draft()
    (tmp_path / "draft.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "review-queue.json").write_text("[]", encoding="utf-8")
    removed = {"id": "source:old-source", "stage": "source", "payload": {"url": "https://example.edu/old"}}
    assert archive_removed_review_items(tmp_path, [removed], []) == 1

    state = reviewed_item_state(tmp_path, "alice", "source", "source:old-source")
    assert state["archived"] is True
    assert state["item"] == removed
    assert state["editing"] is False
    assert "不会进入本轮 gold" in state["archive_note"]
    assert annotation_state(tmp_path, "alice", "source")["total"] == 0

    save_review(tmp_path, {"item_id": "source:old-source", "stage": "source", "annotator": "alice",
                           "decision": "reject", "payload": {}, "notes": "historical correction"})
    reopened = reviewed_item_state(tmp_path, "alice", "source", "source:old-source")
    assert reopened["archived"] is True
    assert reopened["editing"] is True
    assert reopened["saved_review"]["notes"] == "historical correction"


def test_review_rejects_blank_annotator(tmp_path):
    with pytest.raises(ValueError, match="Incomplete review"):
        save_review(tmp_path, {"item_id": "query:q", "stage": "query", "annotator": "", "decision": "accept"})


def test_accepted_query_wording_edit_is_applied_to_pool_and_gold_input(tmp_path):
    data = generate_draft()
    (tmp_path / "draft.json").write_text(json.dumps(data), encoding="utf-8")
    case = data["cases"][0]
    rewritten = "请核验 CMU MSCS 的 2027 Fall 申请截止日期及 GRE 要求。"
    save_review(tmp_path, {"item_id": "query:" + case["id"], "stage": "query", "annotator": "alice",
                           "decision": "accept", "payload": {"corrected_query": rewritten}, "notes": "自然表达"})
    reviewed = _apply_accepted_query_edits(data, tmp_path)
    item = next(row for row in reviewed["cases"] if row["id"] == case["id"])
    assert item["query"] == rewritten
    assert item["generated_query"] == case["query"]
    assert item["query_edit_status"] == "human_edited_pending_export"


def test_gold_export_is_blocked_until_human_review_is_complete(tmp_path):
    data = generate_draft()
    (tmp_path / "draft.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "review-queue.json").write_text(json.dumps(review_queue(data)), encoding="utf-8")
    (tmp_path / "candidate-pool.json").write_text(json.dumps({"pairs": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="Annotation workspace is incomplete"):
        export_gold(tmp_path)

    assert not (tmp_path / "gold.json").exists()


def test_chunk_identity_is_scoped_to_programme():
    base = {"id": "old", "source_id": "source", "url": "https://example.edu/shared", "title": "Shared",
            "text": "same text", "metadata": {"content_hash": "body", "program_id": "program-a"}}
    other = json.loads(json.dumps(base))
    other["metadata"]["program_id"] = "program-b"
    result = normalise_document_ids([base, other])
    assert len(result) == 2
    assert result[0]["id"] != result[1]["id"]


def test_sectioned_chunker_uses_tokenizer_and_keeps_heading_paths_separate():
    sections = chunk_sectioned_text(
        "# MSCS\nIntro paragraph.\n## Curriculum\nML systems course.\n### Electives\nArchitecture elective.",
        tokenizer=FakeE5Tokenizer(), size=4, overlap=1)
    assert sections
    assert {path for _, path in sections} == {
        "MSCS", "MSCS > Curriculum", "MSCS > Curriculum > Electives"}
    assert not any(text.startswith("#") for text, _ in sections)
    assert all("Curriculum" not in text for text, path in sections if path == "MSCS")


def test_new_chunk_identity_includes_section_and_chunker_revision():
    base = {"id": "old", "source_id": "source", "url": "https://example.edu/shared", "title": "Shared",
            "text": "same text", "metadata": {"program_id": "program-a", "content_hash": "body",
                "chunker_version": CHUNKER_VERSION, "tokenizer_model": "intfloat/multilingual-e5-small",
                "tokenizer_revision": "rev-a", "chunk_index": 0, "section_path": "Curriculum"}}
    other_section = json.loads(json.dumps(base))
    other_section["metadata"]["section_path"] = "Research"
    result = normalise_document_ids([base, other_section])
    assert len(result) == 2
    assert result[0]["id"] != result[1]["id"]


def test_duplicate_source_roles_do_not_multiply_retokenized_chunks():
    from opportunity_agent.v2.evaluation.research_dataset import _retokenize_existing_documents

    program = generate_draft()["programs"][0]
    source = {"id": "shared-source", "program_id": program["id"], "url": "https://cmu.edu/shared",
              "content_hash": "same-page", "page_type": "admissions", "facts": [{"id": "fact-1"}]}
    other_source = {**source, "page_type": "curriculum", "facts": []}
    document = {"id": "legacy-chunk", "source_id": source["id"], "text": "# Curriculum\nMachine learning course.",
                "metadata": {"page_type": "admissions"}}
    duplicate = {**document, "metadata": {"page_type": "curriculum"}}
    repaired = repair_corpus_duplicates({"sources": [source, other_source], "documents": [document, duplicate]})
    assert len(repaired["sources"]) == len(repaired["documents"]) == 1
    assert repaired["documents"][0]["id"] == "legacy-chunk"
    assert repaired["sources"][0]["facts"] == [{"id": "fact-1"}]
    assert repaired["sources"][0]["page_types"] == ["admissions", "curriculum"]
    result = _retokenize_existing_documents([source, other_source], [document, duplicate], [program],
                                            FakeE5Tokenizer(), "e5", "revision")
    assert len(result) == 1
    assert result[0]["metadata"]["page_types"] == ["admissions", "curriculum"]
    again = _retokenize_existing_documents(repaired["sources"], result, [program],
                                           FakeE5Tokenizer(), "e5", "revision")
    assert again == result


def test_duplicate_source_with_different_body_is_not_silently_dropped():
    source = {"id": "source", "program_id": "program", "url": "https://cmu.edu/page", "content_hash": "v1"}
    with pytest.raises(ValueError, match="Conflicting source snapshots"):
        repair_corpus_duplicates({"sources": [source, {**source, "content_hash": "v2"}]})


def test_collect_does_not_duplicate_redirected_canonical_page(monkeypatch):
    import opportunity_agent.v2.evaluation.research_dataset as dataset_module
    import opportunity_agent.v2.research.web as web_module

    draft = generate_draft()
    program = next(p for p in draft["programs"] if p["school_code"] == "ucsd")
    draft["programs"] = [program]
    canonical = "https://ucsd.edu/masters"
    text = "# Master of Science\n" + "Courses and admissions information for the master's program. " * 10

    class Web:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def search(self, query, domains):
            return {"results": [{"url": canonical + "?alias=" + str(len(query))}]}

        async def read(self, url, domains):
            return {"url": canonical, "title": program["program"], "text": text}

    monkeypatch.setattr(web_module, "TavilyMCP", lambda **kwargs: Web())
    monkeypatch.setattr(dataset_module, "classify_program_page", lambda *args: ("exact", "program", []))
    monkeypatch.setattr(dataset_module, "PAGE_SEEDS", {})
    result = asyncio.run(collect_corpus(draft, tokenizer=FakeE5Tokenizer()))
    active = [s for s in result["sources"] if s.get("id")]
    assert len(active) == 1
    assert len(result["documents"]) == len({d["id"] for d in result["documents"]})
    assert set(result["collection_gaps"][program["id"]]) == {"curriculum", "research"}


def test_mcp_extract_rejects_non_official_url_before_network():
    from opportunity_agent.v2.research.web import TavilyMCP
    web = TavilyMCP()
    web.tools["tavily_extract"] = ("tavily_extract", {"properties": {}})
    with pytest.raises(ValueError, match="verified official domains"):
        asyncio.run(web.extract("https://attacker.example/page", ["cmu.edu"]))


def test_language_policy_is_normalized_and_duplicate_mentions_are_collapsed():
    text = ("TOEFL/IELTS/Duolingo scores are required for applicants whose native language is not English. "
            "Recent TOEFL or IELTS scores must be valid. We do not issue language-test waivers.")
    facts = [item for item in _fact_candidates(text) if item["field"] == "language"]
    assert len(facts) == 1
    assert facts[0]["value"] == "required"
    assert facts[0]["value"] != facts[0]["quote"]


def test_fact_extractor_drops_deadline_headings_and_handles_optional_gre_context():
    text = ("### Deadlines\nSee the deadlines page.\n"
            "For the Spring 2027 and Fall 2027 admission cycles, the GRE requirement will be optional. "
            "Applicants who do not submit a GRE score report will still be reviewed.\n"
            "English proficiency is not required for applicants who have graduated from a US university. "
            "Other applicants must submit TOEFL or IELTS scores.")
    facts = _fact_candidates(text)
    assert not [item for item in facts if item["field"] == "deadline"]
    gre = [item for item in facts if item["field"] == "gre_policy"]
    assert gre and {item["value"] for item in gre} == {"optional"}
    language = [item for item in facts if item["field"] == "language"]
    assert language and language[0]["value"] == "conditional"


def test_historical_uiuc_chicago_news_cannot_supply_2027_urbana_fact():
    data = generate_draft()
    program = next(p for p in data["programs"] if p["id"] == "uiuc-master-of-computer-science-2027-fall")
    text = ("Professional Master of Computer Science in Chicago starts Spring 2023. "
            "The application deadline is October 31, 2022. The GRE is not required.")
    url = "https://siebelschool.illinois.edu/news/mcs-in-chicago"
    data["sources"] = [{"id": "old-news", "program_id": program["id"], "url": url,
                        "title": "Master of Computer Science in Chicago | News", "page_type": "research",
                        "facts": [{"id": "old-date", "field": "deadline", "value": "2022-10-31"}]}]
    data["documents"] = [{"id": "old-chunk", "source_id": "old-news", "url": url,
                          "title": data["sources"][0]["title"], "text": text,
                          "metadata": {"program_id": program["id"]}}]
    sources, documents = _revalidate_existing_corpus(data)
    assert not documents
    assert sources[0]["status"] == "editorial_page"
    assert not any(f["field"] == "deadline" for f in _fact_candidates(text, target_intake="2027 Fall"))


def test_2027_admissions_table_row_yields_date_without_using_2022_cycle():
    text = ("Application Quarter(s) and Deadline(s)\nApplication Quarter\nFinal Deadline\n"
            "Fall\u00a02027\nN/A\nN/A\nWednesday, December 16, 2026\n")
    deadlines = [fact for fact in _fact_candidates(text, target_intake="2027 Fall")
                 if fact["field"] == "deadline"]
    assert [fact["value"] for fact in deadlines] == ["2026-12-16"]


def test_deadline_extractor_skips_open_date_and_selects_target_degree_deadline():
    text = ("Application Deadlines for Fall 2027 intake. Applications open September 2nd, 2026, "
            "with the following deadlines: Ph.D.: December 16th, 2026. "
            "M.S.: January 6th, 2027.")
    masters = [fact for fact in _fact_candidates(text, target_intake="2027 Fall", target_degree="masters")
               if fact["field"] == "deadline"]
    doctoral = [fact for fact in _fact_candidates(text, target_intake="2027 Fall", target_degree="doctoral")
                if fact["field"] == "deadline"]
    assert [(fact["value"], fact["quote"]) for fact in masters] == [
        ("2027-01-06", "M.S.: January 6th, 2027")]
    assert [(fact["value"], fact["quote"]) for fact in doctoral] == [
        ("2026-12-16", "Ph.D.: December 16th, 2026")]


def test_gre_non_requirement_when_negation_precedes_gre():
    facts = _fact_candidates("The Siebel School does not require GRE scores for its graduate programs.")
    assert [(fact["field"], fact["value"]) for fact in facts] == [("gre_policy", "not_required")]


def test_reconstruct_page_text_removes_chunk_overlap_before_fact_extraction():
    documents = [{"text": "The GRE requirement will be optional. English proficiency"},
                 {"text": "English proficiency is required for international applicants."}]
    assert _reconstruct_page_text(documents) == (
        "The GRE requirement will be optional. English proficiency is required for international applicants."
    )


def test_existing_wrong_program_page_is_removed_before_resuming_collection():
    data = generate_draft()
    program = data["programs"][0]
    source_id = "wrong-msaii"
    data["sources"] = [{"id": source_id, "program_id": program["id"],
        "url": "https://msaii.cs.cmu.edu/how-apply",
        "title": "How to Apply | Master of Science in Artificial Intelligence and Innovation",
        "page_type": "admissions", "facts": []}]
    data["documents"] = [{"id": "chunk", "source_id": source_id, "url": data["sources"][0]["url"],
        "title": data["sources"][0]["title"], "text": "MSAII admissions. School of Computer Science.",
        "metadata": {"program_id": program["id"]}}]
    sources, documents = _revalidate_existing_corpus(data)
    assert not documents
    assert not any(source.get("id") for source in sources)
    assert sources[0]["status"] == "program_mismatch"


def test_duke_four_plus_one_page_is_rejected_for_regular_graduate_mscs():
    program = next(p for p in generate_draft()["programs"]
                   if p["id"] == "duke-master-of-science-in-computer-science-2027-fall")
    assert _source_rejection(program, "https://cs.duke.edu/ms-cs-41-program-duke-undergraduates",
                             "MS in CS 4+1 Program for Duke Undergraduates") == "wrong_program_or_audience"


def test_duke_parent_policies_extract_the_target_masters_row_and_language_conditions():
    program = next(p for p in generate_draft()["programs"]
                   if p["id"] == "duke-master-of-science-in-computer-science-2027-fall")
    pages = {item["field"]: item for item in DUKE_GRADUATE_POLICY_PAGES[program["id"]]}
    deadline_text = ("Fall 2027 graduate deadlines. Ph.D. Deadlines\nComputer Science | 12/15/2026\n"
                     "Master's Deadlines\nMaster's Programs\nComputer Science | 02/01/2027\nSpring Semester")
    deadline = _graduate_policy_facts(program, pages["deadline"], deadline_text)
    assert [(item["value"], item["quote"]) for item in deadline] == [
        ("2027-02-01", "Computer Science | 02/01/2027")]

    gre_text = "GRE Required\nComputer Science (MS)\nGRE Optional\nComputer Science (Ph.D.)"
    gre = _graduate_policy_facts(program, pages["gre_policy"], gre_text)
    assert [(item["value"], item["quote"]) for item in gre] == [
        ("required", "GRE Required: Computer Science (MS)")]

    language_text = ("If your first language is not English, you must submit scores from either the Test of English "
                     "as a Foreign Language (TOEFL), IELTS, or Duolingo English Test. "
                     "To be eligible for a TOEFL/IELTS/Duolingo English Test waiver, you must have studied fulltime "
                     "for two years or more at a college or university.")
    language = _graduate_policy_facts(program, pages["language"], language_text)
    assert len(language) == 1
    assert language[0]["value"] == "conditional"
    assert language[0]["quote"].count("must submit") == 1
    assert "waiver" in language[0]["quote"]


def test_collect_fetches_parent_policy_pages_even_when_department_page_types_exist(monkeypatch):
    import opportunity_agent.v2.evaluation.research_dataset as dataset_module
    import opportunity_agent.v2.research.web as web_module

    draft = generate_draft()
    sources = [{"id": f"old:{program['id']}:{kind}", "program_id": program["id"],
                "url": f"https://example.invalid/{program['id']}/{kind}", "page_type": kind}
               for program in draft["programs"] for kind in {"admissions", "curriculum", "research"}]
    monkeypatch.setattr(dataset_module, "_revalidate_existing_corpus", lambda _, **__: (sources, []))
    page_texts = {
        "application-deadlines": (
            "Application deadlines for fall 2027 graduate studies.\n## Master's Deadlines\n"
            "Computer Science | 02/01/2027\nElectrical and Computer Engineering | 01/16/2027\n"
            "These dates are listed by The Graduate School for master's programs."),
        "gre-scores": ("GRE Testing Requirements by Program. GRE Required\nComputer Science (MS)\n"
                       "GRE Optional\nComputer Science (Ph.D.)\nElectrical and Computer Engineering (Ph.D., MS). "
                       "These lists detail test requirements for Graduate School programs."),
        "english-language-proficiency-test-scores": (
            "If your first language is not English, you must submit scores from either TOEFL, IELTS, "
            "or Duolingo English Test. To be eligible for an English language test waiver, you must have "
            "studied fulltime for two years or more at an eligible university. This policy applies to "
            "applicants to The Graduate School.")
    }

    class FakeTavily:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def search(self, *_):
            raise AssertionError("existing project page types should not trigger search")

        async def read(self, url, _domains):
            key = next(key for key in page_texts if key in url)
            # Simulate a canonical URL change during refresh. The refreshed
            # source should replace the old page by program + policy field,
            # rather than leaving both URL versions active.
            canonical_url = url + "?canonical=refreshed" if key == "application-deadlines" else url
            return {"url": canonical_url, "title": "The Graduate School Policy", "text": page_texts[key]}

    monkeypatch.setattr(web_module, "TavilyMCP", lambda **_: FakeTavily())
    result = asyncio.run(collect_corpus({**draft, "corpus_status": "collecting_pending_human_review"},
                                        tokenizer=FakeE5Tokenizer()))
    duke_programs = {item["id"] for item in draft["programs"] if item["school_code"] == "duke"}
    added = [item for item in result["sources"] if item.get("classifier_scope") == "graduate_school_policy"]
    assert len(added) == 6
    assert {item["program_id"] for item in added} == duke_programs
    assert all(item["facts"] for item in added)
    assert result["version"] == VERSION
    assert result["collection_strategy"] == "one-page-per-type-plus-parent-policy-e5-sectioned-v3"
    duke_mscs = next(p["id"] for p in draft["programs"]
                     if p["school_code"] == "duke" and "Computer Science" in p["program"])
    duke_deadline = next(item for item in result["sources"]
                         if item.get("program_id") == duke_mscs and item.get("policy_fields") == ["deadline"])
    deadline_chunks = [item for item in result["documents"] if item["source_id"] == duke_deadline["id"]]
    assert deadline_chunks
    assert all(item["metadata"]["tokenizer_mode"] == "e5" for item in deadline_chunks)
    assert all(item["metadata"]["chunker_version"] == CHUNKER_VERSION for item in deadline_chunks)
    assert any(item["metadata"]["section_path"] == "Master's Deadlines" for item in deadline_chunks)
    revalidated, _ = _revalidate_existing_corpus(result)
    retained_policies = [item for item in revalidated
                         if item.get("classifier_scope") == "graduate_school_policy"]
    assert len(retained_policies) == 6
    assert all(item["facts"] for item in retained_policies)


def test_force_refresh_school_recollects_only_selected_school_and_preserves_others(monkeypatch):
    import opportunity_agent.v2.research.web as web_module

    draft = generate_draft()
    draft["as_of"] = "2026-10-05"
    draft["cases"] = [{**case, "as_of": "2026-10-05"} for case in draft["cases"]]
    duke_programs = [item for item in draft["programs"] if item["school_code"] == "duke"]
    other_program = next(item for item in draft["programs"] if item["school_code"] == "cmu")
    other_source = {"id": "src-cmu-frozen", "program_id": other_program["id"],
                    "url": "https://www.cmu.edu/frozen", "title": "CMU frozen page", "facts": []}
    other_document = {"id": "chunk-cmu-frozen", "source_id": other_source["id"], "text": "unchanged"}
    stale_duke_sources, stale_duke_documents = [], []
    for program in duke_programs:
        deadline_url = DUKE_GRADUATE_POLICY_PAGES[program["id"]][0]["url"]
        source_id = "src-" + hashlib.sha256((program["id"] + "|" + deadline_url).encode()).hexdigest()[:24]
        stale_duke_sources.append({"id": source_id, "program_id": program["id"],
                                   "url": deadline_url, "title": "Duke Graduate School Deadlines",
                                   "page_type": "admissions", "policy_fields": ["deadline"],
                                   "facts": [{"field": "deadline", "value": "old"}]})
        stale_duke_documents.append({"id": f"chunk-old-{program['id']}", "source_id": source_id,
                                     "text": ("Master's Deadlines\nComputer Science | 02/01/2027\n"
                                              "Electrical and Computer Engineering | 01/16/2027\n" + "details " * 30)})
    rejected_duke = {"program_id": duke_programs[0]["id"], "url": "https://cs.duke.edu/ms-cs-41-program-duke-undergraduates",
                     "status": "wrong_program_or_audience", "classifier_version": "programme-identity-v4-parent-policy-scope"}
    draft.update({"corpus_status": "collecting_pending_human_review",
                  "sources": [other_source, *stale_duke_sources, rejected_duke],
                  "documents": [other_document, *stale_duke_documents]})

    page_texts = {
        "application-deadlines": (
            "Application deadlines for fall 2027 graduate studies. Master's Deadlines\n"
            "Computer Science | 02/01/2027\nElectrical and Computer Engineering | 01/16/2027\n"
            "These dates are listed by The Graduate School for master's programs."),
        "gre-scores": ("GRE Testing Requirements by Program. GRE Required\nComputer Science (MS)\n"
                       "GRE Optional\nComputer Science (Ph.D.)\nElectrical and Computer Engineering (Ph.D., MS). "
                       "These lists detail test requirements for Graduate School programs."),
        "english-language-proficiency-test-scores": (
            "If your first language is not English, you must submit scores from either TOEFL, IELTS, "
            "or Duolingo English Test. To be eligible for an English language test waiver, you must have "
            "studied fulltime for two years or more at an eligible university. This policy applies to "
            "applicants to The Graduate School."),
    }
    search_domains = []

    class FakeTavily:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def search(self, _query, domains):
            search_domains.extend(domains)
            return {"results": []}

        async def read(self, url, _domains):
            key = next(key for key in page_texts if key in url)
            # Simulate a canonical URL change during refresh. The refreshed
            # source should replace the old page by program + policy field,
            # rather than leaving both URL versions active.
            canonical_url = url + "?canonical=refreshed" if key == "application-deadlines" else url
            return {"url": canonical_url, "title": "The Graduate School Policy", "text": page_texts[key]}

    monkeypatch.setattr(web_module, "TavilyMCP", lambda **_: FakeTavily())
    result = asyncio.run(collect_corpus(draft, school_codes={"duke"}, force_refresh=True,
                                        tokenizer=FakeE5Tokenizer()))

    assert result["as_of"] == date.today().isoformat()
    assert {case["as_of"] for case in result["cases"]} == {date.today().isoformat()}
    assert search_domains and set(search_domains) == {"duke.edu"}
    assert next(item for item in result["sources"] if item["id"] == other_source["id"]) == other_source
    assert next(item for item in result["documents"] if item["id"] == other_document["id"]) == other_document
    refreshed_sources = {item["id"]: item for item in result["sources"] if item.get("id")}
    for stale_source in stale_duke_sources:
        refreshed = [item for item in refreshed_sources.values()
                     if item.get("program_id") == stale_source["program_id"]
                     and item.get("policy_fields") == stale_source.get("policy_fields")]
        assert len(refreshed) == 1
        assert refreshed[0]["title"] == "The Graduate School Policy"
        assert refreshed[0]["url"] != stale_source["url"] if stale_source["policy_fields"] == ["deadline"] else True
        assert stale_source["id"] not in refreshed_sources
    active_document_ids = {item["id"] for item in result["documents"]}
    assert not {item["id"] for item in stale_duke_documents} & active_document_ids
    assert rejected_duke in result["sources"]
    policy_sources = [item for item in result["sources"] if item.get("classifier_scope") == "graduate_school_policy"]
    assert len(policy_sources) == 6
    mscs = next(item for item in policy_sources if item["program_id"] == duke_programs[0]["id"]
                and item.get("policy_fields") == ["deadline"])
    assert mscs["facts"][0]["value"] == "2027-02-01"


def test_failed_force_refresh_preserves_existing_school_sources_and_checkpoint(monkeypatch):
    import opportunity_agent.v2.research.web as web_module

    draft = generate_draft()
    duke_program = next(item for item in draft["programs"] if item["school_code"] == "duke")
    old_source = {
        "id": "src-duke-policy-before-refresh", "program_id": duke_program["id"],
        "url": DUKE_GRADUATE_POLICY_PAGES[duke_program["id"]][0]["url"],
        "title": "Duke Graduate School Deadlines", "page_type": "admissions",
        "policy_fields": ["deadline"], "facts": [],
    }
    old_document = {"id": "chunk-duke-policy-before-refresh", "source_id": old_source["id"],
                    "text": "Master's Deadlines Computer Science | 02/01/2027 " + "graduate policy " * 20}
    draft.update({"corpus_status": "collecting_pending_human_review", "sources": [old_source],
                  "documents": [old_document]})

    class FailedTavily:
        async def __aenter__(self):
            raise OSError("simulated MCP connection failure")

    monkeypatch.setattr(web_module, "TavilyMCP", lambda **_: FailedTavily())
    checkpoints = []
    result = asyncio.run(collect_corpus(draft, school_codes={"duke"}, force_refresh=True,
                                        checkpoint=checkpoints.append, tokenizer=FakeE5Tokenizer()))

    assert any(source.get("id") == old_source["id"] for source in result["sources"])
    retained_documents = [document for document in result["documents"]
                          if document.get("source_id") == old_source["id"]]
    assert retained_documents
    assert all(document["metadata"]["chunker_version"] == CHUNKER_VERSION for document in retained_documents)
    assert sum(source.get("status") == "mcp_connect_failed" for source in result["sources"]) == 2
    assert checkpoints
    assert all(any(source.get("id") == old_source["id"] for source in snapshot["sources"])
               for snapshot in checkpoints)
    assert all(any(document.get("source_id") == old_source["id"]
                   and document.get("metadata", {}).get("chunker_version") == CHUNKER_VERSION
                   for document in snapshot["documents"]) for snapshot in checkpoints)
