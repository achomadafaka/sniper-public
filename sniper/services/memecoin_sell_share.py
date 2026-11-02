"""Refuse an entry when the tape is overwhelmingly one-sided against us.

OPERATOR, 2026-08-26: "we wont enter if we are reading above 80% volume in just sells... 80 might
even be too conservative the truth might be something around 65%." They declined measure-first for
the initial guard; the number gets refined from outcomes later.

WHAT THIS MEASURES, AND WHAT IT DOES NOT. The operator said VOLUME. This gate reads transaction
COUNTS, and the difference is real: a tape can be 80% sells by count and 40% by volume — many small
sells against a few large buys, which is a different market and arguably a bullish one. Every
refusal says "of m5 txns" out loud for exactly this reason.

WHY COUNTS. The volume split needs per-side notional, which needs the pool's own flow, which needs
a mint -> pool mapping at decision time. Measured 2026-08-26 across funded Solana entries: the pool
was resolved BEFORE the position opened on 0 of 99 — every window, including since the pre-warm
lane went live. Pool resolution is a CONSEQUENCE of holding a coin, not a precondition of entering
one, and we enter within seconds of a signal on coins that are usually brand new. A volume-share
gate would therefore have failed open on 100% of entries: live-looking, never once firing. That is
the chase-gate class (dead until 2026-08-19 because its quote priced to None and it skipped itself)
and it is not worth repeating.

DexScreener's `txns.m5` carries the count split, arrives in the payload the entry path ALREADY
fetches, and uses the same five-minute window the operator's threshold lives in. So counts are free
and available; volume is neither.

THE HOLE IS SIZED, NOT GUESSED (Gamma, 2026-08-26, 820 tape windows / 44 pools / 300s / >=5 swaps).
A count-80 cap catches only about 30% of what a volume-80 cap would refuse — 86 against 168, with
the two agreeing on 81.5% of windows — and the misses run in the UNSAFE direction. The worst case
is exactly the pathology the operator described: one whale sell against a crowd of dust buys reads
10-17% by count and 91-99% by volume. Counts are blind to size by construction, so that transfers
to txns.m5 even though it was measured on the tape.

So this is a SIZED HOLE, not a proxy error, and the 80 here is NOT the operator's volume-weighted
80. Do not read it as one. Caveats travel with those numbers: local tape rather than DexScreener
(the construct transfers, the figures do not), pools we already traded so survivor-selected, and
in-sample.

EXPECT IT TO FIRE. Count-80 tripped on 10.5% of those windows. This refuses regularly, not rarely,
which is why the reading is logged on every attempt — admitted and refused alike — so the true
threshold comes from outcomes rather than from this first guess.

Volume share is still recorded opportunistically whenever a pool does happen to resolve, because
the count-vs-volume relationship on real coins is the only thing that can later promote this gate
to volume or bless counts as a proxy. It is evidence, never the decision. Coverage for it is thin
by construction: only 5 of 2,019 funded attempts since 08-24 carried pre-entry flow at all, and a
live probe returned none for 6 of 6 recent signal mints (2 no_pool, 4 no_swaps).

FAIL OPEN ON EVERY UNKNOWN — the chase-guard convention. No stats, no txns field, too few
transactions to mean anything, or a read that errors all ADMIT. An unknown tape is not a dumping
tape, and a gate that refuses on missing data would refuse hardest exactly when the provider is
down.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def count_sell_share(stats):
    """(share_pct, total_txns) from the m5 count split, or (None, 0) when unreadable.

    None means UNKNOWN and the caller must admit. Zero total transactions is unknown, not 0% sells.
    """
    try:
        buys = (stats or {}).get("txns_m5_buys")
        sells = (stats or {}).get("txns_m5_sells")
        if buys is None or sells is None:
            return None, 0
        buys, sells = float(buys), float(sells)
        total = buys + sells
        if total <= 0:
            return None, 0
        return 100.0 * sells / total, int(total)
    except (TypeError, ValueError):
        return None, 0


def sell_share_decision(stats, *, cap_pct, min_txns):
    """(allow, share_pct, total, reason). Refuses only on a CONFIDENT, one-sided count.

    `min_txns` exists because a 2-transaction window with 2 sells is "100% sells" on nothing at all.
    Below the floor the share is recorded and ignored — the sample cannot support a refusal.
    """
    share, total = count_sell_share(stats)
    if share is None:
        return True, None, 0, "sell share unknown (no m5 txns data)"
    if not cap_pct or float(cap_pct) <= 0:
        return True, share, total, "sell share gate off"
    if total < int(min_txns):
        return True, share, total, (
            "sell share %.0f%% of only %d m5 txns (floor %d, not evaluated)"
            % (share, total, int(min_txns)))
    if share > float(cap_pct):
        return False, share, total, (
            "sell share %.0f%% of m5 txns over %d trades (cap %.0f%%)"
            % (share, total, float(cap_pct)))
    return True, share, total, "sell share %.0f%% of m5 txns (cap %.0f%%)" % (share, float(cap_pct))


def volume_sell_share(tc, *, pool, since_iso, until_iso=None, flow_fn=None):
    """Per-side NOTIONAL sell share from the pool's own flow, or None when unreadable.

    Opportunistic evidence only — never the decision. Derived from the reader's existing totals
    rather than re-walking the tape:

        gross = P + N     (sum of |quote| per swap)
        net   = P - N     (signed sum)
        so    P = (gross + net) / 2   quote IN  -> buys of the coin
              N = (gross - net) / 2   quote OUT -> sells of the coin

    Sign verified against 32 of our own funded buy legs, 32/32 landing on the positive side. Both
    sums share parity, so the halving is exact in integers.
    """
    if not pool:
        return None
    try:
        if flow_fn is None:
            from sniper.services.memecoin_solana_tape import pool_flow_window as flow_fn
        f = flow_fn(tc, pool, since_iso=since_iso, until_iso=until_iso)
        if not f:
            return None
        gross = float(f.get("gross_quote") or 0.0)
        net = float(f.get("net_quote") or 0.0)
        if gross <= 0:
            return None
        sell_notional = (gross - net) / 2.0
        return max(0.0, min(100.0, 100.0 * sell_notional / gross))
    except Exception:
        logger.exception("volume sell share unreadable for pool %s", pool)
        return None


def pool_for_mint(tc, mint):
    """Local mint -> pool, or None. Cheap indexed read; measured at 0.008ms per lookup.

    Returns None far more often than not at ENTRY time (0 of 99 funded entries had one), which is
    why this feeds the evidence lane and never the decision.
    """
    try:
        row = tc.execute("SELECT pool FROM sol_pool_vaults WHERE mint=? LIMIT 1", (mint,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None
