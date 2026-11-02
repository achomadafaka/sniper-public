"""Chain-1 multihop: the entry-only gate is entry-only, and the gas guard prices the route it got
(2026-10-07, operator yes via Alpha).

1. evm_real_seams._quote_v4_multihop applied the ROUND-TRIP budget (an entry rule) to SELL quotes
   too: an exit quote carries no round_trip_pct, round_trip_within_budget(None) is False on every
   chain, so v4_multihop never answered a sell in the ladder and the chain-1 safety sell probe
   refused every multihop-only coin (0x24c2c6f7, 5 attempts 18:17-18:18Z). The gate is now buys
   only; a buy over budget still refuses.
2. The chain-1 gas guard priced every route at one flat 622,791 units (the 09-30 hooked-WETH p90).
   Fork receipts: V2V3 p90 382,335, V4 p90 414,240, V4_HOOKED p90 638,162 (ELA 904,983 is a known
   under-price), V4_MULTIHOP 488,436 (hookless, max of n=2), V4_MULTIHOP_HOOKED 1,051,328 (one coin,
   two runs). The class comes from the quote's own route; HOOKED and both MULTIHOP knobs are
   required (refuse while unset); V2V3 and V4 fall back to the flat knob; an unknown class refuses.
   A winner whose class changes after the guard is re-priced before the swap is signed.
"""
import pytest

from sniper.execution import evm_real_seams as seams
from sniper.execution import evm_v4_multihop as mh
from sniper.services import memecoin_evm_risk as risk
from sniper.settings import memecoin as cfg

QUOTE_4663 = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
MINT = "0x" + "d6" * 20
ZERO = "0x" + "00" * 20
HOOK = "0x" + "cb" * 20


def _ladder(monkeypatch, multihop_quote):
    from sniper.execution import geckoterminal_quote as gt
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True)
    monkeypatch.setattr(gt, "cooldown_active", lambda *a, **k: False)
    monkeypatch.setattr(mh, "dex_v4_multihop_quote", multihop_quote)
    rungs = []
    fn = seams.venue_quote_with_v2_fallback(
        lambda s, b, a, slippage_bps=None: None, chain="4663", quote_token=QUOTE_4663,
        taker="0xtaker", v2_quote_fn=lambda *a, **k: None, v4_quote_fn=lambda *a, **k: None,
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append)
    return fn, rungs


def _mh_quote(round_trip_pct=None):
    route = {"protocol": "uniswap_v4_multihop", "legs": 2}
    if round_trip_pct is not None:
        route["round_trip_pct"] = round_trip_pct
    return {"buyAmount": "123456", "sellAmount": "1000", "route": route,
            "transaction": {"to": "0xrouter", "data": "0x", "value": "0x0"}}


# --- 1. the round-trip gate is an entry gate -------------------------------------------------

def test_a_multihop_sell_quote_is_not_discarded_by_the_entry_budget(monkeypatch):
    """THE regression: 'round_trip_over_budget:None' on every sell."""
    fn, rungs = _ladder(monkeypatch, lambda *a, **k: _mh_quote())
    q = fn(MINT, QUOTE_4663, 10 ** 18)
    assert q is not None and q["route"]["protocol"] == "uniswap_v4_multihop", rungs
    assert not [r for r in rungs if str(r.get("reason", "")).startswith("round_trip_over_budget")]


def test_a_multihop_buy_over_budget_still_refuses(monkeypatch):
    fn, rungs = _ladder(monkeypatch, lambda *a, **k: _mh_quote(round_trip_pct=-60.0))
    assert fn(QUOTE_4663, MINT, 10 ** 16) is None
    assert [r for r in rungs if r["rung"] == "v4_multihop"][-1]["reason"].startswith(
        "round_trip_over_budget")


# --- 2. the route class comes from the chosen route --------------------------------------------

