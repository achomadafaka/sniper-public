"""Exotic-leg compute-unit raise (operator via Alpha 2026-10-07, option (b): live, default on).

An exotic-quote build signs the lane default (MEMECOIN_SLOT_LANE_CU_LIMIT) because no measured CU class
covers it. 3wGiVeiz (probablyinsider, 6yE29Hvd quoted in QUBTAD8C, 15:37Z) ran out of compute units
at the then-default 260,000: quote ATA 22.2k + one Raydium CLMM Jupiter hop 58.5k + coin ATA 18.6k +
pump BuyExactQuoteInV2 still running at 160.3k. A leg with more hops can pass any flat default.

So every exotic build estimates its composition from the leg's own routePlan (in memory, no I/O):
ATA creates + each hop's budget (by Jupiter label where our receipts measured it, 150k otherwise) +
the venue's budget, and signs max(default, estimate x 1.15) capped at 1.4M. It only ever raises.
leg_hops and cu_estimate ride on the build's trace."""
import base64
import struct

import pytest
from solders.message import MessageV0

from sniper.settings import memecoin as cfg

CB = "ComputeBudget111111111111111111111111111111"


def _plan(*labels):
    return {"quote": {"routePlan": [{"swapInfo": {"label": label, "ammKey": "Pool%d" % i}}
                                    for i, label in enumerate(labels)]}}


def _cu_limit(message):
    keys = [str(k) for k in message.account_keys]
    for ix in message.instructions:
        data = bytes(ix.data)
        if keys[ix.program_id_index] == CB and data[:1] == b"\x02":
            return struct.unpack("<I", data[1:5])[0]
    raise AssertionError("no SetComputeUnitLimit")


# --- the estimate ---------------------------------------------------------------------------------

def test_the_qubtad8c_composition_that_ran_out_is_raised_past_what_it_died_at():
    limit, hops, estimate = cfg.slot_lane_exotic_cu(_plan("Raydium CLMM"), "pump_curve", 400_000)
    # ATAs 60k + Raydium CLMM 90k (route max 66.4k measured) + pump quote curve 250k
    assert (hops, estimate) == (1, 400_000)
    assert estimate > 260_000 and limit == 460_000
    limit_then, _, _ = cfg.slot_lane_exotic_cu(_plan("Raydium CLMM"), "pump_curve", 260_000)
    assert limit_then == 460_000                          # what the 260k default would have signed


def test_a_three_hop_dlmm_leg_is_raised():
    limit, hops, estimate = cfg.slot_lane_exotic_cu(
        _plan("Meteora DLMM", "Meteora DLMM", "Meteora DLMM"), "raydium_cpmm", 400_000)
    assert hops == 3 and estimate == 60_000 + 3 * 150_000 + 50_000 and limit == 644_000


@pytest.mark.parametrize("leg", [None, {}, {"quote": {}}, {"quote": {"routePlan": []}},
                                 {"quote": {"routePlan": "x"}}, {"quote": None}],
                         ids=["none", "empty", "no_plan", "empty_plan", "garbage_plan", "null_quote"])
def test_no_route_plan_keeps_the_default(leg):
    assert cfg.slot_lane_exotic_cu(leg, "pump_curve", 400_000) == (400_000, None, None)


def test_it_only_ever_raises():
    limit, hops, estimate = cfg.slot_lane_exotic_cu(_plan("BisonFi"), "pump_amm", 400_000)
    assert hops == 1 and estimate == 60_000 + 90_000 + 140_000 and limit == 400_000
    assert cfg.slot_lane_exotic_cu(_plan("Meteora DLMM", "Meteora DLMM"), "pump_curve", 900_000)[0] == 900_000


def test_unmeasured_labels_and_venues_take_the_conservative_budget():
    _l, _h, estimate = cfg.slot_lane_exotic_cu(_plan("SomeNewDex"), "meteora_dbc", 400_000)
    assert estimate == 60_000 + 150_000 + 150_000


def test_the_raise_is_capped_at_the_chain_maximum():
    limit, hops, _e = cfg.slot_lane_exotic_cu(_plan(*["Meteora DLMM"] * 12), "pump_curve", 400_000)
    assert hops == 12 and limit == 1_400_000


# --- every exotic builder signs it and reports it ------------------------------------------------------

THREE = ("Meteora DLMM", "Meteora DLMM", "Meteora DLMM")


def _expected(venue, base=None):
    return cfg.slot_lane_exotic_cu(_plan(*THREE), venue, base or cfg.MEMECOIN_SLOT_LANE_CU_LIMIT)[0]


