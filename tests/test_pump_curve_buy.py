"""pump.fun curve buy: instruction bytes, account order and curve math against a LIVE buy.

The fixture is a real mainnet transaction (tests/fixtures/pump_curve_buy_frames.json, read
2026-09-26): a direct buy_exact_sol_in and its TradeEvent. A synthesised frame would only assert
this module's own assumptions back at it.
"""
import json
from pathlib import Path

import pytest

from sniper.execution import pump_curve_buy as pcb

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "pump_curve_buy_frames.json").read_text())
LIVE = FIXTURE["direct_buy_exact_sol_in"]
GVA = 12   # global_volume_accumulator


def _event():
    return pcb.parse_trade_event(LIVE["trade_event_log"])


def _pre_trade(event):
    """The reserves the live buyer priced against: post-trade reserves minus its own trade."""
    pre = dict(event)
    pre["virtual_sol_reserves"] = event["virtual_sol_reserves"] - event["sol_amount"]
    pre["virtual_token_reserves"] = event["virtual_token_reserves"] + event["token_amount"]
    pre["real_token_reserves"] = event["real_token_reserves"] + event["token_amount"]
    return pre


def test_account_list_matches_the_live_buy_in_order():
    ev, accounts = _event(), LIVE["accounts"]
    ours = pcb.curve_buy_accounts(
        user=accounts[6][0], mint=ev["mint"], token_program=accounts[8][0], creator=ev["creator"],
        fee_recipient=ev["fee_recipient"], buyback_fee_recipient=accounts[17][0])
    assert len(ours) == len(accounts) == 18          # the IDL names 16; live buys carry 2 more
    # [5] associated_user: this live buyer received into a token account that is NOT its canonical
    # ATA (the program accepts any account of the user's for the mint). We always use the ATA,
    # which the create-idempotent instruction guarantees exists.
    assert str(ours[5][0]) == str(pcb.ata(accounts[6][0], ev["mint"], accounts[8][0]))
    assert [str(k) for i, (k, _s, _w) in enumerate(ours) if i != 5] == [
        a[0] for i, a in enumerate(accounts) if i != 5]
    assert [s for _k, s, _w in ours] == [a[1] for a in accounts]
    # Writability matches the live buy on every account but the global volume accumulator: the IDL
    # marks it read-only and the live client write-locks it. Read-only simulated clean with
    # track_volume false, and it keeps our buy out of that hot account's per-block write-lock budget.
    for i, ((_k, _s, w), live) in enumerate(zip(ours, accounts)):
        assert w == (False if i == GVA else live[2]), i


def test_instruction_data_matches_the_live_buy():
    live = bytes.fromhex(LIVE["data_hex"])            # 24 bytes: the live client omits OptionBool
    spend, floor = int.from_bytes(live[8:16], "little"), int.from_bytes(live[16:24], "little")
    ours = pcb.buy_exact_sol_in_data(spend, floor)
    assert ours[:24] == live and ours[24:] == b"\x00" and len(ours) == 25


def test_curve_math_is_exact_on_the_live_buy():
    ev = _event()
    spend = int.from_bytes(bytes.fromhex(LIVE["data_hex"])[8:16], "little")
    assert spend == ev["sol_amount"] + ev["fee"] + ev["creator_fee"]   # the fee split, as booked
    assert pcb.expected_tokens_out(spend, _pre_trade(ev)) == ev["token_amount"]


def test_a_buyback_share_is_not_a_fee():
    # Buyback is a split of the protocol fee (Global.buyback_basis_points 5000). Counting it as a
    # fee would price a 50% fee and set the floor at roughly two thirds of the real output.
    ev = _event()
    spend = int.from_bytes(bytes.fromhex(LIVE["data_hex"])[8:16], "little")
    with_share = {**_pre_trade(ev), "buyback_fee_basis_points": 5000}
    assert pcb.expected_tokens_out(spend, with_share) == ev["token_amount"]


def test_trade_event_fields_the_buy_depends_on():
    ev = _event()
    assert ev["is_buy"] and ev["ix_name"] == "buy_exact_sol_in"
    assert ev["quote_mint"] in pcb.SOL_QUOTE_MINTS
    assert (ev["fee_basis_points"], ev["creator_fee_basis_points"]) == (95, 30)


