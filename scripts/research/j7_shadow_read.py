#!/usr/bin/env python3
"""The J7-on-Solana SHADOW read: did the fast lanes fill what J7 would have entered? Read-only.

MEMECOIN_J7_SOLANA_MODE=shadow (2a8072f7) makes a covered J7 Solana buy run every entry gate and
stop with terminal 'shadow' (delivery_lane 'j7_shadow') instead of reserving or signing. Alpha's
hourly read, from the restart on:

  would-enter   shadow attempts (J7 passed every gate and would have gone to the leg)
  covered       of those, how many a fast lane FILLED on the same source buy, and the rest bucketed
                with Gamma's census classifier (fast_lane_coverage_census.classify): c refused on
                a gate (named), a slot venue refusal, b wallet unknown, e no fast-lane sighting;
                d (the lane skipped it as the feed's) must not happen in shadow, and is flagged
  fills         Solana funded fills by lane since --since, closed PnL and open count

STOP-class (Alpha, 2026-09-28): (1) 0 fills on the chain in the last 90 min while would-enter > 3
in that window; (2) one fast-lane refusal family covers > 50% of all would-enters (at least 4);
(3) EVM only (2026-09-29): any MISS in the window. An EVM buy is shadowed only while that chain's
watcher is healthy, so a MISS there is a real miss with the watcher up: revert the chain to live;
(4) EVM only, BLIND (2026-09-29): any covered shadow buy in the window the lane never sighted,
WHATEVER J7's own gate said. Sighted = a watcher detection or fast-lane attempt carrying the same
source tx, else (no tx in J7's origin) the same wallet and coin within 30 s of the source time.
Cash-asset "buys" (wSOL, USDC, USDT) are not counted. On 4663 three 0xdetweiler buys were refused over_ceiling by J7's gate while the lane
never saw them: STOP(3) counts only would-enters, so a blind lane hid behind the gate.

--chain names the chain (default solana): `--chain 4663` reads the EVM shadow of
MEMECOIN_J7_SHADOW_EVM_CHAINS (spec 2026-09-29-memecoin-j7-shadow-evm-buys-design.md).
POST-QUOTE families (chase, round trip, price impact, quote-vs-chart sanity) are counted apart
and never trip STOP(2): shadow returns before J7's quote leg, so J7's own quote gates never ran on
a would-enter, and a fast-lane refusal there is not a lane gap (Alpha, 2026-09-28 13:58Z).

    python3 scripts/research/j7_shadow_read.py                       # since the runner's start
    python3 scripts/research/j7_shadow_read.py --since 2026-09-28T13:00:00+00:00
    python3 scripts/research/j7_shadow_read.py --selftest
"""
import argparse
import collections
import contextlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE = Path(os.getenv("SNIPER_STATE_ROOT") or REPO)     # ledger, heartbeat, log
STOP_WINDOW_S = 90 * 60
BLIND_WINDOW_S = 30.0
STOP_MIN_WOULD = 4
POST_QUOTE = re.compile(r"exact quote is N above source|round trip N over|price impact N over|"
                        r"route quote is Nx", re.I)


# THE J7-LIVE VERDICT (Alpha, 2026-09-28): a STOP(1) is not automatic. For each would-enter: would J7-live
# have been refused by the SAME downstream gate a fast lane hit? Shadow stops before the reservation and
# the quote, so J7-live still faces the reservation's re-entry cooldown and one-position rule, the
# quote's chase / round trip / impact / chart sanity, no route, the coin age and post-peak checks. Only a
# row with no fast-lane fill and no such shared refusal is a real fast-lane miss (a lane-only gate, an
# on-chain failure on its own, unseen, wallet unknown).
# The reservation's risk envelope is shared too (Beta, 2026-09-28 22:50Z read): shadow returns before
# reserve_entry, so a deploy's kill-switch trip, the daily-loss latch, the drawdown pause and the slot
# and exposure caps refuse J7-live exactly as they refuse a fast lane. The first such row (DegenCapitalLLC
# 4RLhqc1j, 22:00:09Z, kill_switch_tripped during a deploy) read as a MISS.
SHARED = re.compile(r"exact quote is|round trip|price impact|route quote is|no quote|no_route|no route|"
                    r"re-entry cooldown|one position per coin|coin_too_young|post_peak|"
                    r"kill.switch|daily loss limit|drawdown pause|max concurrent positions|"
                    r"total exposure limit", re.I)


def j7_live_verdict(bucket, detail):
    """'filled' (a fast lane took it), 'shared' (J7-live would have hit the same gate) or 'miss'. Pure."""
    if bucket == "f":
        return "filled"
    if SHARED.search(detail or ""):        # an unseen (e) or unknown-wallet (b) detail never matches
        return "shared"
    return "miss"


