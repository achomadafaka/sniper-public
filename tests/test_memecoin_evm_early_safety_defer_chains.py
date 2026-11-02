"""Speed fix item 2(A) (operator yes via Alpha 2026-10-15): the exact-reserved-clip early-safety
reuse that 56 has (f31eeb917) on every chain listed in MEMECOIN_EVM_DEFER_EARLY_SAFETY_CHAINS
(default 56 = today). A non-56 chain defers its own safety until after the reservation only when
the early worker's safety has already started, so a missing early check adds no latency.

7e8b1870 (4663, 2026-10-15 05:56:42Z): an early pass existed at the exact final clip (33.13), but
the leg ran its own 541 ms safety at the pre-reservation size, and the gate joined that run before
re-checking the reserved clip."""
import pytest

from sniper.settings import memecoin as cfg
from test_memecoin_bsc_early_safety_clip import _run


@pytest.fixture
def listed(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_DEFER_EARLY_SAFETY_CHAINS", "56,4663,8453,1")


@pytest.mark.parametrize("chain", ["4663", "8453", "1"])
def test_a_listed_chain_with_a_started_early_check_runs_no_stale_safety(monkeypatch, tmp_path, listed, chain):
    result, fresh_calls, mark = _run(monkeypatch, tmp_path, chain=chain)
    assert result["status"] == "filled"
    assert fresh_calls == [], "no run at the preliminary size, no wait on it"
    assert mark["early"] is True and mark["early_queries"] == (("0xtok", 142.11),)


def test_a_listed_chain_whose_early_check_never_started_keeps_todays_order(monkeypatch, tmp_path, listed):
    result, fresh_calls, mark = _run(monkeypatch, tmp_path, chain="4663", early_safety=False)
    assert fresh_calls == [("0xtok", 175, 100.5), ("0xtok", 142.11, 100.5)]
    assert result["status"] == "unsafe" and mark["early"] is False


def test_a_listed_chain_with_an_early_pass_for_another_clip_checks_the_reserved_clip_fresh(
        monkeypatch, tmp_path, listed):
    result, fresh_calls, mark = _run(monkeypatch, tmp_path, chain="4663", early_clip=175)
    assert fresh_calls == [("0xtok", 142.11, 100.5)]
    assert result["status"] == "unsafe" and mark["early"] is False


@pytest.mark.parametrize("raw, want", [
    (None, {"56"}), ("", set()), ("56,4663", {"56", "4663"}), ("56,base", {"56"}), (" 4663 ", {"4663"}),
])
def test_the_chain_list(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv("MEMECOIN_EVM_DEFER_EARLY_SAFETY_CHAINS", raising=False)
    else:
        monkeypatch.setenv("MEMECOIN_EVM_DEFER_EARLY_SAFETY_CHAINS", raw)
    assert cfg.memecoin_evm_defer_early_safety_chains() == frozenset(want)


def test_bsc_keeps_its_own_rule_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMECOIN_EVM_DEFER_EARLY_SAFETY_CHAINS", raising=False)
    result, fresh_calls, mark = _run(monkeypatch, tmp_path, chain="56", early_safety=False)
    assert fresh_calls == [("0xtok", 142.11, 100.5)], "56 defers even before its early check starts"


@pytest.mark.parametrize("chain", ["4663", "8453", "1"])
def test_the_reserved_clip_check_starts_beside_the_quote_not_at_the_gate(monkeypatch, tmp_path, chain):
    # Gamma MEDIUM on 1b46ef794: a fractional reservation (48 of 65 on 4663) re-ran the full check
    # serially inside the gate, after the quote (4663 p50 541 / p90 1002 ms). The approved clip's
    # check now starts the moment the reservation sizes it, before the executor quotes.
    order = []
    result, fresh_calls, _mark = _run(monkeypatch, tmp_path, chain=chain, early_present=False,
                                      order=order)
    assert order == [("safety", 175), ("safety", 142.11), ("execute", 142.11)]
    assert fresh_calls == [("0xtok", 175, 100.5), ("0xtok", 142.11, 100.5)]
    assert result["status"] == "unsafe", "only the reserved clip's verdict authorizes"
