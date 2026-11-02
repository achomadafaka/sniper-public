"""Per-route compute-unit limits for the slot lane's own builds (operator "fix everything" via Alpha,
2026-10-02; Gamma's own-receipt table, ~/.cache/gamma/cuvenue).

A transaction that runs out of compute units fails, and the trade is lost. So a class gets a limit
below the lane default only when its successful sends were measured (n >= 10). That limit is the
recorded max x 1.15, rounded up to the next 1k. It applies only to a build whose leg dex and coin
token program the sample contained; every other build keeps MEMECOIN_SLOT_LANE_CU_LIMIT. The lookup
is pure: no I/O at build time.
"""
import math
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from sniper.settings import memecoin as cfg

T22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SPL = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _sampled(cls):
    return sorted(cfg.SLOT_LANE_CU_CLASSES[cls][2])[0]


@pytest.fixture(autouse=True)
def _no_demotions():
    from sniper.execution import pump_curve_buy as curve
    curve.CU_DEMOTED.clear()
    yield
    curve.CU_DEMOTED.clear()


def test_every_measured_class_limit_covers_its_recorded_max_with_the_margin():
    assert cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS            # the table is not empty
    for cls, limit in cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS.items():
        n, recorded_max, _programs = cfg.SLOT_LANE_CU_CLASSES[cls]
        assert n >= cfg.SLOT_LANE_CU_MIN_SENDS
        assert limit >= recorded_max, cls                     # under it, the trade fails


def test_n_ten_applies_per_dex_so_a_rarely_seen_dex_keeps_the_default():
    # Gamma on a81d7d381: SolFi V2's one curve send (211,983) is 18% over BisonFi's max (n 20).
    for cls, (n, _mx, _p) in cfg.SLOT_LANE_CU_CLASSES.items():
        assert (cls in cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS) == (n >= cfg.SLOT_LANE_CU_MIN_SENDS)
    for dex in ("ZeroFi", "SolFi V2"):
        assert cfg.slot_lane_cu_limit("pump_curve", "leg", leg_dex=dex, token_program=T22) == \
            cfg.MEMECOIN_SLOT_LANE_CU_LIMIT


def test_the_class_limit_is_the_max_with_15_percent_rounded_up_to_1k():
    assert cfg.slot_lane_cu_class_limit(179_887) == 207_000
    assert cfg.slot_lane_cu_class_limit(186_857) == 215_000
    assert cfg.slot_lane_cu_class_limit(158_250) == 182_000
    assert cfg.slot_lane_cu_class_limit(200_000) == 230_000


_PROBE = ("from sniper.settings import memecoin as c;"
          "print(sorted(k + (v,) for k, v in c.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS.items()))")


def _probe(**env):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("MEMECOIN_SLOT_LANE_CU")}
    out = subprocess.run([sys.executable, "-c", _PROBE], env={**clean, **env}, text=True,
                         capture_output=True, check=True, timeout=60)
    return out.stdout.strip()


def test_the_defaults_and_a_per_class_env_override():
    assert _probe() == ("[('pump_amm', 'leg', 'BisonFi', 215000), ('pump_amm', 'leg', 'ZeroFi', 182000), "
                        "('pump_amm', 'native', '26_accounts', 152000), ('pump_curve', 'leg', 'BisonFi', 207000)]")
    assert cfg.slot_lane_cu_override_name("pump_curve", "leg", "SolFi V2") == \
        "MEMECOIN_SLOT_LANE_CU_LIMIT_PUMP_CURVE_LEG_SOLFI_V2"
    assert _probe(MEMECOIN_SLOT_LANE_CU_LIMIT_PUMP_CURVE_LEG_BISONFI="250000") == (
        "[('pump_amm', 'leg', 'BisonFi', 215000), ('pump_amm', 'leg', 'ZeroFi', 182000), "
        "('pump_amm', 'native', '26_accounts', 152000), ('pump_curve', 'leg', 'BisonFi', 250000)]")


