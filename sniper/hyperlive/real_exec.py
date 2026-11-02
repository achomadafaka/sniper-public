"""Pure decision core for the HL real copy leg — no network, no DB.

Everything here is deterministic and unit-tested: cloid derivation, eligibility,
sizing, entry rails, order-result normalization, crash-recovery settlement,
close accounting (drift vs the paper twin), and reconcile diffing.
Spec: docs/superpowers/specs/2026-07-17-hl-real-copy-leg-design.md
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

HL_MIN_ORDER_USD = 10.0


def derive_cloid(deployment_id: str, copy_id: int, leg: str) -> str:
    """Deterministic 128-bit client order id. Same (deployment, paper row, leg) always maps
    to the same cloid, so a crashed submit can be settled by query instead of re-sent."""
    h = hashlib.sha256(f"{deployment_id}|{copy_id}|{leg}".encode()).hexdigest()
    return "0x" + h[:32]


def eligibility(row: dict, *, allow_targets: list[str], epoch_ms: int,
                alt_short_only: bool = False) -> tuple[bool, str]:
    """Startup must ignore the pre-existing book: only proven-tier, non-cold-start rows
    detected after the deployment epoch, from allowlisted targets. When alt_short_only is set,
    also restrict to non-major shorts (the cell the copy edge lives in)."""
    from sniper.hyperlive.copy_filters import is_alt_short
    if str(row.get("target", "")).lower() not in [t.lower() for t in allow_targets]:
        return (False, "target_not_allowed")
    if alt_short_only and not is_alt_short(row.get("coin"), row.get("side")):
        return (False, "not_alt_short")
    if (row.get("copy_tier") or "") != "proven":
        return (False, "tier_not_proven")
    if row.get("is_cold_start"):
        return (False, "cold_start")
    if (row.get("detect_ts_ms") or 0) < epoch_ms:
        return (False, "pre_epoch")
    return (True, "ok")


def size_coin(usd: float, mark: float, sz_decimals: int) -> float | None:
    """Coin size floored to szDecimals; None when the mark is unusable or the floored
    size lands under Hyperliquid's $10 minimum order notional."""
    if not mark or mark <= 0:
        return None
    step = 10 ** sz_decimals
    sz = math.floor((usd / mark) * step) / step
    if sz <= 0 or sz * mark < HL_MIN_ORDER_USD:
        return None
    return sz


def chase_bps(side: str, paper_mark: float, mark: float) -> float:
    """How much worse the current mark is than the paper twin's entry (positive = we'd
    pay up / chase). Negative means a better price than paper got."""
    g = (mark - paper_mark) / paper_mark * 1e4
    return g if side == "long" else -g


@dataclass
class EntryDecision:
    action: str                    # 'submit' | 'skip'
    reason: str
    sz: float | None = None


def entry_decision(row: dict, *, enabled: bool, breaker_latched: bool, open_n: int,
                   total_notional: float, mark: float, sz_decimals: int,
                   cfg) -> EntryDecision:
    if not enabled:
        return EntryDecision("skip", "disabled")
    if breaker_latched:
        return EntryDecision("skip", "breaker_latched")
    if open_n >= cfg.HL_REAL_MAX_CONCURRENT:
        return EntryDecision("skip", "max_concurrent")
    if total_notional + cfg.HL_REAL_PER_POS_USD > cfg.HL_REAL_MAX_TOTAL_NOTIONAL:
        return EntryDecision("skip", "max_total_notional")
    if chase_bps(row["side"], row["entry_mark"], mark) > cfg.HL_REAL_MAX_CHASE_BPS:
        return EntryDecision("skip", "chase_guard")
    sz = size_coin(cfg.HL_REAL_PER_POS_USD, mark, sz_decimals)
    if sz is None:
        return EntryDecision("skip", "below_min_notional")
    return EntryDecision("submit", "ok", sz=sz)


def normalize_order_result(resp, requested_sz: float) -> dict:
    """SDK order response -> {'state', 'fill_px', 'fill_sz'}. Anything malformed or raised
    is 'ambiguous' — the recovery pass settles it by cloid query, never by resend."""
    if isinstance(resp, BaseException):
        return {"state": "ambiguous", "fill_px": None, "fill_sz": None}
    try:
        status = resp["response"]["data"]["statuses"][0]
    except (KeyError, IndexError, TypeError):
        return {"state": "ambiguous", "fill_px": None, "fill_sz": None}
    if "error" in status:
        return {"state": "rejected", "fill_px": None, "fill_sz": None}
    filled = status.get("filled")
    if not filled:
        return {"state": "ambiguous", "fill_px": None, "fill_sz": None}
    try:
        sz = float(filled["totalSz"])
        px = float(filled["avgPx"])
    except (KeyError, TypeError, ValueError):
        return {"state": "ambiguous", "fill_px": None, "fill_sz": None}
    state = "filled" if sz >= requested_sz - 1e-12 else "partial"
    return {"state": state, "fill_px": px, "fill_sz": sz}


def settle_from_status(status: dict | None) -> str | None:
    """Map a query_order_by_cloid inner payload ({"order": {...}, "status": "filled", ...})
    to a terminal intent state. None = still unknown (order live or query empty) — leave
    the intent pending and keep entries blocked."""
    if not status or not isinstance(status, dict):
        return None
    order_state = str(status.get("status") or "").lower()
    if order_state == "filled":
        return "filled"
    if order_state in ("canceled", "cancelled", "rejected", "margincanceled"):
        return "rejected"
    return None


def close_accounting(open_fill: dict, close_fill: dict, *, fees_usd: float,
                     paper_pnl_bps: float, side: str) -> dict:
    """Realized PnL on the matched size, net of fees, plus drift vs the paper twin.
    drift_bps is SIGNED: negative = real underperformed paper; the pre-registered kill
    reads mean drift < -100 bps (spec §11)."""
    sz = min(float(open_fill["fill_sz"]), float(close_fill["fill_sz"]))
    sign = 1.0 if side == "long" else -1.0
    gross = sign * (float(close_fill["fill_px"]) - float(open_fill["fill_px"])) * sz
    realized = gross - float(fees_usd)
    entry_notional = float(open_fill["fill_px"]) * sz
    net_bps = realized / entry_notional * 1e4 if entry_notional > 0 else 0.0
    return {"realized_usd": realized, "real_net_bps": net_bps,
            "drift_bps": net_bps - float(paper_pnl_bps)}


def reconcile(book: list[dict], exchange_positions: dict[str, dict],
              *, sz_tol: float = 1e-9) -> list[dict]:
    """Diff the leg-owned book against exchange positions. Any fault blocks new entries
    until resolved (spec §8). Sign of szi encodes side: long > 0, short < 0."""
    faults: list[dict] = []
    seen: set[str] = set()
    for b in book:
        coin = str(b["coin"]).upper()
        seen.add(coin)
        ex = exchange_positions.get(coin)
        if not ex:
            faults.append({"kind": "missing_on_exchange", "coin": coin,
                           "copy_id": b["copy_id"]})
            continue
        want = float(b["fill_sz"]) * (1.0 if b["side"] == "long" else -1.0)
        if abs(float(ex.get("szi", 0.0)) - want) > max(sz_tol, abs(want) * 1e-6):
            faults.append({"kind": "size_mismatch", "coin": coin, "copy_id": b["copy_id"]})
    for coin in exchange_positions:
        if coin.upper() not in seen:
            faults.append({"kind": "unknown_on_exchange", "coin": coin, "copy_id": None})
    return faults
