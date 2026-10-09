"""Shared lazy models; scores are not factual probabilities."""
from __future__ import annotations

import json
import math
import os
import threading
import time
from functools import lru_cache


def cached_model_revision(model_name):
    from pathlib import Path
    try:
        from huggingface_hub import try_to_load_from_cache
        cached = try_to_load_from_cache(model_name, "config.json")
        if isinstance(cached, str):
            parts = Path(cached).parts
            if "snapshots" in parts:
                return parts[parts.index("snapshots") + 1]
    except ImportError:
        pass
    return None


class EmbeddingProvider:
    def __init__(self, model_name="intfloat/multilingual-e5-small"):
        self.model_name, self._model, self._tokenizer, self.last_error = model_name, None, None, None
        self.lock = threading.RLock()

    def model(self):
        with self.lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name, device=os.getenv("EMBEDDING_DEVICE", "cpu"),
                    local_files_only=os.getenv("RESEARCH_ALLOW_MODEL_DOWNLOAD", "0") != "1")
            return self._model

    def encode(self, texts, kind):
        try:
            with self.lock:
                result = self.model().encode([kind + ": " + text for text in texts], normalize_embeddings=True)
            self.last_error = None
            return [[float(x) for x in row] for row in result]
        except Exception as exc:
            self.last_error = type(exc).__name__
            return [None] * len(texts)

    def embed(self, text):
        return self.encode([text], "query")[0]

    def passages(self, texts):
        return self.encode(texts, "passage")

    def tokenizer(self):
        """Load the checkpoint's fast tokenizer without loading encoder weights."""
        with self.lock:
            if self._tokenizer is None:
                from transformers import AutoTokenizer
                tokenizer = AutoTokenizer.from_pretrained(
                    self.model_name,
                    use_fast=True,
                    local_files_only=os.getenv("RESEARCH_ALLOW_MODEL_DOWNLOAD", "0") != "1",
                )
                if not getattr(tokenizer, "is_fast", False):
                    raise RuntimeError(f"{self.model_name} tokenizer must support offset mappings")
                self._tokenizer = tokenizer
            return self._tokenizer

    @property
    def revision(self):
        return cached_model_revision(self.model_name)


@lru_cache(maxsize=2)
def shared_embedder():
    return EmbeddingProvider(os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-small"))


class Reranker:
    def __init__(self, model_name=None, device=None):
        self.model_name = model_name or os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
        self.device = device or os.getenv("RERANKER_DEVICE", "cuda")
        self.batch_size = int(os.getenv("RERANKER_BATCH_SIZE", "4"))
        if self.batch_size < 1:
            raise ValueError("RERANKER_BATCH_SIZE must be positive")
        self.last_batch_size = self.batch_size
        self._model = None
        self.lock = threading.RLock()

    def score(self, query, passages):
        import torch
        from sentence_transformers import CrossEncoder
        with self.lock:
            if self.device == "cuda" and not torch.cuda.is_available():
                raise RuntimeError("CUDA unavailable; install a CUDA-enabled PyTorch build")
            if self._model is None:
                self._model = CrossEncoder(self.model_name, device=self.device, max_length=512,
                    local_files_only=os.getenv("RESEARCH_ALLOW_MODEL_DOWNLOAD", "0") != "1")
                if self.device == "cuda":
                    self._model.model.half()
            batch = self.batch_size
            while True:
                try:
                    raw = self._model.predict([(query, text) for text in passages], batch_size=batch,
                        activation_fn=torch.nn.Identity(), show_progress_bar=False)
                    self.last_batch_size = batch
                    return [float(x) for x in raw]
                except torch.cuda.OutOfMemoryError:
                    if batch <= 1:
                        raise
                    batch //= 2
                    torch.cuda.empty_cache()

    def rerank(self, query, hits):
        started = time.perf_counter()
        original = [h.chunk_id for h in hits]
        try:
            scores = self.score(query, [h.title + "\n" + str(h.metadata.get("section_path", "")) + "\n" + h.content for h in hits])
            for hit, raw in zip(hits, scores, strict=True):
                if not math.isfinite(raw):
                    raise ValueError("Nonfinite reranker score")
                hit.rerank_score = raw
                hit.score = 1 / (1 + math.exp(-max(-80, min(80, raw))))
                hit.relevance_method = "cross_encoder"
            hits = sorted(hits, key=lambda h: (-h.score, h.chunk_id))
            import torch
            return hits, {"reranker": self.model_name, "reranker_revision": self.revision,
                          "device": self.device, "batch_size": self.last_batch_size,
                          "peak_gpu_bytes": torch.cuda.max_memory_allocated() if self.device == "cuda" and torch.cuda.is_available() else 0,
                          "rerank_before": original,
                          "rerank_after": [h.chunk_id for h in hits], "rerank_ms": (time.perf_counter() - started) * 1000}
        except Exception as exc:
            return hits, {"reranker_error": type(exc).__name__, "reranker": self.model_name,
                          "mode": "reranker_unavailable", "rerank_ms": (time.perf_counter() - started) * 1000}

    def warmup(self):
        import torch
        started = time.perf_counter()
        scores = self.score("machine learning curriculum", ["Courses include machine learning and neural networks."])
        return {"device": self.device, "model": self.model_name, "score": scores[0],
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "peak_gpu_bytes": torch.cuda.max_memory_allocated() if self.device == "cuda" else 0}

    @property
    def revision(self):
        return cached_model_revision(self.model_name)


@lru_cache(maxsize=1)
def shared_reranker():
    return Reranker()


def calibrated_threshold(model_name):
    path = os.getenv("RESEARCH_CALIBRATION_FILE", "")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return None
    if data.get("model") != model_name or data.get("method") != "cross_encoder" or data.get("split") != "dev":
        raise ValueError("Calibration must match model/method and be fitted on dev only")
    if data.get("revision") and data["revision"] != cached_model_revision(model_name):
        raise ValueError("Calibration revision differs from cached reranker weights")
    threshold = data.get("threshold")
    if threshold is not None and (not isinstance(threshold, (float, int)) or not 0 <= threshold <= 1):
        raise ValueError("Calibration threshold must be a sigmoid score in [0,1]")
    return threshold


def warmup_models():
    import torch
    ranker, embedder = shared_reranker(), shared_embedder()
    report = ranker.warmup()
    query = embedder.embed("machine learning curriculum")
    passage = embedder.passages(["Courses include machine learning and neural networks."])[0]
    if query is None or passage is None or len(query) != 384 or len(passage) != 384:
        raise RuntimeError("E5 warmup failed or output dimension is not 384: " + str(embedder.last_error))
    report.update(torch=torch.__version__, gpu=torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        embedding_model=embedder.model_name, embedding_dimension=len(query),
        query_norm=math.sqrt(sum(v*v for v in query)), passage_norm=math.sqrt(sum(v*v for v in passage)),
        embedding_revision=embedder.revision, reranker_revision=ranker.revision)
    return report


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description="Actual BGE CUDA and E5 prefix/dimension warmup")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = warmup_models()
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
