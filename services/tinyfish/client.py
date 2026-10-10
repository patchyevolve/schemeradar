"""TinyFish Tier 1 (Search) and Tier 2 (Fetch) HTTP clients — ARCHITECTURE §7.1/§7.2.

Both clients share one request path that owns authentication, latency budgets
and rate-limit backoff, so neither tier ever talks to the network on its own.
Nothing outside ``services/tinyfish/`` may import these — that is the §5.5
façade boundary rule.

Mocking note (Step 3 Gate G3): every client accepts an ``httpx.AsyncClient``,
so a test can hand it ``httpx.AsyncClient(transport=httpx.MockTransport(...))``
and exercise the full retry/backoff path with **zero** real TinyFish calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence
from urllib.parse import urlencode

import httpx

from services.api.config import get_settings

logger = logging.getLogger("schemeradar.tinyfish")

# ARCHITECTURE §7.4 — Tier budgets (frozen).
TIER1_BUDGET_MS = 2000
TIER2_BUDGET_MS = 8000

# ARCHITECTURE §7.1/§7.2 — "retry ×3 with exponential backoff".
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 0.25
BACKOFF_CAP_S = 4.0

# Accepted Indian government source domains (DATA_SPEC V-CF7 / WORKFLOW §2.3).
GOV_HOST_PATTERN = re.compile(r"^https://[^/]+\.(gov\.in|nic\.in)(:\d+)?(/.*)?$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class TinyFishError(Exception):
    """Base class for every Tier 1/2 failure."""


class TinyFishAuthError(TinyFishError):
    """401/403 from TinyFish — the API key is missing or rejected."""


class TinyFishRateLimited(TinyFishError):
    """429 persisted past the backoff budget."""


class TinyFishUnavailable(TinyFishError):
    """5xx / transport failure persisted past the retry budget."""


class SsrfBlocked(TinyFishError):
    """URL failed the government-allowlist or private-IP check (L4)."""


# ---------------------------------------------------------------------------
# SSRF guard — BUILD_ORDER task 3.1 · DATA_SPEC L4 · WORKFLOW §2.3 step 1
# ---------------------------------------------------------------------------
def assert_allowed_gov_url(url: str) -> str:
    """Reject anything that is not a public ``*.gov.in`` / ``*.nic.in`` https URL.

    Static half of V-CF7. The DNS half (resolved IP must not be loopback,
    private, link-local or a cloud-metadata address) is done by
    :func:`assert_resolves_public` when a resolver is supplied.

    Raises :class:`SsrfBlocked` — callers map it to L4, which never retries.
    """
    if not isinstance(url, str) or not GOV_HOST_PATTERN.match(url):
        raise SsrfBlocked(f"url fails the gov.in/nic.in allowlist: {url!r}")
    return url


def _ip_is_private(ip: str) -> bool:
    """True for loopback / RFC1918 / link-local / metadata addresses."""
    if ":" in ip:  # IPv6
        return ip.lower() in {"::1", "::"} or ip.lower().startswith(("fe80:", "fc", "fd"))
    parts = [int(p) for p in ip.split(".")]
    if len(parts) != 4:
        return True
    a, b = parts[0], parts[1]
    if a == 127 or a == 0 or a >= 224:
        return True
    if a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168):
        return True
    if a == 169 and b == 254:  # link-local + 169.254.169.254 metadata
        return True
    if a == 100 and 64 <= b <= 127:
        return True
    return False


def assert_resolves_public(url: str, resolver: Any | None = None) -> list[str]:
    """Resolve ``url``'s host and reject private/link-local/metadata answers.

    ``resolver`` is injectable so tests never touch the real DNS (deterministic
    Gate G3). The default is :func:`socket.getaddrinfo`.
    """
    host = re.sub(r"^https://", "", url).split("/")[0].split(":")[0]
    if resolver is None:
        import socket

        def resolver(h: str, *_a: Any, **_k: Any) -> list[Any]:  # type: ignore[misc]
            return socket.getaddrinfo(h, None)

    infos = resolver(host)
    addresses = sorted({i[4][0] if isinstance(i, tuple) else str(i) for i in infos})
    if not addresses:
        raise SsrfBlocked(f"{host} did not resolve")
    for ip in addresses:
        if _ip_is_private(ip):
            raise SsrfBlocked(f"{host} resolves to a non-public address {ip}")
    return addresses


# ---------------------------------------------------------------------------
# Payload models (ARCHITECTURE §7.1/§7.2 invocation contracts)
# ---------------------------------------------------------------------------
class SearchHit(dict):
    """`{url, title, snippet, published_at}` — ARCHITECTURE §7.1."""

    @property
    def url(self) -> str:
        return str(self.get("url", ""))


class FetchResult(dict):
    """`{markdown, final_url, http_status, rendered_at, content_hash}` — §7.2."""

    @property
    def markdown(self) -> str:
        return str(self.get("markdown", ""))

    @property
    def content_hash(self) -> str:
        return str(self.get("content_hash", ""))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Shared request path
# ---------------------------------------------------------------------------
class _BaseClient:
    """Auth + budget + retry/backoff for one TinyFish tier."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_ms: int | None = None,
        http: httpx.AsyncClient | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_base_s: float = BACKOFF_BASE_S,
    ) -> None:
        s = get_settings()
        self.api_key = s.tinyfish_api_key if api_key is None else api_key
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_ms = timeout_ms
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_ms / 1000 if timeout_ms else 30.0)
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "_BaseClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            # TinyFish reads the key from either header; Bearer is canonical.
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        return headers

    @staticmethod
    def _retry_after_s(response: httpx.Response) -> float:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return min(float(raw), BACKOFF_CAP_S)
            except ValueError:
                pass
        return 0.0

    async def _send(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        """Auth, tier budget and the §7.1/§7.4 retry policy — **both verbs**.

        429 honours ``Retry-After`` first, then exponential backoff with jitter.
        401/403 are terminal (no retry). Everything else 5xx retries.
        """
        url = f"{self.base_url}{path}"
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                if method == "GET":
                    resp = await self._http.get(url, headers=self._headers())
                else:
                    resp = await self._http.post(url, json=payload, headers=self._headers())
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = TinyFishUnavailable(f"{path}: {type(exc).__name__}: {exc}")
            else:
                if resp.status_code in (401, 403):
                    raise TinyFishAuthError(f"{path}: HTTP {resp.status_code}")
                if resp.status_code == 429:
                    last = TinyFishRateLimited(f"{path}: HTTP 429")
                    await asyncio.sleep(
                        max(self._retry_after_s(resp), self._backoff(attempt))
                    )
                    continue
                if resp.status_code >= 500:
                    last = TinyFishUnavailable(f"{path}: HTTP {resp.status_code}")
                elif resp.is_success:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise TinyFishUnavailable(f"{path}: non-JSON body") from exc
                else:
                    raise TinyFishUnavailable(f"{path}: HTTP {resp.status_code}")
            if attempt < self.max_attempts:
                await asyncio.sleep(self._backoff(attempt))
        raise last or TinyFishUnavailable(f"{path}: retries exhausted")

    async def _post_json(self, path: str, payload: dict[str, Any]) -> Any:
        return await self._send("POST", path, payload)

    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_base_s * (2 ** (attempt - 1)), BACKOFF_CAP_S) + random.uniform(0, 0.05)

    async def _get(self, path: str) -> Any:
        return await self._send("GET", path)


