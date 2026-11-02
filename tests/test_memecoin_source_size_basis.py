"""One source-size basis across lanes: what the trader PAID into the swap (2026-09-28, Alpha GO).

Census (docs/audits/2026-09-28-fast-lane-coverage-census.md): J7 reports what the trader paid,
while the on-chain and slot lanes decoded the pool leg NET of pump fees. Roveryaps' ~$199 buys
read $196.55 on the fast lanes and failed the $197 absolute floor that J7 passed; 5 of 13 J7
Solana fills since the address registry were refused this way. The absolute floor also had no
boundary tolerance, while the ratio floor had 2%. The source PRICE is untouched.
"""
import json
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from sniper.execution.pump_curve_buy import parse_trade_event, trade_event_gross_lamports
from sniper.execution.pumpswap_buy import buy_event_gross_quote, parse_buy_event
from sniper.services import memecoin_onchain_lane as lane
from sniper.services import memecoin_slot_lane as L
from sniper.services import memecoin_slot_signal as sig
from sniper.services.memecoin_real_runner import source_buy_band_reason, source_buy_family_reason
from sniper.settings import memecoin as cfg

FRAMES = json.loads((Path(__file__).parent / "fixtures/memecoin/slot_signal_frames.json").read_text())
CURVE_BUYER = "2SQarCLrLGA73Hwqfo5fegUbzff88uFMnEq8Y5jP65Yf"
AMM_SECOND_BUYER = "Hj8e8KNBbmvdUUVbTmnJTEEQ6mEi5PRf2BnjRDjQCLTF"

# Detection 2674, verbatim from the ledger: Roveryaps' PumpSwap buy 46hQdz8Qnv, 2026-09-27 15:43Z.
# The on-chain lane refused it "source buy $197 under absolute floor $197"; J7 filled the same buy.
ROVERYAPS = {
    "signature": "46hQdz8Qnv3y1twNmK9Uim6LdMycx2wBqrjJ7SZRPgPgqqKDVMUx6HX8urPSB2bnrKGhkiBmjLmLUG512TUcW8Zq",
    "source_user_id": "9e953765-5056-5868-98dc-590e46d24cab", "handle": "Roveryaps",
    "network_id": "solana", "mint": "4kL41egpnLvEBFq5GnTa74oLMwMhjnK4C1rqXf8qpump",
    "source_size_usd": 196.55081589659056, "source_price": 1.2831583401065553e-05,
    "detected_at": "2026-09-27T15:43:02.733048+00:00",
    "source_valuation_json": json.dumps({
        "source_size_usd": 196.55081589659056, "basis": "pump_event_net_sol",
        "gross_quote_atomic": "1636489041", "net_pool_quote_atomic": "1616285471",
        "program_id": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA", "instruction_name": "buy",
        "sol_usd": 121.606498, "gross_swap_size_usd": 199.0077012913884}),
}


