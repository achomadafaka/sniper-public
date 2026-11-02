#!/usr/bin/env python3
"""Where should the funded Solana per-entry clip be capped? A read-only counterfactual study.

Operator question 2026-09-27 (via Alpha): MEMECOIN_SOLANA_MAX_CLIP_USD=175 was picked on a whim;
find the ceiling that maximises expected $ PnL, flat or tied to the coin at entry. Design approved
by Alpha 11:25Z with five amendments; the audit is docs/audits/2026-09-27-solana-clip-ceiling.md.

Stages (each resumable, cached under output/research/solana-clip-ceiling-20260927/):
    fetch   ledger rows + getTransaction for every entry/exit fill signature (research key only,
            <=1 request/s, a quota 429 stops, a rate 429 backs off 6/12/24 s then stops)
    pools   getMultipleAccounts for every PumpSwap pool seen, for the Pool's virtual quote V
    model   decode venues, run the negative controls, price every row at clip X, score the rules
    --selftest  the math checks, runnable offline

Ledger reads only through get_readonly_connection under contextlib.closing. Never the funded
custody RPC, never GeckoTerminal, never DexScreener.
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import contextlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))

OUT = REPO / "output" / "research" / "solana-clip-ceiling-20260927"
TX_DIR = OUT / "tx"
WINDOW_START = "2026-09-01T00:00:00"
HOLDOUT_START = "2026-09-20T00:00:00"
SPACING_S = 1.05
EXIT_KINDS = ("bracket_exit", "manual_close", "mirror_exit", "derisk")

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
RAYDIUM_CP = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
TRADE_DISC = bytes([189, 219, 127, 211, 78, 230, 97, 238])
AMM_BUY_DISC = bytes([103, 244, 82, 31, 44, 245, 119, 119])
AMM_SELL_DISC = bytes([62, 47, 55, 10, 165, 3, 220, 42])


class Stop(Exception):
    pass


# ---------------------------------------------------------------------------------------- rpc --

_URL = None
COUNTS = {"calls": 0, "rate_429": 0}


def _url():
    global _URL
    if _URL is None:
        import slot_lane_send_monitor as mon
        from sniper.execution.solana_rpc_pool import key_id
        _URL = mon.rpc_url()
        print("research key", key_id(_URL), file=sys.stderr, flush=True)
    return _URL


def rpc(method, params):
    """One JSON-RPC call on the research key. Returns (result, error). A quota 429 (body 'max
    usage') or any non-429 HTTP error raises Stop; a rate 429 waits 6/12/24 s, then Stop. A
    JSON-RPC error is RETURNED, never read as a null result."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(_url(), body, {"Content-Type": "application/json",
                                               "User-Agent": "curl/8.5.0"})
    for attempt in range(4):
        try:
            COUNTS["calls"] += 1
            out = json.loads(urllib.request.urlopen(req, timeout=30).read())
            break
        except urllib.error.HTTPError as e:
            text = e.read()[:200]
            if e.code != 429 or b"max usage" in text or attempt == 3:
                raise Stop(f"HTTP {e.code} on {method}: {text!r}")
            COUNTS["rate_429"] += 1
            print(f"429 on {method} body={text!r}, waiting {6 * 2 ** attempt} s",
                  file=sys.stderr, flush=True)
            time.sleep(6 * 2 ** attempt)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == 3:
                return None, {"transport": repr(e)[:160]}
            time.sleep(2.0)
    time.sleep(SPACING_S)
    return out.get("result"), out.get("error")


# ------------------------------------------------------------------------------------- ledger --

def load_rows():
    """Non-manual funded Solana closes in the window, with fills, policy and entry-trace facts."""
    import sqlite3
    from sniper.persistence.db_core import get_readonly_connection
    with contextlib.closing(get_readonly_connection()) as c:
        c.row_factory = sqlite3.Row
        pos = [dict(r) for r in c.execute(
            "SELECT id, mint, ticker, source_handle, signal_source, entry_notional, entry_price,"
            " realized_pnl, exit_reason, opened_at, opened_recorded_at, closed_at, their_size_usd,"
            " funded_contract_json, attempt_id FROM memecoin_positions"
            " WHERE execution_mode='real' AND is_open=0 AND network_id='solana'"
            " AND signal_source != 'manual' AND closed_at >= ? ORDER BY id", (WINDOW_START,))]
        ids = [p["id"] for p in pos]
        q = ",".join("?" * len(ids))
        fills = {}
        for r in c.execute(f"SELECT position_id, kind, qty, notional, price, fraction, tx_sig,"
                           f" created_at FROM memecoin_fills WHERE position_id IN ({q}) ORDER BY id",
                           ids):
            fills.setdefault(r["position_id"], []).append(dict(r))
        traces = {}
        for r in c.execute(f"SELECT position_id, trace_json, classification_json"
                           f" FROM memecoin_entry_attempts WHERE position_id IN ({q})", ids):
            traces[r["position_id"]] = (r["trace_json"], r["classification_json"])
        series = {}
        for r in c.execute(f"SELECT position_id, id, observed_at, expected_usdc, open_qty,"
                           f" quote_evidence_json FROM memecoin_route_series WHERE seam='exit_decision'"
                           f" AND position_id IN ({q}) ORDER BY id", ids):
            series.setdefault(r["position_id"], []).append(dict(r))
    for p in pos:
        p["fills"] = fills.get(p["id"], [])
        trace, cls = traces.get(p["id"], (None, None))
        p.update(_trace_facts(trace), **_contract_facts(p.pop("funded_contract_json"), cls))
        p["series"] = series.get(p["id"], [])
    return pos


