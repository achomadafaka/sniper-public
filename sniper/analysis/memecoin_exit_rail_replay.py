"""Rank exit-rail variants over the historical mark series — and state what that series cannot say.

STATUS 2026-08-27: DO NOT USE THE RANKINGS, and the substrate now EXISTS. Defect 3 below removes
the mark series as an input: it is chart space and does not contain the realized route-space
outcome, so no re-anchoring repairs it. The cohort, censoring and comparison code stay valid.

CORRECTED LATER THE SAME DAY. This first said a route-space series "does not exist yet". It does.
`memecoin_route_series` was built on 2026-08-26 and has been filling since 13:08Z that day, by
KEEPING the exact whole-bag quotes the funded exit path already computes — no new API call. I
concluded absence after checking `memecoin_position_marks` and `memecoin_exit_trigger_audit` and
never looking for a table added the day before. Check whether it already exists.

WHAT THE REBUILD NEEDS, measured 2026-08-27 on the `exit_decision` seam:
  28 positions, median 13 observations each, spanning a median 95% of the hold.
  Density is skewed: p10 is 1 observation, p90 is 43, max 858 on a 69-minute hold.
That is enough resolution to replay a take-profit or a stop, and NOT enough to replay a trail on
the thin tail — a trail needs the path, and one observation is not a path. Accrual is roughly 28
positions/day, so the cohort reaches the ~200 mark this module's comparison layer wants in about a
week from 2026-08-26. Filter by seam: a consumer that pools peak_preview, exit_decision, exit_audit
and equity_mark is holding a mixture of different quantities.

THE COHORT AND COMPARISON LAYER on top of `memecoin_rails_model`. The model simulates ONE policy
over ONE series and is frozen; this module decides which positions are eligible, in which price
space, against which baseline, and with what error band.

THREE INSTRUMENT DEFECTS WERE MEASURED. The first two predate this module. Both would have produced a confident wrong
answer, and neither is visible from inside the rails model:

1. THE ENTRY ANCHOR IS A PRICE SPACE. `memecoin_positions.entry_price` is our FILL. The mark series
   is CHART mid. On the current-era funded cohort the two differ by a median 1.187x (n=44, p10 1.003,
   p90 1.685, max 1.920; 29 of 44 above 1.10) — the route premium. Anchoring a chart series on a fill
   entry starts every position ~19% underwater before the price moves, which is over half the way to
   a 35% stop. The model LABELS the series space and takes `entry_price` on trust, so the mix is the
   caller's to get right, and it is got right HERE.

   We anchor on the FILL, deliberately, because the live funded nominator does: under exact_policy
   `decision_mid` is the exact route unit price and the rail compares it to `entry_price`
   (memecoin_real_runner: `pnl_pct = (mid / entry - 1.0) * 100`). Holding the live convention fixed is
   what makes "what would a DIFFERENT rail have done" the question being answered. Measured both ways
   against realized outcomes: fill anchor reproduces 22/32, chart anchor 20/33.

2. CENSORING ON GAP WIDTH WAS ADDRESSING THE WRONG DEFECT. The model's tolerance was calibrated on a
   synthetic uniform-jitter grid. The real tape is bursty: per position, median gap 1.53s but median
   MAXIMUM gap 9.18s — a max/median ratio of 4.0x median, 15.4x at p90. At 1.5x the median, 39 of 42
   positions censored. Sweeping the basis shows the knob is monotonic in sample size and does not
   buy fidelity:

       basis x mult    determinate/42   agrees with realized
       median x1.5            3               1/3
       p90    x2.0           14              10/13
       p90    x3.0           23              16/22
       max    x1.5           34              22/32

   So gap width is not what makes this replay wrong, and censoring hard only shrinks n. The default
   basis is the position's own p90 gap, which censors on what is unusual FOR THAT SERIES rather than
   against an assumed cadence.

3. THE MARK SERIES DOES NOT CONTAIN THE OUTCOME, WHICH IS WORSE THAN A WRONG ANCHOR. Found by the
   Gamma session on the 08-26 funded post-mortem; re-measured here. Of 19 funded `real_tp` closes on
   2026-08-26, 12 realized MORE than the MAXIMUM mid of their own mark window. CODEX (#44722)
   realized +109.9% against a chart max of +17.9%; pdog (#44707) realized +62.6% against a chart max
   of -19.4%. Defect 1 is a shift and a shift is correctable. A series whose maximum sits below the
   realized exit is a DIFFERENT QUANTITY, and no affine correction reaches it. (Gamma reported 17 of
   19; the re-measurement gives 12 of 19 on the same cohort. The two cited positions reproduce
   exactly and the conclusion does not depend on the count.)

   `memecoin_position_marks.executable_exit_value` is not the escape. It equals `mid` on all
   3,251,733 rows — zero information despite the name.

   THE ROUTE-SPACE INSTRUMENT IS A POINT, NOT A SERIES. `memecoin_exit_trigger_audit.preview_usdc`
   tracks the realized exit within ~1pt on 13 of the 19, worst case 16pt (#44713), against 92pt for
   the chart tape on CODEX. So it is the correct space. But the table holds 8,735 rows over 262
   distinct positions and the allowed=1 histogram is {1: 212, 2: 18, 3: 2, 4: 1} — one decision
   instant per exit. A handful of long-held coins do accumulate 1,000+ rows, but that is a debounce
   loop repeating its nomination, not a designed series, and it does not exist for the cohort anyone
   would replay. A trail cannot be replayed on one point.

   CONSEQUENCE: there is no route-space time series in the ledger today. Rebuilding this module
   requires recording one first. Until then the baseline-fidelity figure (22/32) and every ordering
   below describe the chart tape, not the rails.

WHAT THE OUTPUT MAY SAY. Ordered comparisons with a stated band, never expected PnL. The band covers
SAMPLING error only — it cannot cover the price-space dispersion above, which is systematic. A paired
comparison is better conditioned than either level because both arms run on the same series and the
bias partly cancels, but rail firing is discrete, so it does not cancel fully. "Indistinguishable"
is a real result and is reported as one.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime

from sniper.analysis import memecoin_rails_model as rm

# The live rail as of 2026-08-24. Positions are cohorted by their OWN contract, never by date:
# 28 distinct parameter tuples exist across 352 contracted funded closes, and pooling them would
# compare policies to each other under the name of one baseline.
CURRENT_ERA = ("follow-30-50-v1", 25.0, 35.0, 780.0)

DENSITY_FLOOR = 0.1          # marks/second while open; below it a position is censored and counted
GAP_BASIS_Q = 0.9            # the position's own p90 gap is the cadence hint
MIN_MARKS = 3


@dataclass(frozen=True)
class CohortPosition:
    id: int
    mint: str
    series: tuple            # ((t_from_open_s, chart_mid), ...)
    entry_price: float       # our FILL — see defect 1
    chart_at_open: float | None
    policy: dict             # the position's own funded contract policy
    realized: str            # exit_reason as it happened
    hold_s: float
    density: float
    gaps: tuple              # sorted inter-observation gaps, for the cadence sensitivity below

    def cadence_at(self, q):
        """The cadence hint at quantile `q` of THIS series' own gaps. q=1.0 disables interior
        censoring entirely (tolerance 1.5x the largest gap the series contains), which is the
        permissive end of the sensitivity check rather than a recommended setting."""
        if not self.gaps:
            return 5.0
        return max(self.gaps[int(q * (len(self.gaps) - 1))], 0.01)

    @property
    def fill_over_chart(self):
        """The measured price-space gap for THIS position. None when no mark opened close enough."""
        if not self.chart_at_open or self.chart_at_open <= 0 or self.entry_price <= 0:
            return None
        return self.entry_price / self.chart_at_open


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def _contract_policy(raw):
    try:
        return json.loads(raw)["policy"]
    except (TypeError, ValueError, KeyError):
        return None


def policy_key(policy):
    return (policy.get("policy_id"), policy.get("tp_pct"),
            policy.get("stop_pct"), policy.get("max_hold_s"))


def baseline_policy(policy):
    """The position's own live rail, as a RailsPolicy."""
    return rm.RailsPolicy(
        tp_pct=float(policy.get("tp_pct") or 0.0),
        stop_pct=float(policy.get("stop_pct") or 0.0),
        trail_arm_pct=float(policy.get("trail_arm_pct") or 0.0),
        trail_pct=float(policy.get("trail_pct") or 0.0),
        max_hold_s=float(policy.get("max_hold_s") or 0.0),
    )


