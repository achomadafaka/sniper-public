#!/usr/bin/env python3
"""Golden roster refresh under multiplicity control, per the 2026-08-28 pre-registration.

    docs/superpowers/specs/2026-08-28-golden-roster-refresh-prereg.md

WHY THE SEALED RULE IS NOT RE-RUN. It asks for >=2 wins in >=3 coins at a 40% hit rate. Against a
forward base rate of 0.186 a wallet clears that BY CHANCE 9.1% of the time on three coins and 23.5%
on five. With ~16,000 candidates it admits 3,110 wallets of which roughly 1,807 are noise — 58%. It
worked at seal time because the pool was small. It does not scale, so re-running it weekly would
fill the roster with chance.

The registered gate is Benjamini-Hochberg FDR at q=0.05 on an exact one-sided binomial. It imposes
no coin minimum and needs none: 3 wins in 3 coins has p=0.0064 and is genuinely unlikely, while the
sealed rule's 2-in-3 has p=0.091 and is not. It asks for EVIDENCE rather than a fixed threshold,
which is the property that makes it safe to re-run.

THIS SCRIPT NEVER WRITES THE SEALED ROSTER. Every live lane threshold was calibrated on the
290-wallet v1 population and changes meaning if that population moves underneath it, so v2 is a
separate artifact and the swap is the operator's decision, made on the shadow table.

ABSENCE IS NOT UNDER-PERFORMANCE. A wallet with no forward coins has no evidence either way and is
retained. Only measured under-performance demotes, and today nothing does — the smallest raw p is
0.157 across 158 tested wallets.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from math import exp, lgamma, log
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
# Quarantined (Codex W11, 2026-10-06): this prints admission/demotion counts and hit-rate shadows from
# a tracked-peak rule, not authenticated execution paths. A bare launch is refused; the weekly cron
# passes --diagnostic-only. Its old consumer (runner_scanner) was archived 2026-10-02.
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch  # noqa: E402
_legacy_launch(__name__)
SIGNALS = REPO / "data" / "runner_signals.db"
V1 = REPO / "data" / "runner_golden_wallets.json"
V2 = REPO / "data" / "runner_golden_wallets_v2.json"
DEMOTION_LOG = REPO / "data" / "golden_roster_demotions.jsonl"

WIN_MCAP = 40_000.0
# Bump on ANY change to the four gate parameters below or to the outcome definition. A demotion-log
# row records the version AND the parameter values, so a row stays readable after this file moves on
# — the log outlives the code that wrote it, and a bare version string would need this file at the
# matching commit to mean anything.
RULE_VERSION = "bh-fdr-v1"
ADMIT_Q = 0.05          # section 3
DEMOTE_Q = 0.10         # section 4, screening
DEMOTE_MIN_COINS = 5    # section 4
CANDIDATE_MIN_COINS = 3  # section 1


def binom_tail(k, n, p, *, upper=True):
    """Exact binomial tail in LOG SPACE. P(X>=k) when upper, else P(X<=k).

    comb(n, i) overflows a float for a wallet appearing in thousands of coins, and those are
    exactly the wallets the gate has the most evidence about — a naive implementation raises on
    the rows that matter most. Sums the smaller side and complements, so neither tail loses
    precision to cancellation.
    """
    if n <= 0:
        return 1.0
    k = int(k)
    # OUT OF RANGE IS ANSWERED, NOT CLAMPED. Clamping k into [0, n] silently turned P(X <= -1),
    # which is 0, into P(X <= 0), which is not — the two tails then summed to 1.24 instead of 1.
    # A clamp is the right shape for an index and the wrong shape for a bound.
    if k < 0:
        return 1.0 if upper else 0.0
    if k > n:
        return 0.0 if upper else 1.0
    lp, lq = log(p), log(1.0 - p)

    def term(i):
        return exp(lgamma(n + 1) - lgamma(i + 1) - lgamma(n - i + 1) + i * lp + (n - i) * lq)

    if upper:
        if k <= 0:
            return 1.0
        if k > n * p:
            return min(1.0, sum(term(i) for i in range(k, n + 1)))
        return max(0.0, 1.0 - sum(term(i) for i in range(0, k)))
    if k >= n:
        return 1.0
    if k < n * p:
        return min(1.0, sum(term(i) for i in range(0, k + 1)))
    return max(0.0, 1.0 - sum(term(i) for i in range(k + 1, n + 1)))


def benjamini_hochberg(pvalues, q):
    """Indices of the rejected hypotheses. `pvalues` is a list of (p, payload), any order."""
    ordered = sorted(range(len(pvalues)), key=lambda i: pvalues[i][0])
    m = len(ordered)
    cut = 0
    for rank, idx in enumerate(ordered, 1):
        if pvalues[idx][0] <= q * rank / m:
            cut = rank
    return {ordered[i] for i in range(cut)}


def holm(pvalues, alpha):
    """Indices rejected under Holm step-down FWER control."""
    ordered = sorted(range(len(pvalues)), key=lambda i: pvalues[i][0])
    m = len(ordered)
    out = set()
    for rank, idx in enumerate(ordered, 1):
        if pvalues[idx][0] > alpha / (m - rank + 1):
            break
        out.add(idx)
    return out


def forward_window(conn, epoch, *, until=None):
    """Buyer-covered forward coins, their peaks, and address -> coins presence.

    BUYER-COVERED ONLY, per section 1. Only about half of forward tracked coins carry a buyer row,
    and a coin with no buyer data CANNOT show a golden wallet — including those coins deflates
    every rate. Against the diluted population the same lift reads 4.45x instead of 5.10x.
    """
    upper = " AND created_at <= '%s'" % until if until else ""
    peak = {m: pk for m, pk in conn.execute(
        "SELECT mint, MAX(mcap_usd*COALESCE(track_peak_x,1)) FROM runner_signals "
        "WHERE track_peak_x IS NOT NULL AND created_at > ?" + upper + " GROUP BY mint", (epoch,))}
    presence, covered = defaultdict(set), set()
    for address, mint in conn.execute(
            "SELECT DISTINCT b.address, s.mint FROM runner_signal_buyers b "
            "JOIN runner_signals s ON s.id = b.signal_id WHERE s.created_at > ?"
            + upper.replace("created_at", "s.created_at"), (epoch,)):
        if mint in peak:
            presence[address].add(mint)
            covered.add(mint)
    wins = {m for m in covered if (peak[m] or 0) >= WIN_MCAP}
    return peak, presence, covered, wins


def refresh(conn, *, epoch, v1_wallets, write=False):
    # The quarantine holds at the writer itself, not only at main() (Codex night review W11): an
    # importing caller cannot publish a roster without the diagnostic authorization.
    from sniper.research.wallet_execution.publication import UnsupportedResearch, diagnostic_active
    if write and not diagnostic_active():
        raise UnsupportedResearch("golden roster v2 writes only under --diagnostic-only / diagnostic_mode()")
    peak, presence, covered, wins = forward_window(conn, epoch)
    if not covered:
        raise SystemExit("no buyer-covered forward coins after %s; nothing to grade" % epoch)
    base = len(wins) / len(covered)

    # --- admission, section 3 -------------------------------------------------------------
    candidates = [(a, len(ms), sum(1 for m in ms if m in wins))
                  for a, ms in presence.items() if len(ms) >= CANDIDATE_MIN_COINS]
    tests = [(binom_tail(w, n, base, upper=True), (a, n, w)) for a, n, w in candidates]
    admitted_idx = benjamini_hochberg(tests, ADMIT_Q)
    admitted = [tests[i][1] for i in sorted(admitted_idx, key=lambda i: tests[i][0])]

    # --- demotion, section 4 --------------------------------------------------------------
    # Only roster wallets WITH forward evidence are tested. A wallet with no forward coins has no
    # evidence either way; it is retained and recorded as such, never demoted.
    graded, absent = [], []
    for a in v1_wallets:
        ms = presence.get(a, set())
        (graded if len(ms) >= DEMOTE_MIN_COINS else absent).append(
            (a, len(ms), sum(1 for m in ms if m in wins)))
    dtests = [(binom_tail(w, n, base, upper=False), (a, n, w)) for a, n, w in graded]
    demoted_idx = benjamini_hochberg(dtests, DEMOTE_Q) if dtests else set()
    holm_idx = holm(dtests, 0.05) if dtests else set()
    demoted = [dtests[i][1] for i in sorted(demoted_idx)]
    raw_hits = sum(1 for p, _ in dtests if p < 0.05)

    demoted_set = {a for a, _n, _w in demoted}
    retained = [a for a in v1_wallets if a not in demoted_set]
    new_only = [a for a, _n, _w in admitted if a not in set(v1_wallets)]

    report = {
        "epoch_graded": epoch,
        "forward_covered_coins": len(covered),
        "forward_base_rate": round(base, 4),
        "candidates": len(candidates),
        "admitted": len(admitted),
        "admitted_new": len(new_only),
        "expected_false_admissions": round(ADMIT_Q * len(admitted), 1),
        "roster_v1": len(v1_wallets),
        "demotion_tested": len(graded),
        "demotion_absent_retained": len(absent),
        "demotion_raw_p_under_05": raw_hits,
        "demoted_bh": len(demoted),
        "demoted_holm": len(holm_idx),
        "roster_v2": len(set(retained) | set(new_only)),
    }
    if not write:
        return report, None

    from sniper.util.clock import real_utc_now_iso
    now = real_utc_now_iso()
    assert V2 != V1, "the sealed roster is never the output"
    wallets = ([{"address": a, "origin": "v1_retained"} for a in retained]
               + [{"address": a, "coins": n, "wins40k": w, "origin": "bh_admitted"}
                  for a, n, w in admitted if a in set(new_only)])
    from sniper.research.wallet_execution.publication import legacy_eligibility
    V2.write_text(json.dumps({
        "defined_at": now,
        "eligibility": legacy_eligibility(),     # a diagnostic artifact, never an approved roster
        "supersedes": {"path": V1.name, "defined_at": epoch, "wallets": len(v1_wallets)},
        "rule": ("BH-FDR q=%.2f over an exact one-sided binomial against the forward base rate; "
                 "no coin minimum beyond %d covered forward coins"
                 % (ADMIT_Q, CANDIDATE_MIN_COINS)),
        "demotion_rule": ("exact one-sided binomial under-performance, >=%d forward coins, "
                          "BH q=%.2f; absence never demotes" % (DEMOTE_MIN_COINS, DEMOTE_Q)),
        "forward_base_rate": round(base, 4),
        "forward_covered_coins": len(covered),
        "report": report,
        "wallets": wallets,
    }, indent=1))

    # Append-only, so a re-run leaves a trace instead of rebuilding from scratch. Every row carries
    # the epoch it was decided under (prereg 7.2) and the rule that decided it, because the file
    # accumulates across epochs and a row with neither cannot be attributed to a run.
    stamp = {"rule_version": RULE_VERSION,
             "rule_params": {"admit_q": ADMIT_Q, "demote_q": DEMOTE_Q,
                             "demote_min_coins": DEMOTE_MIN_COINS,
                             "candidate_min_coins": CANDIDATE_MIN_COINS,
                             "win_mcap": WIN_MCAP},
             "base_rate": round(base, 4)}
    with DEMOTION_LOG.open("a") as fh:
        for a, n, w in demoted:
            fh.write(json.dumps({"at": now, "graded_epoch": epoch, "address": a,
                                 "action": "demoted", "forward_coins": n, "wins40k": w,
                                 "why": "under-performs the forward base rate, BH q=%.2f"
                                        % DEMOTE_Q, **stamp}) + "\n")
        for a, n, w in absent:
            if n == 0:
                fh.write(json.dumps({"at": now, "graded_epoch": epoch, "address": a,
                                     "action": "retained_no_evidence", "forward_coins": 0,
                                     "why": "absence is not under-performance", **stamp}) + "\n")
    return report, V2


def shadow(conn, *, epoch, v1_wallets, v2_wallets):
    """How the golden lanes WOULD have paged under v2, against how v1 actually paged.

    THE RECONSTRUCTION IS CHECKED AGAINST WHAT THE SCANNER RECORDED, and that check is the point:
    if a rebuilt v1 does not reproduce the paging the scanner actually did, the v2 counterfactual
    is computed by a lane model that does not match the lane, and its numbers mean nothing. The
    check is reported, never assumed.
    """
    _peak, presence, covered, wins = forward_window(conn, epoch)
    coin_buyers = defaultdict(set)
    for address, coins in presence.items():
        for m in coins:
            coin_buyers[m].add(address)

    def paged(roster):
        r = set(roster)
        return {m for m in covered if coin_buyers[m] & r}

    v1_model, v2_model = paged(v1_wallets), paged(v2_wallets)
    actual = {m for (m,) in conn.execute(
        "SELECT DISTINCT mint FROM runner_signals WHERE golden_buyers>0 AND created_at > ?",
        (epoch,))} & covered

    def rate(s):
        return (100.0 * len(s & wins) / len(s)) if s else 0.0

    return {
        "forward_covered_coins": len(covered),
        "base_rate_pct": round(100.0 * len(wins) / len(covered), 1),
        "v1_actual_paged": len(actual),
        "v1_actual_hit_pct": round(rate(actual), 1),
        "v1_model_paged": len(v1_model),
        "v1_model_recovers_actual_pct": round(
            100.0 * len(actual & v1_model) / max(len(actual), 1), 1),
        "v2_model_paged": len(v2_model),
        "v2_model_hit_pct": round(rate(v2_model), 1),
        "overlap_v1_v2": len(v1_model & v2_model),
        "v2_only": len(v2_model - v1_model),
        "v1_only": len(v1_model - v2_model),
        "v2_only_hit_pct": round(rate(v2_model - v1_model), 1),
    }


def split_shadow(conn, *, epoch, v1_wallets):
    """The shadow again, OUT OF SAMPLE, which is the only version that answers the swap question.

    THE PROBLEM WITH THE PLAIN SHADOW. v2's wallets are admitted on the forward window, and the
    plain shadow then grades them on that same window. Every admitted wallet was selected FOR
    having won there, so its hit rate there is guaranteed high and means nothing — it is the
    selection restated. The pre-registration's own section 1 forbids exactly this: "a wallet is
    never graded on the data that admitted it". Grading v2 properly needs signals after v2's
    epoch, and those do not exist yet.

    So the forward window is split in half by time. Admission runs on the first half only, under
    the same registered BH gate; grading runs on the second half, which the gate never saw. That
    is a real out-of-sample read on a smaller roster, and it is the honest preview of what a swap
    would buy.
    """
    row = conn.execute(
        "SELECT MIN(created_at), MAX(created_at) FROM runner_signals WHERE created_at > ?",
        (epoch,)).fetchone()
    lo, hi = row
    from datetime import datetime
    mid = (datetime.fromisoformat(lo) + (datetime.fromisoformat(hi)
                                         - datetime.fromisoformat(lo)) / 2).isoformat()

    _p, pres_a, cov_a, wins_a = forward_window(conn, epoch, until=mid)
    if not cov_a:
        return {"error": "no coverage in the admission half"}
    base_a = len(wins_a) / len(cov_a)
    cand = [(a, len(ms), sum(1 for m in ms if m in wins_a))
            for a, ms in pres_a.items() if len(ms) >= CANDIDATE_MIN_COINS]
    tests = [(binom_tail(w, n, base_a, upper=True), a) for a, n, w in cand]
    admitted = {tests[i][1] for i in benjamini_hochberg(tests, ADMIT_Q)}
    v2_half = set(v1_wallets) | admitted

    _p2, pres_b, cov_b, wins_b = forward_window(conn, mid)
    coin_buyers = defaultdict(set)
    for address, coins in pres_b.items():
        for m in coins:
            coin_buyers[m].add(address)

    def paged(roster):
        r = set(roster)
        return {m for m in cov_b if coin_buyers[m] & r}

    def rate(s):
        return round(100.0 * len(s & wins_b) / len(s), 1) if s else 0.0

    p1, p2 = paged(v1_wallets), paged(v2_half)
    return {
        "admit_half_ends": mid[:19],
        "admit_half_coins": len(cov_a),
        "admitted_on_half": len(admitted - set(v1_wallets)),
        "grade_half_coins": len(cov_b),
        "grade_half_base_pct": round(100.0 * len(wins_b) / len(cov_b), 1) if cov_b else 0.0,
        "v1_paged": len(p1), "v1_hit_pct": rate(p1),
        "v2_paged": len(p2), "v2_hit_pct": rate(p2),
        "v2_only_paged": len(p2 - p1), "v2_only_hit_pct": rate(p2 - p1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="write the v2 artifact and append the demotion log")
    ap.add_argument("--shadow", action="store_true", help="also print the paging comparison")
    a = ap.parse_args()
    sys.path.insert(0, str(REPO))
    v1 = json.loads(V1.read_text())
    v1_wallets = [w["address"] for w in v1["wallets"]]
    conn = sqlite3.connect("file:%s?mode=ro" % SIGNALS, uri=True)

    report, path = refresh(conn, epoch=v1["defined_at"], v1_wallets=v1_wallets, write=a.write)
    print("REFRESH (graded strictly after %s)" % v1["defined_at"])
    for k, v in report.items():
        print("  %-28s %s" % (k, v))
    if path:
        print("  wrote %s" % path)
        print("  demotion log %s" % DEMOTION_LOG)

    if a.shadow:
        v2_wallets = v1_wallets
        if V2.exists():
            v2_wallets = [w["address"] for w in json.loads(V2.read_text())["wallets"]]
        print("\nSHADOW — v2 counterfactual against what v1 actually paged")
        for k, v in shadow(conn, epoch=v1["defined_at"], v1_wallets=v1_wallets,
                           v2_wallets=v2_wallets).items():
            print("  %-28s %s" % (k, v))
        print("\nSHADOW, OUT OF SAMPLE — admit on the first half of the forward window, grade "
              "on the second")
        print("  The table above is IN SAMPLE: v2's wallets were admitted on the very coins it is "
              "graded on, so their hit rate there is the selection restated.")
        for k, v in split_shadow(conn, epoch=v1["defined_at"], v1_wallets=v1_wallets).items():
            print("  %-28s %s" % (k, v))
        print("  NOTE  golden and golden_scalp_v1 open from ONE quote, so their paged sets are "
              "the same by construction (1100 shared signals; 26 golden-only rows predate the "
              "scalp arm). A per-lane split of this table would be two copies of one number.")
    return 0


from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports  # noqa: E402
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    raise SystemExit(main())
