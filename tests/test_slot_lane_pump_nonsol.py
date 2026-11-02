"""E2: pump AMM pools quoted in anything but SOL (spec
docs/superpowers/specs/2026-10-02-memecoin-slot-lane-exotic-quotes-live-design.md §2, operator GO
2026-10-02 12:0xZ). 54 refusals in 7 days, all pump.fun migrations, 11 quote mints, 4 of them USDC.

With "pump_amm" on MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES (and the lane live): a USDC pool spends the
clip straight from the wallet's USDC account; any other quote goes through E1's worker and comes
back with its leg. Off: exactly today's refusal. Frames are real buys read 2026-10-02
(tests/fixtures/memecoin/pumpswap_nonsol_frames.json): a USDC pool, an SPCX pool (Token-2022 xStock
quote) and a pumpCmXq pool, each with its pool account and both mints.
"""
import base64
import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as pcb
from sniper.execution import pumpswap_buy as ps
from sniper.services import memecoin_slot_lane as L

FX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "pumpswap_nonsol_frames.json").read_text())
BUYS = {b["quote_key"]: b for b in FX["buys"]}
USDC, SPCX = BUYS["EPjFWdd5"], BUYS["SPCXxcqX"]
USER = "3EKhb6kyrKQSzzYgR3oHVBtGwHKo6YzDxSjgEz2Bfpg6"
BUYBACK = "11111111111111111111111111111112"
BLOCKHASH = "11111111111111111111111111111111"
SNAP = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=150.0)


@pytest.fixture
def pump_listed(monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"pump_amm"}),
                        raising=False)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", "live")


def _facts(frame):
    """The pool cache's facts for a frame, through the cache's own _store (both mints read)."""
    cache = ps.PumpSwapPoolCache(rpc=None)
    facts = ps.parse_pool_account(frame["pool_account_b64"])
    mint = lambda m: {"owner": FX["mints"][m]["owner"], "data": [FX["mints"][m]["data_b64"], "base64"]}  # noqa: E731
    assert cache._store(frame["pool"], facts, mint(facts["base_mint"]), mint(facts["quote_mint"]))
    return cache


def _slot_buy(frame, **over):
    from sniper.services.memecoin_slot_signal import SlotBuy
    ev = ps.parse_buy_event(frame["buy_event_log"])
    fields = dict(venue="pump_amm", source_user_id="u1", handle="h", owner=ev["user"], signature=frame["signature"],
                  slot=frame["slot"], mint=None, pool=frame["pool"], sol_amount=0, token_amount=0,
                  virtual_sol_reserves=None, virtual_token_reserves=None,
                  real_sol_reserves=ev["pool_quote_token_reserves"],
                  real_token_reserves=ev["pool_base_token_reserves"], token_program=None,
                  trade_event_log=frame["buy_event_log"], recv_monotonic=time.monotonic(), recv_utc="t")
    fields.update(over)
    return SlotBuy(**fields)


# --- the pool cache -----------------------------------------------------------------------------

def test_the_pool_cache_keeps_the_quote_mints_decimals_and_program():
    facts = _facts(SPCX).get(SPCX["pool"])
    assert facts["quote_decimals"] == 6 and facts["quote_token_program"] == pcb.TOKEN_2022_PROGRAM
    assert facts["pump_migrated"] is True and facts["base_decimals"] == 6


# --- admission ----------------------------------------------------------------------------------

def test_off_the_list_a_non_sol_pool_refuses_as_today(monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset(), raising=False)
    assert L.amm_market_facts(_slot_buy(USDC), _facts(USDC), 150.0)[0] == "slot_lane_quote_not_sol"
    assert L.amm_market_facts(_slot_buy(SPCX), _facts(SPCX), 150.0)[0] == "slot_lane_quote_not_sol"


