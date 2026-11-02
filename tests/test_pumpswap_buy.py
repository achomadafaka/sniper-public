"""PumpSwap buy: accounts, bytes, pricing and the slot-lane adapter, against LIVE mainnet frames.

tests/fixtures/pumpswap_buy_frames.json (read 2026-09-26): a direct buy_exact_quote_in with its
BuyEvent and its pool's account, two consecutive buys of one pool, and an exact-out buy.
"""
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as pcb
from sniper.execution import pumpswap_buy as ps
LIGHTHOUSE_PROGRAM = "L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95"   # the removed slot guard's program; must never appear

FX = json.loads((Path(__file__).parent / "fixtures" / "pumpswap_buy_frames.json").read_text())
LIVE = FX["direct_buy_exact_quote_in"]
ACC = LIVE["accounts"]
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
BLOCKHASH = "11111111111111111111111111111111"


def _event():
    return ps.parse_buy_event(LIVE["buy_event_log"])


def _facts():
    facts = ps.parse_pool_account(FX["pool_account_b64"])
    # base_decimals 6 is what the pool cache learns from this mint (see the learning test below).
    facts.update(base_token_program=ACC[11][0], quote_token_program=ACC[12][0], base_decimals=6)
    return facts


def test_pool_account_fields_match_the_live_buy():
    facts = ps.parse_pool_account(FX["pool_account_b64"])
    assert (facts["base_mint"], facts["quote_mint"]) == (ACC[3][0], ACC[4][0])
    assert (facts["pool_base_token_account"], facts["pool_quote_token_account"]) == (ACC[7][0], ACC[8][0])
    assert facts["virtual_quote"] == 17_584_505_596
    assert ps.buyback_fee_recipient_from_global_config(FX["global_config_b64"]) == ACC[24][0]


@pytest.mark.parametrize("virtual_quote", [
    0, 17_584_505_596, -994_798_809, 2**63, 2**64 + 17,
    2**80 + 17, -(2**63) - 1, -(2**80) + 17, -(2**127), 2**127 - 1,
])
def test_pool_virtual_quote_preserves_full_signed_i128(virtual_quote):
    # Official pump_amm IDL: Pool.virtual_quote_reserves is i128 at byte 245.
    raw = bytearray(base64.b64decode(FX["pool_account_b64"]))
    raw[245:261] = virtual_quote.to_bytes(16, "little", signed=True)
    assert ps.parse_pool_account(base64.b64encode(raw).decode())["virtual_quote"] == virtual_quote


@pytest.mark.parametrize("length", range(245, 261))
def test_pool_virtual_quote_rejects_partial_i128(length):
    raw = base64.b64decode(FX["pool_account_b64"])[:length]
    with pytest.raises(ValueError, match="not a PumpSwap Pool account"):
        ps.parse_pool_account(base64.b64encode(raw).decode())


def test_a_negative_virtual_quote_decodes_signed_and_prices_the_live_buy():
    """BADCLAUDE's pool 3fWA8Z3y (read live 2026-10-09T12:3xZ) holds virtual_quote = -994,798,809
    lamports. Read as u64 it was ~1.8e19 lamports, so the slot lane priced the coin at a
    $7,968,815,000,607 mcap (J7: $27,639) and refused three buys on 10-09 (Degen x2, Resell)."""
    raw = bytearray(base64.b64decode(FX["pool_account_b64"]))
    raw[ps.POOL_VIRTUAL_QUOTE_OFFSET:ps.POOL_VIRTUAL_QUOTE_OFFSET + 16] = (
        -994_798_809).to_bytes(16, "little", signed=True)
    vq = ps.parse_pool_account(base64.b64encode(bytes(raw)).decode())["virtual_quote"]
    assert vq == -994_798_809
    # Degen's live buy on that pool (tx 3MT2a62a..., slot 454866732): it paid 9,004,113,736 lamports
    # (fees 2 + 93 + 30 bps) against pre-trade vault reserves 293,234,171,027,792 / 60,976,720,992
    # and received 37,494,266,010,494. The signed read lands within 2% of that fill (the cached
    # virtual quote drifts between trades); the u64 read bought ~nothing.
    out = ps.expected_base_out(9_004_113_736, 293_234_171_027_792, 60_976_720_992, vq, 125)
    assert abs(out / 37_494_266_010_494 - 1) < 0.02


