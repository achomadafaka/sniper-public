#!/usr/bin/env python3
"""Who landed ahead of our funded Solana buys, what they paid, and which armed wallets draw a crowd.

Operator order 2026-09-27 ("Go, do both"), via Alpha. Read-only, chain only, on the research key
(funded_exit_truth.rpc: the first MEMECOIN_RESEARCH_HELIUS_RPC_URLS entry, which refuses a key shared
with a funded variable, backs off on a rate 429 and stops on a quota 429). Never the funded RPC.

  fees   For each funded Solana fill since --since (or --ids): every transaction on the mint from
         the source's slot to ours (getTransactionsForAddress, full, in block order), with its block
         index, compute-unit price and limit, CU consumed, fee, Jito tip, signer and top-level
         programs, and whether it bought. The transactions between the source's buy and ours are
         the copiers ahead of us.
  crowd  For each armed Solana wallet (registry owner): its last ~20 buys; per buy the other buys on
         the mint in slots S..S+2, whether the wallet sold >=30% within 120 s, and the price 60 s
         later against its own fill.

    python3 scripts/research/copier_crowd_and_fees.py fees --since 2026-09-26T21:30
    python3 scripts/research/copier_crowd_and_fees.py fees --ids 65854 65906
    python3 scripts/research/copier_crowd_and_fees.py crowd --buys 20
    python3 scripts/research/copier_crowd_and_fees.py --selftest
"""
import argparse
import contextlib
import json
import struct
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
import funded_exit_truth as fet  # noqa: E402
from sniper.persistence.db_core import get_readonly_connection  # noqa: E402
import dump_forensics as dfx  # noqa: E402

fet.SPACING_S = 0.5       # the key is shared with the runner's research lanes (pool prewarm, scanner)
LEDGER = str(REPO / "sniper_tracker.db")
FLAT = threading.Event()
FLAT_POLL_S = 2.0


def open_solana_book(conn):
    """Open real Solana positions plus reserved Solana entries: research reads wait while any exist."""
    return (conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                         "AND network_id='solana' AND is_open=1 AND COALESCE(tracking_paused_at,'')=''").fetchone()[0]
            + conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved' "
                           "AND COALESCE(network_id,'solana')='solana'").fetchone()[0])


def install_flat_guard():
    """Poll the book every FLAT_POLL_S and hold every research-key call while it is not flat.
    A call already in flight when a position opens still completes (at most one call, 30 s)."""
    def watch():
        with contextlib.closing(get_readonly_connection(LEDGER)) as c:
            while True:
                (FLAT.set if open_solana_book(c) == 0 else FLAT.clear)()
                time.sleep(FLAT_POLL_S)

    threading.Thread(target=watch, daemon=True).start()
    raw = fet.rpc

    def guarded(method, params):
        hold_until_flat(FLAT_POLL_S * 2)
        return raw(method, params)

    fet.rpc = guarded           # dfx.get_tx and mint_txs call fet.rpc through the module


def hold_until_flat(grace_s):
    if not FLAT.wait(grace_s):      # the first poll has not landed, or the book is open
        print("paused: real Solana position or reservation open", file=sys.stderr, flush=True)
        FLAT.wait()
        print("resumed: book flat", file=sys.stderr, flush=True)


COMPUTE_BUDGET ="ComputeBudget111111111111111111111111111111"
SYSTEM = "11111111111111111111111111111111"
JITO_TIP_ACCOUNTS = frozenset({
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5", "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY", "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh", "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL", "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
})
MAX_SLOTS = 40          # one read per 100 transactions now, not one per slot
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(text):
    n = 0
    for ch in text:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + raw


def account_keys(tx):
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return (tx["transaction"]["message"]["accountKeys"] + loaded.get("writable", [])
            + loaded.get("readonly", []))


