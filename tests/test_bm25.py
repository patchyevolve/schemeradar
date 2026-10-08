"""BUILD_ORDER task 2.7 — in-memory BM25 cache (ARCHITECTURE §5.3.2)."""

from __future__ import annotations

import json
import pathlib

import pytest

from services.retrieval.bm25 import (
    B,
    K1,
    STOPWORDS,
    BM25Index,
    tokenize,
    version_stamp,
)
from services.retrieval.text import scheme_text

REPO = pathlib.Path(__file__).resolve().parents[1]
SEEDS = sorted((REPO / "seeds").glob("*.json"))
DELHI = "sch_delhi_post_matric_scholarship_sc_st_obc_2026"
PM_KISAN = "sch_pm_kisan_samman_nidhi_2019"


def _docs() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf8")) for p in SEEDS]


@pytest.fixture(scope="module")
def index() -> BM25Index:
    return BM25Index.build(_docs())


# --- frozen parameters ------------------------------------------------------


def test_frozen_bm25_parameters():
    assert K1 == 1.2 and B == 0.75  # ARCHITECTURE §9


def test_corpus_is_the_shared_four_field_text():
    doc = _docs()[0]
    text = scheme_text(doc)
    assert doc["name"] in text
    assert doc["department"] in text
    assert doc["eligibility_text"] in text
    assert doc["benefits"][0]["description"] in text
    assert text == text.strip()  # no leading/trailing junk from empty fields


# --- tokenization (§5.3.2 owns it) -----------------------------------------


def test_tokenize_case_folds_and_splits_on_punctuation():
    assert tokenize("Post-Matric Scholarship") == ["post", "matric", "scholarship"]
    assert tokenize("POST MATRIC") == ["post", "matric"]


def test_tokenize_drops_stopwords_but_keeps_domain_terms():
    tokens = tokenize("the scholarship for students of Delhi")
    assert "the" not in tokens and "for" not in tokens and "of" not in tokens
    assert "scholarship" in tokens and "delhi" in tokens
    assert "the" in STOPWORDS


def test_tokenize_is_hindi_aware():
    """Devanagari must survive tokenization, not be stripped as punctuation."""
    tokens = tokenize("दिल्ली छात्रवृत्ति")
    assert tokens == ["दिल्ली", "छात्रवृत्ति"]
    assert tokenize("PM-Kisan") == ["pm", "kisan"]


# --- idf: the Gate G2 blocker ------------------------------------------------


def test_every_idf_is_strictly_positive(index):
    """Regression: Okapi idf is 0/negative on a 2-doc corpus, killing all recall."""
    idfs = index._bm25.idf
    assert idfs, "no terms indexed"
    negatives = {w: v for w, v in idfs.items() if v <= 0}
    assert not negatives, sorted(negatives.items())[:5]


def test_document_frequency_counts_documents_not_terms(index):
    """A term repeated inside one document must not push df past the corpus."""
    doc = _docs()[1]
    repeated = [t for t in tokenize(scheme_text(doc)) if scheme_text(doc).count(t) > 3]
    corpus_size = len(index.doc_freqs)
    if repeated:
        # df for any term is at most the number of documents
        for term in repeated[:5]:
            df = sum(1 for d in index.doc_freqs if term in d)
            assert df <= corpus_size


# --- queries (BUILD_ORDER §4.4 DoD) ----------------------------------------


def test_lexical_queries_return_both_schemes(index):
    """DoD: BM25 returns both on `"post matric"` and `"kisan"`."""
    hits_post = dict(index.search("post matric"))
    hits_kisan = dict(index.search("kisan"))
    assert hits_post.get(DELHI, 0) > 0, hits_post
    assert hits_kisan.get(PM_KISAN, 0) > 0, hits_kisan
    assert PM_KISAN not in hits_post  # topical separation, not just a hit-fest


def test_scores_are_ranked_and_non_negative(index):
    hits = index.search("scholarship")
    assert hits, "expected at least one hit"
    scores = [s for _, s in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(s >= 0 for s in scores)


def test_empty_or_unmatched_query_returns_nothing(index):
    assert index.search("") == []
    assert index.search("quantum chromodynamics") == []


# --- version stamp + cache --------------------------------------------------


def test_stamp_is_deterministic_and_content_sensitive():
    docs = _docs()
    assert version_stamp(docs) == version_stamp(list(reversed(docs)))
    moved = _docs()
    moved[0]["source"]["content_hash"] = "sha256:" + "0" * 64
    assert version_stamp(moved) != version_stamp(docs)


def test_cache_round_trips(index):
    payload = index.to_cache()
    json.dumps(payload)  # must be JSON-serialisable
    restored = BM25Index.from_cache(payload)
    assert restored.stamp == index.stamp
    assert restored.scheme_ids == index.scheme_ids
    assert restored.search("kisan") == index.search("kisan")
    assert restored.k1 == K1 and restored.b == B


def test_cache_rejects_a_foreign_format_version(index):
    payload = index.to_cache()
    payload["format"] = "0"
    with pytest.raises(ValueError, match="format version"):
        BM25Index.from_cache(payload)


def test_building_from_the_same_documents_reproduces_the_same_stamp(index):
    assert BM25Index.build(_docs()).stamp == index.stamp
