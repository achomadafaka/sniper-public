"""Meteora DBC (dynamic bonding curve) on the slot lane, LIVE (operator order 2026-10-07 via Alpha:
"we need that live no time for shadows"; trigger position 71651 ELON, a routed DBC buy the lane
handed off `no_slot_venue` that the on-chain lane filled ~2 s later at +19.3% vs the source).

The rows are REAL mainnet reads (tests/fixtures/memecoin/dbc_pools.json, 2026-10-07): ELON quoted in
the Xs3o xStock (exotic), a SOL-quoted classic coin, a Token-2022 coin with a transfer hook, and a
migrated (completed) curve. DBC emits its swap event by self-CPI, so a frame names no pool: every
DBC routed buy is learned by mint (getProgramAccounts on base_mint, then one getMultipleAccounts of
config, coin and quote) inside the MissLearner budget, and the buy carries those facts.

Money guards pinned here: a hooked coin is refused; a migrated, complete or completing curve is
refused; a rate-limiter pool is refused; the signed minimum is the source-average price floor on the
leg's guaranteed quote amount; the instruction is swap2 ExactIn, never swap2_with_transfer_hook.
MEMECOIN_SLOT_LANE_DBC: off (default) | live.
"""
import base64
import dataclasses
import json
import struct
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.execution import meteora_dbc_buy as B
from sniper.execution import pump_curve_buy as pcb
from sniper.services import memecoin_slot_lane as lane
from sniper.services import memecoin_slot_signal as s
from sniper.settings import memecoin as cfg

FX = json.loads((Path(__file__).parent / "fixtures/memecoin/dbc_pools.json").read_text())
ROWS = {row["name"]: row for row in FX["rows"]}
EXOTIC, SOL, HOOK, MIGRATED = ROWS["exotic"], ROWS["sol"], ROWS["hook"], ROWS["migrated"]
USER = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
BLOCKHASH = "11111111111111111111111111111111"
SNAP = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=150.0)
XS3O = "Xs3oZwbHvqis4NYcf4YKWmEia2eC84wSiVrcYcTqpH8"


class FakeRpc:
    def __init__(self, row=None, *, gpa=None, gma=None, fail=None, quote_value=None):
        self.calls = []
        self.gpa = row["gpa"] if gpa is None and row is not None else gpa
        self.gma = row["gma"] if gma is None and row is not None else gma
        self.quote_value = row["quote_value"] if quote_value is None and row is not None else quote_value
        self.fail = fail

    def __call__(self, method, params):
        self.calls.append((method, params))
        if self.fail == method:
            raise TimeoutError("read timed out")
        if method == "getProgramAccounts":
            return self.gpa
        if method == "getMultipleAccounts":
            return {"value": self.gma}
        if method == "getAccountInfo":
            return {"value": self.quote_value}
        raise AssertionError(method)


@pytest.fixture
def dbc_live(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DBC", "live", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"meteora_dbc"}),
                        raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)


def _facts(row, **changes):
    facts, why = B.find_pool(FakeRpc(row), row["coin"])
    assert why is None, why
    return dataclasses.replace(facts, **changes) if changes else facts


def _routed(row, *, usdc=100_000_000, tokens=None, decimals=None, events=(B.DbcSeen(),)):
    # $100 for 10M whole tokens = $0.00001 a token; a 1B supply -> ~$10k mcap.
    decimals = _facts(row).coin_facts[1] if decimals is None else decimals
    tokens = 10_000_000 * 10 ** decimals if tokens is None else tokens
    return s.RoutedBuy(source_user_id="w1", handle="h1", owner="Owner1", signature="Sig1" * 22,
                       slot=454_300_000, mint=row["coin"], token_delta_atomic=tokens,
                       token_decimals=decimals, usdc_spent_atomic=usdc,
                       recv_monotonic=time.monotonic(), recv_utc="2026-10-07T19:05:03+00:00",
                       paired_monotonic=time.monotonic(), venue_events=tuple(events))


def _buy(row, **changes):
    buy, why = B.dbc_slot_buy(_routed(row), _facts(row, **changes))
    assert why is None, why
    return buy


def _builder(leg=None):
    return B.SlotLaneDbcBuilder(user=USER, rpc=None, leg_fn=lambda: leg, clock=lambda: 1000.0)


def _leg(quote, out=300_000_000, usdc=100_000_000):
    return {"ixs": [], "tables": [], "usdc_atomic": usdc, "min_out_lamports": out * 985 // 1000,
            "out_lamports": out, "fetched_at": 1000.0, "quote": {}, "output_mint": quote}


