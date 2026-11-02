"""A read goes to the least stale member; signing never comes near this seam.

A simulation on state the chain has left behind costs gas. On 4663 the custody endpoint runs 6-23
blocks behind the public member, median 8 (about 0.6 s at 12-13 blocks a second), and it does not
merely lag -- in 13 consecutive 1.5 s samples it failed to advance once, which is the sample where
the gap reached 23 blocks and 1.8 s. The `row` entry 0xb30fcc1d is what that costs: preflight
PASSED at 21:40:21.35 on custody state, chain REVERTED the transaction two seconds later, status
0x0, 115,763 gas, no logs.

No network. The pool is stubbed.
"""
import pytest

from sniper.execution import evm_read_rpc as R

CUSTODY = "https://custody.example/key"
FRESH = "https://fresh.example"
STALE = "https://stale.example"


class _Pool:
    """A stand-in for evm_rpc_pool; members are chain-proven unless a test says otherwise."""

    def __init__(self, members, lags, *, down=(), refusing=()):
        self._members, self._lags = members, lags
        self._down, self._refusing = set(down), set(refusing)
        self.calls = []

    def pool_for(self, _url):
        return list(self._members)

    def lag_ms(self, url):
        self.calls.append(("lag_ms", url))
        return self._lags.get(url)

    def available(self, url):
        self.calls.append(("available", url))
        return url not in self._down

    def refuses(self, url, method):
        self.calls.append(("refuses", url, method))
        return url in self._refusing

    def identity_state(self, url):
        return True


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_FRESH_READ_ENABLED", "1")


def _use(monkeypatch, pool):
    # `from sniper.execution import evm_rpc_pool` reads the PACKAGE ATTRIBUTE, so patching
    # sys.modules alone leaves the real pool in place and every test passes vacuously.
    import sniper.execution
    monkeypatch.setattr(sniper.execution, "evm_rpc_pool", pool, raising=False)
    return pool


def test_the_least_stale_member_wins(monkeypatch):
    """THE PROPERTY THIS FILE EXISTS FOR."""
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    assert R._freshest_read_member(CUSTODY) == FRESH


@pytest.mark.parametrize("identity", [None, False])
def test_a_fresh_backup_must_be_proven_on_the_right_chain(monkeypatch, identity):
    pool = _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    monkeypatch.setattr(pool, "identity_state", lambda url: identity)
    assert R._freshest_read_member(CUSTODY) == CUSTODY


def test_the_configured_url_stands_when_it_is_already_the_freshest(monkeypatch):
    _use(monkeypatch, _Pool([CUSTODY, STALE], {CUSTODY: 0.0, STALE: 900.0}))
    assert R._freshest_read_member(CUSTODY) == CUSTODY


def test_a_quarantined_member_is_never_chosen(monkeypatch):
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}, down=[FRESH]))
    assert R._freshest_read_member(CUSTODY) == CUSTODY


def test_a_member_refusing_THIS_method_is_never_chosen(monkeypatch):
    """publicnode answers some methods with 403. A fresher endpoint that will not serve the call
    is not fresher, it is absent."""
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}, refusing=[FRESH]))
    assert R._freshest_read_member(CUSTODY) == CUSTODY


def test_an_unmeasured_member_is_not_treated_as_fresh(monkeypatch):
    """`lag_ms` is None before anything is sampled. Unmeasured is not known-fresh, and guessing
    would route the funded read path to an endpoint nobody has timed."""
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: None}))
    assert R._freshest_read_member(CUSTODY) == CUSTODY


def test_an_empty_pool_falls_back_to_the_caller_url(monkeypatch):
    _use(monkeypatch, _Pool([], {}))
    assert R._freshest_read_member(CUSTODY) == CUSTODY


def test_a_broken_pool_never_decides_a_read(monkeypatch):
    class Boom:
        def pool_for(self, _u):
            raise RuntimeError("pool exploded")
    _use(monkeypatch, Boom())
    assert R._freshest_read_member(CUSTODY) == CUSTODY


