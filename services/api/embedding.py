"""The dense embedder — `BAAI/bge-m3` → 1024-dim vectors (ARCHITECTURE §5.3.1).

The *text* that gets embedded is not defined here: it comes from
`services.retrieval.text.scheme_text`, which is also the BM25 corpus, so sparse
and dense recall can never drift apart.

Frozen: 1024 dimensions, model `BAAI/bge-m3` (Phase 1 §9).
"""

from __future__ import annotations

from typing import Sequence

# Frozen: ARCHITECTURE §5.3.1 / Phase 1 §9.
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024


class Embedder:
    """Thin, lazily-loaded wrapper around ``BAAI/bge-m3``.

    The model is downloaded on first use (~2.3 GB) and kept in this process
    only — never cached to disk by hand, so ``pip``/HF caching stays the single
    source of that weight file.
    """

    def __init__(self, model_name: str = EMBEDDING_MODEL) -> None:
        self.model_name = model_name
        self._model = None

    @property
    def dim(self) -> int:
        return EMBEDDING_DIM

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Encode texts into 1024-dim vectors (one row per input string)."""
        if not texts:
            return []
        vectors = self._load().encode(list(texts), show_progress_bar=False)
        out = [list(map(float, row)) for row in vectors]
        for row in out:
            if len(row) != EMBEDDING_DIM:
                raise ValueError(
                    f"{self.model_name} produced {len(row)}-d vectors, "
                    f"expected {EMBEDDING_DIM} (ARCHITECTURE §5.3.1)"
                )
        return out
