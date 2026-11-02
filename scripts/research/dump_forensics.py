#!/usr/bin/env python3
"""Are we exit liquidity? Chain forensics for funded Solana copies, one position at a time.

Operator question 2026-09-27: the stop-loss and vol_death losers since the slot lane and the
confirmed watcher went live entered +14..+25% over the source and died within 32-88 s. For each
position, from the chain only (research key, never memecoin_position_marks):
  1. the SOURCE wallet's buy and its first sell of the coin: seconds and slots after its buy and
     after our fill, and the share of its bag;
  2. the top sellers between our fill and our exit, flagged when they bought in the source's slot,
     bought between the source and us, are the coin's creator, or are the source;
  3. our landing position: buys between the source and us, and whether our fill was the local top
     (highest spot in the 30 s after it);
  4. had we filled right behind the source (its post-trade spot + 3.5%, tonight's measured own
     impact + curve fee), would that entry have been above water when the source sold?

Research key only (funded_exit_truth.rpc: first MEMECOIN_RESEARCH_HELIUS_RPC_URLS entry, funded
overlap refused), bounded 429 backoff, stops on quota. One JSON line per position. The source's
sell comes from its own token account for the mint; `src_sell_search` and `window_reaches_fill` say
how complete each row is.

    python3 scripts/research/dump_forensics.py 65891 65922 ...
    python3 scripts/research/dump_forensics.py --selftest
"""
import argparse
import contextlib
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
import funded_exit_truth as fet  # noqa: E402

fet.SPACING_S = 0.5        # bounded 429 backoff in fet.rpc; the key is shared with runner_scanner and prewarm
WSOL, USDC, PUMP, TRADE_DISC = fet.WSOL, fet.USDC, fet.PUMP, fet.TRADE_DISC
S_ENTRY_MARKUP = 1.035      # own impact + curve fee measured on tonight's S+1 fills (1.5-2.3% + 1.25%)
HELD_CAP = 180
MAX_PAGES = 20             # 65568's mint ran ~140 tx/s: 6 pages covered 42 s and never reached our fill
# Some coins trade against Portal WETH, not SOL (65856: Raydium CPMM WETH/G2KB6Yvh). ETH_USD is the
# route rate of that position's source buy (1000 USDC -> 0.36963352 WETH, sig 64pw4d2m); --eth-usd.
WETH = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
ETH_USD = 2705.0


def _signers(tx):
    """The transaction's signers, fee payer first."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    if keys and isinstance(keys[0], dict):                       # jsonParsed encoding
        return [k.get("pubkey") for k in keys if k.get("signer")]
    return list(keys[:int((msg.get("header") or {}).get("numRequiredSignatures") or 0)])


def _account_keys(tx):
    """Static keys then the lookup-table keys: the order preBalances/postBalances index."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k if isinstance(k, str) else (k or {}).get("pubkey") for k in msg.get("accountKeys") or []]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])


def _own_sol_lamports(tx, trader, deltas, accounts, mint):
    """The trader's own SOL change in lamports: native plus WSOL, with the fee added back when it paid
    it, and net of the rent of its own coin or WSOL token accounts this transaction opened or closed."""
    meta = tx["meta"]
    keys, pre, post = _account_keys(tx), meta.get("preBalances") or [], meta.get("postBalances") or []
    lam = deltas.get((trader, WSOL), 0)
    if trader in keys:
        i = keys.index(trader)
        if i < len(pre) and i < len(post):
            lam += post[i] - pre[i]
    if keys and keys[0] == trader:
        lam += int(meta.get("fee") or 0)
    for j, (owner, m, pre_amount, post_amount) in accounts.items():
        if owner != trader or m not in (mint, WSOL) or j >= len(pre) or j >= len(post):
            continue
        if pre[j] == 0 and post[j] > 0:              # opened here: its rent was paid, not traded
            lam += post[j] - (post_amount if m == WSOL else 0)
        elif post[j] == 0 and pre[j] > 0:            # closed here: its rent came back, not traded
            lam -= pre[j] - (pre_amount if m == WSOL else 0)
    return lam