def test_account_list_matches_the_live_buy_in_order():
    ev = _event()
    ours = ps.pumpswap_buy_accounts(
        user=ACC[1][0], pool=ev["pool"], pool_facts=_facts(),
        protocol_fee_recipient=ev["protocol_fee_recipient"],
        protocol_fee_recipient_token_account=ev["protocol_fee_recipient_token_account"],
        buyback_fee_recipient=ACC[24][0])
    assert len(ours) == len(ACC) == 26               # the IDL names 23; live buys carry 3 more
    assert [(str(k), s, w) for k, s, w in ours] == [tuple(a) for a in ACC]


def test_zero_coin_creator_omits_pool_v2_before_buyback_accounts():
    """REF's zero creator uses two remaining accounts, matching the PumpSwap SDK."""
    ev, facts = _event(), _facts()
    facts["coin_creator"] = pcb.SYSTEM_PROGRAM
    ours = ps.pumpswap_buy_accounts(
        user=ACC[1][0], pool=ev["pool"], pool_facts=facts,
        protocol_fee_recipient=ev["protocol_fee_recipient"],
        protocol_fee_recipient_token_account=ev["protocol_fee_recipient_token_account"],
        buyback_fee_recipient=ACC[24][0])
    assert len(ours) == 25
    assert [(str(k), s, w) for k, s, w in ours[23:]] == [tuple(a) for a in ACC[24:]]


def test_instruction_data_matches_the_live_buy():
    live = bytes.fromhex(LIVE["data_hex"])            # 24 bytes: the live client omits OptionBool
    spend, floor = int.from_bytes(live[8:16], "little"), int.from_bytes(live[16:24], "little")
    ours = ps.buy_exact_quote_in_data(spend, floor)
    assert ours[:24] == live and ours[24:] == b"\x00"


def test_exact_in_pricing_is_exact_with_the_virtual_quote():
    ev, facts = _event(), _facts()
    spend = int.from_bytes(bytes.fromhex(LIVE["data_hex"])[8:16], "little")
    got = ps.expected_base_out(spend, ev["pool_base_token_reserves"], ev["pool_quote_token_reserves"],
                               facts["virtual_quote"], ps.total_fee_bps(ev))
    assert got == ev["base_amount_out"]
    # Without the virtual quote the same buy prices 7% high: the field the IDL does not name.
    naive = ps.expected_base_out(spend, ev["pool_base_token_reserves"],
                                 ev["pool_quote_token_reserves"], 0, ps.total_fee_bps(ev))
    assert naive > ev["base_amount_out"] * 1.07


def test_post_trade_reserves_are_the_next_buys_pre_trade():
    first = ps.parse_buy_event(FX["consecutive_same_pool"]["first"]["buy_event_log"])
    second = ps.parse_buy_event(FX["consecutive_same_pool"]["second"]["buy_event_log"])
    assert first["pool"] == second["pool"]
    assert ps.post_trade_reserves(first) == (second["pool_base_token_reserves"],
                                             second["pool_quote_token_reserves"])


def test_buyback_is_a_share_of_the_protocol_fee():
    ev = _event()
    assert ev["buyback_fee_basis_points"] == 5000 and ev["buyback_fee"] * 2 == ev["protocol_fee"]
    assert ps.total_fee_bps(ev) == 110


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _builder(clock, clip_usd=5.0):
    b = ps.SlotLanePumpSwapBuilder(user=ACC[1][0], jup_call=lambda *a, **k: None,
                                   rpc=lambda *a: None, clock=clock)
    b._buyback = ACC[24][0]
    b.pools._pools[_event()["pool"]] = _facts()
    b._leg = {"ixs": [], "tables": [], "usdc_atomic": pcb.clip_atomic(clip_usd),
              "min_out_lamports": 30_000_000, "out_lamports": 30_400_000, "fetched_at": clock()}
    return b


