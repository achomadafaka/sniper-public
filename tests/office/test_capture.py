"""Task 8 capture: bounded forward price path per candidate with a typed path state. Never a scan, never a zero for unknown."""
from . import eqp_guard as G

from sniper.office import capture as C

T = 1_800_000_000.0
H = 3600.0


def swap(c, pool, t, slot, sig, base=10 ** 12, quote=100 * 10 ** 9, buy=1):
    c.execute("INSERT INTO swaps_ps VALUES(?,?,?,?,?,0,?,?,?, 'u','mapped',?,?,'20','5','95')", (pool, "m" + pool, slot, sig, 0, buy, int(t), t, str(base), str(quote)))


def covered(c, lo=T - 1000, hi=T + 100 * H):
    c.execute("INSERT INTO ps_capture_intervals VALUES('s',?,?,1)", (lo, hi))


def test_observed_path_has_rows_inside_the_window_only():
    c = G.tape()
    covered(c)
    for i in range(10):
        swap(c, "P", T + i * 60, 1000 + i, "a%d" % i)
    swap(c, "P", T - 500, 900, "before")
    swap(c, "P", T + 5 * H, 5000, "after")
    swap(c, "Q", T + 100, 1004, "other")                                    # another pool inside the slot range
    r = C.read_forward_path(c, "P", T, H, now=T + 2 * H)
    assert r["state"] == "observed" and len(r["rows"]) == 10 and all(T <= x["received_at"] < T + H for x in r["rows"])


def test_pending_until_the_horizon_matures():
    c = G.tape()
    covered(c)
    swap(c, "P", T + 10, 1, "a")
    assert C.read_forward_path(c, "P", T, H, now=T + 10)["state"] == "pending"


def test_no_pool_and_source_off_and_path_gap_are_typed_not_empty():
    c = G.tape()
    covered(c, T - 1000, T + 600)                                           # capture frontier stops 10 min in
    swap(c, "P", T + 10, 1, "a")
    assert C.read_forward_path(c, None, T, H, now=T + 2 * H)["state"] == "no_pool"
    r = C.read_forward_path(c, "P", T, H, now=T + 2 * H)
    assert r["state"] == "path_gap" and r["reason"] == "capture_frontier_short"
    off = G.tape()
    assert C.read_forward_path(off, "P", T, H, now=T + 2 * H)["state"] == "source_off"        # no rows tape-wide in the window
    nocov = G.tape()
    swap(nocov, "P", T + 10, 1, "a")
    assert C.read_forward_path(nocov, "P", T, H, now=T + 2 * H) == {"state": "path_gap", "reason": "coverage_unknown", "rows": []}


def test_a_covered_quiet_pool_is_observed_with_no_rows_never_a_flat_price():
    c = G.tape()
    covered(c)
    swap(c, "Q", T + 10, 1, "q")                                            # tape is alive, pool P never trades
    r = C.read_forward_path(c, "P", T, H, now=T + 2 * H)
    assert r["state"] == "observed" and r["rows"] == [] and r["reason"] == "no_swaps"


def test_capture_reads_only_through_indexes():
    c = G.tape()
    covered(c)
    for i in range(20):
        swap(c, "P", T + i * 30, 100 + i, "p%d" % i)
    G.assert_indexed(G.plans_of(c, lambda con: C.read_forward_path(con, "P", T, H, now=T + 2 * H)))
    G.assert_guard_catches_a_scan()


def test_censored_share_by_reason_splits_entered_from_skipped():
    recs = [{"entered": True, "state": "observed"}, {"entered": True, "state": "path_gap"}, {"entered": False, "state": "no_pool"}, {"entered": False, "state": "observed"},
            {"entered": False, "state": "pending"}]
    out = C.censored_share(recs)
    assert out["entered"] == {"n": 2, "censored": 1, "share": 0.5, "by_reason": {"path_gap": 1}}
    assert out["skipped"] == {"n": 2, "censored": 1, "share": 0.5, "by_reason": {"no_pool": 1}}          # pending is not yet matured: excluded from the denominator
    assert out["all"]["share"] == 0.5 and out["pending"] == 1


def test_interior_slot_outlier_cannot_be_hidden_as_no_swaps():
    c = G.tape()
    covered(c)
    swap(c, 'Q', T + 1, 1000, 'first')
    swap(c, 'P', T + 50, 2000, 'middle')
    swap(c, 'Q', T + 99, 1000, 'last')
    result = C.read_forward_path(c, 'P', T, 100., now=T + 100.)
    assert result['state'] == 'observed'
    assert [r['signature'] for r in result['rows']] == ['middle']


def test_received_window_cap_censors_instead_of_using_a_prefix(monkeypatch):
    c = G.tape()
    covered(c)
    for i in range(3):
        swap(c, 'Q', T + i, 1000 + i, 'q'+str(i))
    swap(c, 'P', T + 10, 2000, 'target')
    monkeypatch.setattr(C, 'ROWS_CAP', 2)
    result = C.read_forward_path(c, 'P', T, 100., now=T + 100.)
    assert result == {'state': 'path_gap', 'reason': 'rows_cap', 'rows': []}
