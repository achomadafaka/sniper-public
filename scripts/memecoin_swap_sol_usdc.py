#!/usr/bin/env python3
"""Convert between native SOL and USDC in the funded Solana wallet, in either direction.

WHY BOTH WAYS: the runner spends USDC on Solana (entry quotes are USDC -> mint; sizing reads the
USDC balance) and spends SOL only on gas. Bridged-in native SOL is invisible to the cockpit's
cash/clip numbers until it becomes USDC, and a wallet that is long USDC but short gas cannot enter
at all. Both swaps go through the SAME quote, build, sign, send, and confirm seams the live
executor uses — no new signing surface either way.

SAFETY MODEL
  - DRY-RUN by default; --confirm required to move real money.
  - The swap stays inside our own wallet (Jupiter/Raydium swap, wrapSol handled by the builder).
  - sol-to-usdc: --keep SOL stays untouched for gas (default 0.14).
  - usdc-to-sol: --amount is in USDC (dollars). This SHRINKS the trading bankroll, so the clip
    the runner sizes from it shrinks too. Refuses to spend USDC the book has already reserved.

    # SOL -> USDC (top up the trading bankroll)
    python3 scripts/memecoin_swap_sol_usdc.py --amount all --dry-run
    python3 scripts/memecoin_swap_sol_usdc.py --amount all --confirm

    # USDC -> SOL (top up gas)
    python3 scripts/memecoin_swap_sol_usdc.py --direction usdc-to-sol --amount 5 --dry-run
    python3 scripts/memecoin_swap_sol_usdc.py --direction usdc-to-sol --amount 5 --confirm
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.util.envfiles import load_env_files

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SLIPPAGE_BPS = 50


def _reserved_usdc(state_root):
    """USDC the funded book has already encumbered on Solana: unresolved reservations.

    Spending it would sell the bankroll out from under an entry that is mid-flight. `pending_totals`
    is the same reader the runner's own sizing uses, so this script and the runner agree on what
    "free" means rather than each inventing it. A read failure returns 0.0 and is announced by the
    caller's printed line, because refusing a $5 gas top-up over an unreadable side-table would be
    worse than the risk it guards; the balance read itself already fails closed.
    """
    try:
        from sniper.persistence.db_core import get_readonly_connection
        from sniper.persistence import db_memecoin as m
        conn = get_readonly_connection()
        try:
            _count, reserved = m.pending_totals(conn, "solana")
            return float(reserved or 0.0)
        finally:
            conn.close()
    except Exception as exc:
        print(f"  (note: could not read reserved USDC ({type(exc).__name__}); treating as 0.00)")
        return 0.0


def main():
    ap = argparse.ArgumentParser(
        description="Swap between native SOL and USDC in the funded Solana wallet.")
    ap.add_argument("--direction", choices=("sol-to-usdc", "usdc-to-sol"), default="sol-to-usdc",
                    help="sol-to-usdc (default) tops up the trading bankroll; "
                         "usdc-to-sol tops up gas")
    ap.add_argument("--amount", required=True,
                    help="amount to SPEND, in the input token (SOL, or USDC for usdc-to-sol). "
                         "'all' is sol-to-usdc only")
    ap.add_argument("--keep", type=float, default=0.14,
                    help="sol-to-usdc only: SOL to keep for gas (default 0.14)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True, help="inspect only (default)")
    g.add_argument("--confirm", action="store_true", help="SIGN + broadcast real money")
    ap.add_argument("--state-root", default="/home/bigpapi/Desktop/Sniper")
    args = ap.parse_args()

    load_env_files(args.state_root)
    from sniper.settings import memecoin as cfg
    from sniper.execution.solana_wallet import (load_keypair, pubkey_str, sol_balance,
                                                usdc_balance)
    from sniper.execution.solana_real_seams import (_quote_with_fallback, jupiter_swap_tx,
                                                    sign_swap_tx, rpc_send, rpc_confirm)

    kp = load_keypair(cfg.MEMECOIN_SOLANA_KEYPAIR)
    pk = pubkey_str(kp)

    # READ THROUGH EVERY CONFIGURED ENDPOINT, AND ABORT IF NONE ANSWER.
    # `sol_balance` and `usdc_balance` are compatibility wrappers that collapse EVERY failure to
    # 0.0. Bound to the single custody URL, a rate-limited read made this script print
    # "SOL 0.000000 | USDC 0.00" on a wallet holding 0.69 SOL (2026-08-25, custody key HTTP 429 all
    # day). It refused with "amount must be positive", which was luck rather than design: a
    # partially-failed read could have produced a POSITIVE but WRONG amount and swapped it.
    # A balance this script cannot read is a reason to STOP, never a zero.
    from sniper.execution.solana_wallet import sol_balance_read, token_balance_checked
    rpc = None
    sol = usdc = None
    if cfg.MEMECOIN_SOLANA_INFRA_PROVIDER == 'shyft':
        candidates = list(cfg.solana_funded_rpc_urls())
    else:
        candidates = [cfg.MEMECOIN_SOLANA_RPC_URL]
        try:
            candidates += list(cfg.solana_funded_rpc_urls())
        except Exception:
            pass
        candidates.append(os.getenv("MEMECOIN_RESEARCH_HELIUS_RPC_URL"))
    for url in candidates:
        if not url:
            continue
        read = sol_balance_read(pk, url)
        if not read.ok:
            continue
        try:
            usdc = token_balance_checked(pk, USDC_MINT, url)
        except Exception:
            continue
        sol, rpc = read.balance, url
        break
    if sol is None or usdc is None or rpc is None:
        sys.exit("REFUSE: could not read the wallet balance on any configured RPC. "
                 "This is NOT a zero balance — it is an unread one. Nothing was swapped.")
    print(f"wallet {pk}")
    print(f"  SOL {sol:.6f} | USDC {usdc:.2f}   (read via {rpc.split('//')[-1].split('/')[0]})")

    reverse = args.direction == "usdc-to-sol"
    if reverse:
        if args.amount == "all":
            sys.exit("REFUSE: --amount all is sol-to-usdc only. Name the USDC amount to spend, "
                     "so draining the trading bankroll is always deliberate.")
        amount = float(args.amount)                       # USDC (dollars)
        # A reservation that is already holding USDC for an in-flight or open position is NOT
        # spendable. Reading it here rather than trusting the raw balance keeps this script from
        # selling the bankroll out from under a live entry.
        reserved = _reserved_usdc(args.state_root)
        free = usdc - reserved
        print(f"  plan: swap {amount:.2f} USDC -> SOL for gas")
        print(f"        USDC {usdc:.2f} held, {reserved:.2f} reserved by the book, "
              f"{free:.2f} free\n")
        if amount <= 0:
            sys.exit("REFUSE: amount must be positive")
        if amount > free + 1e-9:
            sys.exit(f"REFUSE: {amount:.2f} USDC exceeds the {free:.2f} that is free "
                     f"({usdc:.2f} held minus {reserved:.2f} reserved). Nothing was swapped.")
        in_mint, out_mint, in_atomic = USDC_MINT, SOL_MINT, int(round(amount * 1e6))
    else:
        amount = (sol - args.keep) if args.amount == "all" else float(args.amount)
        print(f"  plan: swap {amount:.6f} SOL -> USDC (keep {args.keep} SOL for gas)\n")
        if amount <= 0:
            sys.exit("REFUSE: amount must be positive")
        if amount > sol - args.keep + 1e-12:
            sys.exit(f"REFUSE: {amount:.6f} leaves under the {args.keep} SOL gas reserve")
        in_mint, out_mint, in_atomic = SOL_MINT, USDC_MINT, int(round(amount * 1e9))

    quote = _quote_with_fallback(in_mint, out_mint, in_atomic, SLIPPAGE_BPS, None)
    if not quote:
        sys.exit("REFUSE: no %s quote" % ("USDC->SOL" if reverse else "SOL->USDC"))
    impact = float(quote.get("priceImpactPct") or 0.0)
    venue = quote.get("_venue") or "jupiter"
    if reverse:
        out_sol = int(quote["outAmount"]) / 1e9
        print(f"quote ({venue}): {amount:.2f} USDC -> {out_sol:.6f} SOL  "
              f"impact={impact * 100:.3f}%  slippage={SLIPPAGE_BPS}bps")
        print(f"        gas after: {sol:.6f} + {out_sol:.6f} = {sol + out_sol:.6f} SOL")
    else:
        out_usdc = int(quote["outAmount"]) / 1e6
        print(f"quote ({venue}): {amount:.6f} SOL -> {out_usdc:.2f} USDC  "
              f"impact={impact * 100:.3f}%  slippage={SLIPPAGE_BPS}bps")

    if not args.confirm:
        print("\nDRY-RUN — nothing signed. Re-run with --confirm to broadcast real money.")
        return

    print("\n--confirm: broadcasting real money.")
    tx_b64 = jupiter_swap_tx(quote, pk)
    if not tx_b64:
        sys.exit("REFUSE: swap build failed")
    signed = sign_swap_tx(tx_b64, kp)
    if not signed:
        sys.exit("REFUSE: signing failed")
    signed_b64, sig = signed[0], signed[1]
    print(f"  sending {sig}…")
    sent = rpc_send(signed_b64, rpc)
    if sent is None:
        sys.exit("STOP: broadcast failed — nothing sent")
    status = rpc_confirm(sig, rpc)
    print(f"  {sig} -> {status}")
    if status != "confirmed":
        sys.exit(f"STOP: swap status={status} — check the explorer before any retry")
    print(f"\nfinal: SOL {sol_balance(pk, rpc):.6f} | USDC {usdc_balance(pk, rpc):.2f}")


if __name__ == "__main__":
    main()
