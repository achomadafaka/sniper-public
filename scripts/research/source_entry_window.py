#!/usr/bin/env python3
"""Tier W fetch for the source-entry setups study: for one Solana pump coin, the swaps in [tc-900 s, tc] plus the coin's first swap, read from the
bonding-curve PDA's transaction history (Helius getTransactionsForAddress, research key only, never the funded host's lanes). Swaps come from the
TradeEvent in the tx logs (same fields as the tape's swaps table). Block time is whole seconds; tc is cut at floor(tc).
    python3 scripts/research/source_entry_window.py MINT TC_ISO"""
import base58
import base64
import json
import os
import struct
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sniper.services.memecoin_desk_safety import curve_address  # noqa: E402
from sniper.services.memecoin_pump_tape import _TRADE, _SOL_QUOTES  # noqa: E402

PREFIX = "Program data: "
WINDOW_S, PAGE, MIN_GAP_S, MAX_PAGES = 900, 100, 0.34, 200
INIT_Q, INIT_B, INIT_TOL = 30e9, 1.073e15, 0.005     # tapefeat first_ok


def research_url():
    for line in open(os.environ.get("SNIPER_ENV", "/home/bigpapi/Desktop/Sniper/.env")):
        if line.startswith("MEMECOIN_RESEARCH_HELIUS_RPC_URL="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("MEMECOIN_RESEARCH_HELIUS_RPC_URL missing")


class Rpc:
    def __init__(self, url=None):
        self.url, self.calls, self.bytes, self._last = url or research_url(), 0, 0, 0.0

    def tx_page(self, address, opts):
        wait = MIN_GAP_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTransactionsForAddress",
                           "params": [address, dict(opts, transactionDetails="full", encoding="json", maxSupportedTransactionVersion=1)]}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        for attempt in range(4):
            try:
                raw = urllib.request.urlopen(req, timeout=60).read()
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        self._last = time.monotonic()
        self.calls += 1
        self.bytes += len(raw)
        out = json.loads(raw)
        if "error" in out:
            raise RuntimeError(str(out["error"])[:200])
        return out["result"]


def decode_event(raw):
    """SOL-quoted TradeEvent, any era. Layout as memecoin_pump_tape.decode_trade; the fee block (2025-05+) and the quote tail (later) are optional:
    an event without them keeps fee_bps/cfee_bps None and counts as SOL-quoted (non-SOL quotes did not exist before the tail)."""
    o = 8
    mint = raw[o:o + 32]; o += 32
    sol, tok, buy = struct.unpack_from("<QQ?", raw, o); o += 17
    user = raw[o:o + 32]; o += 32
    ts, vsol, vtok, rsol, rtok = struct.unpack_from("<qQQQQ", raw, o); o += 40
    fee_bps = cfee_bps = None
    quote_ok = True
    if len(raw) >= o + 32 + 16 + 32 + 16:
        o += 32
        fee_bps, _fee = struct.unpack_from("<QQ", raw, o); o += 16
        o += 32
        cfee_bps, _cfee = struct.unpack_from("<QQ", raw, o); o += 16
        try:
            o += 1 + 32
            n = struct.unpack_from("<I", raw, o)[0]; o += 4 + n
            o += 1 + 32
            sh = struct.unpack_from("<I", raw, o)[0]; o += 4 + sh * 34
            if len(raw) >= o + 32:
                quote_ok = raw[o:o + 32] in _SOL_QUOTES
        except struct.error:
            pass                                                   # mid-era layout without the tail: SOL only
    return {"mint": mint, "qa": sol, "ta": tok, "buy": bool(buy), "actor": user, "ts": ts, "vq": vsol, "vb": vtok, "rt": rtok,
            "fee_bps": fee_bps, "cfee_bps": cfee_bps, "quote_ok": quote_ok}


EVENT_IX = bytes.fromhex("e445a52e51cb9a1d")          # Anchor self-CPI event wrapper: inner-instruction data = wrapper + event bytes


def events(tx, mint32):
    """Trade events of one tx in instruction order. Inner-instruction event CPI first (logs are truncated on busy txs); txs that predate the CPI
    (before ~mid-2025) fall back to the 'Program data' log line."""
    if tx["meta"].get("err") is not None:
        return []
    raws = []
    for grp in tx["meta"].get("innerInstructions") or []:
        for ix in grp.get("instructions", []):
            try:
                data = base58.b58decode(ix["data"])
            except Exception:
                continue
            if data[:8] == EVENT_IX and data[8:16] == _TRADE:
                raws.append(data[8:])
    if not raws:
        for line in tx["meta"].get("logMessages") or []:
            if line.startswith(PREFIX + "vdt/"):
                try:
                    raw = base64.b64decode(line[len(PREFIX):])
                except Exception:
                    continue
                if raw[:8] == _TRADE:
                    raws.append(raw)
    out = []
    for raw in raws:
        try:
            ev = decode_event(raw)
        except Exception:
            continue
        if ev["mint"] == mint32:
            ev.update(slot=tx["slot"], idx=tx.get("transactionIndex", 0), bt=tx["blockTime"], sig=tx["transaction"]["signatures"][0])
            out.append(ev)
    return out