def _buy(**over):
    fields = {"pool": _event()["pool"], "trade_event_log": LIVE["buy_event_log"], "mint": None}
    fields.update(over)
    return SimpleNamespace(**fields)


SNAP = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=428_826_306, sol_usd=175.0)


def test_adapter_builds_with_no_slot_guard_after_the_compute_budget(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 10.0)
    b = _builder(_Clock())
    message, validity = b.build(_buy(), 5.0, SNAP)
    assert validity["blockhash"] == BLOCKHASH and validity["last_valid_height"] == 428_826_306
    assert validity["pump_price_limit"]["min_tokens_atomic"] == b.last_build["min_tokens_out"]
    keys = [str(k) for k in message.account_keys]
    programs = [keys[ix.program_id_index] for ix in message.instructions]
    assert keys[0] == ACC[1][0]
    assert programs == [COMPUTE_BUDGET, COMPUTE_BUDGET,
                        pcb.ATA_PROGRAM, ps.AMM_PROGRAM, pcb.TOKEN_PROGRAM]
    assert bytes(message.instructions[4].data) == bytes([9])          # CloseAccount on the wSOL ATA
    assert LIGHTHOUSE_PROGRAM not in keys                              # soft deadline: no slot guard
    cu_limit = int.from_bytes(bytes(message.instructions[0].data)[1:5], "little")
    assert cu_limit == 400_000                  # 401,016 while the guard's 1,016 CU was added
    assert b.last_build["min_tokens_out"] == max(
        b.last_build["output_floor_atomic"], b.last_build["source_price_floor_atomic"])
    assert b.last_build["mint"] == ACC[3][0] and b.last_build["bytes"] <= 1232


def test_native_funding_wraps_exact_spend_and_leaves_shared_wsol_account_open(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 10.0)
    builder = _builder(_Clock())
    message, validity = builder.build(_buy(), 5.0, SNAP, funding_lamports=28_000_000)
    assert validity["funding_asset"] == "sol"
    assert validity["funding_amount_atomic"] == 28_000_000
    keys = [str(key) for key in message.account_keys]
    programs = [keys[ix.program_id_index] for ix in message.instructions]
    assert programs == [COMPUTE_BUDGET, COMPUTE_BUDGET, pcb.ATA_PROGRAM,
                        pcb.SYSTEM_PROGRAM, pcb.TOKEN_PROGRAM, pcb.ATA_PROGRAM,
                        ps.AMM_PROGRAM]
    assert bytes(message.instructions[3].data)[4:12] == (28_000_000).to_bytes(8, "little")
    assert bytes(message.instructions[4].data) == bytes([17])
    assert builder.last_build["mint"] == ACC[3][0]


@pytest.mark.parametrize("case, reason", [
    ("no_leg", "leg_stale"), ("clip_scaled_down", "leg_over_clip"),
    ("clip_scaled_up", "leg_under_clip"), ("no_event", "no_trade_event"),
    ("other_pool", "event_pool_mismatch"), ("unknown_pool", "pool_unknown"),
    ("usdc_quoted_pool", "not wSOL"), ("no_blockhash", "no_blockhash"),
    ("wrong_quote_program", "quote token program"),
])
def test_adapter_refusals(case, reason):
    b, buy, snap, clip = _builder(_Clock()), _buy(), SNAP, 5.0
    if case == "no_leg":
        b._leg = None
    elif case == "clip_scaled_down":
        clip = 4.99
    elif case == "clip_scaled_up":
        clip = 5.0 / 0.75          # a leg sized for a 0.75-scaled clip against a full approval
    elif case == "no_event":
        buy = _buy(trade_event_log=None)
    elif case == "other_pool":
        buy = _buy(pool=pcb.WSOL_MINT)
    elif case == "unknown_pool":
        b.pools._pools.clear()
    elif case == "usdc_quoted_pool":
        b.pools._pools[_event()["pool"]] = {**_facts(), "quote_mint": pcb.USDC_MINT}
    elif case == "wrong_quote_program":
        b.pools._pools[_event()["pool"]] = {**_facts(),
                                           "quote_token_program": pcb.TOKEN_2022_PROGRAM}
    elif case == "no_blockhash":
        snap = SimpleNamespace(blockhash=None, last_valid_height=None)
    msg, got = b.build(buy, clip, snap)
    assert msg is None and reason in got


