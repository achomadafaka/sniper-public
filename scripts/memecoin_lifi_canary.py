#!/usr/bin/env python3
"""Run one explicit $5 LI.FI entry and full exit canary on chain 4663."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))


def _state_root():
    configured = os.getenv("SNIPER_STATE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    runtime = next((parent for parent in CODE_ROOT.parents if parent.name == ".runtime"), None)
    return runtime.parent if runtime is not None else CODE_ROOT


STATE_ROOT = _state_root()
os.environ.setdefault("SNIPER_STATE_ROOT", str(STATE_ROOT))

from sniper.util.envfiles import load_env_files
load_env_files(STATE_ROOT)
os.environ["DB_PATH"] = str(STATE_ROOT / "sniper_tracker.db")

from sniper.execution.evm_wallet import CHAIN_QUOTE, load_keypair, quote_usd_price
from sniper.execution.lifi_executor import LifiClient
from sniper.execution.lifi_runtime import build_lifi_request
from sniper.execution.lifi_route import QuotaGuard
from sniper.persistence import db_memecoin as m
from sniper.persistence.db_core import get_connection
from sniper.services.memecoin_evm_leg import build_real_evm_context, evm_real_entry
from sniper.services.memecoin_entry_trace import EntryAttemptTrace
from sniper.services.memecoin_market_state import classify_funded_entry
from sniper.services.memecoin_runner import acquire_run_lock
from sniper.settings import memecoin as cfg
from sniper.util.clock import real_utc_now_iso
from sniper.util.state_paths import state_path

DEFAULT_MINT = "0x447096437c7476413e752df0b1db3e2c9169e341"
PROOF_RPC = "https://rpc.mainnet.chain.robinhood.com"


def _release_sha():
    target = Path(os.path.realpath(STATE_ROOT / ".runtime/memecoin/current")).name.lower()
    if len(target) != 40 or any(char not in "0123456789abcdef" for char in target):
        raise RuntimeError("the active memecoin release SHA is invalid")
    return target


def _quota_probe(mint, custody, amount_atomic, manifest):
    client = LifiClient(
        os.getenv("MEMECOIN_LIFI_API_URL", "https://li.quest/v1/quote"),
        api_key=os.getenv("MEMECOIN_LIFI_API_KEY", ""), manifest=manifest,
        corrected_now=real_utc_now_iso,
    )
    result = client.quote(
        build_lifi_request(
            "buy", amount_atomic, mint=mint, sell_token=CHAIN_QUOTE["4663"]["token"],
            custody=custody, manifest=manifest,
        ),
        role="entry", stage="canary_quota_probe",
    )
    if not result.ok:
        raise RuntimeError("LI.FI canary quota probe failed: " + str(result.failure_type))
    quota = result.quota
    if not QuotaGuard(remaining=quota.get("remaining") or 0).admit_canary().allowed:
        raise RuntimeError("LI.FI quota cannot admit the canary")
    return result, quota


def main():
    parser = argparse.ArgumentParser(description="Run the chain-4663 LI.FI $5 canary")
    parser.add_argument("--mint", default=DEFAULT_MINT)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if not args.confirm:
        raise SystemExit("use --confirm to spend $5 and run the full canary")
    if not os.getenv("MEMECOIN_LIFI_API_KEY", "").strip():
        raise SystemExit("MEMECOIN_LIFI_API_KEY is absent")

    run_lock = acquire_run_lock(str(state_path("data", "memecoin_runner.lock")))
    if run_lock is None:
        raise SystemExit("stop the funded runner before the canary")

    release_sha = _release_sha()
    os.environ["MEMECOIN_LIFI_ACTIVE_RELEASE_SHA"] = release_sha
    os.environ["MEMECOIN_LIFI_PROOF_RPC_URL_4663"] = PROOF_RPC
    manifest, manifest_hash = cfg.load_lifi_manifest()
    account = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR)
    price = quote_usd_price("4663")
    if not price:
        raise SystemExit("the WETH/USD price is unavailable")
    amount_atomic = int(5.0 / float(price) * 10**18)
    _probe, quota = _quota_probe(args.mint.lower(), account.address, amount_atomic, manifest)
    activation = {
        "quota_limit": str(quota["limit"]),
        "quota_remaining": str(quota["remaining"]),
        "quota_reset": str(quota["reset"]),
    }

    conn = get_connection()
    m.ensure_memecoin_tables(conn)
    context = build_real_evm_context(
        "4663", conn=conn, lifi_canary_activation=activation,
    )
    if context is None or not context.get("lifi_enabled"):
        raise SystemExit("the LI.FI canary context is unavailable")
    now = real_utc_now_iso()
    classification = classify_funded_entry(
        conn, network_id="4663", mint=args.mint.lower(), decision_at=now,
        signal_source="manual", effective_mcap=None, assumed_micro=True,
        market_stats={}, base_clip_usd=5.0, consensus_n_60m=0,
    )
    event = SimpleNamespace(
        mint=args.mint.lower(), source_user_id="lifi-canary", signal_source="manual",
        source_handle="lifi-canary", ticker="LIFI-CANARY", network_id="4663",
        side="buy", fomo_event_id=None, their_price=None, their_market_cap=None,
    )
    trace = EntryAttemptTrace.start(conn, event, now, time)
    try:
        buy = evm_real_entry(
            conn, event, route="evm:4663", day=datetime.now(timezone.utc).date().isoformat(),
            usdc_balance=context["usdc_fn"](), native_balance=context["native_fn"](),
            now_iso=now, trade_size=5.0, token_safety_fn=context["token_safety_fn"],
            execute_fn=context["execute_fn"], classification=classification, lifi_enabled=True,
            impact_cap_pct=cfg.MEMECOIN_ENTRY_IMPACT_CAP_PCT,
            chase_max_pct=cfg.MEMECOIN_REAL_CHASE_MAX_PCT, trace=trace,
        )
    except Exception as exc:
        trace.finish("error", type(exc).__name__)
        raise
    trace.finish(
        str(buy.get("status") or "unknown"), str(buy.get("reason") or buy.get("status")),
        **{key: buy[key] for key in ("pending_id", "position_id") if buy.get(key) is not None},
        **({"tx_sig": buy["tx_hash"]} if buy.get("tx_hash") else {}),
    )
    if buy.get("status") != "filled":
        raise SystemExit("LI.FI canary entry failed: " + str(buy.get("reason") or buy.get("status")))

    position = m.get_position(conn, buy["position_id"])
    close_at = real_utc_now_iso()
    claim_id = m.claim_position_close(conn, position["id"], close_at, "lifi_canary")
    if not claim_id:
        raise SystemExit("LI.FI canary close claim failed")
    sell = context["lifi_exit_fn"](
        conn=conn, local=context["lifi_local"], position=position,
        claim_id=claim_id, exit_reason="lifi_canary",
        slippage_ladder=cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER, fraction=1.0,
        direct_miss={"code": "no_route", "detail": "LI.FI canary"},
        now_iso=close_at, sell_token=CHAIN_QUOTE["4663"]["token"],
        sell_token_usd=float(price),
    )
    if sell.get("status") != "closed":
        raise SystemExit("LI.FI canary exit is unresolved: " + str(sell.get("reason") or sell.get("status")))

    spender = manifest["top_level"]["spender"]
    pairs = [(CHAIN_QUOTE["4663"]["token"], spender), (args.mint.lower(), spender)]
    allowances = [
        (token, spender_address,
         context["lifi_local"]["allowance_fn"](token, account.address, spender_address))
        for token, spender_address in pairs
    ]
    guard = context["lifi_recovery_context"]["quota_guard"]
    if not m.complete_lifi_canary(
            conn, lifecycle_id=buy["lifecycle_id"], allowances=allowances,
            quota_limit=guard.limit, quota_remaining=guard.remaining,
            quota_reset=guard.reset, created_at=real_utc_now_iso()):
        raise SystemExit("LI.FI canary allowance proof failed")
    print("canary_complete lifecycle=" + buy["lifecycle_id"])
    print("entry_tx=" + str(buy.get("tx_hash")))
    print("exit_tx=" + str(sell.get("tx_hash")))


if __name__ == "__main__":
    main()
