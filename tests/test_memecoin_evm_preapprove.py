"""EVM token pre-approval for Permit2 at entry (spec 2026-09-30-memecoin-4663-approve-at-entry-design.md).

After a V4 entry is booked, a daemon thread approves the token for Permit2 through the SAME
executor instance the exits use, so the exit's approve stage (p50 310 ms on 4663) is off the sell.
The checks below are the spec's six, minus check 5 (an exit whose allowance covers sell_atomic sends
no approval), which is today's behaviour and already pinned by
tests/test_memecoin_evm_exit_executor.py::test_existing_token_allowance_skips_redundant_approval.
"""
import json
import sqlite3
import threading
import time

import pytest
from eth_account import Account

import sniper.execution.evm_real_seams as S
from sniper.execution.evm_v4_quote import V4
from sniper.persistence import db_memecoin as m

CHAIN = "8453"
PERMIT2 = V4[CHAIN]["permit2"].lower()
TOKEN = "0x" + "ab" * 20
SEL_BALANCE_OF = "0x70a08231"
SEL_ALLOWANCE = "0xdd62ed3e"
V4_ROUTE = {"protocol": "uniswap_v4", "pool_id": "0x" + "11" * 32}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 1.0)
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", CHAIN)
    monkeypatch.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", CHAIN)
    return monkeypatch


def _db(tmp_path):
    path = str(tmp_path / "ledger.db")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return path, c


def _open(c, *, route=V4_ROUTE, claim=None):
    pid = m.open_position(
        c, source_user_id="u", source_handle="(real)", mint=TOKEN, ticker="", entry_price=1.0,
        entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
        opened_at="2026-09-30T00:00:00Z", strategy="alpha_b", execution_mode="real",
        network_id=CHAIN, evm_route_json=json.dumps(route) if route is not None else None)
    if claim:
        c.execute("UPDATE memecoin_positions SET close_claim_id=? WHERE id=?", (claim, pid))
        c.commit()
    return pid


def _http(*, nonce="0x5", balance=1000, allowance=0, send=None, on_send=None):
    """RPC stub. The node's pending count is STALE (never advances), which is exactly the case the
    nonce floor exists for."""
    seen = {"sends": [], "balance_reads": 0, "allowance_reads": 0}

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth = req["method"]
        if meth == "eth_getTransactionCount":
            return 200, {"result": nonce}, None
        if meth == "eth_gasPrice":
            return 200, {"result": "0x3b9aca00"}, None
        if meth == "eth_call":
            call = req["params"][0]
            data = str(call.get("data") or "")
            if str(call.get("to") or "").lower() == TOKEN and data.startswith(SEL_BALANCE_OF):
                seen["balance_reads"] += 1
                if balance is None:
                    return 200, {"error": {"message": "boom"}}, None
                return 200, {"result": "0x%064x" % balance}, None
            if str(call.get("to") or "").lower() == TOKEN and data.startswith(SEL_ALLOWANCE):
                seen["allowance_reads"] += 1
                if allowance is None:
                    return 200, {"error": {"message": "boom"}}, None
                return 200, {"result": "0x%064x" % allowance}, None
            return 200, {"result": "0x0"}, None
        if meth == "eth_sendRawTransaction":
            seen["sends"].append(req["params"][0])
            if on_send is not None:
                on_send()
            if send == "ambiguous":
                return 200, {"error": {"message": "gateway timeout, please retry"}}, None
            if send == "rejected":
                return 200, {"error": {"code": -32000, "message": "insufficient funds"}}, None
            return 200, {"result": "0x" + "cd" * 32}, None
        if meth == "eth_getTransactionReceipt":
            return 200, {"result": {"status": "0x1", "gasUsed": "0xb4a0",
                                    "effectiveGasPrice": "0x3b9aca00", "logs": []}}, None
        return 200, {"result": "0x0"}, None
    return http, seen


def _ex(http, chain=CHAIN):
    return S.build_real_evm_executor(acct=Account.create(), chain_id=chain, rpc_url="https://rpc",
                                     http=http)


