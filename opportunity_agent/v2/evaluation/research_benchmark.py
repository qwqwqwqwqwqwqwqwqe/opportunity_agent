"""Reproducible research ablations. Synthetic fixtures never represent measured production quality.

python -m opportunity_agent.v2.evaluation.research_benchmark --help
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from statistics import mean

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from ..db.base import Base
from ..db.models import OfficialSource, KnowledgeDocument, KnowledgeChunk
from ..rag.models import shared_embedder, shared_reranker
from ..rag.retrieval import HybridRetriever, lexical_text, tokens
from ..research.rewrite import query_rewrites
from .database import default_eval_database_url, validate_eval_database_url
from .corpus import deduplicate_documents
from .metrics import retrieval_metrics, paired_bootstrap, calibrate_threshold, answer_metrics, set_metrics


def fixture_dataset():
    """150 disjoint synthetic question families, 50 dev/100 test; human review required for real-world gold."""
    cases, documents = [], []
    categories = ["sql"] * 30 + ["rag"] * 40 + ["hybrid"] * 40 + ["mcp_web"] * 20 + ["negative"] * 20
    for i, category in enumerate(categories):
        identifier = f"q{i:03}"
        school, program = f"Fixture University {i:03}", f"MS Test{i:03}"
        prefix = f"{school} {program} 2027 Fall"
        suffix = {"sql": "GRE policy and deadline", "rag": "machine learning curriculum research",
                  "hybrid": "GRE optional deadline after 2026-12-01 machine learning curriculum",
                  "mcp_web": "latest official GRE policy deadline", "negative": "scholarship guarantee"}[category]
        expected = []
        content = [f"{prefix}. Application deadline: December 10, 2026.",
                   f"{prefix}. GRE is optional.",
                   f"{prefix}. Courses include machine learning, neural networks and AI research.",
                   f"{school} ECE undergraduate degree. GRE is required. This is a different programme."]
        for j, text in enumerate(content):
            cid = f"{identifier}-c{j}"
            relevant = ((category in {"sql", "mcp_web"} and j in {0, 1}) or (category == "rag" and j == 2)
                        or (category == "hybrid" and j in {0, 1, 2}))
            if relevant:
                expected.append(cid)
            documents.append({"id": cid, "source_id": f"s{i}", "url": f"https://fixture{i}.example.edu/ms",
                "title": prefix, "text": text, "metadata": {"school": school, "program": program,
                "intake": "2027 Fall", "program_match": "exact" if j < 3 else "rejected",
                "retrieved_at": "2026-10-04", "expires_at": "2027-10-04", "fixture": True}})
        cases.append({"id": identifier, "group": f"family-{i}", "split": "dev" if i % 3 == 0 else "test",
            "category": category, "query": prefix + " " + suffix, "as_of": "2026-10-04",
            "filters": {"school": school, "program": program, "intake": "2027 Fall"},
            "relevant_ids": expected, "gold_programs": [] if category == "negative" else [f"p{i}"],
            "required_claims": expected, "expected_route": "rag" if category == "negative" else category,
            "expected_outcome": "abstain" if category == "negative" else "answer",
            "target_count": 1, "annotation_status": "synthetic_exact_not_human_gold"})
    return {"version": "research-fixture-v1", "synthetic": True, "as_of": "2026-10-04",
            "cases": cases, "documents": documents}


def validate_dataset(data):
    groups, projects, gold_projects, ids = {}, {}, {}, set()
    docs = {d["id"] for d in data["documents"]}
    for case in data["cases"]:
        if case["id"] in ids or case["split"] not in {"dev", "test"}:
            raise ValueError("Duplicate case or invalid split")
        ids.add(case["id"])
        prior = groups.setdefault(case["group"], case["split"])
        if prior != case["split"]:
            raise ValueError("Question family leaks across splits")
        project = tuple(str(case.get("filters", {}).get(k, "")).casefold() for k in ("school", "program", "intake"))
        if any(project) and projects.setdefault(project, case["split"]) != case["split"]:
            raise ValueError("Project leaks across splits")
        if not data.get("synthetic") and case.get("annotation_status") != "human_reviewed":
            raise ValueError("Real gold cases require human_reviewed annotation_status")
        for programme in case.get("gold_programs", []):
            if gold_projects.setdefault(programme, case["split"]) != case["split"]:
                raise ValueError("Gold programme leaks across splits")
        if not set(case["relevant_ids"]) <= docs:
            raise ValueError("Unknown gold evidence ID")
        if not data.get("synthetic"):
            judgments = case.get("relevance_judgments", {})
            if any(value not in {0, 1, 2} for value in judgments.values()):
                raise ValueError("Real gold relevance judgments must use 0/1/2")
            if set(case["relevant_ids"]) != {key for key, value in judgments.items() if value == 2}:
                raise ValueError("relevant_ids must exactly match grade-2 judgments")


class FixtureEmbedder:
    """Token hashing for plumbing tests ONLY. Not E5 and not production performance."""
    model_name = "fixture-hash-384"
    last_error = None

    def embed(self, text):
        vector = [0.] * 384
        for word in tokens(text):
            vector[int(hashlib.sha256(word.encode()).hexdigest()[:8], 16) % 384] += 1
        norm = math.sqrt(sum(x*x for x in vector)) or 1
        return [x / norm for x in vector]

    def passages(self, texts):
        return [self.embed(text) for text in texts]


class FixtureReranker:
    model_name = "fixture-token-overlap"

    def rerank(self, query, hits):
        for hit in hits:
            hit.score = len(tokens(query) & tokens(hit.content)) / max(1, len(tokens(query)))
            hit.relevance_method = "fixture"
            hit.rerank_score = hit.score
        return sorted(hits, key=lambda h: (-h.score, h.chunk_id)), {"reranker": self.model_name, "fixture": True}


def _passage_text(item):
    """Give E5/lexical retrieval the page title and section context, not only body text."""
    metadata = item.get("metadata", {})
    title = str(item.get("title") or metadata.get("page_title") or "").strip()
    section = str(metadata.get("section_path", "")).strip()
    context = []
    if title:
        context.append("Title: " + title)
    if section:
        context.append("Section: " + section)
    context.append(str(item.get("text", "")))
    return "\n".join(context)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def index_dataset(data, url, embedder):
    # Dedupe before embedding and before opening a database transaction.
    documents = deduplicate_documents(data["documents"])
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                await connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
            await connection.run_sync(Base.metadata.create_all)
            if connection.dialect.name == "postgresql":
                await connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_research_fts "
                    "ON knowledge_chunks USING GIN "
                    "(to_tsvector('simple'::regconfig, COALESCE(metadata ->> 'lexical_text', content)))"
                )
        ids = [item["id"] for item in documents]
        existing_by_id = {}
        if ids:
            async with async_sessionmaker(engine)() as session:
                rows = await session.execute(select(KnowledgeChunk, KnowledgeDocument)
                    .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
                    .where(KnowledgeChunk.id.in_(ids)))
                existing_by_id = {chunk.id: (chunk, document) for chunk, document in rows.all()}
        pending = []
        embedding_revision = getattr(embedder, "revision", None)
        for item in documents:
            existing = existing_by_id.get(item["id"])
            if existing:
                chunk, document = existing
                if (document.metadata_json.get("embedding_model") != embedder.model_name
                        or document.metadata_json.get("embedding_revision") != embedding_revision
                        or document.metadata_json.get("embedding_context_version") != "title-section-body-v1"
                        or chunk.content != item["text"]):
                    raise ValueError("Benchmark database contains a different corpus/model; use a new --database")
            else:
                pending.append(item)
        vectors = await asyncio.to_thread(embedder.passages, [_passage_text(item) for item in pending]) if pending else []
        if len(vectors) != len(pending) or any(vector is None for vector in vectors):
            raise RuntimeError("Embedding unavailable: benchmark cannot label a lexical fallback as vector-only")
        async with async_sessionmaker(engine, expire_on_commit=False).begin() as session:
            for item, vector in zip(pending, vectors, strict=True):
                source = await session.get(OfficialSource, item["source_id"])
                if source is None:
                    source = await session.scalar(select(OfficialSource).where(OfficialSource.url == item["url"]))
                if source is None:
                    source = OfficialSource(id=item["source_id"], source_key=item["source_id"], university=item["metadata"]["school"],
                        url=item["url"], title=item["title"], status="verified")
                    session.add(source)
                    await session.flush()
                digest = hashlib.sha256((item["id"] + item["text"]).encode()).hexdigest()
                session.add(KnowledgeDocument(id=item["id"], source_id=source.id, url=item["url"], title=item["title"],
                    authority="official", source_type="fixture" if data.get("synthetic") else "official", content_hash=digest,
                    metadata_json={**item["metadata"], "embedding_model": embedder.model_name,
                                   "embedding_revision": embedding_revision,
                                   "embedding_context_version": "title-section-body-v1"}))
                await session.flush()
                session.add(KnowledgeChunk(id=item["id"], document_id=item["id"], chunk_index=0, content=item["text"],
                    embedding=vector, metadata_json={**item["metadata"],
                        "lexical_text": lexical_text(_passage_text(item))}))
        return engine
    except BaseException:
        await engine.dispose()
        raise


async def run_ablation(data, database, split, fixture_models=False):
    validate_dataset(data)
    validate_eval_database_url(database, require_postgresql=not data.get("synthetic", False))
    embedder, reranker = (FixtureEmbedder(), FixtureReranker()) if fixture_models else (shared_embedder(), shared_reranker())
    engine = await index_dataset(data, database, embedder)
    records, calibration = [], []
    try:
        async with async_sessionmaker(engine)() as session:
            retriever = HybridRetriever(session, embedder, reranker)
            for case in data["cases"]:
                if case["split"] != split:
                    continue
                # SQL/MCP route quality is evaluated separately; ablation compares text retrieval only.
                if case["category"] not in {"rag", "profile_match", "hybrid", "negative"}:
                    continue
                variants, latencies = {}, {}
                started = time.perf_counter()
                a, a_trace = await retriever.candidates(case["query"], case["filters"], mode="vector", as_of=case["as_of"])
                latencies["A"] = (time.perf_counter() - started) * 1000
                started = time.perf_counter()
                b, b_trace = await retriever.candidates(case["query"], case["filters"], as_of=case["as_of"])
                latencies["B"] = (time.perf_counter() - started) * 1000
                if a_trace["embedding_error"] or b_trace["embedding_error"]:
                    raise RuntimeError("Embedding failure invalidates the experiment")
                started = time.perf_counter()
                c, c_trace = await asyncio.to_thread(reranker.rerank, case["query"], copy.deepcopy(b))
                latencies["C"] = latencies["B"] + (time.perf_counter() - started) * 1000
                if c_trace.get("reranker_error"):
                    raise RuntimeError("Reranker failure invalidates B/C comparison")
                started = time.perf_counter()
                d, d_trace = await retriever.search(case["query"], limit=50, filters=case["filters"], rerank=True,
                    rewrites=query_rewrites(case["query"]), as_of=case["as_of"])
                latencies["D"] = (time.perf_counter() - started) * 1000
                if d_trace.get("reranker_error"):
                    raise RuntimeError("Reranker failure invalidates D")
                for name, hits in [("A", a), ("B", b), ("C", c), ("D", d)]:
                    variants[name] = {"ranked_ids": [h.chunk_id for h in hits], "latency_ms": latencies[name]}
                assert set(variants["B"]["ranked_ids"]) == set(variants["C"]["ranked_ids"])
                records.append({"id": case["id"], "category": case["category"], "gold": case["relevant_ids"], "variants": variants})
                if split == "dev":
                    calibration.extend({"case_id": case["id"], "split": "dev", "score": h.score,
                        "label": 2 if h.chunk_id in case["relevant_ids"] else 0} for h in c)
    finally:
        await engine.dispose()
    def summarise(rows):
        output = {}
        for name in "ABCD":
            ranks, gold = [r["variants"][name]["ranked_ids"] for r in rows], [set(r["gold"]) for r in rows]
            timing = sorted(r["variants"][name]["latency_ms"] for r in rows)
            output[name] = asdict(retrieval_metrics(ranks, gold))
            output[name].update(p50_ms=timing[len(timing)//2] if timing else None,
                                p95_ms=timing[min(len(timing)-1, int(.95*len(timing)))] if timing else None)
        return output
    summary = summarise(records)
    by_id = {case["id"]: case for case in data["cases"]}
    categories = {category: summarise([row for row in records if row["category"] == category])
                  for category in sorted({row["category"] for row in records})}
    tags = sorted({tag for row in records for tag in by_id[row["id"]].get("semantic_tags", [])})
    tag_summary = {tag: summarise([row for row in records if tag in by_id[row["id"]].get("semantic_tags", [])]) for tag in tags}
    differences = {}
    for before, after in [("B", "C"), ("C", "D"), ("A", "D")]:
        gold_cases = [r for r in records if r["gold"]]
        get = lambda name: [len(set(r["variants"][name]["ranked_ids"][:5]) & set(r["gold"])) / len(r["gold"]) for r in gold_cases]
        if gold_cases:
            differences[before + "_to_" + after] = paired_bootstrap(get(before), get(after))
    return {"dataset": data["version"], "dataset_sha256": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
            "split": split, "fixture_models": fixture_models, "synthetic": data.get("synthetic", False),
            "embedding_model": embedder.model_name, "reranker_model": reranker.model_name,
            "embedding_revision": getattr(embedder, "revision", None), "reranker_revision": getattr(reranker, "revision", None),
            "summary": summary, "summary_by_category": categories, "summary_by_semantic_tag": tag_summary,
            "recall_differences": differences, "records": records,
            "gpu_peak_bytes": __import__("torch").cuda.max_memory_allocated() if not fixture_models and __import__("torch").cuda.is_available() else 0,
            "calibration_records": calibration,
            "answer_metrics": "Requires independently annotated answer records; not inferred from retrieval."}


def score_annotated_answers(data, annotations):
    if any("variant" in record for record in annotations):
        names = sorted({record.get("variant", "unspecified") for record in annotations})
        return {"variants": {name: score_annotated_answers(data, [
            {k: v for k, v in record.items() if k != "variant"}
            for record in annotations if record.get("variant", "unspecified") == name]) for name in names}}
    by_id = {c["id"]: c for c in data["cases"]}
    output = []
    for record in annotations:
        case = by_id[record["case_id"]]
        metrics = answer_metrics(claims=record["claims"], required_claims=case["required_claims"],
            correct_claims=record["correct_claims"], supported_claims=record["supported_claims"],
            citation_pairs=record["citation_pairs"], valid_pairs=record["valid_pairs"])
        programmes = set_metrics(record["programs"], case["gold_programs"])
        correct_programs = set(record["programs"]) & set(case["gold_programs"])
        success = (record.get("outcome") == case["expected_outcome"] and not record["programs"] and not record["claims"]
            ) if case["expected_outcome"] in {"abstain", "needs_user"} else (
            len(correct_programs) >= (case["target_count"] or 0)
            and (programmes["precision"] == 1 or not record["programs"] and not case["gold_programs"])
            and metrics["fact_recall"] == 1 and metrics["fact_precision"] == 1 and metrics["citation_coverage"] == 1
            and metrics["citation_precision"] == 1 and metrics["answer_faithfulness"] == 1)
        output.append({"case_id": case["id"], "round": record["round"], **metrics,
                       "program_precision": programmes["precision"], "program_recall": programmes["recall"],
                       "task_success": int(success)})
    def success_rate(round_filter):
        rows = [r for r in output if round_filter(r)]
        return mean(r["task_success"] for r in rows) if rows else None
    final_rounds = {cid: max(r["round"] for r in output if r["case_id"] == cid) for cid in {r["case_id"] for r in output}}
    last = [r for r in output if r["round"] == final_rounds[r["case_id"]]]
    metric_names = ["program_precision", "program_recall", "fact_precision", "fact_recall",
        "citation_precision", "citation_coverage", "answer_faithfulness"]
    summary = {name: mean(r[name] for r in last if r[name] is not None)
        if any(r[name] is not None for r in last) else None for name in metric_names}
    return {"records": output, "evaluated_case_count": len(final_rounds), "answer_summary": summary,
            "first_round_success": success_rate(lambda r: r["round"] == 1),
            "final_success": success_rate(lambda r: r["round"] == final_rounds[r["case_id"]])}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("command", choices=["prepare", "run", "calibrate", "score-answers"])
    parser.add_argument("--dataset", default="deliverables/research/dataset.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--input")
    parser.add_argument("--database", default=default_eval_database_url())
    parser.add_argument("--split", choices=["dev", "test"], default="test")
    parser.add_argument("--fixture-models", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        output = fixture_dataset()
    elif args.command == "calibrate":
        report = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if report["split"] != "dev" or report["fixture_models"] or report.get("synthetic"):
            raise ValueError("Production calibration requires real-model, human-labelled dev scores")
        output = {**calibrate_threshold(report["calibration_records"]), "model": report["reranker_model"],
                  "revision": report.get("reranker_revision"), "method": "cross_encoder", "split": "dev", "dataset_sha256": report["dataset_sha256"]}
    else:
        data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
        validate_dataset(data)
        if args.command == "run":
            output = asyncio.run(run_ablation(data, args.database, args.split, args.fixture_models))
        else:
            output = score_annotated_answers(data, json.loads(Path(args.input).read_text(encoding="utf-8")))
    write_json(args.output, output)
    if "summary" in output:
        rows = ["| Variant | Precision@5 | Recall@5 | Hit@5 | MRR@50 | Candidate Recall@50 | p95 ms |", "|---|---:|---:|---:|---:|---:|---:|"]
        for name, result in output["summary"].items():
            rows.append("| " + name + " | " + " | ".join(f"{result[k]:.4f}" for k in
                ["precision_at_5", "recall_at_5", "hit_at_5", "mrr", "candidate_recall_at_50", "p95_ms"]) + " |")
        Path(args.output).with_suffix(".md").write_text(
            f"Synthetic corpus: {output['synthetic']}; fixture models: {output['fixture_models']}\n\n" + "\n".join(rows), encoding="utf-8")
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
