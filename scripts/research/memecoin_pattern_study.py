#!/usr/bin/env python3
"""Build read-only memecoin path-archetype and frozen-policy research artifacts."""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.research.memecoin_capture_v2_manifest import assert_capture_version
from scripts.research.normalize_clock_domains import classify_legacy_timestamp
from sniper.research.memecoin_strategy.pattern_context import (
    conviction_windows,
    find_fresh_conviction_reentry,
    thesis_context,
)
from sniper.research.memecoin_strategy.pattern_paths import (
    analyze_path,
    classify_observed_path,
    classify_path,
)
from sniper.research.memecoin_strategy.pattern_policies import (
    POLICIES,
    match_past_control,
    rank_policies,
    simulate_policy,
)


def _time(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("study timestamps must be timezone aware")
    return parsed


def _mint(value) -> str:
    text = str(value or "")
    return text.lower() if text.lower().startswith("0x") else text


def _number(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive(value):
    parsed = _number(value)
    return parsed if parsed is not None and parsed > 0 else None


def _market_cap_band(value) -> str:
    parsed = _positive(value)
    if parsed is None:
        return "unknown"
    if parsed < 1_000_000:
        return "<1M"
    if parsed < 5_000_000:
        return "1-5M"
    if parsed < 30_000_000:
        return "5-30M"
    return ">30M"


def _liquidity_band(value) -> str:
    parsed = _positive(value)
    if parsed is None:
        return "unknown"
    if parsed < 25_000:
        return "<25k"
    if parsed < 50_000:
        return "25-50k"
    if parsed < 100_000:
        return "50-100k"
    return "100k+"


def _return_band(value) -> str:
    parsed = _number(value)
    if parsed is None:
        return "unknown"
    if parsed < -30:
        return "<-30"
    if parsed < 0:
        return "-30-0"
    if parsed < 20:
        return "0-20"
    if parsed < 40:
        return "20-40"
    return "40+"


def _canonical_hash(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _episode_id(lane: str, network_id: str, mint: str, signal_at: datetime) -> str:
    key = f"{lane}|{network_id}|{_mint(mint)}|{signal_at.astimezone(timezone.utc).date()}"
    return f"episode-{hashlib.sha256(key.encode()).hexdigest()[:20]}"


def _dense_path(quotes, signal_at, capture_period_s, *, lane):
    deadline = signal_at + timedelta(hours=24)
    late_tolerance = max(90.0, 2.0 * capture_period_s)
    start_tolerance = 300.0 if lane == "legacy" else late_tolerance
    valid = []
    for row in quotes:
        if row.get("quality") != "ok":
            continue
        observed_at = _time(row["observed_at"])
        price = _positive(row.get("price"))
        if price is not None and signal_at <= observed_at <= deadline + timedelta(seconds=late_tolerance):
            valid.append((observed_at, row))
    valid.sort(key=lambda item: (item[0], float(item[1]["price"]), str(item[1].get("id") or "")))
    expected = math.ceil(86400 / float(capture_period_s))
    in_window = [item for item in valid if item[0] <= deadline]
    coverage = min(1.0, len(in_window) / expected) if expected else 0.0
    starts_on_time = bool(in_window) and in_window[0][0] <= signal_at + timedelta(seconds=start_tolerance)
    reaches_deadline = bool(valid) and valid[-1][0] >= deadline
    complete = starts_on_time and reaches_deadline and coverage >= 0.90
    return [item[1] for item in in_window], complete, coverage, expected


def _parse_marks(raw):
    try:
        marks = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return []
    parsed = []
    for target, row in marks.items():
        if not isinstance(row, dict):
            continue
        price = _positive(row.get("price"))
        age = _number(row.get("age_s"))
        if price is None or age is None or age < 0:
            continue
        parsed.append((int(target), float(age), price))
    return sorted(parsed)


def _legacy_rows(conn, cutoff):
    return [dict(row) for row in conn.execute(
        "SELECT o.fomo_event_id source_observation_id,o.source_user_id,o.handle,o.mint,"
        "COALESCE(o.network_id,'solana') network_id,o.buy_ts,o.buy_ts_normalized signal_at,"
        "o.ref_price,o.marks_json,o.max_gain_pct sparse_peak_pct,o.last_price,o.status,"
        "o.clock_domain,e.created_at event_created_at,e.their_market_cap,e.their_size_usd "
        "FROM memecoin_signal_outcomes o LEFT JOIN memecoin_events e "
        "ON e.fomo_event_id=o.fomo_event_id WHERE o.capture_version=1 "
        "AND e.created_at<=? ORDER BY COALESCE(o.buy_ts_normalized,o.buy_ts),"
        "o.fomo_event_id",
        (cutoff.isoformat(),),
    )]


def _resolve_legacy_time(row, *, stamp_key, normalized_key, source_key, offset_seconds):
    if row.get(normalized_key):
        return _time(row[normalized_key]).isoformat(), False
    classified = classify_legacy_timestamp(row.get(stamp_key), row.get(source_key), offset_seconds)
    if classified is None:
        return None, False
    _domain, normalized = classified
    return _time(normalized).isoformat(), True


def _legacy_feed(conn, cutoff, offset_seconds):
    rows = []
    for db_row in conn.execute(
        "SELECT id,source_user_id,COALESCE(network_id,'solana') network_id,mint,"
        "side normalized_side,their_size_usd,created_at,received_at,received_at_normalized,"
        "clock_domain FROM memecoin_events WHERE capture_version=1 AND created_at<=? "
        "ORDER BY received_at,id",
        (cutoff.isoformat(),),
    ):
        row = dict(db_row)
        observed_at, derived = _resolve_legacy_time(
            row, stamp_key="received_at", normalized_key="received_at_normalized",
            source_key="created_at", offset_seconds=offset_seconds,
        )
        if observed_at is None or _time(observed_at) > cutoff:
            continue
        row["observed_at"] = observed_at
        row["clock_derived"] = derived
        rows.append(row)
    return rows


def _legacy_theses(conn, cutoff, offset_seconds):
    rows = []
    for db_row in conn.execute(
        "SELECT id,source_user_id,COALESCE(network_id,'solana') network_id,mint,"
        "their_pnl_pct,price,created_at,received_at,received_at_normalized,clock_domain "
        "FROM memecoin_theses WHERE capture_version=1 AND created_at<=? ORDER BY received_at,id",
        (cutoff.isoformat(),),
    ):
        row = dict(db_row)
        observed_at, derived = _resolve_legacy_time(
            row, stamp_key="received_at", normalized_key="received_at_normalized",
            source_key="created_at", offset_seconds=offset_seconds,
        )
        if observed_at is None or _time(observed_at) > cutoff:
            continue
        row["observed_at"] = observed_at
        row["clock_derived"] = derived
        rows.append(row)
    return rows


def _legacy_quotes(conn, mint, signal_at, cutoff, network_id=None):
    deadline = min(cutoff, signal_at + timedelta(hours=24, minutes=5))
    q = ("SELECT id,mint,ts observed_at,price,liquidity,quality,network_id "
         "FROM memecoin_quote_ledger WHERE mint=? AND ts>=? AND ts<=? ")
    params = [mint, signal_at.isoformat(), deadline.isoformat()]
    if network_id is not None:
        # NULL legacy rows match the requested chain (pre-migration rows are un-attributed).
        q += "AND COALESCE(network_id,?)=? "
        params += [str(network_id), str(network_id)]
    q += "ORDER BY ts,id"
    rows = []
    for row in conn.execute(q, params):
        item = dict(row)
        # Stored chain wins. For legacy NULL rows, infer solana ONLY for non-0x mints (Solana-native,
        # unambiguous); a 0x mint stays unknown rather than being mislabeled solana.
        if not item.get("network_id"):
            item["network_id"] = "solana" if not str(mint).startswith("0x") else None
        item["legacy_ungraded"] = item.get("quality") is None
        if item.get("quality") is None:
            item["quality"] = "ok"
        rows.append(item)
    return rows


def _v2_feed(conn, cutoff):
    assert_capture_version(conn, "memecoin_feed_observations_v2", 2)
    return [dict(row) for row in conn.execute(
        "SELECT * FROM memecoin_feed_observations_v2 WHERE capture_version=2 "
        "AND observed_at<=? ORDER BY observed_at,COALESCE(source_ts,''),id",
        (cutoff.isoformat(),),
    )]


def _v2_theses(feed_rows):
    theses = []
    for row in feed_rows:
        if row.get("raw_type") != "thesis_created":
            continue
        try:
            raw = json.loads(row.get("raw_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            raw = {}
        body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
        theses.append({
            "id": row.get("id"), "source_user_id": row.get("source_user_id"),
            "network_id": row.get("network_id"), "mint": row.get("mint"),
            "their_pnl_pct": body.get("percentageUnrealizedPnl"),
            "price": body.get("price") or row.get("their_price"),
            "observed_at": row.get("observed_at"),
        })
    return theses


def _v2_quotes(conn, network_id, mint, signal_at, cutoff):
    assert_capture_version(conn, "memecoin_quote_observations_v2", 2)
    deadline = min(cutoff, signal_at + timedelta(hours=24, minutes=5))
    return [dict(row) for row in conn.execute(
        "SELECT id,network_id,mint,observed_at,price,liquidity,market_cap,volume_24h,quality "
        "FROM memecoin_quote_observations_v2 WHERE capture_version=2 AND network_id=? "
        "AND mint=? AND observed_at>=? AND observed_at<=? ORDER BY observed_at,id",
        (network_id, mint, signal_at.isoformat(), deadline.isoformat()),
    )]


def load_lane(
    conn, lane: str, asof, *, capture_period_s: int | None = None,
    legacy_clock_offset_s: float = 0,
):
    if lane not in {"legacy", "v2"}:
        raise ValueError("lane must be legacy or v2")
    cutoff = _time(asof)
    period = int(capture_period_s or (60 if lane == "legacy" else 30))
    if period <= 0:
        raise ValueError("capture_period_s must be positive")
    clock_offset = _number(legacy_clock_offset_s)
    if clock_offset is None:
        raise ValueError("legacy_clock_offset_s must be finite")
    exclusions = {
        "legacy_unknown_clock": 0, "bad_quote": 0, "invalid_entry": 0,
        "unsupported_network": 0, "missing_market_cap": 0,
    }
    if lane == "legacy":
        sources = _legacy_rows(conn, cutoff)
        all_feed = _legacy_feed(conn, cutoff, clock_offset)
        all_theses = _legacy_theses(conn, cutoff, clock_offset)
        anchors = []
        legacy_clock_derived = 0
        open_partial_outcomes = 0
        for row in sources:
            if row.get("status") == "bad_quote":
                exclusions["bad_quote"] += 1
                continue
            signal_at, derived = _resolve_legacy_time(
                row, stamp_key="buy_ts", normalized_key="signal_at",
                source_key="event_created_at", offset_seconds=clock_offset,
            )
            if signal_at is None:
                exclusions["legacy_unknown_clock"] += 1
                continue
            if row.get("status") not in {"matured", "open"}:
                continue
            open_partial_outcomes += row.get("status") == "open"
            row["signal_at"] = signal_at
            row["clock_derived"] = derived
            legacy_clock_derived += derived
            anchors.append(row)
    else:
        all_feed = _v2_feed(conn, cutoff)
        all_theses = _v2_theses(all_feed)
        sources = all_feed
        anchors = [row for row in all_feed if row.get("normalized_side") == "buy"]

    deduped = {}
    for row in anchors:
        signal_at = _time(row.get("signal_at") or row.get("observed_at"))
        network_id = str(row.get("network_id") or "")
        mint = _mint(row.get("mint"))
        if network_id != "solana":
            exclusions["unsupported_network"] += 1
            continue
        entry_price = _positive(row.get("ref_price") or row.get("their_price"))
        if not mint or entry_price is None:
            exclusions["invalid_entry"] += 1
            continue
        key = (network_id, mint, signal_at.astimezone(timezone.utc).date().isoformat())
        deduped.setdefault(key, (signal_at, row, entry_price))

    episodes = []
    for (network_id, mint, _day), (signal_at, row, entry_price) in sorted(
        deduped.items(), key=lambda item: (item[1][0], item[0]),
    ):
        if lane == "legacy":
            quotes = _legacy_quotes(conn, mint, signal_at, cutoff)
            marks = _parse_marks(row.get("marks_json"))
        else:
            quotes = _v2_quotes(conn, network_id, mint, signal_at, cutoff)
            marks = []
        path_quotes, complete, coverage, expected = _dense_path(
            quotes, signal_at, period, lane=lane,
        )
        cap = _positive(row.get("their_market_cap"))
        if cap is None:
            exclusions["missing_market_cap"] += 1
        coin_feed = [feed for feed in all_feed if str(feed.get("network_id") or "") == network_id
                     and _mint(feed.get("mint")) == mint]
        coin_theses = [thesis for thesis in all_theses if str(thesis.get("network_id") or "") == network_id
                       and _mint(thesis.get("mint")) == mint]
        first_liquidity = next((_positive(quote.get("liquidity")) for quote in path_quotes
                                if _positive(quote.get("liquidity")) is not None), None)
        source_id = row.get("source_observation_id") or row.get("upstream_event_id") or row.get("id")
        episodes.append({
            "episode_id": _episode_id(lane, network_id, mint, signal_at),
            "lane": lane, "network_id": network_id, "mint": mint,
            "signal_at": signal_at.isoformat(), "entry_price": entry_price,
            "entry_price_source": "legacy_outcome_ref" if lane == "legacy" else "v2_feed_ref",
            "entry_market_cap": cap, "entry_liquidity": first_liquidity,
            "source_observation_id": source_id,
            "source_user_id": row.get("source_user_id"),
            "their_size_usd": _positive(row.get("their_size_usd")),
            "dense_complete": complete, "dense_coverage": coverage,
            "expected_quote_count": expected, "quotes": path_quotes,
            "feed_rows": coin_feed, "thesis_rows": coin_theses,
            "sparse_marks": marks, "sparse_peak_pct": _number(row.get("sparse_peak_pct")),
            "legacy_ungraded_quotes": sum(bool(quote.get("legacy_ungraded")) for quote in quotes),
            "primary_eligible": cap is not None and cap < 5_000_000,
        })
    manifest = {
        "lane": lane, "asof": cutoff.isoformat(), "capture_period_s": period,
        "source_rows": len(sources), "episode_count": len(episodes),
        "source_first_observed_at": min(
            (_time(row.get("signal_at") or row.get("observed_at") or row.get("buy_ts")).isoformat()
             for row in sources), default=None,
        ),
        "source_last_observed_at": max(
            (_time(row.get("signal_at") or row.get("observed_at") or row.get("buy_ts")).isoformat()
             for row in sources), default=None,
        ),
        "deduplicated_sightings": max(0, len(anchors) - len(deduped)),
        "exclusions": exclusions,
    }
    if lane == "legacy":
        manifest["legacy_clock_derived"] = legacy_clock_derived
        manifest["open_partial_outcomes"] = open_partial_outcomes
    if lane == "v2":
        manifest["budget_skipped"] = int(conn.execute(
            "SELECT COALESCE(SUM(budget_skipped_count),0) FROM memecoin_quote_sweeps_v2 "
            "WHERE capture_version=2 AND observed_at<=?", (cutoff.isoformat(),),
        ).fetchone()[0] or 0)
    return episodes, manifest


def _initial_return(episode):
    signal = _time(episode["signal_at"])
    deadline = signal + timedelta(minutes=15)
    eligible = [quote for quote in episode["quotes"] if _time(quote["observed_at"]) <= deadline]
    if eligible:
        return (float(eligible[-1]["price"]) / episode["entry_price"] - 1.0) * 100.0
    marks = [mark for mark in episode.get("sparse_marks") or [] if mark[1] <= 900]
    if marks:
        return (marks[-1][2] / episode["entry_price"] - 1.0) * 100.0
    return None


def _public_episode(episode, metrics, archetype, observed_shape, conviction, thesis, control_id=None):
    return {
        "episode_id": episode["episode_id"], "lane": episode["lane"],
        "network_id": episode["network_id"], "mint": episode["mint"],
        "signal_at": episode["signal_at"], "source_observation_id": episode["source_observation_id"],
        "source_user_id": episode["source_user_id"], "entry_price": episode["entry_price"],
        "entry_price_source": episode["entry_price_source"],
        "entry_market_cap": episode["entry_market_cap"],
        "entry_liquidity": episode["entry_liquidity"],
        "market_cap_band": _market_cap_band(episode["entry_market_cap"]),
        "liquidity_band": _liquidity_band(episode["entry_liquidity"]),
        "initial_15m_return_pct": _initial_return(episode),
        "initial_15m_return_band": _return_band(_initial_return(episode)),
        "primary_eligible": episode["primary_eligible"],
        "dense_complete": episode["dense_complete"],
        "dense_coverage": episode["dense_coverage"],
        "path_status": "dense_complete" if episode["dense_complete"] else "sparse_only",
        "archetype": archetype,
        "observed_shape": observed_shape,
        "observation_count": metrics.observation_count,
        "max_gain_pct": metrics.max_gain_pct,
        "max_drawdown_pct": metrics.max_drawdown_pct,
        "terminal_return_pct": metrics.terminal_return_pct,
        "max_peak_to_later_drawdown_pct": metrics.max_peak_to_later_drawdown_pct,
        "recovery_from_trough_pct": metrics.recovery_from_trough_pct,
        "reclaimed_entry": metrics.reclaimed_entry,
        "new_high_after_trough": metrics.new_high_after_trough,
        "runner_then_deep_recovery_new_high": (
            observed_shape == "observed_immediate_runner"
            and metrics.drew_down_30
            and metrics.reclaimed_entry
            and metrics.new_high_after_trough
        ),
        "sparse_peak_pct": episode["sparse_peak_pct"],
        "legacy_ungraded_quotes": episode["legacy_ungraded_quotes"],
        "conviction_json": json.dumps(conviction, sort_keys=True, separators=(",", ":")),
        "thesis_count": thesis["count"], "first_thesis_at": thesis["first_at"],
        "first_thesis_stance": thesis["first_stance"],
        "thesis_lag_from_first_buy_s": thesis["lag_from_first_buy_s"],
        "thesis_lag_from_poster_buy_s": thesis["lag_from_poster_buy_s"],
        "thesis_forward_return_pct": thesis["forward_return_pct"],
        "control_episode_id": control_id,
    }


def run_study(
    conn,
    lane: str,
    asof,
    *,
    capture_period_s: int | None = None,
    legacy_clock_offset_s: float = 0,
    entry_bps: float = 100,
    exit_bps: float = 100,
):
    for name, value in (("entry_bps", entry_bps), ("exit_bps", exit_bps)):
        parsed = _number(value)
        if parsed is None or parsed < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    cutoff = _time(asof)
    episodes, source_manifest = load_lane(
        conn, lane, cutoff.isoformat(), capture_period_s=capture_period_s,
        legacy_clock_offset_s=legacy_clock_offset_s,
    )
    analyzed = []
    policy_rows = []
    for episode in episodes:
        metrics = analyze_path(
            episode["entry_price"], episode["signal_at"], episode["quotes"],
            complete=episode["dense_complete"],
        )
        archetype = classify_path(metrics)
        observed_shape = classify_observed_path(metrics)
        conviction = conviction_windows(
            episode["signal_at"], episode["feed_rows"], lane=lane,
            network_id=episode["network_id"], mint=episode["mint"],
        )
        thesis = thesis_context(
            episode["signal_at"], episode["thesis_rows"], episode["feed_rows"], episode["quotes"],
            network_id=episode["network_id"], mint=episode["mint"], asof=cutoff.isoformat(),
        )
        public = _public_episode(episode, metrics, archetype, observed_shape, conviction, thesis)
        analyzed.append(public)
        if not episode["primary_eligible"] or not episode["dense_complete"]:
            continue
        path = {
            "path_id": episode["episode_id"], "network_id": episode["network_id"],
            "mint": episode["mint"], "signal_at": episode["signal_at"],
            "entry_price": episode["entry_price"], "notional": 100,
            "observations": episode["quotes"], "complete": True,
        }
        baseline = simulate_policy(
            path, "tp60_sl50x4_24h", entry_bps=entry_bps, exit_bps=exit_bps,
        )
        reentry = None
        if baseline.valid and baseline.reason == "stop":
            reentry = find_fresh_conviction_reentry(
                baseline.exited_at, episode["source_user_id"], episode["feed_rows"], episode["quotes"],
                network_id=episode["network_id"], mint=episode["mint"],
            ) if lane == "v2" else None
        for policy_name in POLICIES:
            result = simulate_policy(
                path, policy_name, entry_bps=entry_bps, exit_bps=exit_bps,
                reentry=reentry if policy_name == "stop_then_fresh_conviction_reentry" else None,
            )
            row = asdict(result)
            row.update({
                "episode_id": episode["episode_id"], "lane": lane,
                "network_id": episode["network_id"], "mint": episode["mint"],
                "archetype": archetype,
                "market_cap_band": _market_cap_band(episode["entry_market_cap"]),
            })
            row["legs"] = json.dumps(row["legs"], sort_keys=True, separators=(",", ":"))
            policy_rows.append(row)

    eligible_recoveries = 0
    matched_controls = 0
    for row in analyzed:
        if not row["archetype"].startswith("deep_recovery"):
            continue
        eligible_recoveries += 1
        control = match_past_control(row, analyzed)
        row["control_episode_id"] = control["episode_id"] if control else None
        matched_controls += control is not None

    ranking = rank_policies(policy_rows, baseline_name="tp60_sl50x4_24h") if policy_rows else {
        "status": "blocked_no_complete_paths", "winner": None, "policies": {},
    }
    grouped_rows = {}
    for row in policy_rows:
        key = f"{row['archetype']}|{row['market_cap_band']}"
        grouped_rows.setdefault(key, []).append(row)
    grouped_rankings = {
        key: rank_policies(rows, baseline_name="tp60_sl50x4_24h")
        for key, rows in sorted(grouped_rows.items())
    }
    claims = {
        "lane_role": "hypothesis_discovery_only" if lane == "legacy" else "forward_validation",
        "policy_ranking": ranking["status"],
        "winner": ranking["winner"],
        "exit_search_gate": "unchanged_21d_200paths_100coins_90pct_coverage",
    }
    manifest = {
        **source_manifest,
        "parameters": {
            "entry_bps": float(entry_bps), "exit_bps": float(exit_bps),
            "legacy_clock_offset_s": float(legacy_clock_offset_s),
        },
        "complete_primary_paths": sum(
            row["dense_complete"] and row["primary_eligible"] for row in analyzed
        ),
        "policy_rows": len(policy_rows), "claims": claims,
        "overlap_patterns": {
            "quote_observed_episodes": sum(row["observation_count"] > 0 for row in analyzed),
            "runner_then_deep_recovery_new_high": sum(
                row["runner_then_deep_recovery_new_high"] for row in analyzed
            ),
        },
        "control_matches": {
            "eligible_recoveries": eligible_recoveries, "matched": matched_controls,
        },
        "rankings": {"overall": ranking, "by_archetype_market_cap": grouped_rankings},
    }
    evidence = {"manifest": manifest, "episodes": analyzed, "policy_rows": policy_rows}
    manifest["evidence_hash"] = _canonical_hash(evidence)
    return {"manifest": manifest, "episodes": analyzed, "policy_rows": policy_rows}


def _csv_bytes(rows, default_fields):
    import io

    fields = sorted({key for row in rows for key in row}) if rows else list(default_fields)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows([{key: row.get(key) for key in fields} for row in rows])
    return buffer.getvalue().encode()


def _report(result) -> str:
    manifest = result["manifest"]
    archetypes = {}
    observed_shapes = {}
    for row in result["episodes"]:
        archetypes[row["archetype"]] = archetypes.get(row["archetype"], 0) + 1
        observed_shapes[row["observed_shape"]] = observed_shapes.get(row["observed_shape"], 0) + 1
    lines = [
        f"# Memecoin pattern study - {manifest['lane']} - {manifest['asof']}", "",
        f"Evidence hash: `{manifest['evidence_hash']}`", "",
        f"Episodes: {manifest['episode_count']}; complete primary paths: "
        f"{manifest['complete_primary_paths']}; policy rows: {manifest['policy_rows']}.", "",
        f"Matched recovery controls: {manifest['control_matches']['matched']}/"
        f"{manifest['control_matches']['eligible_recoveries']}.", "",
        "Runner then deep-drawdown recovery to a new high: "
        f"{manifest['overlap_patterns']['runner_then_deep_recovery_new_high']}/"
        f"{manifest['overlap_patterns']['quote_observed_episodes']} quote-observed episode(s).", "",
        "## Claim Boundary", "",
        ("Legacy results discover hypotheses only." if manifest["lane"] == "legacy"
         else "V2 results are forward evidence and remain subject to promotion gates."),
        "The existing exit-search gate remains 21 days, 200 complete paths, 100 coins, and 90% coverage.", "",
        "## Archetypes", "",
        "| archetype | episodes |", "|---|---:|",
    ]
    lines.extend(f"| {name} | {count} |" for name, count in sorted(archetypes.items()))
    lines.extend(["", "## Provisional Observed Shapes", "",
                  "Partial paths below are descriptive and cannot enter policy simulation.", "",
                  "| observed shape | episodes |", "|---|---:|"])
    lines.extend(f"| {name} | {count} |" for name, count in sorted(observed_shapes.items()))
    lines.extend(["", "## Policy Read", ""])
    if manifest["claims"]["winner"]:
        lines.append(f"Eligible policy winner: `{manifest['claims']['winner']}`.")
    else:
        lines.append(f"No policy winner. Status: `{manifest['claims']['policy_ranking']}`.")
    lines.extend(["", "## Exclusions", "", "| reason | rows |", "|---|---:|"])
    lines.extend(
        f"| {name} | {count} |" for name, count in sorted(manifest["exclusions"].items())
    )
    return "\n".join(lines) + "\n"


def write_artifacts(result, output_dir):
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = dict(result["manifest"])
    stamp = _time(manifest["asof"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"memecoin_pattern_{manifest['lane']}_{stamp}"
    episodes_bytes = _csv_bytes(result["episodes"], ("episode_id", "lane", "mint"))
    policies_bytes = _csv_bytes(
        result["policy_rows"], ("episode_id", "policy_name", "valid", "net_return"),
    )
    report_bytes = _report(result).encode()
    artifact_hashes = {
        "episodes_csv": hashlib.sha256(episodes_bytes).hexdigest(),
        "policies_csv": hashlib.sha256(policies_bytes).hexdigest(),
        "report_md": hashlib.sha256(report_bytes).hexdigest(),
    }
    manifest["artifact_hashes"] = artifact_hashes
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    paths = {
        "manifest_json": output / f"{stem}_manifest.json",
        "episodes_csv": output / f"{stem}_episodes.csv",
        "policies_csv": output / f"{stem}_policies.csv",
        "report_md": output / f"{stem}.md",
    }
    for key, payload in (
        ("manifest_json", manifest_bytes), ("episodes_csv", episodes_bytes),
        ("policies_csv", policies_bytes), ("report_md", report_bytes),
    ):
        paths[key].write_bytes(payload)
    return {key: str(path) for key, path in paths.items()}


def _open_readonly(db_path):
    path = Path(db_path).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="sniper_tracker.db")
    parser.add_argument("--lane", choices=("legacy", "v2"), required=True)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--capture-period-s", type=int)
    parser.add_argument("--legacy-clock-offset-s", type=float, default=0)
    parser.add_argument("--entry-bps", type=float, default=100)
    parser.add_argument("--exit-bps", type=float, default=100)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    output_path = Path(args.output_dir).resolve()
    if output_path == db_path:
        raise SystemExit("output directory cannot overwrite the source database")
    with _open_readonly(db_path) as conn:
        result = run_study(
            conn, args.lane, args.asof, capture_period_s=args.capture_period_s,
            legacy_clock_offset_s=args.legacy_clock_offset_s,
            entry_bps=args.entry_bps, exit_bps=args.exit_bps,
        )
    paths = write_artifacts(result, output_path)
    print(json.dumps({"evidence_hash": result["manifest"]["evidence_hash"], **paths}, indent=2))



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('run_study', '_report', 'write_artifacts', 'main'))

if __name__ == "__main__":
    main()
