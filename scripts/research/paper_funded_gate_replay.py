#!/usr/bin/env python3
"""Replay the live funded ENTRY gates over the paper book, so paper answers a funded question.

WHY. The paper lane and the funded lane do NOT admit the same signals. `memecoin_copy_trader`
opens a paper arm after a chase cap, a dump guard and a duplicate check, and nothing else — no
market-cap ceiling, no volume floor, no turnover band, no source-size floor, and no wallet
approval. So a paper PnL is the PnL of "copy everything this wallet did", which is not any
strategy we run. The funded rails ARE mirrored on the exit side (`funded_clone` freezes the
funded policy resolved at entry), so the exits are comparable and only the ADMISSION is not.

This closes that gap without touching either lane: every paper position is re-judged against the
live entry knobs using the market features already stored at its own decision instant, and the
book is reported as the funded-equivalent subset next to the part funded would have refused.

WHERE THE INPUTS COME FROM. `memecoin_entry_snapshots` is written by the paper open path itself
(`_log_entry_snapshot`) and keyed on `upstream_event_id`, which is the position's
`origin_event_id`. Coverage is exact on the current book: 2,657 of 2,657 `funded_clone` positions
over 7 days carry a snapshot. Its `features_json` holds the DexScreener read of that moment —
mcap, liquidity, volume_h1, volume_m5, txns, pair age — so no provider call is made here and no
value is re-fetched at a later price.

⚠️ KNOBS ARE READ THROUGH `memecoin_entry_knobs.effective()`, NOT FROM `.env`. The cockpit overlay
outranks the file and three knobs are overridden right now, including the source-buy floor at
$197 against the file's $200 and the follow age at 60 s against the file's 86,400. A replay off
`.env` would silently judge with thresholds the runner is not using.

⚠️ AN UNKNOWN INPUT FAILS OPEN, exactly as the live gate does, and is COUNTED SEPARATELY. A gate
that cannot see a number does not fire (that is how RAYGUN was admitted on 2026-09-23 with a null
h1 volume), so "would pass" contains coins that passed only because a provider was silent. The
`unknown` column is that population, and a verdict that moves when it is excluded is not a verdict.

WHAT THIS DOES NOT REPLAY, so nobody reads the pass set as "funded would have bought it": wallet
approval and chain arming, the kill switch, drawdown and concurrency state, re-entry and cooldown
rules, route/quote/impact gates (they need a live route quote at that instant, which is not
stored), and the per-wallet exemption overlay. Those are state and execution, not judgements about
the coin.

    python3 scripts/research/paper_funded_gate_replay.py --days 7
    python3 scripts/research/paper_funded_gate_replay.py --days 7 --arm-state disarmed --by-wallet
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import contextlib
import datetime as dt
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sniper.persistence.db_core import get_readonly_connection  # noqa: E402
from sniper.util.envfiles import load_env_files  # noqa: E402


def knobs():
    """Live effective entry knobs: the cockpit overlay on top of `.env`, never `.env` alone."""
    load_env_files(str(ROOT))
    import sniper.settings.memecoin as cfg
    from sniper.services import memecoin_entry_knobs as K
    with contextlib.closing(get_readonly_connection(str(ROOT / "sniper_tracker.db"))) as conn:
        eff = K.effective(conn, cfg)
    out = {n: v[0] if isinstance(v, tuple) else v for n, v in eff.items()}
    out["_sources"] = {n: (v[1] if isinstance(v, tuple) else "default") for n, v in eff.items()}
    out["_by_wallet_mcap"] = str(getattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", "") or "")
    out["_turnover_band_max_mcap"] = float(
        getattr(cfg, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", 0.0) or 0.0)
    return out


def wallet_mcap_ceiling(raw: str, network_id, source_user_id, default_ceiling):
    """Per-wallet ceiling from `chain:uid=value,...`, else the global one."""
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, val = part.partition("=")
        chain, _, uid = key.partition(":")
        if uid and uid == str(source_user_id) and chain.strip() == str(network_id):
            try:
                return float(val)
            except ValueError:
                continue
    return default_ceiling


def num(v):
    """A float, or None. A missing number is UNKNOWN and must never become 0.0."""
    if v is None or v is True or v is False:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def judge(feat, k, *, network_id, source_user_id):
    """(verdict, gate, unknowns) for one position's stored features under the live knobs.

    verdict: 'pass' or 'refuse'. `gate` names the FIRST gate that refuses, in the runner's own
    order of cheapness. `unknowns` lists the gates that could not decide and so failed open.
    """
    unknown = []
    mcap = num(feat.get("mcap")) or num(feat.get("market_cap")) or num(feat.get("their_market_cap"))
    ceiling = wallet_mcap_ceiling(k["_by_wallet_mcap"], network_id, source_user_id,
                                  float(k.get("MEMECOIN_REAL_MAX_MCAP_USD") or 0.0))
    floor_mcap = float(k.get("MEMECOIN_REAL_MIN_MCAP_USD") or 0.0)
    if mcap is None:
        unknown.append("mcap")
    else:
        if ceiling > 0 and mcap > ceiling:
            return "refuse", "mcap over ceiling", unknown
        if floor_mcap > 0 and mcap < floor_mcap:
            return "refuse", "mcap under floor", unknown

    # Source buy floor: the larger of the absolute floor and a share of market cap. An unknown
    # market cap has its own, lower floor rather than skipping the gate.
    size = num(feat.get("their_size_usd"))
    abs_floor = float(k.get("MEMECOIN_REAL_MIN_SOURCE_BUY_USD") or 0.0)
    ratio = float(k.get("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO") or 0.0)
    unknown_floor = float(k.get("MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD") or 0.0)
    if size is None:
        unknown.append("source_size")
    else:
        need = abs_floor if mcap is not None else max(abs_floor, unknown_floor)
        if mcap is not None and ratio > 0:
            need = max(need, ratio * mcap)
        if need > 0 and size < need:
            return "refuse", "source buy under floor", unknown

    v_h1 = num(feat.get("volume_h1"))
    h1_floor = float(k.get("MEMECOIN_REAL_MIN_VOLUME_H1_USD") or 0.0)
    if v_h1 is None:
        unknown.append("volume_h1")
    elif h1_floor > 0 and v_h1 < h1_floor:
        return "refuse", "h1 volume under floor", unknown

    buys, sells = num(feat.get("txns_m5_buys")), num(feat.get("txns_m5_sells"))
    min_txns = float(k.get("MEMECOIN_REAL_MIN_TXNS_M5") or 0)
    if buys is None and sells is None:
        unknown.append("txns_m5")
    elif min_txns > 0 and (buys or 0) + (sells or 0) < min_txns:
        return "refuse", "m5 trades under floor", unknown

    # Turnover is m5 volume as a share of market cap, with a wider floor above the band.
    v_m5 = num(feat.get("volume_m5"))
    if v_m5 is None or mcap is None or mcap <= 0:
        unknown.append("turnover_m5")
    else:
        turn = v_m5 / mcap
        band_max = k["_turnover_band_max_mcap"]
        in_band = band_max <= 0 or mcap <= band_max
        lo = float(k.get("MEMECOIN_REAL_MIN_TURNOVER_M5") or 0.0) if in_band else \
            float(k.get("MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND") or 0.0)
        hi = float(k.get("MEMECOIN_REAL_MAX_TURNOVER_M5") or 0.0)
        if lo > 0 and turn < lo:
            return "refuse", "m5 turnover under floor", unknown
        if hi > 0 and turn > hi:
            return "refuse", "m5 turnover over ceiling", unknown

    lag = num(feat.get("lag_s"))
    max_age = float(k.get("MEMECOIN_REAL_FOLLOW_MAX_AGE_S") or 0.0)
    if lag is None:
        unknown.append("signal_age")
    elif max_age > 0 and lag > max_age:
        return "refuse", "signal older than max age", unknown

    return "pass", None, unknown


def stats(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return dict(n=0, total=0.0, mean=0.0, median=0.0, win=0.0, trimmed=0.0)
    top = sum(v[-5:])
    return dict(n=len(v), total=sum(v), mean=sum(v) / len(v), median=statistics.median(v),
                win=100.0 * sum(1 for x in v if x > 0) / len(v),
                trimmed=(sum(v) - top))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--arm", default="funded_clone", help="paper strategy arm to replay")
    ap.add_argument("--arm-state", choices=("all", "armed", "disarmed"), default="all")
    ap.add_argument("--by-wallet", action="store_true")
    ap.add_argument("--exclude-unknown", action="store_true",
                    help="drop positions where any gate failed open on a missing number")
    args = ap.parse_args()
    k = knobs()
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)).isoformat()
    sql = (
        "SELECT p.id, p.network_id, p.source_user_id, p.realized_pnl, p.is_open, "
        "       p.exit_reason, w.handle, COALESCE(w.real_approved,0) armed, s.features_json "
        "  FROM memecoin_positions p "
        "  JOIN memecoin_entry_snapshots s ON s.upstream_event_id = p.origin_event_id "
        "  LEFT JOIN memecoin_wallets w ON w.source_user_id = p.source_user_id "
        " WHERE p.execution_mode='paper' AND p.strategy=? AND p.opened_at > ?")
    rows = []
    with contextlib.closing(get_readonly_connection(str(ROOT / "sniper_tracker.db"))) as conn:
        conn.row_factory = None
        raw = conn.execute(sql, (args.arm, since)).fetchall()
        total_positions = conn.execute(
            "SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='paper' "
            "AND strategy=? AND opened_at>?", (args.arm, since)).fetchone()[0]
    for pid, net, uid, pnl, is_open, exit_reason, handle, armed, fj in raw:
        if args.arm_state == "armed" and not armed:
            continue
        if args.arm_state == "disarmed" and armed:
            continue
        try:
            feat = json.loads(fj) if fj else {}
        except (TypeError, ValueError):
            feat = {}
        verdict, gate, unknown = judge(feat, k, network_id=net, source_user_id=uid)
        if args.exclude_unknown and unknown:
            continue
        rows.append(dict(pid=pid, net=net, uid=uid, pnl=pnl, is_open=is_open, handle=handle,
                         armed=armed, verdict=verdict, gate=gate, unknown=unknown,
                         exit_reason=exit_reason))
    ov = [n for n, src in k["_sources"].items() if src != "default"]
    print(f"paper arm {args.arm}, last {args.days:g} days, arm-state {args.arm_state}")
    print(f"  {len(raw)} of {total_positions} positions carry an entry snapshot; "
          f"{len(rows)} judged")
    print(f"  knobs: ceiling ${k.get('MEMECOIN_REAL_MAX_MCAP_USD'):,.0f}, "
          f"h1 volume floor ${k.get('MEMECOIN_REAL_MIN_VOLUME_H1_USD'):,.0f}, "
          f"source floor ${k.get('MEMECOIN_REAL_MIN_SOURCE_BUY_USD'):,.0f} "
          f"or {k.get('MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO')} of mcap, "
          f"m5 turnover {k.get('MEMECOIN_REAL_MIN_TURNOVER_M5')}-{k.get('MEMECOIN_REAL_MAX_TURNOVER_M5')}, "
          f"signal age {k.get('MEMECOIN_REAL_FOLLOW_MAX_AGE_S')}s")
    print(f"  live overrides (NOT .env): {', '.join(sorted(ov)) if ov else '(none)'}")

    closed = [r for r in rows if not r["is_open"] and r["pnl"] is not None]
    print(f"\n{'group':34} {'n':>5} {'total$':>10} {'mean$':>8} {'median$':>8} {'win%':>6} "
          f"{'without top5$':>13}")
    for label, sel in (("would PASS the funded gates", lambda r: r["verdict"] == "pass"),
                       ("would be REFUSED", lambda r: r["verdict"] == "refuse")):
        for state, keep in (("armed", lambda r: r["armed"]), ("disarmed", lambda r: not r["armed"])):
            if args.arm_state not in ("all", state):
                continue
            s = stats([r["pnl"] for r in closed if sel(r) and keep(r)])
            if not s["n"]:
                continue
            print(f"{label + ' / ' + state:34} {s['n']:>5} {s['total']:>+10.2f} {s['mean']:>+8.2f} "
                  f"{s['median']:>+8.2f} {s['win']:>6.1f} {s['trimmed']:>+13.2f}")

    print(f"\nrefusing gate, closed positions only:")
    gates = {}
    for r in closed:
        if r["verdict"] == "refuse":
            g = gates.setdefault(r["gate"], [])
            g.append(r["pnl"])
    for g, v in sorted(gates.items(), key=lambda kv: -len(kv[1])):
        s = stats(v)
        print(f"  {g:32} n={s['n']:>5} total ${s['total']:>+9.2f} mean ${s['mean']:>+7.2f} "
              f"median ${s['median']:>+7.2f} win {s['win']:.0f}%")

    unk = {}
    for r in closed:
        for u in r["unknown"]:
            unk.setdefault(u, []).append(r["pnl"])
    print(f"\ngates that FAILED OPEN on a missing number (a pass here is not a pass):")
    for u, v in sorted(unk.items(), key=lambda kv: -len(kv[1])):
        s = stats(v)
        print(f"  {u:32} n={s['n']:>5} total ${s['total']:>+9.2f} mean ${s['mean']:>+7.2f}")
    clean = [r for r in closed if not r["unknown"]]
    s_pass = stats([r["pnl"] for r in clean if r["verdict"] == "pass"])
    s_ref = stats([r["pnl"] for r in clean if r["verdict"] == "refuse"])
    print(f"  every number present: pass n={s_pass['n']} ${s_pass['total']:+.2f} "
          f"(without top5 ${s_pass['trimmed']:+.2f}) | refuse n={s_ref['n']} ${s_ref['total']:+.2f}")

    if args.by_wallet:
        print(f"\nby wallet, PASS side only, closed:")
        per = {}
        for r in closed:
            if r["verdict"] == "pass":
                per.setdefault((r["handle"], r["armed"]), []).append(r["pnl"])
        for (h, armed), v in sorted(per.items(), key=lambda kv: -sum(kv[1])):
            s = stats(v)
            if s["n"] < 5:
                continue
            print(f"  {str(h)[:22]:<22} {'armed' if armed else 'disarmed':<9} n={s['n']:>4} "
                  f"total ${s['total']:>+9.2f} median ${s['median']:>+7.2f} win {s['win']:.0f}% "
                  f"without top5 ${s['trimmed']:>+9.2f}")
    return 0


def selftest() -> int:
    """The judge's edges: a ceiling, a ratio floor, a fail-open, and a per-wallet override."""
    k = {"MEMECOIN_REAL_MAX_MCAP_USD": 250000.0, "MEMECOIN_REAL_MIN_MCAP_USD": 0.0,
         "MEMECOIN_REAL_MIN_SOURCE_BUY_USD": 197.0, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO": 0.008,
         "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD": 100.0,
         "MEMECOIN_REAL_MIN_VOLUME_H1_USD": 3000.0, "MEMECOIN_REAL_MIN_TXNS_M5": 2,
         "MEMECOIN_REAL_MIN_TURNOVER_M5": 0.005, "MEMECOIN_REAL_MAX_TURNOVER_M5": 1.5,
         "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND": 0.03,
         "MEMECOIN_REAL_FOLLOW_MAX_AGE_S": 60.0,
         "_by_wallet_mcap": "solana:UID9=1000000", "_turnover_band_max_mcap": 2000000.0,
         "_sources": {}}
    ok = {"mcap": 100000.0, "their_size_usd": 900.0, "volume_h1": 50000.0, "volume_m5": 900.0,
          "txns_m5_buys": 5, "txns_m5_sells": 4, "lag_s": 3.0}
    assert judge(ok, k, network_id="solana", source_user_id="U1")[:2] == ("pass", None)
    over = {**ok, "mcap": 400000.0, "volume_m5": 3000.0}
    assert judge(over, k, network_id="solana", source_user_id="U1")[1] == "mcap over ceiling"
    # the same coin passes for the wallet whose override raises the ceiling — with a source buy
    # big enough for the ratio floor, which scales with market cap and would otherwise refuse it
    over_big = {**over, "their_size_usd": 4000.0}
    assert judge(over_big, k, network_id="solana", source_user_id="UID9")[0] == "pass", \
        judge(over_big, k, network_id="solana", source_user_id="UID9")
    assert judge(over_big, k, network_id="solana", source_user_id="U1")[1] == "mcap over ceiling"
    # $900 clears the $197 absolute floor but not 0.8% of a $200k cap
    ratio = {**ok, "mcap": 200000.0, "their_size_usd": 900.0}
    assert judge(ratio, k, network_id="solana", source_user_id="U1")[1] == "source buy under floor"
    # a missing h1 volume FAILS OPEN and is reported, exactly like the live gate
    blind = {**ok}
    blind.pop("volume_h1")
    v, g, unk = judge(blind, k, network_id="solana", source_user_id="U1")
    assert (v, g) == ("pass", None) and "volume_h1" in unk, (v, g, unk)
    # a measured zero is NOT unknown: it refuses
    zero = {**ok, "volume_h1": 0.0}
    assert judge(zero, k, network_id="solana", source_user_id="U1")[1] == "h1 volume under floor"
    # the 60 s follow-age override bites a 90 s old signal
    stale = {**ok, "lag_s": 90.0}
    assert judge(stale, k, network_id="solana", source_user_id="U1")[1] == "signal older than max age"
    print("selftest ok: ceiling, per-wallet override, ratio floor, fail-open, measured zero, age")
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    raise SystemExit(main())
