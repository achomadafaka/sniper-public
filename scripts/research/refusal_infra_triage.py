#!/usr/bin/env python3
"""Every funded entry that did not fill, split into MARKET/POLICY (a correct refusal) and INFRA
(our own plumbing failed, or could not fill where we should have), per chain and lane.

Operator order 2026-09-26 (overnight): watch fills and refusals and fix every refusal caused by
infrastructure. This is the triage instrument, read every ~30 minutes. Read-only: one SELECT on
memecoin_entry_attempts through a read-only connection; no RPC.

The denominator is our own attempts. Roster refusals (wallet not armed, chain not armed) are
excluded: they describe the roster, not a fill we should have had. A reason no rule knows is
printed as UNCLASSIFIED so a new failure mode cannot hide inside a bucket.

    python3 scripts/research/refusal_infra_triage.py --since 2026-09-26T21:00:00Z
    python3 scripts/research/refusal_infra_triage.py --selftest
"""
import argparse
import collections
import contextlib
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "sniper_tracker.db"

ROSTER = re.compile(r"not_approved|unsupported_or_unarmed_network|not armed|disarmed", re.I)
# Order matters: the first match wins. INFRA patterns that could also read as policy come first.
RULES = [
    # First on purpose (2026-09-27): the label carries an arbitrary base58 program prefix, which
    # can spell a later rule's bare pattern (lock_wait's `busy`, for one).
    ("POLICY", "coin_policy", r"^launchpad-gated: "),
    ("INFRA", "source_size_unknown", r"^source buy unknown under"),
    ("INFRA", "funding", r"insufficient native gas|insufficient quote balance|native balance read"
                         r"|below min-trade floor|insufficient (usdc|balance)"),
    # A deploy trips the switch for ~15-30 s (runtime-deploy); --deploy-windows says which trips
    # were deploys. An operator trip would read the same here.
    # The EVM unwrap/wrap pre-transaction ahead of the swap (2026-09-28, 4158 on 4663: reverted,
    # held the in-flight gate 11 min). Ahead of route_build, whose `sign failed` would take one.
    ("INFRA", "evm_pre_transaction", r"^native pre-transaction ambiguous|^pre-tx "),
    ("INFRA", "deploy_window", r"kill_switch_tripped|rejected kill_switch"),
    ("INFRA", "lane_landing_late", r"slot_window_expired|custom program error: 0x1771"),
    ("INFRA", "stale_inputs", r"leg_stale|stale_balances|stale_sol_usd|stale input|quote too old"),
    ("INFRA", "provider_read", r"not visible yet|read failed|could not read|unverifiable|rpc_unknown"
                               r"|deadline|timeout|timed out|\b429\b|rate.?limit|transport"
                               # a failed balance read, e.g. the 12:33Z Wi-Fi outage (2026-09-27)
                               r"|balance unavailable"),
    # Raydium router 0x26 is its minimum-out check (2026-09-27): a moved price, ahead of route_build.
    ("MARKET", "market_moved", r"price moved past min-out"),
    # pump curve error: the curve cannot sell the requested amount (complete or drained), a coin state.
    ("MARKET", "curve_exhausted", r"NotEnoughTokensToBuy"),
    ("INFRA", "route_build", r"route build refused|simulation refused|sign failed|broadcast_rejected"
                             r"|simulation failed|preflight"),
    ("INFRA", "no_route", r"no route|no_route|quoteunavailable|no planned-quantity sell route"
                          r"|route_unavailable|could not quote|no quote"),
    # Our own serialization gate, ahead of receipt_booking whose bare `unresolved` would take it
    # (2026-09-28): memecoin_real_leg refuses EVERY entry on a network while any signed entry there
    # is unresolved. First seen as INFRA on d0d3e813 (00:23:44Z), J7 157 ms behind the slot lane's
    # in-flight buy of the same source transaction: the one-path guard working, not a fault.
    ("POLICY", "inflight_signed_serialization", r"^unresolved signed transaction on "),
    ("INFRA", "receipt_booking", r"anomaly|receipt_unreadable|unresolved|booking"),
    # `busy` only as its own token: SQLITE_BUSY matches, a base58 id that spells it does not.
    ("INFRA", "lock_wait", r"database is locked|lock wait|(?<![A-Za-z0-9])busy(?![A-Za-z0-9])"),
    ("MARKET", "market_moved", r"buyslippagebelowmintokensout|sell_sim_min_out_repeated|slippage"),
    ("POLICY", "source_buy_floor", r"^source buy "),
    ("POLICY", "mcap_ceiling", r"^mcap .* over ceiling|over_ceiling"),
    ("POLICY", "chase_cap", r"above source price|x the source price|x the chart mid|chase|execution_premium"
                            r"|ran .*x|already ran|post.peak"),
    ("POLICY", "capacity", r"max concurrent positions|exposure|bankroll"),
    ("POLICY", "daily_loss_latch", r"daily loss limit"),
    ("POLICY", "duplicate", r"one position per coin|re-entry cooldown|already held"),
    ("POLICY", "roundtrip_cap", r"round trip .* over|route_over_budget"),
    ("POLICY", "impact_cap", r"price impact .* over"),
    ("POLICY", "volume_floors", r"volume|turnover|sell share|txns|h1 floor|revival"),
    ("POLICY", "coin_policy", r"coin is |coin band|too young|coin_too_young|token_safety|honeypot|tax"),
    ("POLICY", "operator_policy", r"quiet_hours|quiet hours|kill switch|real_disabled|drawdown pause"
                                  r"|manual|operator"),
    ("POLICY", "pool_unknown", r"pool_unknown"),
    ("POLICY", "unsupported_chain", r"unroutable_chain"),
    ("POLICY", "stale_signal", r"entry older than"),
    # c88bc6eb: the lane refuses a regime-scaled clip before reserving and the confirmed watcher
    # makes the one ordinary attempt at the scaled clip. A handoff, not a failure.
    ("POLICY", "lane_scaled_clip_handoff", r"slot_lane_scaled_clip"),
]
_COMPILED = [(k, f, re.compile(p, re.I)) for k, f, p in RULES]