def test_an_override_below_the_recorded_max_fails_the_armed_config_check(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS",
                        {**cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS, ("pump_amm", "leg", "BisonFi"): 186_000})
    with pytest.raises(Exception, match="MEMECOIN_SLOT_LANE_CU_LIMIT_PUMP_AMM_LEG_BISONFI"):
        cfg.validate_real_exit_policy()


@pytest.mark.parametrize("venue, funding", [("pump_curve", "native"), ("pump_amm", "native"),
                                            ("pump_amm", "exotic"), ("raydium_cpmm", "leg"),
                                            ("raydium_launchlab", "leg"), ("meteora_damm_v2", "leg"),
                                            ("a_future_venue", "leg")])
def test_unmeasured_classes_keep_the_lane_default(venue, funding):
    assert cfg.slot_lane_cu_limit(venue, funding, leg_dex="BisonFi", token_program=SPL) == \
        cfg.MEMECOIN_SLOT_LANE_CU_LIMIT


@pytest.mark.parametrize("cls", sorted(cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS))
def test_a_branch_the_sample_never_contained_keeps_the_lane_default(cls):
    venue, funding, dex = cls
    program = _sampled(cls)
    limit = cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS[cls]
    assert cfg.slot_lane_cu_limit(venue, funding, leg_dex=dex, token_program=program) == limit
    for other in ({"leg_dex": "SomeNewDex", "token_program": program},
                  {"leg_dex": "Raydium", "token_program": program},
                  {"leg_dex": None, "token_program": program},
                  {"leg_dex": dex, "token_program": "SomeOtherTokenProgram"},
                  {"leg_dex": dex, "token_program": None}):
        assert cfg.slot_lane_cu_limit(venue, funding, **other) == cfg.MEMECOIN_SLOT_LANE_CU_LIMIT, other


def _cu_limit_of(message):
    return int.from_bytes(bytes(message.instructions[0].data)[1:5], "little")


def _with_route(builder, label):
    builder._leg = {**builder._leg, "quote": {"routePlan": [{"swapInfo": {"label": label,
                                                                          "ammKey": "AmmKey1"}}]}}
    return builder


def test_the_curve_builder_signs_its_class_limit_only_on_a_sampled_leg(monkeypatch):
    import test_pump_curve_buy as t
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 10.0)
    cls = ("pump_curve", "leg", "BisonFi")
    dex, program = cls[2], _sampled(cls)
    buy = t._buy(token_program=program)
    message, validity = _with_route(t._builder(t._Clock()), dex).build(buy, 5.0, t.SNAP)
    assert _cu_limit_of(message) == validity["cu_limit"] == cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS[cls]
    message, validity = _with_route(t._builder(t._Clock()), "SomeNewDex").build(buy, 5.0, t.SNAP)
    assert _cu_limit_of(message) == validity["cu_limit"] == cfg.MEMECOIN_SLOT_LANE_CU_LIMIT
    explicit = _with_route(t._builder(t._Clock(), compute_unit_limit=333_000), dex)
    message, validity = explicit.build(buy, 5.0, t.SNAP)
    assert _cu_limit_of(message) == validity["cu_limit"] == 333_000


def test_the_pumpswap_builder_signs_its_class_limit_only_on_a_sampled_leg(monkeypatch):
    import test_pumpswap_buy as t
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 10.0)
    cls = ("pump_amm", "leg", "BisonFi")
    dex, program = cls[2], _sampled(cls)
    builder = _with_route(t._builder(t._Clock()), dex)
    pool = t._buy().pool
    builder.pools._pools[pool]["base_token_program"] = program
    message, validity = builder.build(t._buy(), 5.0, t.SNAP)
    assert _cu_limit_of(message) == validity["cu_limit"] == cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS[cls]
    builder.pools._pools[pool]["base_token_program"] = "11111111111111111111111111111111"
    message, validity = builder.build(t._buy(), 5.0, t.SNAP)
    assert _cu_limit_of(message) == validity["cu_limit"] == cfg.MEMECOIN_SLOT_LANE_CU_LIMIT


