import importlib.util
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location("x", Path(__file__).resolve().parents[1] / "scripts/desk_curve_binding_crosscheck.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def test_crosscheck_counts_and_trips():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE ps_grads(pool, mint, quote_mint)")
    c.execute("CREATE TABLE pools(pool, mint, quote_mint)")
    c.executemany("INSERT INTO ps_grads VALUES(?,?,?)", [("a", "m1", "w"), ("b", "m2", "w"), ("c", "m3", "w")])
    c.execute("INSERT INTO pools VALUES('a','m1','w')")
    assert M.crosscheck(c) == {"agree": 1, "disagree": 0, "unchecked": 2, "tripwire": False, "disagreeing_pools": []}
    c.execute("INSERT INTO pools VALUES('b','mX','w')")
    out = M.crosscheck(c)
    assert out["disagree"] == 1 and out["tripwire"] and out["disagreeing_pools"] == ["b"]