@pytest.fixture(autouse=True)
def _basis(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOURCE_SIZE_BASIS", "gross", raising=False)


def _floor(their_usd, *, abs_min=197.0, tolerance=0.0):
    reason, _info = source_buy_family_reason(
        their_usd=their_usd, source_mcap=None, prior_net_usd=None, ratio=0.0,
        unknown_mcap_floor=0.0, abs_min=abs_min, abs_max=0.0, family_active=False,
        floor_tolerance_pct=tolerance)
    return reason


def test_the_onchain_lane_judges_roveryaps_on_what_he_paid():
    ev = lane.OnchainEv(ROVERYAPS)
    assert ev.their_size_usd == pytest.approx(199.0077012913884)
    assert ev.their_price == ROVERYAPS["source_price"], "the source price must not move"
    assert _floor(ev.their_size_usd) is None
    assert _floor(ROVERYAPS["source_size_usd"]) == "source buy $197 under absolute floor $197"


def test_the_net_knob_restores_the_pool_leg(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOURCE_SIZE_BASIS", "net")
    assert lane.OnchainEv(ROVERYAPS).their_size_usd == ROVERYAPS["source_size_usd"]


@pytest.mark.parametrize("valuation", [
    None, "not json", json.dumps({"basis": "route_owner_quote_out", "gross_swap_size_usd": 250.0}),
    json.dumps({"basis": "pump_event_net_sol"}),
    json.dumps({"basis": "pump_event_net_sol", "gross_swap_size_usd": 150.0}),     # below net
    json.dumps({"basis": "pump_event_net_sol", "gross_swap_size_usd": -1}),
])
def test_anything_but_a_readable_gross_keeps_the_stored_size(valuation):
    row = dict(ROVERYAPS, source_valuation_json=valuation)
    assert lane.paid_size_usd(row) == ROVERYAPS["source_size_usd"]


def test_a_row_without_the_valuation_column_keeps_the_stored_size():
    row = {k: v for k, v in ROVERYAPS.items() if k != "source_valuation_json"}
    assert lane.paid_size_usd(row) == ROVERYAPS["source_size_usd"]


def _curve_buy():
    return sig.decode_owner_buy(FRAMES["curve_buy"], CURVE_BUYER,
                                bytes(Pubkey.from_string(CURVE_BUYER)), "u1", "h",
                                recv_monotonic=1.0, recv_utc="2026-09-26T20:26:05Z")


def test_the_slot_lane_curve_size_is_the_paid_lamports_of_the_real_event():
    buy = _curve_buy()
    event = parse_trade_event(buy.trade_event_log)
    paid = event["sol_amount"] + event["fee"] + event["creator_fee"]
    assert paid > buy.sol_amount == 98765431
    assert trade_event_gross_lamports(event) == paid
    _reason, facts = L.admission_refusal(buy, armed_ids={"u1"}, sol_usd=120.0, kill_tripped=False)
    assert facts["their_usd"] == pytest.approx(paid / 1e9 * 120.0)
    assert facts["their_usd_basis"] == "gross"


def test_the_slot_lane_curve_size_on_the_net_knob(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOURCE_SIZE_BASIS", "net")
    _reason, facts = L.admission_refusal(_curve_buy(), armed_ids={"u1"}, sol_usd=120.0,
                                         kill_tripped=False)
    assert facts["their_usd"] == pytest.approx(98765431 / 1e9 * 120.0)


def test_the_real_pumpswap_event_gives_its_paid_quote():
    buy = sig.decode_owner_buy(FRAMES["amm_buy"], AMM_SECOND_BUYER,
                               bytes(Pubkey.from_string(AMM_SECOND_BUYER)), "u1", "h",
                               recv_monotonic=1.0, recv_utc="2026-09-26T20:26:05Z")
    event = parse_buy_event(buy.trade_event_log)
    gross = buy_event_gross_quote(event)
    net = int(event["quote_amount_in_with_lp_fee"]) - int(event["lp_fee"])
    assert gross == (int(event["quote_amount_in_with_lp_fee"]) + int(event["protocol_fee"])
                     + int(event["coin_creator_fee"]))
    assert gross > net
    assert L.paid_quote_atomic(net, gross) == (gross, "gross")
    assert L.paid_quote_atomic(net, None) == (net, "net")
    assert L.paid_quote_atomic(net, net - 1) == (net, "net")
    broken = dict(event, protocol_fee=int(event["protocol_fee"]) + 1)
    assert buy_event_gross_quote(broken) is None, "a self-contradicting event is not a size"


def test_the_absolute_floor_takes_the_ratio_floors_tolerance():
    assert source_buy_band_reason(193.07, 197.0, 0.0, 2.0) is None
    assert source_buy_band_reason(193.05, 197.0, 0.0, 2.0) == \
        "source buy $193 under absolute floor $197"
    assert source_buy_band_reason(196.99, 197.0, 0.0) == "source buy $197 under absolute floor $197"
    assert source_buy_band_reason(197.5, 197.0, 0.0, -5.0) is None, "a negative tolerance never tightens"
    _reason, info = source_buy_family_reason(
        their_usd=193.07, source_mcap=None, prior_net_usd=None, ratio=0.0, unknown_mcap_floor=0.0,
        abs_min=197.0, abs_max=0.0, family_active=False, floor_tolerance_pct=2.0)
    assert _reason is None and info["effective_abs_floor_usd"] == pytest.approx(193.06)
