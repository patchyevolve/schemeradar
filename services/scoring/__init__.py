"""Neuro-Symbolic Scoring Engine — `services/scoring/` (ARCHITECTURE §5.4).

Master formula (ARCHITECTURE §6.1, frozen):

    Score = clamp(W_D * S_det + W_S * S_sem - P_docs, 0, 1)
    W_D = 0.60, W_S = 0.40, P_cap = 0.60

Sub-modules:
    Deterministic Rule Evaluator   -> S_det in {0,1}, gate_trace[], near_miss
    Semantic Auditor               -> lambda_llm in {1.0, 0.6, 0.2}, semantic_notes[]
    Document Friction Penaltizer   -> P_docs in [0, P_cap], missing_documents[]
    Score Assembler                -> Score, band, score_breakdown

Determinism guarantees:
  * The Rule Evaluator is a pure function of (ProfileContext, Scheme.gates).
  * lambda_llm can only reduce S_sem. It cannot change S_det, cannot un-fail a
    gate, and cannot add a scheme to the feed.
  * S_det = 0 short-circuits: Score := 0, band := NEAR_MISS, assembler never runs.
  * Every response carries score_breakdown so explainability (G5) is testable.

Must NOT own: HTTP concerns, vector math, persistence, browser automation.
"""
