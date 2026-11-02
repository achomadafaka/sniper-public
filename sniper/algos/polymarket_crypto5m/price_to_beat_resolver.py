"""Reconstruct the Chainlink Price-to-Beat from captured RTDS ticks, classify its quality, and
calibrate the boundary-selection rule against Gamma's post-hoc ``priceToBeat`` oracle.

We do NOT guess the threshold and never take it from Binance. The boundary rule (which Chainlink tick
is Polymarket's window open) is UNKNOWN until calibration locks it — hence ``LOCKED_BOUNDARY_RULE``
is ``None`` in v0 and reconstructions are not yet decision-grade (fail-closed). All math is ``Decimal``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .models import ChainlinkTick, PriceToBeatQuality

CANDIDATE_RULES = ("last_at_or_before", "first_at_or_after", "nearest")

# Locked only after the >=100-window calibration experiment clears its match-rate gate. None => v0.
LOCKED_BOUNDARY_RULE: Optional[str] = None


@dataclass(frozen=True)
class Candidate:
    rule: str
    price: Decimal
    source_ts_ms: int
    recv_ts: Optional[int]


def _dedup_prefer_exact(ticks):
    """Collapse duplicate source timestamps, preferring a tick carrying ``full_accuracy_value``."""
    by_src = {}
    for t in ticks:
        cur = by_src.get(t.source_ts_ms)
        if cur is None or (not cur.full_accuracy_value and t.full_accuracy_value):
            by_src[t.source_ts_ms] = t
    return list(by_src.values())


def reconstruct(ticks, *, window_start_ts: int) -> dict:
    """Compute each candidate boundary tick for ``window_start_ts`` (unix seconds).

    Returns ``{rule: Candidate|None}``. ``last_at_or_before`` / ``first_at_or_after`` / ``nearest``.
    """
    start_ms = int(window_start_ts) * 1000
    usable = _dedup_prefer_exact([t for t in ticks if t.source_ts_ms is not None])

    def _cand(rule, t):
        return None if t is None else Candidate(rule, t.price, t.source_ts_ms, t.recv_ts)

    before = [t for t in usable if t.source_ts_ms <= start_ms]
    after = [t for t in usable if t.source_ts_ms >= start_ms]
    last_before = max(before, key=lambda t: t.source_ts_ms) if before else None
    first_after = min(after, key=lambda t: t.source_ts_ms) if after else None
    # nearest: smallest |delta|; on a tie prefer the earlier (at-or-before) observation
    nearest = min(usable, key=lambda t: (abs(t.source_ts_ms - start_ms), t.source_ts_ms)) if usable else None

    return {
        "last_at_or_before": _cand("last_at_or_before", last_before),
        "first_at_or_after": _cand("first_at_or_after", first_after),
        "nearest": _cand("nearest", nearest),
    }


def resolve_quality(*, has_exact_report: bool, locked_rule: Optional[str],
                    has_rtds_candidate: bool, has_proxy: bool) -> PriceToBeatQuality:
    """Classify the authority of the available Price-to-Beat reference (fail-closed)."""
    if has_exact_report:
        return PriceToBeatQuality.EXACT_CHAINLINK_REPORT
    # a rule is only "locked" if it is one of the real candidate rules — a bogus/non-candidate value
    # is never decision-grade (fail-closed).
    if locked_rule in CANDIDATE_RULES:
        if has_rtds_candidate:
            return PriceToBeatQuality.VALIDATED_RTDS_RECONSTRUCTION
        if has_proxy:
            return PriceToBeatQuality.PROXY_ONLY
        return PriceToBeatQuality.UNAVAILABLE
    # No valid boundary rule locked yet -> nothing Chainlink-derived is decision-grade.
    return PriceToBeatQuality.UNAVAILABLE


def decision_allowed(quality: PriceToBeatQuality) -> bool:
    """Only an exact report or a validated RTDS reconstruction may drive a paper decision."""
    return quality in (PriceToBeatQuality.EXACT_CHAINLINK_REPORT,
                       PriceToBeatQuality.VALIDATED_RTDS_RECONSTRUCTION)


def known_at_time(*, source_ts_ms: int, recv_ts: int) -> dict:
    """When a reference became KNOWN to us and how late vs its Chainlink source time. The reference
    may not be used before ``known_ts`` (no lookahead)."""
    return {"known_ts": int(recv_ts), "known_latency_ms": int(recv_ts) * 1000 - int(source_ts_ms)}


def decision_not_before(*, known_ts: int, decision_ts: int) -> bool:
    """A (future) paper decision may use the reference only at or after ``known_ts``."""
    return int(decision_ts) >= int(known_ts)


def compare_to_oracle(candidates: dict, *, oracle_value: Decimal, precision: Decimal) -> dict:
    """Per-rule comparison of a window's candidates against the Gamma oracle priceToBeat."""
    oracle = Decimal(oracle_value)
    out = {}
    for rule in CANDIDATE_RULES:
        cand = candidates.get(rule)
        if cand is None:
            out[rule] = {"missing": True, "abs_error": None, "match": None}
        else:
            err = abs(Decimal(cand.price) - oracle)
            out[rule] = {"missing": False, "abs_error": err, "match": err <= Decimal(precision)}
    return out


