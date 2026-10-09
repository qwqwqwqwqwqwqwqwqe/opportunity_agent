from __future__ import annotations

import asyncio
import copy
import os
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from opportunity_agent.v2.evaluation.database import (
    DEFAULT_EVAL_DATABASE_NAME,
    default_eval_database_url,
    validate_eval_database_url,
)


def test_default_research_evaluation_database_is_dedicated_postgresql(monkeypatch):
    monkeypatch.delenv("RESEARCH_EVAL_DATABASE_URL", raising=False)
    parsed = make_url(default_eval_database_url())
    assert parsed.get_backend_name() == "postgresql"
    assert parsed.database == DEFAULT_EVAL_DATABASE_NAME
    assert parsed.host == "127.0.0.1"


def test_real_evaluation_rejects_sqlite_and_business_database():
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        validate_eval_database_url("sqlite+aiosqlite:///pool.db")
    with pytest.raises(ValueError, match="dedicated database"):
        validate_eval_database_url("postgresql+asyncpg://user:pass@localhost:5432/opportunity_agent")
    assert validate_eval_database_url(
        "postgresql+asyncpg://user:pass@localhost:5432/opportunity_research_eval_smoke") == "postgresql"


def test_env_database_url_is_respected_but_still_guarded(monkeypatch):
    monkeypatch.setenv("RESEARCH_EVAL_DATABASE_URL",
                       "postgresql+asyncpg://user:pass@localhost:5432/opportunity_research_eval_custom")
    assert make_url(default_eval_database_url()).database == "opportunity_research_eval_custom"


def test_e5_tokenizer_load_does_not_load_embedding_weights(monkeypatch):
    from opportunity_agent.v2.rag.models import EmbeddingProvider

    class FastTokenizer:
        is_fast = True

    calls = []

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(model_name, **kwargs):
            calls.append((model_name, kwargs))
            return FastTokenizer()

    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoTokenizer=AutoTokenizer))
    embedder = EmbeddingProvider()
    tokenizer = embedder.tokenizer()
    assert tokenizer.is_fast
    assert embedder._model is None
    assert calls[0][0] == "intfloat/multilingual-e5-small"
    assert calls[0][1]["use_fast"] is True


def test_benchmark_embedding_context_contains_title_and_section():
    from opportunity_agent.v2.evaluation.research_benchmark import fixture_dataset, index_dataset

    class CapturingEmbedder:
        model_name = "test-embedder"
        revision = "test-revision"

        def __init__(self):
            self.inputs = []

        def passages(self, texts):
            self.inputs.extend(texts)
            return [[1.0] + [0.0] * 383 for _ in texts]

    async def run():
        data = fixture_dataset()
        item = data["documents"][0]
        item["title"] = "MSCS Curriculum"
        item["metadata"]["section_path"] = "Degree Requirements > AI Electives"
        data["documents"] = [item]
        embedder = CapturingEmbedder()
        engine = await index_dataset(data, "sqlite+aiosqlite:///:memory:", embedder)
        await engine.dispose()
        assert len(embedder.inputs) == 1
        assert "Title: MSCS Curriculum" in embedder.inputs[0]
        assert "Section: Degree Requirements > AI Electives" in embedder.inputs[0]
        assert item["text"] in embedder.inputs[0]

    asyncio.run(run())


def test_index_dataset_deduplicates_before_embedding_and_is_restartable(monkeypatch):
    from sqlalchemy import func, select
    from opportunity_agent.v2.db.models import KnowledgeChunk, KnowledgeDocument
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from opportunity_agent.v2.evaluation.research_benchmark import fixture_dataset, index_dataset
    import opportunity_agent.v2.evaluation.research_benchmark as benchmark_module

    class Embedder:
        model_name = "test-embedder"
        revision = "test-revision"

        def __init__(self):
            self.count = 0

        def passages(self, texts):
            self.count += len(texts)
            return [[1.0] + [0.0] * 383 for _ in texts]

    async def run():
        data = fixture_dataset()
        original = data["documents"][0]
        duplicate = copy.deepcopy(original)
        duplicate["metadata"]["page_type"] = "curriculum"
        data["documents"] = [original, duplicate, copy.deepcopy(original)]
        embedder = Embedder()
        engine = await index_dataset(data, "sqlite+aiosqlite:///:memory:", embedder)
        try:
            async with async_sessionmaker(engine)() as session:
                assert await session.scalar(select(func.count()).select_from(KnowledgeDocument)) == 1
                assert await session.scalar(select(func.count()).select_from(KnowledgeChunk)) == 1
            assert embedder.count == 1
            monkeypatch.setattr(benchmark_module, "create_async_engine", lambda url: engine)
            await index_dataset(data, "sqlite+aiosqlite:///:memory:", embedder)
            assert embedder.count == 1
            async with async_sessionmaker(engine)() as session:
                assert await session.scalar(select(func.count()).select_from(KnowledgeDocument)) == 1
                assert await session.scalar(select(func.count()).select_from(KnowledgeChunk)) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_index_dataset_rejects_conflicting_chunk_before_embedding():
    from opportunity_agent.v2.evaluation.research_benchmark import fixture_dataset, index_dataset

    data = fixture_dataset()
    original = data["documents"][0]
    conflicting = {**original, "text": "Different evidence under the same ID"}
    data["documents"] = [original, conflicting]
    with pytest.raises(ValueError, match="Conflicting documents"):
        asyncio.run(index_dataset(data, "sqlite+aiosqlite:///:memory:", None))


@pytest.mark.skipif(not os.getenv("RESEARCH_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL test DB required")
def test_duplicate_indexing_and_repeated_run_on_postgresql():
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from opportunity_agent.v2.db.models import KnowledgeChunk, KnowledgeDocument
    from opportunity_agent.v2.evaluation.research_benchmark import FixtureEmbedder, FixtureReranker, fixture_dataset, index_dataset
    from opportunity_agent.v2.rag.retrieval import HybridRetriever

    async def run():
        url = os.environ["RESEARCH_TEST_DATABASE_URL"]
        validate_eval_database_url(url)
        # Keep smoke vectors separate from the real150 evaluation corpus.
        assert make_url(url).database.endswith("dedup_smoke")
        data = fixture_dataset()
        original = data["documents"][0]
        duplicate = copy.deepcopy(original)
        duplicate["metadata"]["page_type"] = "research"
        data["documents"] = [original, duplicate, copy.deepcopy(original)]
        embedder = FixtureEmbedder()
        for _ in range(2):
            engine = await index_dataset(data, url, embedder)
            try:
                async with async_sessionmaker(engine)() as session:
                    for model in (KnowledgeDocument, KnowledgeChunk):
                        assert await session.scalar(select(func.count()).select_from(model)
                                                    .where(model.id == original["id"])) == 1
                    retriever = HybridRetriever(session, embedder, FixtureReranker())
                    filters = {key: original["metadata"][key] for key in ("school", "program", "intake")}
                    hits, detail = await retriever.candidates("machine learning curriculum", filters,
                                                            as_of=data["cases"][0]["as_of"])
                    assert detail["backend"] == "postgresql"
                    assert [hit.chunk_id for hit in hits] == [original["id"]]
            finally:
                await engine.dispose()

    asyncio.run(run())
