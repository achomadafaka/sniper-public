"""wallet_event_ledger — ingestion + normalization only. Never decides edge."""
from __future__ import annotations

import json
import os
import sys
from typing import Iterable, Mapping

from sniper.markets import polymarket_api as P
from sniper.analysis.winning_wallet_thesis.types import LedgerRow, WalletEventLedger

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))), "runs", "wallet_anatomy")

# Incremental refresh re-fetches from (cursor - overlap), not exactly the last stored ts, so a
# Polymarket row backfilled with a timestamp slightly older than our newest stored event isn't
# missed. Dedup (_merge_activity) collapses the re-fetched overlap, so this only costs a little
# extra fetch. 2 days is generous vs observed backfill lag. (review #MED 2026-06-17)
_INCREMENTAL_OVERLAP_S = 2 * 86400

_SIDE_KIND = {"BUY": "buy", "SELL": "sell"}
_TYPE_KIND = {"REDEEM": "redeem", "SPLIT": "split", "MERGE": "merge",
              "REWARD": "reward", "YIELD": "reward", "MAKER_REBATE": "reward"}


def normalize(activity: Iterable[Mapping]) -> list[LedgerRow]:
    """Map raw Data-API activity rows to typed LedgerRows. `seq` preserves original feed
    order so same-timestamp BUY/SELL legs fold deterministically downstream. Reward/yield
    rows (which can be wallet-level, no conditionId) are KEPT — they drive #12 attribution."""
    rows: list[LedgerRow] = []
    for i, a in enumerate(activity):
        typ = a.get("type")
        if typ == "TRADE":
            kind = _SIDE_KIND.get((a.get("side") or "").upper())
        else:
            kind = _TYPE_KIND.get(typ)
        if kind is None:
            continue
        cond = a.get("conditionId") or a.get("condition_id")
        if not cond and kind != "reward":          # only reward rows may be conditionless
            continue
        oidx = a.get("outcomeIndex")
        try:
            oidx = int(oidx) if oidx is not None else None
        except (TypeError, ValueError):
            oidx = None
        rows.append(LedgerRow(
            kind=kind, wallet=str(a.get("proxyWallet") or ""), condition_id=str(cond or ""),
            ts=int(a.get("timestamp") or 0), seq=i, outcome_index=oidx,
            outcome=a.get("outcome") or "", price=float(a.get("price") or 0.0),
            size=float(a.get("size") or 0.0), usdc=float(a.get("usdcSize") or 0.0),
            tx=a.get("transactionHash") or "", event_slug=a.get("eventSlug") or "",
            title=a.get("title") or ""))
    return rows


def _activity_path(wallet: str) -> str:
    return os.path.join(CACHE_DIR, f"{wallet.lower()}_activity.json")


def _activity_identity(row: Mapping) -> tuple:
    """Same per-fill identity used by the Data API fetcher, so incremental cache merges
    preserve distinct fills while collapsing rows re-fetched across the cursor boundary."""
    h = row.get("transactionHash") or row.get("transaction_hash") or ""
    if h:
        return (
            h, int(row.get("timestamp") or 0), row.get("type") or "", row.get("side") or "",
            str(row.get("conditionId") or row.get("condition_id") or ""),
            row.get("outcomeIndex"), str(row.get("asset") or ""),
            row.get("price"), row.get("size"), row.get("usdcSize"),
        )
    return (
        "", int(row.get("timestamp") or 0), row.get("type") or "", row.get("side") or "",
        str(row.get("conditionId") or row.get("condition_id") or ""),
        row.get("outcomeIndex"), row.get("price"), row.get("size"), row.get("usdcSize"),
        row.get("eventSlug") or "", row.get("title") or "",
    )


def _merge_activity(new_rows: Iterable[Mapping], cached_rows: Iterable[Mapping]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()
    for row in list(new_rows) + list(cached_rows):
        key = _activity_identity(row)
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    return out


def _write_activity(path: str, rows: list[dict]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f)
    os.replace(tmp, path)


class LedgerFetchError(Exception):
    """Activity fetch terminated abnormally (refused / failed mid-walk) rather than reaching a
    natural end. Raised so the caller FAILS LOUDLY instead of treating a truncated/empty fetch as
    a complete history — which would produce a false 'insufficient_evidence' read and (worse) cache
    the empty result. Carries the termination reason + HTTP status for geo/auth classification."""

    def __init__(self, reason: str | None, status=None):
        super().__init__(f"activity fetch terminated abnormally: {reason} (status={status})")
        self.reason = reason
        self.status = status


# Terminations that mean "the walk did not finish" — as opposed to natural ends
# (partial_page / empty_page / complete). These must surface, never be cached.
_ABNORMAL_TERMINATION = {"api_refused", "request_failed"}


def _fetch_activity(wallet: str, *, max_pages: int, start_ts: int | None = None) -> list[dict]:
    s = P.build_session()
    term: dict = {}

    def emit(event_name, payload=None):
        if event_name == "fetch_terminated" and isinstance(payload, dict):
            term["reason"] = payload.get("reason")
            term["status"] = payload.get("status")
        print(f"  ...{event_name}", file=sys.stderr)

    rows = P.fetch_user_activity(s, wallet, activity_type=None, page_limit=500,
                                 max_pages=max_pages, walk_full_history=True,
                                 start_ts=start_ts, progress_emitter=emit)
    if term.get("reason") in _ABNORMAL_TERMINATION:
        # Surface — do NOT return partial rows (the caller would cache them as a full history).
        raise LedgerFetchError(term.get("reason"), term.get("status"))
    return rows


def load_activity(wallet: str, *, refresh: bool = False, max_pages: int = 400,
                  incremental: bool = False) -> list[dict]:
    """Backward-walked activity history, cached to disk.

    `incremental=True` only changes the raw activity fetch: it fetches rows at/after the
    last persisted analyzer activity timestamp, merges them into the full cached ledger,
    and then downstream analysis still recomputes from the complete merged ledger.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _activity_path(wallet)
    if os.path.exists(path) and not refresh:
        with open(path) as f:
            return json.load(f)
    if refresh and incremental and os.path.exists(path):
        from sniper.analysis.winning_wallet_thesis import db_store
        state = db_store.analysis_state(wallet)
        cursor = state.get("max_activity_ts") if state.get("has_store") else None
        if cursor is not None:
            with open(path) as f:
                cached = json.load(f)
            start_ts = max(0, int(cursor) - _INCREMENTAL_OVERLAP_S)   # overlap guards backfills
            acts = _merge_activity(_fetch_activity(wallet, max_pages=max_pages, start_ts=start_ts), cached)
            _write_activity(path, acts)
            return acts
    acts = _fetch_activity(wallet, max_pages=max_pages, start_ts=None)
    _write_activity(path, acts)
    return acts


def build_ledger(wallet: str, *, refresh: bool = False, incremental: bool = False) -> WalletEventLedger:
    acts = load_activity(wallet, refresh=refresh, incremental=incremental)
    return WalletEventLedger(wallet=wallet, rows=normalize(acts), raw_activity=acts)