def load_cohort(conn, *, era=CURRENT_ERA, density_floor=DENSITY_FLOOR):
    """Eligible positions plus the exclusion tally. Nothing is interpolated: a position that does not
    meet the floor is DROPPED AND COUNTED, because reconstructing a path through a gap invents the
    prices the replay exists to measure."""
    excluded = {"no_contract": 0, "other_era": 0, "bad_window": 0,
                "no_marks": 0, "below_density_floor": 0}
    out = []
    rows = conn.execute(
        "SELECT id, mint, entry_price, opened_at, closed_at, exit_reason, funded_contract_json "
        "FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 ORDER BY id").fetchall()
    for r in rows:
        policy = _contract_policy(r["funded_contract_json"])
        if policy is None:
            excluded["no_contract"] += 1
            continue
        if policy_key(policy) != era:
            excluded["other_era"] += 1
            continue
        opened, closed = _ts(r["opened_at"]), _ts(r["closed_at"])
        if not opened or not closed or closed <= opened:
            excluded["bad_window"] += 1
            continue
        # QUARANTINED MARKS ARE NOT PRICES THE RAILS EVER SAW. The live exit path reads
        # `quality != 'quarantined_spike'` (memecoin_exits.py:317,434) and this must match it
        # exactly. Position 44690 is why: its window holds 30 accepted marks at ~0.0100 and 49
        # quarantined ones at 0.268-0.308, a coherent alternative series from a different pool for
        # the same mint. Reading the table raw made the simulated baseline return +2562% and the
        # ladder variants -162 points, which reads as a policy finding and is a pool-identity fault.
        marks = conn.execute(
            "SELECT sweep_ts, mid FROM memecoin_position_marks "
            "WHERE mint=? AND sweep_ts>=? AND sweep_ts<=? AND mid>0 "
            "AND quality != 'quarantined_spike' ORDER BY sweep_ts",
            (r["mint"], r["opened_at"], r["closed_at"])).fetchall()
        if len(marks) < MIN_MARKS:
            excluded["no_marks"] += 1
            continue
        hold = closed - opened
        density = len(marks) / hold
        if density < density_floor:
            excluded["below_density_floor"] += 1
            continue
        series = tuple((_ts(m["sweep_ts"]) - opened, float(m["mid"])) for m in marks)
        gaps = sorted(b[0] - a[0] for a, b in zip(series, series[1:]))
        first_t, first_mid = series[0]
        out.append(CohortPosition(
            id=r["id"], mint=r["mint"], series=series,
            entry_price=float(r["entry_price"] or 0.0),
            chart_at_open=(first_mid if first_t <= 5.0 else None),
            policy=policy, realized=r["exit_reason"] or "", hold_s=hold, density=density,
            gaps=tuple(gaps)))
    return out, excluded