def fee_facts(tx):
    """CU price/limit, Jito tip, top-level programs, CU consumed and priority lamports of one tx."""
    keys = account_keys(tx)
    meta = tx.get("meta") or {}
    msg = tx["transaction"]["message"]
    cu_price = cu_limit = None
    tip = 0
    sol_out = []           # every System transfer the signer paid: [recipient, lamports]; tips beyond Jito's
    programs = []
    inner = [i for grp in (meta.get("innerInstructions") or []) for i in grp.get("instructions", [])]
    for depth, ix in [(0, i) for i in msg["instructions"]] + [(1, i) for i in inner]:
        prog = keys[ix["programIdIndex"]]
        if depth == 0:
            programs.append(prog)
        try:
            data = b58decode(ix.get("data") or "")
        except ValueError:
            continue
        if prog == COMPUTE_BUDGET and depth == 0 and data:
            if data[0] == 2 and len(data) >= 5:
                cu_limit = struct.unpack_from("<I", data, 1)[0]
            elif data[0] == 3 and len(data) >= 9:
                cu_price = struct.unpack_from("<Q", data, 1)[0]
        elif prog == SYSTEM and len(data) >= 12 and struct.unpack_from("<I", data, 0)[0] == 2:
            accts = ix.get("accounts") or []
            if len(accts) >= 2 and keys[accts[1]] in JITO_TIP_ACCOUNTS:
                tip += struct.unpack_from("<Q", data, 4)[0]
            if len(accts) >= 2 and keys[accts[0]] == keys[0] and keys[accts[1]] != keys[0]:
                sol_out.append([keys[accts[1]], struct.unpack_from("<Q", data, 4)[0]])
    # A version-1 transaction carries no ComputeBudget instructions: its limit and TOTAL priority fee
    # sit in message.transactionConfig. Its CU price is that fee over that limit (2026-09-28: a v1 pump
    # buy ahead of 66364 paid 6,600,000 lamports at a 68,200 limit, 96.8M uL/CU, and read as no bid).
    config = msg.get("transactionConfig") or {}
    if config.get("computeUnitLimit"):
        cu_limit = config["computeUnitLimit"]
        cu_price = (config.get("priorityFee") or 0) * 1_000_000 // cu_limit
    nsig = len(tx["transaction"].get("signatures") or [1])
    fee = meta.get("fee")
    priority = (fee - 5000 * nsig) if isinstance(fee, int) else None
    consumed = meta.get("computeUnitsConsumed")
    return {"cu_price": cu_price, "cu_limit": cu_limit, "cu_consumed": consumed, "fee": fee,
            "priority_lamports": priority,
            "priority_per_cu_consumed": (priority * 1e6 / consumed
                                         if priority is not None and consumed else None),
            "tip_lamports": tip, "sol_out": sol_out, "signer": keys[0],
            "programs": sorted(set(p for p in programs if p not in (COMPUTE_BUDGET, SYSTEM)))}


def fills(since):
    with contextlib.closing(get_readonly_connection(LEDGER)) as c:
        rows = c.execute(
            "SELECT p.id, p.mint, p.opened_at, p.source_user_id, p.exit_reason, f.tx_sig, "
            "a.delivery_lane, a.origin_event_id, a.trace_json FROM memecoin_positions p "
            "JOIN memecoin_fills f ON f.position_id=p.id AND f.kind='entry' "
            "LEFT JOIN memecoin_entry_attempts a ON a.attempt_id=p.attempt_id "
            "WHERE p.execution_mode='real' AND p.network_id='solana' AND p.opened_at>=? "
            "ORDER BY p.opened_at", (since,)).fetchall()
        out = []
        for r in rows:
            tr = json.loads(r["trace_json"] or "{}")
            oid = str(r["origin_event_id"] or "")
            src = (tr.get("source_evidence") or {}).get("signature")
            if not src and oid.startswith("swap:raybot:"):
                src = oid.split(":")[2]
            if not src and oid.startswith("slotlane:"):
                src = oid.split(":", 1)[1]
            if not src and oid.startswith("onchain:"):
                hit = c.execute("SELECT signature FROM onchain_wallet_detections WHERE signature LIKE ?",
                                (oid.split(":", 1)[1] + "%",)).fetchone()
                src = hit[0] if hit else None
            basis = candidates = None
            if src:
                basis = "exact"
            else:
                src, candidates = heuristic_source(c, r["mint"], r["source_user_id"], r["opened_at"])
                basis = "heuristic_window" if src else None
            out.append({"position_id": r["id"], "mint": r["mint"], "opened_at": r["opened_at"],
                        "lane": r["delivery_lane"], "exit_reason": r["exit_reason"],
                        "ours": r["tx_sig"], "source": src, "source_basis": basis,
                        "heuristic_candidates": candidates})
        return out


