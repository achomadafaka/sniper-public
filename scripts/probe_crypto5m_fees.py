#!/usr/bin/env python3
"""Phase 1C fee-truth probe (READ-ONLY). Samples live + recent BTC 5m markets and records the canonical
fee evidence per market: Gamma diagnostic fields, CLOB /markets/<cid> (the `fd` descriptor if any),
the per-token /fee-rate `base_fee`, and the synthesized fee_status (fee_model.synthesize_fee_status).

NO orders, NO private keys, NO auth, NO production DB. Persists raw payloads to a local sqlite and prints a
per-market report. The pure synthesis is unit-tested; this is the live evidence-gathering smoke.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper.algos.polymarket_crypto5m import fee_model  # noqa: E402

UA = {"User-Agent": "sniper-research/0.1"}
GAMMA = "https://gamma-api.polymarket.com/events?slug=btc-updown-5m-"
CLOB_MARKET = "https://clob.polymarket.com/markets/"
CLOB_FEE_RATE = "https://clob.polymarket.com/fee-rate?token_id="


def _get(url, timeout=15):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read())


def _rate_from_descriptor(d):
    """Decode a fractional rate from a fee descriptor (Gamma feeSchedule or CLOB fd) ONLY when exponent==1
    (we model fee=rate·p·(1-p)) and the rate is a sane fraction; else None (fail closed). A bare
    base_fee/taker_base_fee=1000 is NOT a decoded rate."""
    if not isinstance(d, dict):
        return None
    exp = d.get("exponent", d.get("e"))
    try:
        if exp is not None and abs(float(exp) - 1.0) > 1e-9:
            return None
        rf = float(d.get("rate", d.get("r")))
        return rf if 0.0 <= rf <= 1.0 else None
    except (TypeError, ValueError):
        return None


def _canonical_rate(gamma_market: dict, clob_market: dict):
    """Prefer the CLOB fd descriptor; fall back to the Gamma feeSchedule (both documented structured
    descriptors). Returns the decoded fractional rate, or None."""
    for d in (clob_market.get("fd"), gamma_market.get("feeSchedule")):
        r = _rate_from_descriptor(d)
        if r is not None:
            return r
    return None


def _realized_fee_rate_bps(cid):
    """Best-effort realized taker fee (bps) from a recent trade (diagnostic, never authoritative). None if
    unavailable."""
    for url in (f"https://data-api.polymarket.com/trades?market={cid}&limit=1",):
        try:
            r = _get(url)
            rows = r if isinstance(r, list) else r.get("data") or []
            for t in rows:
                for k in ("fee_rate_bps", "feeRateBps", "fee_bps"):
                    if t.get(k) is not None:
                        return float(t[k])
        except Exception:  # noqa: BLE001
            pass
    return None


def _ensure_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS crypto5m_fee_probe (
        condition_id TEXT, window_start_ts INTEGER, probed_ts INTEGER,
        gamma_fees_enabled TEXT, gamma_fee_type TEXT, gamma_taker_base_fee TEXT,
        gamma_fee_schedule TEXT, clob_fd TEXT, clob_taker_base_fee TEXT, token_base_fees TEXT,
        canonical_rate REAL, realized_fee_rate_bps REAL,
        fee_status TEXT, fee_rate REAL, caveat TEXT, raw_json TEXT)""")
    conn.commit()
    return conn


def probe(*, n_windows, db_path):
    conn = _ensure_db(db_path)
    now = int(_get("https://api.binance.com/api/v3/time")["serverTime"]) // 1000
    cur = now - (now % 300)
    rows = []
    for k in range(n_windows):
        ws = cur - k * 300
        try:
            ev = _get(f"{GAMMA}{ws}")
        except Exception as e:  # noqa: BLE001
            continue
        if not ev:
            continue
        mk = (ev[0].get("markets") or [{}])[0]
        cid = mk.get("conditionId")
        if not cid:
            continue
        try:
            clob = _get(f"{CLOB_MARKET}{cid}")
        except Exception as e:  # noqa: BLE001
            clob = {}
        toks = mk.get("clobTokenIds")
        toks = ast.literal_eval(toks) if isinstance(toks, str) else (toks or [])
        token_base_fees = {}
        for t in toks:
            try:
                token_base_fees[t[:16]] = _get(f"{CLOB_FEE_RATE}{t}").get("base_fee")
            except Exception:  # noqa: BLE001
                token_base_fees[t[:16]] = None
        fees_enabled = fee_model.normalize_bool(mk.get("feesEnabled"))
        gamma_fee_schedule = mk.get("feeSchedule")
        canonical_rate = _canonical_rate(mk, clob)
        realized = _realized_fee_rate_bps(cid)
        syn = fee_model.synthesize_fee_status(
            fees_enabled=fees_enabled, canonical_rate=canonical_rate, ws_rate=None,
            observed_fee_rate_bps=realized)
        raw = {"gamma_market": mk, "clob_market": clob, "token_base_fees": token_base_fees,
               "realized_fee_rate_bps": realized}
        conn.execute("INSERT INTO crypto5m_fee_probe VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            cid, ws, int(time.time()), str(mk.get("feesEnabled")), mk.get("feeType"),
            str(mk.get("takerBaseFee")), json.dumps(gamma_fee_schedule), json.dumps(clob.get("fd")),
            str(clob.get("taker_base_fee")), json.dumps(token_base_fees), canonical_rate, realized,
            syn["fee_status"], syn.get("fee_rate"), syn.get("caveat"), json.dumps(raw)))
        rows.append((ws, cid, mk.get("feeType"), gamma_fee_schedule, clob.get("fd"), canonical_rate,
                     realized, list(token_base_fees.values()), syn))
    conn.commit()
    conn.close()
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only BTC 5m fee-truth probe")
    ap.add_argument("--windows", type=int, default=10, help="how many recent 5m windows to sample")
    ap.add_argument("--db-path", default="data/crypto5m_fee_probe.sqlite")
    a = ap.parse_args(argv)
    os.makedirs(os.path.dirname(a.db_path) or ".", exist_ok=True)
    rows = probe(n_windows=a.windows, db_path=a.db_path)
    print(f"=== BTC 5m fee probe: {len(rows)} markets sampled -> {a.db_path} ===")
    statuses = {}
    for ws, cid, ftype, gfs, fd, crate, realized, tfees, syn in rows:
        statuses[syn["fee_status"]] = statuses.get(syn["fee_status"], 0) + 1
        print(f"  ws={ws} {cid[:14]} feeType={ftype} clob.fd={fd} gamma.feeSchedule={gfs} "
              f"canonical_rate={crate} realized_bps={realized} token_base_fees={tfees} "
              f"-> {syn['fee_status']} ({syn.get('caveat') or syn.get('fee_source','')})")
    print("  fee_status tally:", statuses)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