def _trace_facts(trace_json):
    """The entry quote's Jupiter impact and the round-trip gate's cost/cap, from the LAST quote
    round of the attempt (a retry rebuilds and re-quotes; the last round is the one that filled)."""
    out = {"jupiter_impact": None, "roundtrip_cost_pct": None, "roundtrip_cap_pct": None}
    try:
        events = json.loads(trace_json or "{}").get("substage_events") or []
    except ValueError:
        return out
    for e in events:
        if e.get("name") == "quote_fallback" and e.get("jupiter_impact_pct") is not None:
            out["jupiter_impact"] = float(e["jupiter_impact_pct"])
        if e.get("name") == "roundtrip_cap" and e.get("cost_pct") is not None:
            out["roundtrip_cost_pct"] = float(e["cost_pct"])
            out["roundtrip_cap_pct"] = float(e.get("cap_pct") or 0) or None
    return out


def _contract_facts(contract_json, classification_json):
    out = {"risk_scale": 1.0, "base_clip": None, "policy": {}, "volume_m5": None}
    try:
        c = json.loads(contract_json or "{}")
    except ValueError:
        c = {}
    sizing = c.get("sizing") or {}
    out["risk_scale"] = float(sizing.get("risk_scale") or 1.0)
    out["base_clip"] = sizing.get("base_clip_usd")
    out["policy"] = c.get("policy") or {}
    facts = ((c.get("classification") or {}).get("facts") or {})
    if not facts:
        try:
            facts = (json.loads(classification_json or "{}").get("facts") or {})
        except ValueError:
            facts = {}
    out["volume_m5"] = facts.get("volume_m5")
    return out


# -------------------------------------------------------------------------------------- fetch --

def tx_path(sig):
    return TX_DIR / f"{sig}.json"


