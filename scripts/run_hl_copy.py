#!/usr/bin/env python3
"""24/7 mirror copy-trading loop — 'do exactly what the target wallet does', on paper.

Each cycle: for every active copy target (picked in the HL Analyzer), fetch their current
positions and reconcile a paper book to match — open a mirror at the CURRENT mark when they
hold something we don't, close it (net of 9bps) when they exit, hold when matched. The book's
realized + unrealized bps is realistic-follower PnL (we enter lagged, at the mark we see).

Public read-only data; paper only; no orders, no keys. Not financial advice."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.markets import hyperliquid_api as HL  # noqa: E402
from sniper.hyperlive import follow_sim as FS  # noqa: E402
from sniper.hyperlive import funding as FUND  # noqa: E402
from sniper.hyperlive import copy_sim as CS  # noqa: E402
from sniper.hyperlive import copy_capture as CC  # noqa: E402
from sniper.hyperlive import copy_filters as CF  # noqa: E402
from sniper.hyperlive import intel_store as IS  # noqa: E402
from sniper.hyperlive import source_fills as SOURCE  # noqa: E402
from sniper.hyperlive.altshort_evidence import causal_regime_features  # noqa: E402
from sniper.hyperlive.altshort_universe import lookup_coin, strategy_bucket  # noqa: E402
from sniper.util.clock import real_utc_now  # noqa: E402
from sniper.services.memecoin_runner import acquire_run_lock

HL_INFO = "https://api.hyperliquid.xyz/info"
DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"
_LATEST_FUNDING_RATES: dict[str, float] = {}
_LAST_CYCLE_STATS: dict[str, int] = {}


def _marks() -> dict:
    global _LATEST_FUNDING_RATES
    meta, ctxs = HL._post_info({"type": "metaAndAssetCtxs"})
    out = {}
    funding_rates = {}
    for u, c in zip(meta["universe"], ctxs):
        if u.get("isDelisted"):
            continue
        symbol = str(u.get("name") or "").strip()
        if not symbol:
            continue
        px = c.get("markPx") or c.get("midPx")
        if px:
            out[symbol] = float(px)
        try:
            funding_rates[symbol] = float(c["funding"])
        except (KeyError, TypeError, ValueError):
            pass
    _LATEST_FUNDING_RATES = funding_rates
    return out


def _provider_mark(marks: dict, coin: str) -> tuple[str, object] | None:
    try:
        return lookup_coin(marks, coin)
    except ValueError:
        return None


def _mark(marks: dict, coin: str):
    matched = _provider_mark(marks, coin)
    return matched[1] if matched is not None else None


def _l2(coin: str):
    """L2 book for capture. Fails OPEN (None) — capture is observational, never blocks a trade."""
    try:
        return HL.fetch_l2_book(coin)
    except Exception:
        return None


def _funding(coin: str, start_ms: int, end_ms: int):
    try:
        return HL.fetch_funding_history(coin, start_ms, end_ms)
    except Exception:
        return None


def _gross_bps(side: str, entry_mark, mark) -> float | None:
    try:
        e, m = float(entry_mark), float(mark)
    except (TypeError, ValueError):
        return None
    if e <= 0 or m <= 0:
        return None
    g = (m - e) / e * 1e4
    return round(-g if side == "short" else g, 2)


def _coinside_counts(conn) -> dict:
    """{(COIN, side): open_count} across the whole book — for the consensus/crowding filter."""
    counts: dict = {}
    for r in conn.execute("SELECT coin, side FROM hl_copy_book WHERE status='open'"):
        k = (str(r["coin"]).upper(), r["side"])
        counts[k] = counts.get(k, 0) + 1
    return counts


def _arm_policy(arm: dict) -> dict:
    try:
        return json.loads(arm["policy_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}


def _git_state(*, root: Path | None = None, pinned_code_sha: str | None = None,
               code_paths: list[str] | None = None) -> tuple[str, bool]:
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True,
        capture_output=True,
    ).stdout.strip()
    status_command = ["git", "status", "--porcelain", "--untracked-files=no"]
    if code_paths:
        status_command.extend(["--", *code_paths])
    dirty = bool(subprocess.run(
        status_command,
        cwd=root, check=True, text=True,
        capture_output=True,
    ).stdout.strip())
    code_sha = head_sha
    if pinned_code_sha and code_paths:
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{pinned_code_sha}^{{commit}}"], cwd=root,
            capture_output=True,
        ).returncode == 0
        unchanged = exists and subprocess.run(
            ["git", "diff", "--quiet", pinned_code_sha, "--", *code_paths], cwd=root,
        ).returncode == 0
        if unchanged:
            code_sha = pinned_code_sha
    return code_sha, dirty


def _now_ms() -> int:
    """Corrected UTC for immutable epochs and external-market timestamps."""
    return int(real_utc_now() * 1000)


def _load_or_create_arm(conn, manifest_path: Path, *, code_sha: str,
                        dirty: bool, now_ms: int) -> dict:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("code_sha") != code_sha:
        raise ValueError("code_sha_mismatch")
    if dirty:
        raise ValueError("dirty_tree")
    if not (manifest.get("policy") or {}).get("alt_short_only"):
        raise ValueError("arm_policy_not_alt_short_only")
    if not manifest.get("targets"):
        raise ValueError("arm_targets_empty")
    evidence_arm = IS.find_compatible_altshort_arm(conn, manifest)
    if evidence_arm is None:
        evidence_arm = IS.create_altshort_arm(
            conn, manifest, activated_ts_ms=int(now_ms),
            code_sha=code_sha, created_ts_ms=int(now_ms),
        )
    return {**evidence_arm, "deployment_arm_id": str(manifest["arm_id"])}


def _heartbeat_payload(*, now_ms: int, code_sha: str, dirty: bool, arm: dict | None,
                       policy: CF.EntryPolicy, summary: dict, cycle: dict | None = None,
                       source: dict | None = None) -> dict:
    payload = {
        "ts_ms": int(now_ms),
        "code_sha": code_sha,
        "dirty": bool(dirty),
        "alt_short_only": bool(policy.alt_short_only),
        "open": int(summary.get("open") or 0),
        "closed": int(summary.get("closed") or 0),
        "depth_capture_format_version": 2,
        "depth_capture_scope": "provider_returned_snapshot",
    }
    if arm is not None:
        payload.update({
            "arm_id": arm["arm_id"],
            "evidence_arm_id": arm["arm_id"],
            "deployment_arm_id": arm.get("deployment_arm_id", arm["arm_id"]),
            "activated_ts_ms": int(arm["activated_ts_ms"]),
            "policy_hash": arm["policy_hash"],
            "targets": json.loads(arm["targets_json"]),
            "taxonomy_version": arm["taxonomy_version"],
        })
    if cycle is not None:
        payload["cycle"] = {key: int(value) for key, value in cycle.items()}
    if source is not None:
        payload["source_fills"] = dict(source)
    return payload


def _write_heartbeat(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _source_health(conn, collector, *, wallets, now_ms: int) -> dict | None:
    if collector is None:
        return None
    status = collector.status()
    last_event = status.get("last_event_ts_ms")
    status["last_event_age_ms"] = (
        max(0, int(now_ms) - int(last_event)) if last_event is not None else None
    )
    status["unmatched_count"] = IS.copy_source_unmatched_count(conn, wallets)
    status["gap_count"] = IS.copy_source_gap_count(conn)
    status["interval_complete"] = status["gap_count"] == 0
    return status


def _drain_source(conn, collector, *, now_ms: int) -> None:
    if collector is None:
        return
    source_rows = collector.drain()
    if source_rows:
        IS.record_copy_source_events(conn, source_rows)
    gaps = collector.drain_gaps()
    if gaps:
        IS.record_copy_source_gaps(conn, gaps, persisted_ts_ms=now_ms)


def _arm_entry_snapshot(conn, arm: dict, *, coin: str, marks: dict, now_ms: int) -> tuple[dict, dict]:
    universe = sorted(symbol for symbol in marks if strategy_bucket(symbol) == "alt")
    bars = IS.load_altshort_regime_bars(
        conn, start_ts_ms=now_ms - 14 * 86_400_000, end_ts_ms=now_ms
    )
    regime = causal_regime_features(
        bars, decision_ts_ms=now_ms, universe=universe
    )
    policy = _arm_policy(arm)
    trailing = regime["basket_return_bps"].get(str(72 * 3_600_000))
    if trailing is None:
        label = "unknown"
    elif trailing <= float(policy.get("bearish_threshold_bps", -500.0)):
        label = "bearish"
    elif trailing >= float(policy.get("bullish_threshold_bps", 500.0)):
        label = "bullish"
    else:
        label = "neutral"
    regime["label"] = label
    benchmark = {
        symbol: float(mark)
        for symbol, mark in sorted(marks.items())
        if symbol.upper() != coin.upper() and strategy_bucket(symbol) == "alt" and float(mark) > 0
    }
    return regime, benchmark


def _arm_benchmark_close(observation: dict, *, marks: dict, cost_bps: float,
                         funding_bps_by_coin: dict[str, float] | None = None,
                         min_coverage: float = 0.8) -> dict:
    entries = json.loads(observation["benchmark_entry_json"])
    returns = []
    for coin, entry in entries.items():
        mark = _mark(marks, coin)
        if mark and float(entry) > 0 and (
            funding_bps_by_coin is None or coin in funding_bps_by_coin
        ):
            value = (float(entry) - float(mark)) / float(entry) * 10_000.0
            value += float((funding_bps_by_coin or {}).get(coin, 0.0))
            returns.append(value)
    coverage = len(returns) / len(entries) if entries else 0.0
    result = {
        "eligible_n": len(entries), "covered_n": len(returns), "coverage": coverage,
        "funding_coverage": coverage if funding_bps_by_coin is not None else 0.0,
    }
    if coverage < min_coverage:
        return {**result, "valid": False, "reason": "insufficient_basket_coverage",
                "basket_net_bps": None}
    return {**result, "valid": True, "reason": "ok",
            "basket_net_bps": sum(returns) / len(returns) - float(cost_bps)}


def _funding_event_count(start_ms: int, end_ms: int) -> int:
    hour_ms = 3_600_000
    first = (int(start_ms) // hour_ms + 1) * hour_ms
    return max(0, (int(end_ms) - first) // hour_ms + 1) if first <= int(end_ms) else 0


def _arm_benchmark_funding(conn, observation: dict, *, exit_ts_ms: int) -> dict[str, float]:
    entries = json.loads(observation["benchmark_entry_json"])
    start_ms = int(observation["detect_ts_ms"])
    expected = _funding_event_count(start_ms, exit_ts_ms)
    if expected == 0:
        return {coin: 0.0 for coin in entries}
    bars = IS.load_altshort_regime_bars(
        conn, start_ts_ms=start_ms, end_ts_ms=exit_ts_ms
    )
    out = {}
    for coin in entries:
        rates = [
            float(row["funding_rate"])
            for row in bars.get(coin, [])
            if start_ms < int(row["ts_ms"]) <= exit_ts_ms
            and row.get("funding_rate") is not None
        ]
        if len(rates) >= expected:
            out[coin] = sum(rates) * 10_000.0  # a short receives positive funding
    return out


def _cycle(conn, now_ms: int, *, fetch_delay: float = 0.0,
           policy: "CF.EntryPolicy | None" = None,
           arm: dict | None = None, include_legacy: bool = True,
           source_collector=None) -> tuple[int, int]:
    global _LAST_CYCLE_STATS
    _LAST_CYCLE_STATS = {"targets_high": 0, "targets_legacy": 0, "targets_total": 0}
    policy = policy or CF.EntryPolicy()
    targets = IS.get_copy_targets(conn, active_only=False)
    if not targets:
        return 0, 0
    executable = {t["wallet"]: t for t in IS.get_executable_copy_targets(conn, now_ms=now_ms)}
    freeze_ts = IS.holdout_freeze_ts(conn)
    coinside_counts = _coinside_counts(conn) if policy.active else {}
    work = []
    arm_targets = set(json.loads(arm["targets_json"])) if arm is not None else set()
    for t in targets:
        w = t["wallet"]
        status = executable.get(w, {}).get("copy_status", {"eligible": False})
        book = {c["coin"]: {"side": c["side"], "entry_mark": c["entry_mark"], "id": c["id"],
                            "detect_ts_ms": c["detect_ts_ms"]}
                for c in IS.get_open_copies(conn, target=w)}
        high_priority = (arm is None or w in arm_targets or t.get("copy_tier") == "proven")
        frozen_member = arm is not None and w in arm_targets and bool(t.get("active"))
        if status["eligible"] or frozen_member or book:
            work.append((t, status, book, high_priority))
    if not work:
        return 0, 0
    global _LATEST_FUNDING_RATES
    _LATEST_FUNDING_RATES = {}
    marks = _marks()
    if arm is not None:
        IS.record_altshort_regime_marks(
            conn, marks, funding_rates=_LATEST_FUNDING_RATES, ts_ms=now_ms
        )
    opened = closed = 0
    fetched = 0
    high_fetched = legacy_fetched = 0
    for t, status, book, high_priority in work:
        w = t["wallet"]
        # Intra-hold MFE/MAE for every open mirror (cheap — uses marks, no extra API).
        for coin, b in book.items():
            g = _gross_bps(b["side"], b["entry_mark"], _mark(marks, coin))
            if g is not None:
                IS.update_copy_excursion(conn, b["id"], gross_bps=g)
            mark = _mark(marks, coin)
            if arm is not None and mark:
                IS.update_altshort_observation_mark(
                    conn, copy_id=b["id"], mark=mark, ts_ms=now_ms
                )
        frozen_member = arm is not None and w in arm_targets and bool(t.get("active"))
        can_open = high_priority and (status["eligible"] or frozen_member)
        if not high_priority and not include_legacy:
            continue
        if can_open or book:
            if fetch_delay > 0 and fetched > 0:      # spread per-target fetches; no burst at scale
                time.sleep(fetch_delay)
            fetched += 1
            if high_priority:
                high_fetched += 1
            else:
                legacy_fetched += 1
            try:
                tpos = FS.positions_map(HL.fetch_clearinghouse_state(w))
            except Exception as exc:
                print(f"copy target fetch failed wallet={w}: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                continue
            if source_collector is not None:
                _drain_source(conn, source_collector, now_ms=_now_ms())
        else:
            tpos = {}
        acts = CS.mirror_actions(tpos, {k: {"side": v["side"], "entry_mark": v["entry_mark"]}
                                        for k, v in book.items()}, marks)
        for cl in acts["close"]:
            b = book[cl["coin"]]
            provider_mark = _provider_mark(marks, cl["coin"])
            provider_coin = provider_mark[0] if provider_mark is not None else cl["coin"]
            exit_book = _l2(provider_coin)
            xcap = CC.exit_capture(exit_book, captured_ts_ms=_now_ms())
            funding_rows = _funding(provider_coin, b["detect_ts_ms"], now_ms)
            fund = FUND.funding_pnl(
                funding_rows, side=b["side"], entry_ts_ms=b["detect_ts_ms"],
                exit_ts_ms=now_ms, fetch_failed=funding_rows is None,
            )
            IS.close_copy(conn, b["id"], exit_mark=cl["exit_mark"], pnl_bps=cl["pnl_bps"],
                          ts_ms=now_ms, funding_pnl_bps=fund.value_bps,
                          funding_provenance_status=fund.status, **xcap)
            if arm is not None:
                observation = IS.get_altshort_observation(conn, copy_id=b["id"])
                if observation is not None:
                    benchmark = _arm_benchmark_close(
                        observation, marks=marks,
                        cost_bps=float(_arm_policy(arm).get("cost_bps", 0.0)),
                        funding_bps_by_coin=_arm_benchmark_funding(
                            conn, observation, exit_ts_ms=now_ms
                        ),
                    )
                    IS.close_altshort_observation(
                        conn, copy_id=b["id"], exit_mark=cl["exit_mark"],
                        exit_ts_ms=now_ms, benchmark=benchmark,
                        extra_cost_bps=max(
                            0.0,
                            float(_arm_policy(arm).get("cost_bps", CS.FEE_BPS))
                            - CS.FEE_BPS,
                        ),
                        funding_complete=fund.status in FUND.COMPLETE_STATUSES,
                    )
            closed += 1
        if can_open:
            # Open ONLY genuine entries detected while watching — never cold-start the target's
            # pre-existing book (that buys mid-run: cold-start median -44bps vs +7 for real entries).
            prior_state, prior_state_ts_ms = IS.get_copy_trader_state_snapshot(conn, w)
            first_sight = prior_state_ts_ms is None
            entries = [] if first_sight else FS.detect_entries(prior_state, tpos)
            held = {(coin, b["side"]) for coin, b in book.items()}
            for e in entries:
                coin, side = e["coin"], e["side"]
                if (coin, side) in held:
                    continue                                  # already mirroring this coin+side
                mark = _mark(marks, coin)
                if not mark:
                    continue
                tp = tpos.get(coin) or {}
                if policy.active:
                    tpx, szi = tp.get("entry_px"), tp.get("szi")
                    notional = abs(szi) * tpx if (szi and tpx) else None
                    lag = CC.lag_bps(side, mark, tpx)
                    consensus_n = coinside_counts.get((coin.upper(), side), 0) + 1
                    ok, _why = CF.should_open(coin=coin, side=side, lag_bps=lag,
                                              target_notional=notional,
                                              consensus_n=consensus_n, policy=policy)
                    if not ok:
                        continue
                decision_ts_ms = _now_ms()
                book_request_ts_ms = _now_ms()
                book_snapshot = _l2(coin)
                book_received_ts_ms = _now_ms()
                cap = CC.entry_capture(
                    side, mark, tp, book_snapshot, captured_ts_ms=book_received_ts_ms
                )
                source_match = {"status": "not_instrumented", "tids": []}
                if arm is not None and w in arm_targets:
                    events = IS.list_unmatched_copy_source_events(
                        conn, wallet=w, after_ts_ms=int(prior_state_ts_ms or 0),
                        before_ts_ms=decision_ts_ms,
                    )
                    prior = prior_state.get(coin) or {}
                    source_match = SOURCE.match_entry(
                        events, wallet=w, coin=coin, side=side,
                        prior_size=float(prior.get("szi") or 0.0),
                        current_size=float(tp.get("szi") or 0.0),
                        after_ts_ms=int(prior_state_ts_ms or 0), before_ts_ms=decision_ts_ms,
                    )
                    if (source_match["status"] == "matched" and
                            IS.copy_source_gap_overlaps(
                                conn, after_ts_ms=int(prior_state_ts_ms or 0),
                                before_ts_ms=decision_ts_ms,
                            )):
                        source_match["status"] = "matched_gap_censored"
                        source_match["reason"] = "queue_gap_overlaps_causal_interval"
                copy_id = IS.open_copy(
                    conn, w, coin, side, mark,
                    copy_tier=t.get("copy_tier") or status.get("copy_tier") or "unknown",
                    ts_ms=now_ms, is_holdout=CC.is_holdout(now_ms, freeze_ts),
                    source_event_ts_ms=source_match.get("source_event_ts_ms"),
                    source_received_ts_ms=source_match.get("source_received_ts_ms"),
                    decision_ts_ms=decision_ts_ms, book_request_ts_ms=book_request_ts_ms,
                    book_received_ts_ms=book_received_ts_ms,
                    mirror_open_ts_ms=None,
                    source_latency_status=source_match["status"],
                    source_event_tid=json.dumps(source_match.get("tids") or []), **cap
                )
                mirror_open_ts_ms = _now_ms()
                IS.stamp_copy_mirror_open(
                    conn, copy_id, committed_ts_ms=mirror_open_ts_ms
                )
                if source_match["status"] in {
                    "matched", "matched_recovery", "matched_gap_censored"
                }:
                    IS.match_copy_source_events(
                        conn, copy_id=copy_id, tids=source_match["tids"],
                        matched_ts_ms=mirror_open_ts_ms,
                    )
                if arm is not None:
                    try:
                        regime, benchmark = _arm_entry_snapshot(
                            conn, arm, coin=coin, marks=marks, now_ms=now_ms
                        )
                        IS.record_altshort_observation(
                            conn, arm_id=arm["arm_id"], copy_id=copy_id,
                            regime=regime, benchmark=benchmark,
                        )
                    except ValueError as exc:
                        print(f"alt-short arm excluded copy_id={copy_id}: {exc}", file=sys.stderr)
                opened += 1
        if can_open or book:
            IS.save_copy_trader_state(conn, w, tpos, ts_ms=now_ms)   # baseline for next cycle
    _LAST_CYCLE_STATS = {
        "targets_high": high_fetched, "targets_legacy": legacy_fetched,
        "targets_total": high_fetched + legacy_fetched,
    }
    return opened, closed


def _cycle_wait_seconds(interval_seconds: float, elapsed_seconds: float) -> float:
    return max(0.0, float(interval_seconds) - float(elapsed_seconds))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="24/7 paper mirror copy-trading loop")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--interval-seconds", type=int, default=120)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--fetch-delay", type=float, default=0.0,
                    help="seconds to sleep between per-target position fetches (spreads API load "
                         "when many targets are followed)")
    ap.add_argument("--legacy-interval-seconds", type=int, default=300,
                    help="slow-lane cadence for non-arm, non-proven draining books")
    ap.add_argument("--max-lag-bps", type=float, default=None,
                    help="skip an entry if |detection-lag| exceeds this (anti-chase; e.g. 150)")
    ap.add_argument("--min-conviction-usd", type=float, default=0.0,
                    help="skip an entry if the target's position notional is below this (e.g. 20000)")
    ap.add_argument("--min-consensus", type=int, default=0,
                    help="skip unless at least this many wallets hold the same coin+side")
    ap.add_argument("--max-consensus", type=int, default=None,
                    help="skip if more than this many wallets already crowd the coin+side (e.g. 3)")
    ap.add_argument("--alt-short-only", action="store_true",
                    help="only open non-major shorts — the cell the copy edge lives in "
                         "(BTC/ETH/SOL and all longs are skipped)")
    ap.add_argument("--arm-manifest", default=None,
                    help="immutable forward paper-arm manifest; requires --alt-short-only")
    ap.add_argument("--heartbeat-path", default="data/hl_copy.heartbeat")
    args = ap.parse_args(argv)
    # Single-instance lock held for the PROCESS LIFETIME, not just the watchdog invocation.
    # The watchdog matches a running process by "$CODE_ROOT/scripts/run_hl_copy.py $ARGS", and
    # CODE_ROOT resolves through .runtime/hyperliquid/current — so repointing that symlink makes
    # the running process stop matching and the next cron tick starts a SECOND one against the
    # same DB. Two release repoints on 2026-08-03 left three copies of this worker writing one
    # ledger (visible as 2-4 ticks/minute on a 1/minute cadence). Only a lock the runner itself
    # holds can prevent that; a watchdog-side flock serialises watchdogs, not workers.
    _run_lock = acquire_run_lock(os.getenv(
        "HL_COPY_RUN_LOCK", str(Path(args.db_path).resolve().parent / "hl_copy.run.lock")))
    if _run_lock is None:
        print("another run_hl_copy holds the run lock — refusing to start", file=sys.stderr)
        return 0

    policy = CF.EntryPolicy(max_lag_bps=args.max_lag_bps, min_conviction_usd=args.min_conviction_usd,
                            min_consensus=args.min_consensus, max_consensus=args.max_consensus,
                            alt_short_only=args.alt_short_only)
    if policy.active:
        print(f"entry filters active: {policy}", file=sys.stderr)
    conn = IS.connect(args.db_path)
    IS.ensure_schema(conn)
    pinned_code_sha = None
    if args.arm_manifest:
        pinned_code_sha = json.loads(
            Path(args.arm_manifest).read_text(encoding="utf-8")
        ).get("code_sha")
    code_sha, dirty = _git_state(
        pinned_code_sha=pinned_code_sha,
        code_paths=["scripts/run_hl_copy.py", "sniper/hyperlive", "sniper/markets/hyperliquid_api.py"],
    )
    arm = None
    source_collector = None
    source_targets = []
    if args.arm_manifest:
        if not args.alt_short_only:
            raise ValueError("--arm-manifest requires --alt-short-only")
        arm = _load_or_create_arm(
            conn, Path(args.arm_manifest), code_sha=code_sha, dirty=dirty,
            now_ms=_now_ms(),
        )
        try:
            source_targets = json.loads(arm["targets_json"])
            source_collector = SOURCE.SourceFillCollector(
                source_targets, now_ms=_now_ms,
                recovery_fetch=HL.fetch_user_fills_by_time,
                cursors=IS.copy_source_cursors(conn, source_targets),
            ).start()
        except Exception as exc:
            print(f"source-fill capture unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
    try:
        last_legacy_ms = None
        while True:
            loop_started = time.monotonic()
            try:
                now_ms = _now_ms()
                if source_collector is not None:
                    _drain_source(conn, source_collector, now_ms=now_ms)
                include_legacy = (last_legacy_ms is None or
                                  now_ms - last_legacy_ms >= args.legacy_interval_seconds * 1000)
                cycle_started = time.monotonic()
                o, c = _cycle(conn, now_ms, fetch_delay=args.fetch_delay,
                              policy=policy, arm=arm, include_legacy=include_legacy,
                              source_collector=source_collector)
                if include_legacy:
                    last_legacy_ms = now_ms
                s = IS.copy_summary(conn)
                finished_ms = _now_ms()
                cycle_stats = dict(_LAST_CYCLE_STATS)
                cycle_stats["duration_ms"] = round((time.monotonic() - cycle_started) * 1000)
                cycle_stats["legacy_sweep"] = int(include_legacy)
                _write_heartbeat(
                    Path(args.heartbeat_path),
                    _heartbeat_payload(now_ms=finished_ms, code_sha=code_sha, dirty=dirty,
                                       arm=arm, policy=policy, summary=s, cycle=cycle_stats,
                                       source=_source_health(
                                           conn, source_collector, wallets=source_targets,
                                           now_ms=finished_ms,
                                       )),
                )
                print(f"{time.strftime('%FT%TZ', time.gmtime())} copy: +{o} mirrored, {c} closed | "
                      f"open={s['open']} closed={s['closed']} wr={s['win_rate']:.0%} "
                      f"total={s['total_bps']:+.0f}bps realized={s['realized_sum_bps']:+.0f}bps"
                      if s["total_bps"] is not None else
                      f"open={s['open']} closed={s['closed']} total=unknown "
                      f"realized={s['realized_sum_bps']:+.0f}bps", file=sys.stderr)
            except Exception as e:
                print(f"copy cycle failed: {e}", file=sys.stderr)
            if args.once:
                break
            time.sleep(_cycle_wait_seconds(
                args.interval_seconds, time.monotonic() - loop_started
            ))
    finally:
        if source_collector is not None:
            source_collector.stop()
            _drain_source(conn, source_collector, now_ms=_now_ms())
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
