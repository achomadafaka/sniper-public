"""flap.sh sell-override discovery: try the factory template's slots first (Alpha, 2026-09-30).

`discover_sell_overrides` finds where a flap token keeps `taker`'s balance and allowance, so a sell
can be simulated before any real approval exists. It scanned: two scans of about 15 `eth_call`s each,
in parallel, on the first sight of every mint, inside the entry ladder's reverse leg. Every flap
token checked so far is one factory template with the balance mapping at slot 51 and the allowance
mapping at slot 52. Measured 2026-09-30 off the funded host: 30 to 55 calls for a first-sight mint,
and a public host answered HTTP 429 to that burst in 3 of 10 repeats. On the funded custody pool a
429 is a penalty and a quarantine step, in the middle of an entry.

The two template keys are now asked first, with the scan's own proof (write the key, read the
getter back): 2 calls in parallel. A clean "not this slot" falls back to the scan, exactly as before.
A transport failure does not start the scan. The proof is NOT a sell simulation: 2 of the 10
on-curve non-native tokens seen that week revert their sell with the right slots funded, and those
are the tokens the rung refuses; a sell-based check would have sent exactly them to the wave.

Rule 5 (CLAUDE.md):
1. Money path: yes, the funded entry ladder's reverse leg and the exit quote for flap coins on 56.
   The override only fabricates a balance and an allowance inside an `eth_call`; it never reaches a
   signed transaction. A key is accepted only when writing it changes what `balanceOf` /
   `allowance` return, the same proof the scan uses, so a wrong pair cannot be cached.
2. Fill-path speed: fewer calls, never more on the template. Measured before and after in the
   commit message (20 cold runs each, off the funded host).
3. Concurrency: the template probes run on the same two-worker executor as the scan; the cache is
   a single dict assignment.
4. Absent vs zero: a reverted simulation is "not this layout" and falls back to the scan. A call
   that failed on transport is unknown: no scan, nothing cached, the caller refuses this ask.
5. Dependencies: each template probe retries a transport failure with the scan's own backoff and
   pool rotation; a sustained failure returns None, as a sustained scan failure did.
6. Units and identity: the slot keys are the scan's own (`_map_slot(taker, 51)`,
   `_nested_slot(spender, taker, 52)`), checked here against keys hashed independently.
7. Sample: none; nothing here produces a statistic.
8. Rollback: revert the commit. The cache lives in the process.
"""
import pytest
from eth_utils import keccak

from sniper.execution import evm_flap_curve as fc
from sniper.execution import evm_read_rpc
from sniper.execution.evm_wallet import SEL_ALLOWANCE, SEL_BALANCE_OF

MINT = "0x" + "a1" * 20
PORTAL = fc.FLAP_PORTAL["56"]
TAKER = "0x" + "7a" * 20
RPC = "https://bsc.example"
AMOUNT = 5 * 10 ** 24


def _pad(value):
    return (value.to_bytes(32, "big") if isinstance(value, int) else bytes.fromhex(value[2:].rjust(64, "0")))


def _balance_key(index):
    return "0x" + keccak(_pad(TAKER) + _pad(index)).hex()


def _allowance_key(index):
    return "0x" + keccak(_pad(PORTAL) + keccak(_pad(TAKER) + _pad(index))).hex()


class Chain:
    """A token with its balance mapping at `balance_index` and its allowance mapping at
    `allowance_index`, and a Portal whose sell succeeds only when the override funds BOTH real slots."""

    def __init__(self, balance_index=51, allowance_index=52, *, sell_reverts=False, fail=0, fail_always=False,
                 payout=12345, standing=0, garbage_on=()):
        self.balance_key, self.allowance_key = _balance_key(balance_index), _allowance_key(allowance_index)
        self.sell_reverts, self.fail, self.fail_always = sell_reverts, fail, fail_always
        self.payout, self.standing, self.garbage_on = payout, standing, set(garbage_on)
        self.calls, self.urls = [], []

    def eth_call_raw(self, to, data, rpc_url, *, http=None, overrides=None, from_addr=None,
                     value_wei=None, raise_on_error=False):
        self.calls.append("sell" if to == PORTAL else "probe")
        self.urls.append(rpc_url)
        if rpc_url in self.garbage_on:
            return "<html>busy</html>"                     # a 200 that is not a word
        if self.fail_always or self.fail > 0:
            self.fail -= 1
            raise OSError("HTTP Error 429: Too Many Requests")
        diff = ((overrides or {}).get(MINT) or {}).get("stateDiff") or {}
        if to == PORTAL:
            funded = (int(diff.get(self.balance_key, "0x0"), 16) >= AMOUNT
                      and int(diff.get(self.allowance_key, "0x0"), 16) >= AMOUNT)
            if self.sell_reverts or not funded:
                if raise_on_error:
                    raise RuntimeError("eth_call RPC error")
                return None
            return "0x" + self.payout.to_bytes(32, "big").hex()
        key = self.balance_key if data[2:10] == SEL_BALANCE_OF else self.allowance_key
        assert data[2:10] in (SEL_BALANCE_OF, SEL_ALLOWANCE)
        return diff.get(key, "0x" + self.standing.to_bytes(32, "big").hex())   # what the taker really holds