def test_the_quote_curve_build(monkeypatch):
    from sniper.execution import pump_curve_buy as P
    from sniper.services import memecoin_slot_lane as L
    from test_slot_lane_quote_curve import EXACT_IN, SNAP, _builder, _leg, _slot_buy
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"pump_curve"}))
    monkeypatch.setattr(L.cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", "live")
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    b = _builder()
    buy = _slot_buy(EXACT_IN)
    assert b.ondemand_quote(buy)[2] is None
    usd = 100.0 / (118_090 / 1e8)
    message, validity = b.build(buy, 100.0, SNAP, leg={**_leg(), **_plan(*THREE)}, quote=(8, usd, 0, 0))
    assert message is not None, validity
    assert _cu_limit(message) == validity["cu_limit"] == _expected("pump_curve") > 400_000
    assert validity["leg_hops"] == 3 and validity["cu_estimate"] == 60_000 + 450_000 + 250_000
    assert P is not None


def test_the_pumpswap_exotic_build(monkeypatch):
    from sniper.services import memecoin_slot_lane as L
    from test_slot_lane_pump_nonsol import SNAP, SPCX, _builder, _leg, _slot_buy
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"pump_amm"}))
    monkeypatch.setattr(L.cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", "live")
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    b = _builder(SPCX)
    message, validity = b.build(_slot_buy(SPCX), 10.0, SNAP,
                                leg={**_leg(SPCX["quote_mint"], 40_000), **_plan(*THREE)},
                                quote=(6, 250.0, 0, 0))
    assert message is not None, validity
    assert _cu_limit(message) == validity["cu_limit"] == _expected("pump_amm")
    assert validity["leg_hops"] == 3


def _three_hops(jup):
    def wrapped(method, path, body=None):
        status, payload = jup(method, path, body)
        if path.startswith("/quote"):
            payload = dict(payload, **_plan(*THREE)["quote"])
        return status, payload
    return wrapped


def _built(result):
    assert result.get("unsigned_b64"), result
    from solders.transaction import VersionedTransaction
    return VersionedTransaction.from_bytes(base64.b64decode(result["unsigned_b64"])).message


def test_the_cpmm_exotic_build():
    from sniper.services import memecoin_slot_lane as L
    from test_slot_lane_cpmm import PEPE_EV, _cpmm_worker, _exotic_builder, _routed
    b = _exotic_builder(PEPE_EV)
    w, _ = _cpmm_worker(b, [], PEPE_EV.input_amount // 10)
    w.jup_call = _three_hops(w.jup_call)
    message = _built(w.process(L.cpmm_slot_buy(_routed(PEPE_EV))))
    assert _cu_limit(message) == _expected("raydium_cpmm", b.compute_unit_limit)


def test_the_launchlab_exotic_build():
    from sniper.services import memecoin_slot_lane as L
    from test_slot_lane_launchlab import NVDA_B, _routed, _worker
    w, _ = _worker(NVDA_B, [], out=44_000_000)
    w.jup_call = _three_hops(w.jup_call)
    message = _built(w.process(L.launchlab_slot_buy(_routed(NVDA_B))))
    builder = w.builders["raydium_launchlab"]
    assert _cu_limit(message) == _expected("raydium_launchlab", builder.compute_unit_limit)


def test_the_damm_v2_exotic_build(monkeypatch):
    from sniper.execution import meteora_damm_v2_buy as D
    from sniper.execution import pump_curve_buy as pcb
    from test_slot_lane_damm_nonsol import SNAP, SPCX, _builder, _leg, _quoted
    from test_slot_lane_damm_v2 import CLASSIC, _routed
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"meteora_damm_v2"}))
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "live")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    cache = _quoted(SPCX, program=pcb.TOKEN_2022_PROGRAM)
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), cache)
    b = _builder(cache)
    message, validity = b.build(buy, 100.0, SNAP, leg={**_leg(SPCX), **_plan(*THREE)}, quote=(6, 250.0, 0, 0))
    assert message is not None, validity
    assert _cu_limit(message) == validity["cu_limit"] == _expected("meteora_damm_v2", b.compute_unit_limit)
    assert validity["leg_hops"] == 3


def test_the_dbc_exotic_build(monkeypatch):
    from test_slot_lane_dbc import EXOTIC, SNAP, XS3O, _buy, _builder, _leg
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DBC", "live")
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"meteora_dbc"}))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    b = _builder()
    message, validity = b.build(_buy(EXOTIC), 100.0, SNAP, leg={**_leg(XS3O), **_plan(*THREE)},
                                quote=(8, 400.0, 0, 0))
    assert message is not None, validity
    assert _cu_limit(message) == validity["cu_limit"] == _expected("meteora_dbc", b.compute_unit_limit)
    assert validity["leg_hops"] == 3