def outcome(pos, policy, *, cadence_q=GAP_BASIS_Q):
    """Run one policy over one position. Always chart space, always fill-anchored — see defect 1."""
    return rm.rails_outcome(pos.series, policy, price_space=rm.SPACE_CHART,
                            entry_price=pos.entry_price, cadence_hint_s=pos.cadence_at(cadence_q))


@dataclass(frozen=True)
class Comparison:
    label: str
    n_paired: int            # positions where BOTH arms are determinate
    n_undetermined: int      # positions where either arm is censored
    mean_delta_pts: float    # variant minus baseline, in return points
    lo: float                # bootstrap interval on the paired mean
    hi: float
    distinguishable: bool    # the interval excludes zero

    def __str__(self):
        verdict = "DIFFERENT" if self.distinguishable else "indistinguishable"
        return ("%-34s %+7.2f pts  [%+7.2f, %+7.2f]  n=%-3d censored=%-3d  %s"
                % (self.label, self.mean_delta_pts, self.lo, self.hi,
                   self.n_paired, self.n_undetermined, verdict))


def compare(cohort, variant, *, label, cadence_q=GAP_BASIS_Q, bootstrap_n=2000,
            seed=20260826, alpha=0.05):
    """Paired variant-minus-baseline delta with a bootstrap interval over POSITIONS.

    Paired on purpose: both arms run on the same series, so the price-space bias partly cancels and
    the delta is better conditioned than either level. It does not cancel fully — rail firing is
    discrete — and the interval covers SAMPLING error only, never that systematic term.
    """
    deltas, censored = [], 0
    for pos in cohort:
        base = outcome(pos, baseline_policy(pos.policy), cadence_q=cadence_q)
        var = outcome(pos, variant, cadence_q=cadence_q)
        if not (base.determined and var.determined):
            censored += 1
            continue
        deltas.append(var.return_pct - base.return_pct)
    if not deltas:
        return Comparison(label, 0, censored, 0.0, 0.0, 0.0, False)
    mean = sum(deltas) / len(deltas)
    rnd = random.Random(seed)
    means = sorted(sum(rnd.choices(deltas, k=len(deltas))) / len(deltas)
                   for _ in range(bootstrap_n))
    lo = means[int((alpha / 2) * (bootstrap_n - 1))]
    hi = means[int((1 - alpha / 2) * (bootstrap_n - 1))]
    return Comparison(label, len(deltas), censored, mean, lo, hi, not (lo <= 0.0 <= hi))