def _swap(message):
    keys = [str(k) for k in message.account_keys]
    swaps = [ix for ix in message.instructions if keys[ix.program_id_index] == B.PROGRAM]
    assert len(swaps) == 1
    (swap,) = swaps
    data = bytes(swap.data)
    amount_in, min_out, mode = struct.unpack("<QQB", data[8:25])
    metas = [(keys[i], message.is_maybe_writable(i), message.is_signer(i)) for i in bytes(swap.accounts)]
    return data[:8], metas, amount_in, min_out, mode


# --- the chain facts --------------------------------------------------------------------------------

def test_the_real_pool_and_config_parse_at_the_verified_offsets():
    raw = base64.b64decode(EXOTIC["gpa"][0]["account"]["data"][0])
    pool = B.parse_pool(raw)
    assert pool["config"] == EXOTIC["config"] and pool["base_mint"] == EXOTIC["coin"]
    assert pool["is_migrated"] == 0 and 0 < pool["quote_reserve"]
    config = B.parse_config(base64.b64decode(EXOTIC["gma"][0]["data"][0]))
    assert config["quote_mint"] == XS3O and config["threshold"] == 6_900_000_000
    assert config["base_fee_mode"] == 0
    with pytest.raises(ValueError):
        B.parse_pool(b"\x00" * B.POOL_LEN)
    with pytest.raises(ValueError):
        B.parse_config(b"\x00" * B.CONFIG_LEN)


def test_the_logs_show_only_that_dbc_ran():
    logs = ["Program dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN invoke [2]",
            "Program log: Instruction: Swap2"]
    assert B.dbc_invoked(logs) and not B.dbc_invoked(["Program x invoke [1]"])
    frame = {"params": {"result": {"value": {"logs": logs}}}}
    assert s._dbc_seen(frame, True) == (B.DbcSeen(),)
    assert s._dbc_seen(frame, False) == ()
    assert s._dbc_seen({"params": {}}, True) == ()                 # unreadable: never raises


@pytest.mark.parametrize("row,coin_program,quote_program,reads", [
    (EXOTIC, pcb.TOKEN_PROGRAM, pcb.TOKEN_2022_PROGRAM, 3), (SOL, pcb.TOKEN_PROGRAM, pcb.TOKEN_PROGRAM, 2)],
    ids=["exotic", "sol"])
def test_find_pool_by_mint_reads_the_pool_then_config_coin_and_quote_vault(row, coin_program, quote_program,
                                                                         reads):
    # The quote mint lives in the config, so it cannot ride the second read; the quote VAULT names it
    # (cross-checked) and its program. SOL/USDC mints need nothing more; an exotic quote's mint is read
    # once more (then kept for QUOTE_RECHECK_S, as the worker does).
    rpc = FakeRpc(row)
    facts, why = B.find_pool(rpc, row["coin"])
    assert why is None
    assert [m for m, _ in rpc.calls] == ["getProgramAccounts", "getMultipleAccounts",
                                         "getAccountInfo"][:reads]
    (_, gpa), (_, gma) = rpc.calls[:2]
    assert gpa[0] == B.PROGRAM and {"dataSize": 424} in gpa[1]["filters"]
    assert {"memcmp": {"offset": 136, "bytes": row["coin"]}} in gpa[1]["filters"]
    assert gma[0] == [row["config"], row["coin"], row["quote_vault"]]
    assert facts.pool == row["pool"] and facts.config == row["config"] and facts.quote == row["quote"]
    assert facts.coin_program == coin_program and facts.quote_program == quote_program
    assert facts.coin_refusal is None and facts.coin_facts is not None


@pytest.mark.parametrize("case,reason", [
    ("none", "no_pool"), ("two", "ambiguous"), ("gpa_error", "unread"), ("gpa_not_list", "unread"),
    ("gma_short", "unread"), ("gma_error", "unread"), ("config_missing", "unread"),
    ("threshold_zero", "config_unreadable"), ("not_a_pool", "unread"),
    ("vault_mint_mismatch", "unread"), ("quote_read_error", "unread"), ("quote_gone", "unread"),
    ("gpa_null", "unread")])
