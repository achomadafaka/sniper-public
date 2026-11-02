"""OKX proVF4 swap_tob -> pump_amm pre-execution decoder (Alpha GO 2026-10-17; memecoin_rabbit_okx_amm).

Fixtures: tests/fixtures/memecoin/okx_pump_amm_frames.json, real armed-wallet source transactions
(getTransaction json, research key) turned into the pre-execution candidate Lambda's seam delivers,
each pool's account, and the funded lane's cached sol_usd within 120 s of each pure frame's block.
The read state is built from the source's OWN executed BuyEvent pre-reserves and fee tier.
"""
import json
import struct
from pathlib import Path

import pytest

from sniper.execution import okx_swap
from sniper.execution.pump_curve_buy import TOKEN_PROGRAM, TOKEN_2022_PROGRAM, WSOL_MINT
from sniper.execution.pumpswap_buy import expected_base_out, parse_buy_event, total_fee_bps
from sniper.services import memecoin_rabbit_dflow_amm as dfa
from sniper.services import memecoin_rabbit_okx_amm as okx
from sniper.settings import memecoin as cfg
from test_memecoin_rabbit_dflow_amm import _fee_config, _pool_at_trade, _token_account, candidate, executed_event

FIX = json.loads((Path(__file__).parent / "fixtures/memecoin/okx_pump_amm_frames.json").read_text())
PURE = sorted(FIX["pure"])