def trade_of(tx, mint, sol_usd):
    """{trader, side, sol, tokens, price, creator, kind, quote} for the trade on `mint`, or None.

    A pump-curve trade is read from its TradeEvent. Anything else is read from the ONE SIGNER whose
    balance of `mint` changed, priced by the quote that moved the opposite way: the signer's own USDC,
    then USDT, then WETH; then SOL, taken as the WSOL every other account took in or paid out (so a native tip is
    never the price), or, where no WSOL moved, the signer's own SOL (native plus WSOL, the fee added
    back, the rent of its own token accounts netted). No signer moving the coin, two signers moving it,
    or no opposite-signed quote leg: None.
    (2026-10-01, Gamma's dip grade: the old rule priced the "pool" leg, so an aggregator route priced
    on an intermediate's dust WSOL leg came out 118x low, and a DAMM v2 liquidity deposit read as a buy
    by the pool authority, which never signed.)

    `quote` names the unit: 'SOL', 'USDC', 'USDT' or 'WETH'. `sol` is the quote amount converted at the
    caller's `sol_usd` (USDC and USDT at 1.0, WETH at ETH_USD), so a SOL-only caller must check `quote`.

    KNOWN BIASES (stated, not corrected): pool and protocol fees paid in WSOL to other accounts stay in
    the price, as on any venue. Only where no WSOL moved does the price come from the trader's own SOL,
    and there tips and platform fees beside the swap stay in it (dip-set 63LC6ePL: a 0.002 SOL tip on a
    0.003 SOL sale, which the WSOL rule removes). Rent of the trader's own coin and WSOL token accounts
    is netted on that path (an SPL ATA is 2,039,280 lamports, 20% of a 0.01 SOL trade); rent of any
    other account it funded is not.
    """
    if tx is None or (tx.get("meta") or {}).get("err") is not None:
        return None
    from sniper.execution.pump_curve_buy import parse_trade_event
    from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes
    from sniper.services.memecoin_opportunity_live import program_data_logs
    for _i, line in program_data_logs(tx["meta"].get("logMessages") or [], PUMP):
        try:
            if anchor_event_bytes(line)[:8] != TRADE_DISC:
                continue
            e = parse_trade_event(line)
        except Exception:
            continue
        if e.get("mint") == mint and e.get("virtual_token_reserves") and e.get("virtual_sol_reserves"):
            return {"trader": e["user"], "side": "buy" if e["is_buy"] else "sell",
                    "sol": e["sol_amount"] / 1e9, "tokens": e["token_amount"] / 1e6,
                    "price": (e["virtual_sol_reserves"] / 1e9) / (e["virtual_token_reserves"] / 1e6) * sol_usd,
                    "creator": e.get("creator"), "kind": "curve", "quote": "SOL"}
    meta = tx["meta"]
    deltas, dec, accounts = {}, {}, {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            amount = int(b["uiTokenAmount"]["amount"])
            key = (b.get("owner"), b.get("mint"))
            deltas[key] = deltas.get(key, 0) + sign * amount
            dec[b.get("mint")] = b["uiTokenAmount"]["decimals"]
            if b.get("accountIndex") is not None:
                rec = accounts.setdefault(b["accountIndex"], [b.get("owner"), b.get("mint"), 0, 0])
                rec[2 if sign < 0 else 3] = amount
    movers = [s for s in _signers(tx) if deltas.get((s, mint))]
    if len(movers) != 1:
        return None
    trader = movers[0]
    coin = deltas[(trader, mint)]
    for quote, unit, to_usd in ((USDC, "USDC", 1.0), (USDT, "USDT", 1.0), (WETH, "WETH", ETH_USD),
                                (WSOL, "SOL", sol_usd)):
        if quote == WSOL:
            # The SOL leg is the WSOL the OTHER side of the trade took in or paid out: tips and fees the
            # trader sent in native SOL never become WSOL, and its own wraps cancel. A venue that holds
            # native SOL (a pump curve whose event did not parse) moves no WSOL: the trader's own SOL.
            others = sum(v for (o, m), v in deltas.items() if m == WSOL and o != trader)
            amount = -others if others else _own_sol_lamports(tx, trader, deltas, accounts, mint)
        else:
            amount = deltas.get((trader, quote), 0)
        if amount and (amount > 0) != (coin > 0):
            break
    else:
        return None
    usd = abs(amount) / 10 ** dec.get(quote, 9 if quote == WSOL else 6) * to_usd
    dt = abs(coin) / 10 ** dec.get(mint, 6)
    return {"trader": trader, "side": "buy" if coin > 0 else "sell", "sol": usd / sol_usd, "tokens": dt,
            "price": usd / dt, "creator": None, "kind": "pool", "quote": unit}


def owner_bag(tx, owner, mint):
    """(pre, post) token balance of `owner` on `mint` in this tx."""
    pre = sum(int(b["uiTokenAmount"]["amount"]) for b in tx["meta"].get("preTokenBalances") or []
              if b.get("owner") == owner and b.get("mint") == mint)
    post = sum(int(b["uiTokenAmount"]["amount"]) for b in tx["meta"].get("postTokenBalances") or []
               if b.get("owner") == owner and b.get("mint") == mint)
    return pre, post


def owner_token_account(tx, owner, mint):
    """The address of `owner`'s token account for `mint` in this tx, or None."""
    if not tx:
        return None
    loaded = tx["meta"].get("loadedAddresses") or {}
    keys = tx["transaction"]["message"]["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])
    return next((keys[b["accountIndex"]] for b in tx["meta"].get("postTokenBalances") or []
                 if b.get("owner") == owner and b.get("mint") == mint), None)


def get_tx(sig):
    return fet.rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                            "commitment": "confirmed"}])