@pytest.mark.parametrize("route, cls", [
    ({"protocol": "uniswap_v2"}, "V2V3"),
    ({"protocol": "uniswap_v3"}, "V2V3"),
    ({"protocol": "pancake_v3"}, "V2V3"),
    ({"protocol": "uniswap_v4", "hooked": True}, "V4_HOOKED"),
    ({"protocol": "uniswap_v4"}, "V4"),
    ({"protocol": "uniswap_v4_multihop", "legs_path": [
        {"currency": "0xb", "key": {"hooks": ZERO}}, {"currency": "0xc", "key": {"hooks": ZERO}}]},
     "V4_MULTIHOP"),
    ({"protocol": "uniswap_v4_multihop", "legs_path": [
        {"currency": "0xb", "key": {"hooks": ZERO}}, {"currency": "0xc", "key": {"hooks": HOOK}}]},
     "V4_MULTIHOP_HOOKED"),
    ({"protocol": "uniswap_v4_multihop", "bridge_key": {"hooks": ZERO}, "coin_key": {"hooks": HOOK}},
     "V4_MULTIHOP_HOOKED"),
    ({"protocol": "uniswap_v4_multihop", "bridge_key": {"hooks": ZERO}, "coin_key": {"hooks": ZERO}},
     "V4_MULTIHOP"),
    ({"protocol": "uniswap_v4_multihop"}, None),          # legs unknown: never guessed hookless
    ({"protocol": "kyberswap"}, None),
    ({"protocol": "relay_same_chain"}, None),
    (None, None),
])
def test_the_route_class_reads_the_quotes_own_route(route, cls):
    assert risk.route_gas_class(route) == cls


# --- the units: per class, required where the flat knob under-prices -------------------------------

def _env(monkeypatch, **units):
    for name in ("", "_V2V3", "_V4", "_V4_HOOKED", "_V4_MULTIHOP", "_V4_MULTIHOP_HOOKED"):
        monkeypatch.delenv("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1" + name, raising=False)
    for name, value in units.items():
        suffix = "" if name == "FLAT" else "_" + name
        monkeypatch.setenv("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1" + suffix, str(value))


def test_v2v3_and_v4_fall_back_to_the_flat_knob(monkeypatch):
    _env(monkeypatch, FLAT=622791)
    assert cfg.evm_roundtrip_gas_units_for("1", route_class="V2V3") == 622791
    assert cfg.evm_roundtrip_gas_units_for("1", route_class="V4") == 622791
    _env(monkeypatch, FLAT=622791, V4=414240)
    assert cfg.evm_roundtrip_gas_units_for("1", route_class="V4") == 414240


@pytest.mark.parametrize("cls", ["V4_HOOKED", "V4_MULTIHOP", "V4_MULTIHOP_HOOKED"])
def test_hooked_and_multihop_knobs_are_required(monkeypatch, cls):
    _env(monkeypatch, FLAT=622791)
    assert cfg.evm_roundtrip_gas_units_for("1", route_class=cls) is None
    _env(monkeypatch, FLAT=622791, **{cls: 1051328})
    assert cfg.evm_roundtrip_gas_units_for("1", route_class=cls) == 1051328


def _guard(route, **kw):
    facts = {}
    why = risk.evm_gas_guard_refusal("1", gas_price_wei=int(1.6e9), clip_usd=35.0,
                                     native_usd=2500.0, facts=facts, route=route, **kw)
    return why, facts


def test_the_guard_prices_the_route_class(monkeypatch):
    _env(monkeypatch, FLAT=622791, V4_MULTIHOP=488436, V4_MULTIHOP_HOOKED=1051328)
    monkeypatch.setenv("MEMECOIN_MAX_ROUNDTRIP_COST_PCT_1", "100")
    hookless = {"protocol": "uniswap_v4_multihop", "bridge_key": {"hooks": ZERO}, "coin_key": {"hooks": ZERO}}
    hooked = {"protocol": "uniswap_v4_multihop", "bridge_key": {"hooks": ZERO}, "coin_key": {"hooks": HOOK}}
    assert _guard(hookless)[1]["units"] == 488436 and _guard(hookless)[1]["route_class"] == "V4_MULTIHOP"
    assert _guard(hooked)[1]["units"] == 1051328


