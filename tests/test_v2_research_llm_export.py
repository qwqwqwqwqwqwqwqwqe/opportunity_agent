from __future__ import annotations

import copy
import io
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from opportunity_agent.v2.evaluation.research_llm_export import (
    build_llm_export,
    resolve_llm_batch,
    validate_llm_response,
)


def sample_data():
    document = {"id": "c1", "source_id": "s1", "url": "https://school.edu/courses", "title": "Curriculum",
                "text": "课程包括机器学习。\nStudents take machine learning and chip design.",
                "metadata": {"school": "School", "program": "MS ECE", "intake": "2027 Fall",
                             "official_domain": "school.edu", "section_path": "Courses > AI"}}
    other = {**copy.deepcopy(document), "id": "c2", "text": "Campus cafeteria hours."}
    case = {"id": "q1", "query": "有哪些机器学习课程？", "filters": {"program": "MS ECE"}, "language": "zh",
            "profile_context": {"research_interests": ["ML"]}, "required_claims": ["claim-ml"],
            "expected_outcome": "answer", "relevant_ids": ["c1"], "relevance_judgments": {"c1": 2}}
    dataset = {"version": "v2", "as_of": "2026-10-06", "cases": [case, {**case, "id": "q2"}],
               "documents": [document, other], "sources": [{"id": "s1", "url": document["url"],
                   "temporal_scope": "evergreen_pending_review", "content_hash": "frozen-body"}]}
    pool = {"dataset_version": "v2", "as_of": dataset["as_of"], "fixture_models": False,
            "queries": {"q1": case["query"], "q2": case["query"]}, "pairs": [
                {"case_id": "q1", "chunk_id": "c1", "ranks": {"A": 1, "B": 1}},
                {"case_id": "q1", "chunk_id": "c2", "ranks": {"hard_negative": 1}},
                {"case_id": "q2", "chunk_id": "c1", "ranks": {"C": 2}}]}
    return dataset, pool


def test_export_preserves_all_pairs_and_shared_text_without_labels_or_rank_hints():
    dataset, pool = sample_data()
    original = copy.deepcopy(dataset)
    bundle = build_llm_export(dataset, pool, batch_size=1,
                              reviews={"source:s1": {"decision": "accept"}})
    assert bundle["counts"] == {"query_count": 2, "pair_count": 3, "unique_chunk_count": 2,
                                "batch_count": 3, "batch_size": 1}
    restored = [(batch["case_id"], chunk) for batch in bundle["batches"] for chunk in batch["chunk_ids"]]
    assert sorted(restored) == sorted((p["case_id"], p["chunk_id"]) for p in pool["pairs"])
    serialized = json.dumps(bundle, ensure_ascii=False)
    for hidden in ("ranks", "hard_negative", "expected_outcome", "relevant_ids", "relevance_judgments"):
        assert hidden not in serialized
    assert bundle["documents"]["c1"]["text"] == dataset["documents"][0]["text"]
    assert bundle["documents"]["c1"]["source_context"]["human_source_review"] == "accept"
    assert dataset == original
    assert build_llm_export(dataset, pool, batch_size=1)["batches"][0]["batch_id"] == build_llm_export(
        dataset, pool, batch_size=1)["batches"][0]["batch_id"]


def test_export_resolves_self_contained_batch_and_splits_large_case():
    dataset, pool = sample_data()
    bundle = build_llm_export(dataset, pool, batch_size=1)
    request = resolve_llm_batch(bundle, bundle["batches"][1]["batch_id"])
    assert request["input"]["case_id"] == "q1"
    assert request["input"]["evidence"][0]["chunk_id"] == "c2"
    assert request["input"]["evidence"][0]["text"] == "Campus cafeteria hours."
    assert "judgments" in request["output_schema"]["properties"]
    with pytest.raises(ValueError, match="Unknown batch_id"):
        resolve_llm_batch(bundle, "missing")


