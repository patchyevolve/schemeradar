"""Tier 3 — the live Web Agent (ARCHITECTURE §7.3 · WORKFLOW §2).

Implements the 18-step ``VerifyPortal`` algorithm (WORKFLOW §2.3), the date
normalisation rules (§2.4) and the 9-row verdict precedence table (§2.5).

    verify_stream(...)  yields status strings while it runs; the FINAL yield is
                        the verdict enum's value.
    verify(...)         consumes the stream and returns a VerificationResult
                        whose ``verification_status`` is the strict
                        ``VerificationStatus`` enum (services/api/models.py).

Deviations from a literal reading of the Step 3 brief, recorded on purpose:

* The verdict enum is the frozen ``VerificationStatus`` (ARCHITECTURE §7.3
  "Emitted statuses"), not a 3-value literal — WORKFLOW §2.5 rows 4–5 require
  ``BLOCKED`` for WAF/captcha, and Edge Case C (TC-C1…C11) tests it.
* ``verify`` returns the frozen §2.1 ``VerificationResult`` envelope, which
  *carries* that enum, instead of the bare enum.

Mocking note (Gate G3): both the browser (``browser_factory``) and the object
storage (``snapshots``) are constructor injectables, so tests drive the whole
algorithm with zero Playwright processes and zero MinIO requests.
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence
from zoneinfo import ZoneInfo

import httpx

from services.api.config import get_settings
from services.api.models import VerificationStatus
from services.tinyfish.client import SsrfBlocked, assert_allowed_gov_url, assert_resolves_public

logger = logging.getLogger("schemeradar.tinyfish.agent")

IST = ZoneInfo("Asia/Kolkata")          # WORKFLOW §2.4 — baseline is IST today
VIEWPORT = {"width": 1366, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Phase 3 constants (WORKFLOW §4) — names below are the code-side aliases.
NAVIGATE_IDLE_WAIT_MS = 1_200         # NAVIGATE_IDLE_WAIT_MS — §2.3 step 3
DISMISS_MAX_ATTEMPTS = 5              # POPUP_DISMISS_MAX_ATTEMPTS — §2.3 step 5
DISMISS_BUDGET_MS = 800               # POPUP_DISMISS_BUDGET_MS — §2.3 step 5
DEADLINE_CANDIDATES = 20              # DEADLINE_CANDIDATE_LIMIT — §2.3 step 9
AGENT_VERSION = "0.1.0"               # DATA_SPEC §8.1 `agent_version`
INTAKE_STATUSES = frozenset({"INTAKE_OPEN", "INTAKE_CLOSED"})
MAX_AGENT_RETRIES = 1                 # §2.7 — timeout / http_5xx only
RETRY_ELIGIBLE_MS = 2_000             # §2.7 — only if elapsed < 2,000 ms

# §2.5 signals catalog
SIGNAL_LANDING = "landing_page_classified"
SIGNAL_POPUP = "notice_popup_dismissed"
SIGNAL_COOKIE = "cookie_banner_dismissed"
SIGNAL_CLOSED_LANG = "closed_language_present"
SIGNAL_DEADLINE_FOUND = "deadline_text_found"
SIGNAL_DEADLINE_UNLABELLED = "deadline_from_unlabelled_context"
SIGNAL_NO_DEADLINE = "no_deadline_found"
SIGNAL_AMBIGUOUS = "verdict_ambiguous"
SIGNAL_SNAPSHOT_OK = "snapshot_captured"
SIGNAL_SNAPSHOT_FAILED = "snapshot_failed"


class AgentError(Exception):
    """Transport/infra failure that maps to an error_class, never a 5xx."""


# ===========================================================================
# DOM inspection — one JS blob per step keeps the Playwright surface tiny
# ===========================================================================
INSPECT_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const bodyText = norm(document.body ? document.body.innerText : '');
  const low = bodyText.toLowerCase();

  const MAINT = ['site is under maintenance','under uparant','temporarily down',
                 'back soon','503 service unavailable','अस्थायी रूप से बंद'];
  const WAF = ['just a moment','checking your browser','verify you are human',
               'access denied','are you a robot'];
  const CLOSED = ['application closed','registration closed','last date over',
                  'समाप्त','आवेदन बंद हो गया'];
  const KEYS = ['apply','apply now','register','new registration','login & apply',
                'online application','click here to apply','आवेदन करें','पंजीकरण करें',
                'ऑनलाइन आवेदन'];

  const vis = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const cs = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' &&
           cs.display !== 'none' && cs.pointerEvents !== 'none' && !!el.offsetParent;
  };
  const closedAncestor = el => {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      const t = norm(n.innerText).toLowerCase();
      if (t && t.length < 160 &&
          ['application closed','registration closed','last date over','समाप्त'].some(k => t.includes(k)))
        return true;
    }
    return false;
  };
  const enabled = el => {
    if (!el) return false;
    const cs = window.getComputedStyle(el);
    if (el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true') return false;
    if (/disabled|inactive|expired|is-closed/i.test(el.className || '')) return false;
    if (cs.pointerEvents === 'none' || !vis(el)) return false;
    if (closedAncestor(el)) return false;
    return true;
  };
  const keyMatch = el => {
    const t = norm(el.innerText || el.value || '').toLowerCase();
    return KEYS.some(k => t === k || t.includes(k));
  };

  // --- apply / register controls, priority S1..S4 -------------------------
  const cands = [];
  document.querySelectorAll(
    'a[href*="apply" i],a[href*="applyonline" i],a[href*="registration" i],' +
    'a[href*="online-application" i],button,input[type=submit],[role=button],' +
    'form[action*="apply" i],form[action*="registration" i]'
  ).forEach(el => cands.push(el));
  document.querySelectorAll('[onclick]').forEach(el => {
    if ((el.getAttribute('onclick') || '').includes('__doPostBack') && keyMatch(el)) cands.push(el);
  });

  let controlState = 'absent', controlCount = 0;
  const chosen = cands.filter(el => vis(el) && (el.tagName === 'A' || keyMatch(el) || el.tagName === 'BUTTON' || el.type === 'submit'));
  controlCount = chosen.length;
  if (chosen.length === 0) controlState = 'absent';
  else controlState = chosen.some(enabled) ? 'enabled' : 'disabled';

  // --- deadline candidates ------------------------------------------------
  const DL_RE = /last date|last date of submission|closing date|deadline|apply by|submission closes|आवेदन की अंतिम तिथि|प्रवेश की अंतिम तिथि|पंजीकरण तिथि/i;
  const raw = [];
  document.querySelectorAll('time').forEach(el => raw.push({t: norm(el.textContent), label: false}));
  document.querySelectorAll('h1,h2,h3,h4,h5,th,strong,b,td,p,div,span,label').forEach(el => {
    const t = norm(el.textContent);
    if (t && t.length <= 400 && DL_RE.test(t)) {
      const kids = el.querySelectorAll ? el.querySelectorAll('time,span,p,td,b,strong') : [];
      kids.forEach(k => raw.push({t: norm(k.textContent), label: true}));
      if (kids.length === 0) raw.push({t: t, label: true});
    }
  });
  const seen = new Set(), deadlines = [];
  for (const r of raw) {
    if (!r.t || r.t.length > 300) continue;
    if (seen.has(r.t)) continue;
    seen.add(r.t);
    deadlines.push(r);
    if (deadlines.length >= 20) break;
  }

  const overlay = !!document.querySelector(
    '.modal.show,.modal.in,.ui-widget-overlay,.modal-backdrop.show,[role=dialog][style*="block"]'
  );
  const captcha = !!document.querySelector(
    '.g-recaptcha,#captcha,iframe[src*="recaptcha"],iframe[src*="hcaptcha"],.h-captcha'
  );

  return {
    final_url: location.href,
    maintenance: MAINT.some(k => low.includes(k)),
    waf: WAF.some(k => low.includes(k)) || !!document.querySelector('#cf-chl,_cf_chl,.cf-chl'),
    captcha: captcha,
    overlay: overlay,
    control_state: controlState,
    control_count: controlCount,
    closed_language: CLOSED.some(k => low.includes(k)),
    deadlines: deadlines
  };
}
"""

