"""Goldsky-public Polymarket subgraph reader.

Closes wallet-protocol-bulletproofing C2 (orderbook fills beyond the 20K
REST cap) and feeds C1 (capital ledger via splits / merges / redemptions).

Endpoints
---------
- orderbook-subgraph/0.0.1 → `OrderFilledEvent` per CLOB fill
- activity-subgraph/0.0.4 → `Split`, `Merge`, `Redemption` per onchain
  conditional-tokens event

Both are public (Goldsky-hosted), no auth, documented rate limit 50 req
per 10s. No row cap when paginating with `id_gt`. Verified live 2026-04-27
(reference memory `reference/c1-c2-free-alternatives-2026-04-27`).

Pagination contract
-------------------
We use `id_gt` cursor pagination rather than `timestamp_gt`. Each entity's
`id` is unique (txhash + log index or order hash) so cursor advances are
deterministic and don't lose ties at the same timestamp. The trade-off is
that returned rows are in id-lexicographic order, not chronological — the
caller sorts by timestamp downstream when needed. Page size capped at
1000 (Goldsky hard cap; first:1001 returns an error).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

ORDERBOOK_ENDPOINT = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/subgraphs/"
    "orderbook-subgraph/0.0.1/gn"
)
ACTIVITY_ENDPOINT = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/subgraphs/"
    "activity-subgraph/0.0.4/gn"
)

# Goldsky enforces a hard cap of 1000 entities per `first:` argument.
PAGE_SIZE = 1000

# Floor for adaptive page-size shrink on statement-timeout. Below this the
# query is so small that a persistent timeout means a real backend problem,
# not a too-large-page problem — so we stop shrinking and raise.
MIN_PAGE_SIZE = 50

# Max consecutive timeout-shrink retries on a single page before giving up.
_MAX_TIMEOUT_RETRIES = 6

# Documented limit is 50 req / 10s. We pace to stay well under it across
# concurrent backfills.
RATE_LIMIT_SLEEP_SEC = 0.05

# Substrings that mark a Goldsky/Postgres statement-timeout in a GraphQL
# error message — recoverable by shrinking the page size.
_TIMEOUT_MARKERS = ("statement timeout", "canceling statement", "timeout")


class SubgraphQueryError(RuntimeError):
    """A GraphQL query returned an `errors` payload (HTTP 200 but no data).

    Goldsky returns HTTP 200 with an `errors` array when a query fails
    server-side (most commonly a Postgres statement timeout on a
    high-volume wallet). Without this, `_paginate` would read the absent
    `data` field as "no more rows" and silently truncate/zero the result —
    the exact bug behind the 2026-05-27 0x20ad75 activity_rows=0 capture.
    """

# Default timeout for Goldsky subgraph requests.
_DEFAULT_SUBGRAPH_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0)


def _get_subgraph_client() -> httpx.Client:
    """Return a configured httpx.Client for Goldsky subgraph requests."""
    return httpx.Client(
        timeout=_DEFAULT_SUBGRAPH_TIMEOUT,
        transport=httpx.HTTPTransport(retries=0),
        headers={"User-Agent": "polymarket-insider-tracker/1.0"},
    )


def _post(
    endpoint: str,
    query: str,
    *,
    session: httpx.Client,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """POST a GraphQL query; raise on HTTP error; return the parsed JSON."""
    resp = session.post(endpoint, json={"query": query}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _post_checked(
    endpoint: str,
    query: str,
    *,
    session: httpx.Client,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """`_post` + GraphQL-error detection.

    Goldsky returns HTTP 200 with a top-level `errors` array when a query
    fails server-side. `_post` alone (HTTP-only raise_for_status) does not
    catch that. Raise `SubgraphQueryError` so callers can retry or fail
    loudly instead of misreading a failed query as an empty result.
    """
    payload = _post(endpoint, query, session=session, timeout=timeout)
    errors = payload.get("errors")
    if errors:
        msg = "; ".join(str(e.get("message", "")) for e in errors) or "unknown GraphQL error"
        raise SubgraphQueryError(msg)
    return payload


def _is_timeout_error(exc: SubgraphQueryError) -> bool:
    low = str(exc).lower()
    return any(marker in low for marker in _TIMEOUT_MARKERS)


def _paginate(
    endpoint: str,
    *,
    entity: str,
    where_extras: str,
    fields: str,
    session: httpx.Client,
    page_size: int = PAGE_SIZE,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    """Cursor-paginate a Goldsky entity by `id_gt` until empty or max_rows.

    `where_extras` is the inner where-clause body (e.g.
    `maker: "0xwallet"`); we splice in the `id_gt` cursor and assemble
    the full query.
    """
    rows: list[dict[str, Any]] = []
    cursor = ""
    effective_page = page_size
    while True:
        # GraphQL string interpolation — id_gt always present, even on the
        # first call where it's empty. Goldsky treats `id_gt: ""` as
        # "everything" since IDs are non-empty hex strings.
        where = f'id_gt: "{cursor}", {where_extras}'

        # Fetch one page, shrinking the page size and retrying on a
        # statement-timeout. High-volume wallets (12K+ fills) intermittently
        # trip the subgraph's Postgres statement timeout at first:1000; a
        # smaller page completes. We halve down to MIN_PAGE_SIZE; if even
        # that times out _MAX_TIMEOUT_RETRIES times, we raise rather than
        # silently return a truncated/empty result.
        timeout_retries = 0
        while True:
            query = (
                f"{{ {entity}("
                f"first: {effective_page}, "
                f"orderBy: id, orderDirection: asc, "
                f"where: {{ {where} }}"
                f") {{ {fields} }} }}"
            )
            try:
                payload = _post_checked(endpoint, query, session=session)
                break
            except SubgraphQueryError as exc:
                if (
                    _is_timeout_error(exc)
                    and timeout_retries < _MAX_TIMEOUT_RETRIES
                ):
                    timeout_retries += 1
                    if effective_page > MIN_PAGE_SIZE:
                        effective_page = max(MIN_PAGE_SIZE, effective_page // 2)
                    # Exponential backoff before retrying the (smaller) page.
                    time.sleep(RATE_LIMIT_SLEEP_SEC * (2 ** timeout_retries))
                    continue
                # Non-timeout error, or timeout that survived MIN_PAGE_SIZE +
                # max retries — fail loudly so the caller flags it honestly
                # instead of misreading it as "no more rows".
                raise

        data = (payload.get("data") or {}).get(entity) or []
        if not data:
            break
        rows.extend(data)
        cursor = data[-1]["id"]
        if max_rows is not None and len(rows) >= max_rows:
            return rows[:max_rows]
        if len(data) < effective_page:
            break  # last page partially filled
        time.sleep(RATE_LIMIT_SLEEP_SEC)
    return rows


# ---------------------------------------------------------------------------
# C2 — orderbook fills
# ---------------------------------------------------------------------------


_ORDER_FILLED_FIELDS = (
    "id timestamp maker taker makerAssetId takerAssetId "
    "makerAmountFilled takerAmountFilled fee transactionHash"
)


def fetch_order_filled_events(
    wallet: str,
    *,
    session: httpx.Client | None = None,
    side: str = "both",
    page_size: int = PAGE_SIZE,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    """Return all `OrderFilledEvent`s where `wallet` is maker, taker, or both.

    `side` ∈ {"maker", "taker", "both"}. When "both", queries are
    issued separately and unioned by `id` (a fill where the wallet is
    on both sides — possible but rare — appears once).

    GraphQL where-clauses are case-sensitive and addresses are stored
    lowercased; the wallet input is normalized.
    """
    sess = session or _get_subgraph_client()
    addr = wallet.lower()
    if side not in ("maker", "taker", "both"):
        raise ValueError(f"side must be maker|taker|both, got {side!r}")

    all_rows: list[dict[str, Any]] = []
    if side in ("maker", "both"):
        all_rows.extend(_paginate(
            ORDERBOOK_ENDPOINT,
            entity="orderFilledEvents",
            where_extras=f'maker: "{addr}"',
            fields=_ORDER_FILLED_FIELDS,
            session=sess,
            page_size=page_size,
            max_rows=max_rows,
        ))
        if max_rows is not None and len(all_rows) >= max_rows:
            return all_rows[:max_rows]
    if side in ("taker", "both"):
        all_rows.extend(_paginate(
            ORDERBOOK_ENDPOINT,
            entity="orderFilledEvents",
            where_extras=f'taker: "{addr}"',
            fields=_ORDER_FILLED_FIELDS,
            session=sess,
            page_size=page_size,
            max_rows=max_rows,
        ))

    if side == "both":
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for r in all_rows:
            rid = r.get("id")
            if rid in seen:
                continue
            seen.add(rid)
            deduped.append(r)
        all_rows = deduped

    if max_rows is not None:
        all_rows = all_rows[:max_rows]
    return all_rows


# ---------------------------------------------------------------------------
# C1 — capital ledger entities
# ---------------------------------------------------------------------------


# activity-subgraph entity fields. Split + Merge share the (stakeholder,
# amount) shape; Redemption diverges on both axes (redeemer + payout +
# indexSets). Field sets verified via GraphQL introspection 2026-04-27.
_SPLIT_MERGE_FIELDS = "id timestamp stakeholder condition amount"
_REDEMPTION_FIELDS = "id timestamp redeemer condition indexSets payout"


def fetch_splits(
    wallet: str,
    *,
    session: httpx.Client | None = None,
    page_size: int = PAGE_SIZE,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    """SPLIT events — onchain USDC → conditional-tokens (deposit-equivalent)."""
    sess = session or _get_subgraph_client()
    return _paginate(
        ACTIVITY_ENDPOINT,
        entity="splits",
        where_extras=f'stakeholder: "{wallet.lower()}"',
        fields=_SPLIT_MERGE_FIELDS,
        session=sess,
        page_size=page_size,
        max_rows=max_rows,
    )


def fetch_merges(
    wallet: str,
    *,
    session: httpx.Client | None = None,
    page_size: int = PAGE_SIZE,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    """MERGE events — onchain conditional-tokens → USDC (withdrawal-equivalent)."""
    sess = session or _get_subgraph_client()
    return _paginate(
        ACTIVITY_ENDPOINT,
        entity="merges",
        where_extras=f'stakeholder: "{wallet.lower()}"',
        fields=_SPLIT_MERGE_FIELDS,
        session=sess,
        page_size=page_size,
        max_rows=max_rows,
    )


def fetch_redemptions(
    wallet: str,
    *,
    session: httpx.Client | None = None,
    page_size: int = PAGE_SIZE,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    """REDEMPTION events — settlement of resolved conditions.

    Schema differs from Split/Merge: stakeholder field is `redeemer`,
    amount field is `payout`, and rows additionally carry `indexSets`
    (the resolved outcome set as a list of BigInts).
    """
    sess = session or _get_subgraph_client()
    return _paginate(
        ACTIVITY_ENDPOINT,
        entity="redemptions",
        where_extras=f'redeemer: "{wallet.lower()}"',
        fields=_REDEMPTION_FIELDS,
        session=sess,
        page_size=page_size,
        max_rows=max_rows,
    )


__all__ = [
    "ORDERBOOK_ENDPOINT",
    "ACTIVITY_ENDPOINT",
    "PAGE_SIZE",
    "fetch_order_filled_events",
    "fetch_splits",
    "fetch_merges",
    "fetch_redemptions",
]
