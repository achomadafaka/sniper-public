"""Capped exact PumpSwap pool read for the Office near-pass sheet (operator 2026-10-29 via Alpha, paid Helius key a56bd061,
never the funded Solana lane). One read = pumpswap_sell.read_pool_state (reached through sniper.services.memecoin_desk_ps_pool_read: the Office may not import sniper.execution): base/quote vault reserves and the
Pool account's virtual_quote at one confirmed context slot (2 getMultipleAccounts, 3 when the tape pool is a vault key).

Fail closed: a read past the monthly credit cap, an unreadable counter, a transport error, a pool that is not this mint
against WSOL, or no usable state all return {"unknown": reason}; nothing here invents a price. The counter is one small
JSON file {"month","credits"}; a new UTC month resets it, so it never grows."""
from __future__ import annotations

import base64
import json
import os
import struct
import threading
import time
import urllib.request
from datetime import datetime, timezone

import base58

WSOL_MINT = "So11111111111111111111111111111111111111112"
MAX_CALLS_PER_READ = 3
# Helius standard RPC is believed to bill ~1 credit/call (unverified); 5 over-counts on purpose so the 1M-credit Office cap
# can never eat the 2M funded-proof reserve or the 5M backfill cap on the shared 10M/month key.
CREDITS_PER_CALL = 5
CAP_CREDITS = 1_000_000
TIMEOUT_S = 4.0


MAX_CALLS_PER_LP_READ = 4
LP_TTL_S = 300.0
LP_CACHE_MAX = 512
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
_POOL_KEYS_AT = 11      # discriminator 8, bump 1, index 2; then creator, base mint, quote mint, lp mint, two vaults, lp_supply u64


def _read_lp(pool, mint, rpc):
    """Pool account -> LP mint -> supply; the largest holder only when supply is not zero. The pool must be this mint against WSOL."""
    account = rpc("getAccountInfo", [pool, {"encoding": "base64", "commitment": "confirmed"}])
    value = account.get("value")
    if not value or value.get("owner") != PUMP_AMM_PROGRAM:
        return {"unknown": "lp_pool_not_pumpswap"}
    raw = base64.b64decode(value["data"][0])
    if len(raw) < _POOL_KEYS_AT + 32 * 6 + 8:
        return {"unknown": "lp_pool_short"}
    key = lambda n: base58.b58encode(raw[_POOL_KEYS_AT + 32 * n:_POOL_KEYS_AT + 32 * (n + 1)]).decode()
    if key(1) != mint or key(2) != WSOL_MINT:
        return {"unknown": "lp_pool_mismatch"}
    lp_mint = key(3)
    supply = rpc("getTokenSupply", [lp_mint, {"commitment": "confirmed"}])
    out = {"lp_mint": lp_mint, "lp_supply_recorded_atomic": struct.unpack_from("<Q", raw, _POOL_KEYS_AT + 32 * 6)[0],
           "lp_mint_supply_atomic": int(supply["value"]["amount"]), "slot": supply["context"]["slot"], "largest_holder": None}
    if out["lp_mint_supply_atomic"]:
        largest = rpc("getTokenLargestAccounts", [lp_mint, {"commitment": "confirmed"}])["value"]
        if largest:
            info = rpc("getAccountInfo", [largest[0]["address"], {"encoding": "jsonParsed", "commitment": "confirmed"}])["value"]
            out["largest_holder"] = {"token_account": largest[0]["address"], "amount_atomic": int(largest[0]["amount"]),
                                     "owner": info["data"]["parsed"]["info"]["owner"] if info else None}
    return out


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.load(resp)


class PoolStateReader:
    def __init__(self, url, counter_path, *, cap_credits=CAP_CREDITS, credits_per_call=CREDITS_PER_CALL, clock=time.time,
                 post=_post, read_pool_state=None):
        self.url, self.path = url, counter_path
        self.cap, self.per_call, self.clock, self.post = cap_credits, credits_per_call, clock, post
        self._read = read_pool_state
        self._lock = threading.Lock()
        self._lp_cache = {}

    def _month(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m")

    def _load(self):
        """Credits used this month; None when the file exists but is unreadable (fail closed, never reset)."""
        month = self._month()
        try:
            with open(self.path) as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict) or type(doc.get("credits")) is not int or doc["credits"] < 0:
            return None
        return doc["credits"] if doc.get("month") == month else 0

    def _store(self, credits):
        tmp = "%s.tmp" % self.path
        with open(tmp, "w") as fh:
            json.dump({"month": self._month(), "credits": credits}, fh)
        os.replace(tmp, self.path)

    def read_lp(self, pool, mint):
        """LP custody for one PumpSwap pool, charged to the same cap; cached LP_TTL_S per pool so re-reads add no calls. {"unknown": reason} on any failure."""
        now = self.clock()
        with self._lock:
            hit = self._lp_cache.get((pool, mint))
            if hit is not None and 0 <= now - hit["read_at"] <= LP_TTL_S:
                return dict(hit)
            used = self._load()
            if used is None:
                return {"unknown": "rpc_counter_unreadable"}
            if used + MAX_CALLS_PER_LP_READ * self.per_call > self.cap:
                return {"unknown": "rpc_cap"}
            calls = [0]

            def rpc(method, params):
                calls[0] += 1
                doc = self.post(self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                if not isinstance(doc, dict) or "result" not in doc:
                    raise ValueError("rpc_error")
                return doc["result"]
            try:
                try:
                    got = _read_lp(pool, mint, rpc)
                finally:
                    self._store(used + calls[0] * self.per_call)
            except Exception as exc:
                return {"unknown": "rpc_error:%s" % type(exc).__name__}
            if "unknown" not in got:
                got["read_at"] = now
                if len(self._lp_cache) >= LP_CACHE_MAX:
                    self._lp_cache.clear()
                self._lp_cache[(pool, mint)] = dict(got)
            return got

    def read(self, pool, mint):
        """{base_reserve, quote_reserve, virtual_quote, slot, read_at} or {"unknown": reason}."""
        from sniper.services import memecoin_desk_ps_pool_read
        reader = self._read or memecoin_desk_ps_pool_read.read_pool_state
        with self._lock:
            used = self._load()
            if used is None:
                return {"unknown": "rpc_counter_unreadable"}
            if used + MAX_CALLS_PER_READ * self.per_call > self.cap:
                return {"unknown": "rpc_cap"}
            calls = [0]

            def rpc(method, params):
                calls[0] += 1
                doc = self.post(self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                if not isinstance(doc, dict) or "result" not in doc:
                    raise ValueError("rpc_error")
                return doc["result"]
            try:
                try:
                    state = reader(pool, rpc=rpc)
                finally:
                    self._store(used + calls[0] * self.per_call)     # failed and partial reads are charged too
            except Exception as exc:
                return {"unknown": "rpc_error:%s" % type(exc).__name__}
        if not isinstance(state, dict):
            return {"unknown": "rpc_pool_unusable"}
        facts = state.get("facts") or {}
        if facts.get("base_mint") != mint or facts.get("quote_mint") != WSOL_MINT:
            return {"unknown": "rpc_pool_mismatch"}
        return {"base_reserve": state["base_reserve"], "quote_reserve": state["quote_reserve"],
                "virtual_quote": facts["virtual_quote"], "slot": state["slot"], "read_at": self.clock()}
