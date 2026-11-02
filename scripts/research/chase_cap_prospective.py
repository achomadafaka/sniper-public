#!/usr/bin/env python3
"""The registered analyzer for the Solana chase-cap prospective run. NOT the retrospective tool.

`chase_cap_drift_bands.py` scans eight caps over CLOSED POSITIONS and compares low-drift mints
against high-drift mints. It cannot express this protocol: it never sees a refused attempt, so it
cannot enter a blocked entry at 0.00 PnL, and mint-vs-mint is not a policy-arm comparison. This
file implements what
`docs/superpowers/specs/2026-08-19-solana-chase-cap-prospective.md` registers.

    python3 scripts/research/chase_cap_prospective.py --start 2026-08-20T00:00:00Z

## Why one live cap measures four arms

The cohort runs at the CONTROL cap of 30, the loosest registered value. Every tighter arm is then
a deterministic function of what was observed: an attempt whose quote drift exceeded `c` would
have been refused under cap `c`, so its arm PnL is 0.00; every other attempt keeps the PnL it
realized. This works in one direction only. A cap LOOSER than the live one is not reconstructable,
because the attempts it would have admitted were refused and never priced.

Every enrolled attempt must therefore have been DECIDED at the control cap. An attempt whose
trace records a different `cap_pct` is excluded and reported: its admission set is not the
control's, so no arm can be reconstructed from it.

## The hypothesis family is FOUR, not five

The control is not a test against itself. Holm runs over the four real arms {10, 15, 20, 25}, so
the first-rank bar is alpha/4 = 0.0125. An earlier draft gave the control a synthetic p=1.0 and
ran a five-test family. That is conservative rather than wrong-in-our-favour, but it is still not
the registered family, and a wrong bar is a wrong bar.

## The permutation applies the EXACT per-attempt policy

The observed arm blocks individual ATTEMPTS by their own drift. An earlier null assigned whole
MINTS to a band by their median drift, so for any mint with attempts on both sides of the cap the
null was evaluating a policy the arm does not implement. Here the per-attempt rule is applied
identically in the observed and permuted worlds; clustering is preserved by permuting whole mints'
DRIFT VECTORS between mints (within equal-attempt-count buckets, so every vector still fits the
mint that receives it) while each mint keeps its own realized PnLs. Mints move as units, the
policy stays exact.

Read-only. Opens the tracker with mode=ro and writes nothing.
"""
import argparse
import json
import random
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Frozen by the protocol. Do not edit without invalidating the run.
CONTROL_CAP = 30.0
ARM_CAPS = (10.0, 15.0, 20.0, 25.0)          # the hypothesis family: four, control excluded
REGISTERED_CAPS = ARM_CAPS + (CONTROL_CAP,)  # what gets a column in the report
ALPHA = 0.05
STOP_COUNT = 120
TERMINAL_BACKSTOP_DAYS = 14
MAX_CENSORED_FRACTION = 0.10
SHUFFLES = 20000
SEED = 7

ELIGIBLE_SQL = """
SELECT a.attempt_id, a.mint, a.source_handle, a.observed_at, a.terminal_status,
       a.source_drift_pct, a.trace_json, a.position_id,
       p.is_open, p.realized_pnl, p.closed_at
FROM memecoin_entry_attempts a
LEFT JOIN memecoin_positions p ON p.id = a.position_id
WHERE a.network_id = 'solana'
  AND a.origin_event_id IS NOT NULL
  AND a.trace_json IS NOT NULL
  AND a.observed_at >= ?
ORDER BY a.observed_at
"""


def _iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _gate_event(trace_json):
    try:
        events = json.loads(trace_json).get("substage_events") or []
    except (TypeError, ValueError):
        return None
    for event in events:
        if event.get("name") in ("chase_quote_confirmation", "chase_quote_skipped"):
            return event
    return None


