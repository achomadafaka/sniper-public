"""A covered Permit2 allowance never pins an amount (Gamma's (d), safety review 2026-10-16).

When the taker's existing allowance to Permit2 covers the sell amount, the simulation overrides the
balance only and the chain's own allowance decides. That balance-only override used to be cached
under (chain, mint, spender) with no amount: a later, larger sell reused it, the real allowance no
longer covered it, transferFrom reverted, and the coin was refused as unsellable. Now the covered
result remembers the allowance it saw: an amount it covers reuses it with no read; a larger one is
decided again (live allowance read, then the allowance slot)."""
import json
import time

from sniper.execution.evm_token_safety import _simulation_amount_word
from test_memecoin_evm_safety_exit_hint import ALLOW_SLOT, BAL_SLOT, MINT, _permit2_safety
from test_memecoin_evm_token_safety import PERMIT2, TAKER, _slot

BAL_KEY = _slot(TAKER, BAL_SLOT)
ALLOW_KEY = _slot(PERMIT2, _slot(TAKER, ALLOW_SLOT))
SMALL, LARGE = 10 ** 21, 10 ** 24


def _word(amount):
    return int(_simulation_amount_word(amount), 16)


# The covered test compares the allowance with the simulation's amount word (the amount plus headroom),
# so the chain's allowance sits between the two words: it covers SMALL's and not LARGE's.
REAL_ALLOWANCE = (_word(SMALL) + _word(LARGE)) // 2


def _chain_http(log, real_allowance=REAL_ALLOWANCE):
    """An ERC-20 whose sell transfer needs balance >= amount and allowance >= amount, where the
    allowance is the CHAIN's own unless the call overrides its slot."""
    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth, params = req["method"], req.get("params") or []
        if meth == "eth_getCode":
            return 200, {"result": "0x60006000"}, None
        if meth != "eth_call":
            return 200, {"result": "0x"}, None
        data = params[0].get("data") or ""
        diff = {}
        for acct in (params[2] if len(params) > 2 else {}).values():
            diff.update(acct.get("stateDiff") or {})
        if data.startswith("0x313ce567"):
            return 200, {"result": hex(18)}, None
        if data.startswith("0x70a08231"):
            return 200, {"result": diff.get(BAL_KEY, "0x0")}, None
        if data.startswith("0xdd62ed3e"):
            if len(params) < 3:
                log.append(("plain_allowance", time.monotonic()))
            return 200, {"result": diff.get(ALLOW_KEY, hex(real_allowance))}, None
        need = int(diff.get(BAL_KEY, "0x0"), 16)
        allowance = int(diff.get(ALLOW_KEY, hex(real_allowance)), 16)
        log.append(("sim", dict(diff)))
        if need <= 0 or allowance < need:
            return 200, {"error": {"code": -32000, "message": "execution reverted"}}, None
        return 200, {"result": "0x"}, None
    return http


class Quote:
    """The sell quote for whatever amount the next check sells (set before each call)."""
    def __init__(self):
        self.amount = None

    def __call__(self, sell_token, buy_token, amount, **kw):
        if sell_token.lower() != MINT:
            return None
        return {"sellAmount": str(self.amount), "buyAmount": "1",
                "transaction": {"to": "0xrouter", "data": "0xdeadbeef"},
                "issues": {"allowance": {"spender": PERMIT2}}}


def _checker(log, real_allowance=None):
    quote = Quote()
    fn = _permit2_safety(_chain_http(log, **({} if real_allowance is None else {"real_allowance": real_allowance})),
                         quote)

    def check(amount):
        log.clear()
        quote.amount = amount
        return fn(MINT)
    return check


def _reads(log):
    return [k for k, _v in log].count("plain_allowance")


def _sims(log):
    return [v for k, v in log if k == "sim"]


def test_a_larger_sell_after_a_covered_one_is_not_refused_on_the_cached_override():
    log = []
    check = _checker(log)
    ok, reason = check(SMALL)
    assert ok, reason
    assert _sims(log)[-1] == {BAL_KEY: _simulation_amount_word(SMALL)}       # covered: balance only
    ok, reason = check(LARGE)
    assert ok, reason                                                       # was: sell_sim_reverted_unknown
    assert _sims(log)[-1] == {BAL_KEY: _simulation_amount_word(LARGE), ALLOW_KEY: _simulation_amount_word(LARGE)}
    assert _reads(log) == 1                                                 # decided again, live
    ok, reason = check(SMALL)
    assert ok, reason


def test_an_amount_the_seen_allowance_covers_reuses_it_with_no_read():
    log, reads = [], []
    check = _checker(log)
    for amount in (SMALL, SMALL // 2, SMALL):
        ok, reason = check(amount)
        assert ok, reason
        assert _sims(log)[-1] == {BAL_KEY: _simulation_amount_word(amount)}
        reads.append(_reads(log))
    assert reads == [1, 0, 0]                                               # no read added on repeats


def test_an_allowance_equal_to_the_word_covers_it():
    log = []
    check = _checker(log, real_allowance=_word(SMALL))
    reads = []
    for _ in range(2):
        ok, reason = check(SMALL)
        assert ok, reason
        assert _sims(log)[-1] == {BAL_KEY: _simulation_amount_word(SMALL)}
        reads.append(_reads(log))
    assert reads == [1, 0]                                                  # equal is covered: reused


def test_one_wei_over_the_seen_allowance_is_decided_again():
    log = []
    check = _checker(log, real_allowance=_word(SMALL))
    assert check(SMALL)[0]
    ok, reason = check(SMALL + 1)
    assert ok, reason
    assert ALLOW_KEY in _sims(log)[-1] and _reads(log) == 1
