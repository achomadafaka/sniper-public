#!/usr/bin/env python3
"""Would Jito help our funded Solana fills? The evidence behind docs/audits/2026-09-27-jito-evaluation.md.

Operator order 2026-09-27 via Alpha ("look into Jito and see if we can benefit from it"). Read-only.
Chain reads use the research key (funded_exit_truth.rpc, the first MEMECOIN_RESEARCH_HELIUS_RPC_URLS
entry); bundle membership uses Jito's public explorer API (bundles.jito.wtf, no key). Never the funded
RPC.

  bundles  For each transaction in the fee-study windows (the JSON written by
           copier_crowd_and_fees.py fees --out) that sits in the source's slot or ours, or that
           tipped, the Jito bundle it landed in, if any: the bundle's transactions, tippers, tip
           and block index. Answers whether the copiers in the source's slot back-run the source
           inside its own bundle.
  premium  For every funded Solana entry since --since, and every b_stop / mirror exit, how far
           the mint's price moved inside our landing slot before our own trade: the most a bundle
           at the top of that slot could have saved. Priced from pump curve TradeEvents and
           PumpSwap Buy/SellEvents (pre-trade reserves); other venues are counted as unpriced.

    python3 scripts/research/jito_evaluation.py bundles --fees A.json B.json --out bundles.json
    python3 scripts/research/jito_evaluation.py premium --since 2026-09-24 --out premium.json
    python3 scripts/research/jito_evaluation.py --selftest
"""
import argparse
import base64
import contextlib
import json
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
import funded_exit_truth as fet  # noqa: E402
import dump_forensics as dfx  # noqa: E402
from copier_crowd_and_fees import mint_txs  # noqa: E402
from sniper.persistence.db_core import get_readonly_connection  # noqa: E402

fet.SPACING_S = 0.3
LEDGER = str(REPO / "sniper_tracker.db")
JITO = "https://bundles.jito.wtf/api/v1/bundles"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
AMM_BUY = bytes.fromhex("67f4521f2cf57777")      # sha256("event:BuyEvent")[:8]
AMM_SELL = bytes.fromhex("3e2f370aa503dc2a")     # sha256("event:SellEvent")[:8]
POOL_BASE_MINT_OFFSET = 43                       # disc 8 + bump 1 + index 2 + creator 32
POOL_QUOTE_MINT_OFFSET = 75
POOL_VIRTUAL_QUOTE_OFFSET = 245                  # sniper/execution/pumpswap_buy.py


# --- Jito explorer --------------------------------------------------------------------------------

