#!/usr/bin/env python3
"""Desk attention worker, TAPE side (operator order 2026-10-22; Gamma pre-registration bff9e00c9).

Live: each second, for every open desk position (ledger read-only via memecoin_desk_risk_shadow.open_desk_positions), read the
mint's committed U-tape prefix and write signal events to data/desk/attention/tape.db (its own file; the watcher and Beta's
sweep read it read-only). Blind is not calm: a stale heartbeat, a tape gap inside the look-back or an unreadable file writes an
explicit 'unknown' row and suppresses the detectors for that position (missing swaps would otherwise read as exodus / volume
collapse). Nice 19, own process, off the fill path, no network, no provider call, no writable ledger handle.

Dry count (--dry-count --days D1,D2): replays the detectors over the U emissions of past days as virtual positions
(emission_at .. +life) and prints per-signal counts and rates; --events-out writes every event as jsonl for Gamma's dev-day
tuning. Outcome-free: no price, no PnL is read.

Knobs: MEMECOIN_DESK_ATTENTION_ENABLED (default 1; 0 exits), MEMECOIN_DESK_ATTN_<PARAM> (see Params), MEMECOIN_DESK_ATTN_COMMIT_MARGIN_S
(default 2.0: the tape writer batches commits about every second and the curve tape has no committed-prefix watermark, so the
cut is now minus this measured margin).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from sniper.services import memecoin_desk_attention_signals as A  # noqa: E402
from sniper.services import memecoin_desk_attention_store as S  # noqa: E402
from sniper.services import memecoin_desk_attention_tape as T  # noqa: E402

HEARTBEAT_MAX_AGE_S = 5.0
COMMIT_MARGIN_S = 2.0
TICK_S = 1.0
WHOLE_LIFE_SIGNALS = ("holder_dump", "creator_sell")
log = logging.getLogger(__name__)


def lookback_s(p):
    return p.volume_baseline_s + p.volume_window_s


def heartbeat(desk_dir, now):
    """(ok, reason, sol_usd). Any problem is a named reason, never a default."""
    try:
        beat = json.loads((Path(desk_dir) / "universe.heartbeat").read_text())
        ts, sol = float(beat["ts"]), beat["sol_usd"]
    except Exception:
        return False, "heartbeat_unreadable", None
    if not math.isfinite(ts) or ts > now or now - ts > HEARTBEAT_MAX_AGE_S or beat.get("connected") is not True:
        return False, "tape_stale", None
    if type(sol) not in (int, float) or not math.isfinite(sol) or not 0 < sol < 1e9:
        return False, "sol_usd_unknown", None
    return True, None, float(sol)


def thresholds_version(environ):
    return str(environ.get("MEMECOIN_DESK_ATTN_THRESHOLDS_VERSION", "") or "").strip() or "unset"


def armed_signals(environ):
    """PER-SIGNAL ARMING (operator yes 2026-10-23): MEMECOIN_DESK_ATTN_ARMED = comma list of A.SIGNALS that may
    fire. Default empty = nothing fires even with a version set: a version alone must never arm the signals
    Gamma's addendum rejected (volume_spike, buyer_exodus, holder_dump). An unknown name raises (no typo arms
    nothing silently, none arms something else)."""
    names = {n.strip() for n in str(environ.get("MEMECOIN_DESK_ATTN_ARMED", "") or "").split(",") if n.strip()}
    unknown = names - set(A.SIGNALS)
    if unknown:
        raise ValueError("MEMECOIN_DESK_ATTN_ARMED names unknown signals: %s" % ",".join(sorted(unknown)))
    return frozenset(names)


def _unknown(conn, pos, signal, reason, key, now, cut, version, stats):
    """An explicit 'unknown' row (fired NULL): blind is never calm. One row per (position, signal, reason, key)."""
    k = "unknown:%s:%s" % (reason, key)
    ok = S.insert(conn, event_id=A.event_id(pos["id"], {"signal": signal, "key": k}, version), position_id=pos["id"],
                  mint=pos["mint"], signal=signal, value={"state": "unknown", "reason": reason, "receipt_cut": cut},
                  observed_at=now, source=A.SOURCE, fired=None, thresholds_version=version)
    stats["unknown"] += int(ok)


def tick(*, desk_dir, out_db, positions, cluster_db, params, now, ours_fn=None, commit_margin=COMMIT_MARGIN_S, conn=None,
         version="unset", armed=None):
    """One pass over `positions` (dicts: id, mint, opened_at epoch). Returns stats. Writes only through `conn`/out_db.
    Rows carry fired=1 only under a frozen thresholds `version` AND for a signal in `armed` (None = every signal,
    for direct callers; main passes armed_signals()); otherwise every measurement is a dry row (fired NULL)."""
    stats = {"positions": len(positions), "fired": 0, "unknown": 0, "dropped": 0, "tape_ok": True, "reason": None}
    own = conn is None
    conn = conn or S.connect(out_db)
    cut = now - commit_margin
    fired_flag = None if version == "unset" else 1
    try:
        ok, reason, sol = heartbeat(desk_dir, now)
        stats["tape_ok"], stats["reason"] = ok, reason
        for pos in positions:
            try:
                if not ok:
                    _unknown(conn, pos, "tape", reason, int(now // 10), now, cut, version, stats)
                    continue
                lo = max(cut - lookback_s(params), 0.0)
                gaps = T.gaps_between(desk_dir, lo, now, mint=pos["mint"])
                if gaps:
                    _unknown(conn, pos, "tape", "tape_gap", int(max(g[0] for g in gaps)), now, cut, version, stats)
                    continue
                # holder_dump (cumulative sold since open) and creator_sell look at the whole life, so a gap older than the
                # look-back still undercounts them: say so, once per position, and withhold exactly those two signals
                blind_life = pos["opened_at"] < lo and bool(T.gaps_between(desk_dir, pos["opened_at"], lo))
                if blind_life:
                    for sig in WHOLE_LIFE_SIGNALS:
                        _unknown(conn, pos, sig, "tape_gap_since_open", pos["id"], now, cut, version, stats)
                price = T.latest_curve_price(desk_dir, pos["mint"], cut, sol, now)
                if price is not None:
                    S.set_price(conn, mint=pos["mint"], price_sol=price[0], price_usd=price[1], slot=price[2],
                                signature=price[3], received_at=price[4], receipt_cut=cut, updated_at=now)
                swaps = T.load_swaps(desk_dir, pos["mint"], lo, cut, sol)
                creator = T.creator_of(desk_dir, pos["mint"], now)
                holders = T.holders_before(cluster_db, pos["mint"], pos["opened_at"]) if cluster_db else None
                if creator is None:
                    _unknown(conn, pos, "creator_sell", "creator_unknown", pos["id"], now, cut, version, stats)
                if holders is None:
                    _unknown(conn, pos, "holder_dump", "no_entry_snapshot", pos["id"], now, cut, version, stats)
                sigs = frozenset(pos.get("fill_sigs") or ())
                ours = set(ours_fn(pos, swaps) if ours_fn else ())
                if sigs:                                           # our wallet, learned from our own fill transactions
                    ours |= T.actors_of_signatures(desk_dir, sigs, max(pos["opened_at"] - 3600, 0.0), now)
                for event in A.detect(swaps, creator=creator, ours=frozenset(ours), ours_sigs=sigs, opened_at=pos["opened_at"],
                                      cut=cut, params=params, holders=holders):
                    if blind_life and event["signal"] in WHOLE_LIFE_SIGNALS:
                        continue
                    A.validate_event(event)
                    new = S.insert(conn, event_id=A.event_id(pos["id"], event, version), position_id=pos["id"], mint=pos["mint"],
                                   signal=event["signal"],
                                   value={**event["value"], "evidence": event["evidence"], "key": event["key"],
                                          "receipt_cut": cut, "params_version": A.PARAMS_VERSION},
                                   observed_at=event["observed_at"], source=A.SOURCE,
                                   fired=fired_flag if armed is None or event["signal"] in armed else None,
                                   thresholds_version=version)
                    stats["fired"] += int(new)            # rows written (fired or dry), as before
            except T.TapeUnreadable as exc:
                _unknown(conn, pos, "tape", "tape_unreadable:%s" % str(exc).split(":")[0], int(now // 10), now, cut,
                         version, stats)
            except Exception as exc:                           # one position never stops the others; counted, never silent
                stats["dropped"] += 1
                log.exception("attention tick failed position=%s", pos.get("id"))
                try:                                           # no row would read as calm to a reader of the table: write blind
                    _unknown(conn, pos, "tape", "worker_error:%s" % type(exc).__name__, int(now // 10), now, cut, version, stats)
                except Exception:
                    log.exception("attention unknown row failed position=%s", pos.get("id"))
        S.set_health(conn, ts=now, tape_ok=ok, reason=reason, receipt_cut=cut, positions=len(positions),
                     inserted=stats["fired"] + stats["unknown"], dropped=stats["dropped"])
        conn.commit()
    finally:
        if own:
            conn.close()
    return stats


def read_fill_sigs(ledger_db, position_id):
    """Our fill transaction signatures for one position (memecoin_fills.tx_sig), ledger read-only."""
    from sniper.persistence.db_core import get_readonly_connection
    conn = get_readonly_connection(str(ledger_db))
    try:
        return tuple(r[0] for r in conn.execute("SELECT tx_sig FROM memecoin_fills WHERE position_id=? AND tx_sig IS NOT NULL "
                                                "AND tx_sig<>''", (position_id,)))
    finally:
        conn.close()


def our_actors(pos, swaps):
    """Actors of the tape swaps in this position's own fill transactions (its fill signatures): our wallet, learned from data."""
    sigs = set(pos.get("fill_sigs") or ())
    return {s["actor"] for s in swaps if s["signature"] in sigs}


