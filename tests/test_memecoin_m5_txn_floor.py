"""M5 trade-count floor: the operator-approved replacement for the mcap-ratio turnover floor.

2026-08-30. The ratio floor sorted the WRONG WAY on Solana — coins it refused returned -4.47%
mean against -27.74% for coins it admitted (n=24 vs 30, permutation p=0.0096). The replacement
asks the same question, "is anyone other than the trader we are copying active", as a trade
count.

THE REPLACEMENT IS NOT A PROVEN IMPROVEMENT. Its own separation is p=0.456 at n=154. It is
shipped because it blocks a quarter as many signals and nothing says the extra 36% it admits are
worse. These tests pin BEHAVIOUR, not merit.
"""
import json

import pytest

import sniper.services.memecoin_real_runner as R


def _decide(**kw):
    base = dict(vol_h1=None, mcap=None, abs_floor=0.0, turnover_floor=0.0)
    base.update(kw)
    return R.volume_alive_decision(base.pop("vol_h1"), base.pop("mcap"), **base)


# ---------------------------------------------------------------- the rule

@pytest.mark.parametrize("txns,allow", [(0, False), (1, False), (2, True), (7, True)])
def test_floor_of_two_implements_the_approved_greater_than_one(txns, allow):
    ok, need, why = _decide(m5_txns=txns, m5_txns_floor=2)
    assert ok is allow
    if not allow:
        assert why == "m5_txns_under_floor"


def test_the_leg_is_OFF_at_floor_zero():
    """Default config is 0. The commit must be inert until the operator sets the knob."""
    assert _decide(m5_txns=0, m5_txns_floor=0)[0] is True


def test_a_count_is_never_returned_in_the_USD_slot():
    """The middle slot is the dollar bar the caller prints. A trade count through it would render
    as '$2' and read as a volume claim, which is the unit-mix class of bug that produced $3.47M
    of fake PnL on 2026-07-30."""
    ok, need, why = _decide(m5_txns=1, m5_txns_floor=2)
    assert ok is False and need is None


# ---------------------------------------------------------------- the reason it exists

def test_the_count_leg_works_with_NO_market_cap():
    """The whole point. The ratio legs live inside `if mc and mc > 0`, and 47.8% of attempts
    reaching the market-data stage have no resolvable mcap — so the gate being replaced was
    structurally inert on nearly half the flow.

    Mutation check: move the count leg inside the `if mc and mc > 0` block and this goes red.
    """
    ok, _need, why = _decide(mcap=None, m5_txns=0, m5_txns_floor=2)
    assert ok is False and why == "m5_txns_under_floor"
    ok2, _n2, _w2 = _decide(mcap=0, m5_txns=0, m5_txns_floor=2)
    assert ok2 is False, "a zero mcap must not silence the count leg either"


def test_the_count_leg_runs_BEFORE_the_ratio_leg():
    """Both would refuse; the reason must name the count, or the refusal-capture registry and the
    exemption list attribute it to the retired ratio gate."""
    ok, _need, why = _decide(mcap=100_000, vol_m5=1.0, m5_floor=0.10,
                             m5_txns=0, m5_txns_floor=2)
    assert ok is False and why == "m5_txns_under_floor"


# ---------------------------------------------------------------- absent data

def test_absent_counts_ADMIT_by_default():
    """~30% of snapshots carry no txn counts. Fail-closed would refuse them on missing DATA,
    which is the opposite of what this change is for, and it would not match the gate it
    replaces (skipped entirely wherever mcap is unresolvable)."""
    assert _decide(m5_txns=None, m5_txns_floor=2)[0] is True


def test_absent_counts_refuse_when_explicitly_required():
    ok, need, why = _decide(m5_txns=None, m5_txns_floor=2, m5_txns_required=True)
    assert ok is False and why == "m5_txns_unreadable" and need is None


def test_one_present_side_is_a_real_zero_not_missing_data():
    assert R._m5_txn_count({"txns_m5_buys": 3}) == 3
    assert R._m5_txn_count({"txns_m5_sells": 0}) == 0
    assert R._m5_txn_count({"txns_m5_buys": 2, "txns_m5_sells": 5}) == 7
    assert R._m5_txn_count({}) is None
    assert R._m5_txn_count(None) is None


# ---------------------------------------------------------------- the mechanism we kept

def test_the_ratio_floor_still_works_when_configured():
    """Kept behind config rather than deleted: it may earn its place back once refusal inputs
    exist and someone can fit it on the roster we actually run."""
    ok, need, why = _decide(mcap=100_000, vol_m5=1.0, m5_floor=0.10, m5_txns=9, m5_txns_floor=2)
    assert ok is False and why == "m5_turnover_under_floor" and need == 10_000.0


# ---------------------------------------------------------------- the evidence path

def test_refusal_inputs_are_captured_for_later_grading():
    class Ev:
        their_size_usd = 412.0
    st = {"market_cap": 80_000, "volume_m5": 120.0, "volume_h1": 4000.0,
          "txns_m5_buys": 1, "txns_m5_sells": 0, "liquidity": 9000.0}
    got = json.loads(R._volume_gate_inputs(st, Ev(), "m5_txns_under_floor", None))
    for key in ("why", "mcap", "volume_m5", "txns_m5_buys", "txns_m5_sells",
                "txns_m5_total", "their_size_usd", "txns_floor", "ratio_floor"):
        assert key in got, f"{key} missing — the next study cannot reconstruct the decision"
    assert got["txns_m5_total"] == 1 and got["their_size_usd"] == 412.0


def test_gate_input_capture_never_breaks_a_refusal():
    """Write-only and best-effort. It records a refusal that already happened; it must not be
    able to cause or change one."""
    class Boom:
        @property
        def their_size_usd(self):
            raise RuntimeError("boom")
    assert R._volume_gate_inputs({"market_cap": 1}, Boom(), "why", None) is None


def test_the_attempt_row_accepts_the_captured_inputs():
    import sqlite3
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(c)
    assert "gate_inputs_json" in {r[1] for r in c.execute(
        "PRAGMA table_info(memecoin_entry_attempts)")}
    assert "gate_inputs_json" in m._ENTRY_ATTEMPT_FINISH_FIELDS


@pytest.mark.parametrize("family,reason", [
    ("m5_txns_under_floor",
     "m5 trades 1 under floor 2 (m5_txns_under_floor, revival guard)"),
    ("m5_txns_unreadable",
     "m5 trades unreadable under floor 2 (m5_txns_unreadable, revival guard)"),
])
def test_count_refusals_are_operator_visible_selection_families(family, reason):
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_refusal_capture import classify_gate

    assert classify_gate(reason) == family
    assert family in m.EXEMPTIBLE_GATE_FAMILIES
