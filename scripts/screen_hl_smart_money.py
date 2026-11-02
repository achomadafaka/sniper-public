#!/usr/bin/env python3
"""Screen Hyperliquid's live leaderboard through the trader-intelligence analyzer (the
Polymarket wallet pipeline, repurposed). Discover -> persistence+risk-adjusted prefilter ->
fills-deep-screen each candidate -> copyability gate. Prints the copyable cohort and the
rejected-with-reason (the gate is the point: most 'winners' are market-makers or leverage variance).

Read-only unless --persist. One fetch_user_fills path per candidate, with optional retry/throttle.
Not copy advice, not a live trigger.
See docs/superpowers/specs/2026-06-22-hl-trader-intelligence-design.md."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time

from sniper.markets import hyperliquid_api as HL  # noqa: E402
from sniper.hyperlive import trader_intel as TI  # noqa: E402
from sniper.hyperlive import intel_store as IS  # noqa: E402

INTEL_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"

MIN_ACCOUNT_VALUE = 50_000.0     # real traders, not dust
MIN_POSITIVE_WINDOWS = 3         # of {day, week, month, all_time} — persistence, not a one-window fluke


def _fetch_user_fills_with_retries(wallet: str, *, retries: int, sleep_s: float) -> list[dict]:
    attempts = max(0, retries) + 1
    last_err: Exception | None = None
    for attempt in range(attempts):
        try:
            return HL.fetch_user_fills(wallet)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt + 1 >= attempts:
                break
            if sleep_s > 0:
                time.sleep(sleep_s)
    assert last_err is not None
    raise last_err


def _existing_wallets(db_path: str) -> set[str]:
    """Known screened/tracked wallets. Read-only helper for widening screens without refetching."""
    if not db_path or not os.path.exists(db_path):
        return set()
    conn = sqlite3.connect(db_path)
    try:
        out: set[str] = set()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table in ("hl_screened_traders", "hl_tracked_traders"):
            if table not in tables:
                continue
            out.update(r[0] for r in conn.execute(f"SELECT wallet FROM {table}").fetchall() if r[0])
        return out
    finally:
        conn.close()


def _tracked_refresh_rows(db_path: str) -> list[dict]:
    """Tracked archetype-pass wallets to re-screen every run so a good wallet is never culled for
    staleness alone, with last-stored fields so it can be screened even after dropping off the
    leaderboard. Excludes wallets culled *for cause* (negative forward evidence or a verdict
    downgrade) — those were removed deliberately and re-fetching them is wasted load; stale-culled
    wallets ARE included so they revive. Read-only helper."""
    if not db_path or not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "hl_tracked_traders" not in tables:
            return []
        has_screened = "hl_screened_traders" in tables
        acct = "s.account_value" if has_screened else "NULL"
        join = "LEFT JOIN hl_screened_traders s ON s.wallet = t.wallet" if has_screened else ""
        rows = conn.execute(
            f"SELECT t.wallet, t.month_roi, {acct} FROM hl_tracked_traders t {join} "
            "WHERE (t.archetype_pass IN (1, '1') OR t.verdict IN ('watch', 'copyable')) "
            "AND (t.lifecycle_status IS NULL OR t.lifecycle_status != 'culled' "
            "     OR t.lifecycle_reason LIKE '%stale%')").fetchall()
        return [{"wallet": r[0], "_month_roi": r[1] or 0.0, "account_value": r[2]}
                for r in rows if r[0]]
    finally:
        conn.close()


def _prefilter(rows: list[dict]) -> list[dict]:
    """Persistence + risk-adjusted prefilter over leaderboard rows. Rank by month ROI (risk-
    adjusted), not raw PnL (which is leverage/size-distorted)."""
    out = []
    for r in rows:
        av = r.get("account_value") or 0.0
        if av < MIN_ACCOUNT_VALUE:
            continue
        windows = [r.get(w) or {} for w in ("day", "week", "month", "all_time")]
        pos = sum(1 for w in windows if (w.get("pnl") or 0.0) > 0)
        if (r.get("all_time") or {}).get("pnl", 0.0) <= 0 or pos < MIN_POSITIVE_WINDOWS:
            continue
        r["_month_roi"] = (r.get("month") or {}).get("roi") or 0.0
        r["_pos_windows"] = pos
        out.append(r)
    out.sort(key=lambda r: -r["_month_roi"])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Screen HL leaderboard through the copyability analyzer")
    ap.add_argument("--candidates", type=int, default=40, help="deep-screen the top-N prefiltered")
    ap.add_argument("--persist", action="store_true",
                    help="write the copyable cohort + screen run to hl_intel.sqlite (for follow loop + UI)")
    ap.add_argument("--no-maintain-lifecycle", action="store_true",
                    help="persist the screen but skip watch lifecycle/copy-target maintenance")
    ap.add_argument("--intel-db", default=INTEL_DB)
    ap.add_argument("--fill-retries", type=int, default=0,
                    help="retry failed fetch_user_fills calls this many times before rejecting")
    ap.add_argument("--fill-sleep", type=float, default=0.0,
                    help="seconds to sleep before retrying a failed fetch_user_fills call")
    ap.add_argument("--skip-existing", action="store_true",
                    help="skip wallets already in hl_screened_traders or hl_tracked_traders")
    ap.add_argument("--fill-delay", type=float, default=0.0,
                    help="seconds to sleep between candidate fill fetches")
    ap.add_argument("--refresh-tracked", action="store_true",
                    help="also re-screen already-tracked archetype-pass wallets each run so they "
                         "don't age into a staleness cull (bypasses the top-N prefilter cut)")
    ap.add_argument("--max-watch-targets", type=int, default=None,
                    help="cap the follow list to the top-N by quality (default: no cap)")
    args = ap.parse_args(argv)

    rows = HL.fetch_leaderboard()
    prefiltered = _prefilter(rows)
    skipped_existing = 0
    if args.skip_existing:
        existing = _existing_wallets(args.intel_db)
        before = len(prefiltered)
        prefiltered = [r for r in prefiltered if r.get("wallet") not in existing]
        skipped_existing = before - len(prefiltered)
    cand = prefiltered[: args.candidates]
    refreshed = 0
    if args.refresh_tracked:
        have = {r.get("wallet") for r in cand}
        by_wallet = {r.get("wallet"): r for r in rows}
        for tr in _tracked_refresh_rows(args.intel_db):
            w = tr["wallet"]
            if w in have:
                continue
            r = by_wallet.get(w)          # prefer the live leaderboard row (real fields, no prefilter cut)
            if r is not None:
                r["_month_roi"] = (r.get("month") or {}).get("roi") or 0.0
                r.setdefault("_pos_windows", 0)
            else:                         # dropped off the leaderboard — synthesize from stored fields
                r = {"wallet": w, "_month_roi": tr["_month_roi"], "_pos_windows": 0,
                     "account_value": tr["account_value"] or 0.0}
            cand.append(r)
            have.add(w)
            refreshed += 1
    skip_msg = f" | skipped {skipped_existing} existing" if args.skip_existing else ""
    refresh_msg = f" | +{refreshed} tracked refreshed" if args.refresh_tracked else ""
    print(f"leaderboard: {len(rows)} traders | {len(cand)} pass persistence+risk prefilter{skip_msg}{refresh_msg} "
          f"(>=${MIN_ACCOUNT_VALUE:,.0f}, >={MIN_POSITIVE_WINDOWS}/4 windows +). Deep-screening fills...\n")

    verdicts, copyable, all_results = Counter(), [], []
    for i, r in enumerate(cand):
        w = r["wallet"]
        try:
            fills = _fetch_user_fills_with_retries(
                w, retries=args.fill_retries, sleep_s=args.fill_sleep)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i+1}] {w[:10]}… fills error: {type(e).__name__}")
            continue
        finally:
            if args.fill_delay > 0 and i + 1 < len(cand):
                time.sleep(args.fill_delay)
        v = TI.assess_trader(fills)
        verdicts[v["verdict"]] += 1
        all_results.append((r, v))
        if v.get("archetype_pass"):                     # phase-1 directional pass = follow cohort
            copyable.append((r, v))                      # (verdict is 'watch' until phase-2 proves it)
        else:
            print(f"  [{i+1}] {w[:10]}… REJECT {v['verdict']:14} — {v['reason']}")

    print(f"\n=== verdict tally (n={sum(verdicts.values())}) ===")
    for k, n in verdicts.most_common():
        print(f"  {k:16} {n}")
    print(f"\n=== FOLLOW cohort (archetype-pass, unproven until phase-2) ({len(copyable)}) ===")
    for r, v in copyable:
        m = v["metrics"]
        print(f"  {r['wallet']}  acct=${r['account_value']:,.0f}  month_roi={r['_month_roi']:+.1%}  "
              f"taker={v['taker_frac']:.0%}  PF={m['profit_factor']}  WR={m['win_rate']:.0%}  "
              f"n={m['n_closes']}  realized=${m['realized_gross']:,.0f}")
    if not copyable:
        print("  (none — every prefiltered winner is a market-maker, leverage-variance, or "
              "low-sample. This is the gate working, not a bug.)")

    if args.persist:
        conn = IS.connect(args.intel_db)
        IS.ensure_schema(conn)
        now = int(time.time() * 1000)
        for r, v in copyable:
            m = v["metrics"]
            IS.upsert_tracked(conn, r["wallet"], {
                "verdict": v["verdict"], "archetype": v["archetype"], "taker_frac": v["taker_frac"],
                "archetype_pass": v.get("archetype_pass", 0),
                "profit_factor": m["profit_factor"], "win_rate": m["win_rate"], "n_closes": m["n_closes"],
                "month_roi": r["_month_roi"], "realized": m["realized_gross"]}, ts_ms=now)
        for r, v in all_results:                       # full graded list -> analyzer tab
            m = v["metrics"]
            IS.upsert_screened(conn, r["wallet"], {
                "verdict": v["verdict"], "archetype": v["archetype"], "taker_frac": v["taker_frac"],
                "archetype_pass": v.get("archetype_pass", 0),
                "profit_factor": m["profit_factor"], "win_rate": m["win_rate"], "n_closes": m["n_closes"],
                "top_coin_frac": m["top_coin_pnl_frac"], "single_fill_frac": m["single_fill_frac"],
                "month_roi": r["_month_roi"], "realized": m["realized_gross"],
                "account_value": r.get("account_value"), "reason": v["reason"]}, ts_ms=now)
        IS.save_screen_run(conn, {"leaderboard_n": len(rows), "prefiltered_n": len(cand),
                                  "screened_n": sum(verdicts.values()), "verdicts": dict(verdicts)}, ts_ms=now)
        if not args.no_maintain_lifecycle:
            from sniper.hyperlive import watch_lifecycle as WL
            summary = WL.maintain_watchlist(conn, now_ms=now, max_watch_targets=args.max_watch_targets)
            print(f"lifecycle: active_watch={summary['active_watch']} proven={summary['proven']} "
                  f"culled={summary['culled']} selected={len(summary['selected_targets'])}")
        conn.close()
        print(f"\npersisted {len(copyable)} copyable + {len(all_results)} graded traders + screen run -> {args.intel_db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
