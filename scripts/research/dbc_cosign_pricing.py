#!/usr/bin/env python3
"""What would the co-sign-gated DBC refusals have made, priced from the coins' own pool swaps?

Pre-registered in HANDOFF 2026-09-29 ~08:55Z (Gamma; operator order via Alpha: the new Fomo account
is for co-sign-gated DBC coins only, price it first). No refusal capture exists for these coins
(Jupiter cannot quote them), so the instrument is the DBC pool itself (research key, flat-gated):
Helius getTransactionsForAddress on the pool from the source's block time to +600 s, every DBC swap
event (emitted by self-CPI) decoded for its post-trade next_sqrt_price, direction and amounts.

  spot     sp = next_sqrt_price / 2^64, i.e. sqrt(quote atomic per base atomic)
  entry    the spot after the last swap at or before source time + 2 s (our J7-lane latency); our
           clip buys with the nearest earlier swap's liquidity L (dy / d sp for a buy, dx / d(1/sp)
           for a sell: concentrated-liquidity math within one curve segment) and the pool's fee rate
  marks    the proceeds of selling our whole position at each later swap's spot, with that time's L
  grade    TP at the first mark >= +25%, stop at the first <= -35% (up to +320 s), else the spot at
           +300 s (the last swap at or before it: a pool's price only moves on a trade)
The clip's quote size uses the attempt's base_clip_usd and the source's USD price at the entry spot.

    python3 scripts/research/dbc_cosign_pricing.py [--out logs/acceptance/dbc_cosign_pricing.json]
    python3 scripts/research/dbc_cosign_pricing.py --selftest
"""
import argparse
import base64
import json
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))

LEDGER = "file:%s?mode=ro" % (REPO / "sniper_tracker.db")
DBC = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
FOMO_EXECUTOR = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
EVENT_CPI = bytes.fromhex("e445a52e51cb9a1d")
SWAP2 = {bytes.fromhex("863ba8785e3372e7"), bytes([189, 66, 51, 168, 38, 80, 117, 153])}   # ..WithTransferHook, EvtSwap2
SWAP1 = bytes([27, 60, 21, 213, 138, 170, 187, 147])
Q64 = 2.0 ** 64
TP, STOP, MARK_S, WINDOW_S, LAG_S = 0.25, -0.35, 300.0, 600.0, 2.0
COSIGN_HOOKS = ("eyesEswW", "eyesfsib", "ESKuS8vD")


def u64(b, o):
    return int.from_bytes(b[o:o + 8], "little")


def decode_swap(data):
    """{pool, buy, dx, dy, sp, fee_rate, ts} from one DBC event-CPI payload, or None."""
    if len(data) < 16 or data[:8] != EVENT_CPI:
        return None
    ev = data[8:]
    from solders.pubkey import Pubkey
    if ev[:8] in SWAP2 and len(ev) >= 187:
        buy = ev[72] == 1                                   # QuoteToBase
        included, excluded, out = u64(ev, 91), u64(ev, 99), u64(ev, 115)
        sqrt = int.from_bytes(ev[123:139], "little")
        fees = u64(ev, 139) + u64(ev, 147) + u64(ev, 155)
        ts = u64(ev, 179)
    elif ev[:8] == SWAP1 and len(ev) >= 146:
        buy = ev[72] == 1
        excluded, out = u64(ev, 90), u64(ev, 98)
        sqrt = int.from_bytes(ev[106:122], "little")
        fees = u64(ev, 122) + u64(ev, 130) + u64(ev, 138)
        included, ts = excluded + fees, u64(ev, 154) if len(ev) >= 162 else None
    else:
        return None
    dy, dx = (excluded, out) if buy else (out, excluded)
    return {"pool": str(Pubkey.from_bytes(ev[8:40])), "buy": buy, "dx": dx, "dy": dy, "sp": sqrt / Q64,
            "fee_rate": fees / included if included else None, "ts": ts}


