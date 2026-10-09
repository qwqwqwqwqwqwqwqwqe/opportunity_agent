"""Isolated machine proposals, auditable human review, and provenance exports."""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .research_llm_export import resolve_llm_batch, validate_llm_response


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _connect(root):
    from .research_annotation import connect
    db = connect(root)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS llm_imports (
      id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, imported_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS llm_proposals (
      import_id TEXT NOT NULL, case_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
      payload TEXT NOT NULL, PRIMARY KEY(import_id,case_id,chunk_id));
    CREATE TABLE IF NOT EXISTS llm_human_reviews (
      import_id TEXT NOT NULL, case_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
      annotator TEXT NOT NULL, payload TEXT NOT NULL, action TEXT NOT NULL,
      updated_at TEXT NOT NULL, PRIMARY KEY(import_id,case_id,chunk_id));
    CREATE TABLE IF NOT EXISTS llm_review_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT, import_id TEXT NOT NULL,
      case_id TEXT NOT NULL, chunk_id TEXT NOT NULL, annotator TEXT NOT NULL,
      payload TEXT NOT NULL, action TEXT NOT NULL, updated_at TEXT NOT NULL);
    """)
    return db


def _context(root):
    missing = [str((root / name).resolve()) for name in
               ("llm-evidence-review.json", "draft.json", "candidate-pool.json") if not (root / name).is_file()]
    if missing:
        raise ValueError("审核数据文件不存在，请检查 serve --dir：" + "; ".join(missing))
    bundle = _read(root / "llm-evidence-review.json")
    for filename, key in (("draft.json", "draft_sha256"), ("candidate-pool.json", "candidate_pool_sha256")):
        if hashlib.sha256((root / filename).read_bytes()).hexdigest() != bundle["input_hashes"][key]:
            raise ValueError("Dataset/pool changed since LLM export; regenerate proposals before reviewing")
    return bundle, _read(root / "draft.json"), _read(root / "candidate-pool.json")


def import_results(root: Path, raw: bytes):
    bundle, _, _ = _context(root)
    result = json.loads(raw.decode("utf-8-sig"))
    if (result.get("schema_version") != "research-evidence-llm-results-v1"
            or result.get("label_origin") != "llm_proposed"):
        raise ValueError("Expected machine-proposal result schema and label origin")
    if result.get("input_hashes") != bundle["input_hashes"] or result.get("dataset_version") != bundle["dataset_version"]:
        raise ValueError("Result provenance does not match the exported task")
    batches = result.get("batches", [])
    ids = [x["batch_id"] for x in batches]
    if len(ids) != len(set(ids)) or set(ids) != {x["batch_id"] for x in bundle["batches"]}:
        raise ValueError("Import requires every batch exactly once")
    records = []
    for batch in batches:
        checked = validate_llm_response(resolve_llm_batch(bundle, batch["batch_id"]), batch)
        records.extend((batch["case_id"], j) for j in checked["judgments"])
    counts = Counter(str(j["relevance"]) if j["relevance"] is not None else "null" for _, j in records)
    summary = result.get("summary", {})
    if (result.get("status") != "complete" or result.get("missing_batch_ids")
            or summary.get("completed_pair_count") != len(records)
            or summary.get("completed_batch_count") != len(batches)
            or summary.get("expected_pair_count") != len(records)
            or summary.get("expected_batch_count") != len(batches)
            or summary.get("grade_counts") != {k: counts[k] for k in ("0", "1", "2", "null")}
            or summary.get("needs_human_review_count") != sum(j["needs_human_review"] for _, j in records)):
        raise ValueError("Result summary is inconsistent or incomplete")
    identifier = hashlib.sha256(raw).hexdigest()
    directory = root / "llm-snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = directory / (identifier + ".json")
    # Content-addressed, exclusive creation: never overwrite an existing snapshot.
    try:
        with snapshot.open("xb") as stream:
            stream.write(raw)
    except FileExistsError:
        if snapshot.read_bytes() != raw:
            raise ValueError("Immutable snapshot was altered")
    db = _connect(root)
    try:
        with db:
            db.execute("INSERT OR IGNORE INTO llm_imports VALUES (?,?,?)",
                       (identifier, snapshot.name, datetime.now(timezone.utc).isoformat()))
            db.executemany("INSERT OR IGNORE INTO llm_proposals VALUES (?,?,?,?)",
                [(identifier, case, j["chunk_id"], json.dumps(j, ensure_ascii=False)) for case, j in records])
    finally:
        db.close()
    return {"import_id": identifier, "pairs": len(records), "snapshot": str(snapshot), "grades": dict(counts)}


def _load(root, import_id=None):
    bundle, dataset, pool = _context(root)
    db = _connect(root)
    try:
        row = db.execute("SELECT * FROM llm_imports WHERE id=?", (import_id,)).fetchone() if import_id else db.execute(
            "SELECT * FROM llm_imports ORDER BY imported_at DESC LIMIT 1").fetchone()
        if row is None:
            raise ValueError("Import LLM results first")
        identifier = row["id"]
        raw = (root / "llm-snapshots" / row["snapshot"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != identifier:
            raise ValueError("Immutable snapshot integrity check failed")
        proposals = {(r["case_id"], r["chunk_id"]): json.loads(r["payload"]) for r in db.execute(
            "SELECT * FROM llm_proposals WHERE import_id=?", (identifier,))}
        reviews = {(r["case_id"], r["chunk_id"]): dict(r) for r in db.execute(
            "SELECT * FROM llm_human_reviews WHERE import_id=?", (identifier,))}
        legacy = defaultdict(list)
        for r in db.execute("SELECT * FROM judgments ORDER BY updated_at"):
            legacy[(r["case_id"], r["chunk_id"])].append(dict(r))
    finally:
        db.close()
    return identifier, bundle, dataset, pool, proposals, reviews, legacy


def _legacy(rows):
    adjudications = [r for r in rows if r["is_adjudication"]]
    if adjudications:
        rows = adjudications[-1:]
    if not rows:
        return None, False
    signatures = {(r["relevance"], r["supports_claims"], r["program_match"], r["intake_match"], r["source_reliable"]) for r in rows}
    if len(signatures) > 1:
        return None, True
    r = rows[-1]
    return {"chunk_id": r["chunk_id"], "relevance": r["relevance"],
        "supports_claims": json.loads(r["supports_claims"]), "supporting_quotes": [],
        "program_match": r["program_match"], "intake_match": r["intake_match"],
        "source_reliable": r["source_reliable"], "reason": r["notes"],
        "needs_human_review": False, "label_origin": "human_existing",
        "annotator": r["annotator"], "updated_at": r["updated_at"]}, False


def _priorities(dataset, pool, proposals, sample_size=100, seed=20261007):
    cases = {c["id"]: c for c in dataset["cases"]}
    positive_cases = {c for (c, _), j in proposals.items() if j["relevance"] == 2}
    random_pool = sorted(k for k, j in proposals.items() if j["relevance"] in {0, 1})
    sampled = set(random.Random(seed).sample(random_pool, min(sample_size, len(random_pool))))
    ranks = {(p["case_id"], p["chunk_id"]): p.get("ranks", {}) for p in pool["pairs"]}
    groups = {}
    for key, j in proposals.items():
        tags = []
        if j["relevance"] == 2:
            tags.append("positive")
        if j["needs_human_review"] or j["relevance"] is None:
            tags.append("flagged")
        if (key[0] not in positive_cases and cases[key[0]]["category"] in {"rag", "profile_match"}
                and any(0 < rank <= 5 for method, rank in ranks.get(key, {}).items() if method in {"A", "B", "C", "D"})):
            tags.append("no_positive_top5")
        if key in sampled:
            tags.append("random")
        groups[key] = tags
    return groups


def review_state(root, *, filter="priority", import_id=None, sample_size=100, seed=20261007, case_id=None, chunk_id=None):
    identifier, bundle, dataset, pool, proposals, reviews, legacy = _load(root, import_id)
    if filter not in {"priority", "positive", "flagged", "no_positive_top5", "random", "all"}:
        raise ValueError("Unknown review filter")
    if not 0 <= sample_size <= 2000:
        raise ValueError("Sample size must be 0..2000")
    tags = _priorities(dataset, pool, proposals, sample_size, seed)
    def completed(key):
        human, conflict = _legacy(legacy[key])
        return key in reviews or (human is not None and not conflict)
    keys = [k for k in proposals if filter == "all" or (bool(tags[k]) if filter == "priority" else filter in tags[k])]
    order = {"positive": 0, "flagged": 1, "no_positive_top5": 2, "random": 3}
    keys.sort(key=lambda k: (min((order[t] for t in tags[k]), default=4), k))
    counts = {name: sum(name in t for t in tags.values()) for name in order}
    key = (case_id, chunk_id) if case_id and chunk_id else next((k for k in keys if not completed(k)), None)
    state = {"import_id": identifier, "total": len(keys), "completed": sum(completed(k) for k in keys),
             "filter_counts": counts, "done": key is None}
    if key is not None:
        if key not in proposals:
            raise ValueError("Unknown review pair")
        human, conflict = _legacy(legacy[key])
        state.update(case={**next(c for c in dataset["cases"] if c["id"] == key[0]),
                           "query": bundle["cases"][key[0]]["query"]},
            document=bundle["documents"][key[1]], proposal=proposals[key], tags=tags[key],
            existing_human=human, existing_conflict=conflict,
            saved_review=json.loads(reviews[key]["payload"]) if key in reviews else None)
    return state


def save_review(root, record):
    identifier, bundle, _, _, proposals, _, legacy = _load(root, record.get("import_id"))
    key = (record.get("case_id"), record.get("chunk_id"))
    if key not in proposals or not str(record.get("annotator", "")).strip():
        raise ValueError("Unknown pair or missing annotator")
    action = record.get("action")
    if action not in {"accept", "modify"}:
        raise ValueError("Action must be accept or modify")
    existing, conflict = _legacy(legacy[key])
    if (existing or conflict) and not record.get("confirm_existing_override"):
        raise ValueError("Explicit confirmation required to supersede existing human labels")
    judgment = dict(proposals[key] if action == "accept" else record.get("judgment", {}))
    if judgment.get("chunk_id") != key[1] or judgment.get("relevance") is None:
        raise ValueError("Human review must select a definite grade for this chunk")
    judgment["label_origin"] = "llm_proposed"  # validate using the shared strict schema
    request = {"input": {"batch_id": "human-review", **bundle["cases"][key[0]],
                         "evidence": [bundle["documents"][key[1]]]}}
    validate_llm_response(request, {"batch_id": "human-review", "case_id": key[0], "judgments": [judgment]})
    judgment["label_origin"] = "human_reviewed"
    judgment["review_notes"] = str(record.get("notes", ""))
    now = datetime.now(timezone.utc).isoformat()
    values = (identifier, *key, record["annotator"].strip(), json.dumps(judgment, ensure_ascii=False), action, now)
    db = _connect(root)
    try:
        with db:
            db.execute("INSERT INTO llm_review_history (import_id,case_id,chunk_id,annotator,payload,action,updated_at) VALUES (?,?,?,?,?,?,?)", values)
            db.execute("INSERT OR REPLACE INTO llm_human_reviews VALUES (?,?,?,?,?,?,?)", values)
    finally:
        db.close()
    return {"saved": True}


def export_results(root, import_id=None):
    identifier, bundle, dataset, pool, proposals, reviews, legacy = _load(root, import_id)
    human, machine, merged, unresolved = [], [], [], []
    by_case = defaultdict(dict)
    for key, proposal in sorted(proposals.items()):
        item = {"case_id": key[0], "import_id": identifier, **proposal}
        machine.append(item)
        previous, conflict = _legacy(legacy[key])
        if key in reviews:
            row = reviews[key]
            item = {"case_id": key[0], "import_id": identifier, **json.loads(row["payload"]),
                    "annotator": row["annotator"], "review_action": row["action"], "updated_at": row["updated_at"]}
        elif previous is not None:
            item = {"case_id": key[0], "import_id": identifier, **previous}
        elif conflict:
            item = {**item, "label_origin": "human_conflict", "relevance": None, "needs_human_review": True}
        if item["label_origin"] in {"human_reviewed", "human_existing"}:
            human.append(item)
        if item["relevance"] is None:
            unresolved.append({"case_id": key[0], "chunk_id": key[1]})
        merged.append(item)
        by_case[key[0]][key[1]] = item
    cases = []
    for case in dataset["cases"]:
        if case["id"] not in by_case:
            continue
        labels = by_case[case["id"]]
        cases.append({**case, "query": bundle["cases"][case["id"]]["query"],
            "relevance_judgments": {k: j["relevance"] for k, j in labels.items() if j["relevance"] is not None},
            "relevant_ids": [k for k, j in labels.items() if j["relevance"] == 2],
            "label_origins": {k: j["label_origin"] for k, j in labels.items()},
            "annotation_status": "llm_assisted_not_human_gold",
            "answerability_status": "positive_evidence_found" if any(j["relevance"] == 2 for j in labels.values()) else "needs_answerability_review"})
    return {"schema_version": "research-llm-reviewed-export-v1", "import_id": identifier,
        "input_hashes": bundle["input_hashes"], "summary": {"total": len(merged), "human": len(human),
            "llm_only": sum(j["label_origin"] == "llm_proposed" for j in merged), "unresolved": len(unresolved)},
        "human_labels": human, "machine_proposals": machine, "effective_labels": merged,
        "unresolved_pairs": unresolved, "dataset": {**dataset, "cases": cases,
            "annotation_summary": {"human_reviewed": False, "label_source": "human_plus_llm_proposals",
                "recall_scope": "annotated_candidate_pool", "requires_answerability_review": True}}}