def _as_list(body: Any, *keys: str) -> list[dict[str, Any]]:
    """TinyFish may return a bare array or a wrapper object — accept both."""
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        for k in keys:
            v = body.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


# ---------------------------------------------------------------------------
# Tier 1 — Search (ARCHITECTURE §7.1)
# ---------------------------------------------------------------------------
class TinyFishSearchClient(_BaseClient):
    """Discovery only. Feeds the ingestion pipeline, never a citizen query."""

    def __init__(self, **kw: Any) -> None:
        s = get_settings()
        kw.setdefault("base_url", s.tinyfish_search_base_url)
        kw.setdefault("timeout_ms", s.tinyfish_timeout_tier1_ms or TIER1_BUDGET_MS)
        super().__init__(**kw)

    async def discover(
        self,
        queries: Sequence[str],
        recency_window: str | None = None,
        max_results: int = 20,
    ) -> list[SearchHit]:
        """``discover(query_templates[], recency_window, max_results) → SearchHit[]``.

        The live endpoint takes **one** ``query`` per ``GET /?query=…`` and
        ignores every limit parameter, so a multi-query discovery walks the
        list sequentially inside the Tier-1 budget and truncates client-side.

        Only ``*.gov.in`` / ``*.nic.in`` hits survive post-processing
        (ARCHITECTURE §7.1 "Target web patterns").
        """
        hits: list[SearchHit] = []
        seen: set[str] = set()
        # §7.4 — the whole discovery, not each request, fits the Tier-1 budget.
        deadline = time.monotonic() + (self.timeout_ms or TIER1_BUDGET_MS) / 1000
        for query in queries:
            if len(hits) >= max_results or time.monotonic() >= deadline:
                break
            params: dict[str, Any] = {"query": str(query)}
            if recency_window:
                # Accepted, but not applied upstream — see §7.1 deviation note.
                params["recency_window"] = recency_window
            body = await self._get("/?" + urlencode(params))
            for raw in _as_list(body, "results", "hits", "data", "items"):
                url = str(raw.get("url") or raw.get("link") or "")
                if not GOV_HOST_PATTERN.match(url) or url in seen:
                    continue
                seen.add(url)
                hits.append(
                    SearchHit(
                        url=url,
                        title=str(raw.get("title") or ""),
                        snippet=str(raw.get("snippet") or raw.get("description") or ""),
                        published_at=raw.get("published_at") or raw.get("date"),
                    )
                )
                if len(hits) >= max_results:
                    break
        return hits


