"""Task 8 null arm: frozen non-LLM policy on the same opportunities (Gamma prereg section 6): enters every eligible coin at first_eligible + L_null, same capacity limits and order of arrival."""
import inspect

from sniper.office import null_arm as N

DAY = 86400.0
D0 = 1_800_000_000.0 // DAY * DAY


def opp(seq, t, path="p"):
    return {"candidate_seq": seq, "first_eligible_at": t, "path": path}


def stub(results):
    """fill stub: results {seq: (pnl, exit_t) | 'censored'}; keyed by the path field."""
    def fill(path, entry_t, clip_usd):
        r = results[path]
        return {"censored": r} if isinstance(r, str) else {"pnl_usd": r[0], "exit_t": r[1]}
    return fill


POL = N.Policy(clip_usd=25.0, max_concurrent=2, daily_loss_usd=50.0, l_null_s=10.0)


def test_enters_every_eligible_coin_at_first_eligible_plus_latency():
    out = N.run([opp(1, D0 + 100, "a")], POL, stub({"a": (3.0, D0 + 200)}))
    assert out == [{"candidate_seq": 1, "state": "entered", "entry_t": D0 + 110, "exit_t": D0 + 200, "n": 3.0}]


def test_capacity_limit_in_order_of_arrival_not_in_candidate_order():
    opps = [opp(2, D0 + 20, "b"), opp(1, D0 + 10, "a"), opp(3, D0 + 30, "c"), opp(4, D0 + 1000, "d")]
    fill = stub({"a": (1.0, D0 + 500), "b": (1.0, D0 + 600), "c": (9.0, D0 + 700), "d": (2.0, D0 + 1100)})
    got = {r["candidate_seq"]: r for r in N.run(opps, POL, fill)}
    assert [got[k]["state"] for k in (1, 2, 3, 4)] == ["entered", "entered", "capacity", "entered"]    # c arrives while a and b are open: skipped, n=0
    assert got[3]["n"] == 0.0 and got[4]["n"] == 2.0                                                  # by d's entry both earlier positions have exited


def test_daily_loss_halts_entries_for_the_rest_of_the_utc_day_only():
    fill = stub({"a": (-60.0, D0 + 200), "b": (5.0, D0 + 400), "c": (5.0, D0 + DAY + 400)})
    got = {r["candidate_seq"]: r for r in N.run([opp(1, D0 + 100, "a"), opp(2, D0 + 300, "b"), opp(3, D0 + DAY + 100, "c")], POL, fill)}
    assert [got[k]["state"] for k in (1, 2, 3)] == ["entered", "daily_loss", "entered"]


def test_a_censored_fill_is_null_not_zero_and_holds_its_slot():
    fill = stub({"a": "path_gap", "b": (1.0, D0 + 300), "c": (1.0, D0 + 310)})
    got = {r["candidate_seq"]: r for r in N.run([opp(1, D0 + 0, "a"), opp(2, D0 + 5, "b"), opp(3, D0 + 6, "c")], POL, fill)}
    assert got[1]["state"] == "censored" and got[1]["n"] is None and got[1]["reason"] == "path_gap"
    assert got[3]["state"] == "capacity"                                          # the unknown position is conservatively still open


def test_the_null_arm_cannot_see_office_decisions():
    assert set(inspect.signature(N.run).parameters) == {"opps", "policy", "fill"}      # no decision, verdict or memo parameter exists
    assert not any("office" in k for k in opp(1, 0).keys())


def test_null_uses_shift_latency_and_refuses_infeasible_shift():
    pol = N.Policy(25., 2, 50., {'asia': 10., 'us': 20., 'bad': 0.})
    rows = [dict(opp(1, D0 + 100, 'a'), shift_id='asia'), dict(opp(2, D0 + 200, 'b'), shift_id='us'), dict(opp(3, D0 + 300, 'c'), shift_id='bad')]
    out = N.run(rows, pol, stub({'a': (1., D0 + 110), 'b': (1., D0 + 220)}))
    assert [x['entry_t'] for x in out[:2]] == [D0 + 110, D0 + 220]
    assert out[2]['state'] == 'infeasible' and out[2]['n'] is None


def test_submit_time_pays_mechanical_delay_once_and_never_backdates():
    from sniper.office.ledger import ENTRY_DELAY_S
    assert N.submission_time(100., 10.) + ENTRY_DELAY_S == 110.
    assert N.submission_time(100., ENTRY_DELAY_S) == 100.
    import pytest
    with pytest.raises(ValueError, match='infeasible'):
        N.submission_time(100., ENTRY_DELAY_S - .01)


def test_frozen_policy_does_not_share_mutable_shift_latency():
    shifts = {'us': 10.}
    pol = N.Policy(25., 2, 50., shifts)
    shifts['us'] = 100.
    result = N.run([dict(opp(1, D0 + 100, 'a'), shift_id='us')], pol, stub({'a': (1., D0 + 200)}))
    assert result[0]['entry_t'] == D0 + 110


def test_a_censored_slot_releases_after_the_ruled_bound():
    """Contract section 6: a censored fill must not lock the arm for ever."""
    from sniper.office.null_arm import CENSORED_SLOT_RELEASE_S, Policy, run
    def opp(i, t):
        return {"candidate_seq": i, "first_eligible_at": t, "path": i, "shift_id": None}
    fill = lambda path, t, clip: {"censored": "path_gap"} if path == 0 else {"pnl_usd": 1.0, "exit_t": t + 1}
    got = run([opp(0, 0.0), opp(1, 100.0), opp(2, CENSORED_SLOT_RELEASE_S + 100.0)], Policy(25.0, 1, 100.0, 5.0), fill)
    assert [r["state"] for r in got] == ["censored", "capacity", "entered"]
