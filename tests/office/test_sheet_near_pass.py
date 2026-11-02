"""Office near-pass admission (Gamma prereg docs/audits/2026-10-29-office-near-pass-stratum-preregistration.md):
section 1 exactly, each refused item refused, the allowed list exact, labels in the cache key and the bundle."""
import pytest

from office.test_sheet_tool import Reads, candidate, hard, call, freeze, CALL_NOW, env  # noqa: F401
from sniper.office.tools import sheet as T

PASSING = {k: {"status": "pass", "reason": "ok"} for k in set(T.NEAR_PASS_REQUIRED) | set(T.NEAR_PASS_ALLOWED)}


def near(checks=None, reason="unknown:dump_group:dump_group_unknown", verdict="SKIP"):
    base = dict(PASSING)
    base.update(checks if checks is not None else {"dump_group": {"status": "unknown", "reason": "dump_group_unknown"}})
    row = hard(verdict=verdict, reason=reason)
    row["evidence"] = dict(row["evidence"], checks=base)
    return row


def nsheet(conn, row):
    return T.sheet(candidate(), desk_dir="/d", policy={"rail_bounds": {}, "hard_filter": {}, "v": 1},
                   clock_sample={"offset_s": 0.25, "sampled_at": CALL_NOW - 10}, hard_filter_row=row, now=CALL_NOW,
                   store_conn=conn, buyer_ledger=[], strategy_epoch="e1", feed="near_pass")


ALLOWED = [("dump_group", "dump_group_unknown"), ("dump_group", "partial_history"),
           ("cluster", "cluster_unknown_dependency"), ("cluster", "cluster_unknown_dependency:owner_program"),
           ("holders", "creator_unknown"), ("holders", "supply_unknown"),
           ("chart", "chart_60s_prints_lt_2"), ("chart", "chart_60s_prints_lt_2_decimals_post_cut_immutable"),
           ("chart", "age_unknown"), ("chart", "age_unknown_decimals_post_cut_immutable")]


@pytest.mark.parametrize("check,reason", ALLOWED)
def test_each_allowed_unknown_is_admitted_as_a_typed_blocker(check, reason):
    got = T.near_pass_blockers(near({check: {"status": "unknown", "reason": reason}}, reason="unknown:%s:%s" % (check, reason)))
    assert got == [{"check": check, "status": "unknown", "reason": reason}]


REFUSED = [("chart", "facts_unavailable"), ("chart", "facts_unavailable:key_error:opportunity_id"),
           ("chart", "chart_unreadable"), ("cluster", "cluster_unreadable"), ("chart", "provenance_unreadable"),
           ("chart", "pool_binding_unknown"), ("chart", "pool_tip_unproven"), ("chart", "pool_snapshot_mismatch"),
           ("chart", "pool_block_cut_unknown"), ("chart", "pool_tape_gap"), ("chart", "sol_usd_future"),
           ("chart", "sol_usd_stale"), ("cluster", "cluster_not_ready"), ("dump_group", "kill_not_proven"),
           ("chart", "flow_60s_tape_loss")]


@pytest.mark.parametrize("check,reason", REFUSED)
def test_each_refused_reason_refuses_the_ticket(check, reason):
    assert T.near_pass_blockers(near({check: {"status": "unknown", "reason": reason}})) is None


@pytest.mark.parametrize("bad", ["market_data", "token_safety", "sell_probe"])
@pytest.mark.parametrize("status,reason", [("unknown", "pool_tip_unproven"), ("unknown", "sol_usd_future"),
                                           ("unknown", "pool_binding_unknown"), ("unknown", "mint_clock_unknown")])
def test_market_data_token_safety_and_sell_probe_must_pass(bad, status, reason):
    row = near()
    row["evidence"]["checks"][bad] = {"status": status, "reason": reason}
    assert T.near_pass_blockers(row) is None


@pytest.mark.parametrize("row", [near(reason="kill:mcap_safety"), near(reason="kill:liq_min"),
                                 near(reason="dump_group_buyers"), near(verdict="PASS"), near(verdict="KILL"),
                                 near({"cluster": {"status": "kill", "reason": "cluster_share_high"}}),
                                 near({"dump_group": {"status": "kill", "reason": "dump_group_buyers"}}),
                                 near({"holders": {"status": "not_observed", "reason": None}}),
                                 near({"liquidity_extra": {"status": "unknown", "reason": "x"}})])
def test_kills_pass_verdicts_absent_and_unlisted_checks_are_refused(row):
    assert T.near_pass_blockers(row) is None


def test_feed_blockers_and_blocker_set_id_reach_the_cache_key_and_the_bundle(env):
    conn, reads = env
    r = nsheet(conn, near())
    assert r["state"] == "known" and r["request"]["feed"] == "near_pass" and r["sheet"]["feed"] == "near_pass"
    blockers = [{"check": "dump_group", "status": "unknown", "reason": "dump_group_unknown"}]
    assert r["sheet"]["blockers"] == r["request"]["blockers"] == blockers
    assert r["sheet"]["blocker_set_id"] == T.blocker_set_id(blockers)
    other = nsheet(conn, near({"dump_group": {"status": "unknown", "reason": "partial_history"}}))
    assert other["cache_key"] != r["cache_key"] and other["sheet_hash"] != r["sheet_hash"]   # a blocker change re-sheets
    assert freeze(conn, "d1", r)                                                     # the frozen bundle covers them


def test_the_pass_feed_is_unchanged_and_never_takes_a_near_pass_ticket(env):
    conn, reads = env
    assert call(conn, row=near())["reason"] == "not_hard_filter_pass"              # default feed: PASS only
    passed = call(conn)
    assert "feed" not in passed["request"] and "blockers" not in passed["sheet"]     # PASS sheets byte-identical
    assert nsheet(conn, hard())["reason"] == "not_near_pass_eligible"               # a PASS ticket is not near-pass
    assert T.sheet(candidate(), desk_dir="/d", policy={}, clock_sample={"offset_s": 0, "sampled_at": CALL_NOW},
                   hard_filter_row=hard(), now=CALL_NOW, store_conn=conn, buyer_ledger=[], strategy_epoch="e1",
                   feed="other")["reason"] == "feed_unknown"
