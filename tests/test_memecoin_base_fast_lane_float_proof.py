"""Base 8453 step 1 part D: the lane's native float on the 8453 gas pass, and the read-only eth_call
template proof (plan 2026-10-14). Offline."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from sniper.execution.evm_v4_quote import V4
from sniper.settings import memecoin as cfg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import memecoin_runner_loop as loop  # noqa: E402
from scripts.research import base_fast_template_proof as proof  # noqa: E402

E = 10 ** 18


@pytest.fixture
def levels(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_FLOAT_CHAINS", "8453")
    for name, value in (("LOW", "0.025"), ("TARGET", "0.030"), ("MIN_WETH_RESERVE", "0.005"),
                        ("DAILY_CAP", "0.10")):
        monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_%s_NATIVE_8453" % name, value)
    monkeypatch.setattr(cfg, "MEMECOIN_BASE_FAST_LANE_FLOAT_WEI", int(0.06 * E))


def _bounds(mode, monkeypatch):
    monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE", mode)
    seen = {}

    def topup(conn, *, context, now_iso, **bounds):
        seen.update(bounds)
        return {"state": "above_low_water"}
    loop._run_chain_gas_pass(None, "8453", contexts={"8453": {"chain": "8453"}}, now_iso="x",
                             topup_fn=topup, gas_alert_state={})
    return seen


def test_only_the_live_base_lane_raises_the_8453_target_and_the_gas_only_bounds_cap_it(levels, monkeypatch):
    from sniper.services.memecoin_evm_gas_topup import gas_pass_bounds
    shadow, live = _bounds("runner_shadow", monkeypatch), _bounds("live", monkeypatch)
    gas_only = gas_pass_bounds("8453", cfg)
    assert shadow["target_wei"] == int(0.030 * E)                 # today's 8453 native float
    assert live["target_wei"] == int(0.030 * E) + int(0.06 * E)
    assert live["low_water_wei"] == shadow["low_water_wei"]
    for got in (shadow, live):
        base = got["balance_bound_base"]
        assert (base["low_water_wei"], base["target_wei"]) == (gas_only["low_water_wei"], gas_only["target_wei"])


def test_the_balance_bound_takes_the_live_target_down_to_half_the_liquid_above_gas(levels, monkeypatch):
    """Alpha's D3 rule on 2026-10-14's balances: ETH 0.030000, WETH 0.039699."""
    from sniper.services.memecoin_evm_gas_topup import balance_bound_float
    live = _bounds("live", monkeypatch)
    capped = balance_bound_float(live, base_bounds=live["balance_bound_base"],
                                 native_balance_wei=29999683017469671, weth_balance_wei=39698958117956349)
    gas_target = live["balance_bound_base"]["target_wei"]
    assert capped["target_wei"] == gas_target + (29999683017469671 + 39698958117956349 - gas_target) // 2


@pytest.mark.parametrize("kind,x", [("v4_native", "0x" + "00" * 20),
                                    ("v4_weth", "0x4200000000000000000000000000000000000006")])
def test_the_proof_builds_the_lane_s_own_buy_with_a_one_atom_and_an_impossible_minimum(kind, x):
    mint = "0x" + "cd" * 20
    key = {"currency0": x, "currency1": mint, "fee": 0, "tick_spacing": 60, "hooks": "0x" + "00" * 20}
    rec = {"route_kind": kind, "key": key, "mint": mint, "x": x, "eth_usd": 2500.0}
    tx, clip, one = proof.build(rec, "0x" + "ab" * 20, min_out_one=True)
    _tx2, _clip2, vast = proof.build(rec, "0x" + "ab" * 20, min_out_one=False)
    assert tx["to"] == V4["8453"]["universal_router"] and clip == 4 * 10 ** 14
    assert one == 1 and 10 ** 30 < vast < 1 << 128


def test_a_kind_passes_only_when_the_swap_runs_and_its_minimum_bites(tmp_path, monkeypatch, capsys):
    db = tmp_path / "d.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE base_fast_decisions (id INTEGER PRIMARY KEY, reason TEXT, route_kind TEXT, "
                 "record_json TEXT)")
    key = {"currency0": "0x" + "00" * 20, "currency1": "0x" + "cd" * 20, "fee": 0, "tick_spacing": 60,
           "hooks": "0x" + "00" * 20}
    conn.execute("INSERT INTO base_fast_decisions (reason, route_kind, record_json) VALUES "
                 "('would_send', 'v4_native', ?)", (json.dumps({"route_kind": "v4_native", "key": key,
                  "mint": key["currency1"], "x": key["currency0"], "eth_usd": 2500.0, "pool": "0xp"}),))
    conn.commit()
    monkeypatch.setattr(proof.time, "sleep", lambda s: None)
    for answers, code in (([(True, "ok"), (False, "execution reverted")], 0),
                          ([(True, "ok"), (True, "ok")], 1),        # the minimum never bit
                          ([(False, "execution reverted"), (False, "x")], 1)):
        it = iter(answers)
        monkeypatch.setattr(proof, "eth_call", lambda *a, **k: next(it))
        assert proof.main(["--taker", "0x" + "ab" * 20, "--kinds", "v4_native", "--url", "https://h",
                           "--decisions", str(db)]) == code
    assert proof.main(["--taker", "0x" + "ab" * 20, "--kinds", "v4_weth", "--url", "https://h",
                       "--decisions", str(db)]) == 1                 # no step-0 coin of that kind
