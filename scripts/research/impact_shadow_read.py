#!/usr/bin/env python3
"""Read the impact-cap shadow: would OUR round trip have admitted it, and what happened after?

Two gates feed it since 2026-09-28 (Alpha): `impact_cap` refusals (would our round trip admit them?)
and `roundtrip_cap` refusals, which already passed the impact cap and carry the entry's own funded
measurement (`entry_rt_cost_pct`) beside the shadow's.

Pre-registered with Alpha on 2026-09-28 (operator order: "make a good blocker"). One row per Solana
impact-cap refusal from memecoin_impact_shadow (data/memecoin_refusal_captures.db), joined to:

  outcome   the refusal capture of the SAME origin: the exact clip's tokens sold back at the
            capture's horizons, as a return on the clip (a paper outcome, sell side, exact route);
  depth     the attempt's own dex_pair_selection at decision time: the pool's quote-side depth;
  source    the source's buy size (memecoin_events.their_size_usd, else the on-chain detection);
  ratio     source / quote-side depth, and a constant-product estimate of the source's own price
            move, ((1 + s/q)^2 - 1), labelled an ESTIMATE (no receipt is read here).

Columns side by side, per Alpha: our round trip (and its fixed/size split) against the source-vs-depth
ratio, so the n>=20 read can say whether the round trip alone refuses "a big source buy into a thin
pool", or whether that ratio predicts the dumps on its own. Read-only on both databases.

    python3 scripts/research/impact_shadow_read.py
    python3 scripts/research/impact_shadow_read.py --selftest
"""
import argparse
import contextlib
import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LEDGER = REPO / "sniper_tracker.db"
CAPTURES = REPO / "data" / "memecoin_refusal_captures.db"
HORIZONS = (30, 60, 300)


def cpmm_source_move_pct(source_usd, quote_side_usd):
    """Constant-product price move from a buy of `source_usd` into `quote_side_usd`: ESTIMATE."""
    if not source_usd or not quote_side_usd or quote_side_usd <= 0:
        return None
    return ((1.0 + float(source_usd) / float(quote_side_usd)) ** 2 - 1.0) * 100.0


def outcome_at(sells, clip_usd, horizon_s, tol_s=20.0):
    """Return on the clip of the sell observation nearest `horizon_s` (within tol), else None."""
    near = [(abs(age - horizon_s), usd) for age, usd in sells if abs(age - horizon_s) <= tol_s]
    if not near or not clip_usd:
        return None
    return (min(near)[1] / float(clip_usd) - 1.0) * 100.0


def ro(path):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def rows(ledger, captures):
    out = []
    if not captures.execute("SELECT 1 FROM sqlite_master WHERE name='memecoin_impact_shadow'").fetchone():
        return out                         # no impact refusal since the shadow shipped
    for s in captures.execute("SELECT * FROM memecoin_impact_shadow ORDER BY refused_at"):
        s = dict(s)
        sells = [(r["age_s"], int(r["quote_output_atomic"]) / 1e6) for r in captures.execute(
            "SELECT age_s, quote_output_atomic FROM memecoin_refusal_captures WHERE origin_event_id=? "
            # USDC atoms (1e6) only on Solana; an EVM exact capture's sells are WETH/WBNB atoms.
            "AND network_id='solana' "
            "AND quote_side='sell' AND quote_output_atomic IS NOT NULL", (s["origin_event_id"],))]
        buy = captures.execute("SELECT quote_input_usd FROM memecoin_refusal_captures WHERE "
                               "origin_event_id=? AND network_id='solana' AND quote_side='buy' LIMIT 1",
                               (s["origin_event_id"],)).fetchone()
        clip = buy[0] if buy else s["clip_usd"]
        a = ledger.execute("SELECT source_handle, trace_json FROM memecoin_entry_attempts WHERE attempt_id=?",
                           (s["attempt_id"],)).fetchone()
        quote_side = None
        if a:
            for e in json.loads(a["trace_json"] or "{}").get("substage_events") or []:
                if e.get("name") == "dex_pair_selection":
                    quote_side = ((e.get("liquidity_pick") or {}).get("quote_side_usd"))
        ev = ledger.execute("SELECT their_size_usd FROM memecoin_events WHERE fomo_event_id=?",
                            (s["origin_event_id"],)).fetchone()
        size = ev[0] if ev and ev[0] else None
        if size is None and str(s["origin_event_id"] or "").startswith("onchain:"):
            d = ledger.execute("SELECT source_size_usd FROM onchain_wallet_detections WHERE signature=?",
                               (s["origin_event_id"].split(":", 1)[1],)).fetchone()
            size = d[0] if d else None
        out.append({**s, "handle": a["source_handle"] if a else None, "quote_side_usd": quote_side,
                    "source_size_usd": size,
                    "source_ratio": (size / quote_side if size and quote_side else None),
                    "source_move_est_pct": cpmm_source_move_pct(size, quote_side),
                    **{"out_%ds" % h: outcome_at(sells, clip, h) for h in HORIZONS}})
    return out