@pytest.fixture(autouse=True)
def knob_on(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_OKX_AMM", True)


def _sig(tx):
    return tx["transaction"]["signatures"][0]


def handler(tx=None, *, sol_usd=None, age=1.0, alerts=None):
    price = sol_usd if sol_usd is not None else (FIX["sol_usd"].get(_sig(tx)) if tx else 100.0)
    sink = [] if alerts is None else alerts
    return okx.OkxAmmHandler(read_fn=lambda m, p: None, sol_usd_fn=lambda: (price, age),
                             alert_fn=lambda t, x: sink.append(x))


def gross(ev):
    from sniper.execution.pumpswap_buy import buy_event_gross_quote
    return buy_event_gross_quote(dict(ev, ix_name="buy_exact_quote_in"))


def trade_time_virtual_quote(ev):
    """The pool's virtual quote AT the trade (the captured pool account is today's): the middle of the
    values for which the exact-quote event's net charge ceil((vault quote + virtual) x out / (base - out))
    equals its user_quote_amount_in (the net; exact-quote events reverse the legacy meanings)."""
    base, quote, out = int(ev["pool_base_token_reserves"]), int(ev["pool_quote_token_reserves"]), int(ev["base_amount_out"])
    net = int(ev["user_quote_amount_in"])
    guess = base * net // out - net - quote
    hits = [vq for vq in range(guess - 20000, guess + 20000) if -(-(quote + vq) * out // (base - out)) == net]
    assert hits, "no virtual quote reproduces the executed charge"
    return hits[len(hits) // 2]


def state_for(tx, plan, *, read_slot, held=0):
    ev = executed_event(tx)
    program = FIX["mints"][plan["mint"]]["owner"]
    pool = _pool_at_trade(FIX["pools"][plan["pool"]], trade_time_virtual_quote(ev))
    accounts = {plan["pool"]: pool, plan["mint"]: FIX["mints"][plan["mint"]],
                plan["fee_config"]: _fee_config(int(ev["lp_fee_basis_points"]), int(ev["protocol_fee_basis_points"]),
                                                int(ev["coin_creator_fee_basis_points"])),
                plan["quote_vault"]: _token_account(WSOL_MINT, plan["pool"], int(ev["pool_quote_token_reserves"]), TOKEN_PROGRAM),
                plan["base_vaults"][program]: _token_account(plan["mint"], plan["pool"], int(ev["pool_base_token_reserves"]), program),
                plan["holders"][program]: (_token_account(plan["mint"], plan["owner"], held, program) if held else None)}
    other = TOKEN_2022_PROGRAM if program == TOKEN_PROGRAM else TOKEN_PROGRAM
    accounts.setdefault(plan["base_vaults"][other], None)
    accounts.setdefault(plan["holders"][other], None)
    return {"slot": read_slot, "accounts": accounts}


def _okx_ix(cand):
    return next(i for i, ix in enumerate(cand["instructions"]) if ix[0] == okx_swap.OKX_PROGRAM)


def _with_okx(cand, *, data=None, accounts=None):
    i = _okx_ix(cand)
    program, accts, raw = cand["instructions"][i]
    ixs = list(cand["instructions"])
    ixs[i] = (program, accts if accounts is None else accounts, raw if data is None else data)
    return dict(cand, instructions=tuple(ixs))


def _build(h, cand, plan, fill):
    return h.build(cand, plan, None, fill, source_user_id="uid", handle="h", signature_basis="ed25519_local",
                   feed="preconf:test", read_ms=3.0, recv_epoch=1792200000.0, now_mono=2.0)


@pytest.mark.parametrize("name", PURE)
def test_the_in_amount_at_the_trades_own_sol_price_is_the_spend_the_pool_took(name):
    tx = FIX["pure"][name]
    plan, why = handler(tx).match(candidate(tx))
    assert why is None, why
    spend = gross(executed_event(tx))
    assert spend is not None and abs(plan["sol_in"] - spend) * 10_000 <= 10 * spend      # measured -7..+7 bps


@pytest.mark.parametrize("name", PURE)
def test_a_modelled_fire_matches_the_executed_buy_event(name):
    tx = FIX["pure"][name]
    h, cand = handler(tx), candidate(tx)
    plan, why = h.match(cand)
    assert why is None, why
    ev = executed_event(tx)
    assert plan["pool"] == ev["pool"]
    exact = dict(plan, sol_in=gross(ev))                       # the model, on the spend the pool took
    fill, why = h.verdict(exact, state_for(tx, exact, read_slot=tx["slot"] - 1), cand)
    assert why is None, why
    # Pump's exact-quote-in rounding is not recoverable from the event to the unit: on these four buys
    # the model sits 3..1309 atomic above the executed out (at most 2.6e-10 relative).
    assert 0 <= fill["base_out"] - int(ev["base_amount_out"]) <= int(ev["base_amount_out"]) // 10 ** 9
    assert abs(fill["quote_amount_in"] - int(ev["user_quote_amount_in"])) <= 1         # the net, to a lamport
    assert (fill["lp_bps"], fill["protocol_bps"], fill["creator_bps"]) == (
        int(ev["lp_fee_basis_points"]), int(ev["protocol_fee_basis_points"]), int(ev["coin_creator_fee_basis_points"]))
    fill, why = h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 1), cand)   # and on our estimate
    assert why is None and abs(fill["base_out"] - int(ev["base_amount_out"])) * 10_000 <= 20 * int(ev["base_amount_out"])
    buy = _build(h, cand, plan, fill)
    assert (buy.venue, buy.mint, buy.pool, buy.token_amount) == ("pump_amm", plan["mint"], ev["pool"], fill["base_out"])
    assert buy.rabbit["route"] == okx.ROUTE == buy.source_initiation["route"]
    assert buy.source_initiation["program_id"] == dfa.AMM_PROGRAM and buy.source_initiation["payer"] == cand["owner"]
    assert buy.rabbit["okx_pump_weight"] == plan["pump_weight"] and buy.rabbit["sol_usd"] == plan["sol_usd"]
    back = parse_buy_event(buy.trade_event_log)
    assert int(back["base_amount_out"]) == fill["base_out"] and back["pool"] == ev["pool"]


def test_a_split_pump_leg_takes_its_weight_of_the_sol():
    name = next(n for n in PURE if n.startswith("9CrT3D52"))
    tx = FIX["pure"][name]
    plan, _ = handler(tx).match(candidate(tx))
    assert plan["pump_weight"] == 9900
    assert plan["sol_in"] == int(plan["amount_in"] * 1000 * 9900 / (10_000 * plan["sol_usd"]))


def test_an_index_2_hop_one_route_refuses():
    for tx in FIX["index2"].values():
        assert handler(tx, sol_usd=100.0).match(candidate(tx)) == (None, "okx_route_unmodelled")


@pytest.mark.parametrize("price,age", [(100.0, 30.5), (100.0, None), (None, 1.0), (0.0, 1.0), (float("nan"), 1.0),
                                       (100.0, -1.0)])
def test_a_stale_or_missing_sol_price_refuses(price, age):
    tx = FIX["pure"][PURE[0]]
    h = okx.OkxAmmHandler(read_fn=None, sol_usd_fn=lambda: (price, age), alert_fn=lambda t, x: None)
    assert h.match(candidate(tx)) == (None, "sol_usd_stale")


def test_a_sol_price_at_the_lanes_age_limit_still_binds():
    tx = FIX["pure"][PURE[0]]
    assert handler(tx, age=30.0).match(candidate(tx))[1] is None


def test_a_broken_sol_price_reader_refuses():
    def boom():
        raise RuntimeError("snapshot")
    tx = FIX["pure"][PURE[0]]
    h = okx.OkxAmmHandler(read_fn=None, sol_usd_fn=boom, alert_fn=lambda t, x: None)
    assert h.match(candidate(tx)) == (None, "sol_usd_stale")


def test_an_unknown_dex_variant_refuses():
    tx = FIX["pure"][PURE[0]]
    cand = candidate(tx)
    data = bytearray(cand["instructions"][_okx_ix(cand)][2])
    first = 8 + 8 + 8 + 8 + 2 + 4                       # disc, order_id, amount_in, expect, slippage, vec len
    assert data[first] < 143
    data[first] = 143                                   # one past the pinned IDL's last Dex variant
    assert handler(tx).match(_with_okx(cand, data=bytes(data))) == (None, "okx_decode")


def test_trailing_bytes_or_a_moved_idl_refuse(monkeypatch):
    tx = FIX["pure"][PURE[0]]
    cand = candidate(tx)
    data = cand["instructions"][_okx_ix(cand)][2]
    assert handler(tx).match(_with_okx(cand, data=bytes(data) + b"\x00")) == (None, "okx_decode")
    okx_swap._types.cache_clear()
    monkeypatch.setattr(okx_swap, "IDL_SHA256", "0" * 64)
    try:
        assert handler(tx).match(cand) == (None, "okx_decode")
    finally:
        okx_swap._types.cache_clear()


def test_another_okx_instruction_refuses_by_name():
    tx = FIX["pure"][PURE[0]]
    cand = candidate(tx)
    data = cand["instructions"][_okx_ix(cand)][2]
    assert handler(tx).match(_with_okx(cand, data=b"\x01" * 8 + bytes(data[8:]))) == (None, "okx_not_swap_tob")


@pytest.mark.parametrize("name", sorted(FIX["noncanonical"]))
def test_an_xstock_quoted_pool_refuses(name):
    tx = FIX["noncanonical"][name]
    assert handler(tx, sol_usd=100.0).match(candidate(tx)) == (None, "pool_noncanonical")


def test_named_account_refusals():
    from sniper.execution.pump_native_exit import ata
    tx = FIX["pure"][PURE[0]]
    cand = candidate(tx)
    accts = list(cand["instructions"][_okx_ix(cand)][1])
    owner, mint = cand["owner"], accts[4]
    h = handler(tx)
    assert h.match(_with_okx(cand, accounts=tuple([accts[1]] + accts[1:]))) == (None, "okx_payer_not_owner")
    assert h.match(_with_okx(cand, accounts=tuple(accts[:3] + [WSOL_MINT] + accts[4:]))) == (None, "okx_source_not_usdc")
    usdc = str(ata(owner, okx.USDC_MINT, TOKEN_PROGRAM))
    assert h.match(candidate(tx, drop_writable=usdc)) == (None, "okx_source_not_usdc")
    assert h.match(candidate(tx, drop_writable=accts[2])) == (None, "holder_unbound")
    assert h.match(_with_okx(cand, accounts=tuple(accts[:2] + [usdc] + accts[3:]))) == (None, "holder_unbound")
    pool = dfa.canonical_pool(mint)
    assert h.match(candidate(tx, drop_writable=pool)) == (None, "pool_noncanonical")
    assert h.match(_with_okx(cand, accounts=tuple(accts[:5]))) == (None, "pool_noncanonical")
    assert h.match(dict(cand, signers=cand["signers"][:1])) == (None, "owner_not_signer")
    assert h.match(_with_okx(cand, accounts=tuple(accts[:4]))) == (None, "okx_accounts_short")


def test_knob_off_or_no_okx_is_no_handler(monkeypatch):
    tx = FIX["pure"][PURE[0]]
    assert handler(tx).match(dict(candidate(tx), instructions=())) is None
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_OKX_AMM", False)
    assert handler(tx).match(candidate(tx)) is None


def test_two_okx_instructions_refuse():
    tx = FIX["pure"][PURE[0]]
    cand = candidate(tx)
    ix = cand["instructions"][_okx_ix(cand)]
    assert handler(tx).match(dict(cand, instructions=cand["instructions"] + (ix,))) == (None, "okx_multi_swap")


def test_the_shared_read_stale_bound_and_pre_ness_hold():
    tx = FIX["pure"][PURE[0]]
    h, cand = handler(tx), candidate(tx)
    plan, _ = h.match(cand)
    assert h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 3), cand) == (None, "read_stale")
    assert h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"], held=5), cand) == (None, "read_not_before_source")
    extra = (TOKEN_PROGRAM, (plan["holders"][FIX["mints"][plan["mint"]]["owner"]],), b"\x03" + struct.pack("<Q", 1))
    moved = dict(cand, instructions=cand["instructions"] + (extra,))
    assert h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"]), moved) == (None, "read_not_before_source")
    assert h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"]), cand)[1] is None   # the OKX ix itself is fine


