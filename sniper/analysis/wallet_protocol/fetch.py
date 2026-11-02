"""Stage 0/pre-Stage-1 data fetching for the v2 protocol.

Per spec §4: fetch all four data sources before ANY analysis. The protocol
explicitly forbids peeking at performance numbers between fetch and stage 1.

This module wraps:
1. /closed-positions (uncapped, DESC/TIMESTAMP sort — newest first, spec A.1)
2. /positions          (uncapped, no sort params)
3. /activity?type=TRADE (full walk by default, spec A.2)
4. wallet_archetype_extractor execution_profile (optional)

Returns one WalletData object that downstream gates and branches consume.
No analysis happens here. No P&L numbers are computed. Just data retrieval.
"""

from __future__ import annotations

import logging
import time

import httpx

import sniper.markets.polymarket_api as polymarket_api
from sniper.analysis.wallet_protocol.capital_ledger import compute_capital_ledger
from sniper.analysis.wallet_protocol.subgraph_reader import (
    fetch_order_filled_events,
    fetch_redemptions,
    fetch_splits,
    fetch_merges,
)
from sniper.analysis.wallet_protocol.types import WalletData

logger = logging.getLogger(__name__)


_ACTIVITY_CAP = 3000  # Polymarket /activity hard cap; verified 2026-04-25

# USDC collateral in the Polymarket CLOB orderbook is token ID "0".
# Assumption: verified by test fixtures (makerAssetId="0" in synthetic fills).
_CLOB_USDC_TOKEN_ID = "0"
_CLOB_TOKEN_SCALE = 1_000_000  # both USDC and CTF token amounts are 6-decimal


def _build_token_lookup(activity_rows: list[dict]) -> dict[str, tuple[str, str]]:
    """Build {asset_id: (conditionId, outcome)} from REST /activity rows.

    REST rows carry the CLOB token ID (`asset`), `conditionId`, and `outcome`.
    USDC token ID excluded — it's never an outcome.
    """
    lookup: dict[str, tuple[str, str]] = {}
    for row in activity_rows:
        asset = str(row.get("asset") or "").strip()
        cid = str(row.get("conditionId") or "").strip()
        outcome = str(row.get("outcome") or "").strip()
        if not asset or not cid or not outcome:
            continue
        if asset == _CLOB_USDC_TOKEN_ID:
            continue
        lookup[asset] = (cid, outcome)
    return lookup


def _normalize_order_fill(
    event: dict,
    wallet: str,
    token_lookup: dict[str, tuple[str, str]],
) -> dict | None:
    """Normalize one OrderFilledEvent to REST /activity row shape.

    Returns None for unmapped tokens, zero outcome amount, degenerate fills.

    BUY/SELL logic (wallet's perspective):
        wallet==maker, makerAssetId==USDC  → BUY (wallet paid USDC for outcome)
        wallet==maker, makerAssetId!=USDC  → SELL (wallet gave outcome for USDC)
        wallet==taker, takerAssetId==USDC  → BUY
        wallet==taker, takerAssetId!=USDC  → SELL

    Price/size: both assets are 6-decimal.
        price = usdc_amount / outcome_amount  (ratio in [0,1])
        size  = outcome_amount / 1_000_000    (shares)
    """
    wallet_lc = wallet.lower()
    is_maker = (event.get("maker") or "").lower() == wallet_lc
    is_taker = (event.get("taker") or "").lower() == wallet_lc
    if not is_maker and not is_taker:
        return None

    maker_asset = str(event.get("makerAssetId") or "")
    taker_asset = str(event.get("takerAssetId") or "")
    maker_is_usdc = maker_asset == _CLOB_USDC_TOKEN_ID
    taker_is_usdc = taker_asset == _CLOB_USDC_TOKEN_ID

    if maker_is_usdc and taker_is_usdc:
        return None

    try:
        maker_amt = int(event.get("makerAmountFilled") or 0)
        taker_amt = int(event.get("takerAmountFilled") or 0)
    except (TypeError, ValueError):
        return None

    if is_maker:
        if maker_is_usdc:
            side, outcome_token = "BUY", taker_asset
            usdc_amt, outcome_amt = maker_amt, taker_amt
        else:
            side, outcome_token = "SELL", maker_asset
            usdc_amt, outcome_amt = taker_amt, maker_amt
    else:  # is_taker
        if taker_is_usdc:
            side, outcome_token = "BUY", maker_asset
            usdc_amt, outcome_amt = taker_amt, maker_amt
        else:
            side, outcome_token = "SELL", taker_asset
            usdc_amt, outcome_amt = maker_amt, taker_amt

    if outcome_amt == 0:
        return None

    mapping = token_lookup.get(outcome_token)
    if mapping is None:
        return None

    condition_id, outcome = mapping
    price = usdc_amt / outcome_amt
    size = outcome_amt / _CLOB_TOKEN_SCALE

    try:
        ts = int(event.get("timestamp") or 0)
    except (TypeError, ValueError):
        return None

    return {
        "conditionId": condition_id,
        "outcome": outcome,
        "side": side,
        "asset": outcome_token,
        "price": price,
        "size": size,
        "timestamp": ts,
        "transactionHash": str(event.get("transactionHash") or ""),
        "type": "TRADE",
        "_source": "subgraph",
    }


