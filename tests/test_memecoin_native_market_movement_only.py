"""Source movement policy never turns a native size/fee cost into market movement."""
from fractions import Fraction

import pytest

from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_rh_fast_decide as decide
from sniper.services import memecoin_rh_fast_lane as rh
from sniper.settings import memecoin as cfg


@pytest.fixture
def movement(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_SLIPPAGE_PCT", 3.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)


def _payload(kind="v4_native"):
    from rh_source_fill_fixture import proof_for
    from sniper.services.memecoin_evm_v4_events import v4_pool_id
    key = dict(currency0="0x" + "00" * 20, currency1="0x" + "11" * 20,
               fee=3000, tick_spacing=200, hooks="0x" + "00" * 20)
    if kind == "v4_weth":
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        key["currency0"] = CHAIN_QUOTE["4663"]["token"]
    payload = dict(chain="4663", tx_hash="0x" + "cc" * 32, seq=1000, wallet="0x" + "ef" * 20,
                route_kind=kind, key=key, mint=key["currency1"], pool=v4_pool_id(key),
                cap_num=1, cap_den=1, post_sqrt=1 << 96, state_liquidity=10**18,
                in_is_c0=True, source_user_id="source", eth_usd=1.0,
                src_in_wei=10**18, src_out=10**18, paid_usd=1.0)
    payload["source_fill"] = proof_for(payload)
    payload["source_fill"]["source_post"]["liquidity_atomic"] = str(payload["state_liquidity"])
    return payload


def _bsc_payload():
    return dict(r_quote=10**18, r_coin=10**18, tax_num=0, tax_den=1,
                src_in=10**18, src_out=1, cap_num=1, cap_den=1, hop=None)


def test_native_copy_floor_is_not_an_average_fill_gate(movement):
    live = object.__new__(rh.LiveLane)
    # A tiny/unknown source average cannot raise or reject the independent output bound.
    for average in (None, 1e-100):
        assert live._copy_floor(_payload(), {"source_price": average}, 10**18, 18) == (0, None)


@pytest.mark.parametrize("kind", ["v4_native", "v4_weth"])
def test_native_signed_minimum_prices_our_size_then_applies_only_slippage(movement, monkeypatch, kind):
    payload = _payload(kind)
    if kind == "v4_bridge":
        payload.update(bridge="0x" + "22" * 20, a_key=payload["key"],
                       a_sqrt=1 << 96, a_state_liquidity=10**18, x_is_c0=True)
    sent = []
    from sniper.execution import evm_v4_multihop_tx as multi
    from sniper.execution import evm_v4_quote as v4
    monkeypatch.setattr(v4, "native_buy_tx", lambda *a, **k: sent.append(a) or {"data": "0x"})
    monkeypatch.setattr(v4, "weth_buy_tx", lambda *a, **k: sent.append(a) or {"data": "0x"})
    monkeypatch.setattr(multi, "native_multihop_buy_tx", lambda *a, **k: sent.append(a) or {"data": "0x"})
    row = dict(sqrt=payload["post_sqrt"], liquidity=payload["state_liquidity"])
    a_row = dict(sqrt=payload["a_sqrt"], liquidity=payload["a_state_liquidity"]) if kind == "v4_bridge" else None
    expected = decide.source_pool_est_out(10**18, row, payload["key"], True,
                                          a_row, payload.get("a_key"))
    # Our own size impact exceeds 35%; it must not become a source-relative rejection.
    assert expected < 10**18 / 1.35
    _tx, minimum = rh.lane_buy_tx(payload, 10**18, 123, "0x" + "33" * 20)
    assert minimum == expected * 97 // 100 > 0
    assert sent


def test_unknown_movement_does_not_authorize_an_unknown_output_quote(movement):
    payload = _payload()
    payload.pop("state_liquidity")
    with pytest.raises(ValueError, match="output.*unavailable"):
        rh.lane_buy_tx(payload, 10**18, 123, "0x" + "33" * 20)


@pytest.mark.parametrize("field,value", [("state_liquidity", None), ("state_liquidity", True),
                                         ("state_liquidity", "12"), ("state_liquidity", 0),
                                         ("post_sqrt", False), ("post_sqrt", -1)])
def test_malformed_output_state_does_not_sign(movement, field, value):
    with pytest.raises(ValueError, match="output.*unavailable"):
        rh.quote_slippage_floor(dict(_payload(), **{field: value}), 10**18)


@pytest.mark.parametrize("change", [{"fee": 0x800000}, {"fee": True}, {"fee": -1},
                                  {"hooks": "0x" + "77" * 20}])
def test_unmodelled_output_fee_does_not_borrow_the_old_source_cap(movement, change):
    payload = _payload()
    payload["key"] = dict(payload["key"], **change)
    with pytest.raises(ValueError, match="output.*unavailable"):
        rh.quote_slippage_floor(payload, 10**18)


def test_bridge_output_with_no_receipt_bound_first_leg_does_not_sign(movement):
    payload = _payload("v4_bridge")
    payload.update(a_key=payload["key"], a_sqrt=1 << 96, a_state_liquidity=10**18,
                   post_sqrt=2 << 96, x_is_c0=False)
    with pytest.raises(ValueError, match="output.*unavailable"):
        rh.quote_slippage_floor(payload, 10**18)


@pytest.mark.parametrize("change", ["missing", "forged", "wrong_sqrt", "wrong_pool"])
def test_output_state_must_match_the_receipt_proof(movement, change):
    payload = _payload()
    if change == "missing":
        payload["source_fill"]["source_post"].pop("liquidity_atomic")
    elif change == "forged":
        payload["state_liquidity"] *= 1000
    elif change == "wrong_sqrt":
        payload["source_fill"]["source_post"]["sqrt_price_x96"] = str(2 << 96)
    else:
        payload["source_fill"]["source_post"]["pool"] = "0x" + "aa" * 32
    with pytest.raises(ValueError, match="output.*unavailable"):
        rh.quote_slippage_floor(payload, 10**18)


def test_native_metadata_is_exact_post_price_without_the_old_average_cap(movement, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.5)
    payload = _payload()
    payload["source_fill"]["acquired_atomic"] = str(10**30)
    payload["src_out"] = 10**30
    assert rh.native_source_price_matches(payload, payload["source_fill"])
    payload["cap_num"] = 2
    assert not rh.native_source_price_matches(payload, payload["source_fill"])


@pytest.mark.parametrize("at_cap", [False, True])
def test_bsc_private_cap_mode_cannot_restore_source_average_floor(movement, at_cap):
    payload = _bsc_payload()
    expected = bsc.expected_out_for(10**18, payload)
    assert expected < 10**18 / 1.35
    assert bsc.min_out_for(10**18, payload, floor_at_cap=at_cap) == expected * 97 // 100


def test_bsc_follow_source_cap_stands_down_in_movement_mode(movement):
    assert bsc.follow_lane_chase_cap(_bsc_payload()) is None


def test_decider_cap_metadata_does_not_depend_on_source_average(movement):
    post = Fraction(1, 2)
    assert decide.cap_price(post, 10**18, 1, chase_pct=30, premium=1.5) == post
    assert bsc.bsc_price_cap(1, 2, 10**18, 1, chase_pct=30, premium=1.5) == post


def test_existing_watcher_liquidity_crosses_the_native_datagram_without_a_read():
    from test_memecoin_rh_fast_lane import REC, OUT
    payload = rh.lane_payload(REC, dict(OUT, state_liquidity=17, a_state_liquidity=23), sent_epoch=1.0)
    assert (payload["state_liquidity"], payload["a_state_liquidity"]) == (17, 23)


def test_base_receipt_liquidity_is_retained_for_the_shared_builder(movement, monkeypatch):
    from test_memecoin_base_fast_lane_datagram import _frame, _hook
    monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE", "runner_shadow")
    monkeypatch.setattr(cfg, "MEMECOIN_BASE_FAST_ROUTES", ("v4_native", "v4_weth"))
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", ("8453",))
    sent = []
    row, tx, receipt = _frame()
    rec = _hook(sent)(row, tx, receipt, 2500.0, 1.0, "cache")
    assert rec["reason"] == "would_send" and sent
    assert sent[0]["state_liquidity"] == int(rec["liquidity"]) > 0


def test_native_unknown_movement_proceeds_with_a_real_signed_slippage_minimum(movement, tmp_path):
    from test_memecoin_rh_fast_live import PAYLOAD, Chain, _accepting_sender, _ledger, _live, _run
    payload = dict(PAYLOAD, state_liquidity=10**30,
                   latest_same_pool={"canonicality": "unverified_background", "sqrt": 1,
                                     "observed": 1005.0})
    import copy
    payload["source_fill"] = copy.deepcopy(PAYLOAD["source_fill"])
    payload["source_fill"]["source_post"]["liquidity_atomic"] = str(payload["state_liquidity"])
    post = Fraction(1 << 192, payload["post_sqrt"]**2)
    payload["cap_num"], payload["cap_den"] = post.numerator, post.denominator
    minimum = rh.quote_slippage_floor(payload, 2 * 10**16)
    conn, chain = _ledger(), Chain(got=minimum)
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path))
    result, rec = _run(live, conn, payload=payload)
    assert result["status"] == "filled", result
    assert rec["copy_floor"] == 0 and rec["min_out"] == minimum > 0
    assert format(minimum, "064x") in chain.raw
    market = rec["execution_evidence"]["copy_market_movement"]
    assert market["state"] == "unknown" and market["unknown_action"] == "proceed"
    assert rec["execution_evidence"]["signed_floors"]["binding"] == ["slippage"]


def test_native_missing_output_state_releases_unsigned_after_unknown_movement(movement, tmp_path):
    from test_memecoin_rh_fast_live import Chain, _accepting_sender, _ledger, _live, _run
    conn, chain = _ledger(), Chain()
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path))
    result, rec = _run(live, conn)
    assert result["status"] == "aborted" and rec["_outcome"] == "build_refused"
    assert not live.sender.sent
    assert rec["execution_evidence"]["copy_market_movement"]["unknown_action"] == "proceed"


def test_legacy_native_cap_keeps_its_original_floor(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False)
    from sniper.execution import evm_v4_quote as v4
    monkeypatch.setattr(v4, "native_buy_tx", lambda *a, **k: {})
    _tx, floor = rh.lane_buy_tx(_payload(), 10**18, 123, "0x" + "33" * 20)
    assert floor == 10**18