def _rows(c):
    return [dict(r) for r in c.execute("SELECT * FROM memecoin_evm_preapprovals ORDER BY id")]


_SWAP = {"to": "0x" + "44" * 20, "data": "0x", "value": "0", "gas": "500000",
         "gasPrice": "1000000000"}


# --- check 2: the pre-approval and a following exit share ONE nonce floor ---

def test_preapproval_and_following_exit_share_one_nonce_floor(env, tmp_path):
    path, c = _db(tmp_path)
    pid = _open(c)
    http, seen = _http()
    ex = _ex(http)
    ex["preapprove_position_fn"](path, pid)
    assert len(seen["sends"]) == 1
    # The exit signs at approval-nonce+1 although the node still says 5.
    assert ex["_sign"](dict(_SWAP))[2] == 6
    [row] = _rows(c)
    assert row["send_status"] == "accepted"
    assert row["nonce"] == 5
    assert row["amount_atomic"] == "1000"                 # balanceOf, not the quoted quantity
    assert row["spender"] == PERMIT2 and row["token"] == TOKEN
    assert row["receipt_status"] == "confirmed"
    assert row["gas_used"] == 0xb4a0 and row["effective_gas_price_wei"] == 0x3b9aca00
    assert row["tx_hash"] and row["resolved_at"]


def test_approval_is_exact_balance_for_permit2(env, tmp_path):
    path, c = _db(tmp_path)
    pid = _open(c)
    http, seen = _http(balance=123456789012345678901234567)     # > 2**63: token atoms, not float
    _ex(http)["preapprove_position_fn"](path, pid)
    from eth_account._utils.legacy_transactions import Transaction
    tx = Transaction.from_bytes(bytes.fromhex(seen["sends"][0][2:]))
    assert tx.to.hex().lower().removeprefix("0x") == TOKEN[2:]
    assert tx.data.hex().removeprefix("0x") == S.encode_approve(PERMIT2, 123456789012345678901234567)[2:]
    assert _rows(c)[0]["amount_atomic"] == "123456789012345678901234567"


# --- check 3: an ambiguous pre-approval gives the nonce back to the node's count ---

def test_ambiguous_preapproval_lets_the_next_exit_read_the_node_nonce(env, tmp_path):
    path, c = _db(tmp_path)
    pid = _open(c)
    http, seen = _http(send="ambiguous")
    ex = _ex(http)
    ex["preapprove_position_fn"](path, pid)
    # Never sign an exit over a gap the approval might not fill: the node says 5, so 5.
    assert ex["_sign"](dict(_SWAP))[2] == 5
    assert _rows(c)[0]["send_status"] == "ambiguous"


def test_ambiguous_preapproval_never_rewinds_past_a_later_signer(env, tmp_path):
    """A signer that took nonce+1 while the approval's send was in flight has already built on it.
    Rewinding then would hand nonce 5 to the next signer AND leave 6 queued behind it."""
    path, c = _db(tmp_path)
    pid = _open(c)
    box = {}
    http, seen = _http(send="ambiguous",
                       on_send=lambda: box.setdefault("concurrent", box["ex"]["_sign"](dict(_SWAP))))
    ex = box["ex"] = _ex(http)
    ex["preapprove_position_fn"](path, pid)
    assert box["concurrent"][2] == 6
    assert ex["_sign"](dict(_SWAP))[2] == 7


def test_rejected_preapproval_releases_its_nonce(env, tmp_path):
    path, c = _db(tmp_path)
    pid = _open(c)
    http, seen = _http(send="rejected")
    ex = _ex(http)
    ex["preapprove_position_fn"](path, pid)
    assert ex["_sign"](dict(_SWAP))[2] == 5
    row = _rows(c)[0]
    assert row["send_status"] == "rejected" and row["receipt_status"] is None


# --- check 4: nothing is sent when it is not needed or not ours to send ---