def test_token_program_is_read_inside_the_pump_cpi():
    # A router moves a classic-program token first; the coin itself is Token-2022.
    logs = FIXTURE["router_v2_buy_logs"]["logs"]
    first = next(l for l in logs if " invoke [" in l and ("Tokenkeg" in l or "TokenzQd" in l))
    assert pcb.TOKEN_PROGRAM in first
    assert pcb.token_program_from_logs(logs) == pcb.TOKEN_2022_PROGRAM
    assert pcb.token_program_from_logs(LIVE["logs"]) == pcb.TOKEN_2022_PROGRAM


def test_instructions_refuse_what_the_curve_cannot_take():
    ev, accounts = _event(), LIVE["accounts"]
    kw = dict(user=accounts[6][0], token_program=accounts[8][0],
              buyback_fee_recipient=accounts[17][0])
    ixs, amounts = pcb.curve_buy_instructions(ev, 3_000_000, 10.0, **kw)
    assert [str(i.program_id) for i in ixs] == [pcb.ATA_PROGRAM, pcb.PUMP_PROGRAM]
    assert bytes(ixs[0].data) == b"\x01"                               # CreateIdempotent
    assert amounts["min_tokens_out"] == int(amounts["expected_tokens_out"] * 0.9)
    with pytest.raises(ValueError):
        pcb.curve_buy_instructions({**ev, "quote_mint": pcb.USDC_MINT}, 3_000_000, 10.0, **kw)
    with pytest.raises(ValueError):
        pcb.curve_buy_instructions({**ev, "is_buy": False}, 3_000_000, 10.0, **kw)
    with pytest.raises(ValueError, match="complete"):
        pcb.curve_buy_instructions({**ev, "real_token_reserves": 0}, 3_000_000, 10.0, **kw)
    with pytest.raises(ValueError):
        pcb.curve_buy_instructions(ev, 3_000_000, 10.0, **{**kw, "token_program": pcb.SYSTEM_PROGRAM})


# --- the slot-lane adapter ----------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

LIGHTHOUSE_PROGRAM = "L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95"   # the removed slot guard's program; must never appear

COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
BLOCKHASH = "11111111111111111111111111111111"


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _builder(clock, clip_usd=5.0, **kw):
    b = pcb.SlotLaneCurveBuilder(user=LIVE["accounts"][6][0], jup_call=lambda *a, **k: None,
                                 rpc=lambda *a: None, clock=clock, **kw)
    b._buyback = LIVE["accounts"][17][0]
    # The leg is not under test here: an empty instruction list with a real minimum-out amount.
    b._leg = {"ixs": [], "tables": [], "usdc_atomic": pcb.clip_atomic(clip_usd),
              "min_out_lamports": 30_000_000, "out_lamports": 30_400_000, "fetched_at": clock()}
    return b


def _buy(**over):
    ev = _event()
    fields = {"mint": ev["mint"], "token_program": LIVE["accounts"][8][0],
              "trade_event_log": LIVE["trade_event_log"]}
    fields.update(over)
    return SimpleNamespace(**fields)


SNAP = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=450_780_600, sol_usd=175.0)


def test_adapter_builds_unsigned_message_with_no_slot_guard(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 10.0)    # read at call time
    clock = _Clock()
    b = _builder(clock)
    message, validity = b.build(_buy(), 5.0, SNAP)
    assert validity["blockhash"] == BLOCKHASH and validity["last_valid_height"] == 450_780_600
    assert validity["pump_price_limit"]["min_tokens_atomic"] == b.last_build["min_tokens_out"]
    keys = [str(k) for k in message.account_keys]
    assert keys[0] == LIVE["accounts"][6][0]                           # payer = user
    programs = [keys[ix.program_id_index] for ix in message.instructions]
    assert programs == [COMPUTE_BUDGET, COMPUTE_BUDGET,
                        pcb.ATA_PROGRAM, pcb.PUMP_PROGRAM]
    cu_limit = int.from_bytes(bytes(message.instructions[0].data)[1:5], "little")
    assert cu_limit == 400_000                  # 401,016 while the guard's 1,016 CU was added
    assert LIGHTHOUSE_PROGRAM not in keys                              # soft deadline: no slot guard
    assert b.last_build["min_tokens_out"] == max(
        b.last_build["output_floor_atomic"], b.last_build["source_price_floor_atomic"])
    assert b.last_build["spend_lamports"] == 30_000_000 and b.last_build["bytes"] <= 1232


