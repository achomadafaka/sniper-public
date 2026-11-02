"""V2 buy tax, measured in SHADOW on a research host after the attempt (spec 2026-10-07 EVM entry
safety, section 2; rebuilt after Gamma's HOLD on 6578cf227).

A shadow leg never calls a funded seam (Alpha): not the quote ladder, not the price seam, not the
funded read pool, and not a research key a funded feeder falls back to (Gamma HOLD on 15d338664:
the RH fast lane's safety cache uses 4663's research QuickNode key). On 4663 it reads only the
research dRPC account, through that account's shared cross-process pacer. The safety PASS only starts a daemon thread (no I/O on the fill path). The thread
waits DELAY_S, long past the attempt's booking or refusal, then reads ONLY the chain's research host:
the router's getAmountsOut for what a buy of PROBE_WEI is quoted at, and one eth_simulateV1 from a
synthetic address for what that buy actually delivers. buy_tax = 1 - received / quoted. One WARNING
line per measurement; an unmeasurable buy logs tax_bps=unknown, never 0. It never gates.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DELAY_S = 60.0
PROBE_WEI = 10 ** 16                 # 0.01 of the chain's wrapped native
TAKER = "0x000000000000000000000000000000000000b0b5"
_RESEARCH_DEFAULTS = {"8453": "https://base.gateway.tenderly.co"}   # rule 573d8817; none on 56


def research_url(chain):
    """The chain's research read host, never a funded one or a funded feeder's: 4663's research dRPC
    key (paced), 8453's public research gateway; None skips the chain (56: no research reads)."""
    chain = str(chain)
    if chain == "4663":
        return os.getenv("MEMECOIN_RESEARCH_DRPC_RPC_URL_4663", "").strip() or None
    return _RESEARCH_DEFAULTS.get(chain)


def _post(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def _paced_post(url, method, params):
    """`_post` behind the dRPC account's shared pacer (a keyed dRPC URL only; others go alone)."""
    from sniper.services.memecoin_research_pacer import drpc_paced_json
    return drpc_paced_json(lambda target, method, params: _post(target, method, params))(
        url, method=method, params=params)


def schedule(chain, path, router=None):
    """Start the measurement thread; None (and nothing started) without a research host. `router`
    is the sale route's own router (Gamma LOW): a fork router quotes on its own factory."""
    if not research_url(chain):
        return None
    thread = threading.Thread(target=measure, args=(str(chain), list(path)),
                              kwargs={"router": router}, name="evm-buy-tax-shadow", daemon=True)
    thread.start()
    return thread


def _word(row):
    data = str((row or {}).get("returnData") or "")
    return int(data, 16) if data.startswith("0x") and len(data) >= 66 else None


def measure(chain, path, *, router=None, post=None, sleep=time.sleep):
    sleep(DELAY_S)
    started = time.perf_counter()
    url = research_url(chain)
    post = post or _paced_post
    quoted = got = None
    why = None
    mint = str(path[0]).lower() if path else None
    try:
        from eth_abi import decode, encode
        from sniper.execution.evm_wallet import CHAIN_QUOTE, SEL_BALANCE_OF
        from sniper.settings.memecoin import MEMECOIN_EVM_V2_ROUTER
        weth = str((CHAIN_QUOTE.get(chain) or {}).get("token") or "").lower()
        from sniper.services.memecoin_research_pacer import drpc_paused_s
        router = str(router or (MEMECOIN_EVM_V2_ROUTER or {}).get(chain) or "").lower() or None
        if not url:
            why = "no_research_host"
        elif drpc_paused_s(url) > 0:
            why = "research_paused"                  # the account is limited: defer to its owners
        elif not weth or not router or len(path) < 2 or str(path[-1]).lower() != weth:
            why = "not_a_quote_token_path"
        else:
            buy_path = [str(p).lower() for p in reversed(path)]
            out = post(url, "eth_call", [{"to": router, "data": "0xd06ca61f" + encode(
                ["uint256", "address[]"], [PROBE_WEI, buy_path]).hex()}, "latest"])
            if "result" not in (out or {}):
                why = "quote_failed"
            else:
                quoted = int(decode(["uint256[]"], bytes.fromhex(out["result"][2:]))[0][-1])

                def call(to, data, value=0):
                    return {"from": TAKER, "to": to, "input": data, "value": hex(value)}
                balance = call(mint, "0x" + SEL_BALANCE_OF + TAKER[2:].rjust(64, "0"))
                swap = "0x5c11d795" + encode(["uint256", "uint256", "address[]", "address", "uint256"],
                                             [PROBE_WEI, 0, buy_path, TAKER, 2 ** 40]).hex()
                calls = [balance, call(weth, "0xd0e30db0", PROBE_WEI),
                         call(weth, "0x095ea7b3" + router[2:].lower().rjust(64, "0") + "f" * 64),
                         call(router, swap), balance]
                answer = post(url, "eth_simulateV1", [{"blockStateCalls": [{
                    "stateOverrides": {TAKER: {"balance": hex(10 ** 20)}}, "calls": calls}],
                    "validation": False}, "latest"])
                rows = ((answer or {}).get("result") or [{}])[0].get("calls")
                error = (answer or {}).get("error")
                if error:
                    code = error.get("code") if isinstance(error, dict) else None
                    why = "simulate_unsupported" if code == -32601 else "simulate_error"
                elif not isinstance(rows, list) or len(rows) != len(calls):
                    why = "simulate_error"
                elif any(int(str(r.get("status") or "0"), 16) != 1 for r in rows):
                    why = "buy_reverted"
                else:
                    start, end = _word(rows[0]), _word(rows[-1])
                    got = None if start is None or end is None else end - start
                    if got is None:
                        why = "unreadable_balance"
    except Exception as exc:                         # a shadow never raises into anything
        why = "error:%s" % type(exc).__name__
    ms = (time.perf_counter() - started) * 1000.0
    tax = ("%.1f" % ((1.0 - got / quoted) * 10000.0)
           if why is None and quoted and got is not None else "unknown")
    logger.warning("evm.buy_tax_shadow chain=%s mint=%s tax_bps=%s quoted=%s received=%s ms=%.1f "
                   "host=%s why=%s", chain, mint, tax, quoted, got, ms,
                   urlsplit(url).hostname if url else None, why or "ok")