def test_find_pool_refusals_never_read_unknown_as_absent(case, reason):
    row = EXOTIC
    gpa, gma, fail = list(row["gpa"]), list(row["gma"]), None
    if case == "none":
        gpa = []
    elif case == "two":
        gpa = gpa + [dict(gpa[0], pubkey="11111111111111111111111111111112")]
    elif case == "gpa_error":
        fail = "getProgramAccounts"
    elif case == "gpa_not_list":
        gpa = {"error": "too many accounts"}
    elif case == "gma_short":
        gma = gma[:2]
    elif case == "gma_error":
        fail = "getMultipleAccounts"
    elif case == "config_missing":
        gma = [None] + gma[1:]
    elif case == "threshold_zero":
        raw = bytearray(base64.b64decode(gma[0]["data"][0]))
        raw[264:272] = b"\x00" * 8
        gma = [dict(gma[0], data=[base64.b64encode(bytes(raw)).decode(), "base64"])] + gma[1:]
    elif case == "vault_mint_mismatch":
        vault = json.loads(json.dumps(gma[2]))
        vault["data"]["parsed"]["info"]["mint"] = pcb.USDC_MINT
        gma = gma[:2] + [vault]
    elif case == "quote_read_error":
        fail = "getAccountInfo"
    elif case == "not_a_pool":
        raw = bytearray(base64.b64decode(gpa[0]["account"]["data"][0]))
        raw[:8] = b"\x00" * 8
        gpa = [dict(gpa[0], account=dict(gpa[0]["account"], data=[base64.b64encode(bytes(raw)).decode(),
                                                                     "base64"]))]
    rpc = FakeRpc(gpa=gpa, gma=gma, fail=fail, quote_value=row["quote_value"])
    if case == "quote_gone":
        rpc.quote_value = None
    if case == "gpa_null":                    # a null result is unknown, never "no pool" (Gamma LOW-2)
        rpc.gpa = None
    assert B.find_pool(rpc, row["coin"]) == (None, reason)


def test_the_learner_keeps_an_exotic_quote_mint_for_the_recheck_window():
    clock = [100.0]
    rpc = FakeRpc(EXOTIC)
    dbc = B.DbcMintLearner(rpc, clock=lambda: clock[0])
    assert dbc.learn(EXOTIC["coin"]) and dbc.learn(EXOTIC["coin"])
    assert [m for m, _ in rpc.calls].count("getAccountInfo") == 1
    clock[0] += 300.0
    assert dbc.learn(EXOTIC["coin"])
    assert [m for m, _ in rpc.calls].count("getAccountInfo") == 2


# --- the money guards ---------------------------------------------------------------------------------

def test_a_hooked_coin_is_refused_twice_and_never_built():
    # Its curve is a TransferHookPool: refused at the first read, before any mint read.
    rpc = FakeRpc(HOOK)
    assert B.find_pool(rpc, HOOK["coin"]) == (None, "transfer_hook_pool")
    assert [m for m, _ in rpc.calls] == ["getProgramAccounts"]
    # A VirtualPool on a ConfigWithTransferHook is refused at the config.
    gma = [HOOK["gma"][0]] + SOL["gma"][1:]
    assert B.find_pool(FakeRpc(SOL, gma=gma), SOL["coin"]) == (None, "transfer_hook_config")
    # And a hooked mint on an ordinary config is refused by the coin's own extensions.
    gma = [SOL["gma"][0], HOOK["gma"][1], SOL["gma"][2]]
    facts, why = B.find_pool(FakeRpc(SOL, gma=gma), SOL["coin"])
    assert why is None and facts.coin_refusal == "extension_transferHook"
    buy, why = B.dbc_slot_buy(_routed(SOL), facts)
    assert buy is None and why == "slot_lane_dbc_coin_extension_transferHook"


def test_a_migrated_or_complete_curve_is_refused():
    buy, why = B.dbc_slot_buy(_routed(MIGRATED), _facts(MIGRATED))
    assert buy is None and why == "slot_lane_dbc_migrated"
    facts = _facts(SOL)
    complete = dataclasses.replace(facts, quote_reserve=facts.threshold)
    assert B.dbc_slot_buy(_routed(SOL), complete) == (None, "slot_lane_dbc_curve_complete")


def test_a_rate_limiter_pool_is_refused():
    assert B.dbc_slot_buy(_routed(SOL), _facts(SOL, base_fee_mode=2)) == (None, "slot_lane_dbc_rate_limiter")


def test_the_admission_rechecks_the_carried_facts(dbc_live):
    buy = _buy(SOL)
    stale = dataclasses.replace(buy, venue_event=dataclasses.replace(buy.venue_event, is_migrated=1))
    assert B.market_facts(stale, 150.0)[0] == "slot_lane_dbc_migrated"
    assert B.market_facts(dataclasses.replace(buy, venue_event=None), 150.0)[0] == "pool_unknown"