def test_a_sol_leg_build_is_unchanged(monkeypatch):
    """Only exotic legs: a SOL-quoted DBC build keeps the builder's own limit and reports no estimate."""
    from sniper.execution import pump_curve_buy as pcb
    from test_slot_lane_dbc import SNAP, SOL, _buy, _builder, _leg
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DBC", "live")
    b = _builder({**_leg(pcb.WSOL_MINT, out=600_000_000), **_plan(*THREE)})
    message, validity = b.build(_buy(SOL), 100.0, SNAP)
    assert message is not None, validity
    assert _cu_limit(message) == b.compute_unit_limit
    assert validity.get("leg_hops") is None and validity.get("cu_estimate") is None


def test_the_trace_records_hops_and_estimate_and_the_executor_never_sees_them():
    from sniper.services.memecoin_slot_lane import record_slot_build
    marks = []
    trace = type("T", (), {"mark_substage": lambda self, name, **kw: marks.append((name, kw))})()
    detail = record_slot_build(trace, {"blockhash": "x", "cu_limit": 644_000, "leg_hops": 3,
                                       "cu_estimate": 560_000}, duration_ms=1.0)
    assert detail == {"blockhash": "x"}
    ((name, kw),) = marks
    assert name == "slot_build" and kw["cu_limit"] == 644_000
    assert kw["leg_hops"] == 3 and kw["cu_estimate"] == 560_000


# --- 3GyqLyoz (CHLOE, 2026-10-29): a Hadron hop ran 300.7k against the 150k blind budget ------------

def test_the_chloe_hadron_composition_that_ran_out_is_raised_past_what_it_needed():
    # measured on chain: quote ATA 21.7k + Jupiter Route via Hadron 307.7k + coin ATA 21.0k, and
    # pump_amm BuyExactQuoteIn ran out at 52.3k of a measured 105k max; 403,000 signed, all spent.
    # Jupiter program-id-to-label maps HADRoNbLovyqhCsocfYQYB7QdfCAAinN9HTePvBCVDQ8 to "Hadron".
    limit, hops, estimate = cfg.slot_lane_exotic_cu(_plan("Hadron"), "pump_amm", 400_000)
    assert hops == 1
    assert limit >= 536_000 > 403_000



# --- unmeasured hop labels: an in-memory counter, exported off-path by the heartbeat ---------------

def test_an_unmeasured_label_is_counted_and_a_measured_one_is_not(monkeypatch):
    monkeypatch.setattr(cfg, "_EXOTIC_CU_BLIND", {})
    cfg.slot_lane_exotic_cu(_plan("SomeNewAmm"), "pump_amm", 400_000)
    cfg.slot_lane_exotic_cu(_plan("SomeNewAmm", "Hadron"), "pump_amm", 400_000)
    assert cfg.slot_lane_exotic_cu_blind_counts() == {"SomeNewAmm": 2}


def test_the_blind_counter_is_bounded(monkeypatch):
    monkeypatch.setattr(cfg, "_EXOTIC_CU_BLIND", {})
    for i in range(cfg._EXOTIC_CU_BLIND_MAX + 5):
        cfg.slot_lane_exotic_cu(_plan("Amm%d" % i), "pump_amm", 400_000)
    counts = cfg.slot_lane_exotic_cu_blind_counts()
    assert len(counts) == cfg._EXOTIC_CU_BLIND_MAX + 1 and counts["(other)"] == 5


def test_a_broken_counter_never_fails_the_build(monkeypatch):
    class Broken(dict):
        def get(self, *a):
            raise RuntimeError("boom")
    monkeypatch.setattr(cfg, "_EXOTIC_CU_BLIND", Broken())
    assert cfg.slot_lane_exotic_cu(_plan("SomeNewAmm"), "pump_amm", 400_000)[0] == 403_000


def test_the_heartbeat_exports_the_blind_counts(tmp_path, monkeypatch):
    import json
    from sniper.services.memecoin_runner import write_heartbeat
    monkeypatch.setattr(cfg, "_EXOTIC_CU_BLIND", {"SomeNewAmm": 3})
    path = tmp_path / "hb"
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status={"mode": "live"})
    assert json.loads(path.read_text())["slot_lane"]["exotic_cu_blind"] == {"SomeNewAmm": 3}