HEURISTIC_WINDOW_S = 180


def parse_utc(text):
    """An aware UTC datetime from an ISO-8601 string with a 'T' or a space and any offset (a
    naive value is the ledger's UTC); None when missing or malformed."""
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def heuristic_source(conn, mint, source_user_id, opened_at, window_s=HEURISTIC_WINDOW_S):
    """(signature, candidates): the source wallet's latest on-chain detection of this mint at most
    window_s BEFORE our fill, bounds inclusive.

    S27-06 (Codex, 2026-09-27): the old query compared ISO text holding 'T' with SQLite datetime()
    text holding a space, so any same-day detection passed the lower bound; an 00:00 detection
    matched an 08:00 fill. Both bounds are now compared as parsed UTC instants, so the separator, the
    fraction and the offset cannot decide. It is still a heuristic: a matching time window is not
    transaction identity, and `candidates` > 1 says two buys fitted it."""
    t_fill = parse_utc(opened_at)
    if t_fill is None:
        return None, 0
    lo = t_fill - timedelta(seconds=window_s)
    hits = []
    for sig, detected_at in conn.execute(
            "SELECT signature, detected_at FROM onchain_wallet_detections "
            "WHERE mint=? AND source_user_id=?", (mint, source_user_id)):
        t = parse_utc(detected_at)
        if t is not None and lo <= t <= t_fill:
            hits.append((t, sig))
    if not hits:
        return None, 0
    return max(hits)[1], len(hits)


def mint_txs(mint, lo, hi):
    """Every transaction naming the mint in slots lo..hi, in block order, from Helius
    getTransactionsForAddress. It replaced a full getBlock per slot. On 65813 it returned the same
    44 signatures at the same block indexes as the getBlock scan (2026-09-27)."""
    out, token = [], None
    while True:
        opts = {"transactionDetails": "full", "sortOrder": "asc", "limit": 100, "encoding": "json",
                "maxSupportedTransactionVersion": 1, "commitment": "confirmed",
                "filters": {"slot": {"gte": lo, "lte": hi}}}
        if token:
            opts["paginationToken"] = token
        res = fet.rpc("getTransactionsForAddress", [mint, opts]) or {}
        page = res.get("data") or []
        out.extend(page)
        token = res.get("paginationToken")
        if not token or len(page) < 100:
            return out


def _sol_trade(tx, mint):
    """trade_of with its SOL figures kept only when it priced in SOL. A USDC, USDT or WETH amount is
    never a SOL column here (Gamma LOW, 2026-10-01); side and tokens stay."""
    t = dfx.trade_of(tx, mint, 1.0)
    if t and t.get("quote") != "SOL":
        t = dict(t, sol=None, price=None)
    return t


def _mint_rows(f, lo, hi):
    rows = []
    for tx in mint_txs(f["mint"], lo, hi):
        sig = tx["transaction"]["signatures"][0]
        trade = _sol_trade(tx, f["mint"])
        rows.append({"slot": tx["slot"], "index": tx["transactionIndex"], "sig": sig,
                     "ok": (tx.get("meta") or {}).get("err") is None,
                     "role": ("ours" if sig == f["ours"] else "source" if sig == f["source"]
                              else None),
                     "side": trade["side"] if trade else None,
                     "sol": round(trade["sol"], 4) if trade and trade["sol"] is not None else None,
                     "tokens": trade.get("tokens") if trade else None, **fee_facts(tx)})
    return rows


