"""Candidate pooling, local annotation UI, and guarded gold export."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request

from ..rag.models import shared_embedder, shared_reranker
from ..rag.retrieval import HybridRetriever
from ..research.rewrite import query_rewrites
from .database import validate_eval_database_url
from .research_benchmark import FixtureEmbedder, FixtureReranker, index_dataset
from .research_dataset import (CHUNKER_VERSION, TOKENIZER_MODEL, SCHOOLS, archive_removed_review_items,
                               review_queue, validate_draft, write_json)


SEMANTIC_CATEGORIES = {"rag", "profile_match", "hybrid", "negative"}


def _read(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _double_required(case_id: str, chunk_id: str) -> bool:
    return int(hashlib.sha256(f"{case_id}|{chunk_id}".encode()).hexdigest()[:8], 16) % 5 == 0


def _merge_candidate_pool(previous: dict, refreshed: dict, replaced_case_ids: set[str]) -> dict:
    if previous.get("dataset_version") != refreshed.get("dataset_version"):
        raise ValueError("Cannot incrementally update a candidate pool from a different dataset version")
    if previous.get("fixture_models") != refreshed.get("fixture_models"):
        raise ValueError("Cannot mix fixture and real-model candidate rankings")
    merged = dict(refreshed)
    merged["pairs"] = ([pair for pair in previous.get("pairs", [])
                         if pair.get("case_id") not in replaced_case_ids]
                        + refreshed.get("pairs", []))
    merged["queries"] = {**{case_id: query for case_id, query in previous.get("queries", {}).items()
                            if case_id not in replaced_case_ids}, **refreshed.get("queries", {})}
    return merged


def _archive_obsolete_candidate_pool(path: Path, dataset_version: str) -> str | None:
    """Keep the previous candidate rankings when a new chunk/schema version replaces them."""
    if not path.exists():
        return None
    previous = _read(path, {})
    old_version = str(previous.get("dataset_version", ""))
    if not old_version or old_version == dataset_version:
        return None
    safe_version = re.sub(r"[^A-Za-z0-9_.-]+", "_", old_version)
    archive = path.with_name(f"{path.stem}.{safe_version}{path.suffix}")
    suffix = 2
    while archive.exists():
        archive = path.with_name(f"{path.stem}.{safe_version}-{suffix}{path.suffix}")
        suffix += 1
    shutil.copy2(path, archive)
    return str(archive)


async def build_pool(draft_path: Path, output_path: Path, database: str, fixture_models: bool = False,
                     school_codes: set[str] | None = None) -> dict:
    validate_eval_database_url(database, require_postgresql=True)
    data = _read(draft_path)
    data = _apply_accepted_query_edits(data, draft_path.parent)
    validate_draft(data)
    if not data.get("documents"):
        raise ValueError("Collect and review a frozen corpus before building a candidate pool")
    stale_chunks = [item["id"] for item in data["documents"]
                    if item.get("metadata", {}).get("chunker_version") != CHUNKER_VERSION
                    or item.get("metadata", {}).get("tokenizer_model") != TOKENIZER_MODEL]
    if stale_chunks:
        raise ValueError("Corpus has legacy chunks; run collect without --school to migrate all frozen pages first")
    selected_codes = {code.casefold() for code in school_codes} if school_codes else None
    schools_by_code = {school["code"]: school for school in SCHOOLS}
    unknown_codes = (selected_codes or set()) - set(schools_by_code)
    if unknown_codes:
        raise ValueError(f"unknown school code(s): {', '.join(sorted(unknown_codes))}")
    selected_names = {schools_by_code[code]["name"] for code in selected_codes or set()}
    semantic_cases = [case for case in data["cases"] if case["category"] in SEMANTIC_CATEGORIES
                      and (selected_codes is None or case.get("filters", {}).get("school") in selected_names)]
    if selected_codes and not semantic_cases:
        raise ValueError("No semantic cases found for the selected school code(s)")
    previous_pool = _read(output_path) if selected_codes else None
    if selected_codes and previous_pool is None:
        raise ValueError("A full candidate pool must exist before incrementally rebuilding a school")
    embedder, reranker = (FixtureEmbedder(), FixtureReranker()) if fixture_models else (shared_embedder(), shared_reranker())
    engine = await index_dataset(data, database, embedder)
    pairs = []
    documents = {d["id"]: d for d in data["documents"]}
    try:
        async with async_sessionmaker(engine)() as session:
            retriever = HybridRetriever(session, embedder, reranker)
            for case in semantic_cases:
                filters = case.get("filters", {})
                a, _ = await retriever.candidates(case["query"], filters, mode="vector", limit=50, as_of=case["as_of"])
                b, _ = await retriever.candidates(case["query"], filters, mode="hybrid", limit=50, as_of=case["as_of"])
                c, _ = await asyncio.to_thread(reranker.rerank, case["query"], copy.deepcopy(b))
                d, _ = await retriever.search(case["query"], limit=50, filters=filters, rerank=True,
                    rewrites=query_rewrites(case["query"]), as_of=case["as_of"])
                ranks = {}
                for name, hits in (("A", a), ("B", b), ("C", c), ("D", d)):
                    for rank, hit in enumerate(hits[:50], 1):
                        ranks.setdefault(hit.chunk_id, {})[name] = rank
                # Deterministic hard negatives: same school but a different programme/intake or rejected scope.
                hard = [item["id"] for item in data["documents"] if item["metadata"].get("school") == filters.get("school")
                        and (item["metadata"].get("program") != filters.get("program")
                             or item["metadata"].get("intake") != filters.get("intake")
                             or item["metadata"].get("program_match") == "rejected")][:10]
                for chunk_id in hard:
                    ranks.setdefault(chunk_id, {})["hard_negative"] = 1
                for chunk_id, variant_ranks in sorted(ranks.items()):
                    if chunk_id not in documents:
                        continue
                    pairs.append({"case_id": case["id"], "chunk_id": chunk_id, "ranks": variant_ranks,
                                  "double_required": False})
    finally:
        await engine.dispose()
    # Exactly 20%, selected reproducibly without exposing a model prediction to annotators.
    ordered = sorted(pairs, key=lambda p: hashlib.sha256(f"{p['case_id']}|{p['chunk_id']}".encode()).hexdigest())
    for pair in ordered[:round(len(ordered) * .20)]:
        pair["double_required"] = True
    pool = {"version": "research-candidate-pool-v2-postgresql-fts", "dataset_version": data["version"],
            "as_of": data["as_of"],
            "fixture_models": fixture_models,
            "retrieval_backend": "postgresql",
            "lexical_retrieval": "postgresql_full_text_search_simple",
            "vector_retrieval": "pgvector_cosine",
            "queries": {case["id"]: case["query"] for case in semantic_cases},
            "pairs": pairs}
    if selected_codes:
        if previous_pool.get("fixture_models") != fixture_models:
            raise ValueError("Cannot incrementally update a candidate pool from a different model mode")
        replaced_case_ids = {case["id"] for case in semantic_cases}
        pool = _merge_candidate_pool(previous_pool, pool, replaced_case_ids)
    else:
        _archive_obsolete_candidate_pool(output_path, data["version"])
    write_json(output_path, pool)
    return {"case_count": len({p['case_id'] for p in pairs}), "pair_count": len(pairs),
            "double_required": sum(p["double_required"] for p in pairs), "output": str(output_path)}


def connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "annotations.sqlite")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS judgments (
      case_id TEXT NOT NULL, chunk_id TEXT NOT NULL, annotator TEXT NOT NULL,
      relevance INTEGER NOT NULL CHECK(relevance BETWEEN 0 AND 2), supports_claims TEXT NOT NULL,
      program_match TEXT NOT NULL, intake_match TEXT NOT NULL, source_reliable TEXT NOT NULL,
      notes TEXT NOT NULL DEFAULT '', is_adjudication INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL, PRIMARY KEY(case_id, chunk_id, annotator)
    );
    CREATE TABLE IF NOT EXISTS reviews (
      item_id TEXT NOT NULL, stage TEXT NOT NULL, annotator TEXT NOT NULL,
      decision TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', notes TEXT NOT NULL DEFAULT '',
      updated_at TEXT NOT NULL, PRIMARY KEY(item_id, annotator)
    );
    CREATE TABLE IF NOT EXISTS review_history (
      revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
      item_id TEXT NOT NULL, stage TEXT NOT NULL, annotator TEXT NOT NULL,
      decision TEXT NOT NULL, payload TEXT NOT NULL, notes TEXT NOT NULL,
      updated_at TEXT NOT NULL, archived_at TEXT NOT NULL
    );
    """)
    return connection