def test_a_missed_pool_is_learned_once_on_refresh():
    reads = []
    pool = _event()["pool"]

    def rpc(method, params):
        reads.append(method)
        if method == "getAccountInfo" and params[0] == pool:
            return {"value": {"owner": ps.AMM_PROGRAM, "data": [FX["pool_account_b64"]]}}
        if method == "getMultipleAccounts":
            return {"value": [{"owner": FX["base_mint_owner"], "data": [FX["base_mint_account_b64"]]},
                              {"owner": ACC[12][0], "data": [""]}]}
        return {"value": {"data": [FX["global_config_b64"]]}}           # the global config

    def jup(method, path, body=None):
        if method == "GET":
            return 200, {"outAmount": "30400000", "otherAmountThreshold": "30000000"}
        return 200, {"swapInstruction": {"programId": "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
                                         "accounts": [], "data": base64.b64encode(b"\x00").decode()}}

    clock = _Clock()
    b = ps.SlotLanePumpSwapBuilder(user=ACC[1][0], jup_call=jup, rpc=rpc, clock=clock)
    assert b.build(_buy(), 5.0, SNAP) == (None, "pool_unknown")   # queued even with no leg
    b.refresh(5.0)
    learned = b.pools.get(pool)
    assert learned["virtual_quote"] == 17_584_505_596
    # The base mint's real supply (burned below 1e9) and decimals, and the pump-migration check.
    assert (learned["base_supply"], learned["base_decimals"]) == (965_316_184_834_876, 6)
    assert learned["pump_migrated"] is True
    before = len(reads)
    b.refresh(5.0)
    assert len(reads) == before                                         # learned once, not re-read
    msg, _validity = b.build(_buy(), 5.0, SNAP)
    assert msg is not None


def test_pool_learning_reads_at_confirmed_so_a_seconds_old_pool_is_visible():
    """A freshly migrated PumpSwap pool is seconds old when its first buy arrives. With no
    commitment the node answers at FINALIZED (~32 slots behind), the pool reads as absent, and every
    buy in that window refuses pool_unknown before any reservation, so no fallback either
    (2026-09-26; the same default 210ac150 fixed for the mint read)."""
    seen = []
    pool = _event()["pool"]

    def rpc(method, params):
        seen.append((method, params[-1]))
        if method == "getAccountInfo":
            return {"value": {"owner": ps.AMM_PROGRAM, "data": [FX["pool_account_b64"]]}}
        return {"value": [{"owner": FX["base_mint_owner"], "data": [FX["base_mint_account_b64"]]},
                          {"owner": ACC[12][0], "data": [""]}]}

    assert ps.PumpSwapPoolCache(rpc=rpc).learn(pool) is True
    assert [m for m, _ in seen] == ["getAccountInfo", "getMultipleAccounts"]
    assert all(opts.get("commitment") == "confirmed" for _, opts in seen)


@pytest.mark.parametrize("clip, builds", [(5.0 / 0.9001, True), (5.0 / 0.899, False)])
def test_the_leg_spends_between_90_and_100_percent_of_the_approved_clip(clip, builds, monkeypatch):
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