def test_a_curve_our_buy_plus_the_sources_would_complete_is_refused_at_build(dbc_live):
    # ExactIn past the curve end reverts (AmountLeftIsNotZero), and the learn read may predate the
    # source's own buy, so both are counted.
    facts = _facts(SOL)
    leg = _leg(pcb.WSOL_MINT, out=1_000_000_000)                       # ~0.985 SOL in
    their_q = int(100.0 / 150.0 * 1e9)                                 # the source's $100 in SOL
    room = int(leg["min_out_lamports"]) + their_q
    near = dataclasses.replace(facts, quote_reserve=facts.threshold - room)
    buy, _ = B.dbc_slot_buy(_routed(SOL), near)
    assert _builder(leg).build(buy, 100.0, SNAP) == (None, "slot_lane_dbc_curve_completing")
    roomy = dataclasses.replace(facts, quote_reserve=facts.threshold - room - 1)
    buy, _ = B.dbc_slot_buy(_routed(SOL), roomy)
    assert _builder(leg).build(buy, 100.0, SNAP)[0] is not None
    unknown = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=None)
    assert _builder(leg).build(buy, 100.0, unknown) == (None, "slot_lane_dbc_curve_unknown")


# --- admission and the lane's venue rules --------------------------------------------------------------

def test_admission_prices_from_the_source_average_and_cues_the_worker_for_an_exotic_quote(dbc_live,
                                                                                           monkeypatch):
    sol = _buy(SOL)
    reason, facts = B.market_facts(sol, 150.0)
    assert reason is None and facts["price_usd"] == pytest.approx(1e-5)
    assert facts["mcap_usd"] == pytest.approx(1e-5 * sol.venue_event.coin_facts[0]
                                              / 10 ** sol.venue_event.coin_facts[1])
    exotic = _buy(EXOTIC)
    assert B.market_facts(exotic, 150.0)[0] == "slot_lane_dbc_quote_not_supported"
    assert "slot_lane_dbc_quote_not_supported" in lane.ONDEMAND_QUOTE_REASONS
    assert B.market_facts(exotic, 150.0, quote=(8, 400.0))[0] is None
    usdc = dataclasses.replace(sol, quote_mint=pcb.USDC_MINT,
                               venue_event=dataclasses.replace(sol.venue_event, quote=pcb.USDC_MINT))
    assert B.market_facts(usdc, 150.0)[0] is None
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset(), raising=False)
    assert B.market_facts(usdc, 150.0)[0] == "slot_lane_dbc_quote_not_supported"


def test_admission_refusal_takes_a_dbc_buy_through_the_shared_gates(dbc_live, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 5_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", {}, raising=False)
    reason, _ = lane.admission_refusal(_buy(SOL), armed_ids={"w1"}, sol_usd=150.0, kill_tripped=False)
    assert reason.startswith("mcap $") and "over ceiling $5000" in reason
    assert lane.admission_refusal(_buy(SOL), armed_ids=set(), sol_usd=150.0,
                                  kill_tripped=False)[0] == "not_approved"


def test_live_needs_the_knob_and_exotic_needs_the_listing(dbc_live, monkeypatch):
    sol, exotic = _buy(SOL), _buy(EXOTIC)
    assert lane.shadow_only(sol) is False and lane.shadow_only(exotic) is False
    assert lane.exotic_live("meteora_dbc") and "meteora_dbc" in lane.EXOTIC_VENUES
    assert lane.SlotLane._cap_key(sol) == "meteora_dbc"
    assert lane.SlotLane._cap_key(dataclasses.replace(sol, quote_mint=pcb.USDC_MINT)) == "meteora_dbc:exotic"
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset(), raising=False)
    assert lane.shadow_only(sol) is False and lane.shadow_only(exotic) is True
    for mode in ("off", "shadow", "LIVEISH"):
        monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DBC", mode, raising=False)
        assert B.lane_mode() == "off" and lane.shadow_only(sol) is True


# --- the build ---------------------------------------------------------------------------------------

EXPECTED_ORDER = ["pool_authority", "config", "pool", "input", "output", "base_vault", "quote_vault",
                  "base_mint", "quote_mint", "payer", "base_program", "quote_program", "referral",
                  "event_authority", "program"]


def _expected_metas(facts, input_account, output_account):
    named = {"pool_authority": (B.POOL_AUTHORITY, False, False), "config": (facts.config, False, False),
             "pool": (facts.pool, True, False), "input": (input_account, True, False),
             "output": (output_account, True, False), "base_vault": (facts.base_vault, True, False),
             "quote_vault": (facts.quote_vault, True, False), "base_mint": (facts.coin, False, False),
             "quote_mint": (facts.quote, False, False), "payer": (USER, True, True),
             "base_program": (facts.coin_program, False, False),
             "quote_program": (facts.quote_program, False, False),
             "referral": (B.PROGRAM, False, False), "event_authority": (B.EVENT_AUTHORITY, False, False),
             "program": (B.PROGRAM, False, False)}
    return [named[n] for n in EXPECTED_ORDER]