def _census():
    spec = importlib.util.spec_from_file_location(
        "fast_lane_coverage_census", REPO / "scripts/research/fast_lane_coverage_census.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.LOG = STATE / "logs" / "memecoin_runner.log"
    return mod


# Cash-asset "buys" are not memecoin buys (Gamma, 2026-09-29): J7 reports StonkyScheffler buying
# wSOL, which no lane follows. The same test the real entry path uses, on every chain.
sys.path.insert(0, str(REPO))
from sniper.analysis.fomo_account.classify import is_cash_asset  # noqa: E402
EVM_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")
SOLANA_SIG = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")


def _source_sig(origin):
    """The source transaction an origin id carries, found by SHAPE (an EVM 0x+64-hex hash or a
    base58 Solana signature), or None. Origins put it in different places: 'swap:raybot:<tx>:...',
    'pump:<sig>:<mint prefix>', 'onchain:<chain>:<tx>:<mint>'. A positional parse read the mint
    prefix of every 'pump:' origin as the signature (Thepennyflippe 0/11 sighted, 2026-09-29)."""
    for part in str(origin or "").split(":"):
        if EVM_TX.match(part) or SOLANA_SIG.match(part):
            return part
    return None


def lane_sighted(conn, net, uid, mint, at_iso, window_s=BLIND_WINDOW_S, origin=None):
    """True when a fast lane saw this buy. J7's own attempt is never a sighting.

    SOURCE TX FIRST (Gamma, 2026-09-29): when J7's origin carries the source tx, the buy is sighted
    iff a watcher detection's signature or a lane attempt's origin carries that SAME tx, however
    late J7 was (DipWheeler's J7 event arrived ~10 min after the lane's detection). A tx the lane
    never saw is blind even beside the same coin's other tx. Without a tx (a 'swap:<uuid>' origin),
    the same wallet and coin within `window_s` of `at_iso`, the source time, counts."""
    t = datetime.fromisoformat(str(at_iso).replace("Z", "+00:00"))
    sig = _source_sig(origin) if origin else None
    if sig:
        pat = "%" + str(sig) + "%"
        lo = (t - timedelta(hours=6)).isoformat()      # bounds the scan; a lane row is never later
        hi = (t + timedelta(hours=6)).isoformat()
        if conn.execute(
                "SELECT 1 FROM onchain_wallet_detections WHERE network_id=? AND signature LIKE ? "
                "AND detected_at>=? AND detected_at<=? LIMIT 1", (net, pat, lo, hi)).fetchone():
            return True
        return conn.execute(
            "SELECT 1 FROM memecoin_entry_attempts WHERE network_id=? "
            "AND signal_source IN ('onchain_watch', 'slot_lane') AND origin_event_id LIKE ? "
            "AND decision_started_at>=? AND decision_started_at<=? LIMIT 1",
            (net, pat, lo, hi)).fetchone() is not None
    lo = (t - timedelta(seconds=window_s)).isoformat()
    hi = (t + timedelta(seconds=window_s)).isoformat()
    if conn.execute(
            "SELECT 1 FROM onchain_wallet_detections WHERE network_id=? AND source_user_id=? "
            "AND LOWER(mint)=LOWER(?) AND detected_at>=? AND detected_at<=? LIMIT 1",
            (net, uid, mint, lo, hi)).fetchone():
        return True
    return conn.execute(
        "SELECT 1 FROM memecoin_entry_attempts WHERE network_id=? AND source_user_id=? "
        "AND LOWER(mint)=LOWER(?) AND signal_source IN ('onchain_watch', 'slot_lane') "
        "AND decision_started_at>=? AND decision_started_at<=? LIMIT 1",
        (net, uid, mint, lo, hi)).fetchone() is not None


def stop_flags(*, fills_window, would_window, would_total, refusal_families, chain="solana",
               miss_window=0, blind_window=0):
    """The STOP-class conditions that hold, as text. Pure."""
    out = []
    label = "Solana" if chain == "solana" else chain
    if fills_window == 0 and would_window > 3:
        out.append("STOP(1): 0 %s fills in %d min with %d shadow would-enters"
                   % (label, STOP_WINDOW_S // 60, would_window))
    if would_total >= STOP_MIN_WOULD:
        for fam, n in refusal_families.items():
            if POST_QUOTE.search(fam):
                continue
            if n > 0.5 * would_total:
                out.append("STOP(2): '%s' refused %d of %d would-enters" % (fam, n, would_total))
    if chain != "solana" and miss_window > 0:
        out.append("STOP(3): %d MISS on %s in %d min with the watcher healthy: revert %s to live"
                   % (miss_window, chain, STOP_WINDOW_S // 60, chain))
    if chain != "solana" and blind_window > 0:
        out.append("STOP(4): %d covered %s J7 buys the lane never sighted in %d min: "
                   "the lane is blind to a covered wallet; revert %s to live"
                   % (blind_window, chain, STOP_WINDOW_S // 60, chain))
    return out


def read(conn, since, now, chain="solana"):
    census = _census()
    rows = conn.execute(
        "SELECT attempt_id, network_id, mint, source_user_id, source_handle, origin_event_id, "
        "decision_started_at, observed_at, terminal_status, terminal_reason FROM "
        "memecoin_entry_attempts WHERE delivery_lane='j7_shadow' AND decision_started_at>=? "
        "AND network_id=? ORDER BY decision_started_at", (since, chain)).fetchall()
    would = [r for r in rows if r["terminal_status"] == "shadow"]
    gate_refused = collections.Counter(census.family(r["terminal_reason"]) for r in rows
                                       if r["terminal_status"] not in ("shadow", None))
    slot_lines, onchain_lines = census.parse_log(census.LOG) if census.LOG.exists() else ({}, {})
    buckets, refusals, per = collections.Counter(), collections.Counter(), []
    for r in would:
        item = {"attempt": r["attempt_id"], "net": r["network_id"], "mint": r["mint"],
                "uid": r["source_user_id"], "handle": r["source_handle"],
                "origin": r["origin_event_id"], "t0": census.utc(r["decision_started_at"]),
                "observed": census.utc(r["observed_at"] or r["decision_started_at"])}
        bucket, detail = census.classify(item, conn, slot_lines, onchain_lines)
        buckets[bucket] += 1
        if bucket in ("c", "a"):
            refusals[detail.split(";")[0].strip()] += 1
        per.append((r["decision_started_at"][11:19], r["source_handle"], r["mint"][:8], bucket, detail,
                    j7_live_verdict(bucket, detail), r["decision_started_at"]))
    window_lo = (now - timedelta(seconds=STOP_WINDOW_S)).isoformat()
    would_window = sum(1 for r in would if r["decision_started_at"] >= window_lo)
    fills = conn.execute(
        "SELECT signal_source, COUNT(*), SUM(CASE WHEN is_open=0 THEN realized_pnl END), "
        "SUM(is_open), MAX(opened_at) FROM memecoin_positions WHERE execution_mode='real' "
        "AND network_id=? AND opened_at>=? GROUP BY signal_source", (chain, since)).fetchall()
    fills_window = conn.execute(
        "SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' AND "
        "network_id=? AND opened_at>=?", (chain, window_lo)).fetchone()[0]
    in_window = collections.Counter(v for *_, v, at in per if at >= window_lo)
    blind, per_wallet = [], collections.defaultdict(lambda: [0, 0])     # handle -> [shadow, sighted]
    for r in rows:
        if is_cash_asset(r["mint"]):
            continue
        seen = lane_sighted(conn, r["network_id"], r["source_user_id"], r["mint"],
                            r["observed_at"] or r["decision_started_at"], origin=r["origin_event_id"])
        per_wallet[r["source_handle"]][0] += 1
        per_wallet[r["source_handle"]][1] += int(seen)
        if not seen:
            blind.append(r)
    blind_window = sum(1 for r in blind if r["decision_started_at"] >= window_lo)
    return {"window_verdicts": in_window, "would": len(would), "gate_refused": gate_refused, "buckets": buckets,
            "refusals": refusals, "per": per, "fills": fills, "fills_window": fills_window,
            "would_window": would_window, "blind": len(blind), "blind_window": blind_window,
            "blind_rows": blind, "per_wallet": {h: tuple(v) for h, v in per_wallet.items()},
            "stop": stop_flags(fills_window=fills_window, would_window=would_window,
                               would_total=len(would), refusal_families=refusals, chain=chain,
                               miss_window=in_window["miss"], blind_window=blind_window)}


def selftest():
    assert stop_flags(fills_window=0, would_window=4, would_total=4, refusal_families={}) == [
        "STOP(1): 0 Solana fills in 90 min with 4 shadow would-enters"]
    assert stop_flags(fills_window=0, would_window=3, would_total=3, refusal_families={}) == []
    assert stop_flags(fills_window=1, would_window=9, would_total=9, refusal_families={}) == []
    assert stop_flags(fills_window=2, would_window=5, would_total=5,
                      refusal_families={"onchain_watch refused source buy": 3}) == [
        "STOP(2): 'onchain_watch refused source buy' refused 3 of 5 would-enters"]
    assert stop_flags(fills_window=2, would_window=3, would_total=3,
                      refusal_families={"x": 3}) == [], "under the minimum count"
    chase = "onchain_watch refused exact quote is N above source price (cap N)"
    for fam in (chase, "onchain_watch refused round trip N over N cap (sell back N of N)",
                "slot_lane refused price impact N over N cap",
                "onchain_watch refused route quote is Nx the chart mid (cap Nx)"):
        assert stop_flags(fills_window=2, would_window=5, would_total=5,
                          refusal_families={fam: 4}) == [], fam
    assert stop_flags(fills_window=2, would_window=5, would_total=5,
                      refusal_families={chase: 4, "onchain_watch refused source buy $N": 3}) == [
        "STOP(2): 'onchain_watch refused source buy $N' refused 3 of 5 would-enters"]
    for bucket, detail, want in (
            ("f", "fast lane filled: slot_lane", "filled"),
            ("c", "slot_lane rejected re-entry cooldown: a real position on this coin closed withi", "shared"),
            ("c", "slot_lane failed chain_error:BondingCurveComplete; onchain_watch aborted no quote (no_route)",
             "shared"),
            ("c", "onchain_watch refused exact quote is N above source price (cap N)", "shared"),
            ("c", "onchain_watch refused kill_switch_tripped", "shared"),
            ("c", "onchain_watch refused daily loss limit reached (latched)", "shared"),
            ("c", "slot_lane rejected solana max concurrent positions reached (N)", "shared"),
            ("c", "slot_lane failed chain_error:BuySlippageBelowMinBaseAmountOut", "miss"),
            ("c", "onchain_watch refused source buy N under absolute floor N", "miss"),
            ("c", "slot_lane rejected slot_lane_scaled_clip", "miss"),
            ("e", "wallet known, no fast-lane sighting", "miss"),
            ("b", "no verified address on solana at decision", "miss")):
        assert j7_live_verdict(bucket, detail) == want, (bucket, detail)
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", help="ISO start (default: the running runner's started_at)")
    ap.add_argument("--chain", default="solana", help="solana (default) or an EVM chain id, e.g. 4663")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    since = args.since
    if not since:
        since = json.loads((STATE / "data/memecoin_runner.heartbeat").read_text())["started_at"]
    now = datetime.now(timezone.utc)
    with contextlib.closing(sqlite3.connect(
            "file:%s?mode=ro" % (STATE / "sniper_tracker.db"), uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        out = read(conn, since, now, chain=args.chain)
    label = "Solana" if args.chain == "solana" else args.chain
    print("J7-%s shadow read  since %s  at %s" % (label, since, now.isoformat(timespec="seconds")))
    print("would-enter %d (last 90 min %d); J7 gate refusals among shadow rows %d"
          % (out["would"], out["would_window"], sum(out["gate_refused"].values())))
    print("  buckets:", dict(out["buckets"]), " (f filled by a fast lane, c/a refused, "
          "b wallet unknown, e unseen, d lane skipped = BUG in shadow)")
    for fam, n in out["refusals"].most_common():
        print("  fast-lane refusal %3d  %s%s" % (n, fam, "  [post-quote: not in STOP(2)]"
                                                   if POST_QUOTE.search(fam) else ""))
    for t, h, mint, b, detail, verdict, _at in out["per"]:
        print("   %s %-16s %s %s %-6s %s" % (t, h, mint, b, verdict, detail[:84]))
    v = out["window_verdicts"]
    print("  j7-live verdict, last 90 min: filled %d, shared %d (J7-live refused by the same gate), "
          "MISS %d" % (v["filled"], v["shared"], v["miss"]))
    print("  BLIND (covered shadow buys, any J7 outcome, never sighted by a lane: same source tx, "
          "else +-%d s of the source time): %d, last 90 min %d"
          % (BLIND_WINDOW_S, out["blind"], out["blind_window"]))
    # THE PER-WALLET MATCH TABLE (Alpha, 2026-09-29): a covered wallet the lane does not see shows
    # as a low share here long before it costs an entry.
    for h, (n, seen) in sorted(out["per_wallet"].items(), key=lambda kv: (kv[1][1] / kv[1][0], -kv[1][0])):
        print("   wallet %-18s shadow %3d  lane-sighted %3d  %3.0f%%" % (h, n, seen, 100.0 * seen / n))
    for r in out["blind_rows"][-5:]:
        print("   blind %s %-16s %s %s" % (r["decision_started_at"][11:19], r["source_handle"],
                                           r["mint"][:10], (r["terminal_reason"] or "")[:60]))
    print("%s fills since (last 90 min %d):" % (label, out["fills_window"]))
    for src, n, pnl, open_n, last in out["fills"]:
        print("  %-16s n=%d closed_pnl=%s open=%d last=%s"
              % (src, n, "%+.2f" % pnl if pnl is not None else "n/a", open_n or 0, str(last)[11:19]))
    for flag in out["stop"] or ["no STOP condition"]:
        print(flag)
    if any(f.startswith("STOP(1)") for f in out["stop"]):
        print("  STOP(1) is not automatic (Alpha): real fast-lane misses in the window = %d" % v["miss"])


if __name__ == "__main__":
    main()