def jito_get(url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0"})
            return json.loads(urllib.request.urlopen(req, timeout=15).read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code != 429 and attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(1)
        finally:
            time.sleep(0.35)
    return None


def bundle_of(sig):
    res = jito_get(f"{JITO}/transaction/{sig}")
    bid = (res[0] or {}).get("bundle_id") if isinstance(res, list) and res else None
    if not bid:
        return None
    det = jito_get(f"{JITO}/bundle/{bid}")
    return det[0] if isinstance(det, list) and det else None


def bundles(fee_files):
    fills = {}
    for path in fee_files:
        for r in json.loads(Path(path).read_text()):
            fills[r["position_id"]] = r
    out, seen = [], {}
    for r in fills.values():
        rows = r.get("rows") or []
        src = next((x for x in rows if x["role"] == "source"), None)
        us = next((x for x in rows if x["role"] == "ours"), None)
        keep = {x["slot"] for x in (src, us) if x}
        for x in rows:
            if x["slot"] in keep or x["tip_lamports"]:
                if x["sig"] not in seen:
                    seen[x["sig"]] = bundle_of(x["sig"])
                out.append({"position_id": r["position_id"], "lane": r.get("lane"), "slot": x["slot"],
                            "index": x["index"], "sig": x["sig"], "role": x["role"],
                            "side": x["side"], "sol": x["sol"], "ok": x["ok"],
                            "tip_lamports": x["tip_lamports"],
                            "source_slot": src["slot"] if src else None,
                            "source_index": src["index"] if src else None,
                            "source_sig": src["sig"] if src else None,
                            "our_slot": us["slot"] if us else None,
                            "our_index": us["index"] if us else None,
                            "bundle": seen[x["sig"]]})
    return out


# --- in-slot price movement -----------------------------------------------------------------------

def _curve_pre_price(e):
    """SOL per whole token before a pump curve trade, from its TradeEvent (reserves are post-trade)."""
    vs, vt = int(e["virtual_sol_reserves"]), int(e["virtual_token_reserves"])
    sol, tok = int(e["sol_amount"]), int(e["token_amount"])
    vs_pre, vt_pre = (vs - sol, vt + tok) if e["is_buy"] else (vs + sol, vt - tok)
    return (vs_pre / 1e9) / (vt_pre / 1e6) if vs_pre > 0 and vt_pre > 0 else None


def _amm_event(line):
    """(pool, pre base reserves, pre quote reserves) from a PumpSwap Buy/SellEvent log line.
    Both events open with timestamp i64 then six u64s (pool reserves 5th and 6th), eight more u64s,
    then the pool pubkey."""
    from sniper.research.memecoin_opportunity.decoders import BorshReader, anchor_event_bytes
    raw = anchor_event_bytes(line)
    if raw[:8] not in (AMM_BUY, AMM_SELL):
        return None
    r = BorshReader(raw[8:])
    r.i64()
    nums = [r.u64() for _ in range(13)]
    # nums[0]: base out (buy) / base in (sell); nums[11]: quote in with LP fee (buy) /
    # quote out without LP fee (sell). Pool reserves nums[4], nums[5] are PRE-trade.
    return {"pool": r.pubkey(), "base": nums[4], "quote": nums[5], "is_buy": raw[:8] == AMM_BUY,
            "base_amount": nums[0], "quote_amount": nums[11]}


_POOLS = {}


def _pool_facts(pool):
    """(base mint, quote mint, virtual quote) of a PumpSwap pool, read once."""
    if pool not in _POOLS:
        acc = (fet.rpc("getAccountInfo", [pool, {"encoding": "base64"}]) or {}).get("value")
        facts = None
        if acc:
            raw = base64.b64decode(acc["data"][0])
            from solders.pubkey import Pubkey
            facts = (str(Pubkey.from_bytes(raw[POOL_BASE_MINT_OFFSET:POOL_BASE_MINT_OFFSET + 32])),
                     str(Pubkey.from_bytes(raw[POOL_QUOTE_MINT_OFFSET:POOL_QUOTE_MINT_OFFSET + 32])),
                     struct.unpack_from("<q", raw, POOL_VIRTUAL_QUOTE_OFFSET)[0]
                     if len(raw) >= POOL_VIRTUAL_QUOTE_OFFSET + 8 else 0)
        _POOLS[pool] = facts
    return _POOLS[pool]


def _trade_prices(tx, mint):
    """(pre-trade price, post-trade price, venue key) of this tx's trade on `mint`, or
    (None, None, reason). The key names the EXACT venue: "curve:<quote mint>" for the mint's pump
    curve, "pumpswap:<pool>" for one PumpSwap pool. Prices are quote per base in raw units, so
    only two prices with the same key may be compared (operator review, 2026-09-27: the family
    alone, "pumpswap", let two different pools of one mint be compared)."""
    if tx is None or (tx.get("meta") or {}).get("err") is not None:
        return None, None, "failed"
    from sniper.execution.pump_curve_buy import parse_trade_event
    from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes
    from sniper.services.memecoin_opportunity_live import program_data_logs
    logs = tx["meta"].get("logMessages") or []
    for _i, line in program_data_logs(logs, fet.PUMP):
        try:
            if anchor_event_bytes(line)[:8] == fet.TRADE_DISC:
                e = parse_trade_event(line)
                if e.get("mint") == mint:
                    vs, vt = int(e["virtual_sol_reserves"]), int(e["virtual_token_reserves"])
                    post = (vs / 1e9) / (vt / 1e6) if vs > 0 and vt > 0 else None
                    return _curve_pre_price(e), post, "curve:%s" % (e.get("quote_mint") or fet.WSOL)
        except Exception:
            continue
    for _i, line in program_data_logs(logs, PUMP_AMM):
        try:
            e = _amm_event(line)
        except Exception:
            continue
        if not e:
            continue
        facts = _pool_facts(e["pool"])
        if not (facts and facts[0] == mint and e["base"] > 0):
            continue
        vq = facts[2]
        pre = (e["quote"] + vq) / e["base"]
        if e["is_buy"]:
            base_post, quote_post = e["base"] - e["base_amount"], e["quote"] + e["quote_amount"]
        else:
            base_post, quote_post = e["base"] + e["base_amount"], e["quote"] - e["quote_amount"]
        post = (quote_post + vq) / base_post if base_post > 0 and quote_post >= 0 else None
        return pre, post, "pumpswap:%s" % e["pool"]
    return None, None, "unpriced"


def pre_price(tx, mint):
    """(price before this tx's trade on `mint`, exact venue key) or (None, reason)."""
    pre, _post, key = _trade_prices(tx, mint)
    return (pre, key) if pre is not None else (None, key)


def post_price(tx, mint):
    """(price after this tx's trade on `mint`, exact venue key) or (None, reason)."""
    _pre, post, key = _trade_prices(tx, mint)
    return (post, key) if post is not None else (None, key)


def funded(since):
    with contextlib.closing(get_readonly_connection(LEDGER)) as c:
        rows = c.execute(
            "SELECT p.id, p.mint, p.exit_reason, f.kind, f.tx_sig, f.notional, a.delivery_lane "
            "FROM memecoin_positions p JOIN memecoin_fills f ON f.position_id=p.id "
            "LEFT JOIN memecoin_entry_attempts a ON a.attempt_id=p.attempt_id "
            "WHERE p.execution_mode='real' AND p.network_id='solana' AND f.tx_sig IS NOT NULL "
            "AND ((f.kind='entry' AND p.opened_at>=?) OR "
            "     (f.kind IN ('bracket_exit','mirror_exit') AND p.exit_reason IN ('b_stop','mirror') "
            "      AND p.closed_at>=?)) ORDER BY f.created_at", (since, since)).fetchall()
    return [dict(r) for r in rows]


def premium_one(f):
    ours = dfx.get_tx(f["tx_sig"])
    if not ours:
        return {**f, "status": "our tx unreadable"}
    slot = ours["slot"]
    txs = mint_txs(f["mint"], slot, slot)
    mine = next((t for t in txs if t["transaction"]["signatures"][0] == f["tx_sig"]), None)
    if mine is None:
        return {**f, "status": "our tx not in the slot read", "slot": slot}
    idx = mine["transactionIndex"]
    ahead = [t for t in txs if t["transactionIndex"] < idx and (t.get("meta") or {}).get("err") is None]
    p_ours, venue = pre_price(mine, f["mint"])
    first = None
    for t in ahead:                           # the first PRICED trade in the slot, SAME pool
        p, v = pre_price(t, f["mint"])
        if p is not None and v == venue:
            first = p
            break
    sides = [dfx.trade_of(t, f["mint"], 1.0) for t in ahead]
    move = (p_ours / first - 1.0) if (p_ours and first) else (0.0 if p_ours and not ahead else None)
    return {**f, "status": "ok" if move is not None else venue, "slot": slot, "index": idx,
            "venue": venue, "ahead_txs": len(ahead),
            "ahead_buys": sum(1 for s in sides if s and s["side"] == "buy"),
            "ahead_sells": sum(1 for s in sides if s and s["side"] == "sell"),
            "in_slot_move": move}


# --- selftest -------------------------------------------------------------------------------------

def selftest():
    # a 1 SOL buy into 30 SOL / 1.073e9 tokens moves the curve price up by ~(31/30)^2
    e = {"virtual_sol_reserves": 31_000_000_000, "virtual_token_reserves": 1_038_387_096_774_194,
         "sol_amount": 1_000_000_000, "token_amount": 34_612_903_225_806, "is_buy": True}
    pre = _curve_pre_price(e)
    post = (31_000_000_000 / 1e9) / (1_038_387_096_774_194 / 1e6)
    assert abs(post / pre - (31 / 30) ** 2) < 1e-6, post / pre
    sell = dict(e, is_buy=False)
    assert _curve_pre_price(sell) > post, "a sell lowers the price, so its pre-trade price is higher"
    assert _curve_pre_price(dict(e, sol_amount=10 ** 12)) is None
    # PumpSwap: pre/post from one BuyEvent, keyed by its exact pool; another pool is another key.
    from solders.pubkey import Pubkey
    pool_a, pool_b, mint = str(Pubkey.new_unique()), str(Pubkey.new_unique()), "MINT"
    _POOLS[pool_a] = (mint, fet.WSOL, 0)
    _POOLS[pool_b] = (mint, fet.USDC, 0)

    def buy_tx(pool, base, quote, base_out, quote_in):
        nums = [base_out, 0, 0, 0, base, quote, quote_in, 0, 0, 0, 0, quote_in, quote_in]
        raw = AMM_BUY + struct.pack("<q", 0) + b"".join(struct.pack("<Q", n) for n in nums)
        raw += bytes(Pubkey.from_string(pool))
        line = "Program data: " + base64.b64encode(raw).decode()
        return {"meta": {"err": None, "logMessages": [
            "Program %s invoke [1]" % PUMP_AMM, line, "Program %s success" % PUMP_AMM]}}
    pre, post, key = _trade_prices(buy_tx(pool_a, 1_000, 100, 100, 20), mint)
    assert (pre, post, key) == (0.1, 120 / 900, "pumpswap:%s" % pool_a), (pre, post, key)
    assert _trade_prices(buy_tx(pool_b, 1_000, 100, 100, 20), mint)[2] != key
    assert pre_price({"meta": {"err": {"x": 1}}}, mint) == (None, "failed")
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("study", nargs="?", choices=("bundles", "premium"))
    ap.add_argument("--fees", nargs="*", default=[])
    ap.add_argument("--since", default="2026-09-24")
    ap.add_argument("--out", required=False)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    if a.study == "bundles":
        out = bundles(a.fees)
    else:
        out = []
        for f in funded(a.since):
            try:
                out.append(premium_one(f))
            except fet.Stop as exc:
                print(f"STOP: {exc}", file=sys.stderr)
                break
    Path(a.out or "/dev/stdout").write_text(json.dumps(out, indent=1))
    print(f"rows {len(out)} 429s {fet.RATE_429[0]}", file=sys.stderr)


if __name__ == "__main__":
    main()