def test_export_uses_reviewed_query_and_rejects_stale_candidate_pool():
    dataset, pool = sample_data()
    reviews = {"query:q1": {"decision": "accept", "payload": {"corrected_query": "请列出机器学习课程。"}}}
    with pytest.raises(ValueError, match="stale for reviewed query"):
        build_llm_export(dataset, pool, reviews=reviews)
    pool["queries"]["q1"] = "请列出机器学习课程。"
    assert build_llm_export(dataset, pool, reviews=reviews)["cases"]["q1"]["query"] == "请列出机器学习课程。"
    pool["dataset_version"] = "old"
    with pytest.raises(ValueError, match="stale for dataset"):
        build_llm_export(dataset, pool)


def test_export_rejects_duplicate_pairs_or_missing_evidence():
    dataset, pool = sample_data()
    pool["pairs"].append(copy.deepcopy(pool["pairs"][0]))
    with pytest.raises(ValueError, match="Duplicate candidate pair"):
        build_llm_export(dataset, pool)
    pool["pairs"][-1]["chunk_id"] = "missing"
    with pytest.raises(ValueError, match="missing case/chunk"):
        build_llm_export(dataset, pool)


def valid_response(request):
    return {"batch_id": request["input"]["batch_id"], "case_id": request["input"]["case_id"],
            "judgments": [{"chunk_id": item["chunk_id"], "relevance": 0, "supports_claims": [],
                "supporting_quotes": [], "program_match": "uncertain", "intake_match": "unknown",
                "source_reliable": "yes", "needs_human_review": False, "reason": "No direct support.",
                "label_origin": "llm_proposed"} for item in request["input"]["evidence"]]}


def test_response_checks_all_ids_claims_and_verbatim_quotes():
    dataset, pool = sample_data()
    request = resolve_llm_batch(build_llm_export(dataset, pool))
    response = valid_response(request)
    response["judgments"][0].update(relevance=2, program_match="exact", supports_claims=["claim-ml"],
                                   supporting_quotes=[{"claim_id": "claim-ml", "quote": "课程包括机器学习。"}])
    assert validate_llm_response(request, response)["judgments"][0]["relevance"] == 2
    response["judgments"][0]["supporting_quotes"][0]["quote"] = "Invented course requirement"
    with pytest.raises(ValueError, match="not verbatim"):
        validate_llm_response(request, response)
    response["judgments"].pop()
    with pytest.raises(ValueError, match="every requested chunk"):
        validate_llm_response(request, response)


def test_response_rejects_duplicate_labels_boolean_grades_and_false_human_origin():
    dataset, pool = sample_data()
    request = resolve_llm_batch(build_llm_export(dataset, pool))
    response = valid_response(request)
    response["judgments"][1] = copy.deepcopy(response["judgments"][0])
    with pytest.raises(ValueError, match="exactly once"):
        validate_llm_response(request, response)
    response = valid_response(request)
    response["judgments"][0]["relevance"] = True
    with pytest.raises(ValidationError):
        validate_llm_response(request, response)
    response["judgments"][0].update(relevance=0, label_origin="human_reviewed")
    with pytest.raises(ValidationError):
        validate_llm_response(request, response)


def test_llm_batch_cli_outputs_utf8_with_gbk_host(monkeypatch):
    from opportunity_agent.v2.evaluation.research_dataset import main

    dataset, pool = sample_data()
    dataset["documents"][0]["text"] += " © 🧠"
    bundle = build_llm_export(dataset, pool)
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: json.dumps(bundle))
    monkeypatch.setattr(sys, "argv", ["research_dataset", "llm-batch", "--input", "example.json"])
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="gbk")
    monkeypatch.setattr(sys, "stdout", stream)
    main()
    stream.flush()
    response = json.loads(buffer.getvalue().decode("utf-8"))
    assert "© 🧠" in response["input"]["evidence"][0]["text"]
