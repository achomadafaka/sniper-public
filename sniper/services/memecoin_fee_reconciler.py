"""Off-path fee reconciler: the network fee of every funded fill, per transaction, on every chain.

Built 2026-10-21 (Alpha; Gamma's 7-day read): fees were recorded only on native-SOL-funded Solana entries,
fast-lane EVM receipt entries and Ink/mainnet. This reads the CONFIRMED transaction of fills already
booked and writes one write-once row per tx (memecoin_sol_tx_fees / memecoin_evm_tx_fees). It never
touches a fill, exit or sizing path. Unknown is never 0: a receipt that cannot be read records nothing
(pending) or an 'unknown:<why>' row.

Hosts are research hosts only, never a funded lane (operator rules; Alpha 2026-10-21):
  solana  first MEMECOIN_RESEARCH_HELIUS_RPC_URLS entry, refused if its key is a funded key  <= 2 calls/s
  8453    MEMECOIN_RESEARCH_DRPC_RPC_URL_8453 (never mainnet.base.org); OP-stack fee         <= 1 call/s
  56      https://bsc.meowrpc.com receipts (operator 2026-10-14)              <= 1/s, <= 150 calls/run
  4663    MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663_2: the plain _4663 key backs the funded RH
          fast-safety cache (brain trap b37aa352)                                         <= 1 call/s
Chains 1 and 57073 keep their existing funded-runner reconcile (evm_op_fees), so they are not here.
Every chain's pass stops on its first transport error, HTTP error or 429. Calls are spent only when
MEMECOIN_FEE_RECONCILER_ENABLED=1, and the operator approves the first backfill (research quota).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

MEOWRPC_56 = "https://bsc.meowrpc.com"
HOSTS = {"solana": ("MEMECOIN_RESEARCH_HELIUS_RPC_URLS", 0.5, None),
         "8453": ("MEMECOIN_RESEARCH_DRPC_RPC_URL_8453", 1.0, None),
         "56": (None, 1.0, 150),
         "4663": ("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663_2", 1.0, None)}
FUNDED_ENV = {"8453": "MEMECOIN_EVM_RPC_URL_8453", "56": "MEMECOIN_EVM_RPC_URL_56",
              "4663": "MEMECOIN_EVM_RPC_URL_4663"}


class Stop(Exception):
    # The chain's pass stops: transport error, HTTP error, 429, or the per-run call cap.
    pass


def host_for(chain, env):
    """The research URL for `chain`, or ValueError naming why not (never a funded host)."""
    from urllib.parse import urlsplit
    if chain not in HOSTS:
        raise ValueError("chain %s is not reconciled here" % chain)
    name = HOSTS[chain][0]
    if chain == "solana":
        from sniper.services.memecoin_research_rpc import funded_overlap
        urls = [u.strip() for u in (env.get(name) or "").split(",") if u.strip()]
        if not urls:
            raise ValueError("%s is unset" % name)
        if funded_overlap(urls[:1], env):
            raise ValueError("the Solana research key is also a funded key; refusing")
        return urls[0]
    url = MEOWRPC_56 if chain == "56" else (env.get(name) or "").strip()
    if not url:
        raise ValueError("%s is unset" % name)
    if chain == "8453" and urlsplit(url).hostname == "mainnet.base.org":
        raise ValueError("mainnet.base.org is the funded 8453 pool; refusing")
    funded = (env.get(FUNDED_ENV[chain]) or "").strip()
    if funded and urlsplit(funded).hostname == urlsplit(url).hostname:
        raise ValueError("chain %s research host equals the funded host; refusing" % chain)
    return url


class Paced:
    """A JSON-RPC caller with a minimum interval, a per-run cap, and Stop on any failure. A keyed dRPC
    URL (8453) also goes through the dRPC account's shared pacer, which every research reader of that
    account uses; a limit answer pauses them all (memecoin_research_pacer)."""

    def __init__(self, url, min_interval_s, cap=None, *, post=None, clock=time.monotonic, sleep=time.sleep):
        self.url, self.gap, self.cap, self.calls = url, float(min_interval_s), cap, 0
        self.post, self.clock, self.sleep, self.last = post, clock, sleep, None

    def __call__(self, method, params):
        if self.cap is not None and self.calls >= self.cap:
            raise Stop("call_cap")
        if self.last is not None:
            wait = self.gap - (self.clock() - self.last)
            if wait > 0:
                self.sleep(wait)
        self.last = self.clock()
        self.calls += 1
        from sniper.services.memecoin_research_pacer import drpc_paced_json
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        try:
            reply = drpc_paced_json(self._post)(self.url, body=body)
        except urllib.error.HTTPError as exc:
            raise Stop("http_%d" % exc.code) from None
        except (TimeoutError, OSError, ValueError) as exc:
            raise Stop(type(exc).__name__) from None
        if not isinstance(reply, dict) or "error" in reply:
            err = reply.get("error") if isinstance(reply, dict) else None
            raise Stop("rpc_429" if isinstance(err, dict) and err.get("code") == 429 else "rpc_error")
        return reply.get("result")

    def _post(self, url, *, body):
        if self.post is not None:
            return self.post(url, body)
        req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())


def tip_accounts():
    from sniper.execution.solana_sender_ab import NOZOMI_TIP_ACCOUNTS, TIP_ACCOUNTS
    return frozenset(TIP_ACCOUNTS) | frozenset(NOZOMI_TIP_ACCOUNTS)


def solana_fee(rpc, sig):
    """{fee_lamports, tip_lamports, slot, status} from the confirmed transaction, or None while not
    found. Tips are SOL transfers into Sender/Nozomi tip accounts outside meta.fee, summed over static
    and loaded (ALT) keys as in solana_exit_grid_study.native_cost_lamports. Either part unreadable =
    an unknown row, never 0."""
    tx = rpc("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                      "maxSupportedTransactionVersion": 1}])
    if tx is None:
        return None
    meta = tx.get("meta") if isinstance(tx, dict) else None
    fee = meta.get("fee") if isinstance(meta, dict) else None
    slot = tx.get("slot") if isinstance(tx, dict) else None
    if not (type(fee) is int and fee >= 0):
        return {"fee_lamports": None, "tip_lamports": None, "slot": slot, "status": "unknown:fee"}
    tips = tip_accounts()
    try:
        loaded = meta.get("loadedAddresses") or {}
        keys = list(tx["transaction"]["message"]["accountKeys"]) + list(loaded.get("writable") or []) + list(
            loaded.get("readonly") or [])
        pre, post = meta["preBalances"], meta["postBalances"]
        if len(pre) != len(keys) or len(post) != len(keys):
            raise ValueError("balance/key length mismatch")
        tip = sum(max(0, int(b) - int(a)) for k, a, b in zip(keys, pre, post) if k in tips)
    except (KeyError, TypeError, ValueError, AttributeError):
        return {"fee_lamports": fee, "tip_lamports": None, "slot": slot, "status": "unknown:tip"}
    return {"fee_lamports": fee, "tip_lamports": tip, "slot": slot, "status": "complete"}


def evm_fee(rpc, chain, tx_hash):
    """memecoin_evm_tx_fees shape from the receipt, or None while there is no receipt. 8453 adds the
    OP-stack l1Fee and the GasPriceOracle operator fee at the tx's block; 56/4663 pay gasUsed x price."""
    from sniper.execution import evm_op_fees as F
    receipt = rpc("eth_getTransactionReceipt", [tx_hash])
    if not isinstance(receipt, dict):
        return None
    block = F._int(receipt.get("blockNumber"))
    if chain == "8453":
        operator = None
        if F._int(receipt.get("gasUsed")) is not None and block is not None:
            answer = rpc("eth_call", [{"to": F.GAS_PRICE_ORACLE,
                                       "data": F.SEL_GET_OPERATOR_FEE + format(F._int(receipt["gasUsed"]), "064x")},
                                      hex(block)])
            operator = int(answer, 16) if isinstance(answer, str) and answer.startswith("0x") and len(answer) == 66 else None
        fee = F.tx_fee(receipt, operator_wei=operator)
    else:
        fee = F.l1_tx_fee(receipt)
    return {**fee, "block_number": block}


def run_pass(conn, chain, rpc, *, limit=20, now_iso=None):
    """Fees for up to `limit` backlog txs on `chain`. Returns {recorded, unknown, pending, stopped}."""
    from sniper.persistence import db_memecoin as m
    out = {"recorded": 0, "unknown": 0, "pending": 0, "stopped": None}
    backlog = (m.sol_fee_backlog(conn, limit=limit) if chain == "solana"
               else m.evm_fee_backlog(conn, chain, limit=limit))
    for tx, kind in backlog:
        try:
            fee = solana_fee(rpc, tx) if chain == "solana" else evm_fee(rpc, chain, tx)
        except Stop as exc:
            out["stopped"] = str(exc)
            break
        if fee is None:
            out["pending"] += 1
            continue
        if chain == "solana":
            m.record_sol_tx_fee(conn, tx_sig=tx, kind=kind, fee=fee, recorded_at=now_iso)
        else:
            m.record_evm_tx_fee(conn, network_id=chain, tx_hash=tx, kind=kind, fee=fee, recorded_at=now_iso)
        out["recorded" if fee["status"] == "complete" else "unknown"] += 1
    return out