def reconstruct_window(conn, *, condition_id, window_start_ts, oracle, code_version, now_ts,
                       precision="0.01") -> dict:
    """Weakness #7 wiring: read this window's captured Chainlink ticks, reconstruct the P2B candidates,
    compare to the Gamma oracle (when known), and persist each candidate via record_price_to_beat. Returns
    the per-rule comparison dict (empty when oracle is None). Quality stays UNAVAILABLE until calibration
    locks the boundary rule. Lazy persistence import avoids any import cycle."""
    from . import persistence as _P
    rows = conn.execute(
        "SELECT symbol, source_ts_ms, server_ts_ms, recv_ts, value, full_accuracy_value, is_snapshot "
        "FROM crypto5m_chainlink_ticks WHERE window_start_ts=?", (window_start_ts,)).fetchall()
    if not rows:
        return {}
    ticks = [ChainlinkTick(symbol=r["symbol"], source_ts_ms=r["source_ts_ms"],
                           server_ts_ms=r["server_ts_ms"], recv_ts=r["recv_ts"], value=r["value"],
                           full_accuracy_value=r["full_accuracy_value"],
                           is_snapshot=bool(r["is_snapshot"])) for r in rows]
    candidates = reconstruct(ticks, window_start_ts=window_start_ts)
    cmp = (compare_to_oracle(candidates, oracle_value=Decimal(str(oracle)), precision=Decimal(precision))
           if oracle is not None else {})
    quality = resolve_quality(has_exact_report=False, locked_rule=LOCKED_BOUNDARY_RULE,
                              has_rtds_candidate=any(c is not None for c in candidates.values()),
                              has_proxy=False)
    for rule, cand in candidates.items():
        if cand is None:
            continue
        c = cmp.get(rule, {})
        latency = (int(cand.recv_ts) * 1000 - int(cand.source_ts_ms)) if cand.recv_ts is not None else None
        _P.record_price_to_beat(conn, condition_id=condition_id, candidate_rule=rule, value=cand.price,
                                source_ts_ms=cand.source_ts_ms, recv_ts=cand.recv_ts,
                                known_latency_ms=latency, quality=quality, gamma_oracle=oracle,
                                abs_error=c.get("abs_error"), match=c.get("match"),
                                code_version=code_version, now_ts=now_ts)
    return cmp


def calibrate(rows: list) -> dict:
    """Aggregate per-window comparisons into per-rule match-rate / error / missing stats."""
    agg = {"n": len(rows)}
    for rule in CANDIDATE_RULES:
        present = [r[rule] for r in rows if r.get(rule) and not r[rule].get("missing")]
        # a malformed/empty row (no rule key) counts as missing, not as silently absent
        missing = sum(1 for r in rows if not r.get(rule) or r[rule].get("missing"))
        matches = sum(1 for p in present if p["match"])
        errs = [p["abs_error"] for p in present]
        agg[rule] = {
            "n_present": len(present),
            "missing": missing,
            "match_rate": (matches / len(present)) if present else None,
            "mean_abs_error": (sum(errs, Decimal(0)) / len(errs)) if errs else None,
            "max_abs_error": max(errs) if errs else None,
        }
    return agg