def classify(reason):
    text = str(reason or "")
    if ROSTER.search(text):
        return "ROSTER", "roster"
    for kind, family, rx in _COMPILED:
        if rx.search(text):
            return kind, family
    return "UNCLASSIFIED", "unclassified"


def read_rows(since, until=None):
    with contextlib.closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)) as c:
        sql = ("SELECT attempt_id, network_id, COALESCE(delivery_lane, signal_source) AS lane, "
               "decision_started_at, source_handle, mint, terminal_status, terminal_reason "
               "FROM memecoin_entry_attempts WHERE decision_started_at >= ?")
        args = [since]
        if until:
            sql += " AND decision_started_at < ?"
            args.append(until)
        return c.execute(sql + " ORDER BY decision_started_at", args).fetchall()


def report(rows):
    filled = sum(1 for r in rows if r[6] == "filled")
    unfinished = sum(1 for r in rows if r[6] is None)
    per_lane = collections.Counter()
    infra = collections.defaultdict(list)
    policy = collections.Counter()
    unclassified = []
    for r in rows:
        if r[6] in ("filled", None):
            continue
        kind, family = classify(r[7])
        if kind == "ROSTER":
            continue
        per_lane[(r[1], r[2], kind)] += 1
        if kind == "INFRA":
            infra[family].append(r)
        elif kind == "UNCLASSIFIED":
            unclassified.append(r)
        else:
            policy[(kind, family)] += 1
    print(f"attempts {len(rows)}  filled {filled}  unfinished {unfinished}")
    for (net, lane, kind), n in sorted(per_lane.items()):
        print(f"  {net:<7} {lane:<36} {kind:<12} {n}")
    print("MARKET/POLICY:", ", ".join(f"{f} {n}" for (k, f), n in policy.most_common()))
    for family, items in sorted(infra.items(), key=lambda kv: -len(kv[1])):
        ex = items[-1]
        print(f"INFRA {family}: {len(items)}  e.g. {ex[0][:8]} {ex[1]} {ex[2]} {str(ex[3])[11:19]} "
              f"{ex[4]} {str(ex[5])[:10]} | {str(ex[7])[:140]}")
    for r in unclassified:
        print(f"UNCLASSIFIED {r[0][:8]} {r[1]} {r[2]} {str(r[3])[11:19]} {r[6]} | {str(r[7])[:160]}")