# --- demotion after a send runs out of compute units (Gamma MED on a81d7d381) ----------------------
# A pump program upgrade that adds ~15% CU would fail every limited send of a class at once, each a
# lost trade. The first class-limited send that runs out of compute units demotes its class to the
# lane default until restart, with one line.

def test_cu_exhaustion_is_read_from_the_chain_error_or_the_logs():
    from sniper.execution.pump_curve_buy import cu_exhausted
    assert cu_exhausted({"chain_err": {"InstructionError": [3, "ComputationalBudgetExceeded"]}})
    assert cu_exhausted({"chain_err": None, "program_error": "exceeded CUs meter at BPF instruction"})
    assert not cu_exhausted({"chain_err": {"InstructionError": [3, {"Custom": 6001}]},
                             "program_error": "slippage tolerance exceeded (Jupiter 0x1771)"})
    assert not cu_exhausted({}) and not cu_exhausted(None)


def test_a_demoted_class_builds_at_the_lane_default_until_restart(monkeypatch):
    import test_pump_curve_buy as t
    from sniper.execution import pump_curve_buy as curve
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 10.0)
    cls = ("pump_curve", "leg", "BisonFi")
    buy = t._buy(token_program=_sampled(cls))
    _message, validity = _with_route(t._builder(t._Clock()), "BisonFi").build(buy, 5.0, t.SNAP)
    assert validity["cu_class"] == cls
    assert curve.demote_cu_class(cls) is True and curve.demote_cu_class(cls) is False
    message, validity = _with_route(t._builder(t._Clock()), "BisonFi").build(buy, 5.0, t.SNAP)
    assert _cu_limit_of(message) == validity["cu_limit"] == cfg.MEMECOIN_SLOT_LANE_CU_LIMIT
    assert validity["cu_class"] is None


@pytest.mark.parametrize("chain_err, cu_class, demoted", [
    ({"InstructionError": [3, "ComputationalBudgetExceeded"]}, ("pump_curve", "leg", "BisonFi"), True),
    ({"InstructionError": [3, {"Custom": 6001}]}, ("pump_curve", "leg", "BisonFi"), False),  # slippage
    ({"InstructionError": [3, "ComputationalBudgetExceeded"]}, None, False),   # already the default
])
def test_a_class_limited_send_that_ran_out_of_compute_units_demotes_its_class_once(
        conn, tmp_path, monkeypatch, caplog, chain_err, cu_class, demoted):  # noqa: F811
    import time
    from sniper.execution import exec_lock, pump_curve_buy as curve
    from sniper.services import memecoin_slot_lane as lane
    from test_memecoin_slot_lane import _buy, _snap
    monkeypatch.setattr(exec_lock, "_lock_path",
                        lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    failed = {"status": "failed", "reason": "chain_error:x", "sig": "SigCU", "chain_err": chain_err}

    def build_fn(buy, clip, snap):
        return "MESSAGE", {"blockhash": "Hash1", "cu_limit": 207_000, "cu_class": cu_class}

    ln = lane.SlotLane(conn=conn, executor={"slot_execute_fn": lambda *a, **k: dict(failed)},
                       build_fn=build_fn, snapshot_fn=lambda: _snap(time.monotonic()),
                       armed_ids_fn=lambda: {"w1"}, kill_tripped_fn=lambda: False,
                       now_iso_fn=lambda: "2026-09-26T20:30:00+00:00", lock_key="sol-test",
                       mode="live")
    def lines():
        return [r.getMessage() for r in caplog.records if "DEMOTED" in r.getMessage()]
    with caplog.at_level("WARNING", logger=lane.__name__):
        assert ln.handle(_buy())["status"] == "failed"
        assert (cu_class in curve.CU_DEMOTED) is demoted          # the lane's own send demoted it
        assert len(lines()) == (1 if demoted else 0)
        if cu_class:
            ln._demote_cu_class_if_exhausted(cu_class, failed)       # a second failure: no new line
    assert len(lines()) == (1 if demoted else 0)
    if demoted:
        assert "pump_curve/leg/BisonFi" in lines()[0] and "SigCU" in lines()[0]


from test_memecoin_slot_lane import conn  # noqa: E402,F401  (fixture)
