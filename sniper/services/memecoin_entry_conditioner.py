"""Memecoin entry conditioner — a computable, feature-band positive-selection gate.

Complements the existing hard gates (arm allowlist, token-safety, $30M mcap ceiling) with a
POSITIVE selection signal from the collected feature store. Scope was narrowed by the 2026-07-13
methodology audit (methodology-review/memecoin-feature-store-hit20-bands-2026-07-13): of the raw
per-signal band effects, only the ones that survive MINT-DEDUP (one outcome per token, so a
pumping coin re-signalled by 20 wallets counts once) are kept here — the rest were
pseudo-replication:

    KEPT (dedup-robust):  mcap 300k-1M  elevated (~57% mint-grain vs ~28% base)
                          mcap 30M+     suppressed (~2.5% mint-grain)   [temporally unstable — see note]
                          size 20k+     mild penalty (~16% vs ~26%)
    DROPPED (not robust): mcap 1M-5M / 5M-30M (collapse/flip under dedup),
                          is_first_buy (26.6% vs 23.1% at mint grain — ~neutral)

CAVEAT baked into the gate's honesty: this may be a small-cap VOLATILITY tilt, not copy-specific
alpha (no no-signal control was run). It is a coarse selection nudge, not an edge claim. Band rates
are frozen constants (refresh from `scripts/research/memecoin_feature_scan.py --mint-grain`); the
runtime decision is deterministic and needs no DB read.

Deploy discipline — this is DEFAULT OFF and DOES NOTHING until an operator sets
`MEMECOIN_ENTRY_CONDITIONER`:
    off    (default) -> always allow; zero behaviour change. Safe to ship/merge/deploy inert.
    shadow           -> always allow, but return the verdict so the runner can LOG what it
                        WOULD have done, to measure effect on live fills before it gates money.
    on               -> block entries whose band-implied p(hit20) < MEMECOIN_ENTRY_MIN_HIT_PROB.

Integration point (NOT wired in this change — operator-gated deploy):
`sniper/services/memecoin_real_leg.py::real_entry`, right after the base-asset drop (line ~68)
and before `reserve_entry`:

    from sniper.services.memecoin_entry_conditioner import evaluate_entry
    v = evaluate_entry(mcap=getattr(ev,'their_market_cap',None),
                       size_usd=getattr(ev,'their_size_usd',None),
                       is_first_buy=getattr(ev,'is_first_buy',None),
                       network_id=getattr(ev,'network_id',None))
    if v.mode != 'off':
        # log v.prob / v.reason on every attempt (shadow measurement)
        if v.mode == 'on' and not v.allow:
            return {"status": "conditioner_skip", "reason": v.reason, "prob": v.prob}
"""
from __future__ import annotations

from dataclasses import dataclass

# --- Frozen hit20 constants. Per-signal BASE is row-level (the runtime unit is one signal); band
# rates are CONSERVATIVE, dedup-informed (2026-07-13 audit). Only dedup-robust bands are listed;
# everything absent falls through to LR = 1.0 (neutral). Refresh via memecoin_feature_scan.py.
BASE_HIT20 = 0.132
_MIN_CELL_N = 30  # bands (mint count) below this are treated as neutral (LR = 1.0)

# band -> (conservative rate, mint-count). rate/BASE = the applied likelihood ratio.
#   300k-1M rate 0.26 -> LR ~1.97 (row 2.97x / mint 2.03x; the conservative middle)
#   30M+    rate 0.04 -> LR ~0.30 (suppressor; robust across pooled + dedup + prior finding)
_MCAP_RATE = {"300k-1M": (0.26, 56), "30M+": (0.04, 40)}
#   only the 20k+ (big-trade) penalty survives dedup; 1k-5k vs 5k-20k does not separate at mint grain
_SIZE_RATE = {"20k+": (0.08, 51)}
# is_first_buy dropped — 26.6% vs 23.1% at mint grain is ~neutral, not the row-level 1.6x.
_FB_RATE: dict = {}


def mcap_band(mcap: float | None) -> str:
    """Canonical market-cap bucket. Finer than the sub-$1M/$1-30M/>$30M convention but rolls up
    to it. Source of truth for both the feature store and this conditioner."""
    if mcap is None:
        return "unknown"
    if mcap < 300_000:
        return "<300k"
    if mcap < 1_000_000:
        return "300k-1M"
    if mcap < 5_000_000:
        return "1M-5M"
    if mcap < 30_000_000:
        return "5M-30M"
    return "30M+"


def size_band(size: float | None) -> str:
    if size is None:
        return "unknown"
    if size < 1_000:
        return "<1k"
    if size < 5_000:
        return "1k-5k"
    if size < 20_000:
        return "5k-20k"
    return "20k+"


def _lr(band_key, table: dict) -> float:
    """Likelihood ratio = band_rate / base_rate, neutral (1.0) for unknown/low-N bands."""
    cell = table.get(band_key)
    if not cell or cell[1] < _MIN_CELL_N or BASE_HIT20 <= 0:
        return 1.0
    return cell[0] / BASE_HIT20


@dataclass(frozen=True)
class EntryVerdict:
    allow: bool
    prob: float          # band-implied p(hit20), clamped [0, 0.99]
    reason: str
    mode: str            # off | shadow | on


def band_implied_prob(mcap: float | None, size_usd: float | None,
                      is_first_buy: int | None) -> float:
    """Transparent naive combination: base * LR(mcap) * LR(size) * LR(first_buy), clamped."""
    p = (BASE_HIT20
         * _lr(mcap_band(mcap), _MCAP_RATE)
         * _lr(size_band(size_usd), _SIZE_RATE)
         * _lr(1 if is_first_buy else 0 if is_first_buy is not None else None, _FB_RATE))
    return max(0.0, min(0.99, p))


def evaluate_entry(*, mcap: float | None, size_usd: float | None,
                   is_first_buy: int | None = None, network_id: str | None = None,
                   mode: str | None = None, min_prob: float | None = None) -> EntryVerdict:
    """Feature-band entry verdict. `mode`/`min_prob` default to settings; pass explicitly in tests.
    In 'off' (default) always allows — inert. In 'on' blocks when band-implied p(hit20) < min_prob."""
    if mode is None or min_prob is None:
        from sniper.settings import memecoin as cfg
        mode = cfg.MEMECOIN_ENTRY_CONDITIONER if mode is None else mode
        min_prob = cfg.MEMECOIN_ENTRY_MIN_HIT_PROB if min_prob is None else min_prob
    prob = band_implied_prob(mcap, size_usd, is_first_buy)
    reason = (f"p(hit20)={prob*100:.1f}% [mcap={mcap_band(mcap)} size={size_band(size_usd)} "
              f"fb={is_first_buy}]")
    if mode == "off":
        return EntryVerdict(True, prob, "conditioner off", "off")
    if mode == "shadow":
        return EntryVerdict(True, prob, "shadow: " + reason, "shadow")
    # mode == 'on'
    allow = prob >= min_prob
    return EntryVerdict(allow, prob,
                        ("pass " if allow else f"blocked (<{min_prob*100:.0f}%) ") + reason, "on")