def test_an_exotic_build_is_swap2_exact_in_on_the_legs_minimum_with_the_price_floor(dbc_live):
    from sniper.execution.pump_curve_buy import source_price_floor_atomic
    from sniper.execution.pump_native_exit import _pk, ata
    buy = _buy(EXOTIC)
    facts = buy.venue_event
    leg = _leg(XS3O)
    message, validity = _builder().build(buy, 100.0, SNAP, leg=leg, quote=(8, 400.0, 0, 0))
    assert message is not None, validity
    disc, metas, amount_in, min_out, mode = _swap(message)
    assert disc == B.SWAP2_DISCRIMINATOR and disc.hex() == "414b3f4ceb5b5b88" and mode == 0
    quote_ata = str(ata(_pk(USER), _pk(XS3O), _pk(pcb.TOKEN_2022_PROGRAM)))
    coin_ata = str(ata(_pk(USER), _pk(facts.coin), _pk(pcb.TOKEN_PROGRAM)))
    assert metas == _expected_metas(facts, quote_ata, coin_ata)
    assert amount_in == leg["min_out_lamports"]
    assert min_out == source_price_floor_atomic(100_000_000, 1e-5, 30.0, base_decimals=facts.coin_facts[1])
    assert validity["min_tokens_out"] == min_out and validity["pool"] == facts.pool
    assert validity["pump_price_limit"]["cost_usdc_atomic"] == 100_000_000


def test_a_sol_build_spends_the_shared_wsol_leg_and_closes_the_wsol_account(dbc_live):
    from sniper.execution.pump_native_exit import _pk, ata
    buy = _buy(SOL)
    leg = _leg(pcb.WSOL_MINT, out=600_000_000)
    message, validity = _builder(leg).build(buy, 100.0, SNAP)
    assert message is not None, validity
    _disc, metas, amount_in, _min, _mode = _swap(message)
    wsol = str(ata(_pk(USER), _pk(pcb.WSOL_MINT), _pk(pcb.TOKEN_PROGRAM)))
    assert metas[3][0] == wsol and amount_in == leg["min_out_lamports"]
    keys = [str(k) for k in message.account_keys]
    closes = [ix for ix in message.instructions
              if keys[ix.program_id_index] == pcb.TOKEN_PROGRAM and bytes(ix.data)[:1] == bytes([9])]
    assert len(closes) == 1


def test_a_usdc_build_spends_the_clip_from_the_usdc_account(dbc_live):
    from sniper.execution.pump_native_exit import _pk, ata
    sol = _buy(SOL)
    facts = dataclasses.replace(sol.venue_event, quote=pcb.USDC_MINT, quote_facts=(6, 0, 0))
    buy = dataclasses.replace(sol, quote_mint=pcb.USDC_MINT, venue_event=facts)
    builder = B.SlotLaneDbcBuilder(user=USER, rpc=None, clock=lambda: 1000.0,
                                   leg_fn=lambda: pytest.fail("a USDC build spends no leg"))
    message, validity = builder.build(buy, 100.0, SNAP)
    assert message is not None, validity
    _disc, metas, amount_in, _min, _mode = _swap(message)
    assert metas[3][0] == str(ata(_pk(USER), _pk(pcb.USDC_MINT), _pk(pcb.TOKEN_PROGRAM)))
    assert amount_in == 100_000_000


@pytest.mark.parametrize("case,reason", [
    ("no_leg", "exotic_leg_missing"), ("wrong_quote_leg", "leg_wrong_quote"), ("stale", "leg_stale"),
    ("native", "native_funding_not_supported_on_dbc"), ("no_blockhash", "no_blockhash")])
def test_exotic_build_refusals(dbc_live, case, reason):
    buy = _buy(EXOTIC)
    kw = {"leg": _leg(XS3O), "quote": (8, 400.0, 0, 0)}
    snap = SNAP
    if case == "no_leg":
        kw = {}
    elif case == "wrong_quote_leg":
        kw["leg"] = _leg(pcb.USDC_MINT)
    elif case == "stale":
        kw["leg"] = dict(_leg(XS3O), fetched_at=0.0)
    elif case == "native":
        kw = {"funding_lamports": 1}
    elif case == "no_blockhash":
        snap = SimpleNamespace(blockhash=None, last_valid_height=1, sol_usd=150.0)
    assert _builder().build(buy, 100.0, snap, **kw) == (None, reason)