def fees_one(f):
    """Our fill's slot, and when the source is known, everything between it and us.

    S27-07 (Codex, 2026-09-27): a missing source used to become our own slot and report "0 slots
    behind". Source-relative fields are now None unless the source transaction was read, and
    `status` says why: source_not_identified, source_not_found, source_after_ours or
    window_truncated. A provider failure raises fet.Stop and ends the run; it never becomes a row.
    S27-08: the CU price reported is the highest OBSERVED before us, not a price that would have
    led; tips are counted as tip transfers, not as bundles; buys are counted from decoded sides."""
    ours = dfx.get_tx(f["ours"])
    if not ours:
        return {**f, "status": "our_tx_not_found"}
    s_us = ours["slot"]
    status, s_src = "ok", None
    if not f["source"]:
        status = "source_not_identified"
    else:
        src = dfx.get_tx(f["source"])
        if not src:
            status = "source_not_found"
        elif src["slot"] > s_us:
            status = "source_after_ours"
        else:
            s_src = src["slot"]
            if s_us - s_src > MAX_SLOTS - 1:
                status = "window_truncated"
    if status == "ok":
        rows = _mint_rows(f, s_src, s_us)
    else:
        rows = _mint_rows(f, s_us, s_us)           # our own slot is still ours to describe
    pos = {r["role"]: (r["slot"], r["index"]) for r in rows if r["role"]}
    us, source = pos.get("ours"), (pos.get("source") if status == "ok" else None)
    if status == "ok" and source is None:
        status = "source_not_in_window"
    for r in rows:
        key = (r["slot"], r["index"])
        r["ahead"] = bool(source and us and source < key < us and r["ok"] and r["side"] == "buy")
        r["same_slot_after_us"] = bool(us and r["slot"] == us[0] and r["index"] > us[1])
    before = [r for r in rows if us and r["slot"] == us[0] and r["index"] < us[1] and r["ok"]]
    buys_before = [r for r in before if r["side"] == "buy"]
    known = status == "ok"
    return {**f, "status": status, "source_slot": s_src if known else None, "our_slot": s_us,
            "slots_behind": (s_us - s_src) if known else None,
            "our_index": us[1] if us else None, "rows": rows,
            "ahead_n": sum(r["ahead"] for r in rows) if known else None,
            "same_slot_after_us_n": sum(r["same_slot_after_us"] for r in rows),
            "same_slot_txs_before_us_n": len(before),
            "same_slot_buys_before_us_n": len(buys_before),
            "highest_observed_cu_price_before_us": max(
                (r["cu_price"] or 0 for r in before), default=None),
            "tip_transfers_before_us_n": sum(1 for r in before if r["tip_lamports"])}


def _q(values, q):
    v = sorted(x for x in values if x is not None)
    return v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))] if v else None


def report(fills_out):
    """The audit's tables from a `fees --out` file, so every number in the doc can be re-derived.
    Only fills whose source transaction was read (status ok) enter source-relative counts."""
    ok = [f for f in fills_out if f.get("status") == "ok"]
    ahead = [r for f in ok for r in f["rows"] if r["ahead"]]
    ours = [r for f in fills_out for r in f.get("rows", []) if r["role"] == "ours"]
    srcs = [r for f in ok for r in f["rows"] if r["role"] == "source"]

    def fees(rows):
        tips = [r["tip_lamports"] for r in rows if r["tip_lamports"]]
        return {"n": len(rows),
                "cu_price_p10_p50_p90_max": [_q([r["cu_price"] or 0 for r in rows], q)
                                             for q in (0.1, 0.5, 0.9)]
                + [max((r["cu_price"] or 0 for r in rows), default=None)],
                "cu_limit_p50": _q([r["cu_limit"] for r in rows], 0.5),
                "cu_consumed_p10_p50_p90": [_q([r["cu_consumed"] for r in rows], q) for q in (0.1, 0.5, 0.9)],
                "tip_transfers": len(tips), "tip_p50": _q(tips, 0.5), "tip_max": max(tips, default=None)}

    lane = [f for f in ok if str(f.get("lane") or "").startswith("slot_lane")]
    where = {"source_slot": [0, 0.0], "between": [0, 0.0], "our_slot": [0, 0.0]}
    for f in lane:
        for r in f["rows"]:
            if r["ahead"]:
                key = ("source_slot" if r["slot"] == f["source_slot"] else
                       "our_slot" if r["slot"] == f["our_slot"] else "between")
                where[key][0] += 1
                where[key][1] = round(where[key][1] + (r["sol"] or 0), 2)
    before = [r for f in fills_out for r in f.get("rows", []) if f.get("our_index") is not None
              and r["slot"] == f["our_slot"] and r["index"] < f["our_index"] and r["ok"]
              and r["side"] == "buy"]
    return {"fills": len(fills_out),
            "status": {s: sum(1 for f in fills_out if f.get("status") == s)
                       for s in sorted({f.get("status") for f in fills_out})},
            "source_basis": {str(b): sum(1 for f in fills_out if f.get("source_basis") == b)
                             for b in sorted({str(f.get("source_basis")) for f in fills_out})},
            "buys_ahead": {**fees(ahead), "above_200k": sum(1 for r in ahead if (r["cu_price"] or 0) > 200_000),
                           "signers": len({r["signer"] for r in ahead}),
                           "gmgn": sum(1 for r in ahead if any(p.startswith("GMgnVFR8") for p in r["programs"]))},
            "ours": fees(ours), "sources": fees(srcs),
            "slot_lane_ahead_by_location": where,
            "buys_before_us_in_our_slot": fees(before),
            "per_fill": [{k: f.get(k) for k in (
                "position_id", "lane", "status", "source_basis", "slots_behind", "our_index",
                "ahead_n", "same_slot_buys_before_us_n", "same_slot_txs_before_us_n",
                "same_slot_after_us_n", "highest_observed_cu_price_before_us",
                "tip_transfers_before_us_n")} | {"ahead_sol": round(sum(
                    r["sol"] or 0 for r in f.get("rows", []) if r["ahead"]), 2)} for f in fills_out]}


