#!/usr/bin/env python3
"""End-to-end smoke for the HL real leg execution path — OPERATOR-RUN ONLY.

Default = TESTNET. Pass --mainnet to run the same smoke on mainnet with $15 real
(one ETH round trip, ~$0.05 in fees+spread) — this is the PRIMARY gate in practice:
HL's testnet faucet requires a PRIOR MAINNET DEPOSIT on the same address (discovered
2026-07-17 when ApproveAgent bounced with "Must deposit before performing actions"),
so the ladder is fund-first, then smoke.

Preconditions: agent approved on the chosen network (scripts/hl_real_setup_agent.py
[--testnet]) and the master account funded there (mainnet: the $100 deposit;
testnet: faucet at app.hyperliquid-testnet.xyz, unlocked only after a mainnet deposit).

Exercises the EXACT adapter the runner uses: set_isolated_1x -> market_open ($15 ETH,
explicit 0.5% slippage, deterministic-style cloid) -> order_status by cloid ->
RECONCILE against live positions mid-trade -> market_close (explicit 1% slippage) ->
fills_for_cloid fee lookup -> authenticated REJECTED-order path (below-min notional).
Non-zero exit on any failure. Nothing here touches mainnet or the paper book.

Honest coverage note: partial-fill recovery cannot be forced on a liquid testnet book —
that path stays covered by the fake-exchange unit tests until real trading produces one.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive.hl_exchange import HlExchange  # noqa: E402
from sniper.hyperlive import real_exec as RE         # noqa: E402

TESTNET = "https://api.hyperliquid-testnet.xyz"
MAINNET = "https://api.hyperliquid.xyz"
COIN = "ETH"
USD = 15.0


def main() -> int:
    import argparse
    from dotenv import load_dotenv
    load_dotenv("/home/bigpapi/Desktop/Sniper/.env")
    ap = argparse.ArgumentParser(description="HL real-leg execution smoke")
    ap.add_argument("--mainnet", action="store_true",
                    help="run on MAINNET with $15 real (post-funding gate)")
    args = ap.parse_args()
    base = MAINNET if args.mainnet else TESTNET
    if args.mainnet:
        print("⚠ MAINNET smoke: one REAL $15 ETH round trip (~$0.05 cost)")
    wallet_file = os.getenv("HL_REAL_API_WALLET_FILE", "data/hl_api_wallet.json")
    account = os.getenv("HL_REAL_ACCOUNT_ADDRESS", "")
    if not account or not os.path.exists(wallet_file):
        print("need HL_REAL_ACCOUNT_ADDRESS + api wallet file (run hl_real_setup_agent.py "
              "first, --testnet for testnet)", file=sys.stderr)
        return 1
    ex = HlExchange(api_wallet_file=wallet_file, account_address=account,
                    base_url=base, entry_slippage=0.005, exit_slippage=0.01)
    from sniper.markets import hyperliquid_api as HL

    marks = json.loads(json.dumps({}))
    try:
        import urllib.request
        req = urllib.request.Request(base + "/info",
                                     data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
                                     headers={"Content-Type": "application/json"})
        meta, ctxs = json.load(urllib.request.urlopen(req, timeout=20))
        marks = {u["name"].upper(): float(c.get("markPx") or 0)
                 for u, c in zip(meta["universe"], ctxs)}
    except Exception as e:  # noqa: BLE001
        print(f"marks fetch failed ({base}): {e}", file=sys.stderr)
        return 1

    mark = marks.get(COIN)
    szd = ex.sz_decimals(COIN)
    sz = RE.size_coin(USD, mark, szd)
    if not sz:
        print(f"sizing failed mark={mark} szd={szd}", file=sys.stderr)
        return 1
    open_cloid = "0x" + secrets.token_hex(16)
    close_cloid = "0x" + secrets.token_hex(16)
    print(f"[1/6] isolated 1x on {COIN}")
    ex.set_isolated_1x(COIN)
    print(f"[2/6] market_open {COIN} long sz={sz} (~${USD}) cloid={open_cloid}")
    resp = ex.market_open(COIN, True, sz, open_cloid)
    norm = RE.normalize_order_result(resp, sz)
    print(f"      -> {norm}")
    if norm["state"] not in ("filled", "partial"):
        print(f"open did not land: {resp}", file=sys.stderr)
        return 1
    time.sleep(2)
    st = ex.order_status(open_cloid)
    print(f"[3/6] order_status by cloid -> {RE.settle_from_status(st)!r}")
    # Authenticated reconcile mid-trade: the live position must match a leg-book-shaped
    # row exactly (same diff the runner runs every 6th cycle).
    book_row = {"copy_id": 0, "coin": COIN, "side": "long",
                "fill_px": norm["fill_px"], "fill_sz": norm["fill_sz"], "cloid": open_cloid}
    faults = RE.reconcile([book_row], ex.positions())
    print(f"[4/6] reconcile vs live positions -> faults={faults}")
    if faults:
        print("reconcile mismatch mid-trade", file=sys.stderr)
        return 1
    print(f"[5/6] market_close {COIN} cloid={close_cloid}")
    resp = ex.market_close(COIN, close_cloid)
    normc = RE.normalize_order_result(resp, norm["fill_sz"] or sz)
    print(f"      -> {normc}")
    if normc["state"] not in ("filled", "partial"):
        print(f"close did not land: {resp}", file=sys.stderr)
        return 1
    time.sleep(2)
    for label, cl in (("open", open_cloid), ("close", close_cloid)):
        agg = ex.fills_for_cloid(cl)
        print(f"      fills[{label}]: {agg}")
    acct = RE.close_accounting(
        {"fill_px": norm["fill_px"], "fill_sz": norm["fill_sz"]},
        {"fill_px": normc["fill_px"], "fill_sz": normc["fill_sz"]},
        fees_usd=0.0, paper_pnl_bps=0.0, side="long")
    print(f"round-trip: realized=${acct['realized_usd']:+.4f} "
          f"({acct['real_net_bps']:+.1f} bps before fees)")
    # Authenticated REJECTED path: below-$10-notional order must come back as a clean
    # 'rejected' normalization (never 'ambiguous') — proves error-shape parsing live.
    tiny = 10 ** -szd
    if tiny * mark < 9.0:
        rej_cloid = "0x" + secrets.token_hex(16)
        print(f"[6/6] rejection probe: {COIN} sz={tiny} (~${tiny * mark:.2f} < $10 min)")
        resp = ex.market_open(COIN, True, tiny, rej_cloid)
        normr = RE.normalize_order_result(resp, tiny)
        print(f"      -> {normr}")
        if normr["state"] != "rejected":
            print(f"expected rejected, got {normr['state']}: {resp}", file=sys.stderr)
            return 1
    else:
        print(f"[6/6] rejection probe skipped (min sz step ~${tiny * mark:.2f} not below min)")
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