def _apply_accepted_query_edits(data: dict, root: Path) -> dict:
    """Use a reviewed wording change consistently when ranking and exporting."""
    if not (root / "annotations.sqlite").exists():
        return data
    connection = connect(root)
    try:
        edits = {}
        for row in connection.execute("SELECT item_id, decision, payload FROM reviews WHERE stage='query'"):
            if row["decision"] != "accept":
                continue
            corrected = str(json.loads(row["payload"]).get("corrected_query", "")).strip()
            if corrected:
                edits[row["item_id"].removeprefix("query:")] = corrected
    finally:
        connection.close()
    if not edits:
        return data
    output = copy.deepcopy(data)
    for case in output.get("cases", []):
        corrected = edits.get(case["id"])
        if corrected and corrected != case["query"]:
            case["generated_query"] = case["query"]
            case["query"] = corrected
            case["query_edit_status"] = "human_edited_pending_export"
    return output


def _effective_judgment(rows: list[sqlite3.Row], double_required: bool):
    adjudicated = [r for r in rows if r["is_adjudication"]]
    if adjudicated:
        return adjudicated[-1]
    ordinary = [r for r in rows if not r["is_adjudication"]]
    if len(ordinary) < (2 if double_required else 1):
        return None
    labels = {r["relevance"] for r in ordinary}
    claims = {r["supports_claims"] for r in ordinary}
    return ordinary[0] if len(labels) == 1 and (labels != {2} or len(claims) == 1) else None