# ---------------------------------------------------------------------------
# Tier 2 — Fetch (ARCHITECTURE §7.2)
# ---------------------------------------------------------------------------
class TinyFishFetchClient(_BaseClient):
    """Renders JS-heavy portals into token-efficient Markdown."""

    def __init__(self, **kw: Any) -> None:
        s = get_settings()
        kw.setdefault("base_url", s.tinyfish_fetch_base_url)
        kw.setdefault("timeout_ms", s.tinyfish_timeout_tier2_ms or TIER2_BUDGET_MS)
        super().__init__(**kw)

    async def render(
        self,
        url: str,
        timeout_ms: int | None = None,
        wait_for: str | None = None,
    ) -> FetchResult:
        """``render(url, timeout_ms, wait_for) → FetchResult`` (§7.2).

        The URL is SSRF-checked before it ever leaves this process (task 3.1).

        The live endpoint is ``POST /`` with a ``urls`` **array** and answers
        with ``{results: [{url, final_url, text, …}], errors: [{url, error,
        status}]}`` — markdown lives under ``text``, and the HTTP status is
        only carried for the failure entry.  A portal that answers with an
        error still returns that status, so the caller records
        ``http_status`` instead of collapsing a dead portal into a transport
        exception.
        """
        assert_allowed_gov_url(url)
        budget = timeout_ms if timeout_ms is not None else (self.timeout_ms or TIER2_BUDGET_MS)
        payload: dict[str, Any] = {"urls": [url], "timeout_ms": budget}
        if wait_for:
            payload["wait_for"] = wait_for
        body = await self._post_json("/", payload)
        if not isinstance(body, dict):
            raise TinyFishUnavailable("fetch: response was not an object")

        def _int(value: Any) -> int:
            try:
                return int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0

        results = [r for r in (body.get("results") or []) if isinstance(r, dict)]
        errors = [e for e in (body.get("errors") or []) if isinstance(e, dict)]
        first = next((r for r in results if str(r.get("url") or "") == url), None)
        first = first or (results[0] if results else None)
        err = next((e for e in errors if str(e.get("url") or "") == url), None)
        err = err or (errors[0] if errors else None)

        if first is None and err is not None:
            markdown, final_url, status = "", url, _int(err.get("status"))
        elif first is None:
            raise TinyFishUnavailable("fetch: no result and no error entry")
        else:
            markdown = str(first.get("text") or first.get("markdown") or first.get("content") or "")
            final_url = str(first.get("final_url") or first.get("url") or url)
            # The API only reports a status on the error entry; a returned
            # result means the portal rendered (§7.2).
            status = _int(err.get("status")) if err is not None else 200

        return FetchResult(
            markdown=markdown,
            final_url=final_url,
            http_status=status,
            rendered_at=str(
                body.get("rendered_at")
                or (first.get("rendered_at") if first else None)
                or datetime.now(timezone.utc).isoformat()
            ),
            content_hash=str((first or {}).get("content_hash") or _sha256(markdown)),
        )
