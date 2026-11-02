"""Polymarket Data API client for wallet activity + positions.

Wraps GET /activity and GET /positions from data-api.polymarket.com. Used by
the analysis layer (wallet classification, edge-decay research) as an
independent data source — explicitly not consuming our production
conviction-gated stream (low_buy_trades etc.).

Scope:
- Paginated activity fetch with newest-first ordering
- Single-call positions fetch
- Typed dataclass response (ActivityRow) preserving all 21 observed fields

Retry policy: network exceptions and HTTP 5xx only. Empty responses are
returned verbatim — callers decide what "empty" means for their use.

Observed activity types (2026-04-21 probing): TRADE (side=BUY|SELL),
REDEEM, MERGE, REWARD, MAKER_REBATE. New types may appear; ActivityRow
preserves the raw type string and raw row.

Observe-and-revise triggers (if these happen in production, add logic):
- Empty-response rate > 2% on wallets known to have data -> add retry-on-empty
- proxy_wallet != queried user in real data -> add proxy normalization
- Rate-limit hits -> add request queuing
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

BASE_URL = "https://data-api.polymarket.com"
DEFAULT_TIMEOUT = 15
PAGE_LIMIT_CAP = 500
_NETWORK_RETRY_BACKOFFS = (0.5, 1.5)
# Polymarket Data API rejects the default Python-urllib User-Agent with HTTP 403.
# Any other UA string is accepted. Using an identifying one for operator traceability.
_USER_AGENT = "sniper-analytics/1.0"


class PolymarketActivityError(Exception):
    """Raised on HTTP non-2xx (non-retryable), JSON parse failure, or repeated network errors."""


@dataclass
class ActivityRow:
    proxy_wallet: str
    timestamp: int
    condition_id: str
    activity_type: str  # TRADE / REDEEM / MERGE / REWARD / MAKER_REBATE / ...
    side: str | None    # BUY or SELL for TRADE rows, None otherwise
    size: float | None
    usdc_size: float | None
    price: float | None
    outcome: str | None
    outcome_index: int | None
    slug: str
    title: str
    event_slug: str
    transaction_hash: str
    asset: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_dict(cls, r: dict[str, Any]) -> "ActivityRow":
        def _f(v: Any) -> float | None:
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _i(v: Any) -> int | None:
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        # Identity fields — raise rather than silently coerce.
        # timestamp: missing/invalid = unfilterable row, would break since_ts cutoffs.
        ts_raw = r.get("timestamp")
        if ts_raw is None:
            raise PolymarketActivityError(f"row missing timestamp: {r!r}")
        try:
            timestamp = int(ts_raw)
        except (TypeError, ValueError) as exc:
            raise PolymarketActivityError(
                f"row has invalid timestamp {ts_raw!r}: {exc}"
            ) from exc

        # condition_id: required for TRADE/REDEEM/MERGE (position-bearing types).
        # REWARD and MAKER_REBATE have no market and may legitimately lack it.
        activity_type = str(r.get("type") or "")
        condition_id = str(r.get("conditionId") or "")
        if activity_type in ("TRADE", "REDEEM", "MERGE") and not condition_id:
            raise PolymarketActivityError(
                f"{activity_type} row missing conditionId: {r!r}"
            )

        raw_side = r.get("side")
        side = raw_side if raw_side in ("BUY", "SELL") else None

        return cls(
            proxy_wallet=str(r.get("proxyWallet") or ""),
            timestamp=timestamp,
            condition_id=condition_id,
            activity_type=activity_type,
            side=side,
            size=_f(r.get("size")),
            usdc_size=_f(r.get("usdcSize")),
            price=_f(r.get("price")),
            outcome=(r.get("outcome") or None),
            outcome_index=_i(r.get("outcomeIndex")),
            slug=str(r.get("slug") or ""),
            title=str(r.get("title") or ""),
            event_slug=str(r.get("eventSlug") or ""),
            transaction_hash=str(r.get("transactionHash") or ""),
            asset=str(r.get("asset") or ""),
            raw=dict(r),  # defensive copy — prevent caller mutation reaching original
        )


def _get_json(path: str, *, timeout: int = DEFAULT_TIMEOUT) -> Any:
    """GET and parse JSON. Retries on network/timeout/5xx only. Empty body → [] raised as parse error."""
    url = f"{BASE_URL}{path}"
    last_exc: Exception | None = None
    for backoff in (0.0,) + _NETWORK_RETRY_BACKOFFS:
        if backoff:
            time.sleep(backoff)
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                body = resp.read().decode("utf-8")
            if 500 <= status < 600:
                last_exc = PolymarketActivityError(f"HTTP {status} on {url}")
                continue
            if not 200 <= status < 300:
                raise PolymarketActivityError(f"HTTP {status} on {url}: {body[:200]}")
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise PolymarketActivityError(f"invalid JSON from {url}: {exc}") from exc
        except urllib.error.HTTPError as exc:
            if 500 <= exc.code < 600:
                last_exc = exc
                continue
            # Include response body preview — critical for diagnosing 4xx like
            # the 403 we hit from default Python-urllib UA.
            try:
                body_preview = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                body_preview = ""
            raise PolymarketActivityError(
                f"HTTP {exc.code} on {url}: {body_preview}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
    raise PolymarketActivityError(f"request failed after retries: {last_exc}")


def get_activity_page(
    wallet: str, *, limit: int = PAGE_LIMIT_CAP, offset: int = 0,
) -> list[ActivityRow]:
    """Fetch a single page of activity. Returns [] if the endpoint does."""
    limit = min(max(int(limit), 1), PAGE_LIMIT_CAP)
    path = (
        f"/activity"
        f"?user={urllib.parse.quote(wallet)}"
        f"&limit={limit}"
        f"&offset={int(offset)}"
    )
    rows = _get_json(path)
    if not isinstance(rows, list):
        raise PolymarketActivityError(
            f"unexpected response shape for {path}: {type(rows).__name__}"
        )
    return [ActivityRow.from_dict(r) for r in rows]


def iter_activity(wallet: str, *, since_ts: int | None = None) -> Iterator[ActivityRow]:
    """Paginate full history for wallet, newest → oldest.

    Termination:
    - Empty page (true end of history)
    - Partial page (< PAGE_LIMIT_CAP rows — also end of history)
    - If since_ts set: the ENTIRE current page is pre-cutoff. Rows within a
      page with timestamp < since_ts are skipped individually (not yielded),
      but do not terminate iteration — the API may return rows slightly out
      of order within a page, and terminating on the first pre-cutoff row
      would silently drop later in-window rows on the same page.

    Assumes pages are approximately newest-first across pages (we've verified
    this empirically). If the API ever returns cross-page ordering instability,
    this logic still holds — we only stop when a full page is pre-cutoff.
    """
    offset = 0
    while True:
        page = get_activity_page(wallet, limit=PAGE_LIMIT_CAP, offset=offset)
        if not page:
            return

        all_pre_cutoff = True
        for row in page:
            if since_ts is not None and row.timestamp < since_ts:
                continue  # skip, but do not terminate
            all_pre_cutoff = False
            yield row

        if since_ts is not None and all_pre_cutoff:
            # No rows in this page met the cutoff → next page is guaranteed older
            return
        if len(page) < PAGE_LIMIT_CAP:
            return
        offset += PAGE_LIMIT_CAP


def get_positions(
    wallet: str, *, size_threshold: float = 0.0, limit: int = 500,
) -> list[dict[str, Any]]:
    """Current open positions. Returns raw dict list (shape not yet dataclass'd).

    Raises PolymarketActivityError on suspected truncation (len == limit) —
    vaults and large arb wallets may have >500 open positions; silent
    truncation would produce incorrect analytics. If you hit this, re-probe
    /positions API to verify offset pagination semantics before raising limit.
    """
    path = (
        f"/positions"
        f"?user={urllib.parse.quote(wallet)}"
        f"&sizeThreshold={size_threshold}"
        f"&limit={int(limit)}"
    )
    rows = _get_json(path)
    if not isinstance(rows, list):
        raise PolymarketActivityError(
            f"unexpected response shape for {path}: {type(rows).__name__}"
        )
    if len(rows) == limit:
        raise PolymarketActivityError(
            f"/positions returned exactly limit={limit} rows for {wallet} — "
            f"suspected truncation. Pagination not implemented; re-probe API "
            f"semantics before raising the limit."
        )
    return rows