def swaps_in(tx, pool):
    """Every DBC swap event on `pool` in one transaction, with the tx's signers."""
    import base58
    meta = tx.get("meta") or {}
    ld = meta.get("loadedAddresses") or {}
    msg = tx["transaction"]["message"]
    ks = msg["accountKeys"] + ld.get("writable", []) + ld.get("readonly", [])
    signers = set(msg["accountKeys"][:msg["header"]["numRequiredSignatures"]])
    out = []
    for inner in meta.get("innerInstructions") or []:
        for ix in inner["instructions"]:
            if ks[ix["programIdIndex"]] != DBC:
                continue
            ev = decode_swap(base58.b58decode(ix["data"]))
            if ev and ev["pool"] == pool:
                out.append({**ev, "slot": tx["slot"], "index": tx.get("transactionIndex"),
                            "sig": tx["transaction"]["signatures"][0], "fomo_signed": FOMO_EXECUTOR in signers,
                            "block_time": tx.get("blockTime")})
    return out


def liquidity(prev_sp, ev):
    """The segment liquidity L implied by one swap from spot prev_sp, or None."""
    if not prev_sp or not ev["sp"] or prev_sp == ev["sp"]:
        return None
    if ev["buy"]:
        return ev["dy"] / (ev["sp"] - prev_sp) if ev["sp"] > prev_sp else None
    return ev["dx"] / (1 / ev["sp"] - 1 / prev_sp) if ev["sp"] < prev_sp else None


def buy_tokens(dy, sp, L):
    """Base atomic received for quote atomic dy from spot sp (no impact when L is unknown)."""
    if not L:
        return dy / sp ** 2
    sp1 = sp + dy / L
    return L * (1 / sp - 1 / sp1)


def sell_quote(dx, sp, L):
    """Quote atomic received for base atomic dx from spot sp."""
    if not L:
        return dx * sp ** 2
    inv = 1 / sp + dx / L
    return L * (sp - 1 / inv)


def grade(marks):
    """marks: [(age_s, ret)] ascending. (ret, how) or (None, 'censored')."""
    last = None
    for age, r in marks:
        if age > MARK_S + 20:
            break
        if r >= TP:
            return r, "tp"
        if r <= STOP:
            return r, "stop"
        if age <= MARK_S:
            last = r
    return (last, "mark") if last is not None else (None, "censored")


def price_origin(o, events, fee_default=0.01, lag_s=LAG_S):
    """The graded result for one origin from its pool's ordered events, entering at t0 + lag_s."""
    t0 = o["t0"]
    evs = sorted((e for e in events if e["ts"] is not None), key=lambda e: (e["slot"], e["index"] or 0))
    before = [e for e in evs if e["ts"] <= t0 + lag_s]
    if not before:
        return {**o, "how": "no_entry_state"}
    sp0 = before[-1]["sp"]
    prev = None
    for e in evs:                                  # liquidity per event, from the previous spot
        e["L"] = liquidity(prev, e)
        prev = e["sp"]
    L0 = next((e["L"] for e in reversed(before) if e["L"]), None)
    fees = [e["fee_rate"] for e in evs if e["fee_rate"] is not None]
    f = statistics.median(fees) if fees else fee_default
    usd_per_q = (o["source_price"] / 10 ** o["decimals"]) / sp0 ** 2
    clip_q = o["clip_usd"] / usd_per_q
    dx = buy_tokens(clip_q * (1 - f), sp0, L0)
    marks, L = [], L0
    for e in evs:
        if e["ts"] <= t0 + lag_s:
            continue
        L = e["L"] or L
        ret = sell_quote(dx, e["sp"], L) * (1 - f) / clip_q - 1
        marks.append((e["ts"] - t0, ret))
    rt_now = sell_quote(dx, sp0, L0) * (1 - f) / clip_q - 1            # round trip at entry
    marks_full = [(lag_s, rt_now)] + marks
    ret, how = grade(marks_full)
    return {**o, "sp0": sp0, "L_known": L0 is not None, "fee": f, "roundtrip_at_entry": rt_now, "grade": ret,
            "how": how, "n_events": len(evs), "n_after": len(marks),
            "max_60": max((r for a, r in marks if a <= 60), default=None),
            "sells_without_fomo": sum(1 for e in evs if not e["buy"] and not e["fomo_signed"]),
            "sells": sum(1 for e in evs if not e["buy"])}