def annotation_state(root: Path, annotator: str, stage: str, secondary: bool = False) -> dict:
    dataset = _read(root / "draft.json", {})
    connection = connect(root)
    try:
        if stage == "evidence":
            pool = _read(root / "candidate-pool.json", {"pairs": []})
            pairs = pool["pairs"]
            if annotator.casefold().startswith("adjudicator"):
                all_rows = connection.execute("SELECT * FROM judgments ORDER BY updated_at").fetchall()
                grouped = defaultdict(list)
                for row in all_rows:
                    grouped[(row["case_id"], row["chunk_id"])].append(row)
                pairs = [p for p in pairs if p["double_required"] and len([
                    r for r in grouped[(p["case_id"], p["chunk_id"])] if not r["is_adjudication"]]) >= 2
                    and _effective_judgment(grouped[(p["case_id"], p["chunk_id"])], True) is None]
            elif secondary:
                pairs = [pair for pair in pairs if pair["double_required"]]
            active_pairs = {(pair["case_id"], pair["chunk_id"]) for pair in pairs}
            seen = {tuple(row) for row in connection.execute(
                "SELECT case_id, chunk_id FROM judgments WHERE annotator=?", (annotator,)).fetchall()} & active_pairs
            pending = next((p for p in pairs if (p["case_id"], p["chunk_id"]) not in seen), None)
            if pending is None:
                return {"stage": stage, "done": True, "completed": len(seen), "total": len(pairs)}
            case = next(c for c in dataset["cases"] if c["id"] == pending["case_id"])
            document = next(d for d in dataset["documents"] if d["id"] == pending["chunk_id"])
            return {"stage": stage, "done": False, "completed": len(seen), "total": len(pairs),
                    "item": pending, "case": case, "document": document}
        queue = _read(root / "review-queue.json", review_queue(dataset))
        items = [item for item in queue if item["stage"] == stage]
        active_ids = {item["id"] for item in items}
        seen = {row[0] for row in connection.execute("SELECT item_id FROM reviews WHERE annotator=? AND stage=?",
                                                     (annotator, stage)).fetchall()} & active_ids
        pending = next((item for item in items if item["id"] not in seen), None)
        return {"stage": stage, "done": pending is None, "completed": len(seen), "total": len(items), "item": pending}
    finally:
        connection.close()


def reviewed_item_state(root: Path, annotator: str, stage: str, item_id: str) -> dict:
    """Reload a previously saved non-evidence review so it can be corrected."""
    if stage == "evidence":
        raise ValueError("Evidence judgments are edited through a separate workflow")
    dataset = _read(root / "draft.json", {})
    queue = _read(root / "review-queue.json", review_queue(dataset))
    item = next((candidate for candidate in queue
                 if candidate["stage"] == stage and candidate["id"] == item_id), None)
    archived = False
    if item is None:
        archive = _read(root / "review-archive.json", [])
        entry = next((candidate for candidate in archive
                      if candidate.get("item", {}).get("id") == item_id
                      and candidate.get("item", {}).get("stage") == stage), None)
        if entry:
            item = entry["item"]
            archived = True
    connection = connect(root)
    try:
        row = connection.execute("SELECT * FROM reviews WHERE item_id=? AND annotator=? AND stage=?",
                                 (item_id, annotator, stage)).fetchone()
        if item is None:
            raise ValueError("Review item is no longer active and has no archived snapshot")
        if row is None and not archived:
            raise ValueError("This annotator has not reviewed the requested item")
        active_ids = {candidate["id"] for candidate in queue if candidate["stage"] == stage}
        completed_ids = {result[0] for result in connection.execute(
            "SELECT item_id FROM reviews WHERE annotator=? AND stage=?", (annotator, stage)).fetchall()} & active_ids
        return {"stage": stage, "done": False, "completed": len(completed_ids),
                "total": len(active_ids),
                "item": item, "archived": archived,
                "archive_note": (("该条目已从当前数据集移除，只能查看/修改历史标注，不会进入本轮 gold 导出。"
                                  + (f" 移除原因：{entry.get('reason')}。" if entry.get("reason") else ""))
                                 if archived else ""),
                "saved_review": ({"decision": row["decision"], "payload": json.loads(row["payload"]),
                                  "notes": row["notes"]} if row else {}),
                "editing": row is not None}
    finally:
        connection.close()