def load(ids):
    q = ",".join("?" * len(ids))
    with contextlib.closing(sqlite3.connect(f"file:{REPO / 'sniper_tracker.db'}?mode=ro", uri=True,
                                            timeout=10)) as c:
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            f"SELECT p.id, p.mint, p.source_user_id, p.source_handle, p.opened_recorded_at, p.closed_at, "
            f"p.exit_reason, p.entry_price, p.entry_notional, p.realized_pnl, p.close_sig, "
            f"a.origin_event_id, COALESCE(a.tx_sig, pe.tx_sig) AS entry_sig, a.source_handle AS handle "
            f"FROM memecoin_positions p LEFT JOIN memecoin_entry_attempts a ON a.attempt_id=p.attempt_id "
            f"LEFT JOIN memecoin_pending_entries pe ON pe.attempt_id=p.attempt_id "
            f"WHERE p.id IN ({q}) ORDER BY p.id", ids)]
        for r in rows:
            uid = r["source_user_id"] or ""
            r["owner"] = uid[5:] if uid.startswith("pump:") else next(
                (a for (a,) in c.execute(
                    "SELECT address FROM memecoin_address_registry WHERE source_user_id=? "
                    "AND network_id='solana' AND status IN ('verified','registry_verified') LIMIT 1",
                    (uid,))), None)
            oid = r["origin_event_id"] or ""
            r["source_sig"] = (oid.split(":", 1)[1] if oid.startswith(("slotlane:", "onchain:"))
                               else oid.split(":")[2] if oid.startswith("swap:raybot:") else None)
    return rows


