#!/usr/bin/env python3
"""Exit one stuck funded chain-56 flap.sh coin whose curve is quoted in a TOKEN, in two legs.

The operator's one-shot tool; the core is sniper/execution/evm_flap_quote_exit.py.

WHY (operator "fix daodun asap", 2026-10-02, via Alpha). Position 68544 DAODUN 0xf98a0f65 is a
graduated flap.sh coin quoted in Cheems (native_to_quote_swap_enabled=0). The funded exit ladder
refuses it (flap_curve_non_native_quote_refused), Kyber answers "route not found", and no Pancake
or Uniswap pool holds it, so it sat stuck_bag=1. The flap Portal still swaps it: an eth_call of
swapExactInput(DAODUN -> Cheems) for the whole balance returned 120,071,791.79 Cheems at 14:59Z;
DAODUN -> native reverts. Cheems trades on Pancake V2 and V3.

WHAT IT DOES, under the per-wallet EVM exec lock (evm_exec_lock_key) for the whole run:
  leg 1  exact approve(Portal, balance), then Portal swapExactInput(coin -> quote token) for the
         whole balance; the floor comes from an eth_call made right before signing.
  leg 2  exactly leg 1's quote-token delta (from leg 1's receipt, never the wallet's whole
         balance) -> WBNB on the best of Pancake V2 and V3 by quote; exact approve(router, delta);
         eth_call / estimateGas proof right before signing.
  book   the WBNB delta from leg 2's receipt, valued at the live BNB price, through
         book_full_exit_and_finalize (is_open=0 and the fill in one transaction).
Every send goes through eth_send_raw (stamp_send advances the wallet generation the fast lanes
read). The nonce is the pending count, read under the lock, and the run refuses while any of the
wallet's transactions is pending. The journal (data/flap_quote_exit/<position>.json) is written
before every broadcast. The position's close claim is taken first and leg 1's hash is its
close_sig, so the runner's exit loop sees "already_closing" and its recovery holds a confirmed
claim it cannot price instead of re-selling.

RESUME. A rerun continues from the journal: an unresolved leg-1 or leg-2 hash is read for its
receipt first; a confirmed leg 1 goes to leg 2 with the recorded delta; a failed leg 2 is re-quoted
and retried; a confirmed leg 2 is booked. An ambiguous send (no receipt) stops the run and is never
re-signed. A failed or rejected leg 1 resets the claim, so the position is as it was.

REFUSES: a position that is not open, not funded, not on 56, already claimed; a wallet balance
that differs from the ledger's open quantity beyond dust; a coin that is not a flap.sh coin quoted
in a token; any pending transaction on the wallet.

    python3 scripts/memecoin_flap_quote_exit.py 68544              # dry run: quotes, no writes
    python3 scripts/memecoin_flap_quote_exit.py 68544 --confirm    # the operator runs this
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.execution.evm_flap_quote_exit import CHAIN, Chain, Refused, Run  # noqa: E402


def _production(position_id, *, db=None, readonly=False):
    from sniper.execution import evm_flap_curve as F
    from sniper.execution.evm_wallet import load_keypair, quote_usd_price
    from sniper.execution.evm_real_seams import sign_tx
    from sniper.persistence.db_core import get_connection, get_readonly_connection
    from sniper.settings import memecoin as cfg
    from sniper.util.state_paths import state_path
    acct = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR)
    rpc = cfg.evm_rpc_for_chain(CHAIN)
    if not rpc:
        raise Refused("no funded RPC for chain 56")
    from sniper.execution.evm_flap_quote_exit import addresses as chain_addresses
    addresses = chain_addresses(cfg)
    conn = (get_readonly_connection(db) if readonly else get_connection(db))   # a dry run writes nothing
    return dict(conn=conn, chain=Chain(rpc, acct.address), sign_fn=lambda tx: sign_tx(tx, acct),
                journal_path=state_path("data", "flap_quote_exit", "%d.json" % int(position_id)),
                price_fn=lambda: quote_usd_price(CHAIN), addresses=addresses)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("position_id", type=int)
    ap.add_argument("--confirm", action="store_true", help="sign and send (the operator's)")
    ap.add_argument("--slippage-bps", type=int, default=300)
    ap.add_argument("--db", help="ledger path (default: the funded ledger)")
    args = ap.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))           # before any settings import: the funded RPC and key
    try:
        run = Run(position_id=args.position_id, slippage_bps=args.slippage_bps,
                  **_production(args.position_id, db=args.db, readonly=not args.confirm))
        if not args.confirm:
            run.dry_run()
            return 0
        journal = run.confirm()
        print("state: %s" % journal.get("state"))
        return 0 if journal.get("state") == "booked" else 2
    except Refused as exc:
        print("REFUSED: %s" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
