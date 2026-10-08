"""In-memory BM25 sparse index (ARCHITECTURE §5.3.2, BUILD_ORDER task 2.7).

Responsibility: sparse lexical recall for hard legal tokens ("OBC", "SC/ST",
"Delhi domicile", "post-matric", "PM-Kisan") that dense embeddings blur.

Owns: tokenization (case-folded, Hindi-aware, stopwords), IDF/BM25 scoring
(``k1 = 1.2``, ``b = 0.75`` — frozen, ARCHITECTURE §9), top-K sparse ranking.

Must NOT own: persistence, or filtering by structured fields — those filters
are applied *after* fusion by the orchestrator (§6.3.1).

The index is a **rebuildable cache and is never authoritative**.  Every build
carries a version stamp derived from the frozen parameters plus each
document's ``source.content_hash``; a cache built from stale content fails the
stamp check and is rebuilt instead of served (ARCHITECTURE §6.6).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Mapping, Sequence

# --- frozen (ARCHITECTURE §9 / §5.3.2) -------------------------------------
K1 = 1.2
B = 0.75

# Bump when the tokenizer, corpus shape or score formula changes, so old
# caches are invalidated even if the documents did not move.
CACHE_FORMAT_VERSION = "1"

# Keep Latin alphanumerics (case-folded) and Devanagari runs.  A contiguous
# Devanagari run is kept whole rather than split: without a morphological
# analyser, naive splitting shatters Hindi words and destroys recall.
_TOKEN_RE = re.compile(r"[a-z0-9]+|[ऀ-ॿ]+")

# A compact, explicit stoplist — small because these two corpora are short and
# domain terms ("for", "the") carry no IDF weight worth keeping either way.
STOPWORDS = frozenset(
    """
    a an the and or of to in for on at by with from is are was were be been
    being this that these those it its as not no nor but if then than so
    such own same too very can will just do does did done have has had
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Case-folded, Hindi-aware tokenization with stopwords removed."""
    return [t for t in _TOKEN_RE.findall(text.casefold()) if t not in STOPWORDS]


class BM25Index:
    """A built, stamped BM25 index over the canonical corpus."""

    def __init__(
        self,
        scheme_ids: Sequence[str],
        doc_freqs: Sequence[Sequence[str]],
        *,
        k1: float = K1,
        b: float = B,
        stamp: str = "",
    ) -> None:
        from rank_bm25 import BM25Okapi

        self.scheme_ids = list(scheme_ids)
        self.k1 = k1
        self.b = b
        self.stamp = stamp
        self.doc_freqs = [list(tokens) for tokens in doc_freqs]
        self._bm25 = BM25Okapi(list(doc_freqs), k1=k1, b=b)
        self._apply_positive_idf()

    def _apply_positive_idf(self) -> None:
        """Replace Okapi idf with a strictly positive (Lucene-style) idf.

        Okapi's ``ln((N - df + 0.5) / (df + 0.5))`` is *exactly* zero when
        ``df == (N + 1) / 2`` and negative when a term is common.  On the
        two-document Step 2 corpus that means every term idf's to 0, all
        scores are 0, and **no lexical query can ever hit** — Gate G2's
        `"post matric"` / `"kisan"` requirement would be unmeetable.

        Lucene's ``ln(1 + (N - df + 0.5) / (df + 0.5))`` is strictly positive
        for every ``0 < df <= N`` and agrees with Okapi's ordering for large
        ``N``.  ``k1`` and ``b`` stay at their frozen values (ARCHITECTURE §9);
        only the idf, which §5.3.2 does not freeze, changes.
        """
        import math
        from collections import Counter

        corpus_size = len(self.doc_freqs)
        if corpus_size == 0:
            return
        # df = number of *documents* containing the term, not total term
        # frequency — using the latter pushes df past the corpus size and
        # makes the formula negative again.
        doc_freq: Counter = Counter()
        for doc in self.doc_freqs:
            doc_freq.update(set(doc))
        self._bm25.idf = {
            word: math.log(1 + (corpus_size - freq + 0.5) / (freq + 0.5))
            for word, freq in doc_freq.items()
        }

    # --- construction ------------------------------------------------------

    @classmethod
    def build(
        cls,
        docs: Iterable[Mapping[str, Any]],
        *,
        text_of=None,
    ) -> "BM25Index":
        """Build from canonical Scheme documents read out of MongoDB."""
        from services.retrieval.text import scheme_text

        if text_of is None:
            text_of = scheme_text
        records = sorted(docs, key=lambda d: d["scheme_id"])
        scheme_ids = [d["scheme_id"] for d in records]
        freqs = [tokenize(text_of(d)) for d in records]
        stamp = version_stamp(records, K1, B)
        return cls(scheme_ids, freqs, k1=K1, b=B, stamp=stamp)

    @classmethod
    def from_cache(cls, payload: Mapping[str, Any]) -> "BM25Index":
        if payload.get("format") != CACHE_FORMAT_VERSION:
            raise ValueError("cache format version mismatch")
        return cls(
            payload["scheme_ids"],
            payload["doc_freqs"],
            k1=payload["k1"],
            b=payload["b"],
            stamp=payload["stamp"],
        )

    def to_cache(self) -> dict[str, Any]:
        return {
            "format": CACHE_FORMAT_VERSION,
            "k1": self.k1,
            "b": self.b,
            "stamp": self.stamp,
            "scheme_ids": self.scheme_ids,
            "doc_freqs": self.doc_freqs,
        }

    # --- queries -----------------------------------------------------------

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """Lexical top-K. Filters are NOT applied here (§5.3.2 must-not-own)."""
        tokens = tokenize(query)
        if not tokens or not self.scheme_ids:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(
            zip(self.scheme_ids, map(float, scores)),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return [(sid, score) for sid, score in ranked[:top_k] if score > 0.0]

    def __len__(self) -> int:
        return len(self.scheme_ids)


def version_stamp(
    records: Iterable[Mapping[str, Any]], k1: float = K1, b: float = B
) -> str:
    """Deterministic ``sha256`` over params + every document's content hash.

    Idempotent by construction: seeding without changing any
    ``source.content_hash`` reproduces the identical stamp.
    """
    digest = hashlib.sha256()
    digest.update(f"bm25/{CACHE_FORMAT_VERSION};k1={k1};b={b};".encode("utf8"))
    for record in sorted(records, key=lambda d: d["scheme_id"]):
        source = record.get("source") or {}
        digest.update(
            f"{record['scheme_id']}={source.get('content_hash', '?')};".encode("utf8")
        )
    return "sha256:" + digest.hexdigest()
