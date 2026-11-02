"""Standing allowance startup seed (Gamma, 2026-10-21). The books live in memory, so every restart
re-sent one cap approve per pinned spender (6 on the 13:10Z restart, gas each). The bootstrap now
reads allowance(owner, spender) once per spender on its own thread and seeds the count when the
on-chain allowance already covers the cap; below the cap, or on a failed read, today's single
top-up runs. A book touched during the read is never seeded."""
import json

import pytest

import sniper.execution.evm_real_seams as S
from sniper.execution import evm_standing_allowance as sa
from test_memecoin_evm_standing_allowance_4663 import CHAIN, E18, PERMIT2, _key, env  # noqa: F401
from eth_account import Account

CAP = sa.cap_atomic(500, 2400.0, 18)


def _http(allowance):
    seen = {"sends": [], "allowance_reads": 0}

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth = req["method"]
        if meth == "eth_chainId":
            return 200, {"result": hex(int(CHAIN))}, None
        if meth == "eth_call" and req["params"][0]["data"].startswith("0xdd62ed3e"):
            seen["allowance_reads"] += 1
            if isinstance(allowance, Exception):
                raise allowance
            return 200, {"result": hex(allowance)}, None
        if meth == "eth_getTransactionCount":
            return 200, {"result": "0x5"}, None
        if meth == "eth_gasPrice":
            return 200, {"result": "0x3b9aca00"}, None
        if meth == "eth_sendRawTransaction":
            seen["sends"].append(req["params"][0])
            return 200, {"result": "0x" + "cd" * 32}, None
        if meth == "eth_getTransactionReceipt":
            return 200, {"result": {"status": "0x1", "gasUsed": "0xb4a0",
                                    "effectiveGasPrice": "0x3b9aca00", "logs": []}}, None
        return 200, {"result": "0x0"}, None
    return http, seen


def _start(http):
    acct = Account.create()
    ex = S.build_real_evm_executor(acct=acct, chain_id=CHAIN, rpc_url="https://rpc", http=http)
    assert ex["standing_bootstrap_fn"]() == "seeded"
    return ex, acct


@pytest.mark.parametrize("allowance,sends,seeded", [(None, 0, True), (CAP + 1, 0, True),
                                                    (CAP - 1, 1, False), (0, 1, False)])
def test_startup_sends_a_top_up_only_when_the_chain_allowance_is_below_the_cap(env, allowance, sends,
                                                                                seeded):
    http, seen = _http(CAP if allowance is None else allowance)
    ex, acct = _start(http)
    assert seen["allowance_reads"] == 1                     # one read per pinned spender
    ex["standing_topup_fn"](PERMIT2)
    assert len(seen["sends"]) == sends
    k = _key(acct)
    if seeded:
        assert sa.value(k) == (CAP if allowance is None else allowance)
        assert ex["standing_approve_fn"](PERMIT2, E18 // 100, 2400.0, []) == {"standing": True}
    else:
        assert sa.value(k) == CAP                            # the single top-up confirmed


def test_a_failed_read_leaves_todays_single_top_up(env):
    http, seen = _http(RuntimeError("rpc down"))
    ex, acct = _start(http)
    assert sa.value(_key(acct)) is None
    ex["standing_topup_fn"](PERMIT2)
    assert len(seen["sends"]) == 1


def test_a_book_touched_during_the_read_is_never_seeded():
    key = ("4663", "0xw", "0xt", PERMIT2)
    generation = sa.seed_generation(key)
    sa.exact_approved(key, 10 ** 15, token_usd=2400.0)      # an entry overwrote the allowance meanwhile
    sa.settle(key, 999, False)                               # ...and its outcome is unknown: v is None
    assert sa.value(key) is None
    assert sa.seed_allowance(key, CAP, CAP, generation) is False   # the read predates both
    assert sa.value(key) is None
    fresh = ("4663", "0xw2", "0xt", PERMIT2)
    assert sa.seed_allowance(fresh, CAP, CAP, sa.seed_generation(fresh)) is True
    assert sa.snapshot() is not None


def test_the_seed_read_never_runs_inside_the_wallet_lock(env, monkeypatch):
    """Alpha's third outcome: the allowance read takes no wallet lock and never runs while this thread
    holds one (entries hold it from quote to confirm). A busy wallet neither blocks nor skips the read."""
    import threading
    from contextlib import contextmanager
    from sniper.execution import exec_lock
    held = {"n": 0}
    real = exec_lock.wallet_exec_lock

    @contextmanager
    def tracking(key, **kw):
        with real(key, **kw) as got:
            held["n"] += 1 if got else 0
            try:
                yield got
            finally:
                held["n"] -= 1 if got else 0
    monkeypatch.setattr(exec_lock, "wallet_exec_lock", tracking)
    http, seen = _http(CAP - 1)                               # below the cap: the top-up must run too
    inside = []

    def watching(method, url, headers=None, body=None, timeout=10):
        if '"0xdd62ed3e' in body:
            inside.append(held["n"])
        return http(method, url, headers=headers, body=body, timeout=timeout)
    acct = Account.create()
    ex = S.build_real_evm_executor(acct=acct, chain_id=CHAIN, rpc_url="https://rpc", http=watching)
    key = exec_lock.evm_exec_lock_key(CHAIN, acct.address)
    taken, release = threading.Event(), threading.Event()

    def entry_holds_the_wallet():
        with real(key):
            taken.set()
            release.wait(5)
    t = threading.Thread(target=entry_holds_the_wallet)
    t.start()
    try:
        assert taken.wait(5)
        assert ex["standing_bootstrap_fn"]() == "seeded"       # completes while an entry holds it
        assert ex["standing_topup_fn"](PERMIT2) == "wallet_busy"   # the top-up does wait its turn
    finally:
        release.set()
        t.join(5)
    ex["standing_topup_fn"](PERMIT2)                           # wallet free: the top-up takes the lock
    assert inside == [0] and seen["allowance_reads"] == 1      # one read, never under our lock
    assert len(seen["sends"]) == 1