def _fired(alerts, *, price_scale=1.0):
    tx = FIX["pure"][PURE[0]]
    h, cand = handler(tx, sol_usd=FIX["sol_usd"][_sig(tx)] * price_scale, alerts=alerts), candidate(tx)
    plan, _ = h.match(cand)
    fill, _ = h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 1), cand)
    _build(h, cand, plan, fill)
    return h, cand, executed_event(tx)


def test_the_landed_join_is_quiet_when_the_estimate_and_the_model_hold():
    alerts = []
    h, cand, ev = _fired(alerts)
    out = h.observe_processed(cand["signature"], dfa.encode_buy_event(dict(ev)))
    assert out["fee_bps_match"] and out["miss_bps"] < 0.01 and alerts == []
    assert out["executed_spend"] == gross(ev) and len(h.stats["spend_drift_bps"]) == 1
    assert abs(h.stats["spend_drift_bps"][0]) <= 10


def test_the_landed_join_pages_on_an_in_amount_miss():
    alerts = []
    h, cand, ev = _fired(alerts, price_scale=0.99)          # our SOL price 1% low: the estimate 1% high
    out = h.observe_processed(cand["signature"], dfa.encode_buy_event(dict(ev)))
    assert out["miss_bps"] < 0.01 and len(alerts) == 1 and "in-amount estimate" in alerts[0]
    assert h.stats["math_mismatch"] == 1 and h.stats["paged"] == 1


