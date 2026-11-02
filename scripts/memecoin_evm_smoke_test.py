#!/usr/bin/env python3
"""EVM leg smoke test: a WETH->token->WETH round trip on Robinhood chain 4663 through the full
direct stack (custody -> venue quote -> exact approval -> nonce write-ahead -> send -> receipt ->
chain-delta fill -> exit ladder). Operator-run only after funding the EVM wallet.

It exercises the production executor (build_real_evm_executor.execute_fn / exit_fn) against a TEMP DB
(never the prod DB), so a pass means the real code path works — not a bespoke probe.

  --dry-run   balances + a live direct buy quote only (no broadcast).
  --confirm   actually broadcast (SPENDS REAL FUNDS). Required for the round trip.

Env: MEMECOIN_EVM_KEYPAIR (funded, 0600), MEMECOIN_EVM_RPC_URL_4663, and
MEMECOIN_EVM_EXEC_VENUE_4663=direct. Never pass secrets on the CLI.
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sniper.util.envfiles import load_env_files
load_env_files(_ROOT)   # same loader as the runner — creds found wherever the operator put them

from sniper.execution import evm_wallet as W
from sniper.execution.evm_real_seams import build_real_evm_executor, usd_to_quote_atomic
from sniper.persistence import db_memecoin as m
from sniper.settings.memecoin import (
    MEMECOIN_EVM_KEYPAIR, evm_rpc_for_chain, memecoin_evm_exec_venue,
    memecoin_evm_v3_addresses,
)

# Liquid probe token for Robinhood chain 4663 (WETH-quoted, no USDC pools).
PROBE_TOKEN = {
    "4663": "0x020bfC650A365f8BB26819deAAbF3E21291018b4",   # CASHCAT on Robinhood Chain — the
    # $7M-liq flagship; several same-name clones exist (one burned the first probe attempt), so
    # this address was verified against the deepest DexScreener pair 2026-07-10.
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def main():
    ap = argparse.ArgumentParser(description="Direct EVM smoke test on Robinhood chain 4663")
    ap.add_argument("--chain", default="4663", help="must be 4663")
    ap.add_argument("--usd", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true", help="balances + buy quote only, no broadcast")
    ap.add_argument("--confirm", action="store_true", help="broadcast (SPENDS REAL FUNDS)")
    args = ap.parse_args()

    chain = str(args.chain)
    if chain != "4663":
        sys.exit("only direct Robinhood chain 4663 is supported")
    probe = PROBE_TOKEN.get(chain)
    if not probe:
        sys.exit(f"no probe token configured for chain {chain} (supported: {sorted(PROBE_TOKEN)})")
    rpc = evm_rpc_for_chain(chain)
    if not rpc:
        sys.exit(f"MEMECOIN_EVM_RPC_URL[_{chain}] is unset — cannot reach the chain")
    if memecoin_evm_exec_venue(chain) != "direct" or not memecoin_evm_v3_addresses(chain):
        sys.exit("direct 4663 venue is disabled or missing verified V3 configuration")
    if not os.path.exists(MEMECOIN_EVM_KEYPAIR):
        sys.exit(f"EVM keypair not found at {MEMECOIN_EVM_KEYPAIR} — fund + place it (0600) first")

    acct = W.load_keypair(MEMECOIN_EVM_KEYPAIR)
    addr = W.address_str(acct)
    native = W.native_balance(addr, rpc)
    quote_cfg = W.CHAIN_QUOTE[chain]
    quote_token = quote_cfg["token"]
    quote_units = W.erc20_balance(addr, quote_token, rpc)
    bankroll_usd = W.quote_balance_usd(addr, chain, rpc)
    quote_name = "USDC" if quote_cfg["kind"] == "usd" else "WETH"
    print(f"wallet   {addr}")
    print(f"chain    {chain}   rpc set   venue direct")
    print(f"balances native={native:.6f}  {quote_name}={quote_units:.6f}  (${bankroll_usd:.2f} bankroll)")

    sell_atomic = usd_to_quote_atomic(chain, args.usd)
    if not sell_atomic:
        sys.exit("FAIL: cannot size the buy (quote-token USD price unavailable — fail-closed).")
    ex = build_real_evm_executor(acct=acct, chain_id=chain, rpc_url=rpc)
    q = ex["venue_quote_fn"](quote_token, probe, sell_atomic)
    if not q:
        sys.exit("FAIL: direct buy quote returned nothing (check RPC and pool topology).")
    print(f"buy quote OK: sell {q.get('sellAmount')} {quote_name} -> buy {q.get('buyAmount')} probe  "
          f"(allowance issue: {bool((q.get('issues') or {}).get('allowance'))})")

    if args.dry_run or not args.confirm:
        print("\nDRY RUN — no broadcast. Re-run with --confirm to spend $%.2f and complete the round trip."
              % args.usd)
        return

    if bankroll_usd < args.usd:
        sys.exit(f"FAIL: bankroll ${bankroll_usd:.2f} < trade size ${args.usd} — fund the wallet "
                 f"({quote_name} on chain {chain}).")

    # Exercise the PRODUCTION executor against a temp DB (never prod).
    db_path = os.path.join(_ROOT, "data", "evm_smoke.sqlite")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    print(f"\n--- BUY ({quote_name} -> probe token) via production execute_fn ---")
    pid = m.reserve_pending(conn, mint=probe, source_user_id="smoke", strategy="alpha_b",
                            notional=args.usd, reserved_at=_now(), network_id=chain)
    t0 = time.perf_counter()
    buy = ex["execute_fn"](conn, pid, probe, args.usd, _now())
    print(f"buy result: {buy}  ({(time.perf_counter()-t0)*1000:.0f}ms)")
    if buy.get("status") != "filled":
        sys.exit(f"FAIL: buy did not fill (status={buy.get('status')}). Inspect above.")

    print(f"\n--- SELL (probe token -> {quote_name}) via production exit_fn ---")
    pos = m.get_position(conn, buy["position_id"])
    t1 = time.perf_counter()
    sell = ex["exit_fn"](conn, pos, "smoke", _now())
    print(f"sell result: {sell}  ({(time.perf_counter()-t1)*1000:.0f}ms)")

    usd_after = W.quote_balance_usd(addr, chain, rpc)
    print(f"\nround-trip bankroll: ${bankroll_usd:.4f} -> ${usd_after:.4f}  (cost ${bankroll_usd - usd_after:.4f} on ${args.usd})")
    if sell.get("status") == "closed":
        print("SMOKE TEST PASSED — the full EVM buy+sell path works. Arm the EVM leg per the runbook.")
    else:
        print(f"SELL not clean (status={sell.get('status')}). Do NOT arm until the exit path is proven.")


if __name__ == "__main__":
    main()