def test_native_funding_builds_the_same_target_without_a_conversion_leg(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 10.0)
    b = _builder(_Clock())
    b._leg = None
    message, validity = b.build(_buy(), 5.0, SNAP, funding_lamports=28_000_000)
    assert message is not None
    keys = [str(k) for k in message.account_keys]
    assert [keys[ix.program_id_index] for ix in message.instructions] == [
        COMPUTE_BUDGET, COMPUTE_BUDGET, pcb.ATA_PROGRAM, pcb.PUMP_PROGRAM]
    assert validity["route_target"] == pcb.curve_route_target(_event()["mint"], {"quote": {}})
    assert validity["funding_asset"] == "sol"
    assert validity["funding_amount_atomic"] == 28_000_000
    assert b.last_build["min_tokens_out"] == max(
        b.last_build["output_floor_atomic"], b.last_build["source_price_floor_atomic"])


@pytest.mark.parametrize("case, reason", [
    ("no_leg", "leg_stale"), ("aged_leg", "leg_stale"), ("clip_scaled_down", "leg_over_clip"),
    ("clip_scaled_up", "leg_under_clip"),
    ("no_token_program", "token_program_unknown"), ("no_event", "no_trade_event"),
    ("other_mint", "event_mint_mismatch"), ("no_blockhash", "no_blockhash"),
    ("complete_curve", "curve is complete"), ("no_buyback", "no_buyback_recipient"),
])
def test_adapter_refusals(case, reason, monkeypatch):
    clock = _Clock()
    b, buy, snap, clip = _builder(clock), _buy(), SNAP, 5.0
    if case == "no_leg":
        b._leg = None
    elif case == "aged_leg":
        clock.t += pcb.MAX_LEG_AGE_S + 0.01
    elif case == "clip_scaled_down":
        clip = 4.99
    elif case == "clip_scaled_up":
        clip = 5.0 / 0.75          # a leg sized for a 0.75-scaled clip against a full approval
    elif case == "no_token_program":
        buy = _buy(token_program=None)
    elif case == "no_event":
        buy = _buy(trade_event_log=None)
    elif case == "other_mint":
        buy = _buy(mint="So11111111111111111111111111111111111111112")
    elif case == "no_blockhash":
        snap = SimpleNamespace(blockhash=None, last_valid_height=None)
    elif case == "complete_curve":
        real = pcb.parse_trade_event
        monkeypatch.setattr(pcb, "parse_trade_event",
                            lambda line: {**real(line), "real_token_reserves": 0})
    elif case == "no_buyback":
        b._buyback = None
    msg, got = b.build(buy, clip, snap)
    assert msg is None and reason in got


def test_refresh_requotes_only_when_old_or_the_clip_changed():
    import base64
    import json as _json
    calls = []

    def jup(method, path, body=None):
        calls.append(method)
        if method == "GET":
            return 200, {"inAmount": "5000000", "outAmount": "30400000",
                         "otherAmountThreshold": "30000000", "routePlan": []}
        return 200, {"swapInstruction": {"programId": JUP, "accounts": [],
                                         "data": base64.b64encode(b"\x00").decode()},
                     "addressLookupTableAddresses": []}

    global_raw = bytearray(1087)
    from solders.pubkey import Pubkey
    global_raw[:8] = pcb.GLOBAL_DISCRIMINATOR
    global_raw[8] = 1
    for offset in (pcb.GLOBAL_FEE_RECIPIENT_OFFSET, pcb.GLOBAL_RESERVED_FEE_RECIPIENT_OFFSET):
        global_raw[offset:offset + 32] = bytes(Pubkey.from_string(_event()["fee_recipient"]))
    global_raw[933:965] = bytes(Pubkey.from_string(LIVE["accounts"][17][0]))
    rpc = lambda method, params: {"value": {"owner": pcb.PUMP_PROGRAM, "executable": False,
        "data": [base64.b64encode(bytes(global_raw)).decode(), "base64"]}}  # noqa: E731
    clock = _Clock()
    b = pcb.SlotLaneCurveBuilder(user=LIVE["accounts"][6][0], jup_call=jup, rpc=rpc, clock=clock)
    assert b.refresh(5.0) and len(calls) == 2 and b._buyback == LIVE["accounts"][17][0]
    clock.t += pcb.LEG_REFRESH_AFTER_S - 0.1
    assert b.refresh(5.0) and len(calls) == 2                            # young leg: no re-quote
    assert b.refresh(6.0) and len(calls) == 4                            # clip changed
    clock.t += pcb.LEG_REFRESH_AFTER_S + 0.1
    assert b.refresh(6.0) and len(calls) == 6                            # aged: re-quote
    assert _json.loads(_json.dumps(b._leg["usdc_atomic"])) == 6_000_000


