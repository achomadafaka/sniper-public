"""Graduated PumpSwap census (scripts/research/pumpswap_census.py): count-only, read-only."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "pumpswap_census", Path(__file__).resolve().parents[1] / "scripts" / "research" / "pumpswap_census.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)
AGENT = next(iter(C.AGENTS))
H = 1_800_000_000 // 3600 * 3600
W = (H - 300.0, H + 3600 + 1800.0)


def tape(rows, gaps=()):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE swaps_ps(pool TEXT, block_time INTEGER, slot INTEGER, tx_index INTEGER, ordinal INTEGER, "
              "actor TEXT, map_status TEXT, lp_fee_bps TEXT, protocol_fee_bps TEXT, creator_fee_bps TEXT, mint TEXT, "
              "signature TEXT, received_at REAL, PRIMARY KEY(signature, ordinal))")
    c.execute("CREATE TABLE gaps_ps(at REAL, cause TEXT, last_slot INTEGER)")
    c.executemany("INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  [(*r, "sig%04d" % i, float(r[1]) if r[1] is not None else H + 10.0) for i, r in enumerate(rows)])      # received_at = block_time (a row with none: inside the hour)
    c.executemany("INSERT INTO gaps_ps VALUES(?, 'x', 0)", [(g,) for g in gaps])
    c.commit()
    return c


def sw(pool, slot, actor="u", status="mapped", fee=("20", "5", "95"), bt=H + 10, mint=None):
    return (pool, bt, slot, 0, 0, actor, status, *fee, mint if mint is not None else "m" + pool)


def test_counts_pools_fee_proof_and_mapping_per_hour():
    rows = [sw("A", 1), sw("A", 2), sw("A", 3, fee=("20", "5", "300")),       # newest 3: 2 agree -> proven
            sw("B", 1), sw("B", 2, fee=("2", "93", "30")), sw("B", 3, fee=("20", "5", "90")),   # all differ
            sw("C", 1, actor=AGENT, fee=("0", "0", "0")), sw("C", 2, actor=AGENT, fee=("0", "0", "0")),
            sw("C", 3),                                                       # agent rows never prove
            sw("D", 1, status="unmapped"), sw("D", 2),                        # one unmapped row -> not mapped
            sw("E", 1, bt=H + 3600 + 5), sw("E", 2, bt=H + 3600 + 6),         # next hour
            sw("F", 1, bt=None)]
    out = C.census(tape(rows, gaps=[H + 100]), start=H, end=H + 7200)
    h0, h1 = out["hours"]
    assert {k: h0[k] for k in ("pools", "fee_proven", "mapped", "unmapped", "conflict", "mapped_mints", "swaps")} == {
        "pools": 4, "fee_proven": 2, "mapped": 3, "unmapped": 1, "conflict": 0, "mapped_mints": 3, "swaps": 11}
    assert (h1["pools"], h1["fee_proven"], h1["mapped"], h1["swaps"]) == (1, 1, 1, 2)
    assert out["no_block_time_rows"] == 1 and out["gaps"] == 1


def test_fee_proof_uses_the_newest_three_only():
    old_agree = [("20", "5", "95"), ("20", "5", "95")]
    rows = [sw("A", s, fee=f) for s, f in zip((1, 2), old_agree)] + [
        sw("A", 3, fee=("20", "5", "90")), sw("A", 4, fee=("20", "5", "85")), sw("A", 5, fee=("20", "5", "80"))]
    assert C.census(tape(rows), start=H, end=H + 3600)["hours"][0]["fee_proven"] == 0
    assert C.fee_proven([(1, 2, 3)]) is False and C.fee_proven([(1, 2, None), (1, 2, None)]) is False


def test_new_pools_conflict_mints_and_swap_spread():
    rows = [sw("A", 1, bt=H - 7200), sw("A", 2), sw("A", 3),                      # A active since before the hour
            sw("B", 1), sw("B", 2, status="conflict"),                            # B new and conflicting
            sw("C", 1, mint="shared"), sw("D", 1, mint="shared"),                 # two pools, one mint
            sw("C", 2, mint="shared"), sw("C", 3, mint="shared"), sw("C", 4, mint="shared")]
    out = C.census(tape(rows), start=H, end=H + 3600)
    [h] = out["hours"]
    assert (h["new_pools"], h["conflict"], h["unmapped"], h["mapped"], h["mapped_mints"]) == (3, 1, 0, 3, 2)
    assert (h["swaps_per_pool_median"], h["swaps_per_pool_p90"]) == (2, 4)               # counts 1,2,2,4
    assert out["days"] == [{"day": H // 86400 * 86400, "pools": 4, "new_pools": 3}]


def test_posthoc_pool_mints_fill_unmapped_only():
    rows = [sw("A", 1), sw("B", 1, status="unmapped", mint=""), sw("C", 1, status="unmapped", mint="")]
    [h] = C.census(tape(rows), start=H, end=H + 3600, pool_mints={"A": "ignored", "B": "tokB"})["hours"]
    assert (h["mapped"], h["unmapped"], h["posthoc_mapped"], h["posthoc_mints"], h["all_mints"]) == (1, 2, 1, 1, 2)


spec_m = importlib.util.spec_from_file_location(
    "pumpswap_pool_mints", Path(__file__).resolve().parents[1] / "scripts" / "research" / "pumpswap_pool_mints.py")
M = importlib.util.module_from_spec(spec_m)
spec_m.loader.exec_module(M)


def _acct(base, quote, owner=M.PUMPSWAP_PROGRAM, n=301):
    import base64
    data = bytearray(max(n, 107))
    data[43:75], data[75:107] = base, quote
    return {"owner": owner, "data": [base64.b64encode(bytes(data[:n])).decode(), "base64"]}


def test_pool_mints_statuses_and_failed_batch_writes_nothing(tmp_path):
    t = tape([sw("P1", 1, status="unmapped"), sw("P2", 1, status="unmapped"), sw("P3", 1, status="unmapped"),
              sw("P4", 1, status="unmapped"), sw("OK", 1)])
    side = sqlite3.connect(str(tmp_path / "s.db"))

    def boom(pools):
        raise TimeoutError("rpc down")
    try:
        M.fill(t, side, boom, window=W)
    except TimeoutError:
        pass
    assert side.execute("SELECT COUNT(*) FROM pool_mints").fetchone()[0] == 0     # error never reads as missing
    vals = {"P1": _acct(b"\x01" * 32, b"\x02" * 32), "P2": None,
            "P3": _acct(b"\x01" * 32, b"\x02" * 32, owner="x"), "P4": _acct(b"\x01" * 32, b"\x02" * 32, n=100)}
    assert M.fill(t, side, lambda pools: (7, [vals[p] for p in pools]), window=W) == 4
    got = {r[0]: r[1:] for r in side.execute("SELECT pool, status, base_mint FROM pool_mints")}
    assert got["P1"] == ("read", M._b58(b"\x01" * 32)) and got["P2"] == ("missing", None)
    assert got["P3"][0] == "wrong_owner" and got["P4"][0] == "short_data" and "OK" not in got
    assert M.fill(t, side, lambda pools: (_ for _ in ()).throw(AssertionError("reread")), window=W) == 0   # once per pool


def test_curve_coverage_counts_curve_side_graduations_against_captured_create_pool():
    hourly = importlib.util.spec_from_file_location(
        "pumpswap_census_hourly", Path(__file__).resolve().parents[1] / "scripts" / "research" / "pumpswap_census_hourly.py")
    sys_path = __import__("sys").path
    sys_path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research"))
    mod = importlib.util.module_from_spec(hourly)
    hourly.loader.exec_module(mod)
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE pools(pool TEXT, canonical INTEGER)")
    c.execute("CREATE TABLE ps_grads(pool TEXT, received_at REAL)")
    c.execute("CREATE TABLE swaps_ps(pool TEXT, block_time INTEGER, map_status TEXT, received_at REAL)")
    c.executemany("INSERT INTO ps_grads VALUES(?,?)", [("A", H + 1), ("B", H + 2), ("C", H + 3), ("C", H + 4), ("D", H + 4000)])
    c.executemany("INSERT INTO pools VALUES(?,?)", [("A", 1), ("B", 0)])          # B captured but NOT canonical
    c.executemany("INSERT INTO swaps_ps VALUES(?,?,?,?)", [("B", H + 5, "mapped_curve_derived", H + 6), ("A", H + 5, "mapped", H + 6)])
    out = mod.curve_coverage(c, H, H + 3600)
    assert {k: out[k] for k in ("curve_graduations", "curve_grads_with_create_pool", "create_pool_coverage",
                                "derived_mapped_swaps")} == {"curve_graduations": 3, "curve_grads_with_create_pool": 1,
                                                             "create_pool_coverage": 1 / 3, "derived_mapped_swaps": 1}
    assert out["by_class"] is None                                                # no grad_class column: per class unknown
    old = sqlite3.connect(":memory:")                                             # a tape from before ps_grads
    assert mod.curve_coverage(old, H, H + 3600)["create_pool_coverage"] is None
    empty = sqlite3.connect(":memory:")
    empty.execute("CREATE TABLE pools(pool TEXT, canonical INTEGER)")
    empty.execute("CREATE TABLE ps_grads(pool TEXT, received_at REAL)")
    empty.execute("CREATE TABLE swaps_ps(pool TEXT, block_time INTEGER, map_status TEXT, received_at REAL)")
    assert mod.curve_coverage(empty, H, H + 3600)["create_pool_coverage"] is None   # none seen: unknown, not 0 / 1


def test_curve_coverage_reports_distinct_graduations_and_create_pool_coverage_per_class():
    hourly = importlib.util.spec_from_file_location(
        "pumpswap_census_hourly2", Path(__file__).resolve().parents[1] / "scripts" / "research" / "pumpswap_census_hourly.py")
    __import__("sys").path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research"))
    mod = importlib.util.module_from_spec(hourly)
    hourly.loader.exec_module(mod)
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE pools(pool TEXT, canonical INTEGER)")
    c.execute("CREATE TABLE ps_grads(pool TEXT, received_at REAL, grad_class TEXT)")
    c.execute("CREATE TABLE swaps_ps(pool TEXT, block_time INTEGER, map_status TEXT, received_at REAL)")
    rows = [("A", H + 1, "sol85"), ("A", H + 2, "sol85"),                  # two events of ONE graduation: counted once
            ("B", H + 3, "sol85"), ("C", H + 4, "sub_threshold"), ("D", H + 5, "sub_threshold"),
            ("E", H + 6, "quote_token"), ("F", H + 7, None)]               # NULL class = a row from before grad_class
    c.executemany("INSERT INTO ps_grads VALUES(?,?,?)", rows)
    c.executemany("INSERT INTO pools VALUES(?,?)", [("C", 1), ("D", 1), ("A", 0)])   # A captured but not canonical
    out = mod.curve_coverage(c, H, H + 3600)
    assert out["by_class"]["sol85"] == {"graduations": 2, "with_create_pool": 0, "coverage": 0.0}
    assert out["by_class"]["sub_threshold"] == {"graduations": 2, "with_create_pool": 2, "coverage": 1.0}
    assert out["by_class"]["quote_token"]["graduations"] == 1 and out["by_class"]["unknown"]["graduations"] == 1
    assert (out["sol85_graduations"], out["sol85_with_create_pool"], out["sol85_create_pool_coverage"]) == (2, 0, 0.0)
    assert out["curve_graduations"] == 6                                      # all classes still reported
    none = mod.curve_coverage(sqlite3.connect(":memory:"), H, H + 3600)
    assert none["by_class"] is None and none["sol85_create_pool_coverage"] is None


def test_paged_scan_matches_one_page_and_never_holds_a_transaction(monkeypatch):
    rows = [sw("A", 1), sw("A", 2), sw("B", 1, bt=H - 7200), sw("B", 2), sw("C", 1, bt=None), sw("C", 2, status="conflict")]
    whole = C.census(tape(rows), start=H, end=H + 3600)
    monkeypatch.setattr(C, "CHUNK_ROWS", 1)
    t = tape(rows)
    assert len(list(C._received_chunks(t, H - 300.0, H + 3600 + 1800.0))) == len([r for r in rows if r[1] is None or r[1] >= H - 300])                 # one row per page: the knob is live
    assert C.census(t, start=H, end=H + 3600) == whole
    assert not t.in_transaction
    assert whole["hours"][0]["new_pools"] == 2 and whole["no_block_time_rows"] == 1   # B first seen before H; A and C new


def test_hourly_run_does_not_queue_behind_a_running_census(tmp_path):
    import fcntl
    import pytest
    h = importlib.util.spec_from_file_location(
        "pumpswap_census_hourly", Path(__file__).resolve().parents[1] / "scripts" / "research" / "pumpswap_census_hourly.py")
    H2 = importlib.util.module_from_spec(h)
    h.loader.exec_module(H2)
    with open(tmp_path / "census_hourly.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(SystemExit, match="still holds the lock"):
            H2.roll(tmp_path, H, None)