def origins():
    from dbc_hook_refusals import attempts, source_sig
    rows = attempts("2026-09-13T00:00:00")
    out, seen = [], set()
    with sqlite3.connect(LEDGER, uri=True, timeout=30) as c:
        for r in rows:
            if r["origin_event_id"] in seen:
                continue
            reason = r["terminal_reason"] or ""
            if r["mint"][:8] not in ("ZrueWB1Y", "74cmFdL5", "CpzF978U", "AzmuURyd") and not any(
                    h in reason for h in COSIGN_HOOKS):
                continue
            seen.add(r["origin_event_id"])
            sc = c.execute("SELECT source_created_at FROM memecoin_entry_attempts WHERE attempt_id=?",
                           (r["attempt_id"],)).fetchone()[0]
            out.append({"attempt": r["attempt_id"], "origin": r["origin_event_id"], "mint": r["mint"],
                        "handle": r["source_handle"], "source_price": r["source_price"], "clip_usd": r["clip"],
                        "sig": source_sig(r["origin_event_id"]), "source_created_at": sc})
    return out


def run(args):
    import funded_exit_truth as fet
    import wallet_crowd_dump_scoreboard as wcs
    from slot_minout_grading import wait_flat
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.util.envfiles import load_env_files
    load_env_files(str(REPO))
    fet.SPACING_S = 0.5
    real = fet.rpc

    def gated(method, params):
        wait_flat(lambda: get_readonly_connection(str(REPO / "sniper_tracker.db")), max_wait_s=1800)
        return real(method, params)
    fet.rpc = gated
    os_ = origins()
    pools, decimals = {}, {}
    for o in os_:
        m = o["mint"]
        if m not in pools:
            res = gated("getProgramAccounts", [DBC, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0},
                                                     "filters": [{"memcmp": {"offset": 136, "bytes": m}}]}]) or []
            pools[m] = res[0]["pubkey"] if len(res) == 1 else None
            mi = (gated("getAccountInfo", [m, {"encoding": "base64"}]) or {}).get("value")
            decimals[m] = base64.b64decode(mi["data"][0])[44] if mi else 6
        o["pool"], o["decimals"] = pools[m], decimals[m]
        if o["sig"]:
            tx = gated("getTransaction", [o["sig"], {"encoding": "json", "maxSupportedTransactionVersion": 1}])
            o["t0"] = tx["blockTime"] if tx else None
        else:
            o["t0"] = int(datetime.fromisoformat(o["source_created_at"]).timestamp())
    cache, results = {}, []
    for o in os_:
        if not o["pool"] or o["t0"] is None:
            results.append({**o, "how": "pool_or_time_unknown"})
            continue
        key = (o["pool"], o["t0"])
        if key not in cache:
            txs, _done = wcs.gtfa(o["pool"], {"blockTime": {"gte": o["t0"] - 30, "lte": o["t0"] + int(WINDOW_S)}},
                                  limit=100, pages=20)
            cache[key] = [e for tx in txs for e in swaps_in(tx, o["pool"])]
        results.append(price_origin(o, list(cache[key])))
        for lag in (5.0, 10.0, 30.0):                 # the co-sign route's latency is unknown
            r = price_origin(o, list(cache[key]), lag_s=lag)
            results[-1]["lag_%d" % lag] = r.get("grade")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=1, default=str))
    for r in results:
        print("%s %-14s %s %-9s grade %s rt@entry %s max60 %s events %s sells %s (no Fomo sig %s) L %s" % (
            r["attempt"][:8], (r["handle"] or "")[:14], r["mint"][:8], r.get("how"),
            None if r.get("grade") is None else "%+.1f%%" % (100 * r["grade"]),
            None if r.get("roundtrip_at_entry") is None else "%+.1f%%" % (100 * r["roundtrip_at_entry"]),
            None if r.get("max_60") is None else "%+.1f%%" % (100 * r["max_60"]),
            r.get("n_events"), r.get("sells"), r.get("sells_without_fomo"), r.get("L_known")))
    for lag in ("grade", "lag_5", "lag_10", "lag_30"):
        first, seen = [], set()
        for r in results:
            if r["mint"] not in seen and r.get(lag) is not None:
                seen.add(r["mint"])
                first.append(r)
        allg = [r for r in results if r.get(lag) is not None]
        print("%-7s all origins n=%d mean %+.1f%% sum $%+.2f | first origin per coin n=%d mean %+.1f%% sum $%+.2f" % (
            lag, len(allg), 100 * sum(r[lag] for r in allg) / max(1, len(allg)), sum(r[lag] * r["clip_usd"] for r in allg),
            len(first), 100 * sum(r[lag] for r in first) / max(1, len(first)), sum(r[lag] * r["clip_usd"] for r in first)))
    g = [r for r in results if r.get("grade") is not None]
    if g:
        print("\ngraded n=%d mean %+.1f%% sum $%+.2f (per origin); coins %d" % (
            len(g), 100 * sum(r["grade"] for r in g) / len(g), sum(r["grade"] * r["clip_usd"] for r in g),
            len({r["mint"] for r in g})))