def test_the_kill_switch_restores_the_old_behaviour_exactly(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_FRESH_READ_ENABLED", "0")
    pool = _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    assert R._freshest_read_member(CUSTODY) == CUSTODY
    assert pool.calls == [], "a disabled resolver must not even consult the pool"


def test_the_choice_is_re_asked_on_every_call_never_cached(monkeypatch):
    """Health, quarantine and per-method refusals all move on their own. A remembered winner is
    how a lane keeps talking to an endpoint that has started refusing."""
    pool = _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    assert R._freshest_read_member(CUSTODY) == FRESH
    pool._refusing.add(FRESH)
    assert R._freshest_read_member(CUSTODY) == CUSTODY, "the second call must re-ask"


def test_eth_call_raw_sends_to_the_fresh_member(monkeypatch):
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    seen = {}

    def fake_rpc(url, method, params, http=None):
        seen.update(url=url, method=method, params=params)
        return {"result": "0xfeed"}
    monkeypatch.setattr(R, "_rpc", fake_rpc)
    assert R.eth_call_raw("0xto", "0xdata", CUSTODY) == "0xfeed"
    assert seen["url"] == FRESH
    assert seen["method"] == "eth_call"
    assert seen["params"][1] == "latest"


def test_state_overrides_still_reach_the_node(monkeypatch):
    """The preflight depends on them; routing must not drop the third parameter."""
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    seen = {}
    monkeypatch.setattr(R, "_rpc",
                        lambda url, m, p, http=None: seen.update(p=p) or {"result": "0x1"})
    R.eth_call_raw("0xto", "0xdata", CUSTODY, overrides={"0xme": {"balance": "0x1"}})
    assert seen["p"][2] == {"0xme": {"balance": "0x1"}}


def test_an_rpc_error_still_returns_None_and_can_still_raise(monkeypatch):
    _use(monkeypatch, _Pool([CUSTODY], {CUSTODY: 0.0}))
    monkeypatch.setattr(R, "_rpc", lambda *a, **k: {"error": {"message": "boom"}})
    assert R.eth_call_raw("0xto", "0xdata", CUSTODY) is None
    with pytest.raises(RuntimeError):
        R.eth_call_raw("0xto", "0xdata", CUSTODY, raise_on_error=True)


# ---------------------------------------------------------------------------------------------
# The preflight was NOT covered by the first cut. It calls `_rpc` directly, so a simulation that
# decides whether to sign was still reading the lagging custody member while the quote half had
# already been made fresh. `row` 0xb30fcc1d is what that cost.

def test_the_preflight_simulation_reads_the_fresh_member(monkeypatch):
    """THE PROPERTY. Asserts the CHANGE -- a member other than custody is chosen -- not a
    fallback, because a dead stub makes every fallback assertion pass vacuously."""
    import sniper.execution.evm_real_seams as S
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    seen = {}

    def fake_rpc(url, method, params, http=None):
        seen.update(url=url, method=method, params=params)
        return {"result": "0x1"}
    monkeypatch.setattr(S, "_rpc", fake_rpc)
    status, _kind, _detail = S._rpc_eth_call_result(
        {"to": "0xr", "data": "0xab", "value": 0}, "0xme", CUSTODY,
        overrides={"0xme": {"balance": "0x1"}})
    assert status is True
    assert seen["url"] == FRESH, "the simulation that decides signing must read the fresh member"
    assert seen["method"] == "eth_call"
    assert seen["params"][2] == {"0xme": {"balance": "0x1"}}, "overrides must survive the routing"


def test_the_preflight_falls_back_to_custody_when_nothing_is_fresher(monkeypatch):
    import sniper.execution.evm_real_seams as S
    _use(monkeypatch, _Pool([CUSTODY, STALE], {CUSTODY: 0.0, STALE: 900.0}))
    seen = {}
    monkeypatch.setattr(S, "_rpc",
                        lambda url, m, p, http=None: seen.update(url=url) or {"result": "0x1"})
    S._rpc_eth_call_result({"to": "0xr", "data": "0xab", "value": 0}, "0xme", CUSTODY)
    assert seen["url"] == CUSTODY


def test_the_preflight_kill_switch_pins_it_back_to_the_caller_url(monkeypatch):
    import sniper.execution.evm_real_seams as S
    monkeypatch.setenv("MEMECOIN_EVM_FRESH_READ_ENABLED", "0")
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    seen = {}
    monkeypatch.setattr(S, "_rpc",
                        lambda url, m, p, http=None: seen.update(url=url) or {"result": "0x1"})
    S._rpc_eth_call_result({"to": "0xr", "data": "0xab", "value": 0}, "0xme", CUSTODY)
    assert seen["url"] == CUSTODY


# ---------------------------------------------------------------------------------------------
# Sites 2-5. Each asserts the CHANGE. The three deliberately-excluded sites are pinned below,
# because a later "completeness" pass routing them would be a regression, not an improvement.

def test_the_v4_quoter_reads_the_fresh_member(monkeypatch):
    from sniper.execution import evm_v4_quote as Q
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    seen = {}
    monkeypatch.setattr(Q, "_rpc", lambda url, m, p, http=None: seen.update(url=url, at=p[1])
                        or {"result": "0x" + "1" * 64})
    key = {"currency0": "0x" + "aa" * 20, "currency1": "0x" + "bb" * 20,
           "fee": 0, "tick_spacing": 200, "hooks": "0x" + "00" * 20}
    Q._quote_single("4663", key, True, 10 ** 16, CUSTODY)
    assert seen["url"] == FRESH and seen["at"] == "latest"


def test_the_multihop_path_quote_reads_fresh_at_latest(monkeypatch):
    from sniper.execution import evm_v4_multihop as M
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    seen = {}
    monkeypatch.setattr("sniper.execution.evm_read_rpc._rpc",
                        lambda url, m, p, http=None: seen.update(url=url, at=p[1])
                        or {"result": "0x" + "1" * 64})
    path = [("0x" + "bb" * 20, {"fee": 0, "tick_spacing": 200, "hooks": "0x" + "00" * 20})]
    M._quote_path("4663", "0x" + "aa" * 20, path, 10 ** 16, CUSTODY)
    assert seen["url"] == FRESH and seen["at"] == "latest"


def test_the_multihop_path_quote_STAYS_on_the_caller_node_at_a_pinned_block(monkeypatch):
    """A search that pinned a block on one node must quote on that node: another member may not
    hold that block yet. Routing this would be a regression, so it is pinned."""
    from sniper.execution import evm_v4_multihop as M
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    seen = {}
    monkeypatch.setattr("sniper.execution.evm_read_rpc._rpc",
                        lambda url, m, p, http=None: seen.update(url=url, at=p[1])
                        or {"result": "0x" + "1" * 64})
    path = [("0x" + "bb" * 20, {"fee": 0, "tick_spacing": 200, "hooks": "0x" + "00" * 20})]
    M._quote_path("4663", "0x" + "aa" * 20, path, 10 ** 16, CUSTODY, block="0x1234")
    assert seen["url"] == CUSTODY, "a pinned block must not be re-routed"
    assert seen["at"] == "0x1234"


def test_onchain_liquidity_reads_the_fresh_member(monkeypatch):
    from sniper.execution import evm_v4_multihop as M
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    seen = {}
    monkeypatch.setattr("sniper.execution.evm_read_rpc._rpc",
                        lambda url, m, p, http=None: seen.update(url=url) or {"result": "0x10"})
    M._onchain_liquidity("4663", "0x" + "cc" * 32, CUSTODY)
    assert seen["url"] == FRESH


def test_the_batched_liquidity_read_uses_the_fresh_member(monkeypatch):
    from sniper.execution import evm_v4_multihop as M
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    seen = {}
    monkeypatch.setattr("sniper.execution.evm_read_rpc._rpc",
                        lambda url, m, p, http=None: seen.update(url=url)
                        or {"result": "0x" + "%064x" % 32 + "%064x" % 1 + "%064x" % 7})
    assert M._onchain_liquidities("4663", ["0x" + "cc" * 32], CUSTODY) == {"0x" + "cc" * 32: 7}
    assert seen["url"] == FRESH


def test_the_second_eth_call_raw_reads_the_fresh_member(monkeypatch):
    """evm_real_seams defines its OWN eth_call_raw, distinct from evm_read_rpc's. token safety and
    the price service import this one, so the first cut never covered them."""
    import sniper.execution.evm_real_seams as S
    from sniper.execution.evm_read_rpc import eth_call_raw as other
    assert S.eth_call_raw is not other, "two distinct functions; that is the whole point"
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    seen = {}
    monkeypatch.setattr(S, "_rpc", lambda url, m, p, http=None: seen.update(url=url)
                        or {"result": "0xabc"})
    assert S.eth_call_raw("0xto", "0xdata", CUSTODY) == "0xabc"
    assert seen["url"] == FRESH


def test_the_permit2_NONCE_is_never_re_routed(monkeypatch):
    """A nonce must come from the node we submit through. A wrong permit2 nonce invalidates a
    signature, which is worse than a stale quote."""
    from sniper.execution import evm_v4_quote as Q
    _use(monkeypatch, _Pool([CUSTODY, FRESH], {CUSTODY: 640.0, FRESH: 0.0}))
    seen = {}
    monkeypatch.setattr(Q, "_rpc", lambda url, m, p, http=None: seen.update(url=url)
                        or {"result": "0x" + "0" * 64})
    Q._permit2_nonce("4663", "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20, CUSTODY)
    assert seen["url"] == CUSTODY, "the nonce must not be read from another member"
