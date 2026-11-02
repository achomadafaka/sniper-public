"""§4.7 — FIFO flip reconstruction from /activity rows.

A "flip" is a BUY→SELL pair of fills on the same (wallet, conditionId, outcome).
Per-flip ROI is the unit of observation for the flipper branch (FH1-FH6).

Algorithm:
1. Group activity rows by (conditionId, outcome).
2. Sort by timestamp ASC within each group.
3. FIFO match: each BUY's shares are consumed by subsequent SELLs in order.
4. Each completed match yields one Flip with entry/exit price and ROI.
5. Unmatched BUY tail (residual shares not yet sold) is held; not a flip.

Per spec §4.7:
- flip_rate = n_positions_with_pre_resolution_sells / n_closed_positions
- Fail loudly when flip_rate > 0.50 AND len(activity) >= 3000

The 3000-cap means high-volume flippers' lifetime flip history is invisible.
This module surfaces that cleanly via the FlipReconstructionResult.activity_cap_hit
flag for downstream consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sniper.analysis.wallet_protocol.types import WalletData


@dataclass
class Flip:
    """One BUY→SELL pair (or partial BUY consumed by a SELL)."""

    condition_id: str
    outcome: str
    buy_ts: int
    sell_ts: int
    buy_price: float
    sell_price: float
    size: float                                            # shares — partial fills allowed
    buy_notional: float                                    # buy_price * size (USDC at entry)
    flip_roi: float                                        # (sell_price - buy_price) / buy_price
    hold_duration_seconds: int                             # sell_ts - buy_ts
    # CLOB outcome token id ("asset" field on /activity rows). Required by
    # FH6 because Polymarket's /prices-history endpoint silently returns an
    # empty `history` array when queried with a conditionId (only accepts
    # token/asset ids — verified live 2026-04-26 in C5 closure). Empty
    # string default keeps existing test fixtures back-compat; FH6 skips
    # flips with empty asset_id.
    asset_id: str = ""


@dataclass
class FlipReconstructionResult:
    """Output of reconstruct_flips()."""

    flips: list[Flip] = field(default_factory=list)
    n_flips: int = 0
    flip_rate: float = 0.0                                 # n_pre_resolution_sells / n_closed_positions
    n_pre_resolution_sells: int = 0                        # positions with at least one SELL before resolution
    n_closed_positions: int = 0                            # from WalletData.closed_positions
    activity_cap_hit: bool = False
    flipper_analysis_truncated: bool = False               # flip_rate > 0.50 AND cap hit


def _group_by_position(activity: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group TRADE rows by (conditionId, outcome). Sort each group by timestamp ASC."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in activity:
        cid = row.get("conditionId") or ""
        outcome = row.get("outcome") or ""
        if not cid or not outcome:
            continue
        key = (cid, outcome)
        grouped.setdefault(key, []).append(row)
    for key in grouped:
        grouped[key].sort(key=lambda r: int(r.get("timestamp", 0) or 0))
    return grouped


def _fifo_match_one_position(rows: list[dict]) -> list[Flip]:
    """FIFO-match BUY→SELL pairs within one (conditionId, outcome).

    Maintains a queue of unmatched BUY shares. Each SELL consumes BUY shares
    in arrival order, producing one Flip per BUY-fragment-that-was-sold.

    Pure function; no exceptions raised on bad data — bad rows are skipped.
    """
    cid = rows[0].get("conditionId") if rows else ""
    outcome = rows[0].get("outcome") if rows else ""

    # Each entry in buy_queue: [remaining_shares, ts, price, asset_id]
    buy_queue: list[list] = []
    flips: list[Flip] = []

    for r in rows:
        side = (r.get("side") or "").upper()
        size = float(r.get("size") or 0.0)
        price = float(r.get("price") or 0.0)
        ts = int(r.get("timestamp") or 0)
        if size <= 0 or ts <= 0:
            continue

        if side == "BUY":
            asset = str(r.get("asset") or "")
            buy_queue.append([size, ts, price, asset])
            continue

        if side != "SELL":
            continue

        sell_remaining = size
        # Consume BUY shares FIFO until this SELL is exhausted
        while sell_remaining > 0 and buy_queue:
            head = buy_queue[0]
            buy_remaining = head[0]
            buy_ts = int(head[1])
            buy_price = head[2]
            buy_asset = head[3] if len(head) >= 4 else ""
            consumed = min(buy_remaining, sell_remaining)
            if buy_price > 0:
                flip_roi = (price - buy_price) / buy_price
            else:
                flip_roi = 0.0
            flips.append(Flip(
                condition_id=cid,
                outcome=outcome,
                buy_ts=buy_ts,
                sell_ts=ts,
                buy_price=buy_price,
                sell_price=price,
                size=consumed,
                buy_notional=buy_price * consumed,
                flip_roi=flip_roi,
                hold_duration_seconds=max(0, ts - buy_ts),
                asset_id=buy_asset,
            ))
            head[0] -= consumed
            sell_remaining -= consumed
            if head[0] <= 1e-9:
                buy_queue.pop(0)
        # If sell_remaining > 0, the wallet sold more shares than they bought
        # in the captured /activity window (likely truncated history). Drop.

    return flips


def _count_positions_with_pre_resolution_sells(
    activity: list[dict],
    closed_positions: list[dict],
) -> tuple[int, int]:
    """Returns (n_with_sells, n_closed_positions).

    A "pre-resolution sell" is any SELL event on a (conditionId, outcome) that
    appears in `closed_positions`. The "any SELL ⇒ pre-resolution" simplification
    rests on a locked assumption: `/activity?type=TRADE` excludes redemption /
    settlement events (those use activity_type='REDEEM' on the Polymarket Data API).
    Therefore any SELL row in our activity stream is necessarily a pre-resolution
    market exit, not a post-resolution settlement.

    If that filter ever changes upstream (e.g., REDEEM rows leak into TRADE),
    this function will overcount flippers. Guard against that drift in the fetch
    layer, not here.
    """
    closed_keys = {
        (p.get("conditionId"), p.get("outcome"))
        for p in closed_positions
        if p.get("conditionId") and p.get("outcome")
    }
    n_closed = len(closed_keys)
    cids_with_pre_resolution_sell: set[tuple[str, str]] = set()
    for r in activity:
        if (r.get("side") or "").upper() != "SELL":
            continue
        size = float(r.get("size") or 0.0)
        if size <= 0:
            continue
        key = (r.get("conditionId"), r.get("outcome"))
        if key in closed_keys:
            cids_with_pre_resolution_sell.add(key)
    return len(cids_with_pre_resolution_sell), n_closed


def reconstruct_flips(data: WalletData) -> FlipReconstructionResult:
    """Build flip set + flip_rate + truncation flag from WalletData.

    Per spec §4.7. flip_rate uses the closed-positions universe as denominator
    (not n_activity_rows or n_open_positions) so it represents the share of
    the wallet's COMPLETED trading that involved pre-resolution exits.
    """
    n_pre_sells, n_closed = _count_positions_with_pre_resolution_sells(
        data.activity_rows, data.closed_positions,
    )
    flip_rate = (n_pre_sells / n_closed) if n_closed > 0 else 0.0

    # FIFO match per position
    grouped = _group_by_position(data.activity_rows)
    all_flips: list[Flip] = []
    for rows in grouped.values():
        all_flips.extend(_fifo_match_one_position(rows))

    truncated = data.activity_cap_hit and flip_rate > 0.50

    return FlipReconstructionResult(
        flips=all_flips,
        n_flips=len(all_flips),
        flip_rate=flip_rate,
        n_pre_resolution_sells=n_pre_sells,
        n_closed_positions=n_closed,
        activity_cap_hit=data.activity_cap_hit,
        flipper_analysis_truncated=truncated,
    )


__all__ = [
    "Flip",
    "FlipReconstructionResult",
    "reconstruct_flips",
]
