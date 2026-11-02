#!/usr/bin/env python3
"""Ask every venue we can reach whether each leftover holding is actually sellable.

WHY. The cockpit's holdings list mixes three different things: bags with a live market, bags whose
only market is invisible to our price provider, and bags that are genuinely dead. The row's
`liquidity_usd` comes from DexScreener, which does not index Uniswap V4 on chain 4663 — and V4 is
where these tokens trade. So an empty liquidity column is not evidence of anything, and the
operator cannot tell a stuck $5 from a worthless $5 by looking.

This settles each row against the venues themselves and prints a verdict with the quote that
earned it. Read-only: it quotes, it never sells, and it writes nothing.

WHAT COUNTS AS SELLABLE. One venue returning a positive output for THE WHOLE BAG. Not a price, not
a market cap, not a liquidity reading — an amount of ETH or SOL a venue offers for the exact
balance. UNIFROG (2026-09-23) is why: DexScreener has no pair for it, GMGN prices it, and its V4
pool quotes 0.0012983 ETH for the whole bag. Three sources, one of them a bid.

VENUES, in the order they are asked, stopping at the first that answers:
  4663   V4 through our stored pool keys (bounded to MAX_V4_POOLS per token, deepest fee tiers
         first), then V2 direct and through each immutable connector, then V3 at each fee tier.
  solana Jupiter, through the same exit-quote helper the funded exits use.

⚠️ LOAD. Heavy reads from this host stall the live 4663 tape (2026-09-22), so publicnode calls are
paced at PACE_S and bounded by --max-calls; a run that hits the cap says so instead of finishing
quietly on partial evidence. GMGN is paced process-wide by its own client at 1/s. Nothing here
touches the funded custody RPC.

⚠️ A FAILED READ IS NOT A ZERO. A positive whole-bag quote proves sellability. V4's helper merges
no quote with provider errors, and the stored-key walk is bounded; a negative 4663 search is
`unknown`, never a hide-list candidate.

    python3 scripts/research/holdings_sellability_triage.py
    python3 scripts/research/holdings_sellability_triage.py --max-calls 400 --selftest
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DB = ROOT / "sniper_tracker.db"
RPC_4663 = "https://robinhood-rpc.publicnode.com"
# publicnode refuses Python's default User-Agent with a 403 and no explanatory body, the same trap
# the GMGN client documents. Measured 2026-09-23.
UA = "curl/8.5.0"
WETH_4663 = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
PACE_S = 0.25
MAX_V4_POOLS = 6
ZERO_ADDR = "0x" + "0" * 40


class Budget:
    """A hard ceiling on chain calls, so a triage can never become the load that hurts the tape."""

    def __init__(self, limit):
        self.limit = int(limit)
        self.used = 0
        self.last = 0.0

    def take(self):
        if self.used >= self.limit:
            return False
        wait = PACE_S - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        self.used += 1
        return True


def rpc_call(to, data, budget):
    """eth_call, or None when the budget is spent or the node errors. Never raises."""
    if not budget.take():
        return "budget_spent"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": data}, "latest"]}).encode()
    req = urllib.request.Request(RPC_4663, data=body,
                                 headers={"content-type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode())
    except Exception as exc:
        return f"error:{type(exc).__name__}"
    if "error" in payload:
        # ONLY A REVERT IS EVIDENCE ABOUT THE MARKET. Every other JSON-RPC error is evidence
        # about the node: -32429 is a rate limit, -32603 an internal fault, and calling either
        # of them a refusal puts a live token on the hide list.
        err = payload["error"] if isinstance(payload.get("error"), dict) else {}
        message = str(err.get("message") or "").lower()
        if "revert" in message or "out of gas" in message:
            return "refused"
        return f"error:rpc_{err.get('code', 'unknown')}"
    res = payload.get("result")
    return res if res not in (None, "0x") else "refused"


def _w(x):
    return hex(int(x))[2:].rjust(64, "0")


def _a(addr):
    return str(addr).lower().replace("0x", "").rjust(64, "0")


def v2_out(router, path, amount, budget):
    data = "0xd06ca61f" + _w(amount) + _w(64) + _w(len(path)) + "".join(_a(p) for p in path)
    res = rpc_call(router, data, budget)
    if not isinstance(res, str) or not res.startswith("0x"):
        return res
    words = [res[2:][i:i + 64] for i in range(0, len(res) - 2, 64)]
    return int(words[-1], 16) if words else "refused"


def v3_out(quoter, token, weth, fee, amount, budget):
    res = rpc_call(quoter, "0xc6a5026a" + _a(token) + _a(weth) + _w(amount) + _w(fee) + _w(0),
                   budget)
    if not isinstance(res, str) or not res.startswith("0x") or len(res) < 66:
        return res
    return int(res[2:66], 16)


def v4_keys_for(conn, token, limit=MAX_V4_POOLS):
    """Stored V4 pool keys naming this token, deepest-looking fee tiers first."""
    rows = conn.execute(
        "SELECT pool_id, key_json FROM memecoin_evm_v4_pool_keys WHERE network_id='4663' "
        "AND LOWER(key_json) LIKE ?", (f"%{token[2:].lower()}%",)).fetchall()
    keys = []
    for pool_id, raw in rows:
        try:
            k = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if token.lower() not in (str(k.get("currency0", "")).lower(),
                                 str(k.get("currency1", "")).lower()):
            continue                      # the LIKE can match a substring of the other currency
        keys.append((pool_id, k))
    # A native-ETH pair is the one the ladder can actually route, so try those first.
    keys.sort(key=lambda pk: (str(pk[1].get("currency0", "")).lower() != ZERO_ADDR,
                              -int(pk[1].get("fee") or 0)))
    return keys if limit is None else keys[:limit]


def _settles_native(key, token):
    """True when the OTHER side of this V4 pool is the asset we settle in.

    A pool that pays an unrelated token is not proceeds: selling into it leaves a different bag,
    and no leg to native has been quoted. Only native and its wrapper count (Codex review N10).
    """
    other = (str(key.get("currency1", "")) if str(key.get("currency0", "")).lower()
             == token.lower() else str(key.get("currency0", ""))).lower()
    return other in (ZERO_ADDR, WETH_4663.lower())


def v4_key_count(conn, token):
    """How many stored V4 keys name this token, so a bounded walk can state its own scope."""
    return sum(1 for _ in v4_keys_for(conn, token, limit=None))


def quote_4663(conn, token, amount, budget, quote_single):
    """(verdict, detail). Stops at the first venue that offers something for the whole bag.

    A FAILURE ANYWHERE FORCES `unknown`. A bounded set of stored V4 keys cannot prove no route
    exists elsewhere, so negative 4663 searches also remain unknown.
    """
    failures = []
    keys = v4_keys_for(conn, token)
    stored = v4_key_count(conn, token)
    for pool_id, k in keys:
        if budget.used >= budget.limit:
            return "unknown", "budget spent before every venue answered"
        zero_for_one = str(k.get("currency0", "")).lower() == token.lower()
        budget.take()
        try:
            out = quote_single("4663", k, zero_for_one, amount, RPC_4663)
        except Exception as exc:
            failures.append(f"v4 {pool_id[:10]} raised {type(exc).__name__}")
            continue
        if out is None:
            failures.append(f"v4 {pool_id[:10]} no quote or provider failure")
            continue
        if out:
            if not _settles_native(k, token):
                other = (str(k.get("currency1")) if str(k.get("currency0", "")).lower()
                         == token.lower() else str(k.get("currency0")))
                return ("unknown",
                        f"v4 pool {pool_id[:14]}… pays {out} of {other[:10]}…, not native; "
                        "no leg to ETH was quoted")
            return "sellable", f"v4 pool {pool_id[:14]}… fee {k.get('fee')} -> {out} wei"
    from sniper.settings import memecoin as cfg
    router = cfg.MEMECOIN_EVM_V2_ROUTER["4663"]
    paths = [[token, WETH_4663]]
    paths += [[token, c, WETH_4663] for c in cfg.MEMECOIN_EVM_V2_CONNECTORS["4663"]]
    for path in paths:
        out = v2_out(router, path, amount, budget)
        if isinstance(out, int) and out > 0:
            return "sellable", f"v2 via {len(path)} hops -> {out} wei"
        if out == "budget_spent":
            return "unknown", "budget spent before every venue answered"
        if isinstance(out, str) and out.startswith("error"):
            failures.append(f"v2/{len(path)}hop {out}")
    quoter = cfg.MEMECOIN_EVM_V3_QUOTER["4663"]
    for fee in (100, 500, 3000, 10000):
        out = v3_out(quoter, token, WETH_4663, fee, amount, budget)
        if isinstance(out, int) and out > 0:
            return "sellable", f"v3 fee {fee} -> {out} wei"
        if out == "budget_spent":
            return "unknown", "budget spent before every venue answered"
        if isinstance(out, str) and out.startswith("error"):
            failures.append(f"v3/{fee} {out}")
    if failures:
        return "unknown", "a venue failed to answer: " + "; ".join(failures[:3])
    return "unknown", (f"no route in the tested scope: {len(keys)} of {stored} stored v4 keys, "
                       f"v2 direct + {len(paths) - 1} connectors, v3 at 4 fee tiers")


# `jupiter_exit_quote` RAISES QuoteUnavailable with a typed failure; it does not return None.
# Three of those failures are evidence about the TOKEN, four are evidence about US.
_TOKEN_FAILURES = ("NO_ROUTE", "INSUFFICIENT_LIQUIDITY", "TOKEN_REJECTED")


def quote_solana(token, amount_atomic):
    """Jupiter, through the funded exits' own helper. (verdict, detail).

    It quotes token -> USDC, never token -> SOL, so the proceeds are USDC at USDC_DECIMALS.
    """
    from sniper.services import memecoin_quotes as mq
    try:
        q = mq.jupiter_exit_quote(token, int(amount_atomic), 5.0)
    except mq.QuoteUnavailable as exc:
        name = getattr(getattr(exc, "failure", None), "name", "UNKNOWN")
        if name in _TOKEN_FAILURES:
            return "unsellable", f"jupiter: {name.lower()} for the whole bag"
        return "unknown", f"jupiter provider failure: {name.lower()}"
    except Exception as exc:
        return "unknown", f"jupiter raised {type(exc).__name__}"
    if not q:
        return "unknown", "jupiter answered nothing at all"
    out = q.get("outAmount") if isinstance(q, dict) else None
    mint = q.get("outputMint") if isinstance(q, dict) else None
    try:
        out = int(out)
    except (TypeError, ValueError):
        return "unknown", f"jupiter answered an unreadable shape: {str(q)[:60]}"
    if mint and str(mint) != mq.USDC_MINT:
        return "unknown", f"jupiter settled in {str(mint)[:10]}…, not USDC; units unverified"
    human = out / 10 ** mq.USDC_DECIMALS
    return ("sellable", f"jupiter -> {human:.2f} USDC ({out} atomic, {mq.USDC_DECIMALS} dp)") \
        if out > 0 else ("unsellable", "jupiter offered zero for the whole bag")


def holdings(conn):
    """The newest snapshot PER CHAIN. A single global max would drop the chain scanned less often."""
    out = []
    for (net,) in conn.execute("SELECT DISTINCT network_id FROM memecoin_other_holdings"):
        mx = conn.execute("SELECT MAX(observed_at) FROM memecoin_other_holdings WHERE network_id=?",
                          (net,)).fetchone()[0]
        out += [dict(r) for r in conn.execute(
            "SELECT network_id, mint, symbol, amount, decimals, price_usd, value_usd, liquidity_usd "
            "FROM memecoin_other_holdings WHERE network_id=? AND observed_at=?", (net, mx))]
    out.sort(key=lambda r: -(float(r["value_usd"] or 0)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-calls", type=int, default=600, help="hard ceiling on 4663 eth_calls")
    ap.add_argument("--min-value-usd", type=float, default=0.0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    from sniper.util.envfiles import load_env_files
    load_env_files(str(ROOT))
    from sniper.execution.evm_v4_quote import _quote_single
    budget = Budget(args.max_calls)
    with contextlib.closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = holdings(conn)
        rows = [r for r in rows if float(r["value_usd"] or 0) >= args.min_value_usd]
        print(f"{len(rows)} holdings, call budget {args.max_calls}, pace {PACE_S}s")
        print("amounts are rebuilt from the float snapshot: approximate, not exact atomic "
              "balances\n")
        print(f"{'symbol':<12} {'chain':<7} {'value':>9} {'verdict':<11} detail")
        verdicts = {}
        for r in rows:
            # DECIMALS 0 IS A VALUE, NOT A MISSING FIELD. `or 18` turned a zero-decimal balance
            # into a quote for 10^18 times the bag (Codex review N12). Absent stays absent.
            amount, scale_problem = None, None
            decimals = r["decimals"]
            if decimals is None:
                scale_problem = "the snapshot carries no decimals, so the atomic scale is unknown"
            elif not (0 <= int(decimals) <= 36):
                scale_problem = f"decimals {decimals} is outside any real token's range"
            else:
                amount = int(float(r["amount"] or 0) * 10 ** int(decimals))
            chain = str(r["network_id"])
            if scale_problem:
                verdict, detail = "unknown", scale_problem
            elif amount <= 0:
                verdict, detail = "unknown", "the snapshot carries no balance"
            elif chain == "solana":
                verdict, detail = quote_solana(r["mint"], amount)
            elif chain == "4663":
                # The amount is rebuilt from a FLOAT snapshot, so it is the bag to the snapshot's
                # precision, not an exact atomic balance.
                verdict, detail = quote_4663(conn, str(r["mint"]), amount, budget, _quote_single)
            else:
                # One hardcoded node, one chain. Quoting 8453 against it would answer about the
                # wrong chain's state (Codex review N10).
                verdict, detail = "unknown", f"chain {chain} is not quoted by this tool"
            verdicts.setdefault(verdict, []).append(r)
            print(f"{str(r['symbol'])[:12]:<12} {str(r['network_id']):<7} "
                  f"${float(r['value_usd'] or 0):>8.2f} {verdict:<11} {detail}")
    print(f"\ncalls used: {budget.used} of {budget.limit}")
    for v in ("sellable", "unsellable", "unknown"):
        got = verdicts.get(v, [])
        total = sum(float(x["value_usd"] or 0) for x in got)
        print(f"  {v:<11} {len(got):>3} rows, ${total:,.2f}")
    dead = verdicts.get("unsellable", [])
    if dead:
        print("\nHIDE LIST — every venue refused the whole bag (paste into the hidden-holdings file):")
        print(json.dumps({"4663": sorted(str(r["mint"]) for r in dead
                                         if str(r["network_id"]) == "4663"),
                          "solana": sorted(str(r["mint"]) for r in dead
                                           if str(r["network_id"]) == "solana")}, indent=1))
    if verdicts.get("unknown"):
        print("\n⚠️ UNKNOWN rows are NOT unsellable — a read failed or the budget ran out. "
              "Rerun before treating any of them as dead.")
    return 0


def selftest() -> int:
    """The parts that decide a verdict, without a network: key selection and answer reading."""
    T = "0x87f1ed895b460c002e82075f1038e7f2ce4d51cd"
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memecoin_evm_v4_pool_keys (network_id TEXT, pool_id TEXT, "
                 "pool_manager TEXT, key_json TEXT, verified_at TEXT)")
    rows = [
        ("native high fee", {"currency0": ZERO_ADDR, "currency1": T, "fee": 330000,
                             "tick_spacing": 3300}),
        ("native low fee", {"currency0": ZERO_ADDR, "currency1": T, "fee": 100,
                            "tick_spacing": 1}),
        ("token paired", {"currency0": "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
                          "currency1": T, "fee": 70000, "tick_spacing": 100}),
        ("another token entirely", {"currency0": ZERO_ADDR,
                                    "currency1": "0x1111111111111111111111111111111111111111",
                                    "fee": 500, "tick_spacing": 10}),
    ]
    for i, (pid, k) in enumerate(rows):
        conn.execute("INSERT INTO memecoin_evm_v4_pool_keys VALUES ('4663',?,?,?,?)",
                     (pid, "pm", json.dumps(k), "t"))
    keys = v4_keys_for(conn, T)
    assert [p for p, _ in keys] == ["native high fee", "native low fee", "token paired"], keys
    assert all("another token" not in p for p, _ in keys)

    # A budget that is spent must stop the run, not silently return "no quote" — which would read
    # as unsellable and hide a live token.
    spent = Budget(0)
    assert rpc_call("0x", "0x", spent) == "budget_spent"
    assert spent.used == 0

    calls = {"n": 0}

    def quote_stub(chain, key, zero_for_one, amount, rpc):
        calls["n"] += 1
        return 12345 if key.get("fee") == 330000 else None

    verdict, detail = quote_4663(conn, T, 10 ** 18, Budget(50), quote_stub)
    assert verdict == "sellable" and "12345 wei" in detail, (verdict, detail)
    assert calls["n"] == 1, "it must stop at the first venue that answers"

    def quote_none(*a, **k):
        return None

    verdict, _ = quote_4663(conn, T, 10 ** 18, Budget(0), quote_none)
    assert verdict == "unknown", "a spent budget is not evidence of a dead market"

    # A provider failure is not a refusal, and a refusal is still a refusal.
    class _Reply:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    real_urlopen = urllib.request.urlopen
    try:
        urllib.request.urlopen = lambda *a, **k: _Reply(
            {"error": {"code": -32429, "message": "rate limit"}})
        assert rpc_call("0x", "0x", Budget(1)) == "error:rpc_-32429", "a rate limit is not a refusal"
        urllib.request.urlopen = lambda *a, **k: _Reply(
            {"error": {"code": -32000, "message": "execution reverted"}})
        assert rpc_call("0x", "0x", Budget(1)) == "refused", "a revert IS evidence about the market"
    finally:
        urllib.request.urlopen = real_urlopen

    # An error at any venue forces unknown; only an all-answered no is unsellable.
    empty = sqlite3.connect(":memory:")
    empty.execute("CREATE TABLE memecoin_evm_v4_pool_keys (network_id TEXT, pool_id TEXT, "
                  "pool_manager TEXT, key_json TEXT, verified_at TEXT)")
    real_v2, real_v3 = globals()["v2_out"], globals()["v3_out"]
    try:
        globals()["v2_out"] = lambda *a: "error:TimeoutError"
        globals()["v3_out"] = lambda *a: "error:TimeoutError"
        verdict, _ = quote_4663(empty, T, 10 ** 18, Budget(50), quote_none)
        assert verdict == "unknown", "a venue that never answered is not a venue that refused"
        globals()["v2_out"] = lambda *a: 0
        globals()["v3_out"] = lambda *a: 0
        verdict, detail = quote_4663(empty, T, 10 ** 18, Budget(50), quote_none)
        assert verdict == "unknown" and "tested scope" in detail, (verdict, detail)
    finally:
        globals()["v2_out"], globals()["v3_out"] = real_v2, real_v3

    # A V4 pool that pays an unrelated token has not quoted a route to native.
    foreign = sqlite3.connect(":memory:")
    foreign.execute("CREATE TABLE memecoin_evm_v4_pool_keys (network_id TEXT, pool_id TEXT, "
                    "pool_manager TEXT, key_json TEXT, verified_at TEXT)")
    foreign.execute("INSERT INTO memecoin_evm_v4_pool_keys VALUES ('4663','p','pm',?,'t')",
                    (json.dumps({"currency0": T, "currency1": "0x" + "22" * 20, "fee": 500}),))
    verdict, _ = quote_4663(foreign, T, 10 ** 18, Budget(50), lambda *a: 10 ** 18)
    assert verdict == "unknown", "an unrelated token's output is not native proceeds"

    print("selftest ok: key selection, native-first ordering, first-answer stop, budget honesty, "
          "typed provider failures, tested-scope negatives, settlement-asset identity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
