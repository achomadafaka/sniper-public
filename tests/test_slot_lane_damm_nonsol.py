"""E3: Meteora DAMM v2 pools quoted in USDC or any other token (spec
docs/superpowers/specs/2026-10-02-memecoin-slot-lane-exotic-quotes-live-design.md; operator yes
2026-10-02 "fill regardless of USDC or SOL quote"; Alpha approved the rule-5 pass with one addition:
a USDC balance below the clip, or unread, refuses before signing and never becomes a SOL build).

DAMM prices from the source's routed average whatever the pool's quote, so only the funding changes:
USDC spends the clip from the USDC account; another quote spends E1's on-demand leg. Gate:
"meteora_damm_v2" on MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES plus DAMM's own mode (shadow today, so
E3 builds and simulates in shadow until DAMM goes live). The pools are the B2 SOL fixtures with
their quote side swapped: the swap's accounts do not depend on the quote, and the chain simulation
of a real non-SOL pool waits for a flat book.
"""
import dataclasses
import struct
import time
from types import SimpleNamespace

import pytest

from sniper.execution import meteora_damm_v2_buy as D
from sniper.execution import pump_curve_buy as pcb
from sniper.services import memecoin_slot_lane as lane
from sniper.settings import memecoin as cfg
from test_slot_lane_damm_v2 import (BLOCKHASH, CLASSIC, USER, FakeRpc, _learned, _routed, conn)  # noqa: F401

SPCX = "SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb"
SNAP = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=150.0)


@pytest.fixture
def damm_listed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"meteora_damm_v2"}),
                        raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "shadow", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)


def _quoted(quote, program=pcb.TOKEN_PROGRAM, decimals=6):
    """The CLASSIC pool learned, with its SOL side read as `quote`."""
    cache = _learned(CLASSIC)
    facts = cache.get(CLASSIC["coin"])
    side = "mint_a" if facts["mint_a"] != CLASSIC["coin"] else "mint_b"
    facts.update(quote=quote, quote_facts=(decimals, 0, 0), quote_program=program, **{side: quote})
    facts["program_a" if side == "mint_a" else "program_b"] = program
    return cache


def _builder(cache):
    b = D.SlotLaneDammV2Builder(user=USER, rpc=FakeRpc(CLASSIC), leg_fn=lambda: pytest.fail(
        "a non-SOL build never spends the SOL leg"), clock=lambda: 1000.0, start_learner=False)
    b.pools = cache
    return b


def _swap(message):
    keys = [str(k) for k in message.account_keys]
    (swap,) = [ix for ix in message.instructions if keys[ix.program_id_index] == D.PROGRAM]
    amount_in, min_out = struct.unpack("<QQ", bytes(swap.data)[8:24])
    return keys, [keys[i] for i in bytes(swap.accounts)], amount_in, min_out


# --- the pool cache and admission -----------------------------------------------------------------

def test_the_pool_cache_keeps_the_quotes_facts_and_program():
    facts = _learned(CLASSIC).get(CLASSIC["coin"])
    assert facts["quote"] == D.WSOL_MINT and facts["quote_facts"] == (9, 0, 0)
    assert facts["quote_program"] == pcb.TOKEN_PROGRAM


def test_admission_takes_a_usdc_pool_only_when_listed_and_an_exotic_one_with_the_legs_quote(
        damm_listed, monkeypatch):
    cache = _quoted(pcb.USDC_MINT)
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), cache)
    reason, facts = D.market_facts(buy, cache, 150.0)
    assert reason is None and facts["price_usd"] == pytest.approx(1e-5)   # the routed average
    exotic = _quoted(SPCX, program=pcb.TOKEN_2022_PROGRAM)
    ebuy, _ = D.damm_slot_buy(_routed(CLASSIC), exotic)
    assert D.market_facts(ebuy, exotic, 150.0)[0] == "slot_lane_damm_v2_quote_not_supported"
    assert "slot_lane_damm_v2_quote_not_supported" in lane.ONDEMAND_QUOTE_REASONS
    assert D.market_facts(ebuy, exotic, 150.0, quote=(6, 250.0))[0] is None
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset(), raising=False)
    assert D.market_facts(buy, cache, 150.0)[0] == "slot_lane_damm_v2_quote_not_supported"


def test_a_non_sol_damm_buy_is_live_only_when_listed_and_damm_is_live(damm_listed, monkeypatch):
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), _quoted(pcb.USDC_MINT))
    assert lane.shadow_only(buy) is True                              # DAMM itself is shadow
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "live", raising=False)
    assert lane.shadow_only(buy) is False and lane.exotic_live("meteora_damm_v2")
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset(), raising=False)
    assert lane.shadow_only(buy) is True