def dry_count(desk_dir, days, params, sol_usd, life_s=1800.0, step_s=5.0, events_out=None, cluster_db=None):
    """Replay the detectors over each day's U emissions as virtual positions. Outcome-free; read-only."""
    counts, positions, hours, unknown_gap, no_holders = {s: 0 for s in A.SIGNALS}, 0, 0.0, 0, 0
    sink = open(events_out, "w") if events_out else None
    try:
        for day in days:
            path = Path(desk_dir) / "candidate_emissions" / ("%s.jsonl" % day)
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                    mint, at = row["mint"], float(row["snapshot"]["emission_at"])
                except (ValueError, KeyError, TypeError):
                    continue
                positions += 1
                hours += life_s / 3600
                creator = T.creator_of(desk_dir, mint, at)
                swaps = T.load_swaps(desk_dir, mint, at - lookback_s(params), at + life_s, sol_usd)
                gaps = T.gaps_between(desk_dir, at - lookback_s(params), at + life_s)
                holders = T.holders_before(cluster_db, mint, at) if cluster_db else None   # the entry snapshot, as in the live tick
                no_holders += holders is None
                seen = set()
                t = at + step_s
                while t <= at + life_s:
                    if any(t - lookback_s(params) <= g[0] <= t for g in gaps):
                        unknown_gap += 1
                    else:
                        for e in A.detect(swaps, creator=creator, ours=frozenset(), opened_at=at, cut=t, params=params, holders=holders):
                            ident = (e["signal"], e["key"])
                            if ident not in seen:
                                seen.add(ident)
                                counts[e["signal"]] += 1
                                if sink:
                                    sink.write(json.dumps({"day": day, "mint": mint, "emission_at": at, **e}, sort_keys=True) + "\n")
                    t += step_s
    finally:
        if sink:
            sink.close()
    return {"positions": positions, "position_hours": round(hours, 3), "events": counts,
            "events_per_position_hour": {k: (round(v / hours, 4) if hours else None) for k, v in counts.items()},
            "unknown_steps_tape_gap": unknown_gap, "positions_without_entry_snapshot": no_holders, "sol_usd": sol_usd, "params_version": A.PARAMS_VERSION,
            "step_s": step_s, "life_s": life_s, "days": days}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger-db")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-count", action="store_true")
    ap.add_argument("--days", default="")
    ap.add_argument("--sol-usd", type=float, default=None, help="dry count only; required")
    ap.add_argument("--events-out")
    a = ap.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import require_state_root_outside_release, state_root
    require_state_root_outside_release()
    load_env_files(str(state_root()))
    desk_dir = Path(state_root()) / "data" / "desk"
    params = A.params_from_env(os.environ)
    if a.dry_count:
        if not a.days or a.sol_usd is None or not 0 < a.sol_usd < 1e9:
            ap.error("--dry-count needs --days and --sol-usd (explicit, recorded in the output)")
        os.nice(19)
        print(json.dumps(dry_count(desk_dir, a.days.split(","), params, a.sol_usd, events_out=a.events_out,
                                    cluster_db=desk_dir / "cluster.db"), sort_keys=True))
        return 0
    if os.getenv("MEMECOIN_DESK_ATTENTION_ENABLED", "1").strip() == "0":
        print("[desk-attention] MEMECOIN_DESK_ATTENTION_ENABLED=0; exiting")
        return 0
    os.nice(19)
    import fcntl
    out = desk_dir / "attention"
    out.mkdir(parents=True, exist_ok=True)
    lock = open(out / "tape.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-attention] another worker holds the lock; exiting")
        return 0
    from memecoin_desk_risk_shadow import open_desk_positions
    from sniper.services import memecoin_desk_risk as R
    ledger_db = a.ledger_db or str(Path(state_root()) / "sniper_tracker.db")
    margin = float(os.getenv("MEMECOIN_DESK_ATTN_COMMIT_MARGIN_S", COMMIT_MARGIN_S))
    armed = armed_signals(os.environ)                            # a bad name stops the worker at start, never mid-run
    print(json.dumps({"attention_armed": sorted(armed), "thresholds_version": thresholds_version(os.environ)}), flush=True)
    while True:
        started = time.time()
        try:
            positions = [{"id": pos["id"], "mint": pos["mint"], "opened_at": R._ts(pos["opened_at"]),
                          "fill_sigs": read_fill_sigs(ledger_db, pos["id"])}
                         for pos, _policy, _rails, _mark in open_desk_positions(ledger_db)]
            stats = tick(desk_dir=desk_dir, out_db=out / "tape.db", positions=positions,
                         cluster_db=desk_dir / "cluster.db", params=params, now=started, commit_margin=margin,
                         version=thresholds_version(os.environ), armed=armed)
        except Exception as exc:                                 # a bad pass is logged and counted; the loop continues
            print(json.dumps({"attention_pass_error": type(exc).__name__}), flush=True)
            stats = {"fired": 0, "unknown": 0}
        if stats.get("fired") or stats.get("unknown"):
            print(json.dumps(stats, sort_keys=True), flush=True)
        if a.once:
            return 0
        time.sleep(max(0.0, TICK_S - (time.time() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