def fetch(rows):
    TX_DIR.mkdir(parents=True, exist_ok=True)
    sigs = []
    for p in rows:
        for f in p["fills"]:
            if f["tx_sig"] and (f["kind"] == "entry" or f["kind"] in EXIT_KINDS):
                sigs.append(f["tx_sig"])
    todo = [s for s in dict.fromkeys(sigs) if not tx_path(s).exists()]
    print(f"fetch: {len(sigs)} sigs, {len(todo)} to read", file=sys.stderr, flush=True)
    for i, sig in enumerate(todo, 1):
        result, error = rpc("getTransaction", [sig, {
            "encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 1}])
        if error is not None or result is None:
            # unknown, not absent: kept out of the cache so a rerun retries it
            print(f"  {sig[:12]} result={'null' if result is None else 'ok'} error={error}",
                  file=sys.stderr, flush=True)
            continue
        tx_path(sig).write_text(json.dumps(result))
        if i % 50 == 0:
            print(f"  {i}/{len(todo)} calls={COUNTS['calls']} 429={COUNTS['rate_429']}",
                  file=sys.stderr, flush=True)


def load_tx(sig):
    p = tx_path(sig) if sig else None
    return json.loads(p.read_text()) if p and p.exists() else None



# ------------------------------------------------------------------------------------- decode --

CP_AUTHORITY = "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL"
CP_SWAP_DISCS = (bytes([143, 190, 90, 218, 196, 30, 51, 222]),   # swap_base_input
                 bytes([55, 217, 98, 86, 163, 74, 180, 173]))    # swap_base_output


def _keys(tx):
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return (list(tx["transaction"]["message"]["accountKeys"])
            + list(loaded.get("writable") or []) + list(loaded.get("readonly") or []))


def _balances(tx):
    """accountIndex -> {mint, owner, pre, post} over the token balances (a missing side is 0)."""
    out = {}
    for side in ("pre", "post"):
        for b in tx["meta"].get(f"{side}TokenBalances") or []:
            row = out.setdefault(b["accountIndex"], {"mint": b["mint"], "owner": b.get("owner"),
                                                      "pre": 0, "post": 0})
            row[side] = int(b["uiTokenAmount"]["amount"])
    return out


def _curve_events(tx, mint, user):
    """Our pump.fun TradeEvents on `mint`: minimal field read (older event versions carry fewer
    trailing fields, and nothing past the reserves is needed)."""
    from sniper.research.memecoin_opportunity.decoders import BorshReader, anchor_event_bytes
    from sniper.services.memecoin_opportunity_live import program_data_logs
    out = []
    for _i, line in program_data_logs(tx["meta"].get("logMessages") or [], PUMP):
        raw = anchor_event_bytes(line)
        if raw[:8] != TRADE_DISC:
            continue
        r = BorshReader(raw[8:])
        e = {"mint": r.pubkey(), "sol_amount": r.u64(), "token_amount": r.u64(),
             "is_buy": r.bool(), "user": r.pubkey(), "timestamp": r.i64(),
             "v_sol": r.u64(), "v_tok": r.u64(), "real_sol": r.u64(), "real_tok": r.u64()}
        r.pubkey()
        e["fee_bps"] = r.u64()
        r.u64()
        r.pubkey()
        e["fee_bps"] += r.u64()
        if e["mint"] == mint and e["user"] == user:
            out.append(e)
    return out


_U64 = ("base", "limit", "user_base", "user_quote", "pool_base", "pool_quote", "quote",
        "lp_bps", "lp_fee", "proto_bps", "proto_fee", "quote_x", "user_quote_amount")


def _amm_events(tx, user):
    """Our PumpSwap Buy/Sell events: (side, fields). Both carry PRE-trade vault reserves; `quote`
    is quote_amount_in (buy, before lp fee) or quote_amount_out (sell, before fees)."""
    from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes, base58_encode
    from sniper.services.memecoin_opportunity_live import program_data_logs
    out = []
    for _i, line in program_data_logs(tx["meta"].get("logMessages") or [], PUMP_AMM):
        raw = anchor_event_bytes(line)
        side = {AMM_BUY_DISC: "buy", AMM_SELL_DISC: "sell"}.get(raw[:8])
        if side is None or len(raw) < 360 or base58_encode(raw[152:184]) != user:
            continue
        e = {k: int.from_bytes(raw[16 + 8 * i:24 + 8 * i], "little") for i, k in enumerate(_U64)}
        e["pool"] = base58_encode(raw[120:152])
        e["creator_bps"] = int.from_bytes(raw[344:352], "little")
        e["side"] = side
        out.append(e)
    return out


def _cp_swaps(tx):
    """Raydium CP swap instructions: (input_vault, output_vault, input_mint, output_mint)."""
    import base58
    keys = _keys(tx)
    ixs = list(tx["transaction"]["message"].get("instructions") or [])
    for inner in tx["meta"].get("innerInstructions") or []:
        ixs.extend(inner.get("instructions") or [])
    out = []
    for ix in ixs:
        if keys[ix["programIdIndex"]] != RAYDIUM_CP:
            continue
        data = base58.b58decode(ix["data"])
        if data[:8] not in CP_SWAP_DISCS or len(ix["accounts"]) < 12:
            continue
        a = [keys[i] for i in ix["accounts"]]
        out.append((a[6], a[7], a[10], a[11]))
    return out


def decode_leg(tx, mint, side):
    """The coin legs of OUR trade in `tx`: {"venue", "legs": [...], "censor": reason|None}.

    Each leg: pool id, quote reserve x and coin reserve y BEFORE our trade (x includes PumpSwap's
    virtual quote only once V is known), our quote amount into (buy) or out of (sell) the pool
    before fees, and our coin amount. Every coin vault our trade moved must belong to a
    recognised leg; anything else (CLMM, DLMM, Whirlpool, DBC, LaunchLab, DAMM...) censors."""
    if tx is None:
        return {"censor": "tx_unread"}
    if (tx.get("meta") or {}).get("err") is not None:
        return {"censor": "tx_failed"}
    user = _keys(tx)[0]
    bal = _balances(tx)
    keys = _keys(tx)
    moved = {keys[i]: b for i, b in bal.items()
             if b["mint"] == mint and b["owner"] != user and b["pre"] != b["post"]}
    legs, claimed = [], set()
    for e in _curve_events(tx, mint, user):
        if e["is_buy"] != (side == "buy"):
            return {"censor": "curve_side_mismatch"}
        k = e["v_sol"] * e["v_tok"]
        if k <= 0:
            return {"censor": "curve_zero_sol_reserves"}   # a non-SOL-quote curve reads 0 here
        y = e["v_tok"] + e["token_amount"] if side == "buy" else e["v_tok"] - e["token_amount"]
        x = k / y
        legs.append({"venue": "pump_curve", "pool": "curve:" + mint, "x": x, "y": y, "k": k,
                     "qmint": WSOL,
                     "quote": abs(e["v_sol"] - x), "coin": e["token_amount"],
                     "fee_bps": e["fee_bps"], "form_err": abs(x - (e["v_sol"] - e["sol_amount"]
                     if side == "buy" else e["v_sol"] + e["sol_amount"])) / x})
        claimed |= {a for a, b in moved.items() if b["owner"] not in (user,)
                    and abs(b["post"] - b["pre"]) == e["token_amount"]}
    for e in _amm_events(tx, user):
        if e["side"] != side:
            return {"censor": "pumpswap_reversed_or_mismatch"}
        pool_vaults = {a for a, b in moved.items() if b["owner"] == e["pool"]}
        if not pool_vaults:
            return {"censor": "pumpswap_pool_not_coin"}
        claimed |= pool_vaults
        fee = e["lp_bps"] + e["proto_bps"] + e["creator_bps"]
        # A buy is priced on user_quote_amount_in - 1 (the program's "net - 1"): matched to the
        # unit against V read now on live fills; quote_amount_in carries the fees on top.
        priced = e["user_quote_amount"] - 1 if side == "buy" else e["quote"]
        legs.append({"venue": "pumpswap", "pool": e["pool"], "x_vault": e["pool_quote"], "qmint": None,
                     "y": e["pool_base"], "quote": priced, "coin": e["base"],
                     "fee_bps": fee, "lp_bps": e["lp_bps"]})
    for in_v, out_v, in_mint, out_mint in _cp_swaps(tx):
        coin_v, quote_v = (out_v, in_v) if out_mint == mint else (in_v, out_v)
        if mint not in (in_mint, out_mint):
            continue                       # a quote-to-quote hop (SOL/USDC): deep, not modelled
        cb = next((b for i, b in bal.items() if keys[i] == coin_v), None)
        qb = next((b for i, b in bal.items() if keys[i] == quote_v), None)
        if cb is None or qb is None:
            return {"censor": "cp_vault_balance_missing"}
        claimed.add(coin_v)
        legs.append({"venue": "raydium_cp", "pool": coin_v, "x": qb["pre"], "y": cb["pre"],
                     "qmint": qb["mint"],
                     "quote": abs(qb["post"] - qb["pre"]), "coin": abs(cb["post"] - cb["pre"]),
                     "fee_bps": None})
    unclaimed = set(moved) - claimed
    if unclaimed:
        return {"censor": "coin_leg_other_venue", "legs": legs}
    if not legs:
        return {"censor": "no_coin_leg"}
    return {"venue": "+".join(sorted({l["venue"] for l in legs})), "legs": legs, "censor": None}


# --------------------------------------------------------------------------------------- math --

def buy_out(q, x, y):
    """Coin out of a constant-product pool for net quote q (fees are proportional and cancel in
    every ratio this study takes)."""
    return y * q / (x + q)


def sell_out(t, x, y):
    return x * t / (y + t)


CP_FEE = 0.0025   # the standard Raydium CP tier; a 1% tier error moves the fitted x ~1%


def _eff_x(leg, v_by_pool, side=None):
    """Effective quote reserve. PumpSwap: vault + V (V read now; verified stable, see controls).
    Raydium CP: the vault balance OVERSTATES the reserve by the fees accrued in it (control a:
    predicted fills off by 1.3% median, 15-33% on exotic-quote pools), so x is FITTED to our own
    fill at CP_FEE with the coin vault taken as exact; `accrued` = vault - fitted x."""
    if leg["venue"] == "pumpswap":
        v = v_by_pool.get(leg["pool"])
        return None if v is None else leg["x_vault"] + v
    if leg["venue"] == "raydium_cp" and side and leg["coin"]:
        if side == "buy":
            qn = leg["quote"] * (1 - CP_FEE)
            fit = leg["y"] * qn / leg["coin"] - qn
        else:
            fit = leg["quote"] / (1 - CP_FEE) * (leg["y"] + leg["coin"]) / leg["coin"]
        leg["accrued"] = leg["x"] - fit
        return fit if fit > 0 else None
    return leg["x"]



# -------------------------------------------------------------------------------------- pools --

def pools_path():
    return OUT / "pumpswap_pools.json"


def fetch_pools(rows):
    """Virtual quote V of every PumpSwap pool the decoded legs touch, read NOW (getMultipleAccounts,
    <=100 per call). Amendment 5: V is trusted only where it matches the V our own fill implies."""
    from sniper.execution.pumpswap_buy import parse_pool_account
    pools = set()
    for p in rows:
        for f in p["fills"]:
            d = decode_leg(load_tx(f["tx_sig"]), p["mint"], "buy" if f["kind"] == "entry" else "sell")
            pools |= {l["pool"] for l in d.get("legs") or [] if l["venue"] == "pumpswap"}
    known = json.loads(pools_path().read_text()) if pools_path().exists() else {}
    todo = sorted(pools - set(known))
    print(f"pools: {len(pools)} seen, {len(todo)} to read", file=sys.stderr, flush=True)
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        result, error = rpc("getMultipleAccounts", [chunk, {"encoding": "base64",
                                                            "commitment": "confirmed"}])
        if error is not None or result is None:
            print(f"  pool read failed: {error}", file=sys.stderr, flush=True)
            continue
        for pool, acct in zip(chunk, result["value"]):
            if acct is None:
                known[pool] = None                 # closed or absent now: V unknown, not 0
                continue
            try:
                f = parse_pool_account(acct["data"][0])
                known[pool] = {"virtual_quote": f["virtual_quote"], "quote_mint": f["quote_mint"],
                               "base_mint": f["base_mint"], "context_slot": result["context"]["slot"]}
            except ValueError as exc:
                known[pool] = {"error": str(exc)}
    pools_path().write_text(json.dumps(known, indent=1, sort_keys=True))


# -------------------------------------------------------------------------------------- model --

def _v_by_pool():
    raw = json.loads(pools_path().read_text()) if pools_path().exists() else {}
    out = {k: v["virtual_quote"] for k, v in raw.items() if v and "virtual_quote" in v}
    out.update({"quote_mint:" + k: v["quote_mint"] for k, v in raw.items()
                if v and "quote_mint" in v})
    return out


def implied_v(leg, side):
    """The virtual quote our OWN fill implies: buy y*q/coin - x - q; sell quote*(y+coin)/coin - x."""
    if not leg["coin"]:
        return None
    if side == "buy":
        return leg["y"] * leg["quote"] / leg["coin"] - leg["x_vault"] - leg["quote"]
    return leg["quote"] * (leg["y"] + leg["coin"]) / leg["coin"] - leg["x_vault"]


def prepare(p, v_by_pool):
    """One row ready for the counterfactual, or {'censor': reason}. Fills the model's own check
    of OUR fills (control a) as it goes."""
    entry = [f for f in p["fills"] if f["kind"] == "entry"]
    exits = [f for f in p["fills"] if f["kind"] in EXIT_KINDS]
    if len(entry) != 1 or not exits:
        return {"censor": "fills_shape"}
    e = decode_leg(load_tx(entry[0]["tx_sig"]), p["mint"], "buy")
    if e["censor"]:
        return {"censor": "entry:" + e["censor"]}
    xs = []
    for f in exits:
        d = decode_leg(load_tx(f["tx_sig"]), p["mint"], "sell")
        if d["censor"]:
            return {"censor": "exit:" + d["censor"]}
        xs.append((float(f["notional"] or 0.0), d["legs"]))
    for leg in e["legs"] + [l for _n, legs in xs for l in legs]:
        leg["xe"] = _eff_x(leg, v_by_pool, "buy" if leg in e["legs"] else "sell")
        if leg["xe"] is None:
            return {"censor": leg["venue"] + "_state_unknown"}
        if leg["venue"] == "pumpswap":
            leg["qmint"] = (v_by_pool.get("quote_mint:" + leg["pool"]) or WSOL)
            leg["v_now"] = v_by_pool[leg["pool"]]
            leg["v_implied"] = implied_v(leg, "buy" if leg in e["legs"] else "sell")
    if len({l["qmint"] for l in e["legs"]}) != 1:
        return {"censor": "entry_mixed_quote_mints"}
    return {"censor": None, "entry": e["legs"], "exits": xs}


def control_rows(prepped, v_zero=False):
    """Control (a): predict OUR fills from the pre-trade state at our actual size. Returns
    [(venue, side, relative error)]. PumpSwap state is independent of our amounts (event reserves
    + V read now); the curve's is a form check (k conserved); Raydium CP fits no fee, so its
    error INCLUDES the pool fee (0.25-4%) and is reported as the fee it implies."""
    out = []
    for r in prepped:
        for side, legs in [("buy", r["entry"])] + [("sell", l) for _n, l in r["exits"]]:
            for l in legs:
                x = l["x_vault"] if (v_zero and l["venue"] == "pumpswap") else l["xe"]
                if side == "buy":
                    pred = buy_out(l["quote"], x, l["y"])
                    err = pred / l["coin"] - 1 if l["coin"] else None
                else:
                    pred = sell_out(l["coin"], x, l["y"])
                    err = pred / l["quote"] - 1 if l["quote"] else None
                if err is not None:
                    out.append((l["venue"], side, err))
    return out


def pnl_at(r, clip, scenario):
    """Counterfactual $ PnL of one prepared row at entry clip `clip` (USD). Entry: every leg keeps
    its actual share of the quote, scaled by clip/clip_a. Exit: each fill sells the same fraction
    of the (new) bag through its legs. Scenario 'T' prices the exit on the observed pre-exit
    reserves; 'P' first shifts a leg's reserves along its own curve by the extra quote our bigger
    buy put into THAT pool (same pool id only). The result is the ACTUAL proceeds times the
    model's proceeds ratio, less the clip."""
    s = clip / r["clip_a"]
    t_a = sum(l["coin"] for l in r["entry"])
    t_x = sum(buy_out(s * l["quote"], l["xe"], l["y"]) for l in r["entry"])
    shift = {l["pool"]: (s - 1.0) * l["quote"] for l in r["entry"]}
    total = 0.0
    for notional, legs in r["exits"]:
        num = den = 0.0
        for l in legs:
            x, y = l["xe"], l["y"]
            den += sell_out(l["coin"], x, y)
            if scenario == "P" and l["pool"] in shift:
                k = x * y
                x = max(1e-9, x + shift[l["pool"]])
                y = k / x
            num += sell_out(l["coin"] * t_x / t_a, x, y)
        total += notional * (num / den if den > 0 else 0.0)
    return total - clip


def admitted(r, clip):
    """The live round-trip gate at the new clip (cost grows ~2 x the added entry impact on a
    constant-product pool). Unknown cost or cap: admitted, and counted."""
    cost, cap = r.get("roundtrip_cost_pct"), r.get("roundtrip_cap_pct")
    if cost is None or not cap:
        return True
    extra = 2.0 * 100.0 * (clip / r["clip_a"] - 1.0) * r["q_a"] / r["x_entry"]
    return cost + max(0.0, extra) <= cap


FLAT = (75, 100, 125, 150, 175, 200, 250, 300, 400, 500, math.inf)
IMPACT = (0.01, 0.02, 0.03, 0.05, 0.08)
SOURCE_K = (0.25, 0.5, 1.0, 2.0)
M5_K = (0.005, 0.01, 0.02, 0.05)
HYBRID = [(c, i) for c in (175, 250, 400) for i in (0.02, 0.03, 0.05)]
EQUITY = (356, 525, 750, 1000, 1500, 2500, 5000)


def rules():
    """name -> fn(row, Q) -> clip. Flat caps sit on the BASE clip, then risk_scale (the live
    order, memecoin_risk.py ~287). The coin-linked rules are new code; they are assumed to cap
    the FINAL clip, after risk_scale, since they bound market capacity, not risk."""
    out = {}
    for c in FLAT:
        out[f"flat_{'none' if c == math.inf else int(c)}"] = (
            lambda r, q, c=c: min(q / 3.0, c) * r["risk_scale"])
    for i in IMPACT:
        out[f"impact_{i:g}"] = lambda r, q, i=i: min(q / 3.0 * r["risk_scale"], i * r["x_entry_usd"])
    for k in SOURCE_K:
        out[f"source_x{k:g}"] = lambda r, q, k=k: (
            min(q / 3.0 * r["risk_scale"], k * r["their_size_usd"])
            if r.get("their_size_usd") else q / 3.0 * r["risk_scale"])
    for k in M5_K:
        out[f"m5_{k:g}"] = lambda r, q, k=k: (
            min(q / 3.0 * r["risk_scale"], k * float(r["volume_m5"]))
            if r.get("volume_m5") else q / 3.0 * r["risk_scale"])
    for c, i in HYBRID:
        out[f"flat_{c}+impact_{i:g}"] = lambda r, q, c=c, i=i: min(
            min(q / 3.0, c) * r["risk_scale"], i * r["x_entry_usd"])
    return out


def row_pnl(r, clip, scenario, floor):
    if clip < floor or not admitted(r, clip):
        return 0.0
    return pnl_at(r, clip, scenario)



# ------------------------------------------------------------------------------------ scoring --

B = 10_000
SEED = 20260927


def _q(xs, qs=(0.1, 0.5, 0.9)):
    import numpy as np
    xs = np.asarray([x for x in xs if x is not None and math.isfinite(x)], dtype=float)
    return [round(float(np.quantile(xs, q)), 4) for q in qs] if len(xs) else None


def cvar5(values, weights=None):
    """Mean of the worst 5% of per-row $ PnL (weighted mass; the boundary row counts in part)."""
    import numpy as np
    v = np.asarray(values, dtype=float)
    w = np.ones_like(v) if weights is None else np.asarray(weights, dtype=float)
    order = np.argsort(v)
    v, w = v[order], w[order]
    target = 0.05 * w.sum()
    used = np.clip(target - (np.cumsum(w) - w), 0.0, w)
    return float((used * v).sum() / target)


def bootstrap(matrix, clusters, base_col):
    """Paired cluster bootstrap over MINTS. matrix: rows x rules of $ PnL. Returns, per rule,
    95% CIs of (total - base total) and (CVaR5 - base CVaR5)."""
    import numpy as np
    rng = np.random.default_rng(SEED)
    ids = sorted(set(clusters))
    index = {c: i for i, c in enumerate(ids)}
    member = np.array([index[c] for c in clusters])
    draws = rng.integers(0, len(ids), size=(B, len(ids)))
    counts = np.zeros((B, len(ids)))
    for j in range(len(ids)):
        counts[:, j] = (draws == j).sum(axis=1)
    W = counts[:, member]                                       # B x rows
    totals = W @ matrix                                         # B x rules
    cv = np.empty_like(totals)
    for k in range(matrix.shape[1]):
        order = np.argsort(matrix[:, k])
        v, Ws = matrix[order, k], W[:, order]
        target = 0.05 * Ws.sum(axis=1, keepdims=True)
        used = np.clip(target - (np.cumsum(Ws, axis=1) - Ws), 0.0, Ws)
        cv[:, k] = (used * v).sum(axis=1) / target[:, 0]
    dt = totals - totals[:, [base_col]]
    dc = cv - cv[:, [base_col]]
    lo, hi = 2.5, 97.5
    return ([(float(np.percentile(dt[:, k], lo)), float(np.percentile(dt[:, k], hi)))
             for k in range(matrix.shape[1])],
            [(float(np.percentile(dc[:, k], lo)), float(np.percentile(dc[:, k], hi)))
             for k in range(matrix.shape[1])])


def evaluate(rows, names, fns, q, scenario, floor, boot=True):
    import numpy as np
    m = np.array([[row_pnl(r, fns[n](r, q), scenario, floor) for n in names] for r in rows])
    clips = np.array([[fns[n](r, q) for n in names] for r in rows])
    traded = np.array([[fns[n](r, q) >= floor and admitted(r, fns[n](r, q)) for n in names]
                       for r in rows])
    base = names.index("flat_175")
    out = {"total": m.sum(0).tolist(), "n": traded.sum(0).tolist(),
           "deployed": (clips * traded).sum(0).tolist(),
           "cvar5": [cvar5(m[:, k]) for k in range(len(names))],
           "worst_day": [], "ci_total": None, "ci_cvar": None}
    days = sorted({r["closed_at"][:10] for r in rows})
    for k in range(len(names)):
        by = {d: 0.0 for d in days}
        for r, v in zip(rows, m[:, k]):
            by[r["closed_at"][:10]] += v
        out["worst_day"].append(min(by.values()))
    if boot:
        out["ci_total"], out["ci_cvar"] = bootstrap(m, [r["mint"] for r in rows], base)
    return out


def select(train_P, train_T, names):
    """Amendment 1, fixed before results: maximise total $ in BOTH scenarios among rules whose
    CVaR5 is not worse than flat_175 (upper CI of the CVaR5 difference >= 0). Different winners
    in P and T: recommend neither. A winner whose total difference CI includes 0: keep 175."""
    picks = {}
    for tag, ev in (("P", train_P), ("T", train_T)):
        ok = [k for k in range(len(names)) if ev["ci_cvar"][k][1] >= 0]
        picks[tag] = max(ok, key=lambda k: ev["total"][k]) if ok else None
    if picks["P"] != picks["T"] or picks["P"] is None:
        return {"verdict": "scenarios_disagree", "P": names[picks["P"]] if picks["P"] is not None
                else None, "T": names[picks["T"]] if picks["T"] is not None else None}
    k = picks["P"]
    within = all(ev["ci_total"][k][0] <= 0 <= ev["ci_total"][k][1] for ev in (train_P, train_T))
    return {"verdict": "keep_175" if (within or names[k] == "flat_175") else "adopt",
            "rule": names[k]}



# --------------------------------------------------------------------------------------- run ---

def run_model(rows):
    import numpy as np
    from sniper.settings import memecoin as cfg
    floor = float(cfg.MEMECOIN_MIN_TRADE_FLOOR_USD)
    v = _v_by_pool()
    prepped, censored, v_flag = [], [], []
    for p in rows:
        d = prepare(p, v)
        if d["censor"]:
            censored.append({"id": p["id"], "reason": d["censor"], "clip": p["entry_notional"],
                             "pnl": p["realized_pnl"], "closed_at": p["closed_at"],
                             "exit_reason": p["exit_reason"]})
            continue
        bad_v = [l for l in d["entry"] + [l for _n, ls in d["exits"] for l in ls]
                 if l["venue"] == "pumpswap" and l["v_implied"] is not None
                 and abs(l["v_implied"] - l["v_now"]) > 0.01 * max(l["v_now"], 1.0)]
        q_a = sum(l["quote"] for l in d["entry"])
        x_e = sum(l["xe"] for l in d["entry"])
        row = {k: p[k] for k in ("id", "mint", "closed_at", "risk_scale", "their_size_usd",
                                 "volume_m5", "roundtrip_cost_pct", "roundtrip_cap_pct",
                                 "jupiter_impact", "realized_pnl", "exit_reason", "base_clip")}
        row.update(entry=d["entry"], exits=d["exits"], clip_a=float(p["entry_notional"]),
                   q_a=q_a, x_entry=x_e, x_entry_usd=x_e * float(p["entry_notional"]) / q_a,
                   venue="+".join(sorted({l["venue"] for l in d["entry"]})))
        if bad_v:
            v_flag.append({"id": p["id"], "pools": sorted({l["pool"] for l in bad_v}),
                           "rel": [round((l["v_implied"] - l["v_now"]) / max(l["v_now"], 1.0), 4)
                                   for l in bad_v]})
            censored.append({"id": p["id"], "reason": "pumpswap_v_mismatch_gt_1pct",
                             "clip": p["entry_notional"], "pnl": p["realized_pnl"],
                             "closed_at": p["closed_at"], "exit_reason": p["exit_reason"]})
            continue
        prepped.append(row)
    res = {"window": [WINDOW_START, max(p["closed_at"] for p in rows)], "rows": len(rows),
           "modelled": len(prepped), "floor_usd": floor}
    # sample and censoring (amendment 4)
    from collections import Counter
    res["censor_reasons"] = Counter(c["reason"] for c in censored).most_common()
    for tag, group in (("modelled", [{"clip": r["clip_a"], "pnl": r["realized_pnl"],
                                      "exit_reason": r["exit_reason"]} for r in prepped]),
                       ("censored", censored)):
        res[tag + "_summary"] = {
            "n": len(group), "pnl": round(sum(g["pnl"] or 0 for g in group), 2),
            "win": sum(1 for g in group if (g["pnl"] or 0) > 0),
            "clip_q10_50_90": _q([g["clip"] for g in group]),
            "at_175": sum(1 for g in group if abs((g["clip"] or 0) - 175) < 0.01),
            "exit_reasons": Counter(g["exit_reason"] for g in group).most_common(6)}
    res["venues"] = Counter(r["venue"] for r in prepped).most_common()
    # controls
    ctrl = {}
    for venue, side, err in control_rows(prepped):
        ctrl.setdefault(f"{venue}:{side}", []).append(abs(err))
    res["control_a"] = {k: {"n": len(v_), "abs_err_q50_90_max": _q(v_, (0.5, 0.9, 1.0))}
                        for k, v_ in sorted(ctrl.items())}
    ctrl0 = {}
    for venue, side, err in control_rows(prepped, v_zero=True):
        if venue == "pumpswap":
            ctrl0.setdefault(f"{venue}:{side}", []).append(abs(err))
    res["control_b_v_zero"] = {k: {"n": len(v_), "abs_err_q50_90_max": _q(v_, (0.5, 0.9, 1.0))}
                               for k, v_ in sorted(ctrl0.items())}
    res["v_mismatch_rows"] = v_flag
    pair = []
    for r in prepped:
        for a_ in r["entry"]:
            for _n, ls in r["exits"]:
                for b_ in ls:
                    if a_["venue"] == "raydium_cp" and b_["pool"] == a_["pool"]:
                        pair.append(abs(b_["accrued"] - a_["accrued"]) / a_["xe"])
    res["cp_accrued_entry_vs_exit_over_x"] = {"n": len(pair), "q50_90_max": _q(pair, (0.5, 0.9, 1.0))}
    res["cp_accrued_over_vault"] = _q([l["accrued"] / l["x"] for r in prepped for l in r["entry"]
                                       if l["venue"] == "raydium_cp"], (0.1, 0.5, 0.9))
    res["ledger_gap"] = _q([pnl_at(r, r["clip_a"], "T") - r["realized_pnl"] for r in prepped],
                           (0.05, 0.5, 0.95))
    res["jupiter_vs_model_impact"] = _q([r["jupiter_impact"] / (r["q_a"] / r["x_entry"])
                                         for r in prepped if r["jupiter_impact"]])
    res["entry_impact_at_actual"] = _q([r["q_a"] / r["x_entry"] for r in prepped])
    res["x_entry_usd"] = _q([r["x_entry_usd"] for r in prepped])
    res["admission_known"] = sum(1 for r in prepped if r["roundtrip_cost_pct"] is not None)
    # rules
    fns = rules()
    names = list(fns)
    train = [r for r in prepped if r["closed_at"] < HOLDOUT_START]
    hold = [r for r in prepped if r["closed_at"] >= HOLDOUT_START]
    res["train_n"], res["holdout_n"] = len(train), len(hold)
    res["names"] = names
    res["by_q"] = {}
    for q in EQUITY:
        entry = {}
        for sc in ("P", "T"):
            entry["train_" + sc] = evaluate(train, names, fns, q, sc, floor)
            entry["hold_" + sc] = evaluate(hold, names, fns, q, sc, floor, boot=False)
        entry["select"] = select(entry["train_P"], entry["train_T"], names)
        res["by_q"][q] = entry
        print(f"Q={q}: {entry['select']}", file=sys.stderr, flush=True)
    (OUT / "results.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def selftest():
    # the constant-product pair is its own inverse at zero fee
    x, y, q = 50e9, 800e12, 1.3e9
    t = buy_out(q, x, y)
    assert abs(sell_out(t, x + q, y - t) - q) / q < 1e-12
    # P at the actual clip equals T: no shift when s == 1
    leg = {"venue": "pump_curve", "pool": "c", "quote": q, "coin": t, "xe": x, "y": y}
    ex = {"venue": "pump_curve", "pool": "c", "quote": 1.1e9, "coin": t, "xe": x * 1.2, "y": y / 1.2}
    r = {"clip_a": 150.0, "entry": [leg], "exits": [(140.0, [ex])], "q_a": q, "x_entry": x}
    assert abs(pnl_at(r, 150.0, "P") - (-10.0)) < 1e-9 and abs(pnl_at(r, 150.0, "T") + 10.0) < 1e-9
    # a bigger clip costs more under T than under P, and P with no other flow is fees-only (0 here)
    assert pnl_at(r, 600.0, "T") < pnl_at(r, 600.0, "P")
    flat = {"clip_a": 150.0, "entry": [leg], "q_a": q, "x_entry": x,
            "exits": [(150.0, [{"venue": "pump_curve", "pool": "c", "quote": q, "coin": t,
                                "xe": x + q, "y": y - t}])]}
    assert abs(pnl_at(flat, 600.0, "P")) < 1e-6, pnl_at(flat, 600.0, "P")
    # the V check can fail: a PumpSwap leg priced without its virtual quote is far off
    ps = {"venue": "pumpswap", "pool": "p", "x_vault": 20e9, "xe": 20e9 + 17e9, "y": 5e14,
          "quote": 1e9}
    ps["coin"] = buy_out(1e9, ps["xe"], ps["y"])
    [(_v, _s, ok)] = control_rows([{"entry": [ps], "exits": []}])
    [(_v, _s, bad)] = control_rows([{"entry": [ps], "exits": []}], v_zero=True)
    assert abs(ok) < 1e-12 and abs(bad) > 0.5
    assert abs(implied_v(ps, "buy") - 17e9) / 17e9 < 1e-9
    # CVaR5 of 1..100 is the mean of the worst five
    assert abs(cvar5(list(range(1, 101))) - 3.0) < 1e-12
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=("fetch", "pools", "model", "selftest"))
    args = ap.parse_args()
    if args.stage == "selftest":
        selftest()
        return 0
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))
    OUT.mkdir(parents=True, exist_ok=True)
    rows = load_rows()
    print(f"rows {len(rows)} window {WINDOW_START}..", file=sys.stderr, flush=True)
    try:
        if args.stage == "fetch":
            fetch(rows)
        elif args.stage == "pools":
            fetch_pools(rows)
        else:
            run_model(rows)
    except Stop as exc:
        print(f"STOPPED: {exc}", file=sys.stderr, flush=True)
        return 2
    print(json.dumps(COUNTS), file=sys.stderr)
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('run_model', 'main'))

if __name__ == "__main__":
    sys.exit(main())
