#!/usr/bin/env python3
"""Local embedding model for the Supabase `memories` brain — semantic search.

Uses fastembed BAAI/bge-small-en (384-dim, ONNX/CPU). No API key, no data egress,
no per-query cost. The model is loaded from the local fastembed cache in offline
mode, so query-time embedding never touches the network.

Why bge-small-en (v1.0, not v1.5): huggingface.co is DNS-blocked on this host, so
the model was fetched from the Qdrant GCS bucket (storage.googleapis.com, the only
model host that resolves here). Only the non-v1.5 bge-small has a GCS `url` source;
same 384-dim architecture, fine for a small research corpus. To seed the cache on
a fresh machine:
    curl -sL https://storage.googleapis.com/qdrant-fastembed/BAAI-bge-small-en.tar.gz \\
      | tar xz -C ~/.cache/fastembed

Callers should treat embeddings as best-effort: catch EmbeddingUnavailable and fall
back to full-text search (see bot.supabase_helper.semantic_search_memories).
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

MODEL_NAME = os.getenv("BRAIN_EMBED_MODEL", "BAAI/bge-small-en")
EMBED_DIM = 384
CACHE_DIR = os.getenv("FASTEMBED_CACHE_PATH", str(Path.home() / ".cache" / "fastembed"))

_model = None  # lazy singleton


class EmbeddingUnavailable(RuntimeError):
    """Raised when the local embedding model can't be loaded (missing dep/model)."""


def _get_model():
    """Lazy-load the fastembed model from the local cache, offline."""
    global _model
    if _model is not None:
        return _model
    # Never let a load attempt hit the (blocked) HF network.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        from fastembed import TextEmbedding
    except Exception as e:  # pragma: no cover - env-dependent
        raise EmbeddingUnavailable(f"fastembed not installed: {e}") from e
    try:
        # 4 threads by default: fastembed otherwise uses every core, and this host runs a funded
        # trading process (brain spec R19).
        _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=CACHE_DIR,
                               threads=int(os.getenv("BRAIN_EMBED_THREADS", "4")))
    except Exception as e:
        raise EmbeddingUnavailable(
            f"could not load {MODEL_NAME} from {CACHE_DIR} (seed the cache — see module docstring): {e}"
        ) from e
    return _model


def is_available() -> bool:
    """True if the model can be loaded (dep present + cache seeded)."""
    try:
        _get_model()
        return True
    except EmbeddingUnavailable:
        return False


def embed_passages(texts: list[str]) -> list[list[float]]:
    """Embed documents (passage side of the bge asymmetric model)."""
    model = _get_model()
    return [v.tolist() for v in model.passage_embed(list(texts))]


def embed_query(text: str) -> list[float]:
    """Embed a search query (query side — bge prepends its retrieval instruction)."""
    model = _get_model()
    return list(next(iter(model.query_embed([text]))).tolist())


def to_pgvector(vec: list[float]) -> str:
    """Render a vector as the pgvector text literal '[f,f,...]'."""
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


def memory_passage_text(name: str, description: str, content: str, max_chars: int = 6000) -> str:
    """Compose the text embedded for a memory row (name + description + content).

    Truncated to a char budget; fastembed additionally truncates to the model's
    512-token window. Findings are summary-first, so the head carries the signal.
    """
    parts = [p.strip() for p in (name, description, content) if p and p.strip()]
    return "\n".join(parts)[:max_chars]


def content_hash(text: str) -> str:
    """Stable hash of the embedded text — lets backfill skip unchanged rows."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