def fmt(x, spec="%+.1f"):
    return "-" if x is None else spec % x


def report(args):
    with contextlib.closing(ro(args.ledger)) as ledger, contextlib.closing(ro(args.captures)) as cap:
        data = rows(ledger, cap)
    print("impact-cap shadow rows: %d" % len(data))
    print("%-4s %-19s %-15s %6s | %6s %6s %6s %6s %5s %4s | %7s %6s %5s %6s | %6s %6s %6s" % (
        "gate", "refused_at", "handle", "jupimp", "entrt", "rt", "probe", "size", "rtcap", "pass",
        "src$", "qside", "ratio", "srcmv", "out30", "out60", "out300"))
    for r in data:
        print("%-4s %-19s %-15s %6s | %6s %6s %6s %6s %5s %4s | %7s %6s %5s %6s | %6s %6s %6s" % (
            {"impact_cap": "imp", "roundtrip_cap": "rt"}.get(r.get("gate") or "impact_cap", "?"),
            str(r["refused_at"])[:19], str(r["handle"] or "?")[:15], fmt(r["jupiter_impact_pct"], "%.1f"),
            fmt(r.get("entry_rt_cost_pct"), "%.1f"), fmt(r["rt_cost_pct"], "%.1f"), fmt(r["probe_rt_cost_pct"], "%.1f"), fmt(r["size_cost_pct"], "%.1f"),
            fmt(r["roundtrip_cap_pct"], "%.0f"), {1: "yes", 0: "no"}.get(r["would_pass_roundtrip"], "?"),
            fmt(r["source_size_usd"], "%.0f"), fmt(r["quote_side_usd"], "%.0f"),
            fmt(r["source_ratio"], "%.2f"), fmt(r["source_move_est_pct"], "%.0f"),
            fmt(r["out_30s"]), fmt(r["out_60s"]), fmt(r["out_300s"])))
    gate = lambda r: r.get("gate") or "impact_cap"   # noqa: E731 (rows before the gate column)
    for name, pick in (("impact refused, rt would ADMIT", lambda r: gate(r) == "impact_cap" and r["would_pass_roundtrip"] == 1),
                       ("impact refused, rt would REFUSE", lambda r: gate(r) == "impact_cap" and r["would_pass_roundtrip"] == 0),
                       ("round-trip refused", lambda r: gate(r) == "roundtrip_cap"),
                       ("source ratio >= 0.05", lambda r: (r["source_ratio"] or 0) >= 0.05),
                       ("source ratio < 0.05", lambda r: r["source_ratio"] is not None and r["source_ratio"] < 0.05)):
        sub = [r["out_60s"] for r in data if pick(r) and r["out_60s"] is not None]
        print("  %-32s n=%-3d +60 s mean %s  win %s" % (
            name, len(sub), fmt(sum(sub) / len(sub)) if sub else "-",
            "%d/%d" % (sum(1 for x in sub if x > 0), len(sub)) if sub else "-"))


def selftest():
    assert abs(cpmm_source_move_pct(643, 7183) - 18.74) < 0.05
    assert cpmm_source_move_pct(None, 7183) is None and cpmm_source_move_pct(643, 0) is None
    sells = [(28.5, 65.16), (52.8, 55.75), (79.0, 60.93)]
    assert abs(outcome_at(sells, 102.23, 30) - (65.16 / 102.23 - 1) * 100) < 1e-9
    assert abs(outcome_at(sells, 102.23, 60) - (55.75 / 102.23 - 1) * 100) < 1e-9
    assert outcome_at(sells, 102.23, 300) is None
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--ledger", default=str(LEDGER))
    ap.add_argument("--captures", default=str(CAPTURES))
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    report(a)


if __name__ == "__main__":
    main()
