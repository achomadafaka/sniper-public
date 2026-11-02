"""E11 on the source's post-trade price (spec 2026-10-08-memecoin-evm-entry-gates-onchain-design.md;
operator decision (b), Alpha's pick on the delegated number, 2026-10-08).

With `E11:<chain>` in MEMECOIN_DEXSCREENER_OFF the EVM execution premium never reads the DexScreener
display price. Its reference is the source's post-trade price (the pool right after the source's
swap), its cap MEMECOIN_REAL_MAX_EXEC_PREMIUM_POST_TRADE (1.2 = the live 1.5 / the measured median
post-trade/display 1.25, so the gate keeps today's strictness). No post-trade price: today's
missing-display posture (the source average fill at the live cap, or abstain). On every EVM attempt,
switched or not, the trace records the shadow verdicts the 2026-10-15 refit reads: 1.2x and 1.6x on
post-trade, and the live cap on quote/display while the display price is in hand.
"""
import json

import pytest

from sniper.execution import evm_swap_executor as X
from sniper.services.memecoin_market_state import execution_premium_decision


def _cls(display):
    return {"facts": {"display_price": display}}


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    import sniper.settings.memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.5, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_POST_TRADE", 1.2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_EXEC_PREMIUM_SOURCE_FALLBACK", True, raising=False)


def test_onchain_decides_on_the_post_trade_price_at_its_own_cap_and_never_reads_display():
    r = execution_premium_decision(_cls(1.0), exact_quote_price=1.30, source_price=1.0,
                                   post_trade_price=1.25, onchain=True)
    assert r.reference_basis == "post_trade" and r.reference_price == 1.25
    assert r.cap == 1.2 and r.allow is True and r.ratio == pytest.approx(1.04)
    assert r.display_price is None                     # the chase can never pick it up
    r = execution_premium_decision(_cls(1.0), exact_quote_price=1.60, source_price=1.0,
                                   post_trade_price=1.25, onchain=True)
    assert r.allow is False and r.reason == "execution_premium_over_cap"


def test_onchain_without_a_post_trade_price_keeps_the_missing_display_posture():
    r = execution_premium_decision(_cls(1.0), exact_quote_price=1.40, source_price=1.0,
                                   post_trade_price=None, onchain=True)
    assert r.reference_basis == "source" and r.cap == 1.5 and r.allow is True
    assert r.display_price is None


def test_onchain_unit_fault_is_still_a_unit_fault():
    r = execution_premium_decision(_cls(None), exact_quote_price=2487.26, source_price=0.0065,
                                   post_trade_price=0.0065, onchain=True)
    assert r.reason == "quote_unit_fault" and r.allow is False


def test_switched_off_is_byte_for_byte_today():
    a = execution_premium_decision(_cls(1.0), exact_quote_price=1.4, source_price=0.9)
    b = execution_premium_decision(_cls(1.0), exact_quote_price=1.4, source_price=0.9,
                                   post_trade_price=1.25, onchain=False)
    assert a == b and a.reference_basis == "chart"


def test_a_post_trade_refusal_names_its_reference():
    r = execution_premium_decision(_cls(None), exact_quote_price=1.60, source_price=1.0,
                                   post_trade_price=1.25, onchain=True)
    reason, tag = X._premium_refusal(r)
    assert reason == "route quote is 1.28x the post-trade price (cap 1.20x)" and tag == "execution_premium"


class _Trace:
    def __init__(self):
        self.marks = []

    def mark_substage(self, name, **fields):
        self.marks.append((name, fields))


def _contract(display):
    return {"funded_contract_json": json.dumps({"classification": _cls(display)})}


@pytest.mark.parametrize("switched", [False, True])
def test_the_trace_carries_the_three_shadow_verdicts_switched_or_not(monkeypatch, switched):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E11:4663" if switched else "")
    trace = _Trace()
    r = X._execution_premium(_contract(1.0), 1.40, trace, attempt_no=1, revision="initial",
                             source_price=1.0, post_trade_price=1.25, network_id="4663")
    (name, f), = [m for m in trace.marks if m[0] == "execution_premium"]
    assert f["shadow_post_trade_ratio"] == pytest.approx(1.12)
    assert f["shadow_post_trade_allow_1_2"] is True and f["shadow_post_trade_allow_1_6"] is True
    assert f["shadow_display_ratio"] == pytest.approx(1.40) and f["shadow_display_allow"] is True
    assert r.reference_basis == ("post_trade" if switched else "chart")


def test_another_chain_is_not_switched_by_a_4663_entry(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E11:4663")
    r = X._execution_premium(_contract(1.0), 1.40, None, attempt_no=1, revision="initial",
                             source_price=1.0, post_trade_price=1.25, network_id="56")
    assert r.reference_basis == "chart"


def test_e11_is_a_known_switch_id():
    from sniper.settings import memecoin as cfg
    assert cfg.dexscreener_off("E11", "8453", env={"MEMECOIN_DEXSCREENER_OFF": "E11:8453"})