AUDIT = REPO / "logs" / "kill_switch_audit.log"
RUNNER_LOG = REPO / "logs" / "memecoin_runner.log"


def deploy_windows(since, audit_lines=None, runner_lines=None):
    """[(trip, stop, armed, clear)] for each runtime-deploy kill-switch trip since `since`.

    Funded entries stand down at the TRIP; the old runner stops (SIGTERM); the new one is ARMED;
    the deploy CLEARs the switch. A signal inside [trip, max(armed, clear)] is either refused
    kill_switch_tripped or, while no runner runs, never seen at all (J7 does not replay)."""
    audit_lines = audit_lines if audit_lines is not None else AUDIT.read_text().splitlines()
    runner_lines = runner_lines if runner_lines is not None else [
        line for line in RUNNER_LOG.read_text(errors="replace").splitlines()
        if "received SIGTERM" in line or "REAL leg ARMED" in line]
    trips, out = [], []
    for line in audit_lines:
        stamp = line[:32]
        if stamp < since:
            continue
        if " TRIP source=runtime-deploy" in line:
            trips.append([stamp, None])
        elif " CLEAR" in line and "CLEAR_REJECTED" not in line and trips and trips[-1][1] is None:
            trips[-1][1] = stamp
    # Bounded by the NEXT trip (2026-09-28): a deploy refused before the stop (08:02:48, cleared 67 ms
    # later on a non-flat book) has no stop of its own, and taking the next deploy's stop and ARMED
    # line read it as 199.8 s of stand-down with that deploy's detections inside it.
    for i, (trip, clear) in enumerate(trips):
        nxt = trips[i + 1][0] if i + 1 < len(trips) else "9999"
        stop = next((l[:32] for l in runner_lines if trip <= l[:32] < nxt and "SIGTERM" in l), None)
        armed = next((l[:32] for l in runner_lines if trip <= l[:32] < nxt and "ARMED" in l), None)
        out.append((trip, stop, armed, clear))
    return out


def _secs(a, b):
    from datetime import datetime
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    except (TypeError, ValueError):
        return None