def test_a_usdc_pool_prices_at_one_dollar_a_usdc(pump_listed):
    cache = _facts(USDC)
    reason, facts = L.amm_market_facts(_slot_buy(USDC), cache, 150.0)
    assert reason is None, reason
    ev, pool = ps.parse_buy_event(USDC["buy_event_log"]), cache.get(USDC["pool"])
    base_post, quote_post = ps.post_trade_reserves(ev)
    price = (quote_post + pool["virtual_quote"]) / 1e6 / (base_post / 10 ** pool["base_decimals"])
    assert facts["price_usd"] == pytest.approx(price) and facts["quote_mint"] == pcb.USDC_MINT
    assert facts["their_usd"] == pytest.approx(ps.buy_event_gross_quote(ev) / 1e6)   # the gross basis


def test_an_exotic_pool_goes_to_the_worker_then_prices_with_the_legs_quote(pump_listed):
    cache = _facts(SPCX)
    assert L.amm_market_facts(_slot_buy(SPCX), cache, 150.0)[0] == "slot_lane_pump_quote_not_supported"
    assert "slot_lane_pump_quote_not_supported" in L.ONDEMAND_QUOTE_REASONS
    reason, facts = L.amm_market_facts(_slot_buy(SPCX), cache, 150.0, quote=(6, 250.0))
    assert reason is None
    ev, pool = ps.parse_buy_event(SPCX["buy_event_log"]), cache.get(SPCX["pool"])
    base_post, quote_post = ps.post_trade_reserves(ev)
    assert facts["price_usd"] == pytest.approx(
        (quote_post + pool["virtual_quote"]) / 1e6 / (base_post / 1e6) * 250.0)


def test_a_routed_non_sol_pump_buy_converts_only_when_listed(monkeypatch):
    from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes
    from sniper.services import memecoin_slot_signal as S
    fields = S._amm(anchor_event_bytes(USDC["buy_event_log"]), None, any_user=True)   # the real decoder
    pump_ev = S.RoutedPumpBuy(venue="pump_amm", fields=tuple(fields.items()),
                              trade_event_log=USDC["buy_event_log"], token_program=None)
    pool = _facts(USDC).get(USDC["pool"])
    routed = S.RoutedBuy(source_user_id="u1", handle="h", owner="O", signature=USDC["signature"], slot=1,
                         mint=pool["base_mint"], token_delta_atomic=1, token_decimals=6,
                         usdc_spent_atomic=1, recv_monotonic=1.0, recv_utc="t", paired_monotonic=1.0,
                         venue_events=(pump_ev,))
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset(), raising=False)
    assert L.pump_slot_buy(routed, _facts(USDC)) == (None, "pump_quote_not_sol")
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"pump_amm"}), raising=False)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", "live")
    buy, why = L.pump_slot_buy(routed, _facts(USDC))
    assert why is None and buy.quote_mint == pcb.USDC_MINT and buy.venue == "pump_amm"


# --- the builder --------------------------------------------------------------------------------

def _builder(frame):
    b = ps.SlotLanePumpSwapBuilder(user=USER, jup_call=lambda *a, **k: pytest.fail("no SOL leg here"),
                                   rpc=lambda *a: None, clock=lambda: 50.0)
    b._buyback = BUYBACK
    b.pools = _facts(frame)
    return b


def _programs(message):
    keys = [str(k) for k in message.account_keys]
    return keys, [keys[ix.program_id_index] for ix in message.instructions]