@pytest.mark.parametrize("clip, builds", [(5.0 / 0.9001, True), (5.0 / 0.899, False)])
def test_the_leg_spends_between_90_and_100_percent_of_the_approved_clip(clip, builds, monkeypatch):
    """The reservation is the authority: a leg above the approved clip overspends it, and one under
    90% of it is a leg sized for another regime scale (0.75 in production). Between is mark drift:
    the clip is equity/3 and equity carries the open book's marks (Alpha, 2026-09-27)."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 10.0)
    msg, got = _builder(_Clock()).build(_buy(), clip, SNAP)
    assert (msg is not None) == builds, got


def test_the_build_reports_its_legs_age_and_none_when_native(monkeypatch):
    """The slot lane records it on slot_build (2026-09-27): how old the prefetched leg was."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 10.0)
    clock = _Clock()
    b = _builder(clock)
    clock.t += 0.75
    _message, validity = b.build(_buy(), 5.0, SNAP)
    assert validity["leg_age_s"] == 0.75
    _message, validity = b.build(_buy(), 5.0, SNAP, funding_lamports=28_000_000)
    assert validity["leg_age_s"] is None


def test_hard_copy_floor_uses_paid_average_instead_of_post_trade_price(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 35.)
    ev, accounts = _event(), LIVE['accounts']
    avg = pcb.trade_event_gross_lamports(ev) / 1e9 * 150. / (ev['token_amount'] / 1e6)
    _, amounts = pcb.curve_buy_instructions(ev, 3_000_000, 100., user=accounts[6][0],
        token_program=accounts[8][0], buyback_fee_recipient=accounts[17][0],
        source_price_usd=pcb.curve_source_price_usd(ev, 150.),
        source_cost_usdc_atomic=450_000, source_price_cap_pct=100.)
    assert .45 / (amounts['min_tokens_out'] / 1e6) <= avg * 1.35


def test_expected_tokens_out_is_pumps_own_rule_on_landed_buys():
    """2026-10-16: the floor net was one lamport high on 10 of 20 landed buy_exact_sol_in, so our own
    expected tokens (and their output floor) read ~0.002% high. The net is now pump's: the largest
    whose rounded-up fees fit the spend. 19 of 20 exact; the 20th is a buy the curve capped."""
    fx = json.loads((Path(__file__).parent / "fixtures/memecoin/rabbit_curve_buys_20261016.json").read_text())
    exact = capped = 0
    for rec in fx["direct"]:
        event = pcb.parse_trade_event(rec["trade_event_log"])
        spend = int.from_bytes(bytes.fromhex(rec["data_hex"])[8:16], "little")
        pre = dict(event, virtual_sol_reserves=event["virtual_sol_reserves"] - event["sol_amount"],
                   virtual_token_reserves=event["virtual_token_reserves"] + event["token_amount"],
                   real_token_reserves=event["real_token_reserves"] + event["token_amount"])
        if event["sol_amount"] + event["fee"] + event["creator_fee"] < spend:
            capped += 1
            continue
        assert pcb.curve_buy_net(spend, pre) == event["sol_amount"], rec["signature"]
        assert pcb.expected_tokens_out(spend, pre) == event["token_amount"], rec["signature"]
        exact += 1
    assert (exact, capped) == (19, 1)
