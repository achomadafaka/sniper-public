"""Ink scenarios through the shared funded entry and reservation seams."""

import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as db
from sniper.services import memecoin_arming, memecoin_evm_leg
from sniper.settings import memecoin as cfg


@pytest.fixture
def ink_flow(monkeypatch, tmp_path):
    def run(case):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.ensure_memecoin_tables(conn)
        monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", case != "disabled")
        monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST",
                            set() if case == "admission_off" else {"57073"})
        if case != "capability_blocked":
            monkeypatch.setattr(cfg, "ink_capability_reason", lambda: None)
        monkeypatch.setattr(memecoin_arming, "in_quiet_hours", lambda: False)
        calls = {"approve_calls": 0, "sign_calls": 0, "send_calls": 0}

        def execute(_conn, _pending_id, _mint, _size, _now, quote_safety_fn=None,
                    presign_check_fn=None):
            if presign_check_fn is not None:
                reason = presign_check_fn()
                if reason:
                    return {"status": "unsafe", "reason": reason}
            if quote_safety_fn is not None:
                safe_quote, reason = quote_safety_fn({"buyAmount": "1"})
                if not safe_quote:
                    return {"status": "unsafe", "reason": reason}
            calls["sign_calls"] += 1
            calls["send_calls"] += 1
            return {"status": "submitted_unknown", "tx_hash": "0x" + "a" * 64}

        safe = case != "token_unsafe"
        native = 0.0 if case == "gas_shortage" else 1.0
        event = SimpleNamespace(mint="0x" + "b" * 40, source_user_id="ink-wallet",
                                network_id="57073", signal_source="copy",
                                source_block_time=1_790_700_050)
        result = memecoin_evm_leg.evm_real_entry(
            conn, event, route="evm:57073", day="2026-09-29",
            usdc_balance=300.0, native_balance=native,
            now_iso="2026-09-29T09:00:00Z", trade_size=10.0,
            token_safety_fn=lambda _mint, bought_amount_atomic=None, usd=None, block_tag=None: (
                safe, "ok" if safe else "unsafe_token"),
            execute_fn=execute, kill_path=str(tmp_path / "kill"),
            head_fn=lambda: (57_200_010, 1_790_700_100),       # a head newer than the source
        )
        return {**calls, "result": result, "pending_rows": conn.execute(
            "SELECT count(*) FROM memecoin_pending_entries WHERE network_id='57073'").fetchone()[0]}

    return run


# ------------------------------------------------------------------------------------------------
# A scripted Ink chain at the JSON-RPC boundary (Beta, 2026-09-29, plan Task 5/6/8 harness).
#
# The REAL executor (`build_real_evm_executor`) runs against it: every sign, send, receipt and
# accounting read goes through the production seams, and only the network answers are scripted.
# The fake is a chain, not an answer key: it mines what was sent, moves balances by the swap it
# executed, and serves receipts/blocks/transactions that link (or, when a scenario says so, do not).
# ------------------------------------------------------------------------------------------------
import collections
import json

import rlp
from eth_account import Account
from eth_utils import keccak

from ink_constants import INK, INK_POOL, INK_ROUTER, INK_TOKEN, INK_WETH  # noqa: F401
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SEL_APPROVE = "0x095ea7b3"


def _word(value):
    return "0x" + format(int(value), "064x")


def _topic_addr(addr):
    return "0x" + "0" * 24 + str(addr).lower()[2:]


