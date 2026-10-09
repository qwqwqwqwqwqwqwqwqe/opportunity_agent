"""Official-only hybrid retrieval with a deterministic keyword fallback."""
from __future__ import annotations

import math
import asyncio
import re
from datetime import date
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, func, literal_column, or_
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import KnowledgeChunk, KnowledgeDocument, OfficialSource
from ..core.telemetry import span
from .models import EmbeddingProvider, shared_embedder, shared_reranker


TOKEN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.I)


def lexical_text(text: str) -> str:
    """Fixed zh-char-en-word-v1 tokenization, shared by indexing and querying."""
    return " ".join(TOKEN.findall(text.casefold()))


def tokens(text: str) -> set[str]:
    return {part.lower() for part in TOKEN.findall(text)}


def cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    divisor = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return numerator / divisor if divisor else 0.0


@dataclass
class RetrievalHit:
    chunk_id: str
    document_id: str
    title: str
    url: str
    content: str
    source_id: str | None
    score: float
    lexical_score: float
    vector_score: float | None
    metadata: dict[str, Any]
    rerank_score: float | None = None
    relevance_method: str = "unscored"


def rrf_fuse(rankings, limit=50):
    scores, hits = defaultdict(float), {}
    for ranked in rankings:
        seen = set()
        for rank, hit in enumerate(ranked, 1):
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            scores[hit.chunk_id] += 1 / (60 + rank)
            hits.setdefault(hit.chunk_id, hit)
    for key, hit in hits.items():
        hit.score = scores[key]
    return sorted(hits.values(), key=lambda h: (-h.score, h.chunk_id))[:limit]


