"""Alpha overnight sweep (operator order 2026-10-06): new armed-wallet refusals that are NOT a policy
decision (bad measurement / system failure), and every fill that landed outside the source slot or
the next one (fill-timing gap >= 2, or gap unknown). Read-only. Prints one line per case; a watermark
file keeps each case to one report."""
import json, os, re, sqlite3, sys
from datetime import datetime, timezone

ROOT = "/home/bigpapi/Desktop/Sniper"
STATE = os.path.join(ROOT, "data", "alpha_overnight_sweep.state.json")  # mutable watermark, gitignored
START = "2026-10-06T21:00:00+00:00"

# Deliberate policy outcomes: the system measured correctly and said no.
POLICY = re.compile("|".join([
    r"over.?ceiling", r"under absolute floor", r"under mcap floor", r"above first-seen",
    r"one position per coin", r"re-entry cooldown", r"exact quote is .* above source",
    r"route quote is .* the source price", r"^not_approved$", r"^post_peak_confirmed$",
    r"trial_unmarked", r"scaled_clip", r"launchpad-gated", r"j7_shadow: pre-entry",
    r"below min-trade floor", r"copy premium", r"above source fill \(cap", r"chase", r"^skipped_", r"unroutable_chain",
    # The price cap enforced on chain: the coin ran past our limit before landing (2026-10-07 Degen).
    r"SlippageBelowMin", r"slippage tolerance exceeded",
]), re.I)
J7_LANES = {"j7_shadow", "j7_wallets"}


def _slot_lane_policy_skip(trace):
    """Under FAST_LANE_ONLY a coin the slot lane declined on policy ends as
    native_fast_lane_unavailable; the real reason is the slot_lane_skip substage."""
    try:
        subs = json.loads(trace or "{}").get("substage_events") or []
    except ValueError:
        return False
    return any(e.get("name") == "slot_lane_skip" and POLICY.search(str(e.get("reason") or ""))
               for e in subs)


def main():
    st = {"attempts": START, "timing": START, "seen": {}}
    out = []
    try:
        with open(STATE) as handle:
            st.update(json.load(handle))
    except FileNotFoundError:
        pass                                   # the first sweep
    except Exception as exc:                   # never shown as a clean sweep (Codex CORE-03)
        out.append("STATE UNREADABLE %s: %s; sweeping from %s" % (STATE, type(exc).__name__, START))
    now = datetime.now(timezone.utc).isoformat()
    # id -> completion time. A pre-2026-10-07 list state has no times: keep it one more window.
    seen = (dict(st["seen"]) if isinstance(st["seen"], dict)
            else {aid: st["attempts"] for aid in st["seen"]})
    # Rows can be stamped earlier than when they become visible: re-read a 2 h lookback and dedupe.
    back = lambda iso: max(START, (datetime.fromisoformat(iso) - __import__("datetime").timedelta(hours=2)).isoformat())
    main_db = sqlite3.connect(f"file:{ROOT}/sniper_tracker.db?mode=ro", uri=True, timeout=30)
    armed = {}
    for uid, chains in main_db.execute(
            "SELECT source_user_id, real_chains FROM memecoin_wallets WHERE real_approved=1"):
        armed[uid] = set((chains or "").split(","))
    for (aid, net, uid, handle, ticker, lane, status, reason, at, trace) in main_db.execute(
            "SELECT attempt_id, network_id, source_user_id, source_handle, ticker, delivery_lane,"
            " terminal_status, terminal_reason, completed_at, trace_json FROM memecoin_entry_attempts"
            " WHERE completed_at > ? AND completed_at <= ? ORDER BY completed_at",
            (back(st["attempts"]), now)):
        if aid in seen:
            continue
        if status in ("filled", "shadow") or str(net) not in armed.get(uid, ()):
            continue
        reason = reason or status or ""
        if POLICY.search(reason):
            continue
        if lane in J7_LANES and reason == "native_fast_lane_unavailable":
            continue
        if reason == "native_fast_lane_unavailable" and _slot_lane_policy_skip(trace):
            continue
        seen[aid] = at
        out.append("REFUSAL %s %s %s %s lane=%s status=%s reason=%s ref=%s" % (
            at[11:19], net, handle, ticker or "?", lane, status, reason[:110], aid[:8]))
    ft = sqlite3.connect(f"file:{ROOT}/data/memecoin_fill_timing.db?mode=ro", uri=True, timeout=10)
    for (aid, net, lane, gap, cause, detail, done) in ft.execute(
            "SELECT attempt_id, network_id, lane, gap, cause, cause_detail, completed_at"
            " FROM memecoin_fill_timing WHERE completed_at > ? AND completed_at <= ?"
            " AND (gap IS NULL OR gap >= 2) ORDER BY completed_at", (back(st["timing"]), now)):
        if "t:" + aid in seen:
            continue
        seen["t:" + aid] = done or now
        if cause == "manual":            # an operator entry has no source slot to be late against
            continue
        out.append("LATE_FILL %s %s lane=%s gap=%s cause=%s detail=%s ref=%s" % (
            (done or "")[11:19], net, lane, gap, cause, (detail or "")[:110], aid[:8]))
    st["attempts"] = st["timing"] = now
    # FORGET BY TIME, NOT BY SPELLING (Codex CORE-03): `sorted(seen)[-5000:]` kept the lexically
    # greatest ids, so a new attempt could be dropped at once and re-reported. The next run re-reads
    # from back(now); anything completed before that can never be read again.
    horizon = back(now)
    st["seen"] = {aid: at for aid, at in seen.items() if str(at) > horizon}
    tmp = STATE + ".%d.tmp" % os.getpid()
    with open(tmp, "w") as handle:
        json.dump(st, handle)
    os.replace(tmp, STATE)
    print("\n".join(out) if out else "CLEAN since last sweep")


if __name__ == "__main__":
    sys.exit(main())
