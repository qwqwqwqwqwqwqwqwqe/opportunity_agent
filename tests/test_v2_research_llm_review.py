from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opportunity_agent.v2.evaluation.research_annotation import create_app, save_judgment
from opportunity_agent.v2.evaluation.research_llm_export import build_llm_export
from opportunity_agent.v2.evaluation.research_llm_review import (
    export_results, import_results, review_state, save_review,
)


@pytest.fixture
def workspace():
    # Avoid pytest's restrictive Windows temporary-directory ACL handling.
    tmp_path = Path.cwd() / (".llm-review-test-" + uuid.uuid4().hex)
    tmp_path.mkdir()
    cases = [{"id": "q1", "category": "rag", "query": "ML courses?", "required_claims": ["ml"], "filters": {}},
             {"id": "q2", "category": "profile_match", "query": "Robotics?", "required_claims": ["robot"], "filters": {}}]
    docs = [{"id": "c1", "source_id": "s1", "url": "https://school.edu/", "title": "Courses", "text": "Machine learning courses", "metadata": {}},
            {"id": "c2", "source_id": "s1", "url": "https://school.edu/", "title": "Contact", "text": "Contact us", "metadata": {}}]
    data = {"version": "v1", "as_of": "2026-10-07", "cases": cases, "documents": docs, "sources": [{"id": "s1"}]}
    pool = {"dataset_version": "v1", "as_of": data["as_of"], "queries": {c["id"]: c["query"] for c in cases}, "pairs": [
        {"case_id": "q1", "chunk_id": "c1", "ranks": {"A": 1}},
        {"case_id": "q1", "chunk_id": "c2", "ranks": {"A": 2}},
        {"case_id": "q2", "chunk_id": "c2", "ranks": {"B": 1}}]}
    for name, item in (("draft.json", data), ("candidate-pool.json", pool)):
        (tmp_path / name).write_text(json.dumps(item), encoding="utf-8")
    bundle = build_llm_export(data, pool)
    bundle["input_hashes"] = {key: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        for key, name in (("draft_sha256", "draft.json"), ("candidate_pool_sha256", "candidate-pool.json"))}
    (tmp_path / "llm-evidence-review.json").write_text(json.dumps(bundle), encoding="utf-8")
    batches = []
    for batch in bundle["batches"]:
        judgments = []
        for chunk in batch["chunk_ids"]:
            positive = chunk == "c1"
            judgments.append({"chunk_id": chunk, "relevance": 2 if positive else 0,
                "supports_claims": ["ml"] if positive else [],
                "supporting_quotes": [{"claim_id": "ml", "quote": "Machine learning courses"}] if positive else [],
                "program_match": "exact", "intake_match": "unknown", "source_reliable": "yes",
                "needs_human_review": positive, "reason": "test", "label_origin": "llm_proposed"})
        batches.append({"batch_id": batch["batch_id"], "case_id": batch["case_id"], "judgments": judgments})
    result = {"schema_version": "research-evidence-llm-results-v1", "label_origin": "llm_proposed",
        "dataset_version": "v1", "input_hashes": bundle["input_hashes"], "status": "complete", "missing_batch_ids": [],
        "batches": batches, "summary": {"completed_pair_count": 3, "completed_batch_count": 2,
            "expected_pair_count": 3, "expected_batch_count": 2,
            "grade_counts": {"0": 2, "1": 0, "2": 1, "null": 0}, "needs_human_review_count": 1}}
    raw = json.dumps(result).encode()
    return tmp_path, result, raw


def test_import_is_idempotent_and_never_creates_human_labels(workspace):
    root, _, raw = workspace
    first = import_results(root, raw)
    assert import_results(root, raw)["import_id"] == first["import_id"]
    assert (root / "llm-snapshots" / (first["import_id"] + ".json")).read_bytes() == raw
    exported = export_results(root)
    assert exported["summary"] == {"total": 3, "human": 0, "llm_only": 3, "unresolved": 0}
    assert all(c["annotation_status"] != "human_reviewed" for c in exported["dataset"]["cases"])


def test_priority_overlap_and_random_are_deduplicated(workspace):
    root, _, raw = workspace
    import_results(root, raw)
    state = review_state(root, sample_size=2)
    assert state["total"] == 3
    assert state["proposal"]["relevance"] == 2
    assert state["filter_counts"] == {"positive": 1, "flagged": 1, "no_positive_top5": 1, "random": 2}
    assert review_state(root, filter="no_positive_top5")["case"]["id"] == "q2"
    assert review_state(root, filter="random", seed=4) == review_state(root, filter="random", seed=4)


def test_accept_modify_edit_history_and_provenance(workspace):
    root, _, raw = workspace
    import_results(root, raw)
    state = review_state(root, filter="positive")
    record = {"import_id": state["import_id"], "case_id": "q1", "chunk_id": "c1", "annotator": "alice", "action": "accept"}
    save_review(root, record)
    assert review_state(root, filter="positive")["done"]
    edit = review_state(root, case_id="q1", chunk_id="c1")
    assert edit["saved_review"]["label_origin"] == "human_reviewed"
    judgment = {**state["proposal"], "relevance": 1, "supports_claims": [], "supporting_quotes": []}
    save_review(root, {**record, "action": "modify", "judgment": judgment})
    exported = export_results(root)
    assert exported["human_labels"][0]["relevance"] == 1
    assert exported["machine_proposals"][0]["relevance"] == 2
    assert (root / "llm-snapshots" / (state["import_id"] + ".json")).read_bytes() == raw


