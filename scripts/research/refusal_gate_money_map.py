#!/usr/bin/env python3
"""Refusal gate money map: what every funded entry gate turned away, and what that cost or saved.

Pre-registered in HANDOFF 2026-09-29T22:32Z (amendment 22:40Z) and brain episode c7923401 before any
outcome in this population was computed. Overnight operator order via Alpha: "run stats on all
refusals and entries to see what is in our power to adjust for best entries". Gamma owns the
REFUSAL side. Read-only: the ledger (bounded by decision time) and data/memecoin_refusal_captures.db.

Population: attempts decided in [START, END) with terminal_status in refused/aborted/rejected/
safety_failed, non-manual, ONE row per origin (its first such attempt), origins that ever filled
dropped. Family: memecoin_refusal_capture.classify_gate, else refusal_family_outcomes.family marked
UNCAPTURED. Governance and infrastructure outcomes are counted, never graded.

Instrument (the refused coin, from its refusal capture):
  Solana  exact route: the clip buy at ~3 s, sells of the same quantity; return = sell / clip - 1.
  EVM     DexScreener display price; ref = first ok observation at age <= 60 s; return = p / ref - 1.
          OPTIMISTIC: chart space, no route cost, ~25 s index lag.
  Grade   cap_refusal_outcomes.grade: TP +25%, stop -35%, else the observation nearest +300 s
          (within 20 s); none = censored. Uncaptured and censored are never read as zero.

    python3 scripts/research/refusal_gate_money_map.py            # the report
    python3 scripts/research/refusal_gate_money_map.py --selftest
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import collections
import contextlib
import json
import re
import sqlite3
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))

from cap_refusal_outcomes import grade, mint_ci  # noqa: E402

LEDGER = REPO / "sniper_tracker.db"
CAPTURES = REPO / "data" / "memecoin_refusal_captures.db"
START, END = "2026-09-13T00:00:00+00:00", "2026-09-29T22:29:28+00:00"
DAYS = 16.936                                     # (END - START) in days
STATUSES = ("refused", "aborted", "rejected", "safety_failed")
GOVERNANCE = ("not_approved", "disarmed", "unroutable_chain", "excluded")
INFRA = ("no quote", "no route", "route build refused", "reverted", "preflight simulation",
         "unsellable quote asset")
INFRA_WORDS = ("chain_error", "interrupted", "decimals", "approve failed", "pre-tx failed",
               "build refused", "_shadow", "slot_lane_scaled_clip", "unresolved signed",
               "balance unavailable", "invalid_funded_contract", "mcap_unresolv", "insufficient",
               "max concurrent", "safety_deadline", "not visible", "unreadable", "rejected by node",
               "sell route", "execut", "ambiguous")
REF_MAX_AGE_S = 60.0
# POST HOC (added after the first run, 2026-09-29T22:5xZ, stated in the audit): one BSC coin
# (0xcafdbce934...) returns a DexScreener price 1e41 x its reference, a corrupt quote, and it alone
# turned the 56 mcap row into +9e41%. An observation above 1000x its reference is dropped as corrupt.
CORRUPT_RATIO = 1000.0
SEED = 20260930
MIN_N = 10

# The measured value and its threshold, parsed from the refusal reason, per gate family. `kind` is
# 'ceiling' (refused because value > threshold) or 'floor' (refused because value < threshold).
_N = r"([0-9][0-9,]*\.?[0-9]*)"
PARSERS = {
    "mcap_over_ceiling": ("ceiling", re.compile(r"^mcap \$%s over ceiling \$%s" % (_N, _N))),
    "roundtrip_cap": ("ceiling", re.compile(r"^round trip %s%% over %s%% cap" % (_N, _N))),
    "impact_cap": ("ceiling", re.compile(r"^price impact %s%% over %s%% cap" % (_N, _N))),
    "chase_quote": ("ceiling", re.compile(r"^exact quote is \+%s%% above source price \(cap %s%%" % (_N, _N))),
    "evm_chase_quote": ("ceiling", re.compile(r"^our quote is \+%s%% above source price \(cap %s%%" % (_N, _N))),
    "chase_past_entry": ("ceiling", re.compile(r"^coin already ran \+%s%% past their entry \(cap %s%%" % (_N, _N))),
    "chase_first_seen": ("ceiling", re.compile(r"^coin ran %sx above first-seen price \(cap %sx" % (_N, _N))),
    "route_over_mid": ("ceiling", re.compile(r"^route quote is %sx the chart mid \(cap %sx" % (_N, _N))),
    "route_over_source": ("ceiling", re.compile(r"^route quote is %sx the source price \(cap %sx" % (_N, _N))),
    "m5_turnover_over_ceiling": ("ceiling", re.compile(r"volume \$%s OVER ceiling \$%s" % (_N, _N))),
    "source_buy_floor": ("floor", re.compile(r"\(%s%%\) under mcap floor %s%%" % (_N, _N))),
    "m5_turnover_under_floor": ("floor", re.compile(r"volume \$%s under floor \$%s" % (_N, _N))),
    "turnover_under_floor": ("floor", re.compile(r"volume \$%s under floor \$%s" % (_N, _N))),
    "m5_volume_under_abs_floor": ("floor", re.compile(r"volume \$%s under floor \$%s" % (_N, _N))),
}
KNOBS = {
    "mcap_over_ceiling": "MEMECOIN_REAL_MAX_MCAP_USD=250000 (+ _BY_WALLET overrides)",
    "roundtrip_cap": "MEMECOIN_ENTRY_ROUNDTRIP_CAP_PCT=10 / _TP_FRAC=0.6",
    "impact_cap": "MEMECOIN_ENTRY_IMPACT_CAP_TIERS=50000:30,1000000:12,5000000:8,30000000:6",
    "chase_quote": "MEMECOIN_REAL_CHASE_MAX_PCT=30 (NANO 35)",
    "evm_chase_quote": "MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_4663=30 / CHASE_MAX_PCT=30",
    "chase_past_entry": "MEMECOIN_REAL_CHASE_MAX_PCT=30",
    "chase_first_seen": "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO=3",
    "route_over_mid": "MEMECOIN_REAL_MAX_EXEC_PREMIUM=1.5",
    "route_over_source": "MEMECOIN_REAL_MAX_EXEC_PREMIUM=1.5 (source basis)",
    "m5_turnover_over_ceiling": "MEMECOIN_REAL_MAX_TURNOVER_M5=1.50 / per-band INBAND_MAX (4663, 56: 2.0)",
    "source_buy_floor": "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO=0.008 (_BY_BAND nano/micro 0.005, solana:micro 0.0025)",
    "m5_turnover_under_floor": "MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5_4663/_56=0.02",
    "turnover_under_floor": "MEMECOIN_REAL_SOLANA_MICRO_MIN_TURNOVER_H1=0.143 / MIN_VOLUME_H1_USD=3000",
    "m5_volume_under_abs_floor": "MEMECOIN_REAL_MICRO_MIN_VOLUME_M5_USD=0",
    "reentry_cooldown": "MEMECOIN_REAL_REENTRY_COOLDOWN_S=86400",
    "drawdown_pause": "MEMECOIN_DRAWDOWN_PAUSE_USD=100 / _S=2640",
    "mcap_under_floor": "per-wallet mcap floor",
}


def ro(path):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _num(s):
    return float(s.replace(",", ""))


def measured(family, reason):
    """(kind, r = value / threshold) or None."""
    spec = PARSERS.get(family)
    m = spec and spec[1].search(reason or "")
    if not m:
        return None
    value, thr = _num(m.group(1)), _num(m.group(2))
    return (spec[0], value / thr) if thr > 0 else None


def classify(status, reason):
    """(category, family): category is captured/uncaptured/governance/infra."""
    from sniper.services.memecoin_refusal_capture import classify_gate
    from refusal_family_outcomes import family as fam
    g = classify_gate(reason)
    if g:
        return "captured", g
    f = fam(reason)
    if f in GOVERNANCE:
        return "governance", f
    if f in INFRA or any(w in (reason or "") for w in INFRA_WORDS):
        return "infra", f
    return "uncaptured", f


def _facts(cj):
    try:
        return json.loads(cj or "{}") or {}
    except (TypeError, ValueError):
        return {}


def population(c):
    rows = c.execute(
        "SELECT attempt_id, origin_event_id, network_id, mint, terminal_status, terminal_reason, "
        "signal_source, decision_started_at, classification_json FROM memecoin_entry_attempts "
        "WHERE decision_started_at >= ? AND decision_started_at < ? AND terminal_status IS NOT NULL "
        "ORDER BY decision_started_at, attempt_id", (START, END)).fetchall()
    filled = {r["origin_event_id"] for r in rows if r["terminal_status"] == "filled"}
    first = {}
    for r in rows:
        o = r["origin_event_id"]
        if (r["terminal_status"] not in STATUSES or r["signal_source"] == "manual" or o in first
                or o in filled or not o):
            continue
        cat, fam = classify(r["terminal_status"], r["terminal_reason"])
        first[o] = {"origin": o, "net": str(r["network_id"]), "mint": r["mint"], "at": r["decision_started_at"],
                    "reason": r["terminal_reason"], "cat": cat, "fam": fam,
                    "band": _facts(r["classification_json"]).get("coin_band") or "unknown"}
    return list(first.values()), len(filled)


def solana_obs(cap, origin):
    rows = cap.execute("SELECT age_s, quote_side, quote_input_usd, token_qty_atomic, quote_output_atomic "
                       "FROM memecoin_refusal_captures WHERE network_id='solana' AND origin_event_id=? "
                       "AND quality='ok' ORDER BY age_s", (origin,)).fetchall()
    buy = next((r for r in rows if r["quote_side"] == "buy" and r["quote_input_usd"]), None)
    if buy is None:
        return None, []
    clip = float(buy["quote_input_usd"])
    return clip, [(float(r["age_s"]), int(r["quote_output_atomic"]) / 1e6 / clip - 1.0) for r in rows
                  if r["quote_side"] == "sell" and r["token_qty_atomic"] == buy["token_qty_atomic"]
                  and r["quote_output_atomic"] and r["age_s"] is not None]


def display_obs(cap, net, origin):
    rows = [(float(a), float(p)) for a, p in cap.execute(
        "SELECT age_s, quote_price FROM memecoin_refusal_captures WHERE network_id=? AND origin_event_id=? "
        "AND quote_price_basis='display' AND quality='ok' AND quote_price > 0 AND age_s IS NOT NULL "
        "ORDER BY age_s", (net, origin))]
    if not rows or rows[0][0] > REF_MAX_AGE_S:
        return []
    ref = rows[0][1]
    return [(a, p / ref - 1.0) for a, p in rows[1:] if p / ref <= CORRUPT_RATIO]


def admitted_display(c, a):
    """SAME-INSTRUMENT CONTROL (post hoc, stated in the audit): an admitted EVM fill graded on the
    DexScreener marks of its own position, reference = the first mark at <= 60 s after the decision,
    so the refused and admitted EVM sides are read with one instrument. Returns graded (ret, how)."""
    from datetime import datetime, timezone
    t0 = datetime.fromisoformat(a["at"]).timestamp()
    rows = []
    for ts, mid in c.execute(
            "SELECT sweep_ts, mid FROM memecoin_position_marks WHERE chain_id=? AND mint=? AND sweep_ts >= ? "
            "AND sweep_ts <= ? AND source='dexscreener' AND mid > 0 ORDER BY sweep_ts",
            (a["net"], a["mint"], a["at"], datetime.fromtimestamp(t0 + 400, timezone.utc).isoformat())):
        try:
            rows.append((datetime.fromisoformat(ts).timestamp() - t0, float(mid)))
        except (TypeError, ValueError):
            continue
    if not rows or rows[0][0] > REF_MAX_AGE_S:
        return None, "censored"
    ref = rows[0][1]
    return grade([(t, p / ref - 1.0) for t, p in rows[1:] if p / ref <= CORRUPT_RATIO])


def admitted(c):
    """Admitted non-manual real fills whose attempt decided in the window: (net, band, mint, ret, pnl,
    notional, at, trace, facts)."""
    out = []
    for r in c.execute(
            "SELECT a.network_id, a.decision_started_at, a.classification_json, a.trace_json, p.mint, "
            "p.realized_pnl, p.entry_notional FROM memecoin_entry_attempts a JOIN memecoin_positions p "
            "ON p.id=a.position_id WHERE a.decision_started_at >= ? AND a.decision_started_at < ? "
            "AND a.terminal_status='filled' AND a.signal_source != 'manual' AND p.execution_mode='real' "
            "AND p.is_open=0 AND p.realized_pnl IS NOT NULL AND p.entry_notional > 0", (START, END)):
        cj = _facts(r["classification_json"])
        out.append({"net": str(r["network_id"]), "band": cj.get("coin_band") or "unknown", "mint": r["mint"],
                    "ret": r["realized_pnl"] / r["entry_notional"], "pnl": r["realized_pnl"],
                    "notional": r["entry_notional"], "at": r["decision_started_at"],
                    "facts": cj.get("facts") or {}, "trace": r["trace_json"]})
    return out


def stats(rows):
    """rows: [(mint, ret, usd)] -> dict."""
    if not rows:
        return {"n": 0}
    rets = [r for _m, r, _u in rows]
    lo, hi = mint_ci([(m, r) for m, r, _u in rows], seed=SEED)
    return {"n": len(rows), "mean": sum(rets) / len(rets), "lo": lo, "hi": hi,
            "sum": sum(u for _m, _r, u in rows), "wins": sum(r > 0 for r in rets)}


def fmt(s):
    if not s.get("n"):
        return "n=0"
    return "n=%d mean %+.1f%% CI [%+.1f, %+.1f] sum $%+.0f" % (
        s["n"], 100 * s["mean"], 100 * s["lo"], 100 * s["hi"], s["sum"])


def diff_ci(a, b, n=4000, seed=SEED):
    """95% CI of mean(a) - mean(b), resampling mints within each side. a, b: [(mint, value)]."""
    import random
    ga, gb = collections.defaultdict(list), collections.defaultdict(list)
    for m, v in a:
        ga[m].append(v)
    for m, v in b:
        gb[m].append(v)
    ka, kb, rnd, out = list(ga), list(gb), random.Random(seed), []
    for _ in range(n):
        xa = [x for k in (ka[rnd.randrange(len(ka))] for _ in ka) for x in ga[k]]
        xb = [x for k in (kb[rnd.randrange(len(kb))] for _ in kb) for x in gb[k]]
        out.append(sum(xa) / len(xa) - sum(xb) / len(xb))
    out.sort()
    return out[int(0.025 * n)], out[int(0.975 * n) - 1]


def split_half(rows):
    ats = sorted(r["at"] for r in rows)
    cut = ats[len(ats) // 2]
    return [r for r in rows if r["at"] < cut], [r for r in rows if r["at"] >= cut], cut


def loosen(rows):
    """Best looser threshold factor k on H1, tested on H2. rows: graded refusals with 'r' and 'kind'."""
    rs = [r for r in rows if r.get("r") is not None]
    if len(rs) < MIN_N:
        return {"verdict": "not computable (n=%d with a parsed value)" % len(rs)}
    kind = rs[0]["kind"]
    h1, h2, cut = split_half(rs)
    passes = (lambda r, k: r["r"] <= k) if kind == "ceiling" else (lambda r, k: r["r"] >= k)
    best = None
    for k in sorted({r["r"] for r in h1}):
        s = sum(r["usd"] for r in h1 if passes(r, k))
        if best is None or s > best[1]:
            best = (k, s)
    if best is None or best[1] <= 0:
        return {"verdict": "no looser value pays in H1", "cut": cut}
    k = best[0]
    t = [(r["mint"], r["ret"], r["usd"]) for r in h2 if passes(r, k)]
    st = stats(t)
    if not st["n"]:
        verdict = "NOT REPLICATED (H2 has no rows inside k)"
    elif st["sum"] > 0 and st["lo"] > 0:
        verdict = "REPLICATED"
    elif st["sum"] > 0:
        verdict = "DIRECTION HELD"
    else:
        verdict = "NOT REPLICATED"
    return {"k": k, "h1_sum": best[1], "h1_n": sum(1 for r in h1 if passes(r, k)), "h2": st,
            "verdict": verdict, "cut": cut, "kind": kind}


def _sub(trace, name):
    try:
        evs = (json.loads(trace or "{}") or {}).get("substage_events") or []
    except (TypeError, ValueError):
        return None
    return next((e for e in reversed(evs) if e.get("name") == name), None)


def admitted_r(family, a):
    """The admitted fill's own value for a gate, relative to that gate's threshold, or None."""
    if family == "mcap_over_ceiling":
        m = a["facts"].get("effective_mcap")
        return float(m) / 250000.0 if m else None
    if family == "source_buy_floor":
        s = _sub(a["trace"], "source_buy_floor")
        if s and s.get("judged_usd") and s.get("effective_floor_usd"):
            return float(s["judged_usd"]) / float(s["effective_floor_usd"])
        return None
    if family in ("chase_quote", "evm_chase_quote"):
        s = _sub(a["trace"], "chase_quote_confirmation")
        if s and s.get("gap_pct") is not None and s.get("cap_pct"):
            return float(s["gap_pct"]) / float(s["cap_pct"])
        return None
    if family == "roundtrip_cap":
        s = _sub(a["trace"], "roundtrip_cap")
        if s and s.get("cost_pct") is not None and s.get("cap_pct"):
            return float(s["cost_pct"]) / float(s["cap_pct"])
        return None
    if family == "route_over_mid":
        s = _sub(a["trace"], "execution_premium")
        if s and s.get("ratio") and s.get("cap"):
            return float(s["ratio"]) / float(s["cap"])
        return None
    return None


def tighten(family, adm):
    """Best tighter factor k on H1 admitted fills (dollars saved by refusing), tested on H2."""
    kind = PARSERS.get(family, (None,))[0]
    rs = [dict(a, r=admitted_r(family, a)) for a in adm]
    rs = [r for r in rs if r["r"] is not None]
    if kind is None or len(rs) < MIN_N:
        return {"verdict": "not computable (%d admitted fills carry this gate's input)" % len(rs)}
    h1, h2, cut = split_half(rs)
    # A tighter ceiling k < 1 refuses fills with r > k; a tighter floor k > 1 refuses fills with r < k.
    refuses = (lambda r, k: r["r"] > k) if kind == "ceiling" else (lambda r, k: r["r"] < k)
    best = None
    for k in sorted({r["r"] for r in h1}):
        saved = -sum(r["pnl"] for r in h1 if refuses(r, k))
        if best is None or saved > best[1]:
            best = (k, saved)
    if best is None or best[1] <= 0:
        return {"verdict": "no tighter value saves money in H1", "cut": cut}
    k = best[0]
    t = [(r["mint"], -r["ret"], -r["pnl"]) for r in h2 if refuses(r, k)]
    st = stats(t)
    if not st["n"]:
        verdict = "NOT REPLICATED (H2 has no fills beyond k)"
    elif st["sum"] > 0 and st["lo"] > 0:
        verdict = "REPLICATED"
    elif st["sum"] > 0:
        verdict = "DIRECTION HELD"
    else:
        verdict = "NOT REPLICATED"
    return {"k": k, "h1_saved": best[1], "h1_n": sum(1 for r in h1 if refuses(r, k)), "h2_saved": st,
            "verdict": verdict, "cut": cut, "kind": kind}


def report(args):
    with contextlib.closing(ro(LEDGER)) as c:
        pop, n_filled = population(c)
        adm = admitted(c)
    clip_evm = {net: statistics.median([a["notional"] for a in adm if a["net"] == net])
                for net in {a["net"] for a in adm} if net != "solana"}
    with contextlib.closing(ro(CAPTURES)) as cap:
        captured_origins = {(r[0], r[1]) for r in cap.execute(
            "SELECT DISTINCT network_id, origin_event_id FROM memecoin_refusal_captures WHERE refused_at >= ?",
            (START,))}
        for p in pop:
            p["has_capture"] = (p["net"], p["origin"]) in captured_origins
            if p["cat"] != "captured" or not p["has_capture"]:
                continue
            if p["net"] == "solana":
                clip, obs = solana_obs(cap, p["origin"])
            else:
                clip, obs = clip_evm.get(p["net"]), display_obs(cap, p["net"], p["origin"])
            ret, how = grade(obs) if obs and clip else (None, "censored")
            p.update(ret=ret, how=how, clip=clip, usd=(ret * clip if ret is not None else None))
            mr = measured(p["fam"], p["reason"])
            p["kind"], p["r"] = (mr if mr else (None, None))
    print("# Refusal gate money map  %s .. %s" % (START, END))
    print("population: %d origins (origins that later filled dropped: %d filled origins in window)"
          % (len(pop), n_filled))
    print("categories:", dict(collections.Counter(p["cat"] for p in pop)))
    print("EVM clip (median admitted notional):", {k: round(v, 2) for k, v in sorted(clip_evm.items())})

    print("\n## Governance, infrastructure and uncaptured (counted, never graded)")
    for cat in ("governance", "infra", "uncaptured"):
        cnt = collections.Counter((p["fam"], p["net"]) for p in pop if p["cat"] == cat)
        for (f, n), v in cnt.most_common(40 if cat == "uncaptured" else 12):
            print("  %-11s %-40s %-7s %5d  (%.1f/day)" % (cat, f[:40], n, v, v / DAYS))

    print("\n## Captured gate families: per family x chain")
    groups = collections.defaultdict(list)
    for p in pop:
        if p["cat"] == "captured":
            groups[(p["fam"], p["net"])].append(p)
    table = []
    for (f, n), ps in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        graded = [p for p in ps if p.get("ret") is not None]
        how = collections.Counter(p.get("how", "no capture") if p["has_capture"] else "no capture" for p in ps)
        st = stats([(p["mint"], p["ret"], p["usd"]) for p in graded])
        table.append({"fam": f, "net": n, "ps": ps, "graded": graded, "st": st})
        print("  %-26s %-7s refused %4d (%.1f/day) captured %4d graded %4d %s | %s"
              % (f, n, len(ps), len(ps) / DAYS, sum(p["has_capture"] for p in ps), len(graded),
                 dict(how), fmt(st)))

    print("\n## Refused vs admitted, by chain x band (graded refusals of captured gates vs realized fills)")
    for net in sorted({p["net"] for p in pop if p.get("ret") is not None}):
        for band in sorted({p["band"] for p in pop if p["net"] == net and p.get("ret") is not None}):
            rf = [(p["mint"], p["ret"], p["usd"]) for p in pop if p["net"] == net and p["band"] == band
                  and p.get("ret") is not None]
            ad = [(a["mint"], a["ret"], a["pnl"]) for a in adm if a["net"] == net and a["band"] == band]
            print("  %-7s %-12s refused %s || admitted %s" % (net, band, fmt(stats(rf)), fmt(stats(ad))))

    print("\n## SAME-INSTRUMENT CONTROL (post hoc): admitted EVM fills graded on their own DexScreener marks")
    adm_disp = {}
    with contextlib.closing(ro(LEDGER)) as c2:
        for net in sorted({a["net"] for a in adm if a["net"] != "solana"}):
            disp, real = [], []
            for a in (x for x in adm if x["net"] == net):
                ret, how = admitted_display(c2, a)
                if ret is not None:
                    disp.append((a["mint"], ret, ret * a["notional"]))
                    real.append((a["mint"], a["ret"], a["pnl"]))
            adm_disp[net] = disp
            print("  %-6s admitted on display %s || the same fills realized %s" % (
                net, fmt(stats(disp)), fmt(stats(real))))
    print("  EVM refused families vs admitted, BOTH on display (refused minus admitted, 95% CI):")
    for t in table:
        if t["net"] != "solana" and t["st"].get("n", 0) >= MIN_N and len(adm_disp.get(t["net"], [])) >= MIN_N:
            lo, hi = diff_ci([(p["mint"], p["ret"]) for p in t["graded"]],
                             [(m, r) for m, r, _u in adm_disp[t["net"]]])
            d = t["st"]["mean"] - stats(adm_disp[t["net"]])["mean"]
            verdict = "refused WORSE" if hi < 0 else ("refused BETTER" if lo > 0 else "no difference shown")
            print("    %-26s %-6s %+6.1f pts [%+.1f, %+.1f]  %s" % (t["fam"], t["net"], 100 * d, 100 * lo,
                                                                  100 * hi, verdict))

    decided = [t for t in table if t["st"].get("n", 0) >= MIN_N]
    costing = sorted([t for t in decided if t["st"]["lo"] > 0], key=lambda t: -t["st"]["sum"])
    saving = sorted([t for t in decided if t["st"]["hi"] < 0], key=lambda t: t["st"]["sum"])
    undecided = [t for t in decided if t not in costing and t not in saving]
    print("\n## Ranking (graded n >= %d)" % MIN_N)
    print("COSTING money (refused winners, CI > 0): loosen/remove candidates")
    for t in costing:
        print("  %-26s %-7s %s  knob %s" % (t["fam"], t["net"], fmt(t["st"]), KNOBS.get(t["fam"], "?")))
    print("SAVING money (CI < 0): keep/tighten candidates")
    for t in saving:
        print("  %-26s %-7s %s  knob %s" % (t["fam"], t["net"], fmt(t["st"]), KNOBS.get(t["fam"], "?")))
    print("UNDECIDED (CI spans 0)")
    for t in sorted(undecided, key=lambda t: -t["st"]["sum"]):
        print("  %-26s %-7s %s" % (t["fam"], t["net"], fmt(t["st"])))
    print("too few graded (< %d):" % MIN_N, ", ".join("%s/%s n=%d" % (t["fam"], t["net"], t["st"].get("n", 0))
                                                   for t in table if t not in decided))

    print("\n## Knob search, split-half hold-out (H1 fits, H2 tests)")
    for t in costing[:3]:
        res = loosen(t["graded"])
        print("  LOOSEN %-20s %-7s %s" % (t["fam"], t["net"], json.dumps(_round(res))))
    print("  POST HOC (EVM rows above are display-instrument; the Solana exact-route rows are searched too):")
    for t in [x for x in decided if x["net"] == "solana" and x["st"]["sum"] > 0 and x not in costing[:3]]:
        res = loosen(t["graded"])
        print("  LOOSEN %-20s %-7s %s" % (t["fam"], t["net"], json.dumps(_round(res))))
    for t in saving[:3]:
        res = tighten(t["fam"], [a for a in adm if a["net"] == t["net"]])
        print("  TIGHTEN %-19s %-7s %s" % (t["fam"], t["net"], json.dumps(_round(res))))

    print("\n## Capture coverage")
    fams = collections.defaultdict(lambda: [0, 0])
    for p in pop:
        if p["cat"] in ("captured", "uncaptured"):
            fams[(p["fam"], p["net"], p["cat"])][0] += 1
            fams[(p["fam"], p["net"], p["cat"])][1] += p["has_capture"]
    for (f, n, cat), (tot, have) in sorted(fams.items(), key=lambda kv: -kv[1][0]):
        if tot >= 5:
            print("  %-40s %-7s %-10s refused %5d with capture %5d (%.0f%%)" % (f[:40], n, cat, tot, have, 100 * have / tot))


def _round(d):
    if isinstance(d, dict):
        return {k: _round(v) for k, v in d.items()}
    return round(d, 4) if isinstance(d, float) else d


def selftest():
    assert measured("mcap_over_ceiling", "mcap $343,423 over ceiling $250,000 (over_ceiling)") == \
        ("ceiling", 343423 / 250000)
    assert measured("source_buy_floor", "source buy $120 (0.40%) under mcap floor 0.800% ($240)") == \
        ("floor", 0.5)
    assert measured("roundtrip_cap", "round trip 14.2% over 10.0% cap (sell back $96 of $112)")[1] == 1.42
    assert measured("chase_quote", "exact quote is +41% above source price (cap 30%)")[1] == 41 / 30
    assert measured("m5_turnover_under_floor",
                    "m5 organic volume $500 under floor $1000 (m5_turnover_under_floor, revival guard)") == \
        ("floor", 0.5)
    assert measured("reentry_cooldown", "re-entry cooldown: x") is None
    rows = [{"r": 1.0 + i / 10, "kind": "ceiling", "usd": (10 if i < 6 else -10), "ret": 0.1, "mint": "m%d" % i,
             "at": "2026-09-%02dT00:00:00" % (13 + i)} for i in range(12)]
    res = loosen(rows)
    assert res["verdict"].startswith("NOT REPLICATED") or res["verdict"] in ("REPLICATED", "DIRECTION HELD")
    assert classify("refused", "not_approved")[0] == "governance"
    assert classify("aborted", "no quote (no_route: jupiter 400)")[0] == "infra"
    assert classify("refused", "quiet_hours")[0] == "uncaptured"
    assert classify("aborted", "round trip 12.0% over 10.0% cap (sell back $1 of $2)") == ("captured", "roundtrip_cap")
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    return selftest() if a.selftest else report(a)



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('report', 'main'))

if __name__ == "__main__":
    main()