def report_deploy_windows(since):
    """Per deploy: how long entries stood down, and every armed-wallet on-chain detection inside the
    window that no lane ever attempted, with the size/mcap gates it would face (approximate: a pump
    mint's mcap is price x 1e9; price, route and volume gates cannot be replayed without a quote)."""
    windows = deploy_windows(since)
    with contextlib.closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)) as c:
        armed = {r[0] for r in c.execute(
            "SELECT source_user_id FROM memecoin_wallets WHERE real_approved=1 "
            "AND COALESCE(real_chains,'') LIKE '%solana%'")}
        total = 0.0
        for trip, stop, armed_at, clear in windows:
            end = max(x for x in (armed_at, clear, trip) if x)
            down = _secs(trip, end) or 0.0
            total += down
            refused = c.execute(
                "SELECT COUNT(*) FROM memecoin_entry_attempts WHERE terminal_reason='kill_switch_tripped' "
                "AND decision_started_at BETWEEN ? AND ?", (trip, end)).fetchone()[0]
            dets = c.execute(
                "SELECT signature, source_user_id, handle, mint, detected_at, source_size_usd, source_price "
                "FROM onchain_wallet_detections WHERE network_id='solana' AND detected_at BETWEEN ? AND ?",
                (trip, end)).fetchall()      # the gate replay and the armed set below are Solana-only
            print(f"deploy trip {trip[11:19]} stop {str(stop)[11:19]} armed {str(armed_at)[11:19]} "
                  f"clear {str(clear)[11:19]}: entries down {down:.1f}s, kill_switch refusals {refused}, "
                  f"on-chain detections {len(dets)}")
            for sig, uid, handle, mint, at, size, price in dets:
                # Bounds built in Python: SQLite datetime() renders a space where the stamps carry
                # 'T', and ' ' < 'T', so a datetime() upper bound is silently false (it hid the
                # attempt on 4Q4ucBXS in this function's first run).
                from datetime import datetime, timedelta
                hi = (datetime.fromisoformat(at) + timedelta(seconds=120)).isoformat()
                seen = c.execute(
                    "SELECT terminal_reason FROM memecoin_entry_attempts WHERE mint=? AND "
                    "decision_started_at BETWEEN ? AND ? AND origin_event_id LIKE ?",
                    (mint, at[:19], hi, f"%{sig}%")).fetchall()
                mcap = price * 1e9 if price else None
                ratio = 0.005 if (mcap or 0) < 50_000 else 0.0025
                gates = ("unknown size" if size is None else
                         "pass size/mcap" if (size >= 197 and mcap and size >= ratio * mcap and mcap <= 250_000)
                         else "fails size/mcap")
                print(f"   det {handle} {mint[:8]} {at[11:19]} ${size or 0:,.0f} mcap~{mcap or 0:,.0f} "
                      f"armed={uid in armed} {gates} attempts={[r[0] for r in seen] or 'NONE'}")
    print(f"deploys {len(windows)}, entries stood down {total:.0f}s in total")


