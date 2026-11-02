#!/usr/bin/env python3
"""Read the refusal shadow lane. Two questions, two tables. READ-ONLY, writes nothing.

    python3 scripts/research/refusal_shadow_report.py [--days 7] [--min-n 1]

Spec: Alpha, 2026-09-23, HANDOFF.md "REFUSAL SHADOW LANE SPEC".

NEVER HELD answers the override question: a coin we refused and never bought, priced on the tape.
LATER HELD answers the first-refusal question: the modelled PnL at the FIRST refusal against what
the real position on the same mint actually realized.

`held_later` IS COMPUTED HERE, NOT STORED: a funded position on the same (network_id, mint) opened
within 24 h after the refusal. Storing it would freeze a fact that is still changing while the
lane runs.

READ THE LIMITS BLOCK AT THE END BEFORE ANY NUMBER. Every PnL here is optimistic: the fill pays
impact and the modelled exit does not, and a pool that stops trading holds its last price instead
of falling.
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import datetime as dt
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sniper.util.envfiles import load_env_files                          # noqa: E402
from sniper.util.state_paths import state_root, state_path               # noqa: E402
load_env_files(str(state_root()))

from sniper.persistence.db_core import get_readonly_connection           # noqa: E402
from sniper.services import memecoin_refusal_shadow as shadow            # noqa: E402

RATBRAIN_ATTEMPT = "715511ef259344658c3dcf0af10b22d4"


def held_later(ledger, net, mint, decided_at, *, hours=24):
    """The funded position opened on this mint within `hours` after the refusal, or None.

    Uses `opened_recorded_at`, the CONFIRMED FILL (97.0% populated), and falls back to `opened_at`
    only when it is absent. `opened_at` is the DECISION START reused through the whole attempt, so
    reading it as the fill is the mistake that made a pre-fill feature look like a leak.
    """
    end = (dt.datetime.fromisoformat(str(decided_at).replace("Z", "+00:00"))
           + dt.timedelta(hours=hours)).isoformat()
    return ledger.execute(
        "SELECT id, realized_pnl, is_open, exit_reason, "
        "       COALESCE(opened_recorded_at, opened_at) AS filled_at "
        "  FROM memecoin_positions "
        " WHERE mint=? AND network_id=? AND execution_mode='real' "
        "   AND COALESCE(opened_recorded_at, opened_at) >= ? "
        "   AND COALESCE(opened_recorded_at, opened_at) <= ? "
        " ORDER BY COALESCE(opened_recorded_at, opened_at) LIMIT 1",
        (mint, str(net), str(decided_at), end)).fetchone()


def table(rows, *, title, with_real=False):
    print(f"\n### {title}  ({len(rows)} refusals)")
    if not rows:
        print("  no rows")
        return
    groups = {}
    for r in rows:
        groups.setdefault((r["family"], r["network_id"]), []).append(r)
    # THREE STATES, NEVER TWO. A row with no PnL is either sampled-and-still-open (evidence about
    # the COIN) or never sampled (evidence about OUR coverage). One "censored" column would merge
    # a quiet market with a stopped worker.
    # THE MODELLED SIDE COUNTS COINS, BECAUSE THE REAL SIDE DOES (Codex review R7). Several
    # refusals on one coin share one price path and resolve to ONE later position, so counting
    # refusal rows put the two halves of this table in different units. The campaign is the
    # EARLIEST refusal on a (network_id, mint), which is the unit `held_later` keys on and the
    # unit the LATER HELD question asks about. Raw refusal volume keeps its own column.
    head = (f"  {'family':<34} {'chain':<7} {'coins':>5} {'refusals':>8} {'priced':>6} "
            f"{'open':>5} {'unsamp':>6} {'win%':>6} {'mean$':>8} {'sum$':>9}")
    if with_real:
        # `real n` is DISTINCT closed positions, not refusal rows; `open pos` is distinct positions
        # still open, whose realized figure is a PARTIAL and is never summed into `real sum$`.
        # `open pos` goes first so the closed pair stays the last two columns.
        head += f" {'open pos':>8} {'real n':>6} {'real sum$':>10}"
    print(head)
    for key in sorted(groups, key=lambda k: -len(groups[k])):
        refusals = groups[key]
        campaigns = {}
        for r in refusals:
            ck = (r.get("network_id"), r.get("mint"))
            held = campaigns.get(ck)
            if held is None or str(r.get("decision_started_at") or "") < str(
                    held.get("decision_started_at") or ""):
                campaigns[ck] = r
        g = list(campaigns.values())
        priced = [r for r in g if r["pnl_usd"] is not None]
        still_open = [r for r in g if r["pnl_usd"] is None and r["sampled"] == 1]
        unsampled = [r for r in g if r["sampled"] != 1]
        pnls = [r["pnl_usd"] for r in priced]
        win = (100.0 * sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else None
        line = (f"  {key[0]:<34} {key[1]:<7} {len(g):>5} {len(refusals):>8} {len(priced):>6} "
                f"{len(still_open):>5} {len(unsampled):>6} "
                f"{(f'{win:.0f}' if win is not None else '-'):>6} "
                f"{(f'{statistics.fmean(pnls):.2f}' if pnls else '-'):>8} "
                f"{(f'{sum(pnls):.2f}' if pnls else '-'):>9}")
        if with_real:
            # ONE POSITION COUNTS ONCE, and an OPEN position's realized figure is a partial, not
            # a close, so it is reported separately instead of being summed into a close-like
            # total (Codex review R7).
            seen, closed, open_n = set(), [], 0
            for r in g:
                if r.get("real_pnl") is None and not r.get("real_open"):
                    continue
                # Dedupe on the position id when the caller supplied it, else on the coin, which
                # is what `held_later` keys on anyway — so a caller that does not carry the id
                # still cannot count one position twice.
                key = r.get("real_position_id")
                if key is None:
                    key = ("coin", r.get("network_id"), r.get("mint"))
                if key in seen:
                    continue
                seen.add(key)
                if r.get("real_open"):
                    open_n += 1
                elif r.get("real_pnl") is not None:
                    closed.append(r["real_pnl"])
            line += (f" {open_n:>8} {len(closed):>6} "
                     f"{(f'{sum(closed):.2f}' if closed else '-'):>10}")
        print(line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0)
    args = ap.parse_args()

    store_path = str(state_path("data/memecoin_refusal_shadow.db"))
    if not os.path.exists(store_path):
        print(f"no store at {store_path} -- the lane has not run yet. "
              "THIS IS AN ABSENT READ, NOT A ZERO RESULT.")
        return 1
    store = shadow.connect(store_path, readonly=True)
    store.row_factory = __import__("sqlite3").Row
    ledger = get_readonly_connection(str(state_path("sniper_tracker.db")))
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)).isoformat()

    print(f"# Refusal shadow lane -- {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}")
    print(f"window: refusals since {since}")

    print("\n## Census -- what is measured and what is not")
    total = 0
    for r in store.execute(
            "SELECT sampled, COALESCE(sample_reason,'(pending)') reason, COUNT(*) n "
            "  FROM memecoin_refusal_shadow WHERE decision_started_at >= ? "
            " GROUP BY 1,2 ORDER BY 3 DESC", (since,)):
        state = {1: "sampled", 0: "not sampled", -1: "pending"}.get(r["sampled"], r["sampled"])
        print(f"  {state:<12} {r['reason']:<24} {r['n']:>5}")
        total += r["n"]
    print(f"  {'TOTAL':<12} {'':<24} {total:>5}")

    rows = [dict(r) for r in store.execute(
        "SELECT * FROM memecoin_refusal_shadow WHERE decision_started_at >= ?", (since,))]
    for r in rows:
        pos = held_later(ledger, r["network_id"], r["mint"], r["decision_started_at"])
        r["held_later"] = bool(pos)
        r["real_pnl"] = pos["realized_pnl"] if pos else None
        r["real_open"] = bool(pos["is_open"]) if pos else None
        # THE POSITION IDENTITY MUST SURVIVE TO THE AGGREGATE (Codex review R7, 2026-09-23).
        # Several refusals on one coin all resolve to the SAME later position, so without an id
        # the sum counted one close once per refusal: three refusals before one +$10 close
        # reported real_n=3 and +$30.
        r["real_position_id"] = pos["id"] if pos else None

    print("\n## RATBRAIN -- the motivating refusal (spec item 6: this row first)")
    rb = ledger.execute(
        "SELECT attempt_id, mint, network_id, terminal_reason, decision_started_at "
        "  FROM memecoin_entry_attempts WHERE attempt_id=?", (RATBRAIN_ATTEMPT,)).fetchone()
    if not rb:
        print("  attempt not found in the ledger")
    else:
        print(f"  refused {rb['decision_started_at']} on {rb['network_id']}: {rb['terminal_reason']}")
        mine = [r for r in rows if r["mint"] == rb["mint"]]
        if not mine:
            print("  NOT IN THE LANE. The tape retains ~60 minutes, and this refusal predates the "
                  "lane's first pass, so it has no price and never will.")
        for r in mine:
            print(f"  lane row: family={r['family']} sampled={r['sampled']} "
                  f"reason={r['sample_reason']} pnl={r['pnl_usd']}")
        pos = held_later(ledger, rb["network_id"], rb["mint"], rb["decision_started_at"])
        if pos:
            print(f"  we DID enter later: position {pos['id']} filled {pos['filled_at']}, "
                  f"realized {pos['realized_pnl']}, exit {pos['exit_reason']}")

    table([r for r in rows if not r["held_later"]],
          title="NEVER HELD -- should a strong source buy overturn the blocker?")
    table([r for r in rows if r["held_later"]],
          title="LATER HELD -- should we have entered at the FIRST refusal?", with_real=True)

    print("\n## Limits -- these bound every number above")
    print("  1. The fill pays impact; the modelled exit does NOT. Every PnL is optimistic by")
    print("     roughly one clip's impact.")
    print("  2. A pool that stops trading holds its last spot price, so silence reads as flat, not")
    print("     as a fall. That biases AGAINST the stop firing.")
    print("  3. `open` counts rows the policy did not exit inside the horizon: CENSORED, never")
    print("     closed at the last bar. `unsamp` counts rows with no price at all -- that is our")
    print("     own coverage, not the coin's behaviour. A family that is mostly either has no")
    print("     PnL claim at all.")
    print("  4. Solana is not sampled: its tape follows our own positions' pools, so a coin we")
    print("     never bought has no price there. Measured: 0 of 423 refused-and-never-held Solana")
    print("     mints carry a single mark.")
    print("  5. The tape retains ~60 minutes, so nothing before the lane's first pass can ever be")
    print("     priced. This is forward evidence only.")
    print("  6. A price on a pool whose base decimals are unknown is an `atomic_ratio`, not a")
    print("     quote-per-token price. Ratios are unaffected; a stored price is not comparable")
    print("     across pools.")
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    raise SystemExit(main())
