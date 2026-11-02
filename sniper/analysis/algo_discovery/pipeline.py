"""Algorithm-discovery pipeline — stage adapters + the sequential driver (v2).

v2 (paper-first): the chain profiles a wallet, CLASSIFIES its behavior, and
ROUTES it to a paper experiment — it does NOT use historical backtesting as a
go/no-go gate (history can't prove fills; see the v2 design memo). Flow:

  fetch → run_wallet_protocol → build_dossier → classify → extract_rule → route

Route → paper experiment:
  discretionary → copy_mirror  (shadow_copy_v1 mirrors the wallet's fills)
  algorithmic   → rebuild_rule (independent-selection strategy from guessed params)
  ambiguous/external-intel → human_review (no auto-deploy)

The OOS backtest remains available as an optional DIAGNOSTIC artifact
(run_backtest=True) — informative, never gating. Forward paper is the only proof.

Each adapter is a module-level function so the driver can be unit-tested by
monkeypatching individual stages. Stages 3-5 run in-process; stage 2 (protocol)
shells out to run_wallet_protocol.py. v1 is fixture-only (live fetch is a gap).
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sniper.analysis.algo_discovery.artifacts import RunDir, hash_obj

REPO_ROOT = Path(__file__).resolve().parents[3]
MIN_EPISODES = int(os.getenv("ALGO_DISCOVERY_MIN_EPISODES", "10"))
SLIPPAGE_NORMAL = float(os.getenv("ALGO_DISCOVERY_SLIPPAGE_NORMAL", "0.005"))
SLIPPAGE_STRESS = float(os.getenv("ALGO_DISCOVERY_SLIPPAGE_STRESS_PCT", "0.015"))
PAPER_SIZE_CAP_USDC = float(os.getenv("ALGO_DISCOVERY_PAPER_SIZE_CAP_USDC", "25"))


@dataclass
class Ctx:
    wallet: str = ""
    from_fixture: Any = None          # dict | path | None
    wallet_data_path: Path | None = None
    n_iter: int | None = None
    live: bool = False                # capture a fresh snapshot from the live API
    capture_copy_latency: bool = False  # live: also capture the copy-latency price grid
    cse_context_path: Any = None      # path to a prebuilt CSE MarketContext (Becker index)


# --------------------------------------------------------------------------
# Stage adapters
# --------------------------------------------------------------------------

def stage_fetch(wallet: str, ctx: Ctx | None) -> dict:
    """Return a WalletData snapshot dict — from a fixture, or a fresh live
    capture (record_fetch_snapshot, the canonical capturer)."""
    fx = getattr(ctx, "from_fixture", None) if ctx else None
    if fx is not None:
        return dict(fx) if isinstance(fx, dict) else json.loads(Path(fx).read_text())
    if getattr(ctx, "live", False):
        return _live_capture(wallet, capture_copy_latency=getattr(ctx, "capture_copy_latency", False))
    raise NotImplementedError(
        "no input: pass a WalletData fixture via from_fixture, or set live=True "
        "to capture a fresh snapshot from the Polymarket API"
    )


def _live_capture(wallet: str, *, capture_copy_latency: bool) -> dict:
    """Capture a fresh WalletData snapshot from the live API via the canonical
    record_fetch_snapshot.py (produces the v2 fetch-snapshot shape)."""
    import tempfile
    out = Path(tempfile.mkdtemp(prefix="algo_disc_live_")) / f"{wallet[:12]}_snapshot.json"
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "record_fetch_snapshot.py"),
           wallet, "--out", str(out)]
    if not capture_copy_latency:
        cmd.append("--no-copy-latency-grid")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2400, cwd=str(REPO_ROOT))
    if proc.returncode != 0:
        raise RuntimeError(f"record_fetch_snapshot exit {proc.returncode}: {proc.stderr[-1000:]}")
    if not out.exists():
        raise RuntimeError(f"record_fetch_snapshot produced no output at {out}")
    return json.loads(out.read_text())


def stage_protocol(wallet: str, wallet_data: dict, ctx: Ctx | None) -> dict:
    """Run the wallet protocol via the canonical CLI (reuses all assembly) and
    return the ProtocolReport as a dict parsed from its --json stdout."""
    wd_path = getattr(ctx, "wallet_data_path", None) if ctx else None
    if wd_path is None:
        raise ValueError("stage_protocol requires ctx.wallet_data_path")
    cmd = [
        sys.executable, str(REPO_ROOT / "scripts" / "run_wallet_protocol.py"),
        wallet, "--from-fixture", str(wd_path), "--json",
        "--no-history", "--no-persist",
    ]
    n_iter = getattr(ctx, "n_iter", None) if ctx else None
    if n_iter is not None:
        cmd += ["--n-iter", str(int(n_iter))]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=str(REPO_ROOT))
    if proc.returncode != 0:
        raise RuntimeError(
            f"run_wallet_protocol exit {proc.returncode}: {proc.stderr[-800:]}"
        )
    return _extract_json_object(proc.stdout)


def _extract_json_object(stdout: str) -> dict:
    """Pull the single JSON object the --json path prints (stdout also carries
    '#' comment lines)."""
    start = stdout.find("{")
    if start == -1:
        raise RuntimeError("run_wallet_protocol --json produced no JSON object")
    try:
        return json.loads(stdout[start:])
    except json.JSONDecodeError:
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                return json.loads(line)
        raise


def stage_dossier(wallet: str, wallet_data: dict, protocol_report: dict, ctx: Ctx | None) -> dict:
    """Build DossierTables in-process and return them as a dict-of-lists."""
    from sniper.analysis.wallet_dossier.builder import build_tables
    tables = build_tables(wallet=wallet, wallet_data=wallet_data, protocol_report=protocol_report)
    return dataclasses.asdict(tables)


def stage_classify(tables: dict, ctx: Ctx | None, *, wallet_data: dict | None = None) -> dict:
    """Classify the wallet's behavior (algorithmic/discretionary/...) → route.

    Prefers the Counterfactual Selection Engine when a point-in-time MarketContext
    is supplied (ctx.cse_context_path) AND the raw fills are available — it
    separates selection- from execution-automation and gates 'algorithmic' on a
    real selection edge. Falls back to the behavioral heuristic otherwise (and on
    any CSE error, recording the failure rather than swallowing it)."""
    from sniper.analysis.algo_discovery.classifier import classify_wallet_behavior
    cse_path = getattr(ctx, "cse_context_path", None) if ctx else None
    if cse_path and wallet_data is not None:
        try:
            from sniper.analysis.counterfactual_selection.pit_market_context import MarketContext
            from sniper.analysis.algo_discovery.classifier import cse_classification
            context = MarketContext.from_json_file(str(cse_path))
            return dataclasses.asdict(cse_classification(wallet_data, context))
        except Exception as exc:  # noqa: BLE001 — fall back, but surface the failure
            base = dataclasses.asdict(classify_wallet_behavior(tables))
            base.setdefault("signals", {})["cse_error"] = repr(exc)
            base["signals"]["cse_attempted"] = True
            return base
    return dataclasses.asdict(classify_wallet_behavior(tables))


def stage_rule(tables: dict, ctx: Ctx | None) -> dict:
    """Extract the wallet_rule_v1 from the dossier tables (dict-of-lists)."""
    from sniper.analysis.wallet_parameter_extractor import extract_wallet_rule
    return extract_wallet_rule(tables)


def stage_backtest(rule: dict, wallet_data: dict, ctx: Ctx | None, *, slippage_pct: float) -> dict:
    """Optional DIAGNOSTIC only (v2 does not gate on this)."""
    from sniper.analysis.backtest_harness import backtest_rule_on_wallet
    return backtest_rule_on_wallet(
        rule, wallet_data, slippage_pct=slippage_pct, include_per_position=True
    )


# --------------------------------------------------------------------------
# Routing → paper enrollment
# --------------------------------------------------------------------------

def _rule_extractable(rule: dict | None) -> bool:
    return bool(rule) and not rule.get("rejection_reason") and bool(rule.get("exit_rule"))


def _shadow_copy_enrollment(wallet: str, rule: dict | None) -> dict:
    """copy_mirror: paper-mirror the wallet's actual fills. The market_filter is
    a hint (empty = mirror everything they buy)."""
    mf = (rule or {}).get("market_filter") or {}
    sizing = (rule or {}).get("sizing") or {}
    median = float(sizing.get("median_usdc") or 0.0) or PAPER_SIZE_CAP_USDC
    return {
        "strategy_type": "shadow_copy_v1",
        "params": {
            "target_wallets": [wallet],
            "market_filter": {
                "categories": mf.get("categories") or [],
                "market_types": mf.get("market_types") or [],
                "outcome_roles": mf.get("outcome_roles") or [],
                "entry_price_range": mf.get("entry_price_range"),
            },
            "max_position_size_usdc": min(median, PAPER_SIZE_CAP_USDC),
        },
    }


def _rebuild_rule_enrollment(wallet: str, rule: dict) -> dict:
    """rebuild_rule: an INDEPENDENT-selection paper strategy from the guessed
    parameters — its live selections-vs-theirs is the replication test. The
    plugin itself is v2 step 3; emit the deploy spec now (status plugin_pending)."""
    return {
        "strategy_type": "wallet_rule_v1",
        "status": "plugin_pending",
        "params": {
            "source_wallet": wallet,
            "market_filter": rule.get("market_filter"),
            "sizing": rule.get("sizing"),
            "timing": rule.get("timing"),
            "exit_rule": rule.get("exit_rule"),
        },
    }


def stage_route(wallet: str, rule: dict | None, classification: dict | None, ctx: Ctx | None) -> dict:
    """Map the behavioral classification to a paper experiment (or human review).
    Returns {route, deployable, enrollment, reason?}."""
    route = (classification or {}).get("route_hint") or "human_review"
    if route == "copy_mirror":
        return {"route": "copy_mirror", "deployable": True,
                "enrollment": _shadow_copy_enrollment(wallet, rule), "reason": None}
    if route == "rebuild_rule":
        if not _rule_extractable(rule):
            # Behaves like a bot but we couldn't guess its parameters → flag.
            return {"route": "human_review", "deployable": False, "enrollment": None,
                    "reason": "algorithmic_but_no_extractable_rule"}
        return {"route": "rebuild_rule", "deployable": True,
                "enrollment": _rebuild_rule_enrollment(wallet, rule), "reason": None}
    # ambiguous / insufficient_data / external-intel
    return {"route": "human_review", "deployable": False, "enrollment": None,
            "reason": (classification or {}).get("label", "ambiguous")}


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def run_discovery(
    wallet: str,
    *,
    run_root: Path | str,
    from_fixture: Any = None,
    live: bool = False,
    capture_copy_latency: bool = False,
    force: bool = False,
    n_iter: int | None = None,
    run_backtest: bool = False,
    cse_context_path: Any = None,
) -> dict:
    """Drive the v2 algorithm-discovery chain on one wallet. Returns a result
    dict {wallet, run_dir, classification, route, routing, rule, backtest?}.
    Every outcome (including human_review) is a SUCCESSFUL run — not an error."""
    wallet = (wallet or "").lower()
    run_dir = Path(run_root) / f"{wallet[:10]}_{int(time.time())}"
    rd = RunDir(run_dir, force=force)
    result: dict[str, Any] = {"wallet": wallet, "run_dir": str(run_dir)}
    stage = "01_wallet_data"
    try:
        # 01 — wallet data
        h1 = hash_obj({"wallet": wallet, "has_fixture": from_fixture is not None, "live": live})
        if rd.should_run(stage, h1):
            wd = stage_fetch(wallet, Ctx(
                wallet=wallet, from_fixture=from_fixture,
                live=live, capture_copy_latency=capture_copy_latency,
            ))
            rd.write_artifact(stage, wd, input_hash=h1)
        else:
            wd = rd.read_artifact(stage)
        fixture_wallet = str(wd.get("wallet") or "").lower()
        if fixture_wallet and fixture_wallet != wallet:
            raise ValueError(
                f"fixture wallet {fixture_wallet} != requested {wallet} — refusing to "
                "analyze the wrong wallet"
            )

        # 02 — protocol report
        stage = "02_protocol_report"
        h2 = hash_obj(wd)
        if rd.should_run(stage, h2):
            report = stage_protocol(
                wallet, wd, Ctx(wallet=wallet, wallet_data_path=rd.artifact_path("01_wallet_data"), n_iter=n_iter)
            )
            rd.write_artifact(stage, report, input_hash=h2)
        else:
            report = rd.read_artifact(stage)

        # 03 — dossier
        stage = "03_dossier"
        h3 = hash_obj({"wd": h2, "rep": hash_obj(report)})
        if rd.should_run(stage, h3):
            tables = stage_dossier(wallet, wd, report, None)
            rd.write_artifact(stage, tables, input_hash=h3)
        else:
            tables = rd.read_artifact(stage)

        # 04 — classify behavior → route hint (CSE when a context is supplied)
        stage = "04_classification"
        classification = stage_classify(
            tables, Ctx(wallet=wallet, cse_context_path=cse_context_path), wallet_data=wd
        )
        rd.write_artifact(stage, classification, input_hash=hash_obj({"d": h3}))
        result["classification"] = classification

        # 05 — extract the candidate rule (the deployable hypothesis)
        stage = "05_wallet_rule"
        rule = stage_rule(tables, None)
        rd.write_artifact(stage, rule, input_hash=hash_obj({"d": h3}))
        result["rule"] = rule

        # 06 — route to a paper experiment (or human review)
        stage = "06_route"
        routing = stage_route(wallet, rule, classification, None)
        rd.write_artifact(stage, routing, input_hash=hash_obj({"c": hash_obj(classification), "r": hash_obj(rule)}))
        result["route"] = routing["route"]
        result["routing"] = routing

        # (optional) OOS backtest as a DIAGNOSTIC artifact — never gates v2
        if run_backtest and _rule_extractable(rule) and _episodes(rule) >= MIN_EPISODES:
            stage = "07_backtest_diagnostic"
            bt = stage_backtest(rule, wd, None, slippage_pct=SLIPPAGE_NORMAL)
            rd.write_artifact(stage, bt, input_hash=hash_obj({"rule": hash_obj(rule)}))
            result["backtest"] = bt

        return result
    except Exception as exc:  # noqa: BLE001 — record which stage failed, then re-raise
        rd.mark_failed(stage, repr(exc))
        raise


def _episodes(rule: dict) -> int:
    sg = (rule or {}).get("source_grain") or {}
    try:
        return int(sg.get("n_episodes") or 0)
    except (TypeError, ValueError):
        return 0