def fetch_wallet_data(
    wallet: str,
    session: httpx.Client | None = None,
    archetype_extractor=None,
    walk_full_history: bool = True,
    with_capital_ledger: bool = False,
    progress_emitter=None,
) -> WalletData:
    """Fetch all four data sources for one wallet.

    `archetype_extractor` is an optional callable. The legacy contract is a
    2-tuple `(archetype_layer1, execution_profile_dict)`; an optional 3-tuple
    `(archetype_layer1, archetype_layer2, execution_profile_dict)` is also
    accepted, with `archetype_layer2` plumbed onto WalletData as a
    display-only field. Typically wraps `scripts/wallet_archetype_extractor.py`.
    If None, those fields are skipped and downstream gates that depend on
    archetype will mark themselves underpowered (Stage 1 R1 cannot reject
    without an archetype label).

    `walk_full_history`: when True (default per spec A.2), walks `end=<ts>`
    windows on /activity to fetch beyond the 3000-row offset cap. Costs more
    time + API calls but eliminates the activity-cap blind spot for high-volume
    wallets. Pass False (--bounded-history CLI flag) to revert to the old
    single-window 3K-row behavior.

    Side effects: emits structured log lines for each fetch with row counts.
    """
    # session=None → _request_json uses the module-level httpx.Client
    started = time.time()
    logger.info("wallet_protocol fetch start wallet=%s", wallet[:14])

    # fetch_termination captures why each endpoint stopped paginating.
    # Populated only when progress_emitter is wired; consumers (analyze_wallet)
    # read data.fetch_termination to build time_coverage.fetch_termination.
    _fetch_termination: dict = {}

    # 1. /closed-positions
    _closed_result = polymarket_api.fetch_user_positions(
        session, wallet, endpoint="closed-positions",
        progress_emitter=progress_emitter,
    )
    if isinstance(_closed_result, tuple):
        closed, _closed_term = _closed_result
        _fetch_termination["closed_positions"] = _closed_term.reason
    else:
        closed = list(_closed_result)
    # Proxywal mismatch check (previously in fetch_closed_positions)
    for row in closed:
        proxy = (row.get("proxyWallet") or "").lower()
        if proxy and proxy != wallet.lower():
            logger.warning(
                "fetch_wallet_data: proxyWallet %s != wallet %s",
                row.get("proxyWallet"),
                wallet,
            )
    logger.info(
        "wallet_protocol closed_positions wallet=%s n=%d",
        wallet[:14], len(closed),
    )

    # 2. /positions (open)
    _open_result = polymarket_api.fetch_user_positions(
        session, wallet, endpoint="positions",
        progress_emitter=progress_emitter,
    )
    # Real function returns (rows, FetchTermination); legacy test mocks return a list.
    if isinstance(_open_result, tuple):
        open_positions, _open_term = _open_result
        _fetch_termination["open_positions"] = _open_term.reason
    else:
        open_positions = list(_open_result)
    logger.info(
        "wallet_protocol open_positions wallet=%s n=%d",
        wallet[:14], len(open_positions),
    )

    # 3. /activity?type=TRADE — capped at 3000 unless walk_full_history=True
    # Wrap progress_emitter to capture the real fetch_terminated reason for
    # the activity endpoint via emitter side-channel (approach b).  This
    # preserves the list return type of fetch_user_activity for backward compat
    # while still surfacing the real reason in _fetch_termination.
    _activity_term_reason: list[str] = ["empty_page"]  # mutable cell

    def _activity_emitter(event_name: str, payload: dict) -> None:
        if event_name == "fetch_terminated" and payload.get("endpoint") == "activity":
            _activity_term_reason[0] = str(payload.get("reason") or "empty_page")
        if progress_emitter is not None:
            progress_emitter(event_name, payload)

    try:
        _activity_result = polymarket_api.fetch_user_activity(
            session, wallet, activity_type="TRADE",
            walk_full_history=walk_full_history,
            progress_emitter=_activity_emitter,
        )
        # Real function returns a list; legacy test mocks may also return a list.
        activity = list(_activity_result)
        _fetch_termination["activity"] = _activity_term_reason[0]
    except Exception:
        logger.exception(
            "wallet_protocol activity fetch failed wallet=%s — using empty list",
            wallet[:14],
        )
        activity = []
    # Subgraph fallback fires whenever REST activity reaches the known
    # Polymarket hard cap, regardless of walk_full_history mode.
    activity_cap_hit = len(activity) >= _ACTIVITY_CAP
    fetch_quality_flags: list[str] = []
    subgraph_fallback_used = False
    n_unmapped_fills = 0
    activity_rest = activity  # keep a reference for comparison below

    if activity_cap_hit:
        token_lookup = _build_token_lookup(activity)
        logger.info(
            "wallet_protocol activity_cap_hit wallet=%s rest_rows=%d token_lookup=%d — trying subgraph",
            wallet[:14], len(activity), len(token_lookup),
        )
        try:
            fills = fetch_order_filled_events(wallet, session=session)
            normalized: list[dict] = []
            n_unmapped = 0
            for fill in fills:
                row = _normalize_order_fill(fill, wallet, token_lookup)
                if row is not None:
                    normalized.append(row)
                else:
                    maker_a = str(fill.get("makerAssetId") or "")
                    taker_a = str(fill.get("takerAssetId") or "")
                    outcome_tok = taker_a if maker_a == _CLOB_USDC_TOKEN_ID else maker_a
                    if (outcome_tok
                            and outcome_tok not in token_lookup
                            and outcome_tok != _CLOB_USDC_TOKEN_ID):
                        n_unmapped += 1
            # Only replace REST activity when subgraph is strictly larger.
            # When walk_full_history=True the REST walk may already be far
            # beyond the 3000-row trigger (e.g. 30k rows). Replacing that
            # with a token-lookup-limited subgraph set would be a data
            # completeness regression.
            if len(normalized) > len(activity_rest):
                activity = normalized
                activity_cap_hit = False
                subgraph_fallback_used = True
                n_unmapped_fills = n_unmapped
                fetch_quality_flags.append(
                    f"ACTIVITY_SOURCE:subgraph n_fills={len(fills)} "
                    f"n_mapped={len(normalized)} n_unmapped={n_unmapped}"
                )
                if n_unmapped > 0:
                    fetch_quality_flags.append(
                        f"ACTIVITY_SUBGRAPH_UNMAPPED: n={n_unmapped} "
                        f"(token IDs not in recent REST window; flip-recon rows dropped)"
                    )
                logger.info(
                    "wallet_protocol subgraph fallback wallet=%s fills=%d mapped=%d unmapped=%d — REPLACED rest",
                    wallet[:14], len(fills), len(normalized), n_unmapped,
                )
            else:
                # REST walk already has at least as many rows; subgraph would
                # only lose data (token-lookup limited). Keep REST activity.
                n_unmapped_fills = n_unmapped
                fetch_quality_flags.append(
                    f"ACTIVITY_SUBGRAPH_SKIPPED: rest_rows={len(activity_rest)}>="
                    f"subgraph_rows={len(normalized)} n_fills={len(fills)} n_unmapped={n_unmapped}"
                )
                logger.info(
                    "wallet_protocol subgraph fallback skipped wallet=%s "
                    "rest_rows=%d >= subgraph_rows=%d — keeping REST activity",
                    wallet[:14], len(activity_rest), len(normalized),
                )
        except Exception:
            logger.exception(
                "wallet_protocol subgraph fallback failed wallet=%s — keeping REST rows",
                wallet[:14],
            )
            fetch_quality_flags.append("ACTIVITY_SOURCE:rest_cap_hit_subgraph_failed")
    else:
        logger.info(
            "wallet_protocol activity wallet=%s n=%d cap_hit=%s",
            wallet[:14], len(activity), activity_cap_hit,
        )

    # 4. Archetype + execution profile (optional)
    archetype_layer1 = None
    archetype_layer2 = None
    execution_profile = None
    if archetype_extractor is not None:
        try:
            result = archetype_extractor(wallet)
            # Accept legacy 2-tuple OR 3-tuple. Anything else: treat as 2-tuple
            # and let it raise.
            if isinstance(result, tuple) and len(result) == 3:
                archetype_layer1, archetype_layer2, execution_profile = result
            else:
                archetype_layer1, execution_profile = result
            logger.info(
                "wallet_protocol archetype wallet=%s layer1=%s layer2=%s exec_status=%s",
                wallet[:14],
                archetype_layer1,
                archetype_layer2,
                (execution_profile or {}).get("status") if isinstance(execution_profile, dict) else None,
            )
        except Exception:
            logger.exception(
                "wallet_protocol archetype extraction failed wallet=%s",
                wallet[:14],
            )

    # Optional: capital ledger (3 subgraph queries, ~4s for large wallets).
    capital_ledger = None
    if with_capital_ledger:
        try:
            capital_ledger = compute_capital_ledger(wallet, session=session)
            logger.info(
                "wallet_protocol capital_ledger wallet=%s net_funded=%.2f",
                wallet[:14], capital_ledger.net_capital_funded_usdc,
            )
        except Exception:
            logger.exception(
                "wallet_protocol capital_ledger failed wallet=%s", wallet[:14],
            )

    # Portfolio value for ±2% accounting tie (Follow-up 4).
    portfolio_value_usdc: float | None = None
    if with_capital_ledger:
        try:
            portfolio_value_usdc = polymarket_api.fetch_user_value(session, wallet)
        except Exception:
            logger.exception(
                "wallet_protocol portfolio_value failed wallet=%s", wallet[:14],
            )

    # CTF event streams for position_reconciliation_surface (spec §8.3).
    # Run for EVERY wallet, NOT inside the activity_cap_hit branch.
    # Use local variables; there is no `data` object until the WalletData()
    # constructor returns. On failure: log via logger.exception, set the
    # local to None (fetch failure, distinct from [] = empty fetch), and
    # append a named flag to fetch_quality_flags.
    redemptions: list[dict] | None = None
    splits: list[dict] | None = None
    merges: list[dict] | None = None

    try:
        redemptions = fetch_redemptions(wallet, session=session)
    except Exception:
        logger.exception(
            "wallet_protocol redemption fetch failed wallet=%s", wallet[:14],
        )
        fetch_quality_flags.append("REDEMPTIONS_FETCH_FAILED")

    try:
        splits = fetch_splits(wallet, session=session)
    except Exception:
        logger.exception(
            "wallet_protocol splits fetch failed wallet=%s", wallet[:14],
        )
        fetch_quality_flags.append("SPLITS_FETCH_FAILED")

    try:
        merges = fetch_merges(wallet, session=session)
    except Exception:
        logger.exception(
            "wallet_protocol merges fetch failed wallet=%s", wallet[:14],
        )
        fetch_quality_flags.append("MERGES_FETCH_FAILED")

    elapsed = time.time() - started
    logger.info(
        "wallet_protocol fetch done wallet=%s elapsed=%.1fs",
        wallet[:14], elapsed,
    )

    return WalletData(
        wallet=wallet,
        closed_positions=closed,
        open_positions=open_positions,
        activity_rows=activity,
        activity_cap_hit=activity_cap_hit,
        archetype_layer1=archetype_layer1,
        archetype_layer2=archetype_layer2,
        execution_profile=execution_profile,
        fetched_at=int(time.time()),
        capital_ledger=capital_ledger,
        portfolio_value_usdc=portfolio_value_usdc,
        fetch_quality_flags=fetch_quality_flags,
        subgraph_fallback_used=subgraph_fallback_used,
        walk_full_history=walk_full_history,
        n_unmapped_fills=n_unmapped_fills,
        fetch_termination=_fetch_termination if _fetch_termination else None,
        redemptions=redemptions,
        splits=splits,
        merges=merges,
    )


__all__ = [
    "fetch_wallet_data",
    "_ACTIVITY_CAP",
    "_CLOB_USDC_TOKEN_ID",
    "_CLOB_TOKEN_SCALE",
    "_build_token_lookup",
    "_normalize_order_fill",
]