class FakeInk:
    """One Ink chain. `send`: a mode, or {kind: mode} with kind approve | swap; modes accept |
    timeout (lands, then the transport raises) | timeout_lost (raises, never lands) | reject.
    `receipts`: a mode or {kind: mode}; canonical | provisional (blockHash 0) | none | unlinked
    (the block at that number carries another hash). Tests may change `receipt_mode` between
    phases (a restart)."""

    def __init__(self, owner, *, tokens_per_weth=10 ** 8, weth=10 ** 18, send="accept",
                 receipts="canonical", l1_fee=None, block0=57_200_000, ts0=1_790_700_000,
                 operator_fee=0, same_block=False, serve_upto=None, latency_s=None):
        self.owner = owner.lower()
        self.tokens_per_weth = int(tokens_per_weth)   # exact integer price, both 18 decimals
        self.balances = collections.defaultdict(int)
        self.balances[(INK_WETH.lower(), self.owner)] = int(weth)
        self.allowance = collections.defaultdict(int)
        self.send_mode, self.receipt_mode = send, receipts
        self.l1_fee = l1_fee                     # dict of receipt fee fields, or None
        self.operator_fee = operator_fee         # the GasPriceOracle's answer; None = it reverts
        self.same_block = same_block             # mine every transaction into one block
        self.oracle_blocks = []                  # block tags the oracle was asked at
        self.serve_upto = serve_upto             # pinned reads above this block: header not found
        self.lagging = {"eth_call", "eth_getCode"}   # which pinned reads the lag applies to
        self.latency_s = latency_s or {}         # {method: seconds} slept before answering
        self.read_tags = []                      # (method, block tag) of every state read
        self.block, self.ts0, self.block0 = block0, ts0, block0
        self.base_nonce = 5
        self.sent, self.landed, self.mined = [], {}, {}
        self.quotes = {}                         # (sell, buy, amount) -> buy amount
        self.calls = collections.Counter()
        self.crash = None                        # before_send_<kind> | after_send_<kind> | ...

    # --- the venue the executor quotes (direction-aware; the chain executes what it quoted) ----
    def venue_quote(self, sell, buy, amount, *, slippage_bps=None):
        sell, buy, amount = str(sell).lower(), str(buy).lower(), int(amount)
        out = (amount * self.tokens_per_weth if sell == INK_WETH.lower()
               else amount * 97 // (100 * self.tokens_per_weth))
        self.quotes[(sell, buy, amount)] = out
        return {"sellAmount": str(amount), "buyAmount": str(out),
                "issues": {"allowance": {"spender": INK_ROUTER}},
                # The calldata names the route, so the chain executes exactly what was signed.
                "transaction": {"to": INK_ROUTER, "data": "0x5ae401dc" + _word(int(sell, 16))[2:]
                                + _word(int(buy, 16))[2:] + _word(amount)[2:],
                                "value": "0", "gas": "400000", "gasPrice": hex(1 << 20)}}

    # --- JSON-RPC --------------------------------------------------------------------------------
    def http(self, method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        name, params = req["method"], req.get("params") or []
        self.calls[name] += 1
        key = (name + ":latest" if name == "eth_getBlockByNumber" and params and params[0] == "latest"
               else name)
        if key in self.latency_s:              # e.g. "eth_getBlockByNumber:latest" = the head read
            import time as _t
            _t.sleep(self.latency_s[key])
        if name in ("eth_call", "eth_getCode"):
            tag = params[1] if len(params) > 1 else "latest"
            self.read_tags.append((name, tag))
            if (self.serve_upto is not None and name in self.lagging
                    and isinstance(tag, str) and tag.startswith("0x")
                    and int(tag, 16) > self.serve_upto):
                return 200, {"error": {"code": -32000, "message": "header not found"}}, None
        if name == "eth_sendRawTransaction":
            return self._send(params[0])
        fn = getattr(self, "_" + name, None)
        if fn is None:
            return 200, {"error": {"code": -32601, "message": "unscripted " + name}}, None
        return 200, {"result": fn(*params)}, None

    def _eth_getCode(self, _address, _tag="latest"):
        return "0x6080604052"

    def _eth_chainId(self):
        return hex(int(INK))

    def _eth_gasPrice(self):
        return hex(1 << 20)

    def _eth_getTransactionCount(self, _addr, tag="latest"):
        mined = sum(1 for h in self.landed if h in self.mined)
        return hex(self.base_nonce + (len(self.landed) if tag == "pending" else mined))

    def _eth_call(self, call, *rest):
        data, to = str(call.get("data") or "0x").lower(), str(call.get("to") or "").lower()
        if to == "0x420000000000000000000000000000000000000f" and data.startswith("0x275aedd2"):
            self.oracle_blocks.append(rest[0] if rest else None)
            if self.operator_fee is None:
                raise RuntimeError("oracle unavailable")
            return _word(self.operator_fee)
        if data.startswith("0x70a08231"):                       # balanceOf
            return _word(self.balances[(to, "0x" + data[-40:])])
        if data.startswith("0xdd62ed3e"):                       # allowance(owner, spender)
            return _word(self.allowance[(to, "0x" + data[34:74], "0x" + data[-40:])])
        if data.startswith("0x313ce567"):
            return _word(18)
        return "0x"

    def _send(self, raw):
        tx_hash = "0x" + keccak(bytes.fromhex(raw[2:])).hex()
        tx = self._decode(raw)
        kind = "approve" if tx["data"].startswith(SEL_APPROVE) else "swap"
        mode = self.send_mode.get(kind, "accept") if isinstance(self.send_mode, dict) else self.send_mode
        if self.crash == "before_send_" + kind:
            self.crash = None
            raise SystemExit("process died with the signed identity persisted, before the send")
        self.sent.append(raw)                    # it reached the wire
        if mode == "reject":
            return 200, {"error": {"code": -32000, "message": "nonce too low"}}, None
        if mode != "timeout_lost":
            self.landed[tx_hash] = tx
        if self.crash == "after_send_" + kind:
            self.crash = None
            raise SystemExit("process died after the send landed")
        if mode in ("timeout", "timeout_lost"):
            raise TimeoutError("send timed out")
        return 200, {"result": tx_hash}, None

    @staticmethod
    def _decode(raw):
        nonce, gas_price, gas, to, value, data, _v, _r, _s = rlp.decode(bytes.fromhex(raw[2:]))
        return {"nonce": int.from_bytes(nonce, "big"), "to": "0x" + to.hex(),
                "value": int.from_bytes(value, "big"), "data": "0x" + data.hex(),
                "gas_price": int.from_bytes(gas_price, "big"), "gas": int.from_bytes(gas, "big")}

    def block_hash(self, number):
        return "0x" + keccak(b"ink-block-%d" % number).hex()

    def _mine(self, tx_hash):
        if tx_hash in self.mined:
            return self.mined[tx_hash]
        tx = self.landed[tx_hash]
        if not (self.same_block and self.mined):
            self.block += 1
        number, logs = self.block, []
        if tx["data"].startswith(SEL_APPROVE):
            spender = "0x" + tx["data"][34:74]
            self.allowance[(tx["to"], self.owner, spender)] = int(tx["data"][74:138], 16)
        elif tx["to"] == INK_ROUTER.lower():
            body = tx["data"][10:]
            sell, buy = "0x" + body[24:64], "0x" + body[88:128]
            amount = int(body[128:192], 16)
            out = self.quotes[(sell, buy, amount)]
            self.balances[(sell, self.owner)] -= amount
            self.balances[(buy, self.owner)] += out
            logs = [(sell, self.owner, INK_POOL, amount), (buy, INK_POOL, self.owner, out)]
        receipt = {
            "transactionHash": tx_hash, "blockHash": self.block_hash(number),
            "blockNumber": hex(number), "from": self.owner, "to": tx["to"], "status": "0x1",
            "gasUsed": hex(150_000 + 1_000 * tx["nonce"]), "effectiveGasPrice": hex(tx["gas_price"]),
            "logs": [{"address": token, "topics": [TRANSFER, _topic_addr(src), _topic_addr(dst)],
                      "data": _word(amt), "logIndex": hex(i), "transactionHash": tx_hash,
                      "blockHash": self.block_hash(number), "blockNumber": hex(number)}
                     for i, (token, src, dst, amt) in enumerate(logs)]}
        receipt.update(self.l1_fee or {})
        self.mined[tx_hash] = receipt
        return receipt

    def _mode(self, modes, tx):
        kind = "approve" if tx["data"].startswith(SEL_APPROVE) else "swap"
        return modes.get(kind, "canonical") if isinstance(modes, dict) else modes

    def _eth_getTransactionReceipt(self, tx_hash):
        if tx_hash not in self.landed:
            return None
        mode = self._mode(self.receipt_mode, self.landed[tx_hash])
        if mode == "none":
            return None
        receipt = dict(self._mine(tx_hash))
        if mode == "provisional":
            receipt["blockHash"] = "0x" + "00" * 32
        return receipt

    def _eth_getBlockByNumber(self, number, _full=False):
        n = self.block if number in ("latest", "pending") else int(number, 16)
        block_hash = self.block_hash(n)
        if self.receipt_mode == "unlinked" and n > self.block0:     # whole-chain reorg only
            block_hash = "0x" + keccak(b"reorged-%d" % n).hex()
        return {"number": hex(n), "hash": block_hash, "timestamp": hex(self.ts0 + 2 * (n - self.block0))}

    def _eth_getTransactionByHash(self, tx_hash):
        if tx_hash not in self.landed:
            return None
        tx, receipt = self.landed[tx_hash], self.mined.get(tx_hash)
        return {"hash": tx_hash, "from": self.owner, "to": tx["to"], "value": hex(tx["value"]),
                "input": tx["data"], "nonce": hex(tx["nonce"]),
                "blockHash": receipt["blockHash"] if receipt else None,
                "blockNumber": receipt["blockNumber"] if receipt else None}


class _CountingAccount:
    """The real signer, counted. Every EVM signature goes through `acct.sign_transaction`. The
    same instance serves a rebuilt executor, so counts span a restart."""

    def __init__(self, acct):
        self._acct, self.sign_calls, self.hashes, self.swap_hashes = acct, 0, [], []
        self.chain = None

    @property
    def address(self):
        return self._acct.address

    def sign_transaction(self, tx):
        swap = str(tx.get("to") or "").lower() == INK_ROUTER.lower()
        if swap and self.chain is not None and self.chain.crash == "before_sign_swap":
            self.chain.crash = None
            raise SystemExit("process died before the swap was signed")
        self.sign_calls += 1
        signed = self._acct.sign_transaction(tx)
        tx_hash = "0x" + signed.hash.hex().removeprefix("0x")
        self.hashes.append(tx_hash)
        if swap:
            self.swap_hashes.append(tx_hash)
        return signed


class _NoSleep:
    """`evm_real_seams._time` with sleep removed, local to that module (never the global clock)."""

    def __getattr__(self, name):
        import time
        return getattr(time, name)

    @staticmethod
    def sleep(*_a, **_k):
        return None


@pytest.fixture
def ink_real(monkeypatch):
    """Build the REAL Ink executor over a FakeInk chain: returns make(**chain_kwargs) ->
    SimpleNamespace(chain, acct, ex, conn, reserve()). The capability gate is opened for the test
    only; the pinned manifest still says blocked."""
    from sniper.execution import evm_real_seams as S

    monkeypatch.setattr(S, "_time", _NoSleep())
    confirm = S.eth_confirm                   # imports time itself; give it a no-op sleep

    def _fast_confirm(*args, sleep=None, **kwargs):
        return confirm(*args, sleep=lambda *_a: None, **kwargs)
    monkeypatch.setattr(S, "eth_confirm", _fast_confirm)
    monkeypatch.setattr(cfg, "ink_capability_reason", lambda *a, **k: None)

    def make(**chain_kwargs):
        acct = _CountingAccount(Account.create())
        chain = FakeInk(acct.address, **chain_kwargs)
        acct.chain = chain
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.ensure_memecoin_tables(conn)

        def build():
            """A fresh executor (a restarted process) over the same chain, ledger and key."""
            return S.build_real_evm_executor(
                acct=acct, chain_id=INK, rpc_url="https://ink.test", http=chain.http,
                venue_quote_fn=chain.venue_quote,
                usdc_fn=lambda: chain.balances[(INK_WETH.lower(), chain.owner)] / 1e18 * 2000.0,
                token_fn=lambda mint: chain.balances[(str(mint).lower(), chain.owner)] / 1e18,
                price_fn=lambda: 2000.0, decimals_fn=lambda _token: 18)
        ex = build()

        def reserve(notional=10.0):
            return db.reserve_pending(conn, mint=INK_TOKEN, source_user_id="ink-wallet",
                                      strategy="alpha_b", notional=notional,
                                      reserved_at="2026-09-29T09:00:00Z", network_id=INK)

        def recover(ex, now_iso="2026-09-29T09:30:00Z"):
            """The runner's restart recovery pass over every stale reservation."""
            from sniper.services.memecoin_real_runner import recover_stale_reservations
            return recover_stale_reservations(
                conn, confirm_for=lambda _net, sig: ex["_confirm_once"](sig), now_iso=now_iso,
                max_age_s=60,
                confirmed_entry_for=lambda c, row, now: ex["recover_confirmed_entry_fn"](c, row, now),
                confirmed_nonce_for=lambda _net: ex["confirmed_nonce_fn"](),
                pending_nonce_for=lambda _net: ex["pending_nonce_fn"](),
                token_balance_for=lambda _net, mint: chain.balances[(str(mint).lower(), chain.owner)])

        def ledger():
            one = lambda sql: conn.execute(sql).fetchone()[0]
            return {"positions": one("SELECT COUNT(*) FROM memecoin_positions WHERE network_id='57073'"),
                    "reserved": one("SELECT COUNT(*) FROM memecoin_pending_entries "
                                    "WHERE network_id='57073' AND status='reserved'"),
                    "entry_fills": one("SELECT COUNT(*) FROM memecoin_fills WHERE kind='entry'")}

        return SimpleNamespace(chain=chain, acct=acct, ex=ex, conn=conn, reserve=reserve,
                               build=build, recover=recover, ledger=ledger)

    return make
