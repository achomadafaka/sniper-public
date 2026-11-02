#!/usr/bin/env python3
"""The `skipped_quote_<reason>@<host>` baseline — the input to the promotion study's kill rule 2.

    docs/superpowers/specs/2026-08-28-wallet-promotion-prereg.md §9

Registered so the read cannot silently have no input. Read-only; it decides nothing.

KILL RULE 2 IS A SHARE, NOT A COUNT. "Stop if the residual `rate_limited` share exceeds 10% of
quote attempts" needs a DENOMINATOR, and a label counter alone is a numerator. A failure-only log
gives a count and never a proportion — the same shape that hid 40% host downtime for two weeks. So
this reports attempts, not just skips, and every share is printed with the denominator it used.

AND IT REPORTS ITS OWN ATTRIBUTABILITY FIRST, because that decides whether the rule can be read at
all. A skip labelled `skipped_no_executable_quote` carries no reason and no host: it is a failure
the split could not attribute, and if most skips land there the rate-limited share is a lower bound
on an unknown, not a measurement. Printing a confident 0.3% over a 99% unattributed population
would be the worst available outcome.
"""
from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# The action a skip carries when the split could NOT name a cause.
UNATTRIBUTED = "skipped_no_executable_quote"
ATTRIBUTED_PREFIX = "skipped_quote_"
# Below this, kill rule 2 is not readable: the share of a labelled minority says nothing about the
# population. Not a tuning knob — a statement about when the number stops meaning anything.
MIN_ATTRIBUTABLE = 0.50


# Kill rule 2 is about JUPITER rate-limiting, and Jupiter quotes Solana. An attempt on an EVM
# chain cannot be rate-limited by Jupiter, so counting it in the denominator dilutes the share of
# the thing the rule is about. Measured 2026-08-29: 295 of 298 unattributed skips were EVM, and
# chains 56, 8453 and 1 had 203 attempts and ZERO opens — the paper lane cannot quote them at all.
# Reading the rule over all chains gave 29.3% "unpriceable" when Solana's own skip rate was 0.6%.
JUPITER_CHAIN = "solana"


def rows(conn, since, chain=None):
    sql = ("SELECT action, COUNT(*) n FROM memecoin_entry_snapshots "
           "WHERE decided_at > ? AND action IS NOT NULL")
    args = [since]
    if chain is not None:
        sql += " AND chain = ?"
        args.append(chain)
    return conn.execute(sql + " GROUP BY action", args).fetchall()


def chain_breakdown(conn, since):
    return conn.execute(
        "SELECT chain, COUNT(*) n, SUM(CASE WHEN action='opened' THEN 1 ELSE 0 END) opened "
        "FROM memecoin_entry_snapshots WHERE decided_at > ? GROUP BY chain ORDER BY n DESC",
        (since,)).fetchall()


def summarize(counts):
    """counts: {action: n}. Returns the shape the read needs, with every denominator explicit."""
    attempts = sum(counts.values())
    opened = counts.get("opened", 0)
    unattributed = counts.get(UNATTRIBUTED, 0)
    by_reason, by_host = collections.Counter(), collections.Counter()
    attributed = 0
    for action, n in counts.items():
        if not action.startswith(ATTRIBUTED_PREFIX):
            continue
        attributed += n
        label = action[len(ATTRIBUTED_PREFIX):]
        reason, _, host = label.partition("@")
        by_reason[reason] += n
        by_host[host or "(no host)"] += n
    skips = attributed + unattributed
    return {
        "attempts": attempts, "opened": opened, "skips": skips,
        "attributed": attributed, "unattributed": unattributed,
        "attributable_share": (attributed / skips) if skips else None,
        "by_reason": dict(by_reason), "by_host": dict(by_host),
        # Kill rule 2's actual quantity, over the denominator it names.
        "rate_limited": by_reason.get("rate_limited", 0),
        "rate_limited_share": (by_reason.get("rate_limited", 0) / attempts) if attempts else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-27T00:00:00+00:00")
    a = ap.parse_args()
    conn = sqlite3.connect("file:%s?mode=ro" % (REPO / "sniper_tracker.db"), uri=True)
    conn.row_factory = sqlite3.Row

    print("paper quote baseline since %s" % a.since)
    print("\n  BY CHAIN — a chain with attempts and no opens cannot be quoted at all, and its")
    print("  skips are not evidence about Jupiter capacity:")
    for r in chain_breakdown(conn, a.since):
        note = "  <- never opens; not a Jupiter capacity signal" if not r["opened"] else ""
        print("    chain %-8s attempts %5d  opened %5d%s"
              % (str(r["chain"]), r["n"], r["opened"] or 0, note))

    print("\n  === KILL RULE 2, on the chain Jupiter actually quotes (%s) ===" % JUPITER_CHAIN)
    s = summarize({r["action"]: r["n"] for r in rows(conn, a.since, chain=JUPITER_CHAIN)})
    print("  attempts (entry snapshots)  %d" % s["attempts"])
    print("  opened                      %d" % s["opened"])
    print("  skipped on a quote          %d" % s["skips"])
    if not s["skips"]:
        print("  no skips in the window; kill rule 2 has nothing to read yet")
        return 0

    share = s["attributable_share"]
    print("\n  ATTRIBUTABLE %d/%d = %.1f%%" % (s["attributed"], s["skips"], 100.0 * share))
    if share < MIN_ATTRIBUTABLE:
        print("  *** KILL RULE 2 IS NOT READABLE AT THIS ATTRIBUTION ***")
        print("  %d of %d skips carry no reason and no host. The rate-limited share below is a"
              % (s["unattributed"], s["skips"]))
        print("  LOWER BOUND on an unknown, not a measurement — the unattributed skips could be")
        print("  any mixture. Fix the attribution before reading the rule, not after.")

    print("\n  by reason:")
    for reason, n in sorted(s["by_reason"].items(), key=lambda kv: -kv[1]):
        print("    %-20s %5d  (%.1f%% of attempts)" % (reason, n, 100.0 * n / s["attempts"]))
    if s["unattributed"]:
        print("    %-20s %5d  (%.1f%% of attempts)  <- cause unknown"
              % ("(unattributed)", s["unattributed"], 100.0 * s["unattributed"] / s["attempts"]))
    print("\n  by host:")
    for host, n in sorted(s["by_host"].items(), key=lambda kv: -kv[1]):
        print("    %-20s %5d" % (host, n))

    print("\n  KILL RULE 2: rate_limited %d / %d attempts = %.2f%%  (threshold 10%%)"
          % (s["rate_limited"], s["attempts"], 100.0 * (s["rate_limited_share"] or 0.0)))
    print("  %s" % ("READ IT AS A LOWER BOUND — see the attribution warning above."
                    if share < MIN_ATTRIBUTABLE else "Attribution is sufficient to read this."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
