"""`profile_matched_v1` — fit a per-wallet take-profit from prior route-space peaks.

Registered rule: docs/superpowers/specs/2026-08-29-profile-matched-v1-fitting-addendum.md
Everything here implements sections A1-A10 and nothing else. Where this module and the addendum
disagree, the addendum is right and this is a bug.

THE THREE THINGS THAT ARE EASY TO GET WRONG, all decided in the addendum rather than here.

1. PEAK SPACE IS EXECUTABLE, AND THE MISSING ROWS ARE CENSORED, NOT DROPPED. The chart peak is
   complete and wrong — above the executable peak on 25% of positions, so a target fitted there is
   a price the book never offered. The executable peak is right and biased — absent on 37% of
   closes, and those rows are the losers. Dropping them overstates the cohort median peak by 11.9
   points (+25.6% against +13.7%), which sets targets roughly twice as far out as the evidence
   supports. A position without a recorded peak still has a realised exit and its peak was AT LEAST
   that, so it enters left-censored at `max(realised, 0)`.

2. ONLY THE RELATIVE DEVIATION IS FITTED, NEVER THE LEVEL. Two rules were withdrawn before this
   one. `0.8 x median peak` estimates nothing. EV-argmax is not IDENTIFIABLE from this data: half
   the fitting rows exited on the very rails being varied, so a take-profit exit's realised return
   is about +25% by construction and feeding it back evaluates the current policy against itself;
   the peak histogram spikes at [+25%, +30%) with 61 of 297 observations against ~20 per
   neighbouring bin, so fitting a target to it re-derives the current target; and the one
   uncontaminated instrument covers 14% of the rows. So the LEVEL is contaminated and the anchor
   is taken as given at the funded contract's own +25%. What gets fitted is only how far a wallet
   sits from the cohort in log-peak space, which is the quantity the variance components measured
   and which carries no policy footprint.

3. LOW EVIDENCE IS SHRUNK, NOT THRESHOLDED. A cutoff at "10 closes" throws away every wallet below
   it and trusts everything above it equally. James-Stein shrinkage toward the cohort mean weights
   each wallet at what its evidence is worth: 37% own data at n=3, 49% at n=5, 79% at n=20.

WHAT THIS DOES NOT FIT. Derisk and trail inherit from the funded contract (A4) — their p75/median
spread is not estimable when 6 of 21 wallets have 10 observations and none have 30. The stop is
resolved LIVE and never frozen (A5); `stop_at_fit` is audit only.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass

from sniper.persistence import db_memecoin as m

PEAK_SPACE = "executable"
TP_ANCHOR = 0.25     # the funded contract's own level, taken as GIVEN and never re-derived (A2)
TP_FLOOR = 0.15      # the round-trip cost floor: 6.1 pts at 15s, first 5% step clear of it
TP_CEIL = 0.60       # the observed cohort p90 of the peak distribution (+59.6%)
DEFAULT_SHRINK_RATIO = 5.2      # s2w/s2b; re-estimated per fit and recorded, never assumed


@dataclass(frozen=True)
class Observation:
    """One prior close. `peak` is the route-space peak multiple; `censored` means it is a LOWER
    BOUND rather than a measurement — the position reached at least this and we cannot see how
    much further."""
    position_id: int
    peak: float
    censored: bool
    outcome: float


def observations_sql(*, table: str = "memecoin_positions") -> str:
    """Prior closes that are comparable evidence, per the parent document's section 4.

    BOTH censors, always. Fomo fills are a dishonest ENTRY and all-non-executable exits are an
    unobserved EXIT; applying only the first gives a larger n made of rows the report already threw
    away, which is how discarded evidence once reached funded arm selection.
    """
    # OPERATOR BUYS ARE NOT THIS WALLET'S BEHAVIOUR. A manual entry is the operator picking a
    # coin; the estimand here is a wallet's own prior closes, so a manual buy is definitionally not
    # evidence about it. This is not hypothetical: 14 manual buys carry a `source_user_id`, and one
    # of those (position 39545) carries a real wallet UUID rather than a typed nickname, so it sits
    # inside that wallet's 29-close fit today. Most manual rows cannot reach a fit at all because
    # they have no uid to key on — but "cannot key" is an accident of the keying, not a guard, and
    # it fails the moment a manual buy is recorded against a wallet id.
    #
    # UNKNOWN IS KEPT ON PURPOSE. Rows predating the funded-contract regime record no opinion, and
    # dropping them would discard genuine pre-regime copy history to avoid a contamination that
    # regime never had. They are 59 of 712 eligible closes; the manual exclusion removes 239.
    return "%s.execution_mode='real' AND (%s) <> 'manual' AND %s" % (
        table, m.signal_source_sql(table), m.measurable_outcome_sql(table))


def load_observations(conn, *, fit_window_end, source_user_id, chain):
    """Prior closes for one (wallet, chain), strictly before the cutoff.

    STRICTLY BEFORE. The cutoff is global (A6) and the arm may read nothing newer, so a `<=` here
    would admit a position closing exactly at the boundary into a fit the guard then calls clean.
    """
    rows = conn.execute(
        "SELECT id, entry_price, executable_peak_price, realized_pnl, entry_notional "
        "FROM memecoin_positions WHERE source_user_id=? AND network_id=? AND closed_at < ? "
        "AND " + observations_sql() + " ORDER BY id",
        (source_user_id, chain, fit_window_end)).fetchall()
    out = []
    for row in rows:
        pid, entry, peak_px, pnl, notional = (
            row["id"], row["entry_price"], row["executable_peak_price"],
            row["realized_pnl"], row["entry_notional"])
        if not entry or entry <= 0 or not notional or notional <= 0:
            continue
        outcome = float(pnl) / float(notional)
        if peak_px and peak_px > 0:
            out.append(Observation(pid, float(peak_px) / float(entry) - 1.0, False, outcome))
        else:
            # LEFT-CENSORED at the realised return. Never dropped, never zero: a position that
            # exited up 40% with no recorded peak reached AT LEAST 40%, and recording it as 0
            # would drag the fit down as hard as dropping it drags the fit up.
            out.append(Observation(pid, max(outcome, 0.0), True, outcome))
    return out


def shrinkage_ratio(cohort):
    """s2w / s2b from the measured variance components (A3), re-estimated at every fit.

    Returns the addendum's 5.2 when the components are not estimable — fewer than two wallets with
    two observations each. That is a documented default, not a constant: it is recorded in every
    artifact so a reader can see which was used.
    """
    groups = [[math.log1p(max(o.peak, -0.99)) for o in obs] for obs in cohort if len(obs) >= 2]
    if len(groups) < 2:
        return DEFAULT_SHRINK_RATIO, False
    within = []
    means = []
    for g in groups:
        mu = sum(g) / len(g)
        means.append(mu)
        within.extend((x - mu) ** 2 for x in g)
    dof = sum(len(g) for g in groups) - len(groups)
    s2w = (sum(within) / dof) if dof > 0 else 0.0
    gm = sum(means) / len(means)
    s2b = sum((mu - gm) ** 2 for mu in means) / (len(means) - 1)
    if s2b <= 0 or s2w <= 0:
        # A11.2: indistinguishable wallets means there is nothing to fit. Report it rather than
        # dividing by zero and rather than pretending the default applies.
        return DEFAULT_SHRINK_RATIO, False
    return s2w / s2b, True


def mean_log_peak(observations):
    """The wallet's central tendency in log-peak space.

    `log1p` because a peak is a RETURN: +25% and -20% are not symmetric in level and are close to
    symmetric in log. The floor at -0.99 keeps a total loss finite without letting it dominate.
    """
    if not observations:
        return None
    return sum(math.log1p(max(o.peak, -0.99)) for o in observations) / len(observations)


def fit_take_profit(observations, *, cohort_mean_log, ratio):
    """A shrunk OFFSET from the funded level (A2), clamped to [floor, ceiling].

        tp = 25% x exp( w(n) x (wallet mean log-peak - cohort mean log-peak) )

    THE ANCHOR IS GIVEN, NOT ESTIMATED, and that is the point rather than a shortcut. The level is
    contaminated — half the fitting rows exited on the rails being varied and the peak histogram's
    mode sits on the current target — so any rule that estimates it re-derives what funded already
    runs and calls it a finding. Only the DEVIATION between wallets is fitted, because that is the
    quantity A0's variance components measured and the one the policy's footprint does not touch.

    A wallet at the cohort mean therefore gets exactly the funded level, which is the correct
    behaviour: no evidence of difference means no difference.
    """
    n = len(observations)
    weight = n / (n + ratio) if n else 0.0
    own = mean_log_peak(observations)
    if own is None:
        return TP_ANCHOR, 0.0
    offset = weight * (own - cohort_mean_log)
    return max(TP_FLOOR, min(TP_CEIL, TP_ANCHOR * math.exp(offset))), weight


def dataset_sha256(observations):
    """Over the exact INPUT ROWS (A9), never table state.

    A state hash changes whenever any unrelated row lands, so a rebuild would look
    non-deterministic and a determinism test would be testing the clock rather than the builder.
    """
    payload = [[o.position_id, round(o.peak, 12), o.censored, round(o.outcome, 12)]
               for o in sorted(observations, key=lambda o: o.position_id)]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def build_artifact(*, source_user_id, chain, fit_window_end, observations, cohort_mean_log,
                   ratio, ratio_estimated, stop_at_fit, script_sha256):
    """One frozen artifact for one (uid, chain). A8: never pooled across chains, at any n."""
    n = len(observations)
    if n == 0:
        # A7: the one hard case. The fallback is funded_clone's resolved policy for this origin,
        # NOT the band default — under the live all-wallets regime the band default is unreachable
        # on funded, so falling back to it would measure a configuration difference.
        return {
            "source_user_id": source_user_id, "chain": chain,
            "fit_window_end": fit_window_end, "peak_space": PEAK_SPACE,
            "n_observed": 0, "n_censored": 0,
            "shrinkage_ratio": round(ratio, 6), "shrinkage_ratio_estimated": ratio_estimated,
            "shrinkage_weight": 0.0,
            "tp_pct": None, "derisk_fraction": None, "trail_pct": None, "inherited": True,
            "stop_at_fit": stop_at_fit,
            "fallback": True, "fallback_reason": "no prior measurable closes on this chain",
            "script_sha256": script_sha256, "dataset_sha256": dataset_sha256([]),
        }
    tp, weight = fit_take_profit(observations, cohort_mean_log=cohort_mean_log, ratio=ratio)
    return {
        "source_user_id": source_user_id, "chain": chain,
        "fit_window_end": fit_window_end, "peak_space": PEAK_SPACE,
        "n_observed": sum(1 for o in observations if not o.censored),
        "n_censored": sum(1 for o in observations if o.censored),
        "shrinkage_ratio": round(ratio, 6), "shrinkage_ratio_estimated": ratio_estimated,
        "shrinkage_weight": round(weight, 6),
        "tp_pct": round(tp * 100.0, 4),
        # A4: not fitted in v1. Marked inherited so a reader cannot mistake a null for a zero.
        "derisk_fraction": None, "trail_pct": None, "inherited": True,
        "stop_at_fit": stop_at_fit,          # A5: audit only, never read as policy
        "fallback": False, "fallback_reason": None,
        "script_sha256": script_sha256, "dataset_sha256": dataset_sha256(observations),
    }


# ---------------------------------------------------------------- the loader and the guard

_CACHE = {}


def load_profiles(path=None):
    """The frozen artifact set, keyed by (source_user_id, chain). Read once, never rebuilt here.

    THE BUILDER AND THE ARM ARE DIFFERENT PROCESSES ON PURPOSE. Fitting at decision time would let
    a position influence the profile that prices it, and the whole design rests on the profile
    being frozen before the window opens.
    """
    from pathlib import Path
    path = Path(path) if path else (Path(__file__).resolve().parents[2] / "data"
                                    / "profile_matched_v1.json")
    key = str(path)
    stamp = path.stat().st_mtime if path.exists() else None
    if _CACHE.get("key") == key and _CACHE.get("stamp") == stamp:
        return _CACHE["value"]
    if not path.exists():
        value = (None, {})
    else:
        doc = json.loads(path.read_text())
        value = (doc.get("fit_window_end"),
                 {(a["source_user_id"], a["chain"]): a for a in doc.get("artifacts", [])})
    _CACHE.update(key=key, stamp=stamp, value=value)
    return value


class FitWindowViolation(Exception):
    """The arm tried to price a position from data the profile could not have seen."""


def profile_for(source_user_id, chain, *, opened_at, path=None):
    """Unsupported legacy fitted artifacts cannot choose a paper or funded policy."""
    return None


# Raw historical measurements remain readable; hypothetical conclusions are ineligible.
from sniper.research.wallet_execution.publication import diagnostic_result as _diagnostic_result
build_artifact = _diagnostic_result(build_artifact)
