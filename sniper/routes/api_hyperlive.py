"""Read-only API for the Hyperlive engine — serves the latest pipeline state the UI polls."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from flask import Blueprint, jsonify, request
from sniper.util.clock import real_utc_now

DEFAULT_DB = os.environ.get("HYPERLIVE_DB", "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite")
DATA_DIR = "/home/bigpapi/Desktop/Sniper/data"
INTEL_DB = os.path.join(DATA_DIR, "hl_intel.sqlite")
XYZ_DB = os.path.join(DATA_DIR, "hyperlive_xyz_scan_log.sqlite")
SCREEN_STALE_MS = 7 * 86_400_000


RUNTIME_PROCESSES = [
    {
        "id": "xyz_capture",
        "label": "XYZ lead-lag capture",
        "match": "xyz",
        "cmd_match": f"run_xyz_leadlag_capture.py --db-path {XYZ_DB}",
        "role": "Cross-asset lead-lag research line",
        "cadence": "150s",
    },
    {
        "id": "follow_runner",
        "label": "Forward-follow paper",
        "match": "follow",
        "cmd_match": f"run_hl_follow.py --db-path {INTEL_DB}",
        "role": "Phase-2 copy-honesty data",
        "cadence": "180s",
    },
    {
        "id": "cascade_capture",
        "label": "Cascade capture",
        "match": "cascade_capture",
        "cmd_match": "run_cascade_capture.py",
        "role": "Forward-only realized-cascade observation capture",
        "cadence": "10s buckets",
    },
    {
        "id": "cascade_audit",
        "label": "Cascade exploit audit",
        "match": "cascade_audit",
        "cmd_match": "run_cascade_exploit_audit.py",
        "role": "Periodic controls/FDR/gate verdict",
        "cadence": "operator-gated",
    },
    {
        "id": "copy_runner",
        "label": "24/7 mirror copy",
        "match": "copy_runner",
        "cmd_match": "run_hl_copy.py",
        "role": "Mirror-copy paper engine (marked targets) — offline while copy is paused",
        "cadence": "120s",
    },
    {
        "id": "far_whale_capture",
        "label": "Far-whale magnet capture",
        "match": "far_whale",
        "cmd_match": "run_far_whale_capture.py",
        "role": "fwlm-v0 paper study — sparse visible-whale liquidation magnet",
        "cadence": "180s",
    },
]

RUNTIME_CRONS = [
    {"id": "xyz_watchdog", "label": "XYZ watchdog", "needle": "xyz_leadlag_watchdog.sh", "cadence": "1m"},
    {"id": "xyz_audit", "label": "XYZ audit", "needle": "run_xyz_leadlag_audit.py", "cadence": "hourly"},
    {"id": "follow_watchdog", "label": "Follow watchdog", "needle": "hl_follow_watchdog.sh", "cadence": "1m"},
    {"id": "smart_money_screen", "label": "Smart-money screen", "needle": "screen_hl_smart_money.py", "cadence": "6h"},
    {"id": "cascade_capture", "label": "Cascade capture", "needle": "cascade_capture_watchdog.sh", "cadence": "1m"},
    {"id": "cascade_audit", "label": "Cascade audit", "needle": "run_cascade_exploit_audit.py", "cadence": "operator-gated"},
    {"id": "far_whale_watchdog", "label": "Far-whale watchdog", "needle": "far_whale_watchdog.sh", "cadence": "1m"},
]


def _screen_with_freshness(screen: dict, *, now_ms: int) -> dict:
    if not screen:
        return {}
    from sniper.hyperlive import intel_store as IS

    out = dict(screen)
    out["verdicts"] = IS.normalized_verdict_counts(out.get("verdicts") or {})
    ts_ms = int(out.get("ts_ms") or 0)
    out["stale_after_ms"] = SCREEN_STALE_MS
    if ts_ms <= 0:
        out.update({"age_ms": None, "age_hours": None, "age_days": None, "is_stale": True})
        return out
    age_ms = max(0, now_ms - ts_ms)
    out.update({
        "age_ms": age_ms,
        "age_hours": round(age_ms / 3_600_000, 1),
        "age_days": round(age_ms / 86_400_000, 2),
        "is_stale": age_ms > SCREEN_STALE_MS,
    })
    return out


def _decorate_trader_row(t: dict, fstats: dict, cstats: dict | None = None, *, now_ms: int) -> dict:
    from sniper.hyperlive import intel_store as IS
    from sniper.hyperlive import trader_intel as TI

    stored = t.get("verdict")
    fs = fstats.get(t.get("wallet"))
    cs = (cstats or {}).get(t.get("wallet"))
    t["stored_verdict"] = stored
    t["verdict"] = IS.display_verdict(stored)
    if stored == "copyable":
        t["legacy_verdict"] = "copyable"
    t["follow"] = fs
    t["copy"] = cs
    t["effective_verdict"] = TI.effective_verdict(
        stored, t.get("archetype_pass"), fs, now_ms=now_ms)
    return t


def _attach_tracked_lifecycle(traders: list[dict], tracked_rows: list[dict]) -> None:
    tracked_by_wallet = {row["wallet"]: row for row in tracked_rows}
    for trader in traders:
        tracked = tracked_by_wallet.get(trader.get("wallet"))
        if not tracked:
            trader["tracked"] = False
            trader.setdefault("lifecycle_status", None)
            trader.setdefault("lifecycle_reason", None)
            continue
        trader["tracked"] = True
        trader["lifecycle_status"] = tracked.get("lifecycle_status") or "active"
        trader["lifecycle_reason"] = tracked.get("lifecycle_reason")
        trader["lifecycle_updated_ts_ms"] = tracked.get("lifecycle_updated_ts_ms")
        trader["culled_ts_ms"] = tracked.get("culled_ts_ms")


def _bounded_positive_int(raw, *, default: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, 1), maximum)


def _analyzer_page(
    traders: list[dict],
    *,
    active_target_wallets: set[str],
    view: str,
    query: str,
    verdict: str,
    page: int,
    page_size: int,
) -> dict:
    def lane(row: dict) -> str:
        if (
            row.get("effective_verdict") == "proven"
            or row.get("wallet") in active_target_wallets
        ):
            return "priority"
        if bool(row.get("tracked")) and row.get("lifecycle_status") == "active":
            return "watchlist"
        return "archive"

    for row in traders:
        row["analyzer_view"] = lane(row)
        row["is_copy_target"] = row.get("wallet") in active_target_wallets
    counts = {
        "priority": sum(row["analyzer_view"] == "priority" for row in traders),
        "watchlist": sum(row["analyzer_view"] == "watchlist" for row in traders),
        "archive": sum(row["analyzer_view"] == "archive" for row in traders),
        "all": len(traders),
    }
    query = str(query or "").strip().lower()
    exact_wallet = bool(query) and any(
        str(row.get("wallet") or "").lower() == query for row in traders
    )
    selected = list(traders)
    if view != "all" and not exact_wallet:
        selected = [row for row in selected if row["analyzer_view"] == view]
    if query:
        fields = (
            "wallet",
            "verdict",
            "stored_verdict",
            "effective_verdict",
            "archetype",
            "reason",
            "lifecycle_status",
            "lifecycle_reason",
        )
        selected = [
            row
            for row in selected
            if any(query in str(row.get(field) or "").lower() for field in fields)
        ]

    def verdict_match(row: dict) -> bool:
        if verdict == "all":
            return True
        if verdict in {"watch", "proven"}:
            return row.get("effective_verdict") == verdict
        if verdict == "copying":
            return bool(row.get("is_copy_target"))
        if verdict == "culled":
            return row.get("lifecycle_status") == "culled"
        if verdict == "rejected":
            return row.get("effective_verdict") not in {"watch", "proven"}
        return True

    selected = [row for row in selected if verdict_match(row)]
    selected.sort(key=lambda row: (
        -int(bool(row.get("is_copy_target"))),
        -int(row.get("effective_verdict") == "proven"),
        -int(row.get("effective_verdict") == "watch"),
        -int(bool(row.get("tracked") and row.get("lifecycle_status") == "active")),
        -int(row.get("ts_ms") or 0),
        str(row.get("wallet") or ""),
    ))
    total = len(selected)
    offset = (page - 1) * page_size
    items = selected[offset:offset + page_size]
    return {
        "traders": items,
        "view": view,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": offset + len(items) < total,
        "counts": counts,
    }


def _json_bool(value, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _cleaner_copy_state(conn, *, now_ms: int) -> dict:
    copy_targets, blocked_copy_targets = _copy_target_groups(conn, now_ms=now_ms)
    return {
        "targets": [t["wallet"] for t in copy_targets],
        "target_rows": copy_targets,
        "blocked_targets": blocked_copy_targets,
    }


def _copy_target_groups(conn, *, now_ms: int) -> tuple[list[dict], list[dict]]:
    from sniper.hyperlive import intel_store as IS

    fstats = IS.follow_stats_by_wallet(conn)
    cstats = IS.copy_stats_by_wallet(conn)
    proven_exists = IS.has_proven_wallet(
        conn, now_ms=now_ms, follow_stats=fstats, copy_stats=cstats
    )
    eligible, blocked = [], []
    for target in IS.get_copy_targets(conn):
        status = IS.copy_target_status(
            conn, target["wallet"], now_ms=now_ms, follow_stats=fstats,
            copy_stats=cstats, copy_tier=target.get("copy_tier") or "proven",
            proven_exists=proven_exists)
        row = {**target, "copy_status": status}
        if status["eligible"]:
            eligible.append(row)
        else:
            blocked.append(row)
    return eligible, blocked


def _runtime_pid(match: str) -> int | None:
    proc = next((p for p in RUNTIME_PROCESSES if p["match"] == match), None)
    if not proc:
        return None
    try:
        out = subprocess.check_output(["pgrep", "-f", proc["cmd_match"]], text=True, timeout=2)
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    for raw in out.splitlines():
        if not raw.strip().isdigit():
            continue
        pid = int(raw.strip())
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except OSError:
            continue
        if _cmdline_matches_runner(cmd, proc["cmd_match"]):
            return pid
    return None


def _cmdline_matches_runner(cmd: str, cmd_match: str) -> bool:
    first = os.path.basename((cmd or "").split(" ", 1)[0])
    return first.startswith("python") and cmd_match in (cmd or "")


def _pid_elapsed(pid: int) -> str | None:
    try:
        out = subprocess.check_output(["ps", "-p", str(pid), "-o", "etime="], text=True, timeout=2)
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    return out.strip() or None


def _crontab_text() -> str:
    try:
        return subprocess.check_output(["crontab", "-l"], text=True, stderr=subprocess.DEVNULL, timeout=2)
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def _copy_engine_live() -> bool:
    """True iff the 24/7 mirror-copy runner is actually running. Marking a copy target does
    nothing unless this is up, so the UI must reflect it rather than imply copy is active."""
    try:
        out = subprocess.check_output(["pgrep", "-f", "run_hl_copy.py"], text=True, timeout=2)
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    for raw in out.splitlines():
        pid = raw.strip()
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except OSError:
            continue
        if _cmdline_matches_runner(cmd, "run_hl_copy.py"):
            return True
    return False


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def _runtime_xyz_data() -> dict:
    out = {"exists": os.path.exists(XYZ_DB), "refreshes": 0, "rows": 0, "basis_rows": 0, "latest_ts": None}
    if not out["exists"]:
        return out
    try:
        conn = sqlite3.connect(XYZ_DB)
        try:
            out["refreshes"] = _table_count(conn, "hyperlive_scan_log")
            row = conn.execute("SELECT COUNT(DISTINCT scan_ts), COUNT(*), "
                               "SUM(CASE WHEN binance_basis_bps IS NOT NULL THEN 1 ELSE 0 END), "
                               "MAX(scan_ts) FROM hyperlive_scan_log").fetchone()
            out.update({"refreshes": int(row[0] or 0), "rows": int(row[1] or 0),
                        "basis_rows": int(row[2] or 0), "latest_ts": row[3]})
        finally:
            conn.close()
    except sqlite3.Error:
        out["error"] = "sqlite_error"
    return out


def _runtime_intel_data() -> dict:
    out = {"exists": os.path.exists(INTEL_DB), "screened": 0, "watch": 0, "proven": 0,
           "rejected": 0, "tracked": 0, "last_screen_ts": None}
    if not out["exists"]:
        return out
    try:
        from sniper.hyperlive import intel_store as IS
        from sniper.hyperlive import trader_intel as TI
        import time
        conn = IS.connect_readonly(INTEL_DB)
        try:
            traders = IS.get_screened(conn)
            fstats = IS.follow_stats_by_wallet(conn)
            now_ms = int(time.time() * 1000)
            watch = proven = rejected = 0
            for t in traders:
                eff = TI.effective_verdict(t.get("verdict"), t.get("archetype_pass"),
                                           fstats.get(t.get("wallet")), now_ms=now_ms)
                if eff == "proven":
                    proven += 1
                elif eff == "watch" or t.get("archetype_pass"):
                    watch += 1
                else:
                    rejected += 1
            screen = IS.latest_screen_run(conn)
            out.update({"screened": len(traders), "watch": watch, "proven": proven,
                        "rejected": rejected, "tracked": len(IS.get_tracked(conn)),
                        "last_screen_ts": screen.get("ts_ms")})
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        out["error"] = type(e).__name__
    return out


def _runtime_follow_data() -> dict:
    out = {"open": 0, "closed": 0, "sum_net_bps": 0.0, "avg_net_bps": 0.0, "win_rate": 0.0}
    if not os.path.exists(INTEL_DB):
        return out
    try:
        from sniper.hyperlive import intel_store as IS
        conn = IS.connect_readonly(INTEL_DB)
        try:
            out.update(IS.follow_summary(conn))
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        out["error"] = type(e).__name__
    return out


def _runtime_cascade_data(db_path: str) -> dict:
    out = {"exists": os.path.exists(db_path), "obs": 0, "events": 0, "audits": 0,
           "status": "not_started", "latest_audit_ts": None}
    if not out["exists"]:
        return out
    try:
        from sniper.hyperlive import persistence as P
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            out["obs"] = _table_count(conn, "hl_cascade_obs")
            out["events"] = _table_count(conn, "hl_cascade_events")
            out["audits"] = _table_count(conn, "hl_cascade_audit_runs")
            latest = P.read_latest_cascade_audit(conn) if out["audits"] else None
            out["status"] = (latest["verdict"].get("status") if latest else ("accruing" if out["obs"] else "not_started"))
            out["latest_audit_ts"] = latest["ts_ms"] if latest else None
        finally:
            conn.close()
    except sqlite3.Error:
        out["status"] = "schema_missing"
    return out


def _runtime_far_whale_data(db_path: str) -> dict:
    out = {"exists": os.path.exists(db_path), "episodes": 0, "candidates": 0,
           "outcomes": 0, "status": "not_started", "verdict": None}
    if not out["exists"]:
        return out
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            out["episodes"] = _table_count(conn, "hl_far_whale_liq_episodes")
            out["candidates"] = _table_count(conn, "hl_far_whale_liq_candidates")
            out["outcomes"] = _table_count(conn, "hl_far_whale_liq_outcomes")
            row = conn.execute(
                "SELECT payload_json FROM hl_far_whale_liq_reports WHERE kind='verdict'"
                " ORDER BY ts_ms DESC, id DESC LIMIT 1").fetchone()
            frozen = conn.execute(
                "SELECT COUNT(*) FROM hl_far_whale_liq_reports WHERE kind='freeze'").fetchone()[0]
            if row:
                out["verdict"] = json.loads(row["payload_json"])
                out["status"] = out["verdict"].get("status", "accruing")
            elif out["candidates"]:
                out["status"] = "accruing"
            elif frozen:
                out["status"] = "frozen_awaiting_candidates"
            elif out["episodes"] or _table_count(conn, "hl_far_whale_liq_position_snapshots"):
                out["status"] = "sweep_mode"
        finally:
            conn.close()
    except sqlite3.Error:
        out["status"] = "not_started"      # tables absent -> lane dormant
    return out


def _runtime_payload(db_path: str) -> dict:
    processes = []
    for proc in RUNTIME_PROCESSES:
        pid = _runtime_pid(proc["match"])
        processes.append({
            **{k: proc[k] for k in ("id", "label", "role", "cadence")},
            "state": "running" if pid else "idle",
            "pid": pid,
            "elapsed": _pid_elapsed(pid) if pid else None,
        })
    crons = _crontab_text()
    scheduled = [{**c, "installed": c["needle"] in crons} for c in RUNTIME_CRONS]
    xyz = _runtime_xyz_data()
    intel = _runtime_intel_data()
    follow = _runtime_follow_data()
    cascade = _runtime_cascade_data(db_path)
    far_whale = _runtime_far_whale_data(db_path)
    proc_by_id = {p["id"]: p for p in processes}
    cascade_running = proc_by_id["cascade_capture"]["state"] == "running"
    fclosed = int(follow.get("closed", 0) or 0)
    favg = float(follow.get("avg_net_bps", 0.0) or 0.0)
    fwr = round(float(follow.get("win_rate", 0.0) or 0.0) * 100)
    lanes = [
        {
            "id": "cascade_detector",
            "label": "Liquidation cascade detector",
            "status": "live" if cascade_running else "built_idle",
            "runtime": proc_by_id["cascade_capture"]["state"],
            "data": f"{cascade['obs']} obs · {cascade['events']} events · {cascade['audits']} audits",
            "note": ("LIVE — capturing 10s buckets, forward-only. Accruing to a pre-registered "
                     "milestone; a cascade event fires only on a real σ-breach move + OI drop + "
                     "trade spike. No verdict yet — the one HL taker line still open."
                     if cascade_running else
                     "Built but not capturing — start scripts/cascade_capture_watchdog.sh."),
        },
        {
            "id": "far_whale_magnet",
            "label": "Far-whale liquidation magnet (paper)",
            "status": ("live" if proc_by_id["far_whale_capture"]["state"] == "running"
                       else "built_idle"),
            "runtime": proc_by_id["far_whale_capture"]["state"],
            "data": (f"{far_whale['episodes']} episodes · {far_whale['candidates']} candidates"
                     f" · {far_whale['outcomes']} outcomes · {far_whale['status']}"),
            "note": ("Sparse visible whale sample — NOT full liquidation density. "
                     "fwlm-v0 pre-registered paper study: does a single visible far whale "
                     "liquidation level (50–300bps) pull price? Verdict only at the §12 "
                     "milestone via episode-clustered bootstrap; ITT primary."),
        },
        {
            "id": "forward_follow",
            "label": "Copy / forward-follow",
            "status": "paused",
            "runtime": proc_by_id["follow_runner"]["state"],
            "data": f"{fclosed} closed follows · mean {favg:+.1f} bps · WR {fwr}%",
            "note": ("PAUSED 2026-07-04 — forward test came back null "
                     f"(n={fclosed}, mean {favg:+.1f} bps, {fwr}% WR). The copyability gate was "
                     "anti-selective (screener 'copyable' did worse than 'watch'). Reworking toward "
                     "conviction-hold selection, pre-registered. Runner + crons off; no copy orders."),
        },
        {
            "id": "xyz_leadlag",
            "label": "XYZ cross-asset lead-lag",
            "status": "retired",
            "runtime": proc_by_id["xyz_capture"]["state"],
            "data": f"{xyz['basis_rows']} basis rows · {xyz['refreshes']} refreshes (frozen)",
            "note": ("RETIRED 2026-07-04 — signal is real (t=4.43 @150s) but the move is ~1 bp, under "
                     "the ~9 bp taker fee → after-fee −7 to −8 bps in every cell. Taker-dead. Runner + "
                     "watchdog + audit crons removed."),
        },
        {
            "id": "legacy_idle",
            "label": "Retired taker lanes",
            "status": "retired",
            "runtime": "idle",
            "data": "microstructure A/B · 24/7 mirror copy · scanner engine · market-radar",
            "note": ("Microstructure entry-gate A/B killed 6/25 (−9 bps, cost-gate cleared 0 of 386k "
                     "decisions). Mirror-copy engine offline. Scanner/market-radar idle. Parked so dead "
                     "lanes don't pollute current evidence."),
        },
    ]
    n_live = sum(1 for l in lanes if l["status"] == "live")
    n_paused = sum(1 for l in lanes if l["status"] == "paused")
    n_retired = sum(1 for l in lanes if l["status"] == "retired")
    headline = {
        "live": [l["label"] for l in lanes if l["status"] == "live"],
        "paused": [l["label"] for l in lanes if l["status"] == "paused"],
        "retired": [l["label"] for l in lanes if l["status"] == "retired"],
        "summary": f"{n_live} live · {n_paused} paused · {n_retired} retired",
    }
    return {"available": True, "processes": processes, "scheduled": scheduled,
            "data": {"xyz": xyz, "intel": intel, "follow": follow, "cascade": cascade,
                     "far_whale": far_whale},
            "lanes": lanes, "headline": headline}


def _xyz_summary() -> dict:
    """Compact xyz cross-asset lead-lag status for the UI (accruing until market hours)."""
    if not os.path.exists(XYZ_DB):
        return {"status": "not_started"}
    try:
        from sniper.hyperlive import scan_log as SL
        from sniper.hyperlive import scan_audit as SA
        conn = SL.connect(XYZ_DB)
        try:
            refreshes = conn.execute("SELECT COUNT(DISTINCT scan_ts) FROM hyperlive_scan_log").fetchone()[0]
            with_basis = conn.execute(
                "SELECT COUNT(*) FROM hyperlive_scan_log WHERE binance_basis_bps IS NOT NULL").fetchone()[0]
            de = SA.basis_direction_edge(conn, horizon_ms=300_000, basis_threshold_bps=10.0, min_volume=0.0)
            return {"status": "ok", "refreshes": refreshes, "live_basis_rows": with_basis, "edge": de}
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": str(e)}


def _trader_breakdown(fills: list) -> list:
    """Per-coin/side edge location from closed round-trips (for the analyzer detail panel)."""
    from collections import defaultdict
    agg = defaultdict(lambda: {"pnls": []})
    for f in fills:
        d = str(f.get("dir", ""))
        if not d.startswith("Close"):
            continue
        side = "long" if "Long" in d else ("short" if "Short" in d else "?")
        key = (f.get("coin"), side)
        try:
            agg[key]["pnls"].append(float(f.get("closedPnl") or 0.0))
        except (TypeError, ValueError):
            continue
    out = []
    for (coin, side), v in agg.items():
        pnls = v["pnls"]
        wins = [p for p in pnls if p > 0]
        gl = -sum(p for p in pnls if p < 0)
        out.append({"coin": coin, "side": side, "n": len(pnls),
                    "win_rate": round(len(wins) / len(pnls), 2) if pnls else 0.0,
                    "profit_factor": round(sum(wins) / gl, 2) if gl > 0 else (999.0 if wins else 0.0),
                    "realized": round(sum(pnls), 0)})
    out.sort(key=lambda r: -abs(r["realized"]))
    return out


def _cascade_summary() -> dict:
    """Compact liquidation-cascade magnet status for the UI."""
    if not os.path.exists(DEFAULT_DB):
        return {"status": "not_started"}
    try:
        from sniper.hyperlive import persistence as P
        from sniper.hyperlive import cluster_audit as CA
        conn = P.connect(DEFAULT_DB)
        try:
            polls = conn.execute("SELECT COUNT(DISTINCT ts_ms) FROM hyperlive_cluster_history").fetchone()[0]
            de = CA.cluster_cascade_edge(conn, horizon_ms=360_000, max_distance_bps=100.0, min_notional=250_000.0)
            return {"status": "ok", "polls": polls, "edge": de}
        except sqlite3.Error:
            return {"status": "not_started"}
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": str(e)}


def _copy_scorecard_payload(*, holdout: bool = False) -> dict:
    """Honest deployable scorecard for the paper copy book (hl_copy_book). Live marks + BTC candles
    feed total-book MTM + beta-neutral; decomposition + per-wallet come from the captured rows.
    Read-only; degrades gracefully if HL is unreachable. holdout=True -> out-of-sample rows only."""
    from sniper.hyperlive import copy_decomp as CD
    from sniper.hyperlive import copy_scorecard as SC
    from sniper.hyperlive import intel_store as IS
    from sniper.markets import hyperliquid_api as HL

    if not os.path.exists(INTEL_DB):
        return {"available": False, "reason": "engine not started"}
    conn = IS.connect_readonly(INTEL_DB)
    try:
        wc, wo = "status!='open' AND pnl_bps IS NOT NULL", "status='open'"
        if holdout:
            wc += " AND is_holdout=1"
            wo += " AND is_holdout=1"
        closed = [dict(r) for r in conn.execute(f"SELECT * FROM hl_copy_book WHERE {wc}")]
        open_rows = [dict(r) for r in conn.execute(f"SELECT * FROM hl_copy_book WHERE {wo}")]
        allrows = [dict(r) for r in conn.execute("SELECT * FROM hl_copy_book")]
        freeze = IS.holdout_freeze_ts(conn)
        trend = IS.get_scorecard_snapshots(conn, holdout=holdout, limit=30)
    finally:
        conn.close()
    if not closed and not open_rows:
        return {"available": False, "reason": "no copy rows yet"}

    marks, candles = {}, None
    try:
        marks = HL.fetch_marks()
    except Exception:  # noqa: BLE001
        pass
    try:
        if closed:
            t0 = min(r["detect_ts_ms"] for r in closed)
            t1 = max((r["exit_ts_ms"] or r["detect_ts_ms"]) for r in closed)
            candles = HL.fetch_candles("BTC", "1h", t0 - 3600000, t1 + 3600000)
    except Exception:  # noqa: BLE001
        candles = None

    rep = SC.build_report(closed, open_rows, marks=marks, market_candles=candles)
    now = max((r.get("exit_ts_ms") or 0) for r in allrows) or 0
    # annotate over ALL rows (consensus context = every concurrent mirror), but bucket only the
    # rows this payload reports on — otherwise holdout=True shows full-book consensus as OOS
    ann = CD.annotate_consensus(allrows, now_ms=now)
    bucket_rows = [r for r in ann if r.get("pnl_bps") is not None
                   and (not holdout or r.get("is_holdout"))]
    rep["decomp"] = {
        "major_vs_alt": CD.major_vs_alt(closed),
        "side": CD.by_side(closed),
        "consensus": CD.consensus_buckets(bucket_rows),
        "top_coins": CD.top_contributors(closed, n=8),
    }
    rep["available"] = True
    rep["holdout_only"] = holdout
    rep["holdout_freeze_ts"] = freeze
    rep["trend"] = trend
    rep["hl_real_status"] = _hl_real_status()
    return rep


def _hl_real_status() -> dict | None:
    """Read-only ops block for the real leg. mean_drift_bps is visibility only — the
    pre-registered verdict is sealed until read_at_n filled closes (spec §11)."""
    from sniper.hyperlive import intel_store as IS
    from sniper.hyperlive import real_store as RS
    from sniper.settings import hl_real as cfg
    try:
        conn = IS.connect_readonly(INTEL_DB)
        try:
            dep_id = conn.execute(
                "SELECT value FROM hl_meta WHERE key='hl_real_deployment_id'").fetchone()
            epoch = conn.execute(
                "SELECT value FROM hl_meta WHERE key='hl_real_epoch_ms'").fetchone()
            intents = {r["state"]: r["n"] for r in conn.execute(
                "SELECT state, COUNT(*) n FROM hl_real_intents GROUP BY state")}
            drift = conn.execute(
                "SELECT COUNT(*) n, AVG(drift_bps) mean_drift, SUM(realized_usd) realized "
                "FROM hl_real_fills WHERE leg='close' AND drift_bps IS NOT NULL").fetchone()
            from datetime import datetime, timezone
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            return {
                "deployment": ({"deployment_id": dep_id[0], "epoch_ms": int(epoch[0])}
                               if dep_id and epoch else None),
                "enabled": cfg.HL_REAL_ENABLED, "dry_run": cfg.HL_REAL_DRY_RUN,
                "open_n": len(RS.leg_open_positions(conn)),
                "intents": intents,
                "closes_n": drift["n"] or 0,
                "mean_drift_bps": (round(drift["mean_drift"], 2)
                                   if drift["mean_drift"] is not None else None),
                "realized_usd": (round(drift["realized"], 2)
                                 if drift["realized"] is not None else 0.0),
                "day_realized_usd": RS.day_realized_usd(conn, day),
                "breaker": RS.breaker_latched(conn),
                "read_at_n": 20,
            }
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — status block must never break the scorecard
        return None


def create_api_hyperlive_blueprint(db_path: str = DEFAULT_DB) -> Blueprint:
    bp = Blueprint("api_hyperlive", __name__)

    def _conn():
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    @bp.route("/api/hyperlive/state")
    def state():
        if not os.path.exists(db_path):
            return jsonify({"available": False, "reason": "engine not started"})
        conn = _conn()
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT updated_ts_ms, state_json FROM hyperlive_state WHERE id=1").fetchone()
            if not row:
                return jsonify({"available": False, "reason": "no state yet"})
            st = json.loads(row["state_json"])
            st["available"] = True
            st["updated_ts_ms"] = row["updated_ts_ms"]
            return jsonify(st)
        except sqlite3.Error as e:
            return jsonify({"available": False, "reason": str(e)})
        finally:
            conn.close()

    @bp.route("/api/hyperlive/history")
    def history():
        if not os.path.exists(db_path):
            return jsonify({"rows": []})
        conn = _conn()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT cycle_id, asset, setup, regime, reason, realized_bps, won, thesis, closed_ts_ms "
                "FROM hyperlive_cycles ORDER BY closed_ts_ms DESC LIMIT 100").fetchall()
            return jsonify({"rows": [dict(r) for r in rows]})
        finally:
            conn.close()

    @bp.route("/api/hyperlive/cascade")
    def cascade():
        """Thesis-2 realized-cascade exploitability verdict (accruing/edge/no-edge) + accrual
        counts. Distinct from the Thesis-1 magnet summary in /intel. Read-only."""
        empty = {"available": False, "status": "none", "n_obs": 0, "n_events": 0, "verdict": None}
        if not os.path.exists(db_path):
            return jsonify(empty)
        from sniper.hyperlive import persistence as P
        conn = _conn()
        conn.row_factory = sqlite3.Row
        try:
            latest = P.read_latest_cascade_audit(conn)
            n_obs = conn.execute("SELECT COUNT(*) FROM hl_cascade_obs").fetchone()[0]
            n_events = conn.execute("SELECT COUNT(*) FROM hl_cascade_events").fetchone()[0]
        except sqlite3.Error:
            return jsonify(empty)              # tables not created yet on this engine DB
        finally:
            conn.close()
        return jsonify({
            "available": True,
            "status": (latest["verdict"]["status"] if latest else "none"),
            "n_obs": n_obs, "n_events": n_events,
            "verdict": (latest["verdict"] if latest else None),
        })

    @bp.route("/api/hyperlive/runtime")
    def runtime():
        """Operator truth for current Hyperliquid research: what is running, scheduled,
        accruing, and deliberately idle. Read-only; no process or cron mutation."""
        return jsonify(_runtime_payload(db_path))

    @bp.get("/api/hyperlive/edge-desk")
    def edge_desk():
        """Current local Hyperliquid evidence and paper-runtime truth. Read-only."""
        from sniper.hyperlive import edge_desk as ED

        response = jsonify(ED.build_payload(
            db_path=INTEL_DB,
            data_dir=Path(DATA_DIR),
            now_ms=int(real_utc_now() * 1000),
        ))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @bp.route("/api/hyperlive/intel")
    def intel():
        """Smart-money analyzer (repurposed Polymarket pipeline) + forward paper-follow +
        the forward edge tests (xyz lead-lag, liquidation cascade). All read-only."""
        import time
        out = {"available": True}
        if os.path.exists(INTEL_DB):
            from sniper.hyperlive import intel_store as IS
            conn = IS.connect_readonly(INTEL_DB)
            try:
                now_ms = int(time.time() * 1000)
                out["screen"] = _screen_with_freshness(IS.latest_screen_run(conn), now_ms=now_ms)
                out["cohort"] = IS.get_tracked(conn)
                out["follow"] = IS.follow_summary(conn)
                out["open_follows"] = IS.get_open_follows(conn)
                out["recent_closed"] = IS.recent_closed_follows(conn, limit=20)
                fstats = IS.follow_stats_by_wallet(conn)
                cstats = IS.copy_stats_by_wallet(conn)
                for t in out["cohort"]:
                    _decorate_trader_row(t, fstats, cstats, now_ms=now_ms)
            finally:
                conn.close()
        else:
            out["screen"], out["cohort"], out["follow"] = {}, [], {}
            out["open_follows"], out["recent_closed"] = [], []
        out["forward_tests"] = {"xyz": _xyz_summary(), "cascade": _cascade_summary()}
        return jsonify(out)

    @bp.route("/api/hyperlive/analyzer")
    def analyzer():
        """Bounded graded-trader page + screen and copy summary for HL Analyzer."""
        view = str(request.args.get("view") or "priority").strip().lower()
        if view == "attention":
            view = "priority"
        if view not in {"priority", "watchlist", "archive", "all"}:
            view = "priority"
        query = str(request.args.get("q") or "").strip()
        verdict = str(request.args.get("verdict") or "all").strip().lower()
        include_copy_details = str(
            request.args.get("include_copy_details", "1")
        ).lower() not in {"0", "false", "no"}
        page = _bounded_positive_int(request.args.get("page"), default=1, maximum=100_000)
        page_size = _bounded_positive_int(
            request.args.get("page_size"), default=25, maximum=100
        )
        if not os.path.exists(INTEL_DB):
            return jsonify({
                "available": False,
                "traders": [],
                "screen": {},
                "copy_targets": [],
                "copy_target_rows": [],
                "blocked_copy_targets": [],
                "copy_details_included": include_copy_details,
                "copy": {},
                "copy_engine": "live" if _copy_engine_live() else "offline",
                "view": view,
                "page": page,
                "page_size": page_size,
                "total": 0,
                "has_more": False,
                "counts": {"priority": 0, "watchlist": 0, "archive": 0, "all": 0},
            })
        from sniper.hyperlive import intel_store as IS
        conn = IS.connect_readonly(INTEL_DB)
        try:
            traders = IS.get_screened(conn)
            fstats = IS.follow_stats_by_wallet(conn)
            cstats = IS.copy_stats_by_wallet(conn)
            now_ms = int(real_utc_now() * 1000)
            _attach_tracked_lifecycle(traders, IS.get_tracked(conn, include_culled=True))
            for t in traders:
                _decorate_trader_row(t, fstats, cstats, now_ms=now_ms)
            active_target_wallets = {
                row["wallet"]
                for row in IS.get_copy_targets(conn, active_only=False)
                if row.get("active")
            }
            page_payload = _analyzer_page(
                traders,
                active_target_wallets=active_target_wallets,
                view=view,
                query=query,
                verdict=verdict,
                page=page,
                page_size=page_size,
            )
            if include_copy_details:
                copy_targets, blocked_copy_targets = _copy_target_groups(conn, now_ms=now_ms)
            else:
                copy_targets, blocked_copy_targets = [], []
            return jsonify({
                "available": True,
                **page_payload,
                "screen": _screen_with_freshness(
                    IS.latest_screen_run(conn), now_ms=now_ms
                ),
                "copy_targets": [t["wallet"] for t in copy_targets],
                "copy_target_rows": copy_targets,
                "blocked_copy_targets": blocked_copy_targets,
                "copy_details_included": include_copy_details,
                "copy": IS.copy_summary(conn),
                "copy_engine": "live" if _copy_engine_live() else "offline",
            })
        finally:
            conn.close()

    @bp.route("/api/hyperlive/copy-scorecard")
    def copy_scorecard():
        """Honest deployable scorecard for the paper copy book — total-book MTM (not censored
        realized-only), bootstrap CIs, concentration, beta-neutral alpha, per-wallet ranking,
        size-decay, and edge decomposition. ?holdout=1 for out-of-sample rows only. Read-only."""
        holdout = str(request.args.get("holdout", "")).lower() in ("1", "true", "yes")
        return jsonify(_copy_scorecard_payload(holdout=holdout))

    @bp.route("/api/hyperlive/trader/<wallet>")
    def trader(wallet):
        """Live on-demand deep-screen of any HL wallet (analyzer detail / paste-an-address)."""
        try:
            from sniper.markets import hyperliquid_api as HL
            from sniper.hyperlive import trader_intel as TI
            fills = HL.fetch_user_fills(wallet)
        except Exception as e:  # noqa: BLE001
            return jsonify({"available": False, "reason": type(e).__name__})
        v = TI.assess_trader(fills)
        v["available"] = True
        v["wallet"] = wallet
        v["breakdown"] = _trader_breakdown(fills)
        try:
            import time
            from sniper.hyperlive import intel_store as IS
            if os.path.exists(INTEL_DB):
                ic = IS.connect_readonly(INTEL_DB)
                try:
                    fs = IS.follow_stats_by_wallet(ic).get(wallet)
                    cs = IS.copy_stats_by_wallet(ic).get(wallet)
                finally:
                    ic.close()
                v["follow"] = fs
                v["copy"] = cs
                v["effective_verdict"] = TI.effective_verdict(
                    v.get("verdict"), v.get("archetype_pass"), fs, now_ms=int(time.time() * 1000))
        except Exception:  # noqa: BLE001 — enrichment is optional, never break the route
            v.setdefault("effective_verdict", v.get("verdict"))
        return jsonify(v)

    @bp.route("/api/hyperlive/copy/set", methods=["POST"])
    def copy_set():
        """Mark/unmark a wallet as a 24/7 mirror copy target (the 'Copy this wallet' button)."""
        import time
        from sniper.hyperlive import intel_store as IS
        body = request.get_json(silent=True) or {}
        wallet = (body.get("wallet") or "").strip().lower()
        if not wallet:
            return jsonify({"ok": False, "reason": "no wallet"}), 400
        conn = IS.connect(INTEL_DB)
        IS.ensure_schema(conn)
        try:
            now_ms = int(time.time() * 1000)
            active = _json_bool(body.get("active"), default=True)
            requested_tier = body.get("copy_tier") or "proven"
            status = IS.copy_target_status(conn, wallet, now_ms=now_ms, copy_tier="proven")
            if active and requested_tier != "proven":
                copy_targets, blocked_copy_targets = _copy_target_groups(conn, now_ms=now_ms)
                return jsonify({"ok": False, "wallet": wallet, "active": False,
                                "effective_verdict": status["effective_verdict"],
                                "reason": "watch fallback targets are lifecycle-managed",
                                "copy_status": status,
                                "targets": [t["wallet"] for t in copy_targets],
                                "blocked_targets": blocked_copy_targets}), 403
            if active and not status["eligible"]:
                copy_targets, blocked_copy_targets = _copy_target_groups(conn, now_ms=now_ms)
                return jsonify({"ok": False, "wallet": wallet, "active": False,
                                "effective_verdict": status["effective_verdict"],
                                "reason": status["reason"], "copy_status": status,
                                "targets": [t["wallet"] for t in copy_targets],
                                "blocked_targets": blocked_copy_targets}), 403
            IS.set_copy_target(conn, wallet, active=active, label=body.get("label"),
                               copy_tier="proven", reason=status["reason"], ts_ms=now_ms)
            copy_targets, blocked_copy_targets = _copy_target_groups(conn, now_ms=now_ms)
            return jsonify({"ok": True, "wallet": wallet, "active": active,
                            "effective_verdict": status["effective_verdict"],
                            "reason": status["reason"], "copy_status": status,
                            "targets": [t["wallet"] for t in copy_targets],
                            "blocked_targets": blocked_copy_targets})
        finally:
            conn.close()

    @bp.route("/api/hyperlive/cleaner/manual", methods=["POST"])
    def cleaner_manual():
        """Operator cull for unwanted watched wallets. Paper-only: disables new copy opens."""
        import time
        from sniper.hyperlive import intel_store as IS

        body = request.get_json(silent=True) or {}
        wallet = (body.get("wallet") or "").strip().lower()
        raw_reason = (body.get("reason") or "").strip()
        if not wallet:
            return jsonify({"ok": False, "reason": "no wallet", "error": "no wallet"}), 400
        if not _json_bool(body.get("confirm"), default=False):
            return jsonify({"ok": False, "wallet": wallet, "reason": "confirmation required",
                            "error": "confirmation required"}), 400
        reason = "manual cull: " + (raw_reason[:180] if raw_reason else "operator cleanup")
        conn = IS.connect(INTEL_DB)
        IS.ensure_schema(conn)
        try:
            tracked = {row["wallet"]: row for row in IS.get_tracked(conn, include_culled=True)}
            if wallet not in tracked:
                return jsonify({"ok": False, "wallet": wallet, "reason": "wallet not tracked",
                                "error": "wallet not tracked"}), 404
            now_ms = int(time.time() * 1000)
            tier = next((t.get("copy_tier") for t in IS.get_copy_targets(conn, active_only=False)
                         if t.get("wallet") == wallet), "watch_fallback")
            IS.set_tracked_lifecycle(conn, wallet, status="culled", reason=reason, ts_ms=now_ms)
            IS.set_copy_target(conn, wallet, active=False, copy_tier=tier or "watch_fallback",
                               reason=reason, ts_ms=now_ms)
            return jsonify({"ok": True, "wallet": wallet, "lifecycle_status": "culled",
                            "reason": reason,
                            **_cleaner_copy_state(conn, now_ms=now_ms)})
        finally:
            conn.close()

    @bp.route("/api/hyperlive/cleaner/system", methods=["POST"])
    def cleaner_system():
        """Run the watch lifecycle culler/selector on demand. Paper-only; no exchange calls."""
        import time
        from sniper.hyperlive import intel_store as IS
        from sniper.hyperlive import watch_lifecycle as WL

        body = request.get_json(silent=True) or {}
        try:
            max_watch_targets = int(body.get("max_watch_targets", 5))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "max_watch_targets must be an integer",
                            "error": "max_watch_targets must be an integer"}), 400
        if not _json_bool(body.get("confirm"), default=False):
            return jsonify({"ok": False, "reason": "confirmation required",
                            "error": "confirmation required"}), 400
        max_watch_targets = min(max(max_watch_targets, 1), 25)
        conn = IS.connect(INTEL_DB)
        IS.ensure_schema(conn)
        try:
            now_ms = int(time.time() * 1000)
            cleanup = WL.maintain_watchlist(conn, now_ms=now_ms, max_watch_targets=max_watch_targets)
            return jsonify({"ok": True, "cleanup": cleanup,
                            **_cleaner_copy_state(conn, now_ms=now_ms)})
        finally:
            conn.close()

    @bp.route("/api/hyperlive/copy")
    def copy_state():
        """The 24/7 mirror copy book + PnL summary."""
        if not os.path.exists(INTEL_DB):
            return jsonify({"available": False, "summary": {}, "open": [], "recent_closed": []})
        from sniper.hyperlive import intel_store as IS
        import time
        conn = IS.connect_readonly(INTEL_DB)
        try:
            copy_targets, blocked_copy_targets = _copy_target_groups(conn, now_ms=int(time.time() * 1000))
            include_open = str(request.args.get("include_open", "1")).lower() not in (
                "0", "false", "no"
            )
            return jsonify({"available": True, "summary": IS.copy_summary(conn),
                            "targets": copy_targets,
                            "blocked_targets": blocked_copy_targets,
                            "open": IS.get_open_copies(conn) if include_open else [],
                            "recent_closed": IS.recent_closed_copies(conn, limit=25)})
        finally:
            conn.close()

    return bp