def test_a_usdc_pool_spends_the_clip_from_the_usdc_account_with_no_leg(pump_listed, monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    b = _builder(USDC)
    message, validity = b.build(_slot_buy(USDC), 10.0, SNAP)
    assert message is not None, validity
    keys, programs = _programs(message)
    assert programs == [pcb.COMPUTE_BUDGET_PROGRAM if hasattr(pcb, "COMPUTE_BUDGET_PROGRAM")
                        else "ComputeBudget111111111111111111111111111111"] * 2 + [pcb.ATA_PROGRAM, ps.AMM_PROGRAM]
    assert str(pcb.ata(pcb._pk(USER), pcb._pk(pcb.USDC_MINT), pcb._pk(pcb.TOKEN_PROGRAM))) in keys
    assert str(pcb.ata(pcb._pk(USER), pcb._pk(pcb.WSOL_MINT), pcb._pk(pcb.TOKEN_PROGRAM))) not in keys
    assert b.last_build["spend_lamports"] == 10_000_000                     # the clip, in USDC atoms
    limit = validity["pump_price_limit"]
    assert limit["cost_usdc_atomic"] == 10_000_000 if "cost_usdc_atomic" in limit else True
    assert b.last_build["min_tokens_out"] <= b.last_build["expected_tokens_out"]
    assert validity.get("leg_age_s") is None


def _leg(quote_mint, out, usdc=10_000_000):
    return {"ixs": [], "tables": [], "usdc_atomic": usdc, "min_out_lamports": out * 985 // 1000,
            "out_lamports": out, "fetched_at": 50.0, "quote": {}, "output_mint": quote_mint}


def test_an_exotic_pool_spends_the_legs_minimum_on_its_token_2022_quote_account(pump_listed, monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    b = _builder(SPCX)
    out = 40_000                                                           # 0.04 SPCX for $10
    message, validity = b.build(_slot_buy(SPCX), 10.0, SNAP, leg=_leg(SPCX["quote_mint"], out),
                                quote=(6, 250.0, 0, 0))
    assert message is not None, validity
    keys, _programs_ = _programs(message)
    assert str(pcb.ata(pcb._pk(USER), pcb._pk(SPCX["quote_mint"]), pcb._pk(pcb.TOKEN_2022_PROGRAM))) in keys
    assert b.last_build["spend_lamports"] == out * 985 // 1000
    assert b.last_build["min_tokens_out"] <= b.last_build["expected_tokens_out"]


@pytest.mark.parametrize("overlap", [None, "pool", "pool_base_token_account", "pool_quote_token_account"])
def test_exotic_guard_prefix_proof_binds_every_account_without_changing_signed_amounts(
        pump_listed, monkeypatch, overlap):
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    builder, buy = _builder(SPCX), _slot_buy(SPCX)
    leg = _leg(SPCX["quote_mint"], 40_000)
    original, before = builder.build(buy, 10.0, SNAP, leg=leg, quote=(6, 250.0, 0, 0))
    assert original is not None, before
    facts = builder.pools.get(buy.pool)
    target = facts[overlap] if overlap is not None else USER
    # A readonly prefix meta becomes writable globally when the later buy writes the same key.
    leg["ixs"] = [Instruction(Pubkey.from_string(pcb.SYSTEM_PROGRAM), b"", [
        AccountMeta(Pubkey.from_string(target), False, False)])]
    message, after = builder.build(buy, 10.0, SNAP, leg=leg, quote=(6, 250.0, 0, 0))
    assert message is not None, after
    assert before["entry_slippage"]["funding_prefix_disjoint"] is True
    assert after["entry_slippage"]["funding_prefix_disjoint"] is (overlap is None)
    for key in ("source_sig", "source_slot", "spend_atomic", "min_tokens_atomic"):
        assert after["entry_slippage"][key] == before["entry_slippage"][key]
    def own_buy(message):
        return next(bytes(ix.data) for ix in message.instructions
                    if str(message.account_keys[ix.program_id_index]) == ps.AMM_PROGRAM)
    assert own_buy(message) == own_buy(original)


def test_guard_prefix_proof_uses_the_compiled_prefix_even_if_the_shared_leg_changes(pump_listed, monkeypatch):
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    builder = _builder(SPCX)
    leg = _leg(SPCX["quote_mint"], 40_000)
    leg["ixs"] = [Instruction(Pubkey.from_string(pcb.SYSTEM_PROGRAM), b"", [
        AccountMeta(Pubkey.from_string(SPCX["pool"]), False, False)])]
    build = ps.build_pumpswap_buy_message
    def changed_leg(*args, **kwargs):
        result = build(*args, **kwargs)
        leg["ixs"].clear()
        return result
    monkeypatch.setattr(ps, "build_pumpswap_buy_message", changed_leg)
    message, validity = builder.build(_slot_buy(SPCX), 10.0, SNAP, leg=leg, quote=(6, 250.0, 0, 0))
    assert message is not None, validity
    assert validity["entry_slippage"]["funding_prefix_disjoint"] is False


@pytest.mark.parametrize("case, reason", [
    ("no_leg", "leg"), ("wrong_quote_leg", "leg_wrong_quote"), ("off_the_list", "not wSOL")])
def test_exotic_build_refusals(pump_listed, monkeypatch, case, reason):
    b = _builder(SPCX)
    kw = {"leg": _leg(SPCX["quote_mint"], 40_000), "quote": (6, 250.0, 0, 0)}
    if case == "no_leg":
        kw = {}
    elif case == "wrong_quote_leg":
        kw["leg"] = _leg(pcb.USDC_MINT, 40_000)
    elif case == "off_the_list":
        monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset(), raising=False)
    msg, got = b.build(_slot_buy(SPCX), 10.0, SNAP, **kw)
    assert msg is None and reason in got, got


def test_the_pump_builder_serves_the_on_demand_worker():
    b = _builder(SPCX)
    assert b.REASON_PREFIX == "slot_lane_pump"
    quote_mint, quote, refusal = b.ondemand_quote(_slot_buy(SPCX))
    assert (quote_mint, quote, refusal) == (SPCX["quote_mint"], (6, 0, 0), None)


# --- the lane: cap and live gate ----------------------------------------------------------------

def test_non_sol_pump_sends_count_under_the_exotic_cap_and_sol_pump_is_uncapped(pump_listed):
    lane = L.SlotLane(conn=None, executor={}, build_fn=None, snapshot_fn=None, armed_ids_fn=None,
                      kill_tripped_fn=None, now_iso_fn=None, lock_key="k", mode="live")
    usdc_buy = dataclasses.replace(_slot_buy(USDC), quote_mint=pcb.USDC_MINT)
    sol_buy = dataclasses.replace(_slot_buy(USDC), quote_mint=pcb.WSOL_MINT)
    assert lane._cap_key(usdc_buy) == "pump_amm:exotic" and L.venue_live_cap("pump_amm:exotic") == 2
    lane._venue_live_sends["pump_amm:exotic"].extend([time.monotonic() - 5] * 2)
    assert lane._venue_live_capped(usdc_buy) == "slot_lane_venue_live_cap"
    assert lane._venue_live_capped(sol_buy) is None


def test_pump_amm_is_exotic_live_only_with_the_lane_live(monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"pump_amm"}), raising=False)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", "shadow")
    assert L.exotic_live("pump_amm") is False
    monkeypatch.setattr(L.cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", "live")
    assert L.exotic_live("pump_amm") is True


def test_a_usdc_pump_buy_runs_live_on_usdc_and_never_reserves_native_sol(pump_listed, monkeypatch, tmp_path):
    """The lane prefers SOL funding for pump venues when the clip fits the SOL buffer; a pool quoted
    in USDC (or anything else) cannot be paid in SOL, so it reserves USDC."""
    import contextlib
    import sqlite3
    from sniper.execution import exec_lock
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(L.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 0.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE", {}, raising=False)
    b = _builder(USDC)
    seen = {}

    def execute(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        # Operator 2026-10-20 (#486): quality-approved copies may land in any slot, so the lane
        # passes no source-slot window to the executor.
        assert (entry_min_slot, entry_max_slot) == (None, None)
        assert callable(entry_head_fn)
        seen.update(pending=dict(pending), validity=validity)
        return {"status": "filled", "position_id": 1, "sig": "TXU"}
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    now = time.monotonic()
    snap = L.PrefetchSnapshot(sol_usd=150.0, usdc_balance=400.0, sol_balance=50.0, blockhash=BLOCKHASH,
                              last_valid_height=1, observed_mono={"sol_usd": now - 1, "balances": now - 1,
                                                                  "blockhash": now - 1})
    import sniper.services.memecoin_real_runner as R
    monkeypatch.setattr(R, "source_buy_family_reason", lambda **k: (None, {}))
    lane = L.SlotLane(conn=c, executor={"slot_execute_fn": execute}, build_fn=L.dispatch_build({"pump_amm": b}),
                      snapshot_fn=lambda: snap, armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False,
                      now_iso_fn=lambda: "2026-10-02T12:00:00+00:00", lock_key="sol-test", mode="live",
                      amm_pools=b.pools, shadow_clip_fn=lambda: 10.0, sol_buffer_cap_usd=1000.0)
    buy = _slot_buy(USDC, recv_monotonic=now)
    with contextlib.closing(c):
        result = lane.handle(buy)
        assert result["status"] == "filled", result
        assert seen["pending"].get("funding_asset") in (None, "usdc")
        assert lane._venue_live_sends["pump_amm:exotic"] and lane._canary == {"pump_amm:exotic": 1}


def test_the_production_wiring_hands_pump_exotic_buys_to_the_worker_with_the_pump_pools(pump_listed, monkeypatch):
    """start_slot_lane (no threads): with "pump_amm" listed the worker serves pump AMM, and its own
    admission prices a pump buy from the PumpSwap pool cache with the leg's quote."""
    from sniper.execution import raydium_launchlab_buy as LL

    class Thread:
        def __init__(self, *, target, name, daemon):
            pass

        def start(self):
            pass
    monkeypatch.setattr("threading.Thread", Thread)
    workers, lanes = [], []
    real_worker, real_lane = LL.OnDemandLegWorker, L.SlotLane
    monkeypatch.setattr(LL, "OnDemandLegWorker", lambda **kw: workers.append(real_worker(**kw)) or workers[-1])
    monkeypatch.setattr(L, "SlotLane", lambda **kw: lanes.append(real_lane(**kw)) or lanes[-1])
    pump = _builder(SPCX)
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key="fixture")
    L.start_slot_lane(rc, builders={"pump_curve": object(), "pump_amm": pump},
                      signal_start_fn=lambda *a, **k: None, sol_usd_fn=lambda: None, log=lambda line: None,
                      roster_path="unused", conn_factory=object, readonly_conn_factory=object)
    (worker,), (lane,) = workers, lanes
    assert "pump_amm" in worker.builders and lane.exotic_offer is not None
    monkeypatch.setattr(L, "admission_refusal", lambda b, **k: (
        "amm_pools missing" if k.get("amm_pools") is None else None, {}))
    assert worker.admission_fn(_slot_buy(SPCX), (6, 250.0)) is None


def test_a_usdc_build_prices_the_quote_at_exactly_one_dollar(pump_listed, monkeypatch):
    """Gamma LOW on 1c8368081: the builder's USDC quote USD was not pinned; at 1.05 the price floor
    loosened 5% on every USDC pool buy and every test still passed. The signed minimum must equal
    capped_min_out at quote USD 1.0 in the quote's own 6 decimals."""
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    b = _builder(USDC)
    message, validity = b.build(_slot_buy(USDC), 10.0, SNAP)
    assert message is not None, validity
    ev, facts = ps.parse_buy_event(USDC["buy_event_log"]), b.pools.get(USDC["pool"])
    base_post, quote_post = ps.post_trade_reserves(ev)
    spend = pcb.clip_atomic(10.0)
    expected = ps.expected_base_out(spend, base_post, quote_post, facts["virtual_quote"], ps.total_fee_bps(ev))
    price = (quote_post + facts["virtual_quote"]) / 1e6 / (base_post / 10 ** facts["base_decimals"]) * 1.0
    min_out, _output_floor, price_floor = pcb.capped_min_out(
        expected, 30.0, source_price_usd=price, source_cost_usdc_atomic=spend, source_price_cap_pct=30.0,
        base_decimals=facts["base_decimals"], source_average_price_usd=None)
    assert price_floor == min_out, "the price floor binds here, so the quote's USD price shows in it"
    assert b.last_build["min_tokens_out"] == min_out
    assert validity["pump_price_limit"]["source_price_usd"] == pytest.approx(price, rel=1e-12)


def _published_quote(cache, value=None, stamp=999.0, old=None):
    from test_slot_lane_cpmm import _parsed_mint
    old = cache.get(SPCX["pool"]) if old is None else old
    value = _parsed_mint(decimals=6) if value is None else value
    value["owner"] = old["quote_token_program"]
    value["data"]["parsed"]["type"] = "mint"
    return cache.publish_quote_snapshot(SPCX["pool"], expected_facts=old, value=value, read_at=stamp)


def test_quote_snapshot_uses_one_value_and_original_read_time():
    from test_slot_lane_cpmm import _parsed_mint
    cache = _facts(SPCX)
    cache.clock = lambda: 1000.0
    old = cache.get(SPCX["pool"])
    value = _parsed_mint(decimals=6)
    new = _published_quote(cache, value)
    assert new is cache.get(SPCX["pool"]) and new is not old
    assert new["quote_read_at"] == 999.0
    value["data"]["parsed"]["info"]["decimals"] = 9
    assert new["mint_values"][SPCX["quote_mint"]]["data"]["parsed"]["info"]["decimals"] == 6
    assert _published_quote(cache, value, stamp=999.5, old=old) is None


@pytest.mark.parametrize("stamp", [True, "999", float("nan"), float("inf"), 1001., 700., -1.])
def test_quote_publication_refuses_invalid_or_expired_stamp(stamp):
    cache = _facts(SPCX)
    cache.clock = lambda: 1000.0
    old = cache.get(SPCX["pool"])
    assert _published_quote(cache, stamp=stamp) is None
    assert cache.get(SPCX["pool"]) is old


@pytest.mark.parametrize("mutation", ["binary", "owner", "type", "decimals"])
def test_quote_publication_refuses_wrong_identity(mutation):
    from test_slot_lane_cpmm import _parsed_mint
    cache = _facts(SPCX)
    cache.clock = lambda: 1000.0
    old = cache.get(SPCX["pool"])
    value = _parsed_mint(decimals=6)
    value["owner"] = old["quote_token_program"]
    value["data"]["parsed"]["type"] = "mint"
    if mutation == "binary": value["data"] = ["AAAA", "base64"]
    if mutation == "owner": value["owner"] = ps.TOKEN_PROGRAM
    if mutation == "type": value["data"]["parsed"]["type"] = "account"
    if mutation == "decimals": value["data"]["parsed"]["info"]["decimals"] = 9
    assert cache.publish_quote_snapshot(SPCX["pool"], expected_facts=old, value=value, read_at=999.) is None


def test_pool_known_callback_is_outside_lock_and_excludes_restart():
    cache = _facts(SPCX)
    offered = []
    def offer(pool, facts):
        assert cache._lock.acquire(blocking=False)
        cache._lock.release()
        offered.append((pool, facts))
    cache.on_pool_known = offer
    old = cache.get(SPCX["pool"])
    mint = lambda m: {"owner": FX["mints"][m]["owner"], "data": [FX["mints"][m]["data_b64"], "base64"]}
    for persisted in (True, False):
        assert cache._store(SPCX["pool"], ps.parse_pool_account(SPCX["pool_account_b64"]),
                            mint(old["base_mint"]), mint(old["quote_mint"]), oldest=persisted)
    assert len(offered) == 1 and offered[0][1] is cache.get(SPCX["pool"])


@pytest.mark.parametrize("extension,state,expected", [
    ("transferHook", {"programId": "hook"}, None), ("nonTransferable", {}, None),
    ("defaultAccountState", {"accountState": "frozen"}, None),
    ("pausableConfig", {"paused": True}, None),
    ("transferFeeConfig", {"olderTransferFee": {"transferFeeBasisPoints": 1, "maximumFee": 9},
                           "newerTransferFee": {"transferFeeBasisPoints": 1, "maximumFee": 9}}, (6, 1, 9)),
    ("transferFeeConfig", {"olderTransferFee": {"transferFeeBasisPoints": 1, "maximumFee": 9},
                           "newerTransferFee": {"epoch": 2, "transferFeeBasisPoints": 2, "maximumFee": 9}}, None),
])
def test_complete_unsafe_reads_replace_old_passing_snapshot(monkeypatch, extension, state, expected):
    from test_slot_lane_cpmm import _parsed_mint
    from sniper.execution import raydium_cpmm_buy as cp
    monkeypatch.setattr(cp, "epoch_now", lambda: None)
    cache = _facts(SPCX)
    cache.clock = lambda: 1000.
    assert _published_quote(cache)["quote_facts"] == (6, 0, 0)
    result = _published_quote(cache, _parsed_mint((extension, state)), stamp=999.5)
    assert result is cache.get(SPCX["pool"]) and result["quote_facts"] == expected
    assert result["quote_read_at"] == 999.5


def test_static_reread_retains_original_quote_stamp_and_changed_identity_drops_it():
    cache = _facts(SPCX)
    cache.clock = lambda: 1000.
    before = _published_quote(cache)
    mint = lambda m: {"owner": FX["mints"][m]["owner"], "data": [FX["mints"][m]["data_b64"], "base64"]}
    facts = ps.parse_pool_account(SPCX["pool_account_b64"])
    assert cache._store(SPCX["pool"], facts, mint(facts["base_mint"]), mint(facts["quote_mint"]))
    assert cache.get(SPCX["pool"])["quote_read_at"] == before["quote_read_at"]
    facts = ps.parse_pool_account(SPCX["pool_account_b64"])
    facts["virtual_quote"] += 1
    assert cache._store(SPCX["pool"], facts, mint(facts["base_mint"]), mint(facts["quote_mint"]))
    assert "quote_read_at" not in cache.get(SPCX["pool"])


def test_older_quote_response_cannot_replace_a_newer_snapshot():
    cache = _facts(SPCX)
    cache.clock = lambda: 1000.
    newer = _published_quote(cache, stamp=999.5)
    assert _published_quote(cache, stamp=999.) is None
    assert cache.get(SPCX["pool"]) is newer


@pytest.mark.parametrize("extension", [
    {"extension": "transferHook"}, {"extension": "transferHook", "state": {}},
    {"extension": "transferHook", "state": {"programId": False}},
    {"extension": "pausableConfig"}, {"extension": "pausableConfig", "state": {}},
    {"extension": "pausableConfig", "state": {"paused": None}},
    {"extension": "pausableConfig", "state": {"paused": "false"}},
    {"extension": "defaultAccountState", "state": {}},
    {"extension": "defaultAccountState", "state": {"accountState": "unknown"}},
    {"extension": "transferHook", "state": []},
])
def test_incomplete_safety_extension_does_not_establish_safe_quote(extension):
    from test_slot_lane_cpmm import _parsed_mint
    cache = _facts(SPCX)
    cache.clock = lambda: 1000.
    old = _published_quote(cache)
    value = _parsed_mint()
    value["data"]["parsed"]["info"]["extensions"] = [extension]
    assert _published_quote(cache, value, stamp=999.5) is None
    assert cache.get(SPCX["pool"]) is old and old["quote_read_at"] == 999.


@pytest.mark.parametrize("name,state", [("transferHook", {"programId": None}),
                                       ("pausableConfig", {"paused": False}),
                                       ("defaultAccountState", {"accountState": "initialized"})])
def test_explicit_inactive_extension_remains_zero_fee(name, state):
    from test_slot_lane_cpmm import _parsed_mint
    cache = _facts(SPCX)
    cache.clock = lambda: 1000.
    assert _published_quote(cache, _parsed_mint((name, state)))["quote_facts"] == (6, 0, 0)


def test_first_prepared_pump_buy_builds_and_signs_without_provider_calls(pump_listed, monkeypatch):
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    from sniper.execution import raydium_launchlab_buy as LL
    from test_slot_lane_cpmm import _parsed_mint
    from test_slot_lane_exotic_warm_leg import Clock
    clock, b, kp = Clock(50.0), _builder(SPCX), Keypair()
    b.user, b.clock, b.pools.clock = str(kp.pubkey()), clock.monotonic, clock.monotonic
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS', True, raising=False)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_REAL_CHASE_MAX_PCT', 30.0)
    old = b.pools.get(SPCX['pool'])
    value = _parsed_mint(decimals=6)
    value['owner'] = old['quote_token_program']
    value['data']['parsed']['type'] = 'mint'
    prepared = b.pools.publish_quote_snapshot(SPCX['pool'], expected_facts=old, value=value, read_at=49.9)
    assert prepared is not None
    leg = _leg(SPCX['quote_mint'], 40_000)
    leg['quote'] = dict(inputMint=pcb.USDC_MINT, outputMint=SPCX['quote_mint'],
        inAmount='10000000', outAmount='40000', otherAmountThreshold='39400',
        swapMode='ExactIn', slippageBps=pcb.DEFAULT_LEG_SLIPPAGE_BPS,
        routePlan=[{'swapInfo': {'label': 'fixture', 'ammKey': SPCX['pool']}}])
    event = ps.parse_buy_event(SPCX['buy_event_log'])
    model = ps.exotic_pool_round_trip(event, prepared, leg)
    assert model['state'] == 'measured'
    leg['reverse'] = dict(input_mint=SPCX['quote_mint'], output_mint=pcb.USDC_MINT,
        in_atomic=model['quote_back_atomic'], out_usdc_atomic=10_000_000, min_usdc_atomic=9_900_000,
        usdc_atomic=leg['usdc_atomic'], quote_spend_atomic=leg['min_out_lamports'],
        fetched_at=leg['fetched_at'])
    assert ps.exotic_round_trip(event, prepared, leg, clock_now=50., max_age_s=3.)['state'] == 'measured'
    warm = LL.WarmExoticLegs(clock=clock, fetch_fn=lambda entry: (leg, [200, 200]))
    warm.set_clip_inventory((10_000_000,))
    warm.seed('pump_amm', SPCX['quote_mint'], 10_000_000, builder=b, facts=prepared)
    warm.refresh_once()
    requeued = []
    worker = LL.OnDemandLegWorker(builders={'pump_amm': b}, admission_fn=lambda buy, quote: None,
        snapshot_fn=lambda: SNAP, clip_fn=lambda: 10.0, offer_fn=lambda buy, result: None,
        start=False, clock=clock, live_fn=lambda venue: True, requeue_fn=requeued.append,
        refuse_fn=lambda buy, reason: pytest.fail(reason), warm=warm)
    def forbidden(*args, **kwargs): pytest.fail('provider call after a completely prepared source buy')
    b.pools.rpc = b.rpc = b.read_rpc = forbidden
    warm.fetch_fn = forbidden
    monkeypatch.setattr(pcb, '_funded_jup_call', forbidden)
    buy = _slot_buy(SPCX, recv_monotonic=clock.monotonic())
    assert worker.process(buy)['warm_leg'] is True
    assert requeued[0].recv_monotonic == buy.recv_monotonic
    message, detail = L.dispatch_build({'pump_amm': b})(requeued[0], 10.0, SNAP)
    assert message is not None, detail
    assert bytes(VersionedTransaction(message, [kp]))