def baseline_fidelity(cohort, *, cadence_q=GAP_BASIS_Q):
    """How often the simulated baseline reproduces what actually happened.

    THE NUMBER THAT DECIDES WHETHER ANY RANKING IS WORTH READING. Manual closes are excluded: an
    operator is not a rail. A low value does not mean the deltas are wrong, but it bounds how much
    weight a reader may put on any single comparison.
    """
    realized_to_model = {"real_tp": rm.TP, "b_stop": rm.STOP, "catastrophe": rm.STOP,
                         "b_timecap": rm.MAX_HOLD, "real_trail": rm.TRAIL}
    agree = scored = censored = skipped = 0
    confusion = {}
    for pos in cohort:
        got = outcome(pos, baseline_policy(pos.policy), cadence_q=cadence_q)
        if not got.determined:
            censored += 1
            continue
        want = realized_to_model.get(pos.realized)
        if want is None:
            skipped += 1
            continue
        scored += 1
        agree += (want == got.reason)
        confusion[(pos.realized, got.reason)] = confusion.get((pos.realized, got.reason), 0) + 1
    return {"agree": agree, "scored": scored, "censored": censored,
            "not_a_rail": skipped, "confusion": confusion}


def ordering_is_stable(cohort, variants, *, quantiles=(0.9, 1.0)):
    """Does the censoring choice decide the ranking?

    The censor is a monotonic sample-size dial on this book — strict keeps 3 positions at high
    fidelity, off keeps 34 at lower fidelity, with nothing in between that buys both. So the useful
    question is not which setting is right but whether the ANSWER depends on it. If the ordering
    holds across the range, the censoring choice is not driving the conclusion; if it flips, no
    single setting should be reported alone.
    """
    orders = {}
    for q in quantiles:
        ranked = sorted((compare(cohort, v, label=lbl, cadence_q=q) for lbl, v in variants),
                        key=lambda c: -c.mean_delta_pts)
        orders[q] = [c.label for c in ranked]
    first = orders[quantiles[0]]
    return all(orders[q] == first for q in quantiles), orders

from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('compare',))