DISMISS_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const TXT = ['close','ok','dismiss','proceed','continue','accept','ok, got it',
               'बंद करें','ठीक है','स्वीकार करें'];
  let n = 0;
  const sel = [
    '[aria-label*="close" i]','.close','.modal-close','.dialog-close',
    '[data-dismiss="modal"]','.btn-close','button'
  ].join(',');
  document.querySelectorAll(sel).forEach(el => {
    const t = norm(el.innerText || el.getAttribute('aria-label') || '').toLowerCase();
    const hit = el.matches('[aria-label*="close" i],.close,.modal-close,.dialog-close') ||
                TXT.some(k => t === k || t.includes(k));
    if (hit && el.offsetParent !== null && !el.disabled) { try { el.click(); n++; } catch (e) {} }
  });
  let cookie = false;
  document.querySelectorAll('button,a').forEach(el => {
    const t = norm(el.innerText).toLowerCase();
    if (!cookie && t === 'accept' && el.offsetParent !== null) {
      try { el.click(); cookie = true; } catch (e) {}
    }
  });
  return {dismissed: n, cookie: cookie};
}
"""


# ===========================================================================
# Date normalisation — WORKFLOW §2.4
# ===========================================================================
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
# Devanagari month names seen on state portals.
MONTHS.update({"जनवरी": 1, "फ़रवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6,
               "जुलाई": 7, "अगस्त": 8, "सितम्बर": 9, "सितंबर": 9, "अक्टूबर": 10,
               "नवम्बर": 11, "नवंबर": 11, "दिसम्बर": 12, "दिसंबर": 12})

RE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
RE_DMY = re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})\b")
RE_ORD = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+|[\u0900-\u097F]+)\s*,?\s+(\d{4})\b")
RE_MONTH_FIRST = re.compile(
    r"\b([A-Za-z]+|[\u0900-\u097F]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s+(\d{4})\b")
RE_FY = re.compile(r"\b(?:FY\s*)?(\d{4})\s*[-–]\s*(\d{2}|\d{4})\b")
RE_YEAR_ONLY = re.compile(r"\b(19|20)\d{2}\b")


def ist_today(now: datetime | None = None) -> date:
    """Today in Asia/Kolkata — never the host timezone (§2.4)."""
    return (now or datetime.now(timezone.utc)).astimezone(IST).date()


def _valid(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def normalize_deadline(text: str, today: date | None = None) -> tuple[date | None, str | None]:
    """Normalise one candidate string to a date (ISO-8601) + optional signal.

    Implements every row of WORKFLOW §2.4, including the ambiguous DD/MM
    default (``en-IN``) and the "not a deadline" rejection for fiscal years.
    """
    s = " ".join((text or "").split())
    if not s:
        return None, None
    today = today or ist_today()

    if RE_ISO.search(s):
        m = RE_ISO.search(s)
        d = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return (d, None) if d else (None, "date_rejected_not_a_deadline")

    if m := RE_ORD.search(s):
        day, mon, yr = m.group(1), m.group(2).lower(), m.group(3)
        mi = MONTHS.get(mon)
        if mi:
            d = _valid(int(yr), mi, int(day))
            if d:
                return d, None

    if m := RE_MONTH_FIRST.search(s):
        mon, day, yr = m.group(1).lower(), m.group(2), m.group(3)
        mi = MONTHS.get(mon)
        if mi:
            d = _valid(int(yr), mi, int(day))
            if d:
                return d, None

    if m := RE_DMY.search(s):
        a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = 2000 + int(y) if len(y) == 2 else int(y)
        sig = "date_two_digit_year" if len(y) == 2 else None
        if a > 12 and b <= 12:                       # 10/31/2026 — unambiguous
            return _valid(year, b, a), sig
        if b > 12 and a <= 12:                       # 31/10/2026
            return _valid(year, a, b), sig
        # both <= 12: DD/MM/YYYY is the en-IN default (§2.4)
        d = _valid(year, b, a)
        return d, "date_convention_ambiguous" if (d and not sig) else sig

    # A bare year / fiscal year is never a deadline (§2.4)
    if RE_FY.search(s) and not RE_DMY.search(s) and not RE_ISO.search(s):
        return None, "date_rejected_not_a_deadline"
    if len(RE_YEAR_ONLY.findall(s)) == 1 and len(s) <= 12 and not RE_DMY.search(s):
        return None, "date_rejected_not_a_deadline"

    return None, None


def select_deadline(
    candidates: Sequence[tuple[str, bool]], today: date
) -> tuple[date | None, str | None, bool, list[str]]:
    """§2.3 steps 10–12: normalise, partition future/past, pick by precedence.

    Returns ``(deadline, signal, label_bound, extra_signals)``.  ``extra_signals``
    carries the §2.4 convention signals (``date_convention_ambiguous``,
    ``date_two_digit_year``) that belong in ``observed_signals[]`` even though
    they do not decide which candidate wins.
    """
    parsed: list[tuple[date, bool, bool]] = []   # (date, label_bound, in_past)
    extra: list[str] = []
    for text, labelled in candidates:
        d, sig = normalize_deadline(text, today)
        if d is None:
            continue                              # rejected: not a deadline
        if sig and sig not in extra:
            extra.append(sig)
        parsed.append((d, labelled, d < today))
    if not parsed:
        return None, SIGNAL_NO_DEADLINE, False, extra

    labelled_future = [p for p in parsed if p[1] and not p[2]]
    if labelled_future:
        return max(p[0] for p in labelled_future), SIGNAL_DEADLINE_FOUND, True, extra

    future = [p for p in parsed if not p[2]]
    if future:
        return max(p[0] for p in future), SIGNAL_DEADLINE_UNLABELLED, False, extra

    labelled_past = [p for p in parsed if p[1] and p[2]]
    if labelled_past:
        return max(p[0] for p in labelled_past), "deadline_in_past", True, extra

    return max(p[0] for p in parsed), "deadline_in_past", False, extra


def decide(
    signals: dict[str, Any],
    deadline: date | None,
    label_bound: bool,
    today: date,
) -> tuple[VerificationStatus, str | None, list[str]]:
    """WORKFLOW §2.5 — 9-row precedence table, first match wins."""
    out: list[str] = [SIGNAL_LANDING]
    if signals.get("closed_language"):
        out.append(SIGNAL_CLOSED_LANG)
    if deadline is None:
        out.append(SIGNAL_NO_DEADLINE)
    else:
        out.append(SIGNAL_DEADLINE_FOUND if label_bound else SIGNAL_DEADLINE_UNLABELLED)

    state = signals.get("control_state")
    out.append(f"submit_control_{'enabled' if state == 'enabled' else state if state else 'absent'}")

    # Rows 6 → 7 → 8 → 9
    if signals.get("closed_language"):
        return VerificationStatus.INTAKE_CLOSED, None, out
    if label_bound and deadline is not None and deadline < today:
        out.append("deadline_in_past")
        return VerificationStatus.INTAKE_CLOSED, None, out
    if state == "enabled" and (deadline is None or deadline >= today):
        return VerificationStatus.INTAKE_OPEN, None, out
    if state == "enabled" and deadline is not None and deadline < today:
        # Row 8 only applies to a label-bound date; unlabelled + enabled → row 9.
        out.append("deadline_in_past")
        return VerificationStatus.INTAKE_CLOSED, None, out
    out.append(SIGNAL_AMBIGUOUS)
    return VerificationStatus.UNREACHABLE, SIGNAL_AMBIGUOUS, out


# ===========================================================================
# Snapshot → MinIO (SigV4, stdlib only — no boto3 dependency)
# ===========================================================================
def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


class SnapshotStore:
    """PUT ``snapshots/{scheme_id}/{job_id}.png`` into the MinIO bucket.

    Implements just enough AWS SigV4 for ``PutObject``/``CreateBucket`` so
    Step 3 does not need ``boto3``.  ``put`` returns ``None`` on any failure —
    WORKFLOW §2.3 step 13: a snapshot is evidence, never the decision input,
    so a failed upload emits ``snapshot_failed`` and does **not** change the
    verdict.

    Credentials come from ``Settings`` (which reads ``.env``), not from
    ``os.environ`` — pydantic-settings never re-exports what it parses.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        bucket: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        *,
        region: str = "us-east-1",
        http: Any | None = None,
    ) -> None:
        s = get_settings()
        self.endpoint = (endpoint or s.object_storage_endpoint).rstrip("/")
        self.bucket = bucket or s.object_storage_bucket
        self.access_key = (
            access_key if access_key is not None else s.object_storage_access_key
        )
        self.secret_key = (
            secret_key if secret_key is not None else s.object_storage_secret_key
        )
        self.region = region
        self._http = http

    # -- SigV4 -------------------------------------------------------------
    @property
    def _parts(self) -> tuple[str, str] | None:
        m = re.match(r"^(https?)://([^/]+)(/.*)?$", self.endpoint)
        return (m.group(1), m.group(2)) if m else None

    def _signed_headers(
        self, method: str, path: str, payload_hash: str, content_type: str | None
    ) -> dict[str, str]:
        """Build the SigV4 ``Authorization`` header for one request."""
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        parts = self._parts
        assert parts is not None
        host = parts[1]
        canon: dict[str, str] = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if content_type:
            canon["content-type"] = content_type
        keys = sorted(canon)
        signed_headers = ";".join(keys)
        canonical_headers = "".join(f"{k}:{canon[k]}\n" for k in keys)
        canonical = (
            f"{method}\n{path}\n\n{canonical_headers}\n"
            f"{signed_headers}\n{payload_hash}"
        )
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        to_sign = (
            "AWS4-HMAC-SHA256\n" + amz_date + "\n" + scope + "\n" +
            hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )
        k = _sign(("AWS4" + self.secret_key).encode(), date_stamp)
        k = _sign(k, self.region)
        k = _sign(k, "s3")
        k = _sign(k, "aws4_request")
        signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        headers = dict(canon)
        headers["Host"] = headers.pop("host")
        if content_type:
            headers["Content-Type"] = headers.pop("content-type")
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return headers

    async def _send(
        self,
        method: str,
        path: str,
        content: bytes,
        content_type: str | None = None,
    ) -> httpx.Response:
        parts = self._parts
        assert parts is not None
        payload_hash = hashlib.sha256(content).hexdigest()
        headers = self._signed_headers(method, path, payload_hash, content_type)
        owns = self._http is None
        client = self._http or _async_http()
        try:
            return await client.request(
                method, f"{parts[0]}://{parts[1]}{path}", content=content, headers=headers
            )
        finally:
            if owns:
                await client.aclose()

    # -- public ------------------------------------------------------------
    async def ensure_bucket(self) -> bool:
        """Create the bucket if absent (idempotent: 409 already-exists is OK)."""
        if not self.access_key or not self.secret_key or not self._parts:
            return False
        try:
            resp = await self._send("PUT", f"/{self.bucket}", b"")
        except Exception as exc:  # noqa: BLE001 — evidence path must not raise
            logger.warning("snapshot bucket create failed: %s", exc)
            return False
        ok = resp.status_code in (200, 204, 409)
        if not ok:
            logger.warning(
                "snapshot bucket create failed: HTTP %s %s",
                resp.status_code, resp.text[:200],
            )
        return ok

    async def put(self, key: str, data: bytes, content_type: str = "image/png") -> str | None:
        """Upload and return the object URL, or ``None`` on failure."""
        if not self.access_key or not self.secret_key or not self._parts:
            return None
        path = f"/{self.bucket}/{key.lstrip('/')}"
        url = f"{self.endpoint}{path}"
        try:
            resp = await self._send("PUT", path, data, content_type)
            if resp.status_code == 404:
                # First run on a fresh MinIO: create the bucket, then retry once.
                if await self.ensure_bucket():
                    resp = await self._send("PUT", path, data, content_type)
            if resp.status_code >= 400:
                logger.warning(
                    "snapshot upload failed: HTTP %s %s",
                    resp.status_code, resp.text[:200],
                )
                return None
            return url
        except Exception as exc:  # evidence must never break the verdict
            logger.warning("snapshot upload failed: %s", exc)
            return None