WSOL, USDC = fet.WSOL, fet.USDC
SCAN_CAP = 45          # wallet transactions decoded per wallet
CROWD_DECODE = 6       # crowd transactions decoded per buy for SOL volume (count is exact)
WINDOW_PAGES = 3


def owner_deltas(tx, owner):
    """{mint: (pre, post)} token amounts owned by `owner` in this tx (the token OWNER, not the
    signer: roster wallets trade through shared executors)."""
    out = {}
    meta = tx.get("meta") or {}
    for side, i in (("preTokenBalances", 0), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") != owner:
                continue
            cur = list(out.get(b["mint"], (0, 0)))
            cur[i] += int(b["uiTokenAmount"]["amount"])
            out[b["mint"]] = tuple(cur)
    return out


def _median(values):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def crowd_wallet(owner, want):
    page = fet.rpc("getSignaturesForAddress", [owner, {"limit": 200, "commitment": "confirmed"}]) or []
    sigs = [s for s in page if s.get("err") is None and s.get("blockTime")]
    fetched, buys = [], []
    for s in sigs[:SCAN_CAP]:
        tx = dfx.get_tx(s["signature"])
        if not tx:
            continue
        fetched.append((s, tx))
        for mint, (pre, post) in owner_deltas(tx, owner).items():
            if mint in (WSOL, USDC) or post <= pre:
                continue
            trade = _sol_trade(tx, mint)
            buys.append({"sig": s["signature"], "slot": tx["slot"], "t": s["blockTime"],
                         "mint": mint, "bought": post - pre,
                         "price": trade["price"] if trade else None,
                         "sol": round(trade["sol"], 4) if trade and trade["sol"] is not None else None})
        if len(buys) >= want:
            break
    out = []
    for b in buys[:want]:
        sold = sum(max(0, pre - post) for s, tx in fetched
                   if b["t"] <= s["blockTime"] <= b["t"] + 120 and s["signature"] != b["sig"]
                   for m_, (pre, post) in owner_deltas(tx, owner).items() if m_ == b["mint"])
        later = [s for s in sigs if s["blockTime"] >= b["t"] + 65]
        before = min(later, key=lambda s: s["blockTime"])["signature"] if later else None
        window, complete, cursor = [], False, before
        for _ in range(WINDOW_PAGES):
            opts = {"limit": 1000, "commitment": "confirmed", "until": b["sig"]}
            if cursor:
                opts["before"] = cursor
            pg = fet.rpc("getSignaturesForAddress", [b["mint"], opts]) or []
            window.extend(pg)
            if len(pg) < 1000:
                complete = True
                break
            cursor = pg[-1]["signature"]
        ok = [s for s in window if s.get("err") is None]
        crowd = [s for s in ok if s.get("slot") is not None and s["slot"] <= b["slot"] + 2]
        rec = {**b, "dump": (sold / b["bought"] >= 0.3) if b["bought"] else None,
               "sold_share_120s": round(sold / b["bought"], 3) if b["bought"] else None,
               "window_complete": complete, "crowd_n": len(crowd) if complete else None,
               "crowd_sol": None, "entry_s1": None, "price_60s": None}
        if complete and crowd:
            step = max(1, len(crowd) // CROWD_DECODE)
            sample = sorted(crowd[::step], key=lambda s: s["slot"])
            trades = [(s, _sol_trade(dfx.get_tx(s["signature"]), b["mint"])) for s in sample]
            buys_sol = [t["sol"] for _s, t in trades if t and t["side"] == "buy" and t["sol"] is not None]
            rec["crowd_sol"] = round(sum(buys_sol) * len(crowd) / len(sample), 3)
            s1 = [t["price"] for s, t in trades if t and t["price"] is not None and s["slot"] <= b["slot"] + 1]
            rec["entry_s1"] = s1[-1] if s1 else None
        if rec["entry_s1"] is None:
            rec["entry_s1"] = b["price"]
        at60 = sorted((s for s in ok if (s.get("blockTime") or 0) >= b["t"] + 60),
                      key=lambda s: (s["blockTime"], s.get("slot") or 0))
        for s in at60[:3]:
            t = _sol_trade(dfx.get_tx(s["signature"]), b["mint"])
            if t and t["price"] is not None:
                rec["price_60s"] = t["price"]
                break
        if rec["price_60s"] and rec["entry_s1"]:
            rec["ret_60s_from_s1"] = round(rec["price_60s"] / rec["entry_s1"] - 1, 4)
        if rec["price_60s"] and b["price"]:
            rec["ret_60s_vs_source"] = round(rec["price_60s"] / b["price"] - 1, 4)
        out.append(rec)
    return out


def crowd_summary(handle, uid, owner, rows):
    dumps = [r["dump"] for r in rows if r["dump"] is not None]
    return {"handle": handle, "source_user_id": uid, "owner": owner, "n": len(rows),
            "median_crowd_sol": _median(r["crowd_sol"] for r in rows),
            "median_crowd_n": _median(r["crowd_n"] for r in rows),
            "dump_rate": round(sum(dumps) / len(dumps), 3) if dumps else None,
            "median_ret_60s_from_s1": _median(r.get("ret_60s_from_s1") for r in rows),
            "median_ret_60s_vs_source": _median(r.get("ret_60s_vs_source") for r in rows),
            "n_ret": sum(1 for r in rows if r.get("ret_60s_from_s1") is not None)}


def armed_owners():
    from sniper.services.memecoin_address_registry import armed_verified_owners
    with contextlib.closing(get_readonly_connection(LEDGER)) as c:
        owners = armed_verified_owners(c) or {}
    return sorted(((h or uid, uid, addr) for addr, (uid, h) in owners.items()), key=lambda r: str(r[0]))


def selftest():
    # b58: "3DnvWnCg" style -> SetComputeUnitPrice(200000) is [3] + u64 LE; round-trip our encoder.
    raw = bytes([3]) + struct.pack("<Q", 200_000)
    n = int.from_bytes(raw, "big")
    enc = ""
    while n:
        n, r = divmod(n, 58)
        enc = _B58[r] + enc
    assert b58decode(enc) == raw
    tx = {"transaction": {"signatures": ["s"], "message": {
        "accountKeys": ["payer", COMPUTE_BUDGET, SYSTEM, next(iter(JITO_TIP_ACCOUNTS))],
        "instructions": [
            {"programIdIndex": 1, "accounts": [], "data": enc},
            {"programIdIndex": 2, "accounts": [0, 3],
             "data": _b58(struct.pack("<I", 2) + struct.pack("<Q", 1_000_000))}]}},
          "meta": {"fee": 85_000, "computeUnitsConsumed": 60_000, "innerInstructions": [],
                   "loadedAddresses": {}}}
    ff = fee_facts(tx)
    assert ff["cu_price"] == 200_000 and ff["tip_lamports"] == 1_000_000 and ff["signer"] == "payer"
    assert ff["priority_lamports"] == 80_000 and round(ff["priority_per_cu_consumed"]) == 1_333_333
    assert ff["sol_out"] == [[next(iter(JITO_TIP_ACCOUNTS)), 1_000_000]], ff["sol_out"]
    # A v1 transaction's bid is its config's total priority fee over its compute-unit limit.
    v1 = {"transaction": {"signatures": ["s"], "message": {
        "accountKeys": ["payer"], "instructions": [],
        "transactionConfig": {"priorityFee": 6_600_000, "computeUnitLimit": 68_200}}},
          "meta": {"fee": 6_605_000, "computeUnitsConsumed": 67_311, "loadedAddresses": {}}}
    ff1 = fee_facts(v1)
    assert (ff1["cu_price"], ff1["cu_limit"]) == (96_774_193, 68_200), ff1
    # A transfer INTO the signer is not something it paid.
    tx["transaction"]["message"]["instructions"][1]["accounts"] = [3, 0]
    assert fee_facts(tx)["sol_out"] == []
    # The flat guard holds a call while the book is open and releases it when the book goes flat.
    FLAT.clear()
    t = threading.Thread(target=hold_until_flat, args=(0.05,), daemon=True)
    t.start()
    time.sleep(0.3)
    assert t.is_alive(), "a call went out while the book was open"
    FLAT.set()
    t.join(1.0)
    assert not t.is_alive(), "the call stayed held after the book went flat"
    print("selftest ok")


def _b58(raw):
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("study", nargs="?", choices=("fees", "crowd", "report"))
    ap.add_argument("--since", default="2026-09-26T21:30")
    ap.add_argument("--ids", nargs="*", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--buys", type=int, default=20)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    if a.study == "report":
        print(json.dumps(report(json.loads(Path(a.out).read_text())), indent=1))
        return
    if a.study in ("fees", "crowd"):
        install_flat_guard()
    if a.study == "fees":
        out_path = Path(a.out) if a.out else None
        out = json.loads(out_path.read_text()) if out_path and out_path.exists() else []
        done = {f["position_id"] for f in out}          # resume: a restarted run skips finished fills
        for f in fills(a.since):
            if (a.ids and f["position_id"] not in a.ids) or f["position_id"] in done:
                continue
            try:
                res = fees_one(f)
            except fet.Stop as exc:
                print(f"STOP: {exc}", file=sys.stderr)
                break
            out.append(res)
            print(json.dumps({k: res.get(k) for k in (
                "position_id", "lane", "status", "source_basis", "slots_behind", "our_index",
                "ahead_n", "same_slot_buys_before_us_n", "same_slot_after_us_n",
                "highest_observed_cu_price_before_us", "tip_transfers_before_us_n")}), flush=True)
            if out_path:
                out_path.write_text(json.dumps(out, indent=1))
        Path(a.out or "/dev/stdout").write_text(json.dumps(out, indent=1))
        print(f"429s: {fet.RATE_429[0]}", file=sys.stderr)
    if a.study == "crowd":
        out_path = Path(a.out or "/dev/stdout")
        done = set()
        if out_path.exists() and a.out:
            done = {json.loads(l)["summary"]["owner"] for l in out_path.read_text().splitlines() if l.strip()}
        for handle, uid, owner in armed_owners():
            if owner in done:
                continue
            try:
                rows = crowd_wallet(owner, a.buys)
            except fet.Stop as exc:
                print(f"STOP: {exc}", file=sys.stderr)
                break
            summ = crowd_summary(handle, uid, owner, rows)
            with open(out_path, "a") as fh:
                fh.write(json.dumps({"summary": summ, "rows": rows}) + "\n")
            print(json.dumps(summ), flush=True)
        print(f"429s: {fet.RATE_429[0]}", file=sys.stderr)


if __name__ == "__main__":
    main()