def fetch_coin(rpc, mint, tc, window_s=WINDOW_S):
    """Dict: first swap check, pre-tc swaps in [tc-window, tc] (oldest first), last pre-tc state; reason flags, never imputed."""
    mint32, pda, tc_i = base58.b58decode(mint), curve_address(mint), int(tc)   # PDA history = Pump trades only; empty for non-Pump mints (closed account)
    first_ev, tok = None, None
    for _ in range(3):                                            # first successful trade within the first 300 txs
        o = {"sortOrder": "asc", "limit": 100, "filters": {"blockTime": {"lte": tc_i}, "status": "succeeded"}}
        if tok:
            o["paginationToken"] = tok
        page = rpc.tx_page(pda, o)
        first_ev = next((e for tx in page.get("data", []) for e in events(tx, mint32)), None)
        tok = page.get("paginationToken")
        if first_ev or not tok:
            break
    res = {"mint": mint, "tc": tc, "reason": None, "first_ok": None, "t0": None, "swaps": [], "last_rt": None}
    if first_ev is None:
        res["reason"] = "no_pump_trades"
        return res
    pre_q = first_ev["vq"] - first_ev["qa"] if first_ev["buy"] else first_ev["vq"] + first_ev["qa"]
    pre_b = first_ev["vb"] + first_ev["ta"] if first_ev["buy"] else first_ev["vb"] - first_ev["ta"]
    res["first_ok"] = abs(pre_q / INIT_Q - 1) <= INIT_TOL and abs(pre_b / INIT_B - 1) <= INIT_TOL and first_ev["quote_ok"]
    res["t0"] = first_ev["bt"]
    res["init"] = [pre_q, pre_b, first_ev["rt"] + first_ev["ta"] if first_ev["buy"] else first_ev["rt"] - first_ev["ta"], first_ev["quote_ok"], first_ev["buy"]]   # virtual_quote, virtual_token, real_token before the swap
    if not res["first_ok"]:
        res["reason"] = "nonstandard_start"                       # excluded either way: do not pay for the window
        return res
    got, tok, pages = [], None, 0
    while True:
        opts = {"sortOrder": "desc", "limit": PAGE, "filters": {"blockTime": {"lte": tc_i}, "status": "succeeded"}}
        if tok:
            opts["paginationToken"] = tok
        page = rpc.tx_page(pda, opts)
        data = page.get("data", [])
        for tx in data:
            got.extend(events(tx, mint32))
        tok = page.get("paginationToken")
        pages += 1
        newest = max(got, key=lambda e: (e["slot"], e["idx"]), default=None)
        if newest is not None and newest["rt"] <= 0:               # curve already complete at the newest pre-tc trade: PumpSwap era, out of scope
            break
        if not data or not tok:
            break
        if data[-1]["blockTime"] < tc_i - window_s and any(e["bt"] < tc_i - window_s for e in got):
            break                                                  # window covered AND an anchor swap found before it
        if pages >= MAX_PAGES:
            res["capped"] = True                                   # hot mint: window truncated, row flagged, never silently complete
            break
    got.sort(key=lambda e: (e["slot"], e["idx"]))   # ponytail: log order inside one tx is not kept; ties by (slot, idx) only
    last = got[-1] if got else None
    res["last_state"] = last and {k: last[k] for k in ("bt", "vq", "vb", "rt")}   # newest event <= tc, in or out of the window
    old = [e for e in got if e["bt"] < tc_i - window_s]
    anchor = old[-1:]                                              # last swap before the window: the price the window opens from
    got = anchor + [e for e in got if e["bt"] >= tc_i - window_s]                  
    res["swaps"] = [{k: (v.hex() if isinstance(v, bytes) else v) for k, v in e.items() if k != "mint"} for e in got]
    res["last_rt"] = last and last["rt"]
    if last and last["rt"] <= 0:
        res["reason"] = "graduated_before_tc"
    elif not res["first_ok"]:
        res["reason"] = "nonstandard_start"
    elif not got:
        res["reason"] = "no_swaps_in_window"          # a valid row (silent coin), not an exclusion
    return res


if __name__ == "__main__":
    r = Rpc()
    tc = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00")).timestamp()
    out = fetch_coin(r, sys.argv[1], tc)
    print(json.dumps({k: v for k, v in out.items() if k != "swaps"}), "swaps", len(out["swaps"]), "calls", r.calls, "MB", round(r.bytes / 1e6, 2))