@pytest.mark.parametrize("case, status", [
    ("allowance_covers", "skipped_allowance"),
    ("zero_balance", "skipped_balance_zero"),        # a lagging read, not a covered one
    ("non_v4_route", "skipped_route"),
    ("no_route", "skipped_route"),
    ("close_claimed", "skipped_closing"),
    ("balance_unreadable", "skipped_read"),
    ("allowance_unreadable", "skipped_read"),
])
def test_no_approval_is_sent_when_skipped(env, tmp_path, case, status):
    path, c = _db(tmp_path)
    route = {"non_v4_route": {"protocol": "uniswap_v3"}, "no_route": None}.get(case, V4_ROUTE)
    pid = _open(c, route=route, claim="claim-1" if case == "close_claimed" else None)
    http, seen = _http(
        allowance={"allowance_covers": 1000, "allowance_unreadable": None}.get(case, 0),
        balance={"zero_balance": 0, "balance_unreadable": None}.get(case, 1000))
    ex = _ex(http)
    ex["preapprove_position_fn"](path, pid)
    assert seen["sends"] == []
    assert ex["_sign"](dict(_SWAP))[2] == 5                  # no nonce was taken
    [row] = _rows(c)
    assert row["send_status"] == status and row["nonce"] is None


# --- check 6: only the chain's canonical Permit2 is ever approved ---

def test_a_spender_other_than_permit2_is_refused(env, tmp_path):
    path, c = _db(tmp_path)
    pid = _open(c)
    http, seen = _http()
    _ex(http)["preapprove_position_fn"](path, pid, spender="0x" + "de" * 20)
    assert seen["sends"] == [] and seen["balance_reads"] == 0
    assert _rows(c)[0]["send_status"] == "refused_spender"


# --- check 1: the entry never waits for the pre-approval ---

def _entry_quote_fn():
    swap_to = "0x" + "44" * 20
    entries = {}

    def venue_quote(sell, buy, amount, *, slippage_bps=None):
        pair = (str(sell).lower(), str(buy).lower())
        if pair[::-1] in entries:
            return {"sellAmount": str(amount),
                    "buyAmount": str(int(entries[pair[::-1]] * 0.97)),
                    "issues": {"allowance": None},
                    "transaction": {"to": swap_to, "data": "0x", "value": "0",
                                    "gas": "500000", "gasPrice": "1000000000"}}
        entries[pair] = int(amount)
        return {"sellAmount": str(amount), "buyAmount": "1",
                "issues": {"allowance": {"spender": swap_to}}, "route": dict(V4_ROUTE),
                "transaction": {"to": swap_to, "data": "0x", "value": "0",
                                "gas": "500000", "gasPrice": "1000000000"}}
    return venue_quote


def _seq(vals):
    it = {"i": 0}

    def f(*_a, **_k):
        v = vals[min(it["i"], len(vals) - 1)]
        it["i"] += 1
        return v
    return f


def _entry(env, tmp_path, http):
    from evm_receipt_fake import MISS, answer
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    path, c = _db(tmp_path)
    pid = m.reserve_pending(c, mint=TOKEN, source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="t", network_id=CHAIN)
    acct = Account.create()
    inner = http

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        hit = answer(req["method"], req.get("params") or [], owner=acct.address, mint=TOKEN,
                     quote=CHAIN_QUOTE[CHAIN]["token"], coin=5, paid=10 * 10 ** 18,
                     receipt_extra={"gasUsed": "0xb4a0", "effectiveGasPrice": "0x3b9aca00"})
        if hit is not MISS:                 # every chain books its entry from the receipt
            inner(method, url, headers=headers, body=body, timeout=timeout)   # its counters
            return 200, {"result": hit}, None
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    ex = S.build_real_evm_executor(acct=acct, chain_id=CHAIN,
                                   rpc_url="https://rpc.flashbots.net/fast" if CHAIN == "1" else "https://rpc",
                                   http=http, venue_quote_fn=_entry_quote_fn(),
                                   usdc_fn=_seq([100.0, 90.0]), token_fn=_seq([0.0, 5.0]))
    return path, c, ex, pid


