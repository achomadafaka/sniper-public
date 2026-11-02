"""Desk producer U (spec 2026-10-21-desk-universe-producer-design.md): pure decisions."""
import base64

import pytest

from sniper.services import memecoin_desk_universe as U

MINT = "Mint111111111111111111111111111111111111111"
CURVE = "Curve11111111111111111111111111111111111111"
SOL = 150.0


def reserves_for_mcap(mcap_usd, sol_usd=SOL, supply_tokens=1e9, vt_atomic=500_000_000_000_000):
    """virtual_sol (lamports) that puts the curve at mcap_usd for a fixed virtual_token."""
    price_sol = mcap_usd / supply_tokens / sol_usd
    return int(price_sol * (vt_atomic / 1e6) * 1e9), vt_atomic


def trade(book, mcap, slot, *, t=100.0, sig="sig", ordinal=0, sol_usd=SOL):
    vq, vt = reserves_for_mcap(mcap, sol_usd)
    return book.on_trade(mint=MINT, curve=CURVE, virtual_quote=vq, virtual_token=vt, slot=slot,
                         signature=sig + str(slot), ordinal=ordinal, now=t, sol_usd=sol_usd,
                         sol_usd_at=t)


def test_mcap_math_matches_hand_computed_reserves():
    # 30 SOL virtual vs 1,073,000,000 tokens virtual (6 dp): price 2.796e-8 SOL/token
    m = U.curve_mcap_usd(30_000_000_000, 1_073_000_000_000_000, supply_tokens=1e9, sol_usd=150.0)
    assert m == pytest.approx(30 / 1_073_000_000 * 1e9 * 150.0)
    assert U.curve_mcap_usd(0, 1, supply_tokens=1e9, sol_usd=150.0) is None
    assert U.curve_mcap_usd(1, 0, supply_tokens=1e9, sol_usd=150.0) is None
    assert U.curve_mcap_usd(1, 1, supply_tokens=1e9, sol_usd=None) is None


def test_band_entry_emits_once_then_needs_exit_and_cooldown():
    book = U.CurveBook(band=(30_000, 100_000), cooldown_s=60)
    assert trade(book, 20_000, 1, t=0) is None           # below band
    e = trade(book, 35_000, 2, t=1)
    assert e is not None and e.mint == MINT and e.band_entry_slot == 2
    assert trade(book, 50_000, 3, t=2) is None           # still inside: no re-emit
    assert trade(book, 120_000, 4, t=3) is None          # left above
    assert trade(book, 90_000, 5, t=10) is None          # re-entry inside cooldown
    assert trade(book, 150_000, 6, t=20) is None
    assert trade(book, 80_000, 7, t=70) is not None      # re-entry after cooldown


def test_first_sight_inside_band_is_not_an_entry():
    """Without a prior out-of-band observation we never saw it cross, so no emission."""
    book = U.CurveBook(band=(30_000, 100_000), cooldown_s=60)
    assert trade(book, 40_000, 1) is None


def test_gap_makes_state_unknown_and_blocks_emission_until_seen_outside():
    book = U.CurveBook(band=(30_000, 100_000), cooldown_s=0)
    trade(book, 20_000, 1, t=0)
    book.mark_unknown("disconnect")
    assert book.state(MINT) == "unknown"
    assert trade(book, 40_000, 2, t=1) is None           # cannot prove a crossing across the gap
    trade(book, 20_000, 3, t=2)
    assert trade(book, 40_000, 4, t=3) is not None


def test_stale_sol_price_gives_null_mcap_and_no_emission():
    book = U.CurveBook(band=(30_000, 100_000), cooldown_s=0, sol_max_age_s=120)
    trade(book, 20_000, 1, t=0)
    vq, vt = reserves_for_mcap(40_000)
    e = book.on_trade(mint=MINT, curve=CURVE, virtual_quote=vq, virtual_token=vt, slot=2,
                      signature="s2", ordinal=0, now=500.0, sol_usd=SOL, sol_usd_at=100.0)
    assert e is None and book.last_mcap(MINT) is None


def test_graduated_mint_never_emits():
    book = U.CurveBook(band=(30_000, 100_000), cooldown_s=0)
    trade(book, 20_000, 1)
    book.on_graduated(MINT)
    assert trade(book, 40_000, 2) is None and book.state(MINT) == "graduated"