def save_judgment(root: Path, record: dict) -> None:
    required = {"case_id", "chunk_id", "annotator", "relevance", "supports_claims",
                "program_match", "intake_match", "source_reliable"}
    if not required <= record.keys() or not str(record["annotator"]).strip():
        raise ValueError("Incomplete evidence judgment")
    relevance = int(record["relevance"])
    if relevance not in {0, 1, 2}:
        raise ValueError("Relevance must be 0, 1, or 2")
    if relevance == 2 and not record["supports_claims"]:
        raise ValueError("Direct support requires at least one claim")
    if relevance == 2 and (record["program_match"] != "exact" or record["intake_match"] == "no"
                           or record["source_reliable"] != "yes"):
        raise ValueError("Direct support requires an exact programme, non-conflicting intake, and reliable source")
    dataset = _read(root / "draft.json", {})
    case = next((item for item in dataset.get("cases", []) if item["id"] == record["case_id"]), None)
    if case is None or not set(record["supports_claims"]) <= set(case["required_claims"]):
        raise ValueError("Judgment contains an unknown case or claim")
    connection = connect(root)
    try:
        connection.execute("""INSERT OR REPLACE INTO judgments
          (case_id,chunk_id,annotator,relevance,supports_claims,program_match,intake_match,source_reliable,notes,is_adjudication,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (record["case_id"], record["chunk_id"], record["annotator"], relevance,
          json.dumps(record["supports_claims"], ensure_ascii=False), record["program_match"], record["intake_match"],
          record["source_reliable"], record.get("notes", ""), int(bool(record.get("is_adjudication"))),
          datetime.now(timezone.utc).isoformat()))
        connection.commit()
    finally:
        connection.close()


def save_review(root: Path, record: dict) -> None:
    if not all(record.get(key) for key in ("item_id", "stage", "annotator", "decision")):
        raise ValueError("Incomplete review")
    connection = connect(root)
    try:
        previous = connection.execute("SELECT * FROM reviews WHERE item_id=? AND annotator=?",
                                      (record["item_id"], record["annotator"])).fetchone()
        if previous is not None:
            connection.execute("""INSERT INTO review_history
              (item_id,stage,annotator,decision,payload,notes,updated_at,archived_at)
              VALUES (?,?,?,?,?,?,?,?)""", (previous["item_id"], previous["stage"],
              previous["annotator"], previous["decision"], previous["payload"], previous["notes"],
              previous["updated_at"], datetime.now(timezone.utc).isoformat()))
        connection.execute("""INSERT OR REPLACE INTO reviews
          (item_id,stage,annotator,decision,payload,notes,updated_at) VALUES (?,?,?,?,?,?,?)""",
          (record["item_id"], record["stage"], record["annotator"], record["decision"],
           json.dumps(record.get("payload", {}), ensure_ascii=False), record.get("notes", ""),
           datetime.now(timezone.utc).isoformat()))
        connection.commit()
    finally:
        connection.close()


def _review_map(connection, stage: str) -> dict[str, sqlite3.Row]:
    rows = connection.execute("SELECT * FROM reviews WHERE stage=? ORDER BY updated_at", (stage,)).fetchall()
    return {row["item_id"]: row for row in rows}


def validate_workspace(root: Path, *, require_complete: bool = False) -> dict:
    dataset = _read(root / "draft.json")
    basic = validate_draft(dataset)
    connection = connect(root)
    issues = []
    if not dataset.get("documents"):
        issues.append("corpus_empty")
    if dataset.get("collection_gaps"):
        issues.append("corpus_page_type_gaps")
    try:
        queue = _read(root / "review-queue.json", review_queue(dataset))
        reviews = {(row["item_id"], row["stage"]): row for row in connection.execute("SELECT * FROM reviews").fetchall()}
        for item in queue:
            row = reviews.get((item["id"], item["stage"]))
            if row is None:
                issues.append("missing_review:" + item["id"])
            elif item["stage"] in {"program", "query"} and row["decision"] != "accept":
                issues.append("required_item_not_accepted:" + item["id"])
            elif item["stage"] == "source" and row["decision"] not in {"accept", "reject"}:
                issues.append("source_review_unresolved:" + item["id"])
            elif item["stage"] == "gold" and row["decision"] not in {"accept", "reject"}:
                issues.append("gold_review_unresolved:" + item["id"])
            elif item["stage"] == "fact" and row["decision"] == "accept":
                review_payload = json.loads(row["payload"])
                if item["payload"]["fact"].get("value") is None and not review_payload.get("corrected_value"):
                    issues.append("accepted_fact_missing_value:" + item["id"])
        pool = _read(root / "candidate-pool.json", {"pairs": []})
        if pool.get("as_of") != dataset.get("as_of"):
            issues.append("candidate_pool_stale_for_as_of")
        reviewed_data = _apply_accepted_query_edits(dataset, root)
        reviewed_queries = {case["id"]: case["query"] for case in reviewed_data["cases"]}
        reviewed_categories = {case["id"]: case["category"] for case in reviewed_data["cases"]}
        pool_queries = pool.get("queries", {})
        for case_id, query in reviewed_queries.items():
            if reviewed_categories[case_id] in SEMANTIC_CATEGORIES and pool_queries.get(case_id) != query:
                issues.append("candidate_pool_stale_for_query:" + case_id)
        covered_cases = {pair["case_id"] for pair in pool["pairs"]}
        expected_cases = {case["id"] for case in dataset["cases"] if case["category"] in SEMANTIC_CATEGORIES}
        if covered_cases != expected_cases:
            issues.append("candidate_pool_missing_semantic_cases")
        document_ids = {document["id"] for document in dataset["documents"]}
        if any(pair["chunk_id"] not in document_ids for pair in pool["pairs"]):
            issues.append("candidate_pool_contains_removed_chunks")
        judgment_rows = connection.execute("SELECT * FROM judgments ORDER BY updated_at").fetchall()
        grouped = defaultdict(list)
        for row in judgment_rows:
            grouped[(row["case_id"], row["chunk_id"])].append(row)
        judged_pairs = len(grouped)
        unresolved = []
        for pair in pool["pairs"]:
            effective = _effective_judgment(grouped[(pair["case_id"], pair["chunk_id"])], pair["double_required"])
            if effective is None:
                unresolved.append(pair["case_id"] + ":" + pair["chunk_id"])
        issues += ["unresolved_evidence:" + value for value in unresolved]
        double_rows = []
        for pair in pool["pairs"]:
            if not pair["double_required"]:
                continue
            ordinary = [r for r in grouped[(pair["case_id"], pair["chunk_id"])] if not r["is_adjudication"]]
            if len(ordinary) >= 2:
                double_rows.append((ordinary[0]["relevance"], ordinary[1]["relevance"]))
        kappa = weighted_kappa(double_rows) if double_rows else None
        if require_complete and kappa is not None and kappa < .70:
            issues.append("weighted_kappa_below_0.70")
        result = {**basic, "review_items": len(queue), "candidate_pairs": len(pool["pairs"]),
                  "judged_pairs": judged_pairs, "unresolved_pairs": len(unresolved), "issues": issues}
        result["double_annotated_pairs"] = len(double_rows)
        result["weighted_kappa"] = kappa
        if require_complete and issues:
            raise ValueError(f"Annotation workspace is incomplete ({len(issues)} issues); run validate for details")
        return result
    finally:
        connection.close()


def validation_summary(result: dict, *, sample_size: int = 20) -> dict:
    """Keep progress responses useful without returning thousands of item IDs."""
    issues = result.get("issues", [])
    counts: dict[str, int] = defaultdict(int)
    for issue in issues:
        counts[issue.split(":", 1)[0]] += 1
    return {**{key: value for key, value in result.items() if key != "issues"},
            "issue_count": len(issues), "issue_counts": dict(sorted(counts.items())),
            "issue_sample": issues[:sample_size]}


def weighted_kappa(pairs: list[tuple[int, int]]) -> float:
    """Quadratic weighted Cohen's kappa for the three relevance labels."""
    if not pairs:
        raise ValueError("Kappa requires paired labels")
    observed = [[0] * 3 for _ in range(3)]
    left, right = [0] * 3, [0] * 3
    for a, b in pairs:
        observed[a][b] += 1
        left[a] += 1
        right[b] += 1
    total = len(pairs)
    disagreement = sum(((i - j) ** 2 / 4) * observed[i][j] for i in range(3) for j in range(3)) / total
    expected = sum(((i - j) ** 2 / 4) * left[i] * right[j] for i in range(3) for j in range(3)) / (total * total)
    return 1.0 if expected == 0 and disagreement == 0 else 0.0 if expected == 0 else 1 - disagreement / expected


def export_gold(root: Path) -> dict:
    validation = validate_workspace(root, require_complete=True)
    dataset = _read(root / "draft.json")
    pool = _read(root / "candidate-pool.json", {"pairs": []})
    connection = connect(root)
    try:
        program_reviews = _review_map(connection, "program")
        query_reviews = _review_map(connection, "query")
        source_reviews = _review_map(connection, "source")
        fact_reviews = _review_map(connection, "fact")
        gold_reviews = _review_map(connection, "gold")
        accepted_programs = {p["id"] for p in dataset["programs"]
                             if program_reviews["program:" + p["id"]]["decision"] == "accept"}
        rejected_sources = {key.removeprefix("source:") for key, row in source_reviews.items() if row["decision"] == "reject"}
        accepted_sources = {key.removeprefix("source:") for key, row in source_reviews.items() if row["decision"] == "accept"}
        documents = []
        for source_document in dataset["documents"]:
            if source_document["source_id"] in rejected_sources or source_document["source_id"] not in accepted_sources:
                continue
            document = copy.deepcopy(source_document)
            source_review = source_reviews.get("source:" + document["source_id"])
            if source_review is not None and source_review["decision"] == "accept":
                document["metadata"]["program_match"] = "exact"
                document["metadata"]["human_reviewed"] = True
            documents.append(document)
        document_ids = {d["id"] for d in documents}
        rows = connection.execute("SELECT * FROM judgments ORDER BY updated_at").fetchall()
        grouped = defaultdict(list)
        for row in rows:
            grouped[(row["case_id"], row["chunk_id"])].append(row)
        pairs_by_case = defaultdict(list)
        for pair in pool["pairs"]:
            pairs_by_case[pair["case_id"]].append(pair)
        cases = []
        reviewed_dataset = _apply_accepted_query_edits(dataset, root)
        reviewed_cases = {item["id"]: item for item in reviewed_dataset["cases"]}
        for source_case in dataset["cases"]:
            case = copy.deepcopy(reviewed_cases[source_case["id"]])
            query_review = query_reviews["query:" + case["id"]]
            query_payload = json.loads(query_review["payload"])
            case["expected_outcome"] = query_payload.get("expected_outcome", case["expected_outcome"])
            if case["expected_outcome"] in {"abstain", "needs_user"}:
                case["target_count"] = 0
            judgments, relevant = {}, []
            for pair in pairs_by_case[case["id"]]:
                if pair["chunk_id"] not in document_ids:
                    continue
                row = _effective_judgment(grouped[(case["id"], pair["chunk_id"])], pair["double_required"])
                judgments[pair["chunk_id"]] = row["relevance"]
                if row["relevance"] == 2:
                    relevant.append(pair["chunk_id"])
            candidates = [pid for pid in case.get("candidate_program_ids", []) if pid in accepted_programs]
            gold_review = gold_reviews.get("gold:" + case["id"])
            if gold_review:
                payload = json.loads(gold_review["payload"])
                gold_programs = payload.get("gold_programs", candidates if gold_review["decision"] == "accept" else [])
            else:
                gold_programs = [] if case["expected_outcome"] != "answer" else candidates
            case.update(relevance_judgments=judgments, relevant_ids=sorted(relevant), gold_programs=gold_programs,
                        query_origin="generated_human_reviewed", query_review_status="accepted",
                        annotation_status="human_reviewed")
            cases.append(case)
        accepted_facts = []
        for source in dataset.get("sources", []):
            if source.get("id") not in accepted_sources:
                continue
            for fact in source.get("facts", []):
                item_id = "fact:" + source["id"] + ":" + fact["id"]
                review = fact_reviews.get(item_id)
                if review and review["decision"] in {"accept", "conflict"}:
                    review_payload = json.loads(review["payload"])
                    value = review_payload.get("corrected_value", fact.get("value"))
                    accepted_facts.append({**fact, "value": value, "source_id": source["id"], "program_id": source["program_id"],
                                           "review_status": "verified" if review["decision"] == "accept" else "conflicting"})
        programs = []
        for source_program in dataset["programs"]:
            if source_program["id"] in accepted_programs:
                program = copy.deepcopy(source_program)
                program["review_status"] = "accepted"
                programs.append(program)
        gold = {**dataset, "version": dataset["version"] + "-gold", "query_source": "generated_human_reviewed",
                "programs": programs, "structured_facts": accepted_facts,
                "sources": [source for source in dataset.get("sources", []) if source.get("id") in accepted_sources],
                "cases": cases, "documents": documents, "annotation_summary": {
                    "candidate_pairs": len(pool["pairs"]), "human_reviewed": True,
                    "exported_at": datetime.now(timezone.utc).isoformat()}}
        write_json(root / "gold.json", gold)
        answer_reviews = _review_map(connection, "answer")
        labels = [json.loads(row["payload"]) for row in answer_reviews.values() if row["decision"] == "accept"]
        write_json(root / "answer-labels.json", labels)
        return {**validation, "gold": str(root / "gold.json"), "answer_labels": len(labels)}
    finally:
        connection.close()


HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>Research 150 标注</title>
<style>body{font:16px system-ui;max-width:1100px;margin:24px auto;padding:0 16px;background:#f6f8fb;color:#172033}
.card{background:white;padding:18px;border-radius:12px;margin:12px 0;box-shadow:0 2px 12px #ccd4e055}button{padding:10px 16px;margin:5px;border:0;border-radius:8px;cursor:pointer}
.b1{background:#ffd7d7}.b2{background:#fff0b8}.b3{background:#c9f1d5}pre{white-space:pre-wrap;max-height:360px;overflow:auto}label{margin-right:14px}.muted{color:#607086}input,select,textarea{padding:7px;margin:4px}</style></head><body>
<link rel="stylesheet" href="/annotation-assets/markdown.css"><script src="/annotation-assets/markdown.js"></script>
<h1>Research Agent 真实数据标注</h1><p><a href="/llm">LLM 预标注导入与重点人工复核</a></p><div class="card"><label>标注者 <input id="annotator" value="annotator-1"></label><label><input id="secondary" type="checkbox">只做20%双标</label>
<label>阶段 <select id="stage"><option selected>program</option><option>query</option><option>source</option><option>fact</option><option>gold</option><option>evidence</option><option>answer</option></select></label><button onclick="loadNext()">加载</button><button onclick="editPrevious()">修改上一条</button><span id="progress"></span></div>
<div id="content" class="card"></div><script>
let state=null; const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const reviewKey=()=>`research-annotation:last:${annotator.value.trim()}:${stage.value}`;
async function loadNext(){let a=annotator.value.trim(),s=stage.value;state=await (await fetch(`/api/next?annotator=${encodeURIComponent(a)}&stage=${s}&secondary=${secondary.checked}`)).json();
progress.textContent=` ${state.completed||0}/${state.total||0}`;if(state.done){content.innerHTML='<h2>这个阶段已完成</h2>';return} render();}
async function editPrevious(){let a=annotator.value.trim(),s=stage.value;if(s==='evidence'){alert('证据标注暂不支持“修改上一条”；请先使用非证据审核阶段。');return}let itemId=localStorage.getItem(reviewKey());if(!itemId){alert('当前标注者和阶段尚没有本浏览器保存的上一条审核。');return}let r=await fetch(`/api/item?annotator=${encodeURIComponent(a)}&stage=${encodeURIComponent(s)}&item_id=${encodeURIComponent(itemId)}`);if(!r.ok){let error=await r.json().catch(()=>({}));alert(error.detail||'上一条审核已不可用，请刷新审核队列。');return}state=await r.json();progress.textContent=` 正在修改已审核项（${state.completed||0}/${state.total||0}）`;render()}
function render(){if(state.stage==='evidence'){let c=state.case,d=state.document,i=state.item;content.innerHTML=`<h2>${esc(c.query)}</h2><p class=muted>${esc(c.id)} · ${esc(c.category)} · ranks ${esc(JSON.stringify(i.ranks))}${i.double_required?' · 双标':''}</p><h3>${esc(d.title)}</h3><p>${esc(d.metadata.school)} / ${esc(d.metadata.program)} / ${esc(d.metadata.intake)}</p><p class=muted>章节：${esc(d.metadata.section_path||'（页面开头/未标注章节）')}</p><p><a target=_blank href="${esc(d.url)}">${esc(d.url)}</a></p>${ChunkMarkdown.card(d.text)}<div id=claims>${c.required_claims.map(x=>`<label><input type=checkbox value="${esc(x)}">${esc(x)}</label>`).join('')}</div><p>
<label>项目<select id=pm><option value=exact>正确</option><option value=rejected>错误</option><option value=uncertain>不确定</option></select></label><label>入学季<select id=im><option value=yes>匹配</option><option value=no>不匹配</option><option value=unknown>未说明</option></select></label><label>来源<select id=sr><option value=yes>可靠</option><option value=no>不可靠</option><option value=unknown>不确定</option></select></label></p><button class=b1 onclick="judge(0)">1 无关</button><button class=b2 onclick="judge(1)">2 背景相关</button><button class=b3 onclick="judge(2)">3 直接支持</button>`}
else{let i=state.item,saved=state.saved_review||{},extra=state.stage==='fact'?'<label>确认/修正后的值 <input id=corrected placeholder="留空则采用自动抽取值"></label>':state.stage==='query'?'<label>预期结果 <select id=outcome><option>answer</option><option>abstain</option><option>needs_user</option></select></label><p><label>修订问题（仅允许不改变学校、项目、入学季、数量或事实条件的措辞修改）</label><br><textarea id=correctedQuery rows=4 cols=100></textarea></p>':'';let sourceHelp=state.stage==='source'?`<p class=muted>此页有 ${esc(i.payload.fact_candidate_count||0)} 条自动抽取的候选事实；它们不在 Source 阶段判定，须到 Fact 阶段逐条审核。</p>`:'';let archiveHelp=state.archived?`<p class=muted>${esc(state.archive_note||'该条目已归档，不会进入本轮 gold 导出。')}</p>`:'';content.innerHTML=`<h2>${esc(state.stage)} 审核${state.editing?'（修改已提交审核）':''}${state.archived?'（已移出当前数据集）':''}</h2><p class=muted>${esc(i.id)}</p>${archiveHelp}${sourceHelp}<pre>${esc(JSON.stringify(i.payload,null,2))}</pre><p>${extra}</p><textarea id=notes rows=3 cols=80 placeholder="冲突或备注（可选）"></textarea><br><button class=b3 onclick="review('accept')">接受</button><button class=b1 onclick="review('reject')">拒绝</button><button class=b2 onclick="review('conflict')">保留冲突/需澄清</button>`;notes.value=saved.notes||'';if(state.stage==='fact'&&saved.payload&&saved.payload.corrected_value)corrected.value=saved.payload.corrected_value;if(state.stage==='query'){outcome.value=(saved.payload&&saved.payload.expected_outcome)||i.payload.expected_outcome;correctedQuery.value=(saved.payload&&saved.payload.corrected_query)||i.payload.query}}}
async function judge(n){let claims=[...document.querySelectorAll('#claims input:checked')].map(x=>x.value);let body={case_id:state.item.case_id,chunk_id:state.item.chunk_id,annotator:annotator.value,relevance:n,supports_claims:claims,program_match:pm.value,intake_match:im.value,source_reliable:sr.value,is_adjudication:annotator.value.trim().toLowerCase().startsWith('adjudicator')};let r=await fetch('/api/judgments',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});if(!r.ok){alert(await r.text());return}loadNext()}
async function review(decision){let payload=state.stage==='answer'?state.item.payload:{};if(state.stage==='fact'&&corrected.value)payload.corrected_value=corrected.value;if(state.stage==='query'){payload.expected_outcome=outcome.value;let q=correctedQuery.value.trim();if(!q){alert('修订问题不能为空');return}if(q!==state.item.payload.query)payload.corrected_query=q}let body={item_id:state.item.id,stage:state.stage,annotator:annotator.value,decision,payload,notes:notes.value};let r=await fetch('/api/reviews',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});if(!r.ok){alert(await r.text());return}localStorage.setItem(reviewKey(),state.item.id);loadNext()}loadNext();
</script></body></html>'''


def create_app(root: Path):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse
    root = Path(root).resolve()
    app = FastAPI(title="Research 150 Annotation", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTML

    @app.get("/annotation-assets/{asset}")
    async def annotation_asset(asset: str):
        from fastapi.responses import Response
        assets = {"markdown.js": ("research_markdown.js", "application/javascript"),
                  "markdown.css": ("research_markdown.css", "text/css")}
        if asset not in assets:
            raise HTTPException(404, "Unknown annotation asset")
        filename, media_type = assets[asset]
        return Response(Path(__file__).with_name(filename).read_text(encoding="utf-8"),
                        media_type=media_type, headers={"Cache-Control": "no-cache"})

    @app.get("/llm", response_class=HTMLResponse)
    async def llm_index():
        return Path(__file__).with_name("research_llm_review.html").read_text(encoding="utf-8")

    @app.post("/api/llm/import")
    async def llm_import(request: Request):
        from .research_llm_review import import_results
        try:
            return import_results(root, await request.body())
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/llm/next")
    async def llm_next(filter: str = "positive", sample_size: int = 100, seed: int = 20261007,
                       import_id: str | None = None, case_id: str | None = None, chunk_id: str | None = None):
        from .research_llm_review import review_state
        try:
            return review_state(root, filter=filter, sample_size=sample_size, seed=seed,
                                import_id=import_id, case_id=case_id, chunk_id=chunk_id)
        except (ValueError, KeyError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/llm/review")
    async def llm_review(record: dict):
        from .research_llm_review import save_review as save_llm_review
        try:
            return save_llm_review(root, record)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/llm/export")
    async def llm_export(kind: str = "merged", import_id: str | None = None):
        from .research_llm_review import export_results
        if kind not in {"human", "machine", "merged"}:
            raise HTTPException(400, "Unknown export kind")
        try:
            output = export_results(root, import_id)
            if kind == "merged":
                return output
            return {"schema_version": output["schema_version"], "import_id": output["import_id"],
                    "input_hashes": output["input_hashes"], "label_source": kind,
                    "labels": output["human_labels" if kind == "human" else "machine_proposals"]}
        except (ValueError, KeyError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/next")
    async def next_item(annotator: str, stage: str = "evidence", secondary: bool = False):
        if stage not in {"program", "query", "source", "fact", "gold", "evidence", "answer"}:
            raise HTTPException(400, "Unknown stage")
        return annotation_state(root, annotator.strip(), stage, secondary)

    @app.get("/api/item")
    async def saved_item(annotator: str, stage: str, item_id: str):
        if stage not in {"program", "query", "source", "fact", "gold", "answer"}:
            raise HTTPException(400, "Unknown or unsupported stage")
        try:
            return reviewed_item_state(root, annotator.strip(), stage, item_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/judgments")
    async def judgments(record: dict):
        try:
            save_judgment(root, record)
            return {"saved": True}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/reviews")
    async def reviews(record: dict):
        try:
            save_review(root, record)
            return {"saved": True}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/progress")
    async def progress():
        return validation_summary(validate_workspace(root))
    return app


def serve(root: Path, host: str, port: int) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Annotation UI may bind only to loopback")
    import uvicorn
    uvicorn.run(create_app(root), host=host, port=port)