def analyse(p, sol_usd):
    mint, owner = p["mint"], p["owner"]
    t_fill, t_exit = fet.epoch(p["opened_recorded_at"]), fet.epoch(p["closed_at"])
    # Every tx on the mint from ~2 min before our fill to our exit, oldest first.
    sigs, cursor = [], p["close_sig"]
    for _ in range(MAX_PAGES):
        page = fet.rpc("getSignaturesForAddress",
                       [mint, {"limit": 1000, "commitment": "confirmed", **({"before": cursor} if cursor else {})}]) or []
        sigs.extend(page)
        time.sleep(3.0)   # a 1000-row page is a heavy call: the next call 1.1 s later 429d (03:52Z)
        if len(page) < 1000 or (page and (page[-1].get("blockTime") or 0) < t_fill - 120):
            break
        cursor = page[-1]["signature"]
    window_reaches_fill = bool(sigs) and (sigs[-1].get("blockTime") or 0) < t_fill - 120 or 0 < len(sigs) % 1000
    sigs = [s for s in reversed(sigs) if s.get("err") is None and (s.get("blockTime") or 0) >= t_fill - 120]
    pos = {s["signature"]: i for i, s in enumerate(sigs)}
    i_ours = pos.get(p["entry_sig"])
    i_src = pos.get(p["source_sig"]) if p["source_sig"] else None
    lo = (i_src - 3) if i_src is not None else max(0, (i_ours or 0) - 40)
    window = sigs[max(0, lo):]
    if len(window) > HELD_CAP and i_ours is None:
        # Our own fill is not in the list (no entry signature recorded): sample evenly rather than
        # decode the whole window (65568 decoded for 12 min this way on 2026-09-27).
        window = window[::max(1, len(window) // HELD_CAP)]
    elif len(window) > HELD_CAP:
        head = [s for s in window if pos[s["signature"]] <= i_ours + 20]
        rest = [s for s in window if s not in head]
        step = max(1, len(rest) // max(1, HELD_CAP - len(head)))
        window = head + rest[::step]
    trades = []
    for s in window:
        tx = get_tx(s["signature"])
        t = trade_of(tx, mint, sol_usd)
        if t:
            t.update(sig=s["signature"], slot=s["slot"], time=s.get("blockTime"), i=pos[s["signature"]])
            if owner and t["trader"] == owner and t["side"] == "sell":
                t["bag"] = owner_bag(tx, owner, mint)
            trades.append(t)
    if i_src is None and owner:   # J7 uuid: the source's buy is the owner's last buy before our fill
        src = [t for t in trades if t["trader"] == owner and t["side"] == "buy"
               and (i_ours is None or t["i"] < i_ours)]
        i_src = src[-1]["i"] if src else None
    src_t = next((t for t in trades if t["i"] == i_src), None)
    ours = next((t for t in trades if t["i"] == i_ours), None)
    S = src_t["slot"] if src_t else None
    between = [t for t in trades if i_src is not None and i_ours is not None and i_src < t["i"] < i_ours]
    buys_between = [t for t in between if t["side"] == "buy"]
    after = [t for t in trades if i_ours is not None and t["i"] > i_ours]
    in30 = [t for t in after if (t["time"] or 0) <= (ours["time"] if ours else t_fill) + 30]
    local_top = bool(ours and in30 and all(t["price"] <= ours["price"] for t in in30))
    early = {t["trader"] for t in trades if t["side"] == "buy" and S is not None
             and (t["slot"] == S or (i_src is not None and i_ours is not None and i_src <= t["i"] < i_ours))}
    creator = next((t["creator"] for t in trades if t.get("creator")), None)
    sellers = {}
    for t in after:
        if t["side"] == "sell":
            sellers[t["trader"]] = sellers.get(t["trader"], 0.0) + t["sol"]
    top = sorted(sellers.items(), key=lambda kv: -kv[1])[:4]
    sold_total = sum(sellers.values())
    # The source's first sell of this coin, up to 10 min after our exit, from the signature list of
    # the source's own token account for the mint: only its trades on this coin. The owner's address
    # list is useless here (notdecu's held 1000 sigs in 23 s on 2026-09-27, so "not found" was false).
    src_sell, src_sell_search = None, "no_source_buy"
    if owner and src_t:
        ata = owner_token_account(get_tx(src_t["sig"]), owner, mint)
        src_sell_search = "no_token_account"
        if ata:
            asigs = fet.rpc("getSignaturesForAddress", [ata, {"limit": 1000, "commitment": "confirmed",
                                                              "until": src_t["sig"]}]) or []
            cand = [s for s in reversed(asigs) if s.get("err") is None and (s.get("blockTime") or 0) <= t_exit + 600]
            src_sell_search = f"token_account:{len(cand)}" + ("+truncated" if len(asigs) == 1000 else "")
            for s in cand[:25]:
                tx = get_tx(s["signature"])
                t = trade_of(tx, mint, sol_usd)
                if t and t["trader"] == owner and t["side"] == "sell":
                    t.update(sig=s["signature"], slot=s["slot"], time=s.get("blockTime"), bag=owner_bag(tx, owner, mint))
                    src_sell = t
                    break
    if src_sell is None:    # sampled hold window: a sell here may not be the first one
        src_sell = next((t for t in after if owner and t["trader"] == owner and t["side"] == "sell"), None)
        if src_sell:
            src_sell_search += "|hold_window"
    entry = float(p["entry_price"])
    hyp = src_t["price"] * S_ENTRY_MARKUP if src_t else None
    pre_sell = None
    if src_sell:
        prior = [t for t in trades if t["time"] and src_sell["time"] and t["time"] <= src_sell["time"]
                 and t.get("sig") != src_sell.get("sig")]
        pre_sell = prior[-1]["price"] if prior else None
    pct = lambda a, b: None if a is None or b is None else round((a / b - 1) * 100, 1)
    after_sell = {}
    if src_sell and src_sell.get("time"):
        tail = [t for t in trades if t["time"] and t["time"] >= src_sell["time"] and t.get("sig") != src_sell.get("sig")]
        if not tail or tail[-1]["time"] < src_sell["time"] + 4:   # the sell came after our exit: read on
            more = fet.rpc("getSignaturesForAddress", [mint, {"limit": 1000, "commitment": "confirmed",
                                                               "until": src_sell["sig"]}]) or []
            time.sleep(3.0)
            near = [x for x in reversed(more) if x.get("err") is None
                    and src_sell["time"] <= (x.get("blockTime") or 0) <= src_sell["time"] + 5][:12]
            for x in near:
                t = trade_of(get_tx(x["signature"]), mint, sol_usd)
                if t:
                    t.update(sig=x["signature"], slot=x["slot"], time=x.get("blockTime"))
                    tail.append(t)
        for n in (2, 4):
            at = [t for t in tail if t["time"] <= src_sell["time"] + n]
            after_sell[n] = at[-1]["price"] if at else None
    bag = (src_sell or {}).get("bag")
    return {
        "id": p["id"], "handle": p["handle"] or p["source_handle"], "mint": mint[:8],
        "exit": p["exit_reason"], "pnl": round(float(p["realized_pnl"] or 0), 2),
        "S": S, "ours_slot": ours and ours["slot"], "land_minus_S": (ours["slot"] - S) if ours and S else None,
        "buys_between": len(buys_between), "sol_bought_between": round(sum(t["sol"] for t in buys_between), 2),
        "ours_vs_source_spot_pct": pct(ours["price"] if ours else None, src_t["price"] if src_t else None),
        "bought_local_top_30s": local_top,
        "src_sell_after_src_buy_s": (src_sell["time"] - src_t["time"]) if src_sell and src_t else None,
        "src_sell_after_our_fill_s": (src_sell["time"] - ours["time"]) if src_sell and ours else None,
        "src_sell_slots_after_S": (src_sell["slot"] - S) if src_sell and S else None,
        "src_sold_share_of_bag_pct": round((bag[0] - bag[1]) / bag[0] * 100) if bag and bag[0] else None,
        "src_buy": src_t and {"sig": src_t["sig"], "slot": src_t["slot"], "time": src_t["time"]},
        "src_sell": src_sell and {"sig": src_sell["sig"], "slot": src_sell["slot"], "time": src_sell["time"]},
        "src_sell_search": src_sell_search,
        "our_fill": ours and {"slot": ours["slot"], "time": ours["time"]},
        "our_entry_vs_price_at_src_sell_pct": pct(pre_sell, entry),
        "S_fill_vs_price_at_src_sell_pct": pct(pre_sell, hyp),
        "our_entry_vs_spot_2s_after_src_sell_pct": pct(after_sell.get(2), entry),
        "our_entry_vs_spot_4s_after_src_sell_pct": pct(after_sell.get(4), entry),
        "top_sellers": [{"sol": round(v, 2), "early_buyer": k in early, "creator": k == creator,
                         "source": k == owner} for k, v in top],
        "sold_sol_total": round(sold_total, 2), "trades_decoded": len(trades), "txs_in_window": len(sigs), "window_reaches_fill": window_reaches_fill,
        "source_sig_known": p["source_sig"] is not None, "owner_known": owner is not None,
    }


def selftest():
    tx = {"meta": {"err": None, "logMessages": [], "preTokenBalances": [
        {"owner": "POOL", "mint": "M", "uiTokenAmount": {"amount": "1000000", "decimals": 6}},
        {"owner": "POOL", "mint": WSOL, "uiTokenAmount": {"amount": "5000000000", "decimals": 9}},
        {"owner": "T", "mint": "M", "uiTokenAmount": {"amount": "0", "decimals": 6}}],
        "postTokenBalances": [
        {"owner": "POOL", "mint": "M", "uiTokenAmount": {"amount": "900000", "decimals": 6}},
        {"owner": "POOL", "mint": WSOL, "uiTokenAmount": {"amount": "5010000000", "decimals": 9}},
        {"owner": "T", "mint": "M", "uiTokenAmount": {"amount": "100000", "decimals": 6}}]}}
    t = trade_of(tx, "M", 100.0)
    assert t["trader"] == "T" and t["side"] == "buy" and abs(t["price"] - 10.0) < 1e-9, t
    assert owner_bag(tx, "T", "M") == (0, 100000)
    # WETH-quoted pool (8 decimals): 0.01 WETH for 0.1 M at ETH_USD 2705 -> $270.50/M, a sell by T.
    W = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"    # literal, so a wrong WETH constant fails
    weth = {"meta": {"err": None, "logMessages": [], "preTokenBalances": [
        {"owner": "POOL", "mint": "M", "uiTokenAmount": {"amount": "1000000", "decimals": 6}},
        {"owner": "POOL", "mint": W, "uiTokenAmount": {"amount": "5000000", "decimals": 8}},
        {"owner": "T", "mint": "M", "uiTokenAmount": {"amount": "100000", "decimals": 6}}],
        "postTokenBalances": [
        {"owner": "POOL", "mint": "M", "uiTokenAmount": {"amount": "1100000", "decimals": 6}},
        {"owner": "POOL", "mint": W, "uiTokenAmount": {"amount": "4000000", "decimals": 8}},
        {"owner": "T", "mint": "M", "uiTokenAmount": {"amount": "0", "decimals": 6}}]}}
    v0 = {"transaction": {"message": {"accountKeys": ["FEE", "PROG"]}},
          "meta": {"loadedAddresses": {"writable": ["POOL_ACC", "T_ACC"], "readonly": ["X"]},
                   "postTokenBalances": [{"accountIndex": 3, "owner": "T", "mint": "M"},
                                         {"accountIndex": 2, "owner": "POOL", "mint": "M"}]}}
    assert owner_token_account(v0, "T", "M") == "T_ACC" and owner_token_account(v0, "T", "N") is None
    t = trade_of(weth, "M", 100.0)
    assert t["trader"] == "T" and t["side"] == "sell" and abs(t["price"] - 270.5) < 1e-9, t
    assert abs(t["sol"] - 0.2705) < 1e-12, t
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", type=int)
    ap.add_argument("--sol-usd", type=float, default=121.0)
    ap.add_argument("--eth-usd", type=float, default=ETH_USD)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    globals()["ETH_USD"] = args.eth_usd
    rows = {p["id"]: p for p in load(args.ids)}
    for p in (rows[i] for i in args.ids if i in rows):     # the order asked for, not id order
        try:
            print(json.dumps(analyse(p, args.sol_usd)), flush=True)
        except fet.Stop as exc:
            print(json.dumps({"id": p["id"], "STOPPED": str(exc)}), flush=True)
            return


if __name__ == "__main__":
    main()