@pytest.fixture
def chain(monkeypatch):
    def install(**kw):
        c = Chain(**kw)
        monkeypatch.setattr(evm_read_rpc, "eth_call_raw", c.eth_call_raw)
        return c
    monkeypatch.setattr(fc, "_sell_override_slot_cache", {})
    monkeypatch.setattr("sniper.execution.evm_token_safety._access_list_slots", lambda *a, **k: ())
    return install


def _discover():
    return fc.discover_sell_overrides(MINT, PORTAL, AMOUNT, taker=TAKER, rpc_url=RPC)


def test_the_template_layout_costs_two_read_backs_and_no_scan(chain):
    c = chain()
    out = _discover()
    assert c.calls == ["probe", "probe"]
    assert set(out[MINT]["stateDiff"]) == {_balance_key(51), _allowance_key(52)}      # hashed independently
    assert fc._sell_override_slot_cache[(MINT, TAKER, PORTAL)] == (_balance_key(51), _allowance_key(52))
    assert _discover() == out and c.calls == ["probe", "probe"]                        # cached: no call


def test_another_layout_falls_back_to_the_scan_and_finds_the_same_answer_as_before(chain):
    c = chain(balance_index=3, allowance_index=4)
    out = _discover()
    assert len(c.calls) > 4 and "sell" not in c.calls
    assert set(out[MINT]["stateDiff"]) == {_balance_key(3), _allowance_key(4)}
    assert fc._sell_override_slot_cache[(MINT, TAKER, PORTAL)] == (_balance_key(3), _allowance_key(4))


def test_a_half_template_token_is_scanned_not_accepted(chain):
    c = chain(balance_index=51, allowance_index=4)
    out = _discover()
    assert set(out[MINT]["stateDiff"]) == {_balance_key(51), _allowance_key(4)} and len(c.calls) > 4


def test_a_token_whose_sell_reverts_is_not_sent_to_the_scan(chain):
    """2 of the 10 on-curve non-native tokens of 2026-09-30 revert their sell with the right slots
    funded. The rung refuses them; they must not cost a probe wave on the way."""
    c = chain(sell_reverts=True)
    out = _discover()
    assert c.calls == ["probe", "probe"]
    assert set(out[MINT]["stateDiff"]) == {_balance_key(51), _allowance_key(52)}


def test_a_transport_failure_never_starts_the_scan_and_is_never_cached(chain):
    c = chain(fail_always=True)
    assert _discover() is None
    assert c.calls == ["probe"] * 8                      # 2 probes x the bounded retry, no wave
    assert (MINT, TAKER, PORTAL) not in fc._sell_override_slot_cache
    c.fail_always = False
    assert _discover() is not None and (MINT, TAKER, PORTAL) in fc._sell_override_slot_cache


def test_a_failure_on_the_second_probe_alone_is_not_read_as_another_layout(chain):
    c = chain(balance_index=3)                           # the balance probe answers "not this slot"
    real = c.eth_call_raw

    def allowance_down(to, data, rpc_url, **kw):
        if data[2:10] == SEL_ALLOWANCE:
            c.calls.append("probe")
            raise OSError("HTTP Error 429: Too Many Requests")
        return real(to, data, rpc_url, **kw)
    evm_read_rpc.eth_call_raw = allowance_down
    try:
        assert _discover() is None
    finally:
        evm_read_rpc.eth_call_raw = real
    assert len(c.calls) == 5 and (MINT, TAKER, PORTAL) not in fc._sell_override_slot_cache   # 1 balance probe + 4 tries


def test_one_transport_blip_is_retried_and_the_template_is_accepted(chain):
    c = chain(fail=1)
    assert _discover() is not None
    assert c.calls == ["probe"] * 3


def test_a_node_that_answers_garbage_is_retried_on_an_alternate(chain, monkeypatch):
    """Gamma MEDIUM on 9af34d60f: the body is parsed INSIDE the retried call, as the scan's own
    probe does (the 2026-09-04 SUIT fix), so a member that answers a 200 that is not a word costs a
    retry on the next member, not the ask. Also pins that the template probe rotates at all."""
    alt = "https://alt.example"
    monkeypatch.setattr("sniper.execution.evm_token_safety._slot_probe_alternates", lambda url: (alt,))
    monkeypatch.setattr("sniper.execution.evm_rpc_pool.available", lambda url: True)
    c = chain(garbage_on={RPC})
    out = _discover()
    assert out is not None and fc._sell_override_slot_cache[(MINT, TAKER, PORTAL)] == (_balance_key(51), _allowance_key(52))
    assert c.urls.count(RPC) == 2 and c.urls.count(alt) == 2 and len(c.calls) == 4     # no scan


def test_a_taker_that_already_holds_the_coin_does_not_make_every_key_look_right(chain):
    """Gamma LOW: on an exit the taker's balance and allowance are non-zero at EVERY key. Only the
    fabricated value read back proves the slot."""
    c = chain(balance_index=3, allowance_index=4, standing=777)
    out = _discover()
    assert set(out[MINT]["stateDiff"]) == {_balance_key(3), _allowance_key(4)} and len(c.calls) > 4


def test_an_invalid_amount_makes_no_call(chain):
    c = chain()
    assert fc.discover_sell_overrides(MINT, PORTAL, 0, taker=TAKER, rpc_url=RPC) is None
    assert c.calls == []