def test_the_builder_serves_the_on_demand_worker_with_the_learned_quote_value(dbc_live):
    buy = _buy(EXOTIC)
    builder = _builder()
    quote_mint, quote, refusal = builder.ondemand_quote(buy)
    assert quote_mint == XS3O and refusal is None and quote is not None and quote[0] == 8
    extra = builder.ondemand_facts(buy)
    assert extra["quote_read_at"] == buy.venue_event.read_mono
    assert extra["mint_values"][XS3O] == EXOTIC["quote_value"]


# --- the routed path: the learner, and zero time on the other venues ----------------------------------

class _Learner:
    def __init__(self):
        self.offered = []

    def offer(self, miss):
        self.offered.append(miss)
        return True


def _route(routed, learner, dbc_learner, pump_on=False):
    puts = []
    decided = lane.route_routed_buy(routed, handoff=None, log=lambda *_: None, put=puts.append,
                                    amm_pools=None, cpmm_on=False, pump_on=pump_on, lane_live=True,
                                    learner=learner, dbc_learner=dbc_learner)
    return decided, puts


def test_a_dbc_routed_buy_is_learned_by_mint_and_converted_once(dbc_live):
    learner, dbc = _Learner(), B.DbcMintLearner(FakeRpc(EXOTIC))
    decided, puts = _route(_routed(EXOTIC), learner, dbc)
    assert decided == ("learning", "pool_unknown") and not puts
    (miss,) = learner.offered
    assert miss.venue == "meteora_dbc" and miss.pool == EXOTIC["coin"] and miss.routed is not None
    assert dbc.learn(miss.pool) is True
    buy, why = miss.convert()
    assert why is None and buy.venue == "meteora_dbc" and buy.quote_mint == XS3O
    assert buy.venue_event.pool == EXOTIC["pool"]


def test_a_refused_learned_pool_converts_to_its_reason_and_an_unfound_one_is_not_learned(dbc_live):
    dbc = B.DbcMintLearner(FakeRpc(MIGRATED))
    miss = lane.RoutedDbcMiss(_routed(MIGRATED), dbc)
    assert dbc.learn(miss.pool) is True
    assert miss.convert() == (None, "slot_lane_dbc_migrated")
    hooked = B.DbcMintLearner(FakeRpc(HOOK))
    assert hooked.learn(HOOK["coin"]) is False and hooked.stats["refused"] == {"transfer_hook_pool": 1}
    nothing = B.DbcMintLearner(FakeRpc(gpa=[], gma=[]))
    assert nothing.learn(SOL["coin"]) is False and nothing.stats["refused"] == {"no_pool": 1}
    assert lane.RoutedDbcMiss(_routed(SOL), nothing).convert() == (None, "pool_unknown")


def test_old_learned_facts_are_never_converted(dbc_live, monkeypatch):
    clock = [100.0]
    dbc = B.DbcMintLearner(FakeRpc(SOL), clock=lambda: clock[0])
    assert dbc.learn(SOL["coin"]) is True
    clock[0] += B.FACTS_MAX_AGE_S + 0.01
    assert lane.RoutedDbcMiss(_routed(SOL), dbc).convert() == (None, "pool_unknown")


def test_no_marker_no_learner_or_knob_off_hands_off_as_today(dbc_live, monkeypatch):
    learner, dbc = _Learner(), B.DbcMintLearner(FakeRpc(SOL))
    assert _route(_routed(SOL, events=()), learner, dbc)[0] == ("handoff", "no_slot_venue")
    assert _route(_routed(SOL), learner, None)[0] == ("handoff", "no_slot_venue")
    assert not learner.offered


def test_a_frame_that_also_ran_pump_goes_pump_first(dbc_live, monkeypatch):
    # Zero added time on the other venues: the DBC branch runs after every existing conversion.
    pump_buy = SimpleNamespace(venue="pump_amm", signature="Sig1" * 22)
    monkeypatch.setattr(lane, "pump_slot_buy", lambda routed, pools: (pump_buy, None))
    learner, rpc = _Learner(), FakeRpc(SOL)
    decided, puts = _route(_routed(SOL), learner, B.DbcMintLearner(rpc), pump_on=True)
    assert decided == ("lane", "pump_amm") and puts == [pump_buy]
    assert not learner.offered and not rpc.calls