def _async_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0))


# ===========================================================================
# The agent
# ===========================================================================
@dataclass
class VerificationResult:
    """WORKFLOW §2.1 invocation contract."""

    verification_status: VerificationStatus
    extracted_deadline: date | None = None
    snapshot_url: str | None = None
    final_url: str | None = None
    observed_signals: list[str] = field(default_factory=list)
    error_class: str | None = None
    duration_ms: int = 0
    job_id: str = ""


# ``factory() -> (page, closer)``.  Injected by tests: no Playwright process.
BrowserFactory = Callable[[], Awaitable[tuple[Any, Callable[[], Awaitable[None]]]]]


async def _default_browser_factory() -> tuple[Any, Callable[[], Awaitable[None]]]:
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        viewport=VIEWPORT,
        device_scale_factor=1,
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        user_agent=USER_AGENT,
        accept_downloads=False,
        java_script_enabled=True,
    )
    page = await context.new_page()

    async def closer() -> None:
        for stop in (context.close, browser.close, pw.stop):
            try:
                await stop()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass

    return page, closer


class _Abort(Exception):
    """Internal: jump to the persistence tail with a verdict already set."""


class TinyFishWebAgentClient:
    """Tier 3 facade — ARCHITECTURE §7.3 / §5.5 (WORKFLOW §2.3 algorithm)."""

    def __init__(
        self,
        *,
        browser_factory: BrowserFactory | None = None,
        snapshots: SnapshotStore | None = None,
        timeout_ms: int | None = None,
        resolver: Callable[..., Any] | None = None,
        check_dns: bool = True,
        db: Any | None = None,
        agent_version: str = AGENT_VERSION,
    ) -> None:
        s = get_settings()
        self.timeout_ms = timeout_ms or s.tinyfish_timeout_tier3_ms
        self.top_n = s.tinyfish_top_n_verified
        self.concurrency = s.tinyfish_browser_concurrency
        self.browser_factory = browser_factory or _default_browser_factory
        self.snapshots = snapshots
        self.resolver = resolver
        self.check_dns = check_dns
        # ``db`` is the pymongo Database used by §2.3 steps 15-16.  It is
        # optional so a unit test can drive the algorithm with no Mongo at all.
        self.db = db
        self.agent_version = agent_version

    # -- public API --------------------------------------------------------
    async def verify_status(self, scheme_id: str, portal_url: str, job_id: str) -> VerificationStatus:
        """Just the verdict — the strict enum, nothing else."""
        return (await self.verify(scheme_id, portal_url, job_id)).verification_status

    async def verify(
        self, scheme_id: str, portal_url: str, job_id: str
    ) -> VerificationResult:
        """Run the 18-step algorithm; return the frozen §2.1 envelope."""
        result: VerificationResult | None = None
        async for item in self.events(scheme_id, portal_url, job_id):
            if isinstance(item, VerificationResult):
                result = item
        if result is None:  # pragma: no cover — _run always ends with one
            raise AgentError("agent produced no result")
        return result

    async def verify_stream(
        self, scheme_id: str, portal_url: str, job_id: str
    ) -> AsyncIterator[str]:
        """Yield a status string per step; **final yield = the verdict enum**."""
        async for item in self.events(scheme_id, portal_url, job_id):
            if isinstance(item, VerificationResult):
                yield item.verification_status.value
            else:
                yield str(item)

    async def events(
        self, scheme_id: str, portal_url: str, job_id: str
    ) -> AsyncIterator[Any]:
        """Public stream: one ``str`` status per step, then the final
        ``VerificationResult``.  The SSE route consumes this."""
        async for item in self._run(scheme_id, portal_url, job_id):
            yield item

    # -- the algorithm -----------------------------------------------------
    async def _run(
        self, scheme_id: str, portal_url: str, job_id: str
    ) -> AsyncIterator[Any]:
        """WORKFLOW §2.3. Yields ``str`` statuses, then a ``VerificationResult``."""
        started = time.perf_counter()
        started_at = datetime.now(timezone.utc)   # DATA_SPEC §8.1 server-generated
        today = ist_today()
        seen: list[str] = []

        def mark(*names: str) -> None:
            """Append a §2.5 catalog signal once, in order."""
            for n in names:
                if n not in seen:
                    seen.append(n)

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        status: VerificationStatus | None = None
        error_class: str | None = None
        deadline: date | None = None
        label_bound = False
        snapshot_url: str | None = None
        final_url: str | None = portal_url
        page: Any = None
        closer: Callable[[], Awaitable[None]] | None = None
        sig: dict[str, Any] = {}
        navigated = False   # a landing page exists => there is evidence to keep

        try:
            # ============ phase A — the 18-step algorithm ============
            try:
                # -- step 1: SSRF pre-flight -----------------------------
                yield "ssrf_preflight"
                try:
                    assert_allowed_gov_url(portal_url)
                    if self.check_dns:
                        assert_resolves_public(portal_url, self.resolver)
                except SsrfBlocked as exc:
                    # §2.3 step 1: abort. error_class only — not a signal.
                    error_class, status = "ssrf_blocked", VerificationStatus.UNREACHABLE
                    logger.warning("Tier3 %s: %s", job_id, exc)
                    raise _Abort from exc

                # -- step 2: browser context -----------------------------
                yield "opening_browser"
                page, closer = await self.browser_factory()

                # -- steps 3-4: navigate + classify, with the §2.7 in-job
                #    retry (once, only timeout/http_5xx, only if elapsed
                #    is still under 2,000 ms) ----------------------------
                yield "navigating"
                for nav_try in range(MAX_AGENT_RETRIES + 1):
                    retryable: str | None = None
                    try:
                        # Phase 1 §7.3 / §2.1: the Tier-3 budget is a HARD
                        # 4,000 ms per portal, not a per-step hint — spend only
                        # what is left of it, so a slow portal fails as a
                        # timeout instead of quietly blowing the budget.
                        goto_budget = max(1, self.timeout_ms - elapsed_ms())
                        resp = await page.goto(
                            portal_url,
                            wait_until="domcontentloaded",
                            timeout=goto_budget,
                        )
                    except Exception as exc:  # noqa: BLE001 — Playwright typed
                        name = type(exc).__name__.lower()
                        err = "timeout" if "timeout" in name else "dns_failure"
                        if err != "timeout" or elapsed_ms() >= RETRY_ELIGIBLE_MS:
                            error_class, status = err, VerificationStatus.UNREACHABLE
                            raise _Abort from exc
                        retryable = err
                    else:
                        navigated = True   # goto resolved; a page is worth keeping
                        # §2.3 step 3: then wait for "networkidle-lite" up to
                        # NAVIGATE_IDLE_WAIT_MS of the budget.  A busy portal
                        # that never goes idle must not fail the run — only the
                        # navigation itself is fatal.
                        idle = getattr(page, "wait_for_load_state", None)
                        idle_budget = min(
                            NAVIGATE_IDLE_WAIT_MS, max(0, self.timeout_ms - elapsed_ms())
                        )
                        if idle is not None and idle_budget > 0:
                            try:
                                await idle("networkidle", timeout=idle_budget)
                            except Exception:  # noqa: BLE001 — soft wait
                                pass
                        http_status = (
                            getattr(resp, "status", None) if resp is not None else None
                        )
                        if http_status is not None:
                            mark("http_status")
                            logger.debug("Tier3 %s: HTTP %s", job_id, http_status)
                        final_url = str(getattr(page, "url", portal_url) or portal_url)

                        if http_status is not None and int(http_status) >= 500:
                            retryable = "http_5xx"
                        else:
                            yield "inspecting_landing_page"
                            sig = await page.evaluate(INSPECT_JS)
                            sig = sig if isinstance(sig, dict) else {}
                            mark(SIGNAL_LANDING)
                            if sig.get("maintenance"):
                                retryable = "http_5xx"          # §2.3 step 4b
                            elif sig.get("waf"):
                                error_class, status = "waf_block", VerificationStatus.BLOCKED
                                raise _Abort
                            elif sig.get("captcha") and sig.get("control_state") != "enabled":
                                error_class, status = "captcha", VerificationStatus.BLOCKED
                                raise _Abort

                    if retryable is None:
                        break                                    # classified OK
                    # §2.7: MAX_AGENT_RETRIES = 1, timeout/http_5xx only,
                    # and only while elapsed < 2,000 ms.
                    if nav_try < MAX_AGENT_RETRIES and elapsed_ms() < RETRY_ELIGIBLE_MS:
                        logger.info("Tier3 %s: in-job retry after %s", job_id, retryable)
                        continue
                    error_class, status = retryable, VerificationStatus.UNREACHABLE
                    raise _Abort

                # -- steps 5-6: dismiss static notice pop-ups ------------
                yield "dismissing_notices"
                await self._dismiss(page, mark)

                # -- steps 7-8: apply control ----------------------------
                yield "detecting_submit_control"
                sig = await page.evaluate(INSPECT_JS)
                sig = sig if isinstance(sig, dict) else {}
                state = sig.get("control_state") or "absent"
                mark(f"submit_control_{state}", "control_count")
                if sig.get("overlay") and state != "enabled":
                    error_class, status = "waf_block", VerificationStatus.BLOCKED   # §2.3 step 6
                    raise _Abort
                if sig.get("closed_language"):
                    mark(SIGNAL_CLOSED_LANG)

                # -- steps 9-12: deadline --------------------------------
                yield "extracting_deadline"
                cands = [
                    (str(d.get("t", "")), bool(d.get("label")))
                    for d in (sig.get("deadlines") or [])[:DEADLINE_CANDIDATES]
                ]
                deadline, dl_signal, label_bound, extra = select_deadline(cands, today)
                mark(dl_signal, *extra)
                if deadline is not None and deadline < today:
                    mark("deadline_in_past")

            except _Abort:
                pass
            except Exception:  # noqa: BLE001 — degradation, never a 5xx
                error_class = error_class or "agent_error"
                status = VerificationStatus.UNREACHABLE
                logger.exception("Tier3 %s failed", job_id)

            # ====== phase B — evidence, verdict, persist, emit =========
            # (inside the outer try so the finally below can always close.)
            # §2.3 step 13 needs a page that actually loaded: §2.7 C3/C4 say
            # snapshot_url stays null when navigation itself failed.  Once the
            # hard budget is spent the run is already over-time (C3/TC-C8), so
            # evidence beyond the deadline buys nothing and must not be added.
            if page is not None and navigated and elapsed_ms() < self.timeout_ms:
                yield "capturing_snapshot"
                snapshot_url = await self._capture(page, scheme_id, job_id, mark)

            if status is None:
                yield "deciding_verdict"
                status, error_class, extra = decide(sig, deadline, label_bound, today)
                mark(*extra)

            yield "persisting_result"
            duration_ms = int((time.perf_counter() - started) * 1000)

            # §2.3 step 18 + §2.7 Edge Case C3 ("overall timeout") + TC-C8:
            # the job ALWAYS completes and is always persisted, but a run past
            # the frozen Tier-3 budget (Phase 1 §7.3, 4,000 ms) is reported as
            # UNREACHABLE with error_class=timeout and logged as over-budget.
            # The breach is a metric — there is no catalog signal for it, and
            # a run that already failed for a *specific* reason (dns_failure,
            # waf_block, captcha, ssrf_blocked) keeps that classification: the
            # budget must never mask the real cause.  "timeout" is an
            # error_class, not an observed_signals[] value.
            if duration_ms > self.timeout_ms:
                logger.warning(
                    "Tier3 %s over budget: %d ms > %d ms (logged as over-budget)",
                    job_id, duration_ms, self.timeout_ms,
                )
                if error_class is None:
                    status = VerificationStatus.UNREACHABLE
                    error_class = "timeout"

            result = VerificationResult(
                verification_status=status or VerificationStatus.UNREACHABLE,
                extracted_deadline=deadline,
                snapshot_url=snapshot_url,
                final_url=final_url,
                observed_signals=seen,
                error_class=error_class,
                duration_ms=duration_ms,
                job_id=job_id,
            )

            # §2.3 steps 15-16 — ALWAYS persist, success or failure.  This is
            # the last thing that happens to the verdict, so a Mongo outage can
            # never turn a completed run into a 5xx: _persist swallows its own.
            self._persist(
                scheme_id=scheme_id,
                result=result,
                portal_url=portal_url,
                snapshot_key=(
                    f"snapshots/{scheme_id}/{job_id}.png" if snapshot_url else None
                ),
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )

            yield result
        finally:
            # MUST NOT YIELD.  A `finally` that yields raises
            # "async generator ignored GeneratorExit" the moment a consumer
            # stops reading, which would also leak the browser context.
            # Cleanup therefore happens here, on every exit path.
            if closer is not None:
                try:
                    await closer()
                except Exception:  # noqa: BLE001 — teardown is best-effort
                    pass

    # -- helpers -----------------------------------------------------------
    def _persist(
        self,
        *,
        scheme_id: str,
        result: VerificationResult,
        portal_url: str,
        snapshot_key: str | None,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        """§2.3 steps 15-16 — the audit row, then the ``verification_*`` cluster.

        **Step 15** — one ``verification_logs`` document per run, success *or*
        failure (DATA_SPEC §8.1).  This is the only durable record that a run
        happened, so it is written first.

        **Step 16** — patch the Scheme cluster exactly as the DATA_SPEC §6
        outcome table prescribes: a successful run stamps ``verified_at`` and
        ``verification_job_id`` and clears ``last_known_status``; a failed run
        *preserves* ``verified_at``/``extracted_deadline`` (so ``is_stale``
        keeps ageing honestly) and records ``last_known_status``.

        Both writes are isolated: persistence is evidence, never the decision.
        A Mongo outage must not convert a finished verification into an error.
        """
        if self.db is None:
            return
        status = result.verification_status.value

        try:
            self.db.verification_logs.insert_one({
                "verification_job_id": result.job_id,
                "scheme_id": scheme_id,
                "portal_url": portal_url,
                "final_url": result.final_url,
                "verification_status": status,
                # ISO-8601 after normalisation (§8.1) — BSON has no `date`.
                "extracted_deadline": (
                    result.extracted_deadline.isoformat()
                    if result.extracted_deadline else None
                ),
                "observed_signals": list(result.observed_signals),
                "snapshot_key": snapshot_key,
                "duration_ms": int(result.duration_ms),
                "agent_version": self.agent_version,
                "error_class": result.error_class,
                "started_at": started_at,
                "finished_at": finished_at,
            })
        except Exception as exc:  # noqa: BLE001 — audit write must never raise
            logger.warning("verification_logs insert failed for %s: %s", result.job_id, exc)
            # keep going — the Scheme cluster is what the citizen actually sees,
            # and the two writes are independent (see method docstring).

        try:
            prior = dict(self.db.schemes.find_one({"scheme_id": scheme_id}) or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheme read failed for %s: %s", scheme_id, exc)
            prior = {}

        if status in INTAKE_STATUSES:
            patch = {
                "verification_status": status,
                "verified_at": finished_at,          # server-generated, fresh
                "extracted_deadline": (
                    result.extracted_deadline.isoformat()
                    if result.extracted_deadline else None
                ),
                "snapshot_url": result.snapshot_url,
                "verification_job_id": result.job_id,
                "last_known_status": None,
            }
        else:
            prior_status = prior.get("verification_status")
            patch = {
                "verification_status": status,
                "verified_at": prior.get("verified_at"),            # preserved
                "extracted_deadline": prior.get("extracted_deadline"),  # preserved
                "snapshot_url": None,           # §6: challenge/timeout evidence
                "verification_job_id": None,    # lives in verification_logs only
                "last_known_status": (
                    prior_status if prior_status in INTAKE_STATUSES
                    else prior.get("last_known_status")
                ),
            }

        try:
            self.db.schemes.update_one({"scheme_id": scheme_id}, {"$set": patch})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "verification cluster update failed for %s: %s", scheme_id, exc
            )

    async def _dismiss(self, page: Any, mark: Callable[..., None]) -> None:
        """§2.3 step 5 — bounded dismissals (max 5 attempts, 800 ms)."""
        budget = time.monotonic() + DISMISS_BUDGET_MS / 1000
        for _ in range(DISMISS_MAX_ATTEMPTS):
            if time.monotonic() >= budget:
                break
            try:
                res = await page.evaluate(DISMISS_JS)
            except Exception:  # noqa: BLE001
                res = {"dismissed": 0, "cookie": False}
            res = res if isinstance(res, dict) else {}
            dismissed = int(res.get("dismissed") or 0)
            if res.get("cookie"):
                mark(SIGNAL_COOKIE, SIGNAL_POPUP)
            if dismissed:
                mark(SIGNAL_POPUP)
                continue
            # Escape -> give up (step 5b/6)
            keyboard = getattr(page, "keyboard", None)
            if keyboard is not None:
                try:
                    await keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
            break

    async def _capture(
        self, page: Any, scheme_id: str, job_id: str, mark: Callable[..., None]
    ) -> str | None:
        """§2.3 step 13 — viewport PNG -> ``snapshots/{scheme_id}/{job_id}.png``."""
        try:
            png = await page.screenshot(type="png")
        except Exception:  # noqa: BLE001
            mark(SIGNAL_SNAPSHOT_FAILED)
            return None
        if not png:
            mark(SIGNAL_SNAPSHOT_FAILED)
            return None
        store = self.snapshots if self.snapshots is not None else SnapshotStore()
        url = await store.put(f"snapshots/{scheme_id}/{job_id}.png", bytes(png))
        mark(SIGNAL_SNAPSHOT_OK if url else SIGNAL_SNAPSHOT_FAILED)
        return url