def test_bars_bucket_by_block_time_and_mark_missing_time():
    bars = U.BarBuilder(max_pools=2)
    assert bars.watch(CURVE)
    bars.set_block_time(10, 1000)
    bars.set_block_time(11, 1000)
    bars.set_block_time(12, 1001)
    bars.add_swap(CURVE, slot=10, price=1.0, quote_usd=5.0, is_buy=True, maker="a")
    bars.add_swap(CURVE, slot=11, price=1.2, quote_usd=3.0, is_buy=False, maker="b")
    bars.add_swap(CURVE, slot=12, price=0.9, quote_usd=2.0, is_buy=True, maker="a")
    bars.add_swap(CURVE, slot=13, price=0.8, quote_usd=1.0, is_buy=True, maker="c")   # no time
    out = bars.closed_bars(CURVE, upto_slot=13)
    first = next(b for b in out if b["t"] == 1000)
    assert (first["o"], first["h"], first["l"], first["c"]) == (1.0, 1.2, 1.0, 1.2)
    assert first["buy_usd"] == 5.0 and first["sell_usd"] == 3.0 and first["n_swaps"] == 2
    assert first["n_makers"] == 2 and (first["slot_lo"], first["slot_hi"]) == (10, 11)
    assert bars.insufficient(CURVE) == [13]               # never guessed into a second
    assert all(b["t"] in (1000, 1001) for b in out)


def test_watch_set_is_bounded():
    bars = U.BarBuilder(max_pools=1)
    assert bars.watch("p1") is True
    assert bars.watch("p2") is False
    assert bars.add_swap("p2", slot=1, price=1, quote_usd=1, is_buy=True, maker="x") is False


def test_snapshot_fields_carry_provenance_and_known_at_not_after_snapshot():
    snap = U.build_snapshot(
        emission_at=100.0,
        fields={"mcap_usd": U.Field(41000.0, "pumpfun_grpc:curve_reserves", 99.5, 7, "complete"),
                "flow_60s": U.Field(None, "pumpfun_grpc:bars", 99.5, 7, "partial"),
                "late": U.Field(1, "x", 101.0, 8, "complete")})
    assert snap["fields"]["mcap_usd"] == {"value": 41000.0, "source": "pumpfun_grpc:curve_reserves",
                                          "known_at": 99.5, "slot": 7, "completeness": "complete"}
    assert snap["fields"]["late"]["value"] is None                     # known after the snapshot
    assert snap["fields"]["late"]["completeness"] == "window_not_elapsed"
    assert set(snap["missing"]) == {"flow_60s", "late"}
    assert snap["hash"] == U.build_snapshot(emission_at=100.0, fields={
        "mcap_usd": U.Field(41000.0, "pumpfun_grpc:curve_reserves", 99.5, 7, "complete"),
        "flow_60s": U.Field(None, "pumpfun_grpc:bars", 99.5, 7, "partial"),
        "late": U.Field(1, "x", 101.0, 8, "complete")})["hash"]


def test_cpi_event_extraction_prefers_inner_instructions_then_logs():
    payload = b"\x01\x02\x03"
    keys = [b"\x00" * 32, U.b58decode(U.PUMP_PROGRAM)]
    inner = [(2, [(1, U.CPI_EVENT_PREFIX + payload)])]
    got = U.pump_event_lines(account_keys=keys, inner=inner, logs=[])
    assert got == [((2 << 16) | 0, "Program data: " + base64.b64encode(payload).decode())]
    logs = ["Program %s invoke [1]" % U.PUMP_PROGRAM, "Program data: AQID",
            "Program %s success" % U.PUMP_PROGRAM]
    assert U.pump_event_lines(account_keys=keys, inner=[], logs=logs) == [(1, "Program data: AQID")]


def test_ticket_id_is_stable_and_identity_bound():
    a = U.ticket_id("solana", MINT, 7, "sigA", 0)
    assert a == U.ticket_id("solana", MINT, 7, "sigA", 0) and a.startswith("desk:U:")
    assert a != U.ticket_id("solana", MINT, 7, "sigA", 1)


def test_bonding_curve_pda_matches_known_pair():
    # ELEVEN (position 70402); PDA checked with solders find_program_address on 2026-10-21
    assert U.bonding_curve("EV1KmUuaxS1Erau822CfFN58JuewtuiVamq3tYuBpump") == "EpocFnC1TCP7V3tUmdwxM5SFRFRpGcJ9K7SvEQLQGeCY"