@pytest.mark.parametrize("chain", ["8453", "1"])
@pytest.mark.parametrize("pipeline_chains", ["8453", "4663,56"])
def test_entry_returns_while_the_preapproval_is_blocked_after_the_booking_commit(env, tmp_path, chain, pipeline_chains):
    env.setattr(__import__(__name__), "CHAIN", chain)
    env.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", chain)
    env.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", pipeline_chains)
    env.setenv("MEMECOIN_EVM_TYPE2_CHAINS", "")
    env.setenv("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1_V4", "1000000")
    env.setattr(S, "native_usd_price", lambda *a, **kw: 1.0)
    started, release = threading.Event(), threading.Event()
    inner, _seen = _http()

    def http(method, url, headers=None, body=None, timeout=10):
        call = (json.loads(body).get("params") or [{}])[0]
        if (isinstance(call, dict) and str(call.get("to") or "").lower() == TOKEN
                and str(call.get("data") or "").startswith(SEL_BALANCE_OF)):
            started.set()
            release.wait(30)                 # the pre-approval's first RPC never answers in time
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    path, c, ex, pid = _entry(env, tmp_path, http)
    try:
        t0 = time.monotonic()
        r = ex["execute_fn"](c, pid, TOKEN, 10.0, "2026-09-30T00:00:01Z")
        took = time.monotonic() - t0
        assert r["status"] == "filled", str(r)
        assert took < 10.0, took
        # The thread reached its balance read, which it only does after it has read the booked
        # position on its OWN connection: the booking commit came first.
        assert started.wait(10)
        assert not release.is_set()
    finally:
        release.set()
    for _ in range(200):
        if _rows(c):
            break
        time.sleep(0.05)
    [row] = _rows(c)
    assert row["position_id"] == r["position_id"] and row["send_status"] == "accepted"


@pytest.mark.parametrize("knob, value", [
    ("MEMECOIN_EVM_PREAPPROVE_CHAINS", ""),
    ("MEMECOIN_EVM_PREAPPROVE_CHAINS", "4663"),
])
def test_no_preapproval_unless_the_chain_is_switched_on(env, tmp_path, knob, value):
    env.setenv(knob, value)
    http, seen = _http()
    path, c, ex, pid = _entry(env, tmp_path, http)
    r = ex["execute_fn"](c, pid, TOKEN, 10.0, "2026-09-30T00:00:01Z")
    assert r["status"] == "filled"
    time.sleep(0.2)
    assert seen["balance_reads"] == 0
    assert _rows(c) == []