def test_scaled_buy_uses_provided_leg_and_checks_its_spend(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CHASE_MAX_PCT', 10.0)
    b=_builder(_Clock())
    leg=dict(b._leg, usdc_atomic=4_000_000)
    message, detail=b.build(_buy(), 4.0, SNAP, leg=leg)
    assert message is not None, detail
    assert b.build(_buy(),4.0,SNAP,leg=dict(leg,usdc_atomic=4_000_001))==(None,'leg_over_clip')


def test_hard_copy_floor_uses_paid_average_instead_of_post_trade_price(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 35.)
    ev, facts = _event(), _facts()
    avg = ps.buy_event_gross_quote(ev) / 1e9 * 150. / (ev['base_amount_out'] / 1e6)
    _, amounts = ps.pumpswap_buy_instructions(ev, facts, 3_000_000, 100., user=ACC[1][0],
        buyback_fee_recipient=ACC[24][0], source_price_usd=ps.source_price_usd(ev, facts, 150.),
        source_cost_usdc_atomic=450_000, source_price_cap_pct=100.)
    assert .45 / (amounts['min_tokens_out'] / 1e6) <= avg * 1.35


SANTARO_BUY_LOG = 'Program data: Z/RSHyz1d3fS1stqAAAAAJACt8kFHgAAhUiJiQEAAAAAAAAAAAAAAIVIiYkBAAAAfaUlkc4hAQAbJjYjDQAAAKuDrYQBAAAAAgAAAAAAAAB75hMAAAAAAF0AAAAAAAAANV2dAwAAAAAmasGEAQAAAIVIiYkBAAAAZH35igXjak8vlFLLf6slgvRQWGm/tgorIfrtluaPUiOCsrMaa8G7G8/JKQCq2mcTwII+89kLSipTXZm4zPnAQ4+h39SuRetwUWUd2EM+gjrSw7NaK9QDKpBfaFGi/MxywBnuieZln4xRfjL+U/BNB1Z4lQ8Hu9Sc1WzKlF06ft3/g4OBi6j6KMPNO21ek/n6uPCXm8NyFazFskaHe6jDybnwRqOiz07iErT6kv2dAPpPsLP8/TYeQK5TkflgNepjsN6O9t9SSV4ubsegTf2ibzu0syr70TcLcsQKZcLNS9QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAkAK3yQUeAAADAAAAYnV5HgAAAAAAAAAqgSoBAAAAAIgTAAAAAAAAmq7OAQAAAAAAAAAAAAAAAAAAAAAAAAAAAOIEh0UPjQMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'

@pytest.mark.parametrize("ix_name", ["buy", "buy_exact_quote_in"])
def test_cashback_is_in_the_paid_average_and_signed_copy_floor(ix_name, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.)
    ev = ps.parse_buy_event(SANTARO_BUY_LOG)
    if ix_name == "buy_exact_quote_in":
        ev["quote_amount_in"] = ev["user_quote_amount_in"]
    ev["ix_name"] = ix_name
    gross = 6_602_442_885
    assert ps.buy_event_gross_quote(ev) == gross
    assert ps.buy_event_gross_quote(dict(ev, cashback=ev["cashback"] + 1)) is None
    assert ps.buy_event_gross_quote(dict(ev, cashback=-1)) is None
    assert ps.buy_event_gross_quote(dict(ev, cashback=None)) is None
    facts = dict(_facts(), virtual_quote=0)
    average = gross / 1e9 * 111.953201 / (ev["base_amount_out"] / 1e6)
    _, amounts = ps.pumpswap_buy_instructions(
        ev, facts, 3_000_000, 100., user=ACC[1][0], buyback_fee_recipient=ACC[24][0],
        source_price_usd=ps.source_price_usd(ev, facts, 111.953201),
        source_cost_usdc_atomic=335_860, source_price_cap_pct=100.)
    assert amounts["source_average_price_usd"] == pytest.approx(average)
    assert .33586 / (amounts["min_tokens_out"] / 1e6) <= average * 1.35
