"""TinyFish Cloud Integration Gateway — `services/tinyfish/` (ARCHITECTURE §5.5).

A single internal facade exposing exactly three typed clients:

    TinyFishSearchClient    discover(queries, recency_window) -> SearchHit[]
    TinyFishFetchClient     render(url, timeout) -> FetchResult{markdown, final_url, status}
    TinyFishWebAgentClient  verify(url, scheme_id) -> VerificationResult{status, deadline, snapshot_url}

Boundary rule: NO other module may call TinyFish endpoints directly. This
centralises retries, rate limits, cost accounting and audit logging in one place.

Tier budgets (ARCHITECTURE §7.4 / §9, frozen):
    Tier 1 Search    <= 2,000 ms   ingestion discovery only
    Tier 2 Fetch     <= 8,000 ms   ingestion rendering only
    Tier 3 Web Agent <= 4,000 ms per portal, concurrency 3, top-N 5, async

Core invariant: the eligibility answer never depends on TinyFish being up.
Tier 3 down => UNVERIFIED + last-known status; scores are unaffected.

Must NOT own: Qdrant reads, rule evaluation, canonical document truth.
"""
