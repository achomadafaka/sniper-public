"""Relay is exit-only on the buy side.

Relay is an aggregator, not a pool, and it carries the aggregator's spread. It only wins when every
direct rung has missed, which on 4663 is most of the time — so it became the de facto entry route
on the chain where routing is worst. Measured over 14 days of funded 4663 positions:

    direct pool   n=45   MED ROI  +2.0%   win 53%   total  +$64.82
    relay         n=11   MED ROI -25.1%   win 27%   total -$214.73

The sell side is the opposite: when every direct rung misses on an exit, relay is the only way the
position closes at all, and removing it would strand bags.
"""
import pathlib
import re

import pytest

SRC = pathlib.Path("sniper/execution/evm_real_seams.py").read_text()


def _relay_gate():
    m = re.search(r"if relay_enabled and (.+?):\n(.+?)\n        if relay_enabled:", SRC, re.S)
    assert m, "the relay entry gate has moved or been removed"
    return m.group(1), m.group(2)


def test_the_gate_keys_on_DIRECTION_not_just_a_flag():
    """A chain-level off switch would kill relay for exits too and strand bags."""
    cond, _ = _relay_gate()
    assert "is_buy" in cond, (
        "relay must be refused for BUYS only; a direction-blind switch removes the only route "
        "some exits have")


def test_the_default_is_OFF_for_entries():
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_EVM_RELAY_ENTRY_ENABLED is False


def test_the_knob_can_restore_it(monkeypatch):
    """When the direct rungs can carry entries again this must be reversible without a code change."""
    import importlib
    monkeypatch.setenv("MEMECOIN_EVM_RELAY_ENTRY_ENABLED", "1")
    from sniper.settings import memecoin as cfg
    importlib.reload(cfg)
    assert cfg.MEMECOIN_EVM_RELAY_ENTRY_ENABLED is True
    monkeypatch.delenv("MEMECOIN_EVM_RELAY_ENTRY_ENABLED")
    importlib.reload(cfg)
    assert cfg.MEMECOIN_EVM_RELAY_ENTRY_ENABLED is False


def test_the_refusal_is_RECORDED_not_silent():
    """A rung that vanishes without a trace reads as 'no route existed', which is a different
    fact and the one that has misled us all day."""
    _, body = _relay_gate()
    assert "skipped.append" in body and "relay_entry_disabled" in body


def test_relay_is_still_reachable_for_sells():
    """The gate must not remove relay from the ladder outright."""
    assert 'routes.append(("relay"' in SRC
    gate_at = SRC.index("if relay_enabled and ")
    append_at = SRC.index('routes.append(("relay"')
    assert gate_at < append_at, "the gate must precede the append, not replace it"


def test_the_CALL_SITE_fallback_is_also_off():
    """`getattr(cfg, ..., default)` has its own default, and it becomes live the moment the
    settings constant is removed or renamed. Asserting only the settings value leaves a mutant
    that flips the fallback to True completely undetected."""
    assert 'MEMECOIN_EVM_RELAY_ENTRY_ENABLED", False)' in SRC, (
        "the call-site fallback must be False, so losing the settings constant fails CLOSED "
        "(relay stays off for entries) rather than open")


def test_a_BUY_does_not_consult_relay_under_the_default_config(monkeypatch):
    """THE NEW CONTRACT, and the thing that can silently regress: someone flips a default, relay
    is back on buys, and the only symptom is the -25.1% median showing up again weeks later."""
    from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback
    from sniper.settings import memecoin as cfg

    WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    MINT = "0x18e674231a58c239dc7daedcffe15ec3a24cff5c"
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENTRY_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False, raising=False)

    relay_calls = []
    rungs = []
    fn = venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="4663", quote_token=WETH, taker="0xt",
        v2_quote_fn=lambda *a, **k: None, pons_v2_quote_fn=lambda *a, **k: None,
        relay_quote_fn=lambda *a, **k: relay_calls.append(1),
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append)

    fn(WETH, MINT, 10**18)                       # WETH in == a BUY
    assert relay_calls == [], "relay was consulted on a buy"
    assert "relay" not in [r["rung"] for r in rungs]


def test_a_SELL_still_consults_relay_under_the_default_config(monkeypatch):
    """The complement, and the reason the gate keys on direction: when every direct rung misses
    on an exit, relay is the only way the position closes."""
    from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback
    from sniper.settings import memecoin as cfg

    WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    MINT = "0x18e674231a58c239dc7daedcffe15ec3a24cff5c"
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENTRY_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False, raising=False)

    relay_calls = []
    fn = venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="4663", quote_token=WETH, taker="0xt",
        v2_quote_fn=lambda *a, **k: None, pons_v2_quote_fn=lambda *a, **k: None,
        relay_quote_fn=lambda *a, **k: relay_calls.append(1),
        acct=object(), rpc_url="https://rpc", on_rung=lambda r: None)

    fn(MINT, WETH, 10**18)                       # MINT in == a SELL
    assert relay_calls, "relay was refused on a SELL, which can strand a bag"


def test_is_buy_is_defined_even_when_the_v2_branch_is_skipped():
    """It used to be assigned only inside the v2 `else`, so with v2 entry disabled the relay gate
    read an UNDEFINED name and raised NameError on the funded quote path — which surfaced as a
    typed `provider_rpc_transport` miss, i.e. an infrastructure fault that never happened."""
    import inspect

    from sniper.execution import evm_real_seams
    src = inspect.getsource(evm_real_seams.venue_quote_with_v2_fallback)
    assign = src.index("is_buy = (quote_token")
    branch = src.index('skipped.append("v2")')
    assert assign < branch, "is_buy must be assigned before the branch that can skip v2"