def _base_state(raw, gate):
    """State before the cohort cutoff and the terminal backstop are applied."""
    if raw["terminal_status"] == "refused" and gate["name"] == "chase_quote_confirmation":
        return "blocked"
    if raw["position_id"] is None:
        return "aborted"
    if raw["is_open"]:
        return "open"
    if raw["realized_pnl"] is None:
        return "aborted"
    return "closed"


def load_cohort(db, start_iso):
    """Every eligible attempt after the start instant, in observation order. No cutoff yet."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = []
    for raw in conn.execute(ELIGIBLE_SQL, (start_iso,)):
        gate = _gate_event(raw["trace_json"])
        if gate is None:                       # never reached the gate -> not eligible
            continue
        drift = gate.get("gap_pct")
        if drift is None:
            drift = raw["source_drift_pct"]
        rows.append({
            "attempt_id": raw["attempt_id"], "mint": raw["mint"], "handle": raw["source_handle"],
            "observed_at": raw["observed_at"], "gate": gate["name"],
            "gate_reason": gate.get("reason"), "cap_at_decision": gate.get("cap_pct"),
            "drift_pct": drift, "terminal_status": raw["terminal_status"],
            "position_id": raw["position_id"], "realized_pnl": raw["realized_pnl"],
            "closed_at": raw["closed_at"],
            "state": _base_state(raw, gate),
        })
    conn.close()
    return rows


def apply_cohort_cutoff(rows, stop_count=STOP_COUNT):
    """Enrol in observation order and CLOSE the cohort at the stop count.

    Loading every later attempt would analyse 121+ and let the sample grow while it is read. The
    120th enrolling attempt fixes the cohort; everything after it is out of cohort and reported
    separately. `aborted` attempts never enrol, so they cannot consume a slot.
    """
    enrolled, out_of_cohort, closed_at = [], [], None
    for row in rows:
        if row["state"] == "aborted":
            row["cohort"] = "aborted"
            out_of_cohort.append(row)
            continue
        if len(enrolled) < stop_count:
            row["cohort"] = "enrolled"
            enrolled.append(row)
            if len(enrolled) == stop_count:
                closed_at = row["observed_at"]
        else:
            row["cohort"] = "out_of_cohort"
            out_of_cohort.append(row)
    return enrolled, out_of_cohort, closed_at


def apply_terminal_backstop(enrolled, cohort_closed_at, days=TERMINAL_BACKSTOP_DAYS, now=None):
    """Mark positions still open at the backstop as `censored`.

    The backstop is `days` after the 120th enrollment. Before it, an open position simply means
    the cohort is not readable. After it, an open position is censored: excluded from the primary
    metric, counted, and reported per arm.
    """
    closed = _iso(cohort_closed_at)
    if closed is None:
        return None, 0
    backstop = closed + timedelta(days=days)
    now = now or datetime.now(timezone.utc)
    if now < backstop:
        return backstop, 0
    censored = 0
    for row in enrolled:
        if row["state"] == "open":
            row["state"] = "censored"
            censored += 1
    return backstop, censored


def arm_pnl(row, cap):
    """PnL of one ATTEMPT under a counterfactual cap. This is the exact policy rule."""
    if row["state"] == "blocked":
        return 0.0
    if row["drift_pct"] is None:
        return None                            # unpriceable: the gate stood down, no arm acted
    if row["drift_pct"] > cap:
        return 0.0                             # this cap would have refused it
    if row["state"] in ("open", "censored"):
        return None                            # admitted but no realized outcome
    return row["realized_pnl"]


def censored_by_arm(rows, cap):
    """Attempts this arm ADMITS but whose outcome is unknown.

    Censoring is arm-dependent, which is why it is reported per arm: a tight arm blocks a
    still-open position, so its PnL under that arm is a known 0.00; a loose arm admits it and
    inherits the unknown.
    """
    return sum(1 for r in rows if r["state"] == "censored" and arm_pnl(r, cap) is None)


def _median_arm(rows, cap):
    values = [v for v in (arm_pnl(r, cap) for r in rows) if v is not None]
    return st.median(values) if values else None


def permutation(rows, cap, control, shuffles, seed):
    """Paired arm-vs-control shift under a mint-clustered permutation of the DRIFT vectors.

    The per-attempt policy rule is applied identically in the observed and permuted worlds. Only
    the pairing of drift vectors to mints is randomised, and whole mints move together, so the
    cluster structure survives. Vectors are permuted within equal-length buckets so that every
    mint receives a drift vector that fits its own attempt count.
    """
    scored = [r for r in rows if r["state"] in ("blocked", "closed")]
    if not scored:
        return 0.0, 1.0, 0

    by_mint = defaultdict(list)
    for r in scored:
        by_mint[r["mint"]].append(r)
    mints = sorted(by_mint)

    def stat(drift_by_mint):
        arm, base = [], []
        for mint in mints:
            for row, drift in zip(by_mint[mint], drift_by_mint[mint]):
                pnl = 0.0 if row["realized_pnl"] is None else row["realized_pnl"]
                if drift is None:
                    continue
                arm.append(0.0 if drift > cap else pnl)
                base.append(0.0 if drift > control else pnl)
        if not arm:
            return None
        return st.median(arm) - st.median(base)

    truth = {mint: [r["drift_pct"] for r in by_mint[mint]] for mint in mints}
    observed = stat(truth)
    if observed is None:
        return 0.0, 1.0, 0
    band = sum(1 for mint in mints for d in truth[mint]
               if d is not None and cap < d <= control)
    if band == 0:
        return observed, 1.0, 0                # this arm blocks nothing; it cannot reject

    buckets = defaultdict(list)
    for mint in mints:
        buckets[len(truth[mint])].append(mint)
    rng = random.Random(seed)
    at_least = 0
    for _ in range(shuffles):
        shuffled = {}
        for size, group in buckets.items():
            vectors = [truth[mint] for mint in group]
            rng.shuffle(vectors)
            shuffled.update(dict(zip(group, vectors)))
        drawn = stat(shuffled)
        if drawn is not None and drawn >= observed:
            at_least += 1
    return observed, (at_least + 1) / (shuffles + 1), band


def holm(per_arm, alpha):
    """Ordered Holm-Bonferroni step-down across the ARM hypotheses. The control is not a test."""
    ordered = sorted(per_arm.items(), key=lambda kv: kv[1]["p"])
    k, out, still = len(ordered), [], True
    for i, (cap, res) in enumerate(ordered):
        bar = alpha / (k - i)
        rejected = still and res["p"] <= bar
        still = rejected
        out.append((cap, res["p"], bar, rejected))
    return out


def report(rows, args, now=None):
    enrolled, out_of_cohort, cohort_closed_at = apply_cohort_cutoff(rows)
    backstop, censored = apply_terminal_backstop(enrolled, cohort_closed_at, now=now)

    wrong_cap = [r for r in enrolled
                 if r["cap_at_decision"] is not None
                 and abs(float(r["cap_at_decision"]) - CONTROL_CAP) > 1e-9]
    counts = defaultdict(int)
    for r in enrolled:
        counts[r["state"]] += 1

    print(f"cohort start   : {args.start}")
    print(f"eligible seen  : {len(rows)} attempts reached the gate")
    print(f"enrolled       : {len(enrolled)} / {STOP_COUNT}"
          + (f"  (cohort CLOSED at {cohort_closed_at})" if cohort_closed_at else "  (OPEN)"))
    print(f"out of cohort  : {sum(1 for r in out_of_cohort if r['cohort'] == 'out_of_cohort')}"
          f"   aborted: {sum(1 for r in out_of_cohort if r['cohort'] == 'aborted')}")
    for state in ("blocked", "closed", "open", "censored"):
        print(f"  {state:<9}: {counts[state]}")
    if backstop:
        print(f"terminal backstop: {backstop.isoformat()}"
              + ("  REACHED" if censored or (now or datetime.now(timezone.utc)) >= backstop
                 else "  not yet reached"))

    stand_downs = defaultdict(int)
    for r in enrolled:
        if r["gate"] == "chase_quote_skipped":
            stand_downs[r["gate_reason"]] += 1
    print(f"gate stand-downs by reason: {dict(stand_downs) or 'none'}")

    if wrong_cap:
        print(f"\nWRONG DECISION CAP: {len(wrong_cap)} enrolled attempts were decided at a cap "
              f"other than the control {CONTROL_CAP:g}. Their admission set is not the control's, "
              "so no arm can be reconstructed from them. The run is invalid.")
        for r in wrong_cap[:5]:
            print(f"    {r['attempt_id']} cap_at_decision={r['cap_at_decision']}")
        return 2

    if len(enrolled) < STOP_COUNT:
        print(f"\nENROLLMENT INCOMPLETE ({len(enrolled)}/{STOP_COUNT}). "
              "The protocol forbids an interim read. No arm results printed.")
        return 2

    if counts["open"]:
        print(f"\nNOT TERMINAL: {counts['open']} enrolled positions are still open and the "
              f"backstop ({backstop.isoformat() if backstop else 'unknown'}) has not passed. "
              "Enrollment closing is not the same as outcomes being known.")
        return 2

    if censored:
        fraction = censored / len(enrolled)
        print(f"\ncensored at the backstop: {censored} ({fraction:.1%} of enrolled)")
        if fraction > MAX_CENSORED_FRACTION:
            print(f"CENSORED-INCONCLUSIVE: above the {MAX_CENSORED_FRACTION:.0%} limit. "
                  "The primary read is invalid and the cap stays at 30.")
            return 2

    scored = [r for r in enrolled if r["state"] in ("blocked", "closed")]
    print(f"\n{'arm':>6} {'medianPnL':>11} {'vs control':>11} {'p':>8} {'bandAttempts':>13} "
          f"{'blocked':>8} {'censored':>9}")
    control_median = _median_arm(scored, CONTROL_CAP)
    print(f"{CONTROL_CAP:>6.0f} {control_median:>11.2f} {'control':>11} {'-':>8} {0:>13} "
          f"{sum(1 for r in scored if arm_pnl(r, CONTROL_CAP) == 0.0):>8} "
          f"{censored_by_arm(enrolled, CONTROL_CAP):>9}")
    per_arm = {}
    for cap in ARM_CAPS:
        effect, p, band = permutation(scored, cap, CONTROL_CAP, args.shuffles, args.seed)
        per_arm[cap] = {"median": _median_arm(scored, cap), "effect": effect, "p": p}
        print(f"{cap:>6.0f} {per_arm[cap]['median']:>11.2f} {effect:>+11.2f} {p:>8.4f} "
              f"{band:>13} {sum(1 for r in scored if arm_pnl(r, cap) == 0.0):>8} "
              f"{censored_by_arm(enrolled, cap):>9}")

    print(f"\nHolm step-down over the {len(ARM_CAPS)} arm hypotheses "
          f"(the control is not a test), alpha={ALPHA}")
    print(f"  {'rank':>4} {'arm':>5} {'p':>8} {'bar':>8}  verdict")
    survivors = []
    for rank, (cap, p, bar, rejected) in enumerate(holm(per_arm, ALPHA), 1):
        print(f"  {rank:>4} {cap:>5.0f} {p:>8.4f} {bar:>8.5f}  "
              f"{'reject null' if rejected else 'retain null'}")
        if rejected:
            survivors.append(cap)
    print(f"\nARMS SURVIVING: {survivors or 'NONE -> the cap stays at 30 and the run closes null'}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--start", required=True, help="cohort start instant, ISO UTC")
    ap.add_argument("--shuffles", type=int, default=SHUFFLES)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--ledger", help="write one row per eligible attempt to this JSONL path")
    args = ap.parse_args()

    rows = load_cohort(args.db, args.start)
    if args.ledger:
        with open(args.ledger, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
        print(f"wrote {len(rows)} ledger rows to {args.ledger}")
    raise SystemExit(report(rows, args))


if __name__ == "__main__":
    main()