# --- the build --------------------------------------------------------------------------------------

def test_a_usdc_pool_spends_the_clip_from_the_usdc_account(damm_listed):
    from sniper.execution.pump_curve_buy import source_price_floor_atomic
    from sniper.execution.pump_native_exit import _pk, ata
    cache = _quoted(pcb.USDC_MINT)
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), cache)
    message, validity = _builder(cache).build(buy, 100.0, SNAP)
    assert message is not None, validity
    keys, accounts, amount_in, min_out = _swap(message)
    usdc_ata = str(ata(_pk(USER), _pk(pcb.USDC_MINT), _pk(pcb.TOKEN_PROGRAM)))
    assert accounts[2] == usdc_ata and amount_in == 100_000_000
    assert min_out == source_price_floor_atomic(100_000_000, 1e-5, 30.0, base_decimals=6)
    assert str(ata(_pk(USER), _pk(D.WSOL_MINT), _pk(pcb.TOKEN_PROGRAM))) not in keys   # no wSOL at all
    assert validity["pump_price_limit"]["cost_usdc_atomic"] == 100_000_000


def _leg(quote, out=40_000, usdc=100_000_000):
    return {"ixs": [], "tables": [], "usdc_atomic": usdc, "min_out_lamports": out * 985 // 1000,
            "out_lamports": out, "fetched_at": 1000.0, "quote": {}, "output_mint": quote}


def test_an_exotic_pool_spends_the_legs_minimum_from_its_token_2022_account(damm_listed):
    from sniper.execution.pump_native_exit import _pk, ata
    cache = _quoted(SPCX, program=pcb.TOKEN_2022_PROGRAM)
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), cache)
    message, validity = _builder(cache).build(buy, 100.0, SNAP, leg=_leg(SPCX), quote=(6, 250.0, 0, 0))
    assert message is not None, validity
    _keys, accounts, amount_in, _min = _swap(message)
    assert accounts[2] == str(ata(_pk(USER), _pk(SPCX), _pk(pcb.TOKEN_2022_PROGRAM)))
    assert amount_in == 40_000 * 985 // 1000
    assert validity["pump_price_limit"]["cost_usdc_atomic"] == 100_000_000


@pytest.mark.parametrize("case, reason", [
    ("no_leg", "exotic_leg_missing"), ("wrong_quote_leg", "leg_wrong_quote"),
    ("native", "native_funding_not_supported_on_damm_v2")])
def test_exotic_build_refusals(damm_listed, case, reason):
    cache = _quoted(SPCX, program=pcb.TOKEN_2022_PROGRAM)
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), cache)
    kw = {"leg": _leg(SPCX), "quote": (6, 250.0, 0, 0)}
    if case == "no_leg":
        kw = {}
    elif case == "wrong_quote_leg":
        kw["leg"] = _leg(pcb.USDC_MINT)
    elif case == "native":
        kw = {"funding_lamports": 1}
    assert _builder(cache).build(buy, 100.0, SNAP, **kw) == (None, reason)


def test_the_damm_builder_serves_the_on_demand_worker(damm_listed):
    cache = _quoted(SPCX, program=pcb.TOKEN_2022_PROGRAM)
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), cache)
    assert _builder(cache).ondemand_quote(buy) == (SPCX, (6, 0, 0), None)


# --- the lane: USDC never falls through to SOL, and refuses an unaffordable clip (Alpha) -----------

def _live_usdc_lane(conn, tmp_path, monkeypatch, usdc_balance, sent):  # noqa: F811
    # Source initiation is tested with real receipts in test_memecoin_solana_source_initiation.
    from sniper.services import memecoin_source_initiation as initiation
    monkeypatch.setattr(initiation, "source_initiation_refusal", lambda *_: None)
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    for name, value in (("MEMECOIN_REAL_ENABLED", True), ("MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0),
                        ("MEMECOIN_REAL_MAX_MCAP_USD", 1_000_000.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", {}),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 0.0), ("MEMECOIN_SLOT_LANE_DAMM_V2", "live"),
                        ("MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE", {})):
        monkeypatch.setattr(cfg, name, value, raising=False)
    cache = _quoted(pcb.USDC_MINT)

    def execute(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                entry_min_slot=None, entry_max_slot=None, entry_head_fn):
        # PR #486 (2026-10-20): automatic Solana entries carry no source-slot window; both bounds are None.
        assert (entry_min_slot, entry_max_slot) == (None, None)
        assert callable(entry_head_fn)
        sent.append({"pending": dict(pending), "validity": validity})
        return {"status": "filled", "position_id": 1}
    now = time.monotonic()
    snap = lane.PrefetchSnapshot(sol_usd=150.0, usdc_balance=usdc_balance, sol_balance=50.0,
                                 blockhash=BLOCKHASH, last_valid_height=1,
                                 observed_mono={k: now for k in ("sol_usd", "balances", "blockhash")})
    ln = lane.SlotLane(conn=conn, executor={"slot_execute_fn": execute},
                       build_fn=lane.dispatch_build({"meteora_damm_v2": _builder(cache)}),
                       snapshot_fn=lambda: snap, armed_ids_fn=lambda: {"w1"}, kill_tripped_fn=lambda: False,
                       now_iso_fn=lambda: "2026-10-01T20:30:00+00:00", lock_key="sol-test", mode="live",
                       shadow_clip_fn=lambda: 100.0, damm_pools=cache, sol_buffer_cap_usd=1000.0)
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), cache)
    return ln, dataclasses.replace(buy, recv_monotonic=now)