def selftest():
    cases = {
        "source buy $30 (0.172%) under mcap floor 0.500% ($86)": ("POLICY", "source_buy_floor"),
        "source buy unknown under mcap floor 0.500% ($45)": ("INFRA", "source_size_unknown"),
        "mcap $15027822 over ceiling $250000 (over_ceiling)": ("POLICY", "mcap_ceiling"),
        "exact quote is +47% above source price (cap 35%)": ("POLICY", "chase_cap"),
        "slot_window_expired": ("INFRA", "lane_landing_late"),
        "USDC balance unavailable": ("INFRA", "provider_read"),
        "unroutable_chain": ("POLICY", "unsupported_chain"),
        "entry older than 60s": ("POLICY", "stale_signal"),
        "coin already ran +32% past their entry (cap 30%)": ("POLICY", "chase_cap"),
        "slot_lane_stale_sol_usd": ("INFRA", "stale_inputs"),
        "chain_error:custom program error: 0x1771": ("INFRA", "lane_landing_late"),
        "chain_error:BuySlippageBelowMinTokensOut": ("MARKET", "market_moved"),
        "slot lane build refused: leg_stale": ("INFRA", "stale_inputs"),
        "mint account not visible yet (fail-closed)": ("INFRA", "provider_read"),
        "route build refused: Raydium simulation refused the Token-2022 buy": ("INFRA", "route_build"),
        "route build refused: Raydium simulation: price moved past min-out (router 0x26)": (
            "MARKET", "market_moved"),
        "no planned-quantity sell route": ("INFRA", "no_route"),
        "insufficient native gas for entry + approval + full-book exit reserve (have=0.1, need=0.2)":
            ("INFRA", "funding"),
        "not_approved": ("ROSTER", "roster"),
        "round trip -12.0% over 8.0% cap (sell back $1 of $2)": ("POLICY", "roundtrip_cap"),
        "sell_sim_min_out_repeated: fresh funded sell simulation still hit router min-out": (
            "MARKET", "market_moved"),
        "a reason nobody has seen": ("UNCLASSIFIED", "unclassified"),
        "unresolved signed transaction on solana": ("POLICY", "inflight_signed_serialization"),
        "receipt_unreadable: unresolved": ("INFRA", "receipt_booking"),
        "native pre-transaction ambiguous": ("INFRA", "evm_pre_transaction"),
        "pre-tx sign failed": ("INFRA", "evm_pre_transaction"),
        "pre-tx failed (unwrap)": ("INFRA", "evm_pre_transaction"),
        "kill_switch_tripped": ("INFRA", "deploy_window"),
        "slot_lane_scaled_clip": ("POLICY", "lane_scaled_clip_handoff"),
        "daily loss limit reached (latched)": ("POLICY", "daily_loss_latch"),
        "launchpad-gated: transfer hook HoDLrkxn requires the platform co-sign (Meteora DBC)": (
            "POLICY", "coin_policy"),
        "launchpad-gated: transfer hook busyAbcd requires the platform co-sign (Meteora DBC)": (
            "POLICY", "coin_policy"),
        "sqlite3.OperationalError: SQLITE_BUSY": ("INFRA", "lock_wait"),
        "chain_error:NotEnoughTokensToBuy": ("MARKET", "curve_exhausted"),
        "coin is a honeypot mint 7xbusyQ9": ("POLICY", "coin_policy"),
    }
    bad = {r: (classify(r), want) for r, want in cases.items() if classify(r) != want}
    assert not bad, bad
    audit = ["2026-09-27T00:07:24.321347+00:00 TRIP source=runtime-deploy token=a",
             "2026-09-27T00:07:47.773710+00:00 CLEAR source=runtime-deploy token=a",
             "2026-09-26T21:30:10.442322+00:00 TRIP source=runtime-deploy token=b",  # before since
             "2026-09-27T00:10:14.583694+00:00 TRIP source=runtime-deploy token=c",
             "2026-09-27T00:10:17.000000+00:00 CLEAR_REJECTED source=runtime-deploy token=-",
             "2026-09-27T00:10:34.693266+00:00 CLEAR_OVERRIDE source=web:127.0.0.1 token=c"]
    runner = ["2026-09-27T00:07:24.449961+00:00 [memecoin] runner received SIGTERM — exiting",
              "2026-09-27T00:07:41.222453+00:00 [memecoin] ⚠ REAL leg ARMED — venues=[]",
              "2026-09-27T00:10:14.661973+00:00 [memecoin] runner received SIGTERM — exiting",
              "2026-09-27T00:10:28.053059+00:00 [memecoin] ⚠ REAL leg ARMED — venues=[]"]
    w = deploy_windows("2026-09-27T00:00:00", audit, runner)
    assert [x[0][11:19] for x in w] == ["00:07:24", "00:10:14"], w
    assert w[0][3][11:19] == "00:07:47" and w[1][3][11:19] == "00:10:34", w   # REJECTED skipped
    assert w[1][1][11:19] == "00:10:14" and w[1][2][11:19] == "00:10:28", w
    # a deploy refused before the stop takes neither the next deploy's stop nor its ARMED line
    aborted = ["2026-09-28T08:02:48.996204+00:00 TRIP source=runtime-deploy token=x",
               "2026-09-28T08:02:49.062976+00:00 CLEAR source=runtime-deploy token=x",
               "2026-09-28T08:05:48.914727+00:00 TRIP source=runtime-deploy token=y",
               "2026-09-28T08:06:10.383456+00:00 CLEAR source=runtime-deploy token=y"]
    run2 = ["2026-09-28T08:05:49.000000+00:00 [memecoin] runner received SIGTERM — exiting",
            "2026-09-28T08:06:08.000000+00:00 [memecoin] ⚠ REAL leg ARMED — venues=[]"]
    w2 = deploy_windows("2026-09-28T08:00:00", aborted, run2)
    assert w2[0][1] is None and w2[0][2] is None and w2[1][1][11:19] == "08:05:49", w2
    print(f"selftest ok ({len(cases)} cases)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--deploy-windows", action="store_true",
                    help="per runtime-deploy stand-down window since --since")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    if not args.since:
        sys.exit("--since is required")
    since = args.since.replace("Z", "")
    if args.deploy_windows:
        report_deploy_windows(since)
        return
    until = args.until.replace("Z", "") if args.until else None
    report(read_rows(since, until))


if __name__ == "__main__":
    main()
