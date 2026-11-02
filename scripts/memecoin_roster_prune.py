#!/usr/bin/env python3
"""Retire roster wallets that are on no J7 tracked list.

WHY. J7 caps each account at 50 tracked wallets and relays trades for those only. fomo banned the
account, and the global stream ended on 2026-09-11: 1,411 signalling wallets on 09-09, 49 on 09-11.
So a `memecoin_wallets` row absent from every tracked list can never produce a signal again, and
it sits in the roster looking like coverage we do not have.

WHAT RETIRE MEANS. `remove_wallet`: paused=1, excluded=1, real_approved=0, real_chains=NULL, gate
exemptions and chase caps cleared. IT NEVER DELETES. The identity and every event, attempt and
position stay, so the history of a wallet that is later re-tracked is intact.

TWO REFUSALS THAT MATTER MORE THAN THE FEATURE. The prune stops when the tracked set is EMPTY, and
when its newest `seen_at` is older than the freshness bound. Both describe the same accident:
retiring the roster because a socket was down rather than because J7 stopped tracking anyone. An
empty set is indistinguishable from "J7 tracks nobody", and acting on it would retire all of them.

    python3 scripts/memecoin_roster_prune.py --dry-run
    python3 scripts/memecoin_roster_prune.py --confirm      # the operator runs this, nobody else
"""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REASON = "not on J7 lists 2026-09-22"
ACTOR = "operator"
DEFAULT_MAX_AGE_MIN = 15.0


def _parse(stamp):
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def freshness_error(seen_at, now, *, max_age_min):
    """The reason to refuse, or None. Unparseable is a refusal: an unreadable stamp is not fresh."""
    if not seen_at:
        return "the J7 tracked table is empty — no list has been stored yet"
    stamp = _parse(seen_at)
    if stamp is None:
        return "the newest J7 tracked stamp %r does not parse" % (seen_at,)
    age_min = (now - stamp).total_seconds() / 60.0
    if age_min > float(max_age_min):
        return ("the newest J7 tracked list is %.1f minutes old (bound %.0f) — a dark socket "
                "must not retire the roster" % (age_min, max_age_min))
    if age_min < -5.0:
        return "the newest J7 tracked stamp is %.1f minutes in the FUTURE" % (-age_min,)
    return None


def report(rows):
    """Counts first, then the handles. The counts are what an operator checks before confirming."""
    approved = [r for r in rows if r.get("real_approved")]
    paused = [r for r in rows if r.get("paused")]
    excluded = [r for r in rows if r.get("excluded")]
    lines = ["%d roster wallets are on no J7 tracked list" % len(rows),
             "  real_approved=1 : %d" % len(approved),
             "  paused=1        : %d" % len(paused),
             "  excluded=1      : %d  (already retired; the retire is idempotent)" % len(excluded)]
    if approved:
        lines.append("  APPROVED wallets that would lose funded authority:")
        for r in approved:
            lines.append("    %-28s %s" % (r.get("handle") or "(no handle)", r["source_user_id"]))
    others = [r for r in rows if not r.get("real_approved")]
    if others:
        lines.append("  other roster wallets:")
        for r in others:
            lines.append("    %-28s %s" % (r.get("handle") or "(no handle)", r["source_user_id"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="list what would be retired")
    group.add_argument("--confirm", action="store_true", help="retire them (operator only)")
    ap.add_argument("--max-age-min", type=float, default=DEFAULT_MAX_AGE_MIN,
                    help="refuse when the newest tracked list is older than this")
    args = ap.parse_args(argv)

    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root, configure_memecoin_state_env
    load_env_files(str(state_root()))
    configure_memecoin_state_env()
    from sniper.persistence.db_core import get_connection
    from sniper.persistence import db_memecoin as m
    from sniper.util.clock import real_utc_now_iso

    try:
        from sniper.feeds.j7_feed import configured_tokens, account_fingerprint
        fingerprints = [account_fingerprint(token) for token in configured_tokens()]
    except (OSError, ValueError, RuntimeError) as exc:
        print("REFUSED: J7 account inventory unavailable — %s" % exc)
        return 2

    conn = get_connection()
    try:
        if not m.j7_tracked_identity_matches(conn, fingerprints):
            print("REFUSED: stored J7 lists do not match every configured account and kind")
            return 2
        seen_at = m.j7_tracked_seen_at(conn)
        tracked = m.j7_tracked_ids(conn)
        now = datetime.now(timezone.utc)
        print("J7 tracked wallets stored: %d, newest list seen at %s" % (len(tracked), seen_at))

        incomplete = m.j7_tracked_incomplete(conn)
        if incomplete:
            detail = ", ".join(
                ("no per-kind counts recorded yet — the runner storing these lists predates "
                 "them, so completeness cannot be verified"
                 if r["account_index"] is None else
                 "account %s %s: feed said %s, stored %s" % (
                     r["account_index"], r["kind"], r["reported_count"],
                     "nothing (unreadable)" if r["stored_count"] is None else r["stored_count"]))
                for r in incomplete)
            print("REFUSED: the tracked set is incomplete — %s" % detail)
            print("         Every wallet of an unreadable kind would look untracked. "
                  "Fix the feed parse before pruning.")
            return 2

        error = freshness_error(seen_at, now, max_age_min=args.max_age_min)
        if error and args.confirm:
            print("REFUSED: %s" % error)
            return 2
        if error:
            print("NOTE: %s" % error)
            print("      --confirm would refuse. Showing nothing, because an empty or stale "
                  "tracked set cannot tell an untracked wallet from an unread socket.")
            return 0

        rows = m.wallets_not_tracked(conn)
        print(report(rows))
        if not args.confirm:
            print("\ndry run: nothing changed. Re-run with --confirm to retire these.")
            return 0
        if not rows:
            return 0

        retired = 0
        for row in rows:
            uid = row["source_user_id"]
            try:
                if m.remove_wallet(conn, uid):
                    m.record_wallet_audit(conn, uid, "retire", actor=ACTOR, reason=REASON,
                                          now_iso=real_utc_now_iso())
                    conn.commit()
                    retired += 1
            except Exception as exc:          # one bad row must not strand the rest
                print("  FAILED %s: %s" % (uid, exc))
        print("\nretired %d of %d, actor=%s reason=%r" % (retired, len(rows), ACTOR, REASON))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