def market(args):
    """SECONDARY: J7 Solana buys on hook-launchpad coins since 09-13, by hook class and ISO week.
    A migrated coin's hook is revoked on the mint, so its hook is read from one J7 event's source tx."""
    import funded_exit_truth as fet
    from collections import Counter, defaultdict
    from dbc_hook_refusals import TOKEN_2022, classify, hook_in_logs, source_sig
    from slot_minout_grading import wait_flat
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.util.envfiles import load_env_files
    from solders.pubkey import Pubkey
    import hashlib
    load_env_files(str(REPO))
    fet.SPACING_S = 0.5
    base_rpc = fet.rpc                               # captured before any swap, so the gate never recurses

    def rpc(method, params):
        wait_flat(lambda: get_readonly_connection(str(REPO / "sniper_tracker.db")), max_wait_s=1800)
        return base_rpc(method, params)
    with sqlite3.connect(LEDGER, uri=True, timeout=30) as c:
        lo, hi = 1, c.execute("SELECT MAX(id) FROM memecoin_events").fetchone()[0]
        while lo < hi:                               # the first event id on or after 09-13 (id order ~ time)
            mid = (lo + hi) // 2
            r = c.execute("SELECT created_at FROM memecoin_events WHERE id>=? ORDER BY id LIMIT 1", (mid,)).fetchone()
            lo, hi = (mid + 1, hi) if r[0] < "2026-09-13" else (lo, mid)
        c.row_factory = sqlite3.Row
        armed = set(m.real_approved_user_ids(c, network_id="solana"))
        evs = c.execute("SELECT fomo_event_id, source_user_id, mint, their_size_usd, created_at FROM memecoin_events "
                        "WHERE id>=? AND network_id='solana' AND side='buy' AND created_at>='2026-09-13'", (lo,)).fetchall()
    mints = sorted({e["mint"] for e in evs})
    print("J7 solana buys since 09-13: %d on %d mints; armed now %d wallets" % (len(evs), len(mints), len(armed)))
    ext = {}
    for i in range(0, len(mints), 100):
        vals = (rpc("getMultipleAccounts", [mints[i:i + 100], {"encoding": "base64"}]) or {}).get("value") or []
        for mint, v in zip(mints[i:i + 100], vals):
            if not v or v["owner"] != TOKEN_2022:
                continue
            d = base64.b64decode(v["data"][0])
            j = 166
            while j + 4 <= len(d):
                t, n = int.from_bytes(d[j:j + 2], "little"), int.from_bytes(d[j + 2:j + 4], "little")
                if t == 14 and n >= 64:
                    pid = d[j + 4 + 32:j + 4 + 64]
                    ext[mint] = None if pid == bytes(32) else str(Pubkey.from_bytes(pid))
                j += 4 + n
    print("Token-2022 mints with a TransferHook extension: %d (revoked now %d)" % (
        len(ext), sum(1 for v in ext.values() if v is None)))
    by_mint = defaultdict(list)
    for e in evs:
        by_mint[e["mint"]].append(e)
    import wallet_crowd_dump_scoreboard as wcs
    fet.rpc = rpc                                    # gtfa goes through the flat gate too
    for mint in [k for k, v in ext.items() if v is None]:
        # The coin's EARLIEST transactions are curve-phase trades, which ran the hook before migration
        # revoked it; J7 source txs are the fallback (a uuid event carries no signature).
        early, _done = wcs.gtfa(mint, {}, order="asc", limit=25, pages=1)
        txs = list(early)
        for e in by_mint[mint][:3]:
            sig = source_sig(e["fomo_event_id"])
            if sig:
                txs.append(rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1}]))
        for tx in txs:
            h = hook_in_logs(((tx or {}).get("meta") or {}).get("logMessages") or [])
            if h:
                ext[mint] = h
                break
    fet.rpc = base_rpc
    print("revoked hooks resolved: %d of %d" % (sum(1 for v in ext.values() if v), len(ext)))
    klass = {}
    for prog in sorted({v for v in ext.values() if v}):
        p = (rpc("getAccountInfo", [prog, {"encoding": "base64"}]) or {}).get("value")
        pd_key = str(Pubkey.from_bytes(base64.b64decode(p["data"][0])[4:36])) if p else None
        pd = (rpc("getAccountInfo", [pd_key, {"encoding": "base64"}]) or {}).get("value") if pd_key else None
        exe = base64.b64decode(pd["data"][0])[45:] if pd else b""
        klass[prog] = classify(exe.rstrip(b"\x00"), hashlib.sha256(exe).hexdigest())[0] if pd else "unknown"
    print("hook programs %d by class %s" % (len(klass), dict(Counter(klass.values()))))
    weekly = defaultdict(Counter)
    for mint, prog in ext.items():
        k = klass.get(prog, "unresolved") if prog else "unresolved"
        for e in by_mint[mint]:
            wk = datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")).strftime("%G-W%V")
            weekly[wk][k + ":buys"] += 1
            weekly[wk][k + ":usd"] += float(e["their_size_usd"] or 0)
            if e["source_user_id"] in armed:
                weekly[wk][k + ":armed_buys"] += 1
        for wk in {datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")).strftime("%G-W%V") for e in by_mint[mint]}:
            weekly[wk][k + ":coins"] += 1
    for wk in sorted(weekly):
        print("  %s %s" % (wk, {k: round(v) for k, v in sorted(weekly[wk].items())}))


def selftest():
    # constant-liquidity round trip at one spot costs only fees and nothing else
    L, sp = 5e6, 0.002
    dx = buy_tokens(1000.0, sp, L)
    assert abs(sell_quote(dx, buy_sp := sp + 1000.0 / L, L) - 1000.0) < 1e-6 * 1000   # sell back at the post-buy spot
    assert sell_quote(dx, sp, L) < 1000.0                                            # at the pre-buy spot: impact both ways
    assert abs(buy_tokens(10.0, sp, None) - 10.0 / sp ** 2) < 1e-9
    ev_buy = {"buy": True, "dy": 1000.0, "dx": 0, "sp": sp + 1000.0 / L}
    assert abs(liquidity(sp, ev_buy) - L) < 1e-3
    ev_sell = {"buy": False, "dy": 0, "dx": dx, "sp": sp}
    assert abs(liquidity(buy_sp, ev_sell) - L) / L < 1e-9
    assert grade([(2, -0.02), (40, 0.3)]) == (0.3, "tp")
    assert grade([(2, -0.02), (100, -0.4)]) == (-0.4, "stop")
    assert grade([(2, -0.02), (200, 0.1), (400, 0.5)]) == (0.1, "mark")
    # an EvtSwap2WithTransferHook payload round-trips through the decoder
    from solders.pubkey import Pubkey
    pool = Pubkey.from_string("G3oGUvRhiycTVZVfXGH6MqoJcWkvYLahP5g7T99CLUSZ")
    ev = bytearray(187)
    ev[0:8] = bytes.fromhex("863ba8785e3372e7")
    ev[8:40] = bytes(pool)
    ev[72] = 1
    ev[91:99] = (1010).to_bytes(8, "little")
    ev[99:107] = (1000).to_bytes(8, "little")
    ev[115:123] = (777).to_bytes(8, "little")
    ev[123:139] = int(0.5 * Q64).to_bytes(16, "little")
    ev[139:147] = (10).to_bytes(8, "little")
    ev[179:187] = (1790514582).to_bytes(8, "little")
    d = decode_swap(EVENT_CPI + bytes(ev))
    assert d["pool"] == str(pool) and d["buy"] and d["dy"] == 1000 and d["dx"] == 777
    assert abs(d["sp"] - 0.5) < 1e-12 and abs(d["fee_rate"] - 10 / 1010) < 1e-12 and d["ts"] == 1790514582
    assert decode_swap(bytes(ev)) is None                                   # no event-CPI tag
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--market", action="store_true")
    ap.add_argument("--out", default=str(REPO / "logs" / "acceptance" / "dbc_cosign_pricing.json"))
    a = ap.parse_args()
    return selftest() if a.selftest else market(a) if a.market else run(a)


if __name__ == "__main__":
    main()