def test_preapprove_chains_knob_is_empty_by_default_and_malformed_means_empty(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.delenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", raising=False)
    assert cfg.memecoin_evm_preapprove_chains() == frozenset()
    monkeypatch.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", "4663,base")
    assert cfg.memecoin_evm_preapprove_chains() == frozenset()
    monkeypatch.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", " 4663 ")
    assert cfg.memecoin_evm_preapprove_chains() == frozenset({"4663"})


# --- the ambiguous approval's re-offer: identical bytes, and never over a nonce given away ---

def _closure(fn, name):
    return fn.__closure__[fn.__code__.co_freevars.index(name)].cell_contents


class _TapLock:
    """The executor's nonce lock, with a hook that runs just before the first acquisition that
    follows a rewind (a release that moved the floor down by one)."""

    def __init__(self, state, on_rewind):
        self.real, self.state, self.on_rewind = state["lock"], state, on_rewind
        self.armed = self.fired = False

    def __enter__(self):
        if self.armed and not self.fired:
            self.fired = True
            self.on_rewind()
        self.real.acquire()
        self.before = self.state["floor"]
        return self

    def __exit__(self, *_exc):
        after = self.state["floor"]
        if self.before is not None and after is not None and after == self.before - 1:
            self.armed = True
        self.real.release()


def _no_receipt_http(*, on_send=None):
    inner, seen = _http(send="ambiguous", on_send=on_send)

    def http(method, url, headers=None, body=None, timeout=10):
        if json.loads(body)["method"] == "eth_getTransactionReceipt":
            return 200, {"result": None}, None
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    return http, seen


def test_no_reoffer_after_a_rewind(env, tmp_path):
    """Once its nonce is given back, the approval is never re-offered: any signer may now hold it.
    The worst case is one exit approving for itself, which is today's path."""
    env.setattr(time, "sleep", lambda *_a: None)
    path, c = _db(tmp_path)
    pid = _open(c)
    http, seen = _no_receipt_http()
    ex = _ex(http)
    ex["preapprove_position_fn"](path, pid)
    assert len(seen["sends"]) == 1
    row = _rows(c)[0]
    assert row["send_status"] == "ambiguous" and row["receipt_status"] == "unknown"
    assert row["gas_used"] is None


def test_exit_allocating_between_rewind_and_check_gets_no_reoffer_at_its_nonce(env, tmp_path):
    """Gamma F1 (ca742c795): the rewind and its check were two lock holds. An exit that took nonce 5
    between them made the check read 6, so the approval bytes at 5 were re-offered over the exit."""
    env.setattr(time, "sleep", lambda *_a: None)
    path, c = _db(tmp_path)
    pid = _open(c)
    http, seen = _no_receipt_http()
    ex = _ex(http)
    state = _closure(_closure(ex["_sign"], "_floored_nonce"), "_nonce_state")
    exit_nonce = []
    state["lock"] = _TapLock(state, lambda: exit_nonce.append(ex["_sign"](dict(_SWAP))[2]))
    ex["preapprove_position_fn"](path, pid)
    state["lock"].fired = True
    if not exit_nonce:                   # no acquisition followed the rewind inside the worker
        exit_nonce.append(ex["_sign"](dict(_SWAP))[2])
    assert exit_nonce == [5]
    assert len(seen["sends"]) == 1


def test_reoffer_kept_when_a_later_signer_depends_on_the_nonce(env, tmp_path):
    """No rewind happened: a signer took nonce+1 during the send, so only this approval landing
    unblocks it. Re-offer the identical bytes."""
    env.setattr(time, "sleep", lambda *_a: None)
    path, c = _db(tmp_path)
    pid = _open(c)
    box = {}
    http, seen = _no_receipt_http(
        on_send=lambda: box.get("later") or box.setdefault("later", box["ex"]["_sign"](dict(_SWAP))))
    ex = box["ex"] = _ex(http)
    ex["preapprove_position_fn"](path, pid)
    assert box["later"][2] == 6
    assert len(seen["sends"]) > 1
    assert set(seen["sends"]) == {seen["sends"][0]}


def test_base_background_approval_uses_nonce_floor_without_pipelining(env, tmp_path):
    env.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663,56")
    path, c = _db(tmp_path)
    pid = _open(c)
    http, seen = _http()
    ex = _ex(http)
    ex["preapprove_position_fn"](path, pid)
    assert ex["_sign"](dict(_SWAP))[2] == 6
    assert _rows(c)[0]["receipt_status"] == "confirmed"


def test_base_preapproval_and_maintenance_cannot_reuse_stale_node_nonce(env, tmp_path):
    env.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663,56")
    path, c = _db(tmp_path)
    http, _seen = _http()
    ex = _ex(http)
    ex["preapprove_position_fn"](path, _open(c))
    assert ex["gas_claim_nonce_fn"](ex["lifi_local"]["nonce_fn"]()) == 6
    assert ex["_sign"](dict(_SWAP))[2] == 7



def test_base_concurrent_approval_swap_and_maintenance_get_unique_nonces(env, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from eth_account._utils.legacy_transactions import Transaction
    env.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663,56")
    http, seen = _http()
    ex = _ex(http)
    def allocate(i):
        if i % 3 == 0:
            ex["_approve"](PERMIT2, 1000, token=TOKEN, wait_for_receipt=False)
            return None
        if i % 3 == 1:
            return ex["_sign"](dict(_SWAP))[2]
        return ex["gas_claim_nonce_fn"](ex["lifi_local"]["nonce_fn"]())
    with ThreadPoolExecutor(max_workers=2) as pool:
        nonces = list(pool.map(allocate, range(12)))
    nonces = [n for n in nonces if n is not None]
    nonces += [Transaction.from_bytes(bytes.fromhex(raw[2:])).nonce for raw in seen["sends"]]
    assert sorted(nonces) == list(range(5, 17))


@pytest.mark.parametrize("mode", ["topup", "reclaim"])
@pytest.mark.parametrize("failure", ["sign", "journal", "reject", "ambiguous", "gas", "sign_raise"])
def test_base_maintenance_releases_only_proven_unsent_nonce(env, mode, failure):
    from sniper.services import memecoin_evm_gas_topup as G
    env.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663,56")
    http, seen = _http()
    ex = _ex(http)
    local = ex["lifi_local"]
    def sign(tx):
        if failure == "sign_raise":
            raise ValueError("sign unavailable")
        return None if failure == "sign" else local["sign_fn"](tx)
    def journal(_plan):
        if failure == "journal":
            raise OSError("disk unavailable")
    kw = dict(chain_id=CHAIN, address="0xabc", weth_token=TOKEN, rpc_url="http://rpc",
              native_balance_fn=lambda: 0 if mode == "topup" else 10**18,
              nonce_fn=local["nonce_fn"], claim_nonce_fn=ex["gas_claim_nonce_fn"],
              release_nonce_fn=ex["gas_release_nonce_fn"],
              gas_price_fn=lambda: None if failure == "gas" else 10**9,
              sign_fn=sign, send_fn=lambda raw: None if failure == "ambiguous" else False,
              confirm_fn=None, target_wei=10**16, record_submission_fn=journal)
    if mode == "topup":
        kw.update(weth_balance_fn=lambda: 10**18, low_water_wei=10**15,
                  min_weth_reserve_wei=0, daily_cap_wei=10**18, spent_today_wei=0)
    try:
        getattr(G, "evm_gas_" + mode + "_once")(**kw)
    except ValueError:
        assert failure == "sign_raise"
    assert ex["_sign"](dict(_SWAP))[2] == (6 if failure == "ambiguous" else 5)
    assert seen["sends"] == []


def test_base_timed_out_maintenance_nonce_read_does_not_allocate(env):
    from sniper.services.memecoin_evm_gas_topup import _read_within
    env.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663,56")
    inner, _seen = _http()
    started, finish, completed = threading.Event(), threading.Event(), threading.Event()
    def http(*args, **kwargs):
        if json.loads(kwargs.get("body") or args[3])["method"] == "eth_getTransactionCount":
            started.set()
            finish.wait(1)
            result = inner(*args, **kwargs)
            completed.set()
            return result
        return inner(*args, **kwargs)
    ex = _ex(http)
    assert _read_within(ex["lifi_local"]["nonce_fn"], 0.01) is None
    assert started.is_set()
    finish.set()
    assert completed.wait(1)
    assert ex["_sign"](dict(_SWAP))[2] == 5


def test_base_maintenance_rejection_cannot_rewind_a_later_signer(env):
    from sniper.services import memecoin_evm_gas_topup as G
    env.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663,56")
    http, _seen = _http()
    ex = _ex(http)
    local = ex["lifi_local"]
    later = []
    def send(_raw):
        later.append(ex["_sign"](dict(_SWAP))[2])
        return False
    G.evm_gas_topup_once(
        chain_id=CHAIN, address="0xabc", weth_token=TOKEN, rpc_url="http://rpc",
        native_balance_fn=lambda: 0, weth_balance_fn=lambda: 10**18,
        nonce_fn=local["nonce_fn"], gas_price_fn=lambda: 10**9,
        claim_nonce_fn=ex["gas_claim_nonce_fn"], release_nonce_fn=ex["gas_release_nonce_fn"],
        sign_fn=local["sign_fn"], send_fn=send, confirm_fn=None,
        low_water_wei=10**15, target_wei=10**16, min_weth_reserve_wei=0,
        daily_cap_wei=10**18, spent_today_wei=0)
    assert later == [6]
    assert ex["_sign"](dict(_SWAP))[2] == 7


@pytest.mark.parametrize("chain", ["4663", "1", "56"])
def test_non_base_maintenance_cannot_release_background_floor(env, chain):
    http, _seen = _http()
    env.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", chain)
    env.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", chain)
    ex = _ex(http, chain=chain)
    assert ex["gas_claim_nonce_fn"] is None
    assert ex["gas_release_nonce_fn"] is None
