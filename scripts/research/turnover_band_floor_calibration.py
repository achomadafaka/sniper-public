"""One-shot replay: what should the per-(chain, coin_band) m5 turnover floor be, if anything.

SUPERSEDED, PARTIALLY, BY docs/superpowers/specs/2026-09-09-inband-turnover-floor-band-study.md
(read that document first). Its verdict is NOT ESTIMABLE, not "no cell has a defensible cutoff" --
a materially different finding this script's own MIN_CELL_N bar hides. The bar that governs a
floor is n>=20 in the specific DECISION-REGION bucket a candidate would refuse (below the fixed
cutoffs 0.02/0.04 that document tests), not n>=30 in the whole (chain, band) cell this script
gates on below. The largest decision-region bucket in that document is n=12; nothing here clears
it either, whatever the whole-cell count says. That document also adds a MONOTONICITY condition
this script never checks (solana's response is positive below 0.01, negative 0.01-0.10, positive
above -- a floor cannot express that shape regardless of any single bucket's sign-robustness), and
documents that the decision region is STRUCTURALLY FROZEN (the in-band floor only became reachable
2026-09-08, so sub-floor fills stop accruing once it is live -- no re-run at a later date can grow
this sample the way both this script and the original debate assumed).

What survives here unweakened, and independently cross-checks that document's own section 6b to
the dollar: the per-cell p25/median quartile-cutoff robustness table below. It is a different,
complementary cut (each cell's own quartiles vs the doc's fixed 0.02/0.04), and the captured-refusal
arm (the `rails_outcome`/cadence-hint fix), which that document's section 7 names as the only
mechanism that could grade a future below-floor sampling arm if one is ever authorised.

Design settled across a crew debate (Alpha/Beta/Gamma, 2026-09-09) on
docs/superpowers/specs/2026-07-18-memecoin-band-policy-forward-cohort-preregistration.md's band
convention. Read-only replay over columns that already exist. NO runtime code, NO gate change: it
answers a question, it does not admit or refuse anything. The floor stays the global 0.04
(`sniper.settings.memecoin`) regardless of what this prints, until an operator acts on the result.

WHY A REPLAY AND NOT A LIVE SHADOW: `memecoin_entry_shadow.py` is on the funded entry path
(`_record_entry_shadows_best_effort` is called from `_hook_raw`/`_finish_trace_for_result`) --
Alpha assumed otherwise and Gamma caught it. Nothing about this question needs a hot-path hook: the
turnover value, the band, and the outcome are all already persisted.

THE COHORT (Gamma, verified independently against the `volume_mcap` trap below): funded, closed,
measurable positions whose entry attempt carried a classification. `classification_json.facts` has
`volume_m5` and `effective_mcap` on 636 of them, 2026-08-23..2026-09-09 (position history with
computable turnover starts 08-23, so "all history" and "last 17 days" are the same population).

TRAP, verified directly (2 sampled rows): `classification_json.facts.volume_mcap` is NOT
volume_m5/effective_mcap or volume_h1/effective_mcap -- it is IDENTICAL to `effective_mcap` on
every sampled row (a copy, not a ratio). Never read it as turnover.

TWO ARMS, not pooled -- they answer different questions with different confidence:

  REALIZED arm: filled positions, `realized_pnl` actually booked. Primary evidence.
  CAPTURED-REFUSAL arm: mints refused on `m5_turnover_under_floor`, forward-quoted afterward by
    `memecoin_refusal_capture.py` into its own db (`data/memecoin_refusal_captures.db`, gate
    already present in `GATES` before today -- verified, not assumed). Graded via
    `sniper.analysis.memecoin_rails_model.rails_outcome`, SPACE_ROUTE, entry_price = the series'
    first observation (the refusal-time route quote). This is an UPPER BOUND: a route quote has no
    entry slippage or fill gap, and grading uses the funded book's current baseline policy
    (`memecoin_exit_rail_replay.CURRENT_ERA`) for every row rather than each mint's own band policy,
    because this arm cannot power any cell to n>=30 regardless (37 captures total, all solana,
    ~3/day) -- picking a coarser, already-shipped policy over inventing per-band grading here is
    the deliberate simplification, not an oversight.

CANDIDATE CUTOFFS per cell: that cell's OWN p25 and median turnover, not a round number shared
across cells -- the whole point established in the debate is that the same band means a different
number of chains apart (solana micro 0.498 vs 4663 micro 0.162 on this run).

ROBUSTNESS, all four must agree on sign before a cutoff is called anything but "not robust":
leave-one-day-out (every UTC day present), drop-two-best-days (by summed pnl), both time halves
(split on the cell's median opened_at), drop-largest-absolute-pnl-row.

n>=30 CHECKABLE ROWS PER CELL, or the cell gets no number and inherits the global 0.04 -- stated in
advance, not picked after seeing which cells clear it (they were named in the debate before this
script ran: solana micro/nano/small, 4663 micro).
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sniper.analysis.memecoin_exit_rail_replay import CURRENT_ERA
from sniper.analysis.memecoin_rails_model import SPACE_ROUTE, RailsPolicy, rails_outcome
from sniper.persistence.db_memecoin import measurable_outcome_sql

MIN_CELL_N = 30            # whole-(chain,band)-cell size -- context only, NOT the kill-criterion bar
DECISION_REGION_MIN_N = 20  # the real bar: n below the candidate cutoff itself (per the pre-reg doc)


def _connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def load_realized_cohort(conn):
    """Filled, measurable positions with a computable turnover. One row dict per position."""
    rows = conn.execute(
        f"""
        SELECT p.id, p.mint, p.network_id, p.opened_at, p.realized_pnl, a.classification_json
        FROM memecoin_positions p
        JOIN memecoin_entry_attempts a ON a.attempt_id = p.attempt_id
        WHERE p.execution_mode='real' AND {measurable_outcome_sql('p')}
          AND p.entry_notional > 0
          AND a.classification_json IS NOT NULL
        """
    ).fetchall()
    out = []
    for pid, mint, network_id, opened_at, pnl, cjson in rows:
        try:
            c = json.loads(cjson)
            facts = c.get("facts") or {}
            volume_m5 = facts.get("volume_m5")
            mcap = facts.get("effective_mcap")
            band = c.get("coin_band")
        except (TypeError, ValueError):
            continue
        if volume_m5 is None or not mcap:
            continue
        try:
            turnover = float(volume_m5) / float(mcap)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        out.append(dict(id=pid, mint=mint, network_id=network_id, opened_at=opened_at,
                        pnl=float(pnl), turnover=turnover, band=band))
    return out


def _day(iso_ts):
    return str(iso_ts)[:10]


def _cutoff_effect(rows, cutoff):
    """sum(pnl) of rows a floor at `cutoff` would have refused (turnover < cutoff)."""
    below = [r for r in rows if r["turnover"] < cutoff]
    return below, sum(r["pnl"] for r in below)


def _sign(x):
    if x > 0:
        return "hurts"    # refusing these throws away net-positive pnl
    if x < 0:
        return "helps"    # refusing these avoids net-negative pnl
    return "flat"


def robust_verdict(rows, cutoff):
    """(n_below, total_pnl, verdict) -- verdict is the sign only if it survives every perturbation."""
    below, total = _cutoff_effect(rows, cutoff)
    if not below:
        return 0, 0.0, "n/a (nothing below cutoff)"
    base_sign = _sign(total)
    checks = []
    days = sorted({_day(r["opened_at"]) for r in rows})
    for d in days:
        _, t = _cutoff_effect([r for r in rows if _day(r["opened_at"]) != d], cutoff)
        checks.append(_sign(t))
    by_day_pnl = defaultdict(float)
    for r in rows:
        by_day_pnl[_day(r["opened_at"])] += r["pnl"]
    best_two_days = {d for d, _ in sorted(by_day_pnl.items(), key=lambda kv: -kv[1])[:2]}
    _, t = _cutoff_effect([r for r in rows if _day(r["opened_at"]) not in best_two_days], cutoff)
    checks.append(_sign(t))
    ordered = sorted(rows, key=lambda r: r["opened_at"])
    mid = len(ordered) // 2
    for half in (ordered[:mid], ordered[mid:]):
        _, t = _cutoff_effect(half, cutoff)
        checks.append(_sign(t))
    if below:
        largest = max(below, key=lambda r: abs(r["pnl"]))
        _, t = _cutoff_effect([r for r in rows if r["id"] != largest["id"]], cutoff)
        checks.append(_sign(t))
    robust = all(c == base_sign for c in checks)
    return len(below), total, (base_sign if robust else "not robust (sign flips)")


def realized_arm_report(cohort):
    cells = defaultdict(list)
    for r in cohort:
        cells[(r["network_id"], r["band"])].append(r)
    print(f"REALIZED arm: {len(cohort)} checkable rows total\n")
    print(f"{'chain':<8}{'band':<14}{'n':>5}  {'median_to':>10}  {'p25_cutoff':>28}  {'median_cutoff':>28}")
    for (chain, band), rows in sorted(cells.items(), key=lambda kv: -len(kv[1])):
        turnovers = sorted(r["turnover"] for r in rows)
        med = statistics.median(turnovers)
        p25 = turnovers[max(0, int(len(turnovers) * 0.25) - 1)] if len(turnovers) > 1 else turnovers[0]
        n25, pnl25, v25 = robust_verdict(rows, p25)
        n50, pnl50, v50 = robust_verdict(rows, med)
        # DECISION_REGION_MIN_N gates on n25/n50 -- the count BELOW the candidate cutoff, which is
        # the population a floor there would actually refuse. `len(rows) >= MIN_CELL_N` (whole
        # cell) is a WEAKER, misleading check: the per-band study
        # (docs/superpowers/specs/2026-09-09-inband-turnover-floor-band-study.md) found whole-cell
        # n>=30 satisfied on four cells while every actual decision-region bucket topped out at 12.
        powered = min(n25, n50) >= DECISION_REGION_MIN_N
        tag = ("" if powered else
              f"  [decision-region n<{DECISION_REGION_MIN_N} (whole cell n={len(rows)}): "
              f"inherits global 0.04, no number proposed]")
        print(f"{chain:<8}{str(band):<14}{len(rows):>5}  {med:>10.3f}  "
              f"n={n25:<3} ${pnl25:>8.2f} {v25:<12}  n={n50:<3} ${pnl50:>8.2f} {v50:<12}{tag}")
    return cells


def captured_refusal_arm_report(captures_conn, cohort):
    """Grade every m5_turnover_under_floor capture with quality='ok' via rails_outcome, using the
    funded book's own current baseline policy (CURRENT_ERA) -- an upper-bound counterfactual, not
    a realized outcome. Reported for completeness; per the design this arm cannot reach n>=30 on
    any cell and must never be pooled with the REALIZED arm above."""
    policy_id, tp_pct, stop_pct, max_hold_s = CURRENT_ERA
    policy = RailsPolicy(tp_pct=tp_pct, stop_pct=stop_pct, trail_arm_pct=0.0, trail_pct=0.0,
                         max_hold_s=max_hold_s)
    # Route-priced series only: the EVM lane (2026-09-13) writes DexScreener display marks here.
    # This handle is read-only, so an older file may not have the column yet.
    has_basis = any(r[1] == "quote_price_basis" for r in
                    captures_conn.execute("PRAGMA table_info(memecoin_refusal_captures)"))
    caps = captures_conn.execute(
        "SELECT capture_id, network_id, mint, refused_at, age_s, quote_price, quality "
        "FROM memecoin_refusal_captures WHERE gate='m5_turnover_under_floor' "
        + ("AND COALESCE(quote_price_basis, 'exact_route')='exact_route' " if has_basis else "")
        + "ORDER BY capture_id, age_s"
    ).fetchall()
    by_capture = defaultdict(list)
    meta = {}
    for capture_id, network_id, mint, refused_at, age_s, quote_price, quality in caps:
        if quality != "ok" or quote_price is None or age_s is None:
            continue
        by_capture[capture_id].append((float(age_s), float(quote_price)))
        meta[capture_id] = (network_id, mint, refused_at)
    if not by_capture:
        print("\nCAPTURED-REFUSAL arm: 0 gradable captures (quality != 'ok' or malformed series).")
        return
    print(f"\nCAPTURED-REFUSAL arm (upper bound, {policy_id} policy, NOT pooled with realized): "
          f"{len(by_capture)} gradable captures")
    graded, undetermined = 0, 0
    total_modeled = 0.0
    by_cell = defaultdict(lambda: [0, 0.0])
    for capture_id, series in by_capture.items():
        network_id, mint, refused_at = meta[capture_id]
        series = sorted(series)
        entry_price = series[0][1]
        # The capture lane polls Jupiter every ~25s (33 obs / ~810s), nothing like the funded
        # book's own mark cadence the model's 5.0s default assumes. Pass this series' OWN p90 gap
        # (same GAP_BASIS_Q convention memecoin_exit_rail_replay.py uses) or every capture censors
        # as `undetermined` on its first real gap -- verified: uncorrected, all 37 did exactly that.
        gaps = sorted(b[0] - a[0] for a, b in zip(series, series[1:]))
        cadence_hint = gaps[int(len(gaps) * 0.9)] if gaps else 5.0
        result = rails_outcome(series, policy, price_space=SPACE_ROUTE, entry_price=entry_price,
                               cadence_hint_s=cadence_hint)
        if not result.determined or result.return_pct is None:
            undetermined += 1
            continue
        graded += 1
        total_modeled += result.return_pct
        by_cell[network_id][0] += 1
        by_cell[network_id][1] += result.return_pct
    print(f"  graded={graded} undetermined={undetermined} "
          f"(undetermined = no usable rails result, e.g. entry_price<=0 or empty series)")
    for chain, (n, pct_sum) in sorted(by_cell.items()):
        print(f"  {chain}: n={n} modeled_return_sum_pct={pct_sum:.1f} "
              f"(all n < {MIN_CELL_N}, informational only)")


def main():
    tracker = _connect(REPO / "sniper_tracker.db")
    captures = _connect(REPO / "data" / "memecoin_refusal_captures.db")
    cohort = load_realized_cohort(tracker)
    print(f"Cohort check: {len(cohort)} rows "
          f"(Gamma's independent count was 636 -- must match before trusting anything below)\n")
    realized_arm_report(cohort)
    captured_refusal_arm_report(captures, cohort)
    print("\nFloor stays the global 0.04 everywhere. This script proposes nothing; it reports.")



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('robust_verdict', 'main'))

if __name__ == "__main__":
    main()