def test_an_unknown_class_and_an_unset_required_knob_refuse(monkeypatch):
    _env(monkeypatch, FLAT=622791)
    why, _ = _guard({"protocol": "kyberswap"})
    assert why and "route class unknown" in why
    why, _ = _guard({"protocol": "uniswap_v4_multihop", "bridge_key": {"hooks": ZERO},
                     "coin_key": {"hooks": ZERO}})
    assert why and "MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1_V4_MULTIHOP" in why


def test_the_executor_hands_the_guard_its_route_and_rechecks_a_changed_class():
    import inspect
    from sniper.execution import evm_swap_executor as ex
    src = inspect.getsource(ex.execute_entry_evm)
    assert 'route=(quote or {}).get("route")' in src
    assert "gas_guard_class" in src and 'revision="class_change"' in src
    seam = inspect.getsource(seams.build_real_evm_executor)
    assert "_entry_gas_guard.takes_route = True" in seam


def test_a_winner_that_changes_class_after_the_guard_is_repriced_before_signing():
    """The guard priced a V4 route; the post-approval requote came back hooked multihop (2.2x the
    gas). It is re-priced and refused before the swap is signed; only the approval was spent."""
    from test_memecoin_evm_entry_executor import _QUOTE, _TraceRecorder, _c, _reserve, _seams
    from sniper.execution.evm_swap_executor import execute_entry_evm
    c = _c()
    pid = _reserve(c, network_id="1")
    first = dict(_QUOTE, route={"protocol": "uniswap_v4"})
    second = dict(_QUOTE, route={"protocol": "uniswap_v4_multihop", "bridge_key": {"hooks": ZERO},
                                 "coin_key": {"hooks": HOOK}})
    seams_, calls = _seams(quote=first)
    served = []

    def quote_fn(mint, usd, **kw):
        served.append(1)
        return first if len(served) == 1 else second
    seams_["quote_fn"] = quote_fn
    asked = []

    def guard(usd, facts=None, route=None):
        cls = risk.route_gas_class(route)
        facts["route_class"] = cls
        asked.append(cls)
        return "gas guard: hooked multihop over the cap" if cls == "V4_MULTIHOP_HOOKED" else None
    guard.fills_facts = True
    guard.takes_route = True
    trace = _TraceRecorder()
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="1", usd_amount=20.0, strategy="alpha_b",
        source_user_id="trader1", now_iso="2026-09-30T00:00:01Z", gas_guard_fn=guard, trace=trace,
        **seams_)
    assert asked == ["V4", "V4_MULTIHOP_HOOKED"], asked
    assert (result["status"], result["reason"]) == ("aborted", "gas guard: hooked multihop over the cap")
    assert calls["sign"] == 0 and calls["send"] == 0
    assert [m for n, m in trace.substages if n == "evm.gas_guard" and m.get("revision") == "class_change"]


def test_an_unchanged_class_is_not_asked_twice():
    from test_memecoin_evm_entry_executor import _QUOTE, _c, _reserve, _seams
    from sniper.execution.evm_swap_executor import execute_entry_evm
    c = _c()
    pid = _reserve(c, network_id="1")
    seams_, calls = _seams(quote=dict(_QUOTE, route={"protocol": "uniswap_v4"}))
    asked = []

    def guard(usd, facts=None, route=None):
        facts["route_class"] = risk.route_gas_class(route)
        asked.append(facts["route_class"])
    guard.fills_facts = True
    guard.takes_route = True
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="1", usd_amount=10.0, strategy="alpha_b",
        source_user_id="trader1", now_iso="2026-09-30T00:00:01Z", gas_guard_fn=guard, **seams_)
    assert asked == ["V4"] and result["status"] == "filled" and calls["send"] == 1