def test_existing_human_preserved_and_conflicts_not_promoted(workspace):
    root, _, raw = workspace
    base = {"case_id": "q1", "chunk_id": "c1", "annotator": "old", "relevance": 1,
        "supports_claims": [], "program_match": "exact", "intake_match": "unknown", "source_reliable": "yes"}
    save_judgment(root, base)
    import_results(root, raw)
    assert review_state(root, filter="positive")["done"]
    assert export_results(root)["human_labels"][0]["relevance"] == 1
    save_judgment(root, {**base, "annotator": "other", "relevance": 0})
    state = review_state(root, filter="positive")
    assert state["existing_conflict"]
    assert export_results(root)["summary"]["unresolved"] == 1
    record = {"import_id": state["import_id"], "case_id": "q1", "chunk_id": "c1", "annotator": "alice", "action": "accept"}
    with pytest.raises(ValueError, match="confirmation"):
        save_review(root, record)
    save_review(root, {**record, "confirm_existing_override": True})
    assert export_results(root)["summary"]["unresolved"] == 0


def test_reject_missing_duplicate_and_bad_summary_before_snapshot(workspace):
    root, result, _ = workspace
    for mutate in [lambda x: x["batches"].pop(), lambda x: x["batches"].append(x["batches"][0]),
                   lambda x: x["summary"].update(completed_pair_count=4)]:
        bad = copy.deepcopy(result)
        mutate(bad)
        with pytest.raises(ValueError):
            import_results(root, json.dumps(bad).encode())
    assert not (root / "llm-snapshots").exists()


def test_reject_stale_data_and_altered_snapshot(workspace):
    root, _, raw = workspace
    identifier = import_results(root, raw)["import_id"]
    snapshot = root / "llm-snapshots" / (identifier + ".json")
    snapshot.write_bytes(b"altered")
    with pytest.raises(ValueError, match="integrity"):
        review_state(root)
    snapshot.write_bytes(raw)
    with (root / "draft.json").open("a") as f:
        f.write(" ")
    with pytest.raises(ValueError, match="changed"):
        review_state(root)


def test_frontend_import_review_export_endpoints(workspace):
    root, _, raw = workspace
    with TestClient(create_app(root)) as client:
        assert '/llm' in client.get('/').text
        assert '全部等级 2' in client.get('/llm').text
        assert client.post('/api/llm/import', content=raw, headers={'content-type': 'application/json'}).status_code == 200
        state = client.get('/api/llm/next').json()
        response = client.post('/api/llm/review', json={"import_id": state["import_id"], "case_id": "q1",
            "chunk_id": "c1", "annotator": "alice", "action": "accept"})
        assert response.status_code == 200
        assert len(client.get('/api/llm/export?kind=human').json()['labels']) == 1
        assert len(client.get('/api/llm/export?kind=machine').json()['labels']) == 3
        assert client.get('/api/llm/export?kind=merged').json()['summary']['human'] == 1


def test_human_quote_validation_and_changed_claim_guard(workspace):
    root, _, raw = workspace
    import_results(root, raw)
    state = review_state(root, filter="positive")
    judgment = {**state["proposal"], "supporting_quotes": [{"claim_id": "ml", "quote": "invented quote"}]}
    with pytest.raises(ValueError, match="verbatim"):
        save_review(root, {"case_id": "q1", "chunk_id": "c1", "annotator": "alice", "action": "modify", "judgment": judgment})
    assert export_results(root)["summary"]["human"] == 0


def test_snapshot_cannot_be_overwritten_on_reimport(workspace):
    root, _, raw = workspace
    result = import_results(root, raw)
    snapshot = root / "llm-snapshots" / (result["import_id"] + ".json")
    snapshot.write_bytes(b"altered externally")
    with pytest.raises(ValueError, match="altered"):
        import_results(root, raw)
    assert snapshot.read_bytes() == b"altered externally"


def test_missing_bundle_returns_json_error_for_import_and_review(workspace):
    root, _, raw = workspace
    (root / "llm-evidence-review.json").unlink()
    with TestClient(create_app(root)) as client:
        for response in (client.get('/api/llm/next'), client.post('/api/llm/import', content=raw)):
            assert response.status_code == 422
            assert str(root) in response.json()['detail']
            assert 'serve --dir' in response.json()['detail']


def test_default_dataset_directory_is_independent_of_working_directory():
    from opportunity_agent.v2.evaluation.research_dataset import DEFAULT_DIR
    assert DEFAULT_DIR.is_absolute()
    assert DEFAULT_DIR == Path(__file__).resolve().parents[1] / 'deliverables' / 'research' / 'real150'