def test_a_live_usdc_damm_buy_reserves_usdc_and_counts_under_its_exotic_cap(damm_listed, conn, tmp_path,
                                                                            monkeypatch):
    sent = []
    ln, buy = _live_usdc_lane(conn, tmp_path, monkeypatch, 400.0, sent)
    result = ln.handle(buy)
    assert result["status"] == "filled", result
    (call,) = sent
    assert call["pending"].get("funding_asset") in (None, "usdc")
    assert ln._venue_live_sends["meteora_damm_v2:exotic"] and not ln._venue_live_sends["meteora_damm_v2"]


@pytest.mark.parametrize("balance", [0.0, None])
def test_no_usdc_or_an_unread_balance_refuses_before_signing(damm_listed, conn, tmp_path, monkeypatch,
                                                             balance):
    sent = []
    ln, buy = _live_usdc_lane(conn, tmp_path, monkeypatch, balance, sent)
    result = ln.handle(buy)
    assert sent == [] and result.get("pending_id") is None, result
    if balance is None:
        assert result["reason"] == "slot_lane_stale_balances"
    pend = conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'").fetchone()[0]
    assert pend == 0


def test_a_balance_below_the_clip_spends_only_what_the_reservation_approved(damm_listed, conn, tmp_path,
                                                                            monkeypatch):
    """The reservation never approves more than spendable USDC (real_trade_size), so a USDC build
    spends the approved clip, which the balance covers; it is never a SOL build."""
    sent = []
    ln, buy = _live_usdc_lane(conn, tmp_path, monkeypatch, 50.0, sent)
    result = ln.handle(buy)
    (call,) = sent
    approved = float(call["pending"]["reserved_notional_usd"])
    assert approved <= 50.0 and call["pending"].get("funding_asset") in (None, "usdc")
    assert call["validity"]["pump_price_limit"]["cost_usdc_atomic"] == int(round(approved * 1e6))


def test_the_production_wiring_gives_the_worker_the_damm_pools(damm_listed, monkeypatch):
    from sniper.execution import raydium_launchlab_buy as LL

    class Thread:
        def __init__(self, *, target, name, daemon):
            pass

        def start(self):
            pass
    monkeypatch.setattr("threading.Thread", Thread)
    workers = []
    real_worker = LL.OnDemandLegWorker
    monkeypatch.setattr(LL, "OnDemandLegWorker", lambda **kw: workers.append(real_worker(**kw)) or workers[-1])
    cache = _quoted(SPCX, program=pcb.TOKEN_2022_PROGRAM)
    damm = SimpleNamespace(pools=cache, sim_rpc=None, user=USER, refresh=lambda *a, **k: True)
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key="fixture")
    lane.start_slot_lane(rc, builders={"pump_curve": object(), "meteora_damm_v2": damm},
                         signal_start_fn=lambda *a, **k: None, sol_usd_fn=lambda: None,
                         log=lambda line: None, roster_path="unused", conn_factory=object,
                         readonly_conn_factory=object)
    (worker,) = workers
    assert "meteora_damm_v2" in worker.builders
    monkeypatch.setattr(lane, "admission_refusal", lambda b, **k: (
        "damm_pools missing" if k.get("damm_pools") is None else None, {}))
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), cache)
    assert worker.admission_fn(buy, (6, 250.0)) is None


def test_a_price_floor_beyond_u64_refuses_the_build_instead_of_raising(damm_listed):
    """Found by the E3 chain simulation (2026-10-02): an absurd source average gave a minimum out
    above u64 and struct.error escaped the build (the SOL path shares the code). It refuses."""
    cache = _quoted(pcb.USDC_MINT)
    routed = dataclasses.replace(_routed(CLASSIC), usdc_spent_atomic=1, token_delta_atomic=10 ** 30)
    buy, _ = D.damm_slot_buy(routed, cache)
    assert _builder(cache).build(buy, 100.0, SNAP) == (None, "price floor outside u64")
