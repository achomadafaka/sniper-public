#!/usr/bin/env python3
"""Refusal-family Read 1 of docs/audits/2026-09-23-refusal-family-paper-prereg.md.

For each funded refusal family: did the refusal avoid a loss? Read on the `funded_clone` paper arm's
closed positions, against the same arm's positions on origins the funded path FILLED.

The registered first look is 2026-10-01. The operator ordered this read early on 2026-09-28; every
run says so in its header.

What the prereg fixes, and how this script applies it:
  * Coverage is printed BEFORE any PnL. `--coverage-only` prints it and stops, so the family
    neutrality check can be published before any return exists.
  * No family is read under 30 closed positions; such a family prints "not estimable".
  * Chains are never pooled. Every table row is one chain.
  * The median leads, with a bootstrap 95% interval; the mean sits beside it.
  * The read is on RETURN (realized_pnl / entry_notional), never dollars: refusal rows are sized at
    the paper base, filled rows at the funded clip.
  * Fomo, manual and operator-modified rows are excluded: experiment_status='eligible' plus the
    registered `measurable_outcome_sql()` censor. Only J7 `follow_activity` origins are read; the
    paper lane copies nothing else.
  * Cohort cuts are not pooled. The read starts 2026-09-25T00:00Z, the first full UTC day after the
    last registered cut (2026-09-24T07:37:59.687327Z, funded_clone). `real_shadow` is the labelled
    legacy cohort (rails that cannot fire) and gets counts only. The round-trip family is split at
    the 8b819cad runner start (one re-quote before a round-trip refusal stands).

How the funded outcome of an origin is decided. FILLED when a REAL position carries the origin's
`origin_event_id`. Attempt rows alone miss fills: 33 origins since 09-25 have a single attempt row
with terminal_status NULL and no completed_at, yet a real position references that attempt (the
entry-trace loss fixed in cde6b9ee). Otherwise the family is the LAST attempt's terminal reason (the
first attempt of a retried origin is usually an infra refusal), and an origin with no finished
attempt is `unfinished`.

READ-ONLY: opens the ledger through get_readonly_connection() and writes nothing.

    python3 scripts/research/refusal_family_read1.py --until 2026-09-28T10:20:00Z --coverage-only
    python3 scripts/research/refusal_family_read1.py --until 2026-09-28T10:20:00Z
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import collections
import contextlib
import datetime as dt
import math
import random
import re
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))

from refusal_family_outcomes import family as _base_family  # noqa: E402
from sniper.persistence.db_core import get_readonly_connection  # noqa: E402
from sniper.persistence.db_memecoin import measurable_outcome_sql  # noqa: E402
from sniper.util.envfiles import load_env_files  # noqa: E402
from sniper.util.state_paths import state_root  # noqa: E402

load_env_files(str(state_root()))

ARM = "funded_clone"
LEGACY_ARM = "real_shadow"
LEGACY_CUT = "2026-09-23T20:58:15.205817+00:00"
LAST_CUT = "2026-09-24T07:37:59.687327+00:00"
COHORT_START = "2026-09-25T00:00:00+00:00"
# Runner start of release 8b819cad (MEMECOIN_ENTRY_ROUNDTRIP_REQUOTE, code default 1). A cohort cut
# is the runner start, not the deploy launch (~10:12Z) -- the prereg's own rule for its first cut.
ROUNDTRIP_REQUOTE_AT = "2026-09-28T10:15:04+00:00"
MIN_CLOSED = 30
HELD = ("opened, arm already held coin", "skipped_dup")
SEED = 20260928
DRAWS = 2000

THRESHOLDS = {
    "mcap over ceiling": r"over ceiling (\$[\d,]+)",
    "source buy under absolute floor": r"absolute floor (\$[\d,]+)",
    "source buy under ratio floor": r"under (?:unknown-)?mcap floor ([\d.]+%)",
    "chase: quote above source price": r"\(cap (\d+%)\)",
    "chase: coin already ran": r"\(cap (\d+%)\)",
    "h1 volume under floor": r"under floor (\$[\d,]+)",
    "round trip over cap": r"over ([\d.]+%) cap",
    "price impact over cap": r"over ([\d.]+%) cap",
    "coin too young": r"floor (\d+s)",
}


def utc(value):
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def family(reason, status, decided):
    r = (reason or "").strip()
    if re.match(r"^source buy .*under (unknown-)?mcap floor", r):
        name = "source buy under ratio floor"
    elif r.startswith("one position per coin"):
        name = "one position per coin"
    else:
        name = _base_family(r or (status or ""))
    if name == "round trip over cap":
        name += " (pre-requote)" if decided < utc(ROUNDTRIP_REQUOTE_AT) else " (post-requote)"
    return name


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, 100 * (c - h)), min(100.0, 100 * (c + h)))


def boot_ci(values, stat, rng):
    n = len(values)
    draws = sorted(stat([values[rng.randrange(n)] for _ in range(n)]) for _ in range(DRAWS))
    return draws[int(0.025 * DRAWS)], draws[int(0.975 * DRAWS) - 1]


def load(conn, since, until):
    lo = since.date().isoformat()
    hi = (until.date() + dt.timedelta(days=1)).isoformat()
    attempts = collections.defaultdict(list)
    for a in conn.execute(
            "SELECT attempt_id, origin_event_id, network_id, mint, source_user_id, "
            "decision_started_at, terminal_status, terminal_reason FROM memecoin_entry_attempts "
            "WHERE signal_source='follow_activity' AND decision_started_at>=? AND decision_started_at<?",
            (lo, hi)):
        if a["origin_event_id"]:
            attempts[a["origin_event_id"]].append(dict(a))
    origins = {}
    for oid, rows in attempts.items():
        rows.sort(key=lambda r: (r["decision_started_at"], r["attempt_id"]))
        first = utc(rows[0]["decision_started_at"])
        if since <= first < until:
            origins[oid] = {"rows": rows, "decided": first, "network_id": rows[0]["network_id"],
                            "mint": rows[0]["mint"], "source_user_id": rows[0]["source_user_id"]}
    real = {r["origin_event_id"] for r in conn.execute(
        "SELECT origin_event_id FROM memecoin_positions WHERE execution_mode='real' "
        "AND origin_event_id IS NOT NULL AND opened_at>=?", ((since - dt.timedelta(days=1)).isoformat(),))}
    paper = {}
    for p in conn.execute(
            "SELECT id, origin_event_id, is_open, opened_at, closed_at, realized_pnl, entry_notional, "
            f"exit_reason, experiment_status, ({measurable_outcome_sql()}) AS measurable "
            "FROM memecoin_positions WHERE execution_mode='paper' AND strategy=? AND opened_at>=?",
            (ARM, lo)):
        paper[p["origin_event_id"]] = dict(p)
    for oid, o in origins.items():
        rows = o["rows"]
        finished = [r for r in rows if r["terminal_status"] or r["terminal_reason"]]
        o["retry_family_change"] = False
        if oid in real or any(r["terminal_status"] == "filled" for r in rows):
            o["family"] = "FILLED (control)"
        elif not finished:
            o["family"] = "unfinished"
        else:
            last = finished[-1]
            o["family"] = family(last["terminal_reason"], last["terminal_status"], o["decided"])
            f0 = family(finished[0]["terminal_reason"], finished[0]["terminal_status"], o["decided"])
            o["retry_family_change"] = f0 != o["family"]
        o["paper"] = paper.get(oid)
        if o["paper"] is None:
            snap = conn.execute("SELECT action FROM memecoin_entry_snapshots WHERE upstream_event_id=?",
                                (oid,)).fetchone()
            action = snap["action"] if snap else "no snapshot"
            if action == "opened":
                held = conn.execute(
                    "SELECT 1 FROM memecoin_positions WHERE source_user_id=? AND mint=? AND strategy=? "
                    "AND execution_mode='paper' AND opened_at<=? AND (closed_at IS NULL OR closed_at>?) "
                    "LIMIT 1", (o["source_user_id"], o["mint"], ARM, rows[0]["decision_started_at"],
                                rows[0]["decision_started_at"])).fetchone()
                action = "opened, arm already held coin" if held else "opened, no arm position"
            elif action.startswith("skipped_quote_") or action == "skipped_no_executable_quote":
                action = "no executable paper quote"
            o["uncovered"] = action
    return origins


def arm_counts(conn, until):
    print("A. ARM VERIFICATION (paper rows by arm and span; spans are never pooled)")
    spans = [(LEGACY_ARM, LEGACY_CUT, None), (ARM, "2026-09-24T06:35:48+00:00", LAST_CUT),
             (ARM, LAST_CUT, COHORT_START), (ARM, COHORT_START, None)]
    for arm, a, b in spans:
        # Bounded by --until on both open and close, so two runs with one --until agree.
        q = ("SELECT COUNT(*), SUM(is_open=0 AND closed_at<?), MIN(opened_at), MAX(opened_at) "
             "FROM memecoin_positions WHERE execution_mode='paper' AND strategy=? AND opened_at>=? "
             "AND opened_at<?")
        args = [until.isoformat(), arm, a, min(b or until.isoformat(), until.isoformat())]
        n, closed, lo, hi = conn.execute(q, args).fetchone()
        role = ("LEGACY: tp/stop/trail 0.0, answers nothing about funded" if arm == LEGACY_ARM
                else "READ COHORT" if a == COHORT_START else "partial span, excluded from the read")
        print(f"  {arm:13} {a[:26]} -> {(b or '--until')[:26]:26}  rows {n:5} closed {closed or 0:5}  "
              f"[{(lo or '-')[:19]} .. {(hi or '-')[:19]}]  {role}")


def gate_stability(origins):
    print("\nB. GATE STABILITY (threshold text in the refusal reason, per family, per UTC day; "
          "several values in one day can be per-wallet or per-band thresholds)")
    seen = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    for o in origins.values():
        base = o["family"].split(" (")[0]
        pat = THRESHOLDS.get(base)
        if not pat:
            continue
        reason = [r for r in o["rows"] if r["terminal_reason"]][-1]["terminal_reason"]
        m = re.search(pat, reason)
        seen[(o["network_id"], base)][o["decided"].date().isoformat()][m.group(1) if m else "?"] += 1
    for (net, fam) in sorted(seen):
        days = seen[(net, fam)]
        print(f"  {net:7} {fam}")
        for day in sorted(days):
            vals = ", ".join(f"{v} x{n}" for v, n in days[day].most_common(4))
            more = len(days[day]) - 4
            print(f"      {day}: {vals}" + (f", +{more} more values" if more > 0 else ""))


def coverage(origins):
    print("\nC. COVERAGE: family neutrality, published before any PnL "
          "(J7 follow_activity origins decided in the window -> a funded_clone paper position)")
    table = collections.defaultdict(list)
    for o in origins.values():
        table[(o["network_id"], o["family"])].append(o)
    chains = sorted({k[0] for k in table}, key=lambda n: (-sum(len(v) for k, v in table.items() if k[0] == n), n))
    for net in chains:
        print(f"\n  chain {net}")
        print(f"    {'family':42} {'origins':>7} {'paper':>6} {'cover%':>7} {'95% CI':>13} "
              f"{'openable':>8} {'cover%':>7} {'95% CI':>13}  uncovered by mechanism")
        rows = sorted(((k[1], v) for k, v in table.items() if k[0] == net), key=lambda kv: (-len(kv[1]), kv[0]))
        for fam, lst in rows:
            k = sum(1 for o in lst if o["paper"])
            lo, hi = wilson(k, len(lst))
            # OPENABLE removes the re-buy structure: the arm opens one position per (source, coin)
            # and cannot open a second while the first is held, so a repeat buy is not a coverage
            # miss about the family. What remains is the arm's own quote and chase gates.
            openable = [o for o in lst if o["paper"] or o["uncovered"] not in HELD]
            olo, ohi = wilson(k, len(openable))
            ocov = f"{100 * k / len(openable):>6.1f}%" if openable else "     -"
            why = collections.Counter(o["uncovered"] for o in lst if not o["paper"])
            whys = "; ".join(f"{w} {n}" for w, n in why.most_common(4))
            print(f"    {fam[:42]:42} {len(lst):>7} {k:>6} {100 * k / len(lst):>6.1f}% "
                  f"[{lo:4.1f},{hi:5.1f}] {len(openable):>8} {ocov} [{olo:4.1f},{ohi:5.1f}]  {whys}")
    changed = sum(1 for o in origins.values() if o["retry_family_change"])
    print(f"\n  origins whose first and last attempt fall in different families (family = last): {changed}")


def pnl(origins, until):
    print("\nD. PnL READ on RETURN (realized_pnl / entry_notional, %), closed before --until, "
          f"eligible + measurable; n < {MIN_CLOSED} is not estimable")
    rng = random.Random(SEED)
    table = collections.defaultdict(list)
    excl = collections.defaultdict(collections.Counter)
    for o in origins.values():
        p = o["paper"]
        key = (o["network_id"], o["family"])
        if not p:
            continue
        if p["is_open"] or not p["closed_at"] or utc(p["closed_at"]) >= until:
            excl[key]["open at --until"] += 1
        elif p["experiment_status"] != "eligible":
            excl[key][f"status {p['experiment_status']}"] += 1
        elif not p["measurable"]:
            excl[key]["not measurable"] += 1
        elif not p["entry_notional"] or p["entry_notional"] <= 0:
            excl[key]["no notional"] += 1
        else:
            table[key].append((100 * p["realized_pnl"] / p["entry_notional"], p["exit_reason"] or "?"))
    chains = sorted({k[0] for k in list(table) + list(excl)},
                    key=lambda n: (-sum(len(v) for k, v in table.items() if k[0] == n), n))
    for net in chains:
        print(f"\n  chain {net}")
        print(f"    {'family':42} {'n':>4} {'median%':>8} {'95% CI':>17} {'mean%':>7} {'95% CI':>17} "
              f"{'win%':>5} {'95% CI':>13} {'vd':>3}  exits (top 4)  | excluded")
        keys = sorted((k for k in set(table) | set(excl) if k[0] == net),
                      key=lambda k: (k[1] != "FILLED (control)", -len(table.get(k, [])), k[1]))
        for key in keys:
            vals = table.get(key, [])
            ex = "; ".join(f"{w} {n}" for w, n in excl[key].most_common())
            if len(vals) < MIN_CLOSED:
                print(f"    {key[1][:42]:42} {len(vals):>4}  not estimable (n < {MIN_CLOSED})"
                      + (f"  | {ex}" if ex else ""))
                continue
            r = [v for v, _ in vals]
            med, mean = statistics.median(r), statistics.fmean(r)
            mlo, mhi = boot_ci(r, statistics.median, rng)
            alo, ahi = boot_ci(r, statistics.fmean, rng)
            wins = sum(1 for v in r if v > 0)
            wlo, whi = wilson(wins, len(r))
            # vd = vol_death exits. That rail fires only where a funded tape exists, so it has no
            # counterpart on most refused coins (prereg); a family's exit mix is not the control's.
            vd = sum(1 for _, e in vals if e == "vol_death")
            exits = ", ".join(f"{e} {n}" for e, n in collections.Counter(e for _, e in vals).most_common(4))
            print(f"    {key[1][:42]:42} {len(r):>4} {med:>+8.1f} [{mlo:>+6.1f},{mhi:>+7.1f}] "
                  f"{mean:>+7.1f} [{alo:>+6.1f},{ahi:>+7.1f}] {100 * wins / len(r):>5.0f} "
                  f"[{wlo:4.1f},{whi:5.1f}] {vd:>3}  {exits}" + (f"  | {ex}" if ex else ""))


def selftest():
    """The family boundaries this read depends on. Exits non-zero on the first wrong answer."""
    pre, post = utc("2026-09-28T10:15:03Z"), utc("2026-09-28T10:15:04Z")
    rt = "round trip 10.87% over 10.0% cap (sell back $93.71 of $105.14)"
    assert family(rt, "refused", pre) == "round trip over cap (pre-requote)"
    assert family(rt, "refused", post) == "round trip over cap (post-requote)"
    assert family("source buy $100 (0.191%) under mcap floor 0.250% ($130)", "refused", pre) \
        == "source buy under ratio floor"
    assert family("source buy unknown under mcap floor 0.500% ($186)", "refused", pre) \
        == "source buy under ratio floor"
    assert family("source buy $100 under absolute floor $197", "refused", pre) \
        == "source buy under absolute floor"
    assert family("one position per coin: open funded position", "refused", pre) == "one position per coin"
    assert family("exact quote is +51% above source price (cap 30%)", "refused", pre) \
        == "chase: quote above source price"
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and 27 < hi < 29
    print("selftest ok")
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--until", required=True, help="exclusive UTC instant: origin and close cutoff")
    ap.add_argument("--since", default=COHORT_START)
    ap.add_argument("--coverage-only", action="store_true")
    args = ap.parse_args()
    since, until = utc(args.since), utc(args.until)
    if since < utc(LAST_CUT):
        raise SystemExit(f"--since precedes the last registered cut {LAST_CUT}; spans are not poolable")
    print("REFUSAL FAMILIES, READ 1 -- EARLY READ AT THE OPERATOR'S ORDER (2026-09-28); "
          "the registered first look is 2026-10-01.")
    print(f"arm {ARM}; origins decided [{since.isoformat()}, {until.isoformat()}); chains never pooled; "
          f"median leads; return not dollars; seed {SEED}, {DRAWS} bootstrap draws.")
    with contextlib.closing(get_readonly_connection()) as conn:
        arm_counts(conn, until)
        origins = load(conn, since, until)
    print(f"\n  J7 follow_activity origins in the window: {len(origins)}")
    gate_stability(origins)
    coverage(origins)
    if args.coverage_only:
        print("\n--coverage-only: no PnL computed.")
        return 0
    pnl(origins, until)
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    raise SystemExit(main())