def test_the_landed_join_pages_on_an_event_that_is_not_an_exact_quote_buy():
    alerts = []
    h, cand, ev = _fired(alerts)
    out = h.observe_processed(cand["signature"], dfa.encode_buy_event(
        dict(ev, quote_amount_in=int(ev["quote_amount_in"]) + 1)))
    assert out["executed_spend"] is None and len(alerts) == 1 and "not an exact-quote-in buy" in alerts[0]


def test_the_proof_passes_the_pre_execution_check_only_with_the_okx_knob(monkeypatch):
    from types import SimpleNamespace
    from sniper.services import memecoin_source_initiation as si
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_SIGNAL", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_BUY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_DFLOW_AMM", False)
    tx = FIX["pure"][PURE[0]]
    h, cand = handler(tx), candidate(tx)
    plan, _ = h.match(cand)
    fill, _ = h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 1), cand)
    buy = _build(h, cand, plan, fill)
    ev = SimpleNamespace(signal_source="slot_lane", source_timestamp_trust="rabbit_pre_execution",
                         source_wallet=cand["owner"], mint=plan["mint"], source_block_number=cand["slot"],
                         source_user_id="uid", fomo_event_id="slotlane:" + cand["signature"])
    assert si._pre_execution_refusal(ev, buy.source_initiation) is None
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_OKX_AMM", False)
    assert si._pre_execution_refusal(ev, buy.source_initiation) == "wallet_initiation_unproved"
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_DFLOW_AMM", True)                # the other route's knob
    assert si._pre_execution_refusal(ev, buy.source_initiation) == "wallet_initiation_unproved"


def test_sol_usd_reader_reads_the_prefetch_snapshot_age():
    from types import SimpleNamespace

    class Clock:
        @staticmethod
        def monotonic():
            return 100.0
    snap = SimpleNamespace(sol_usd=150.0, observed_mono={"sol_usd": 88.0})
    assert okx.sol_usd_reader(lambda: snap, clock=Clock)() == (150.0, 12.0)
    never = SimpleNamespace(sol_usd=None, observed_mono={})
    assert okx.sol_usd_reader(lambda: never, clock=Clock)() == (None, None)


def test_status_is_json_safe_with_the_drift_quantiles():
    alerts = []
    h, cand, ev = _fired(alerts)
    h.observe_processed(cand["signature"], dfa.encode_buy_event(dict(ev)))
    st = json.loads(json.dumps(h.status()))
    assert st["spend_drift_bps"]["n"] == 1 and st["drift_base_bps"]["n"] == 1 and st["fills"] == 1
    assert st["spend_drift_bps"]["p50"] == st["spend_drift_bps"]["p90"] == h.stats["spend_drift_bps"][0]
