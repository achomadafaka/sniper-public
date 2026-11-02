#!/usr/bin/env python3
"""Run wallet-analysis-protocol-v2 end-to-end on a single wallet.

Usage:
    python3 scripts/run_wallet_protocol.py 0x84dbb71...
    python3 scripts/run_wallet_protocol.py 0x84dbb71... --no-iter --json
    python3 scripts/run_wallet_protocol.py 0x84dbb71... --persist

Outputs the §12 markdown report to stdout. Honest "data_insufficient" is a
valid result — the protocol's whole point is refusing to fabricate signal.

When ``--persist`` is set, writes one ``analyzer_runs`` row plus one
``wallet_layer5_decision`` row atomically per Phase 2 spec § 3.1; on
failure (fetch or analyze raising) writes a success=0 audit row only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Make repo importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlite3
import uuid

import db
from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.exec_profile_adapter import (
    build_execution_profile,
)
from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data  # progress_emitter kwarg wired below
from sniper.analysis.wallet_protocol.price_history_adapter import (
    build_price_history_fn,
)
from sniper.analysis.wallet_protocol.report import render_report
from sniper.analysis.wallet_protocol.memo_emitter import maybe_emit_memo
from sniper.analysis.wallet_protocol.replay import (
    build_fixture_closures,
    load_fetch_snapshot,
)
from sniper.analysis.wallet_protocol.run_events import JsonlEmitter
from sniper.analysis.wallet_protocol.tag_inference import (
    fetch_tag_lookups_from_db,
)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("wallet")
    ap.add_argument(
        "--db", default="sniper_tracker.db",
        help="SQLite path used for tag-grain lookups + peer baselines",
    )
    ap.add_argument("--n-iter", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--json", action="store_true",
        help="Output the ProtocolReport as JSON instead of markdown",
    )
    ap.add_argument(
        "--no-tags", action="store_true",
        help="Skip tag-grain lookups (run without H5/FH4 tag inference)",
    )
    ap.add_argument(
        "--no-peers", action="store_true",
        help="Skip peer-baseline lookup (forces every H5 grain to inference_not_run)",
    )
    ap.add_argument(
        "--no-exec", action="store_true",
        help="Skip execution_profile (forces H4 entry-timing to underpowered)",
    )
    ap.add_argument(
        "--no-history", action="store_true",
        help="Skip price_history_fn (forces H6 exit-quality to underpowered)",
    )
    ap.add_argument(
        "--max-exec-trades", type=int, default=500,
        help="Per-fill price-history fetch cap when computing execution_profile",
    )
    ap.add_argument(
        "--archetype", default=None,
        help="Layer-1 archetype label to feed the execution_profile gate "
             "(e.g. two_sided_inventory_trader). When omitted, the CLI runs "
             "wallet_archetype_extractor inline unless --no-archetype is set.",
    )
    ap.add_argument(
        "--no-archetype", action="store_true",
        help="Skip archetype detection. The execution_profile gate's "
             "LAYER1_TERMINAL check becomes a no-op (insufficient_activity "
             "still works).",
    )
    ap.add_argument(
        "--force-exec", action="store_true",
        help="Bypass every execution_profile gate (calibration only). "
             "H4 verdicts produced under this flag are not valid for "
             "LAYER1_TERMINAL archetypes — see C4 amendment.",
    )
    ap.add_argument(
        "--bounded-history", action="store_true",
        help="Opt OUT of full-history walking. When set, /activity is fetched "
             "in a single window (most-recent ~3000 rows). Default behavior is "
             "walk_full_history=True (spec A.2). Use this flag only when a fast "
             "partial run is explicitly wanted.",
    )
    ap.add_argument(
        "--full-history", action="store_true",
        help="(Deprecated; full-history is now the default.) Walk /activity "
             "windows backward to recover history beyond the 3000-row offset cap. "
             "No-op when the default walk_full_history=True is already active.",
    )
    ap.add_argument(
        "--capital-ledger", action="store_true",
        help="Fetch onchain capital ledger (SPLIT/MERGE/REDEMPTION via Goldsky subgraph). "
             "Adds ~4s for large wallets. Required for ±2%% accounting tie.",
    )
    ap.add_argument(
        "--persist", action="store_true",
        help="Write the run + decision into the SNIPER_DB_PATH database. "
             "On failure, writes a success=0 analyzer_runs audit row only.",
    )
    ap.add_argument(
        "--no-persist", action="store_true",
        help="Hard override of --persist. If both flags are passed, the "
             "command exits 2 without fetching, analyzing, or writing. "
             "Used by replay scripts to assert no DB writes occur.",
    )
    ap.add_argument(
        "--from-fixture",
        default=None,
        help="Path to a JSON fetch snapshot (see scripts/record_fetch_snapshot.py). "
             "When set, the live fetch is skipped and WalletData is loaded from "
             "the fixture. Required for deterministic replay; --capital-ledger "
             "and --full-history are ignored in this mode (the fixture already "
             "encodes them).",
    )
    ap.add_argument(
        "--run-log-dir",
        default=None,
        help="Optional directory for per-run JSONL logs: <dir>/<wallet>/<run_id>.jsonl",
    )
    ap.add_argument(
        "--persist-memo",
        action="store_true",
        help="Opt in to Supabase brain memo emission after a successful run.",
    )
    ap.add_argument(
        "--persist-flipper-grains",
        action="store_true",
        help="Opt in to writing flipper-branch tag-grain rows into Supabase "
             "wallet_flipper_tag_grain (Phase 1.2 of the flipper-inference "
             "roadmap). Default off. Requires the Phase 1.1 schema migration "
             "(migrations/supabase_021_wallet_flipper_tag_grain.sql) to be "
             "applied to the live project.",
    )
    ap.add_argument(
        "--with-flip-roi-baseline",
        action="store_true",
        help="Build a Supabase-backed flip-ROI peer baseline fn from "
             "wallet_flipper_tag_grain and pass it to FH4 inference "
             "(Phase 2). Default off. Requires SUPABASE_URL + "
             "SUPABASE_SERVICE_KEY (or ANON) in env or .env. Without this "
             "flag FH4 returns inference_not_run regardless of peer "
             "population.",
    )
    ap.add_argument(
        "--analyst-fast",
        action="store_true",
        dest="analyst_fast",
        default=False,
        help="Bounded review preset. Equivalent to --no-history --no-exec "
             "with capital_ledger disabled. Cannot combine with "
             "--capital-ledger (the preset wins with a stderr warning). "
             "Use for first-pass triage on high-volume wallets when you "
             "only need Position Builder + Completeness + branches. "
             "Stamps report.data_quality_flags with "
             "ANALYST_FAST_MODE_OMITTED:price_history,exec_attribution,"
             "capital_ledger so downstream consumers can distinguish "
             "operator-bounded runs from genuinely missing surfaces.",
    )
    ap.add_argument(
        "--triage-context-json",
        dest="triage_context_json",
        default=None,
        help="Operator-supplied JSON describing why this wallet was "
             "analyzed (e.g. leaderboard rank/period/PnL at capture). "
             "Stamped verbatim onto report.triage_context. Recommended "
             "shape: {\"source\":\"polymarket_public_leaderboard\","
             "\"period\":\"MONTH\",\"rank\":3,\"lb_pnl_at_capture\":"
             "1760000,\"fetched_at\":1747700000}. Malformed JSON exits "
             "2 before any analysis runs.",
    )
    return ap


_EXTRACTOR_FEATURE_KEYS = (
    ("trade_shape", "usdc_size_cv"),
    ("trade_shape", "top_condition_fill_share"),
    ("redeems", "redeem_ratio"),
    ("domain_tags", "primary_tag_share"),
    ("returns", "pnl_cv_abs"),
)


def _build_extractor_features_summary(features: dict) -> dict | None:
    """Edge Surface v1 — distill the small subset of `features` we render.

    Pulls a flat dict with keys (usdc_size_cv, top_condition_fill_share,
    redeem_ratio, domain_concentration, pnl_cv_abs). Returns None when none
    of the source paths are populated.
    """
    if not isinstance(features, dict):
        return None
    summary: dict = {}
    for parent, key in _EXTRACTOR_FEATURE_KEYS:
        block = features.get(parent)
        if not isinstance(block, dict):
            continue
        if key not in block:
            continue
        # `domain_tags.primary_tag_share` is exposed as `domain_concentration`.
        out_key = "domain_concentration" if key == "primary_tag_share" else key
        summary[out_key] = block[key]
    return summary or None


def _replay_args_json(args) -> str:
    """Capture the full set of replay-relevant flags into args_json.

    Read by ``scripts/replay_wallet_run.py`` to reconstruct argv. Fields
    whose default would invalidate replay (seed, n_iter, fixture path,
    every gate-disabling flag) are recorded explicitly.
    """
    return json.dumps({
        "wallet": args.wallet.lower(),
        "from_fixture": args.from_fixture,
        "seed": args.seed,
        "n_iter": args.n_iter,
        "no_tags": bool(args.no_tags),
        "no_peers": bool(args.no_peers),
        "no_exec": bool(args.no_exec),
        "no_history": bool(args.no_history),
        "no_archetype": bool(args.no_archetype),
        "force_exec": bool(args.force_exec),
        "max_exec_trades": args.max_exec_trades,
        "archetype": args.archetype,
        "capital_ledger": bool(args.capital_ledger),
        "full_history": not bool(getattr(args, "bounded_history", False)),
        "analyst_fast": bool(getattr(args, "analyst_fast", False)),
        # G3 — raw JSON string preserved verbatim for replay round-trip.
        # Parsing happens at main() time; we never re-serialize a dict
        # here because key order across Python versions isn't guaranteed.
        "triage_context_json": getattr(args, "triage_context_json", None),
    }, sort_keys=True)


def _persist_failed_run_helper(args, *, started: int, exc: Exception) -> None:
    """Write a success=0 analyzer_runs audit row. Does not re-raise."""
    from sniper.analysis.wallet_protocol.persistence import persist_failed_run

    db_path = os.getenv("SNIPER_DB_PATH", "sniper_tracker.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        finished = int(time.time())
        persist_failed_run(
            conn,
            wallet=args.wallet.lower(),
            started_ts=started,
            finished_ts=finished,
            duration_ms=int((finished - started) * 1000),
            error_class=type(exc).__name__,
            error_message=str(exc),
            invocation_source="cli",
            args_json=_replay_args_json(args),
            fetch_quality_flags_json="[]",
            activity_rows_fetched=None,
            activity_cap_hit=None,
            subgraph_fallback_used=False,
        )
    finally:
        conn.close()


def _persist_successful_run_helper(args, *, started: int, report) -> None:
    """Write atomic analyzer_runs + wallet_layer5_decision rows."""
    from sniper.analysis.wallet_protocol.persistence import (
        persist_successful_run_with_decision,
    )
    from sniper.analysis.wallet_protocol.wallet_layer5_mapping import (
        map_protocol_report_to_decision,
    )

    db_path = os.getenv("SNIPER_DB_PATH", "sniper_tracker.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        decision = map_protocol_report_to_decision(report)
        finished = int(time.time())
        persist_successful_run_with_decision(
            conn,
            pr=report,
            decision=decision,
            started_ts=started,
            finished_ts=finished,
            duration_ms=int((finished - started) * 1000),
            invocation_source="cli",
            args_json=_replay_args_json(args),
            fetch_quality_flags_json=json.dumps(
                getattr(report, "fetch_quality_flags", []) or []
            ),
            activity_rows_fetched=report.activity_rows_fetched,
            activity_cap_hit=report.activity_cap_hit,
            subgraph_fallback_used=bool(
                getattr(report, "subgraph_fallback_used", False)
            ),
            report_markdown=render_report(report),
        )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """Returns 0 on success, 1 on fetch/analyze failure. Never re-raises.

    The CLI shim at module-bottom does ``raise SystemExit(main())`` to
    preserve shell exit codes; tests assert on the return value directly.
    """
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = _build_parser().parse_args(raw_argv)
    started = int(time.time())
    explicit_no_surface_flags = any(
        flag in raw_argv
        for flag in (
            "--no-tags",
            "--no-peers",
            "--no-exec",
            "--no-history",
            "--no-archetype",
        )
    )

    # --no-persist hard-overrides --persist. Combining both is a contradiction
    # (replay scripts use this to assert no DB writes occur) so we exit 2
    # before performing any work — no fetch, no analyze, no DB I/O.
    if args.no_persist and args.persist:
        print(
            "# error: --persist and --no-persist are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    if args.no_persist:
        args.persist = False

    # --analyst-fast resolution: bounded review preset. Forces no_history,
    # no_exec, and capital_ledger=off. If the operator also passed
    # --capital-ledger, the preset wins with a stderr warning so the
    # contradiction is visible. Composes additively with other --no-* flags.
    if args.analyst_fast:
        if args.capital_ledger:
            print(
                "# warning: --analyst-fast disables capital_ledger; the "
                "preset wins. Drop --capital-ledger or drop --analyst-fast "
                "to silence this warning.",
                file=sys.stderr,
            )
            args.capital_ledger = False
        args.no_history = True
        args.no_exec = True

    # G3 — parse --triage-context-json before any I/O. Malformed JSON
    # is operator error; exit 2 without fetching/analyzing/writing.
    triage_context: dict | None = None
    if args.triage_context_json is not None:
        try:
            parsed = json.loads(args.triage_context_json)
        except json.JSONDecodeError as e:
            print(
                f"# error: --triage-context-json could not be parsed: {e}",
                file=sys.stderr,
            )
            return 2
        if not isinstance(parsed, dict):
            print(
                "# error: --triage-context-json must decode to a JSON "
                "object (dict), not "
                f"{type(parsed).__name__}.",
                file=sys.stderr,
            )
            return 2
        triage_context = parsed

    wallet = args.wallet.lower()
    session = None  # httpx.Client allocated lazily by module-level _get_httpx_client()
    run_id = str(uuid.uuid4())
    emitter = None
    if args.run_log_dir:
        emitter = JsonlEmitter(
            Path(args.run_log_dir) / wallet / f"{run_id}.jsonl",
            run_id=run_id,
            wallet=wallet,
        )
        emitter.emit("run_started", {"args": vars(args)})

    conn: sqlite3.Connection | None = None
    report = None
    try:
        # Pre-fetch wallet data so we can derive condition_ids needed for
        # tag-grain lookups — analyze_wallet's internal fetch swallows the
        # data otherwise.
        print(
            f"# Running wallet-analysis-protocol-v2 on {wallet}...",
            file=sys.stderr,
        )
        if args.from_fixture:
            print(
                f"# loading fetch fixture {args.from_fixture} (no live API calls)",
                file=sys.stderr,
            )
            data = load_fetch_snapshot(args.from_fixture)
            if data.wallet != wallet:
                print(
                    f"# warning: fixture wallet {data.wallet!r} != cli wallet "
                    f"{wallet!r}; using fixture wallet for analysis",
                    file=sys.stderr,
                )
                wallet = data.wallet
            # G4 — infer tag_lookup_state from the fixture's leafs_by_cid.
            # Fixture mode can't distinguish operator-skipped from
            # lookup-failed at capture time; None is the conservative
            # "unavailable" interpretation.
            if data.leafs_by_cid is None:
                tag_lookup_state = "unavailable"
            elif len(data.leafs_by_cid) == 0:
                tag_lookup_state = "empty"
            else:
                tag_lookup_state = "ok"
            # --no-* flags are advisory under --from-fixture; fixture surface
            # availability (None vs {}) governs whether each closure is wired.
            if explicit_no_surface_flags:
                print(
                    "# warning: --no-* flags are advisory under --from-fixture; "
                    "fixture surface availability governs",
                    file=sys.stderr,
                )
            if data.replay_surface_meta:
                for surface, meta in data.replay_surface_meta.items():
                    print(
                        f"# replay surface {surface}: "
                        f"status={meta.get('status', 'unknown')}",
                        file=sys.stderr,
                    )
            if emitter is not None:
                emitter.emit(
                    "fetch_completed",
                    {
                        "closed_positions": len(data.closed_positions),
                        "open_positions": len(data.open_positions),
                        "activity_rows": len(data.activity_rows),
                        "activity_cap_hit": data.activity_cap_hit,
                        "subgraph_fallback_used": data.subgraph_fallback_used,
                    },
                )
            # Build fixture-backed closures from the loaded data; no live producers.
            # When fixture surfaces are None the closures are None — H4/H5/H6/FH3/
            # FH4/FH6 underpower with no_* reasons, matching a live run with all
            # --no-* flags set.
            price_history_fn, peer_baseline_fn = build_fixture_closures(data)
            # Fixture mode does not carry a flip-ROI peer set; FH4 stays
            # inference_not_run unless the fixture is replayed against a
            # live Supabase. That is by design.
            flip_roi_baseline_fn = None
            leafs_by_cid = data.leafs_by_cid
            nav_lookup = data.nav_lookup
            archetype: str | None = data.archetype_layer1
            # data.execution_profile is already populated on data; protocol reads it.
        else:
            _walk = not bool(getattr(args, "bounded_history", False))
            _progress_callback = None
            if emitter is not None:
                def _progress_callback(event_name: str, payload: dict) -> None:
                    emitter.emit(event_name, payload)
            data = fetch_wallet_data(
                wallet, session, archetype_extractor=None,
                walk_full_history=_walk,
                with_capital_ledger=args.capital_ledger,
                progress_emitter=_progress_callback,
            )
            if emitter is not None:
                emitter.emit(
                    "fetch_completed",
                    {
                        "closed_positions": len(data.closed_positions),
                        "open_positions": len(data.open_positions),
                        "activity_rows": len(data.activity_rows),
                        "activity_cap_hit": data.activity_cap_hit,
                        "subgraph_fallback_used": data.subgraph_fallback_used,
                    },
                )

            archetype: str | None = args.archetype
            if archetype is not None:
                data.archetype_layer1 = archetype
            if (
                (not args.no_exec or args.analyst_fast)
                and archetype is None
                and not args.no_archetype
            ):
                try:
                    from scripts.wallet_archetype_extractor import (
                        extract_wallet_archetype,
                    )
                    features = extract_wallet_archetype(
                        wallet, skip_gate0=True, with_execution=False,
                    )
                    archetype = (
                        features.get("classification", {}).get("archetype")
                    )
                    data.archetype_layer1 = archetype
                    # Display-only: stamp Layer 2 onto WalletData so the
                    # protocol report can surface it. Layer 2 NEVER feeds
                    # verdict, gate, or primary_edge_type logic.
                    layer2_dict = features.get("classification_layer2") or {}
                    if isinstance(layer2_dict, dict):
                        data.archetype_layer2 = layer2_dict.get("archetype")
                    # Edge Surface v1: stamp the small extractor-feature subset
                    # onto WalletData. Display-only — never feeds verdict logic.
                    data.extractor_features_summary = (
                        _build_extractor_features_summary(features)
                    )
                    print(
                        f"# archetype detected: layer1={archetype} "
                        f"layer2={data.archetype_layer2}",
                        file=sys.stderr,
                    )
                except Exception as e:
                    print(
                        f"# archetype detection failed: {e!r}; gate's "
                        f"LAYER1_TERMINAL check will be a no-op",
                        file=sys.stderr,
                    )

            if not args.no_exec:
                try:
                    data.execution_profile = build_execution_profile(
                        wallet,
                        data.activity_rows,
                        data.closed_positions,
                        archetype=archetype,
                        max_trades=args.max_exec_trades,
                        force=args.force_exec,
                    )
                    print(
                        f"# execution_profile status="
                        f"{(data.execution_profile or {}).get('status')}",
                        file=sys.stderr,
                    )
                except Exception as e:
                    print(
                        f"# execution_profile build failed: {e!r}",
                        file=sys.stderr,
                    )

            price_history_fn = None
            if not args.no_history:
                price_history_fn = build_price_history_fn(session=session)
                print("# price_history_fn wired", file=sys.stderr)

            leafs_by_cid = None
            nav_lookup = None
            # G4 — track tag-lookup outcome for the report's data_completeness.
            # Default to "unavailable" so a never-attempted lookup (no DB
            # connection wired) reads correctly. The --no-tags branch
            # overrides to "skipped"; a successful lookup overrides to
            # "ok" or "empty"; a caught exception keeps "unavailable".
            tag_lookup_state = "skipped" if args.no_tags else "unavailable"
            peer_baseline_fn = None
            flip_roi_baseline_fn = None
            if args.with_flip_roi_baseline:
                try:
                    from sniper.analysis.wallet_protocol.ledger.bot_supabase_adapter import (
                        BotSupabaseAdapter,
                    )
                    from sniper.analysis.wallet_protocol.ledger.flip_roi_baseline import (
                        build_cached_flip_roi_baseline_fn,
                    )
                    from sniper.analysis.wallet_protocol.types import PROTOCOL_VERSION
                    _baseline_supabase = BotSupabaseAdapter.from_env_or_dotenv()
                    flip_roi_baseline_fn = build_cached_flip_roi_baseline_fn(
                        _baseline_supabase,
                        wallet_protocol_version=PROTOCOL_VERSION,
                    )
                    print(
                        "# flip_roi_baseline_fn wired (cached, "
                        f"protocol_version={PROTOCOL_VERSION})",
                        file=sys.stderr,
                    )
                except Exception as e:
                    print(
                        f"# --with-flip-roi-baseline requested but build failed: {e!r}",
                        file=sys.stderr,
                    )
                    flip_roi_baseline_fn = None
            if not args.no_tags or not args.no_peers:
                conn = sqlite3.connect(args.db)
                if not args.no_peers:
                    # 3-arg signature: (grain, tag, wallet). The wallet is
                    # forwarded to db.peer_baseline_for_grain via
                    # `exclude_wallet` so the analyzed wallet does not
                    # contaminate its own peer baseline.
                    peer_baseline_fn = (
                        lambda grain, tag, wallet: db.peer_baseline_for_grain(
                            conn, grain, tag, exclude_wallet=wallet,
                        )
                    )
                if not args.no_tags:
                    cids = [
                        p.get("conditionId")
                        for p in data.closed_positions
                        if p.get("conditionId")
                    ]
                    cids += [
                        p.get("conditionId")
                        for p in data.open_positions
                        if p.get("conditionId")
                    ]
                    try:
                        leafs_by_cid, nav_lookup = fetch_tag_lookups_from_db(
                            conn, cids,
                        )
                        # G4 — successful lookup; classify ok vs empty.
                        # An empty dict is falsy in Python so `if leafs_by_cid`
                        # cleanly distinguishes populated from empty.
                        tag_lookup_state = "ok" if leafs_by_cid else "empty"
                    except Exception as e:
                        print(
                            f"# tag-grain lookups failed: {e!r}",
                            file=sys.stderr,
                        )
                        leafs_by_cid = None
                        nav_lookup = None
                        # G4 — exception path: lookup unavailable.
                        tag_lookup_state = "unavailable"

        report = analyze_wallet(
            wallet,
            session=session,
            peer_baseline_fn=peer_baseline_fn,
            flip_roi_baseline_fn=flip_roi_baseline_fn,
            price_history_fn=price_history_fn,
            leafs_by_cid=leafs_by_cid,
            nav_lookup=nav_lookup,
            n_iter=args.n_iter,
            seed=args.seed,
            pre_fetched_data=data,
            walk_full_history=not bool(getattr(args, "bounded_history", False)),
            tag_lookup_state=tag_lookup_state,
            triage_context=triage_context,
        )
        report.subgraph_fallback_used = bool(
            getattr(data, "subgraph_fallback_used", False)
        )
        if args.analyst_fast:
            # Stamp the omitted-surfaces flag so downstream consumers
            # (Strategy Pattern Audit, calibration replay, dossier writer)
            # can tell operator-bounded runs apart from genuinely missing
            # surfaces. Format mirrors other KEY:csv flags already in use.
            report.data_quality_flags.append(
                "ANALYST_FAST_MODE_OMITTED:"
                "price_history,exec_attribution,capital_ledger"
            )
        if emitter is not None:
            emitter.emit(
                "analyze_completed",
                {
                    "overall_verdict": report.overall_verdict,
                    "gate_result": report.gate_result,
                    "trading_mode": report.trading_mode,
                },
            )
    except Exception as e:
        if emitter is not None:
            emitter.emit(
                "run_failed",
                {"error_class": type(e).__name__, "error_message": str(e)},
            )
        if args.persist:
            try:
                _persist_failed_run_helper(args, started=started, exc=e)
            except Exception as audit_exc:
                print(
                    f"# audit-row write failed: {audit_exc!r}",
                    file=sys.stderr,
                )
        print(f"# fetch/analyze failed: {e!r}", file=sys.stderr)
        return 1
    finally:
        # Close the SQLite handle held by `peer_baseline_fn` only AFTER
        # analyze_wallet has returned. The lambda captures `conn`, so closing
        # earlier would invalidate the closure mid-run.
        if conn is not None:
            conn.close()

    if args.json:
        from dataclasses import asdict

        out = asdict(report)
        json.dump(out, sys.stdout, indent=2, default=str)
        print()
    else:
        print(render_report(report))

    if args.persist:
        _persist_successful_run_helper(args, started=started, report=report)

    memo_result = maybe_emit_memo(report, persist_memo=args.persist_memo)
    if memo_result.status == "skipped_missing_env":
        print(memo_result.message, file=sys.stderr)
    if emitter is not None:
        emitter.emit("run_completed", {"rc": 0, "memo_status": memo_result.status})

    if args.persist_flipper_grains:
        _persist_flipper_grains_helper(args, report=report)

    return 0


PERSIST_FLIPPER_GRAINS_MARKER_PREFIX = "[PERSIST_FLIPPER_GRAINS_RESULT]"


def _emit_persist_flipper_grains_marker(payload: dict) -> None:
    """Emit a single machine-parseable persist outcome marker to stdout.

    Contract (reviewer finding #3, 2026-05-17): the seed runner (and any
    other batch caller) must be able to distinguish wrote / skipped /
    failed outcomes without trusting the analyzer's exit code. The pre-
    fix helper logged to stderr in human prose and returned silently;
    rc stayed 0 even on import/env/writer failure, so the seed runner
    could not tell whether `--execute` had actually written rows.

    Format: single line, prefix + JSON object, no embedded newlines.

        [PERSIST_FLIPPER_GRAINS_RESULT] {"status": "wrote", "wallet": ..., ...}

    Statuses:
      * "wrote"   — rows_written > 0
      * "skipped" — legitimate idempotent / no-rows skip (writer set
                    `skipped_reason`)
      * "failed"  — import_error / env_missing / writer_error
    """
    line = f"{PERSIST_FLIPPER_GRAINS_MARKER_PREFIX} {json.dumps(payload, default=str)}"
    print(line, flush=True)


def _persist_flipper_grains_helper(args, *, report) -> None:
    """Phase 1.2 — write flipper-branch tag-grain rows into Supabase.

    Default off; only fires when --persist-flipper-grains is set. Honors
    all spec §3.3 skip rules via the writer module.

    Failure handling: errors do NOT propagate to the process exit code
    (persistence is additive — a batch analyzer run shouldn't fail just
    because Supabase is briefly down). Instead, every code path emits a
    `[PERSIST_FLIPPER_GRAINS_RESULT]` marker line to stdout so callers
    that care (e.g. the seed runner) can detect failures by parsing the
    marker, not by trusting rc==0.
    """
    wallet = getattr(report, "wallet", None)

    try:
        from sniper.analysis.wallet_protocol.ledger.flipper_tag_grain_writer import (
            persist_flipper_tag_grains,
        )
        from sniper.analysis.wallet_protocol.ledger.bot_supabase_adapter import (
            BotSupabaseAdapter,
        )
    except Exception as exc:
        print(
            f"[persist-flipper-grains] could not import writer: {exc}",
            file=sys.stderr,
        )
        _emit_persist_flipper_grains_marker({
            "status": "failed",
            "wallet": wallet,
            "reason": "import_error",
            "detail": f"{type(exc).__name__}: {exc}",
        })
        return

    try:
        supabase = BotSupabaseAdapter.from_env_or_dotenv()
    except Exception as exc:
        print(
            f"[persist-flipper-grains] no Supabase env available: {exc}",
            file=sys.stderr,
        )
        _emit_persist_flipper_grains_marker({
            "status": "failed",
            "wallet": wallet,
            "reason": "env_missing",
            "detail": f"{type(exc).__name__}: {exc}",
        })
        return

    try:
        result = persist_flipper_tag_grains(
            report,
            supabase=supabase,
            fixture_path=args.from_fixture,
            code_version=_resolve_code_version(),
        )
    except Exception as exc:
        print(
            f"[persist-flipper-grains] writer raised {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        _emit_persist_flipper_grains_marker({
            "status": "failed",
            "wallet": wallet,
            "reason": "writer_error",
            "detail": f"{type(exc).__name__}: {exc}",
        })
        return

    if result.skipped_reason:
        print(
            f"[persist-flipper-grains] SKIPPED ({result.skipped_reason}) "
            f"wallet={result.wallet}",
            file=sys.stderr,
        )
        _emit_persist_flipper_grains_marker({
            "status": "skipped",
            "wallet": result.wallet,
            "wallet_fixture_hash": result.wallet_fixture_hash,
            "skipped_reason": result.skipped_reason,
        })
    elif (result.rows_written or 0) <= 0:
        # Writer returned `rows_written=0` without setting `skipped_reason`.
        # That's a contract violation — every legitimate no-rows path in
        # the writer sets a reason. Without this defensive branch, the
        # marker would say status=wrote with rows_written=0 and seed
        # runner would treat it as success (same lie class as the rc=0
        # bug we're fixing).
        print(
            f"[persist-flipper-grains] zero rows written without skip reason "
            f"wallet={result.wallet}",
            file=sys.stderr,
        )
        _emit_persist_flipper_grains_marker({
            "status": "failed",
            "wallet": result.wallet,
            "wallet_fixture_hash": result.wallet_fixture_hash,
            "reason": "zero_rows_written",
            "detail": (
                f"writer returned rows_written={result.rows_written!r} "
                f"with skipped_reason=None"
            ),
        })
    else:
        print(
            f"[persist-flipper-grains] wallet={result.wallet} hash={result.wallet_fixture_hash} "
            f"rows_written={result.rows_written} rows_superseded={result.rows_superseded}",
            file=sys.stderr,
        )
        _emit_persist_flipper_grains_marker({
            "status": "wrote",
            "wallet": result.wallet,
            "wallet_fixture_hash": result.wallet_fixture_hash,
            "rows_written": result.rows_written,
            "rows_superseded": result.rows_superseded,
        })


def _resolve_code_version() -> str:
    """Return current git HEAD as code_version, or 'unknown' offline."""
    try:
        import subprocess
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
