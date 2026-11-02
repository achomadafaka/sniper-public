#!/usr/bin/env python3
"""analyze_fomo_accounts.py — per-trader raw analytics from fomo swap ledgers.

Descriptive stats only (no score, no verdict). fomo returns only the newest <=100
swaps per trader, so every profitability figure is over a recent, often-partial
window — read `coverage_ratio` / `unmatched_sell_ratio` alongside it.

Usage:
  python3 scripts/analyze_fomo_accounts.py --all
  python3 scripts/analyze_fomo_accounts.py --uid <id>
  python3 scripts/analyze_fomo_accounts.py --handle connerb
  python3 scripts/analyze_fomo_accounts.py --ids u1,u2,u3 --sort win_rate
"""
import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sniper.persistence.db_core import get_connection
from sniper.persistence import db_memecoin as m
from sniper.feeds.fomo_feed import FomoAuth, fomo_get
from sniper.analysis.fomo_account.analyzer import analyze_account
from sniper.settings import memecoin as cfg

# columns shown first in the printed table — coverage next to PnL so the
# recent-window / truncation caveat is always visible when ranking.
_TABLE_COLS = ["handle", "n_closed", "realized_pnl_usd", "dollar_weighted_return_pct",
               "win_rate", "profit_factor", "top3_win_share", "median_pnl_pct",
               "median_hold_s", "avg_buy_usd", "large_buys_7d", "swaps_7d",
               "last_swap_age_h", "coverage_ratio", "unmatched_sell_ratio",
               "closed_coverage", "hit_full_history"]


def resolve_targets(conn, uids=None, handles=None, all_curated=False, candidates=0):
    if candidates:
        # discovery funnel: screen the top-N non-curated large-buyers by feed notional
        return [(r["source_user_id"], r.get("handle"))
                for r in m.list_candidates(conn, limit=candidates)]
    if all_curated:
        return [(w["source_user_id"], w.get("handle")) for w in m.list_wallets(conn)]
    out = []
    for uid in (uids or []):
        out.append((uid, None))
    for h in (handles or []):
        w = m.get_wallet_by_handle(conn, h)
        out.append((w["source_user_id"], w.get("handle")) if w else (h, h))
    return out


def _is_429(e):
    return getattr(e, "code", None) == 429 or "429" in str(e)


def run(conn, auth, targets, *, now_iso, sleep_s, fetch=None, analyze=None,
        max_retries=4, sleep_fn=time.sleep):
    """Analyze each target, persisting as we go. fomo shares a WAF budget with the
    live runner, so 429s are expected — back off exponentially and retry that
    trader rather than dropping it. One genuinely-bad trader never kills the batch."""
    analyze = analyze or analyze_account
    rows = []
    for i, (uid, _handle) in enumerate(targets):
        for attempt in range(max_retries):
            try:
                row = analyze(auth, uid, now_iso=now_iso, fetch=fetch)
                m.upsert_trader_analytics(conn, row)
                rows.append(row)
                break
            except Exception as e:                   # noqa: BLE001
                if _is_429(e) and attempt < max_retries - 1:
                    back = sleep_s * (2 ** attempt) + 2.0
                    print(f"  · {uid}: 429, backing off {back:.0f}s "
                          f"(attempt {attempt + 1}/{max_retries})", file=sys.stderr)
                    sleep_fn(back)
                    continue
                print(f"  ! {uid}: {type(e).__name__}: {e}", file=sys.stderr)
                break
        if sleep_s and i < len(targets) - 1:
            sleep_fn(sleep_s)
    return rows


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.0f}" if abs(v) >= 100 else f"{v:.2f}"
    return str(v)


def _print_table(rows, sort_key):
    rows = sorted(rows, key=lambda r: (r.get(sort_key) is None, -(r.get(sort_key) or 0)))
    print("  ".join(c[:13].ljust(13) for c in _TABLE_COLS))
    for r in rows:
        print("  ".join(_fmt(r.get(c))[:13].ljust(13) for c in _TABLE_COLS))


def _dump_csv(rows, path):
    if not rows:
        return
    keys = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    # Load env INSIDE main (not at import): a module-level .fomo.env loader silently polluted
    # os.environ with the operator's real config whenever a test imported this CLI, leaking e.g.
    # MEMECOIN_TG_ALERTS=1 into unrelated tests via a settings reload.
    from sniper.util.envfiles import load_env_files
    load_env_files(_ROOT)
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--candidates", type=int, default=0,
                    help="screen the top-N discovery candidates (non-curated large buyers)")
    ap.add_argument("--uid")
    ap.add_argument("--handle")
    ap.add_argument("--ids")
    ap.add_argument("--sort", default="realized_pnl_usd")
    ap.add_argument("--sleep", type=float, default=cfg.MEMECOIN_ANALYZER_SLEEP_S)
    args = ap.parse_args()

    conn = get_connection()
    m.ensure_memecoin_tables(conn)
    uids = [args.uid] if args.uid else (args.ids.split(",") if args.ids else [])
    handles = [args.handle] if args.handle else []
    targets = resolve_targets(conn, uids=uids, handles=handles, all_curated=args.all,
                              candidates=args.candidates)
    if not targets:
        print("no targets — pass --all / --candidates N / --uid / --handle / --ids", file=sys.stderr)
        return 2

    auth = FomoAuth()
    try:
        auth.refresh_token()
    except Exception as e:                            # noqa: BLE001
        print(f"auth refresh warning: {e}", file=sys.stderr)

    from sniper.util.clock import real_utc_now_iso
    now_iso = real_utc_now_iso()   # corrected UTC — host clock runs +7h; ages diff vs fomo times
    print(f"analyzing {len(targets)} trader(s) — newest-<=100-swaps window each ...")
    rows = run(conn, auth, targets, now_iso=now_iso, sleep_s=args.sleep, fetch=fomo_get)
    _print_table(rows, args.sort)
    os.makedirs(os.path.join(_ROOT, "reports"), exist_ok=True)
    out = os.path.join(_ROOT, "reports", f"fomo_trader_analytics_{now_iso[:10]}.csv")
    _dump_csv(rows, out)
    print(f"\nwrote {len(rows)} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