def test_the_learner_hands_off_a_live_routed_dbc_miss_through_its_routed_row(dbc_live):
    written = []
    handoff = lane.make_learner_handoff(live=True, routed_handoff=SimpleNamespace(
        write=written.append, wake=None), handoff=None, sol_usd_fn=lambda: 150.0)
    miss = lane.RoutedDbcMiss(_routed(SOL), B.DbcMintLearner(FakeRpc(SOL)))
    handoff(miss, "slot_lane_dbc_migrated")
    assert written == [miss.routed]


def test_the_dispatch_build_hands_a_sol_dbc_buy_the_shared_cash_leg(dbc_live):
    leg = _leg(pcb.WSOL_MINT, out=600_000_000)
    amm = SimpleNamespace(matching_leg=lambda clip: leg, pools=None)
    builder = B.SlotLaneDbcBuilder(user=USER, rpc=None, clock=lambda: 1000.0,
                                   leg_fn=lambda: pytest.fail("the cash leg comes from dispatch"))
    build = lane.dispatch_build({"meteora_dbc": builder, "pump_amm": amm})
    message, validity = build(_buy(SOL), 100.0, SNAP)
    assert message is not None, validity


def test_a_price_floor_beyond_u64_refuses_the_build_instead_of_raising(dbc_live):
    routed = dataclasses.replace(_routed(SOL), usdc_spent_atomic=1, token_delta_atomic=10 ** 30)
    buy, _ = B.dbc_slot_buy(routed, _facts(SOL))
    leg = _leg(pcb.WSOL_MINT, out=600_000_000)
    assert _builder(leg).build(buy, 100.0, SNAP) == (None, "price floor outside u64")


# --- the live lane and the production wiring -----------------------------------------------------------

from test_slot_lane_damm_v2 import conn  # noqa: E402,F401