class HybridRetriever:
    def __init__(self, session: AsyncSession, embedder: EmbeddingProvider | None = None, reranker=None) -> None:
        self.session, self.embedder = session, embedder or shared_embedder()
        self.reranker = reranker or shared_reranker()

    async def _postgres_candidates(self, query, vector, filters, mode, limit, as_of):
        base = (select(KnowledgeChunk, KnowledgeDocument, OfficialSource)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .join(OfficialSource, KnowledgeDocument.source_id == OfficialSource.id)
            .where(KnowledgeDocument.authority == "official", OfficialSource.status == "verified"))
        for key, value in filters.items():
            if key not in {"school", "program", "intake", "program_id"}:
                raise ValueError("Unsupported retrieval filter")
            if value:
                actual = func.lower(KnowledgeDocument.metadata_json[key].as_string())
                if key == "intake" and re.fullmatch(r"20\d{2}", value):
                    base = base.where(or_(actual == value, actual.like(value + " %")))
                else:
                    base = base.where(actual == value.casefold())
        base = base.where(or_(KnowledgeDocument.metadata_json["program_match"].as_string().is_(None),
                             KnowledgeDocument.metadata_json["program_match"].as_string() != "rejected"))
        base = base.where(or_(KnowledgeDocument.metadata_json["superseded"].as_boolean().is_(None),
                             KnowledgeDocument.metadata_json["superseded"].as_boolean().is_(False)))
        expires = KnowledgeDocument.metadata_json["expires_at"].as_string()
        base = base.where(or_(expires.is_(None), expires >= as_of))

        def make(row, score, kind):
            chunk, document, source = row[:3]
            meta = {**(document.metadata_json or {}), **(chunk.metadata_json or {}), "content_hash": document.content_hash}
            return RetrievalHit(chunk.id, document.id, document.title, document.url, chunk.content,
                source.id, float(score), float(score) if kind == "lexical" else 0,
                float(score) if kind == "vector" else None, meta, relevance_method=kind)

        vectors, lexical = [], []
        if vector is not None:
            distance = KnowledgeChunk.embedding.cosine_distance(vector)
            model = func.coalesce(KnowledgeChunk.metadata_json["embedding_model"].as_string(),
                                  KnowledgeDocument.metadata_json["embedding_model"].as_string())
            with span("rag.vector_search"):
                rows = (await self.session.execute(base.add_columns(distance.label("distance"))
                    .where(KnowledgeChunk.embedding.is_not(None), model == self.embedder.model_name)
                    .order_by(distance, KnowledgeChunk.id).limit(limit))).all()
            vectors = [make(row, 1 - row[3], "vector") for row in rows]
        if mode == "hybrid":
            text = func.coalesce(KnowledgeChunk.metadata_json["lexical_text"].as_string(), KnowledgeChunk.content)
            document = func.to_tsvector(literal_column("'simple'::regconfig"), text)
            tsquery = func.websearch_to_tsquery(literal_column("'simple'::regconfig"), " OR ".join(lexical_text(query).split()))
            rank = func.ts_rank_cd(document, tsquery)
            with span("rag.lexical_search"):
                rows = (await self.session.execute(base.add_columns(rank.label("rank"))
                    .where(document.op("@@")(tsquery)).order_by(rank.desc(), KnowledgeChunk.id).limit(limit))).all()
            lexical = [make(row, row[3], "lexical") for row in rows]
        with span("rag.fuse", vector_count=len(vectors), lexical_count=len(lexical)):
            return vectors if mode == "vector" else rrf_fuse([vectors, lexical], limit)

    async def candidates(self, query: str, filters=None, *, mode="hybrid", limit=50, as_of=None):
        if mode not in {"vector", "hybrid"}:
            raise ValueError("Unsupported retrieval mode")
        filters = filters or {}
        as_of = str(as_of or date.today())
        with span("rag.embed_query"):
            vector = await asyncio.to_thread(self.embedder.embed, query)
        if self.session.bind.dialect.name == "postgresql":
            results = await self._postgres_candidates(query, vector, filters, mode, limit, as_of)
            return results, {"mode": "vector" if mode == "vector" else "hybrid_rrf" if vector is not None else "keyword_fallback",
                             "candidate_ids": [h.chunk_id for h in results], "embedding_error": self.embedder.last_error,
                             "backend": "postgresql"}
        # Official channel is a hard policy boundary, not merely a prompt instruction.
        rows = list((await self.session.execute(
            select(KnowledgeChunk, KnowledgeDocument, OfficialSource)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .outerjoin(OfficialSource, KnowledgeDocument.source_id == OfficialSource.id)
            .where(KnowledgeDocument.authority == "official", OfficialSource.status == "verified")
            .order_by(KnowledgeChunk.id).limit(2000)
        )).all())
        query_terms = tokens(query)
        scored: list[RetrievalHit] = []
        for chunk, document, source in rows:
            meta = {**(document.metadata_json or {}), **(chunk.metadata_json or {}), "content_hash": document.content_hash}
            if meta.get("program_match") == "rejected" or document.metadata_json.get("superseded"):
                continue
            if meta.get("expires_at") and str(meta["expires_at"]) < as_of:
                continue
            if any(not (str(meta.get(key, "")).lower() == value.lower()
                        or (key == "intake" and re.fullmatch(r"20\d{2}", value)
                            and str(meta.get(key, "")).startswith(value + " ")))
                   for key, value in filters.items() if value):
                continue
            section = str(meta.get("section_path", ""))
            lexical = len(query_terms & tokens(chunk.content + " " + document.title + " " + section)) / max(1, len(query_terms))
            vector_score = (cosine(vector, chunk.embedding) if vector and isinstance(chunk.embedding, list)
                and meta.get("embedding_model") == self.embedder.model_name else None)
            # RRF-compatible rank fusion implemented below; this initial sort only makes ranks stable.
            scored.append(RetrievalHit(chunk.id, document.id, document.title, document.url, chunk.content,
                                       source.id if source else None, 0.0, lexical, vector_score, meta))
        lexical_rank = sorted((h for h in scored if h.lexical_score > 0), key=lambda h: (-h.lexical_score, h.chunk_id))[:limit]
        vector_rank = sorted((h for h in scored if h.vector_score is not None), key=lambda h: (-h.vector_score, h.chunk_id))[:limit]
        for hit in scored:
            hit.score = hit.vector_score if hit.vector_score is not None else hit.lexical_score
            hit.relevance_method = "vector" if hit.vector_score is not None else "lexical"
        with span("rag.fuse", vector_count=len(vector_rank), lexical_count=len(lexical_rank)):
            results = vector_rank if mode == "vector" else rrf_fuse([lexical_rank, vector_rank], limit)
        return results, {"mode": "vector" if mode == "vector" else "hybrid_rrf" if vector is not None else "keyword_fallback",
                         "embedding_error": self.embedder.last_error, "candidate_ids": [h.chunk_id for h in results],
                         "backend": "sqlite_bounded_fallback"}

    async def search(self, query, limit=5, filters=None, *, mode="hybrid", rerank=False, rewrites=(), as_of=None):
        hits, diagnostics = await self.candidates(query, filters, mode=mode, as_of=as_of)
        if rewrites:
            pools = [hits]
            for rewrite in list(rewrites)[:2]:
                more, _ = await self.candidates(rewrite, filters, mode=mode, as_of=as_of)
                pools.append(more)
            hits = rrf_fuse(pools)
            diagnostics["candidate_ids"] = [h.chunk_id for h in hits]
        if rerank and hits:
            with span("rag.rerank", candidate_count=len(hits)):
                hits, detail = await asyncio.to_thread(self.reranker.rerank, query, hits)
            diagnostics.update(detail)
        return hits[:limit], diagnostics
