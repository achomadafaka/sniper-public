#!/usr/bin/env python3
"""What an mcap-relative hourly floor would do, band by band, against today's flat dollar floor.

    python3 scripts/research/h1_floor_band_tradeoff.py

Read-only. No network call, no write, no funded-ledger connection that outlives the read.

THE QUESTION. `MEMECOIN_REAL_MIN_VOLUME_H1_USD` is one flat dollar bar for every coin on every
chain. Expressed as the turnover it demands, that bar is not flat at all: $3,000 is 13% of a
$23,000 coin's market cap and 0.15% of a $2,000,000 one. The operator asked which coin bands a
relative floor would actually help, on the explicit assumption that not all of them would.

THE ASYMMETRY THAT MAKES THIS ANSWERABLE AT ALL, and it is the whole method:

  the coins a relative floor would newly REFUSE are coins we HELD, so their outcomes exist and the
  cost of refusing them is measured;

  the coins it would newly ADMIT were refused by the flat floor, so no outcome exists for them at
  any confidence, and no arithmetic here can invent one.

So this script measures one side and refuses to guess the other. A relative floor that is worse on
the measured side is already decided. A relative floor that is free on the measured side is NOT
thereby good -- it is undecided, and the instrument that would decide it is named at the end.

COVERAGE IS PART OF THE RESULT: the admitted side needs classification facts carrying both
`volume_h1` and a market cap. Missing facts and open outcomes remain explicit unknowns.

TRAPS THIS SCRIPT AVOIDS, each paid for elsewhere in this repo:
  - `classification_json.facts.volume_mcap` is a COPY of `effective_mcap`, not a ratio
    (scripts/research/turnover_band_floor_calibration.py). Turnover is computed here, never read.
  - A settings read without dotenv returns the CODE default, which is narrower than production
    (finding-a-config-read-without-dotenv-is-the-code-default-2026-09-22). `.env` is loaded first
    and the floor actually in force is printed.
  - `gate_inputs_json` exists on REFUSED attempts only, so the refused and admitted sides come from
    two different stores and are never pooled into one denominator.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import pathlib
import sqlite3
import statistics
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import sniper.settings.memecoin as cfg  # noqa: E402  (after dotenv, or this reads code defaults)
from sniper.services.memecoin_real_runner import organic_volume  # noqa: E402

# The canonical bands, from `memecoin_market_state._coin_band`. Repeated rather than imported
# because that helper also decides `assumed_nano`/`out_of_scope` from a flag this cohort has not
# got; the boundaries themselves are the contract.
BANDS = (("nano", 0, 50_000), ("micro", 50_000, 250_000), ("small", 250_000, 1_000_000),
         ("mid", 1_000_000, 2_000_000), ("out_of_scope", 2_000_000, float("inf")))
# Candidate relative floors, as a fraction of market cap per hour.
GRID = (0.005, 0.01, 0.02, 0.03, 0.05, 0.10)


def band_of(mcap):
    if mcap is None:
        return "unknown"
    for name, lo, hi in BANDS:
        if lo <= float(mcap) < hi:
            return name
    return "unknown"


def _f(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _organic(row, volume_h1, their):
    try:
        age = (datetime.fromisoformat(row["decision_started_at"].replace("Z", "+00:00"))
               - datetime.fromisoformat(row["source_created_at"].replace("Z", "+00:00"))).total_seconds()
    except (TypeError, ValueError, KeyError):
        age = None
    signal = their if cfg.MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED else 0.0
    return organic_volume(volume_h1, signal, source_age_s=age,
                          index_lag_s=cfg.provider_index_lag_s(row["network_id"]))


def admitted_rows(conn, since):
    """Funded entries with the hourly volume and market cap THE GATE ITSELF saw, plus the outcome.

    ⚠️ THE SOURCE HERE IS `classification_json.facts`, NOT `memecoin_entry_snapshots`. The first
    version of this script read the snapshot's `features_json` and was wrong: on TRIO (position
    56948, 4663, 2026-09-13) the snapshot says `volume_h1` 3.31 and `mcap` 9,612 while the facts the
    gate classified on say 5,082.41 and 33,698.23. The two stores are different reads of different
    pairs, and only the second is the quantity the floor tested. Reading the snapshot put 25 funded
    entries below a $3,000 floor they had in fact cleared, which would have been published as
    "the floor does not bind on coins we take".

    `volume_h1` equals `volume_m5` exactly on 14.8% of these rows. That is not a defect: a coin
    younger than an hour has one window, and both fields carry it.
    """
    out, missing = [], 0
    for row in conn.execute(
            "SELECT a.network_id, a.ticker, a.classification_json, a.source_created_at, "
            "a.decision_started_at, p.id, p.realized_pnl, "
            "p.is_open, p.their_size_usd FROM memecoin_entry_attempts a "
            "JOIN memecoin_positions p ON p.attempt_id = a.attempt_id "
            "WHERE p.execution_mode='real' AND p.opened_at >= ?", (since,)):
        facts = {}
        with contextlib.suppress(ValueError, TypeError):
            facts = json.loads(row["classification_json"] or "{}").get("facts", {}) or {}
        volume_h1 = _f(facts.get("volume_h1"))
        mcap = _f(facts.get("effective_mcap"))
        if volume_h1 is None or not mcap:
            missing += 1
            continue
        their = _f(row["their_size_usd"]) or 0.0
        out.append({"id": row["id"], "chain": row["network_id"], "ticker": row["ticker"],
                    "mcap": mcap, "volume_h1": volume_h1,
                    "organic": _organic(row, volume_h1, their),
                    "pnl": _f(row["realized_pnl"]), "open": bool(row["is_open"]),
                    "band": band_of(mcap)})
    return out, {"no_gate_facts": missing}


def refused_rows(conn, since):
    """Attempts the flat hourly floor refused, with the numbers it refused them on."""
    out = []
    for row in conn.execute(
            "SELECT network_id, ticker, source_created_at, decision_started_at, gate_inputs_json "
            "FROM memecoin_entry_attempts WHERE terminal_status='refused' "
            "AND terminal_reason LIKE '%(under_abs_floor%' AND decision_started_at >= ?", (since,)):
        gate = {}
        with contextlib.suppress(ValueError, TypeError):
            gate = json.loads(row["gate_inputs_json"] or "{}")
        mcap, volume_h1 = _f(gate.get("mcap")), _f(gate.get("volume_h1"))
        if mcap is None or volume_h1 is None:
            continue
        their = _f(gate.get("their_size_usd")) or 0.0
        out.append({"chain": row["network_id"], "ticker": row["ticker"], "mcap": mcap,
                    "volume_h1": volume_h1,
                    "organic": _organic(row, volume_h1, their),
                    "floor": _f(gate.get("required_usd")), "band": band_of(mcap),
                    "at": row["decision_started_at"]})
    return out


def _stat(values):
    if not values:
        return None, None, 0
    return statistics.median(values), sum(values) / len(values), len(values)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "sniper_tracker.db"))
    parser.add_argument("--since", default="2026-08-24")
    args = parser.parse_args()

    flat = float(getattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 0) or 0)
    relative = float(getattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_H1", 0) or 0)
    print(f"floor in force: flat ${flat:,.0f}/hour, relative leg {relative} "
          f"({'OFF' if relative <= 0 else 'on'});  window from {args.since}")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    with contextlib.closing(conn):
        admitted, censored = admitted_rows(conn, args.since)
        refused = refused_rows(conn, args.since)

    print(f"\nADMITTED SIDE (funded entries, outcomes exist): {len(admitted)} usable; "
          f"dropped {censored['no_gate_facts']} funded entries whose attempt carried no gate facts")
    print(f"REFUSED SIDE (flat floor said no, no outcome exists): {len(refused)} with gate inputs")

    print("\n--- what the FLAT floor demands, by band, as turnover of the coins we actually took")
    print(f'{"band":>13} {"n":>4} {"median mcap":>13} {"flat floor is":>14} '
          f'{"median organic":>15} {"median turnover":>16}')
    for name, _lo, _hi in BANDS:
        rows = [r for r in admitted if r["band"] == name]
        if not rows:
            continue
        mcaps = sorted(r["mcap"] for r in rows)
        med_mcap = statistics.median(mcaps)
        med_org = statistics.median([r["organic"] for r in rows])
        med_turn = statistics.median([r["organic"] / r["mcap"] for r in rows])
        print(f"{name:>13} {len(rows):>4} ${med_mcap:>12,.0f} {flat / med_mcap:>13.2%} "
              f"${med_org:>14,.0f} {med_turn:>15.2%}")

    print("\n--- WHO MOVES. For each candidate relative floor, per band:")
    print("      refuse: funded entries it would have REFUSED (outcome known, cost measured)")
    print("      admit : flat-floor refusals it would have ADMITTED (outcome unknown, always)")
    for ratio in GRID:
        print(f"\n  relative floor {ratio:.3%} of mcap per hour   "
              f"(equals the ${flat:,.0f} flat bar at mcap ${flat / ratio:,.0f})")
        print(f'      {"band":>13} {"held n":>7} {"would refuse":>13} {"closed n":>8} '
              f'{"their pnl":>12} {"win%":>7} {"refused n":>10} {"would admit":>12}')
        for name, _lo, _hi in BANDS:
            held = [r for r in admitted if r["band"] == name]
            gone = [r for r in held if r["organic"] < ratio * r["mcap"]]
            closed = [r["pnl"] for r in gone if not r["open"] and r["pnl"] is not None]
            blocked = [r for r in refused if r["band"] == name]
            freed = [r for r in blocked if r["organic"] >= ratio * r["mcap"]]
            if not held and not blocked:
                continue
            pnl = f"${sum(closed):,.2f}" if closed else "unknown"
            wins = sum(1 for p in closed if p > 0)
            win = f"{100 * wins / len(closed):.0f}%" if closed else "unknown"
            print(f"      {name:>13} {len(held):>7} {len(gone):>13} {len(closed):>8} {pnl:>12} "
                  f'{win:>7} '
                  f"{len(blocked):>10} {len(freed):>12}")

    print("\n--- the refused population, so the admit column is readable")
    print(f'{"band":>13} {"n":>4} {"median mcap":>13} {"median organic":>15} {"median turnover":>16}')
    for name, _lo, _hi in BANDS:
        rows = [r for r in refused if r["band"] == name]
        if not rows:
            continue
        print(f"{name:>13} {len(rows):>4} "
              f'${statistics.median([r["mcap"] for r in rows]):>12,.0f} '
              f'${statistics.median([r["organic"] for r in rows]):>14,.0f} '
              f'{statistics.median([r["organic"] / r["mcap"] for r in rows]):>15.2%}')

    print("\nWHAT THIS CANNOT SAY. Every 'would admit' cell is a count of coins with no outcome at "
          "any confidence: the flat floor has been $3,000 for the whole recorded window, so the "
          "region below it holds no funded fill on any chain and the decision region is EMPTY, not "
          "merely thin. `under_abs_floor` is also absent from `memecoin_refusal_capture.GATES`, so "
          "no forward route series exists for these coins either. Grading the admit side needs that "
          "capture family added first; until then a relative floor can only be justified where it "
          "REFUSES nothing we measured and the operator accepts an unmeasured admit side.")


if __name__ == "__main__":
    main()