def test_a_live_sol_dbc_buy_reserves_builds_and_sends_under_its_venue_cap(dbc_live, conn, tmp_path,  # noqa: F811
                                                                          monkeypatch):
    from sniper.services import memecoin_source_initiation as initiation
    monkeypatch.setattr(initiation, "source_initiation_refusal", lambda *_: None)
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    for name, value in (("MEMECOIN_REAL_ENABLED", True), ("MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0),
                        ("MEMECOIN_REAL_MAX_MCAP_USD", 1_000_000.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", {}),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 0.0),
                        ("MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE", {})):
        monkeypatch.setattr(cfg, name, value, raising=False)
    sent = []

    def execute(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        sent.append({"pending": dict(pending), "validity": validity, "message": message,
                     "entry_min_slot": entry_min_slot, "entry_max_slot": entry_max_slot,
                     "entry_head_fn": entry_head_fn})
        return {"status": "filled", "position_id": 1}
    now = time.monotonic()
    snap = lane.PrefetchSnapshot(sol_usd=150.0, usdc_balance=400.0, sol_balance=50.0,
                                 blockhash=BLOCKHASH, last_valid_height=1,
                                 observed_mono={k: now for k in ("sol_usd", "balances", "blockhash")})
    builder = B.SlotLaneDbcBuilder(user=USER, rpc=None, leg_fn=lambda: None)
    amm = SimpleNamespace(pools=None, matching_leg=lambda clip: dict(
        _leg(pcb.WSOL_MINT, out=600_000_000, usdc=int(round(clip * 1e6))), fetched_at=time.monotonic()))
    ln = lane.SlotLane(conn=conn, executor={"slot_execute_fn": execute},
                       build_fn=lane.dispatch_build({"meteora_dbc": builder, "pump_amm": amm}),
                       snapshot_fn=lambda: snap, armed_ids_fn=lambda: {"w1"}, kill_tripped_fn=lambda: False,
                       now_iso_fn=lambda: "2026-10-07T19:05:03+00:00", lock_key="sol-test", mode="live",
                       shadow_clip_fn=lambda: 100.0, sol_buffer_cap_usd=1000.0)
    buy = dataclasses.replace(_buy(SOL), recv_monotonic=now)
    result = ln.handle(buy)
    assert result["status"] == "filled", result
    (call,) = sent
    # PR #486 (2026-10-20): automatic Solana entries carry no source-slot window; both bounds are None.
    assert call["entry_min_slot"] is None and call["entry_max_slot"] is None
    assert callable(call["entry_head_fn"])
    _disc, metas, _in, min_out, mode = _swap(call["message"])
    assert mode == 0 and min_out == call["validity"]["min_tokens_out"] > 0
    assert ln._venue_live_sends["meteora_dbc"] and not ln._venue_live_sends["meteora_dbc:exotic"]


def test_the_dbc_learner_needs_the_knob_and_a_non_funded_endpoint(dbc_live, monkeypatch):
    lines = []
    url = "https://mainnet.helius-rpc.com/?api-key=watch"
    monkeypatch.setenv("MEMECOIN_SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=funded")
    builders = {"meteora_dbc": object()}
    assert isinstance(lane.dbc_first_sight_learner(builders, log=lines.append,
                                                   env={"ONCHAIN_WATCH_HELIUS_RPC_URL": url}),
                      B.DbcMintLearner)
    funded = {"ONCHAIN_WATCH_HELIUS_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=funded"}
    assert lane.dbc_first_sight_learner(builders, log=lines.append, env=funded) is None
    assert any("dbc" in line and "OFF" in line for line in lines)
    assert lane.dbc_first_sight_learner({}, log=lines.append,
                                        env={"ONCHAIN_WATCH_HELIUS_RPC_URL": url}) is None
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DBC", "off", raising=False)
    assert lane.dbc_first_sight_learner(builders, log=lines.append,
                                        env={"ONCHAIN_WATCH_HELIUS_RPC_URL": url}) is None


def test_the_production_wiring_gives_the_worker_the_dbc_builder(dbc_live, monkeypatch):
    from sniper.execution import raydium_launchlab_buy as LL

    class Thread:
        def __init__(self, *, target, name, daemon):
            pass

        def start(self):
            pass
    monkeypatch.setattr("threading.Thread", Thread)
    workers, learners = [], []
    real_worker, real_learner = LL.OnDemandLegWorker, lane.MissLearner
    monkeypatch.setattr(LL, "OnDemandLegWorker", lambda **kw: workers.append(real_worker(**kw)) or workers[-1])
    monkeypatch.setattr(lane, "MissLearner", lambda **kw: learners.append(real_learner(**kw)) or learners[-1])
    monkeypatch.setenv("ONCHAIN_WATCH_HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=watch")
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_ROUTED", "shadow", raising=False)
    dbc = B.SlotLaneDbcBuilder(user=USER, rpc=None, leg_fn=lambda: None)
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key="fixture")
    lane.start_slot_lane(rc, builders={"pump_curve": object(), "meteora_dbc": dbc},
                         signal_start_fn=lambda *a, **k: None, sol_usd_fn=lambda: None,
                         log=lambda line: None, roster_path="unused", conn_factory=object,
                         readonly_conn_factory=object, routed_baseline_fn=lambda *a, **k: None)
    (worker,) = workers
    assert worker.builders["meteora_dbc"] is dbc
    (learner,) = learners
    assert isinstance(learner.venue_pools["meteora_dbc"], B.DbcMintLearner)
    assert worker.admission_fn(_buy(EXOTIC), (8, 400.0)) in (None, "not_approved")


def test_a_tip_is_appended_last_and_passes_the_sender_guard(dbc_live):
    from sniper.execution import solana_sender_ab as S
    leg = _leg(pcb.WSOL_MINT, out=1_000_000_000)
    buy, _ = B.dbc_slot_buy(_routed(SOL), _facts(SOL))
    plain, _ = _builder(leg).build(buy, 100.0, SNAP)
    msg, detail = _builder(leg).build(buy, 100.0, SNAP, tip=(S.TIP_ACCOUNTS[3], S.TIP_LAMPORTS))
    assert msg is not None, detail
    assert S.tip_transfers(msg) == [(S.TIP_ACCOUNTS[3], 5_000)]
    assert S.guard_refusal(msg, 5_000) is None
    assert len(msg.instructions) == len(plain.instructions) + 1


def test_a_treated_crowded_send_prices_the_target_from_this_builds_cu_limit(dbc_live, monkeypatch):
    """Crowded fee on every slot-lane venue (Alpha GO 2026-10-16): DBC takes the treated priority."""
    from sniper.settings import memecoin as cfg
    from test_memecoin_crowded_fee import budget, fee_lamports
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS", 12_000_000)
    leg = _leg(pcb.WSOL_MINT, out=1_000_000_000)
    buy, _ = B.dbc_slot_buy(_routed(SOL), _facts(SOL))
    assert _builder(leg).accepts_priority_lamports is True
    plain, plain_detail = _builder(leg).build(buy, 100.0, SNAP)
    msg, detail = _builder(leg).build(buy, 100.0, SNAP, priority_lamports=10_000_000)
    limit, price = budget(msg)
    assert limit == detail["cu_limit"] and fee_lamports(limit, price) >= 10_000_000
    assert detail["priority_bid"]["crowded"] == "raised" and "priority_bid" not in plain_detail
    assert budget(plain)[1] < price
