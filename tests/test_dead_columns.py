"""The dead-column auditor, and its calibration IS this test.

Every fixture below is a shape verified in the tree, not one handed over. The calibration that
preceded these tests used an answer key nobody had checked, and it was wrong on two of the lines
that read OK — so the key lives here, with the source shape it came from named beside it.

FOUR STATES, because "is there a writer" and "does the writer ever receive a value" are different
questions needing different fixes:

  NO_WRITE_REFERENCE    nothing names it in a write position
  WRITER_NEVER_REACHED  a reference could carry a real value; the branch never ran
  WRITER_ALWAYS_NULL    the writer is real and every caller passes a literal None
  SPARSE                real values, incomplete — printed, never judged
"""
import importlib.util
import pathlib
import sqlite3

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "research" / "dead_columns.py"
_spec = importlib.util.spec_from_file_location("dead_columns", _SRC)
D = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(D)


def _supplies(column, line):
    """The tool's own value-classification for one source line."""
    rhs = D._assignments(column, line)
    if not rhs:
        return None                                    # not a write reference at all
    return any(not D._NULL_RHS.match(r) for r in rhs)


# --- the source shapes, each verified in the tree -------------------------------------------

def test_a_multiline_dict_key_writer_counts_as_supplying_a_value():
    """`exit_controller.py:164` — the value is on the NEXT line.

    A matcher blind to this shape saw only the `= None` two lines below and reported
    `route_degradation_since` as having NO writer, which is the verdict that means "real find".
    """
    assert _supplies("route_degradation_since",
                     '        changes["route_degradation_since"] = (') is True


def test_the_same_column_cleared_to_None_does_not_supply_a_value():
    """`exit_controller.py:167`. A clear is not a fill."""
    assert _supplies("route_degradation_since",
                     '            changes["route_degradation_since"] = None') is False


def test_a_signature_default_is_not_a_value_even_beside_another_kwarg():
    """`db_memecoin.py:6313`. The kwarg capture runs to end of line, so the RHS carries the NEXT
    argument too; only the leading token is this column's value."""
    assert _supplies("fee_paid_lamports",
                     "  fee_paid_lamports=None, depth_first_rung_pct=None,") is False


def test_an_insert_column_list_is_not_an_assignment():
    """`db_memecoin.py:6325`. Naming a column in an INSERT proves a writer exists and says nothing
    about the value — that is what the caller-side scan decides."""
    assert _supplies("fee_paid_lamports",
                     '"attempted_at,status,sig,total_ms,priority_fee_microlamports,'
                     'fee_paid_lamports,"') is None


def test_a_kwarg_passthrough_supplies_a_value():
    """`memecoin_copy_trader.py:283` — the paper lane's writer for assigned_policy_id."""
    assert _supplies("assigned_policy_id", "            assigned_policy_id=assigned_policy_id,") is True


def test_an_update_set_supplies_a_value():
    assert _supplies("close_pending_at",
                     '"UPDATE memecoin_positions SET close_pending_at=?, close_claim_id=? "') is True


def test_a_bare_read_is_not_a_write_reference():
    assert _supplies("entry_mcap", "        e_mcap = pos.get('entry_mcap') or 0.0") is None


def test_an_equality_comparison_is_not_a_write():
    assert _supplies("status", "        if status == 'filled':") is None


# --- the state machine ----------------------------------------------------------------------

@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_t(id INTEGER PRIMARY KEY, mode TEXT, "
              "never TEXT, nulled TEXT, reached TEXT, partial TEXT)")
    for i in range(300):
        c.execute("INSERT INTO memecoin_t(mode, partial) VALUES(?,?)",
                  ("real", "x" if i % 4 else None))
    c.commit()
    return c


def _scan(conn, monkeypatch, writers):
    monkeypatch.setattr(D, "find_writers", lambda col, limit=4: writers.get(col, []))
    monkeypatch.setattr(D, "column_age", lambda col: "2026-07-01")
    found, _skipped = D.scan(conn, like="memecoin_t", min_rows=200, sparse_under=0.9)
    return {f["column"]: f["state"] for f in found}


def test_no_write_reference_when_nothing_names_it(conn, monkeypatch):
    assert _scan(conn, monkeypatch, {})["never"] == "NO_WRITE_REFERENCE"


def test_writer_always_null_when_every_reference_is_a_literal_none(conn, monkeypatch):
    states = _scan(conn, monkeypatch, {"nulled": [("f.py:1", "col=None", False)]})
    assert states["nulled"] == "WRITER_ALWAYS_NULL"


def test_writer_never_reached_when_any_reference_could_supply_a_value(conn, monkeypatch):
    states = _scan(conn, monkeypatch,
                   {"reached": [("f.py:1", "col=None", False), ("f.py:2", "col=value", True)]})
    assert states["reached"] == "WRITER_NEVER_REACHED", \
        "one reference that could carry a value outranks any number of None defaults"


def test_a_partially_filled_column_is_sparse_not_dead(conn, monkeypatch):
    assert _scan(conn, monkeypatch, {})["partial"] == "SPARSE"


def test_a_full_column_is_not_reported(conn, monkeypatch):
    assert "mode" not in _scan(conn, monkeypatch, {})


# --- the denominator, which was the biggest defect ------------------------------------------

def test_the_where_scope_changes_the_population(conn, monkeypatch):
    """A table mixing populations makes every subset-scoped column look sparse.

    memecoin_positions holds 30,352 paper rows against 1,042 funded, so an unscoped run reports the
    funded lane as abandoned. The arithmetic is right; the denominator is a different population
    from the writer.
    """
    conn.executemany("INSERT INTO memecoin_t(mode, reached) VALUES('paper','v')",
                     [()] * 900)
    conn.commit()
    monkeypatch.setattr(D, "find_writers", lambda col, limit=4: [])
    monkeypatch.setattr(D, "column_age", lambda col: None)
    unscoped = {f["column"]: f for f in D.scan(conn, like="memecoin_t", min_rows=200,
                                               sparse_under=0.9)[0]}
    scoped = {f["column"]: f for f in D.scan(conn, like="memecoin_t", min_rows=200,
                                             sparse_under=0.9, where="mode='paper'")[0]}
    assert unscoped["reached"]["state"] == "SPARSE", "75% of the mixed table, so it reads sparse"
    assert "reached" not in scoped, "scoped to its own population it is complete"


def test_a_small_table_is_skipped_with_its_denominator(conn, monkeypatch):
    monkeypatch.setattr(D, "find_writers", lambda col, limit=4: [])
    found, skipped = D.scan(conn, like="memecoin_t", min_rows=100000, sparse_under=0.9)
    assert found == []
    assert skipped and skipped[0][1] == 300, "the row count travels with the skip"


def test_the_help_warns_about_the_mixed_population():
    """Alpha: it belongs in --help, not only the docstring."""
    import argparse
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        D.main(["--help"])
    assert "FUNDED LANE IS ABANDONED" in buf.getvalue()


# --- grouping, and the scanner not reading itself ---------------------------------------------

def test_a_comment_describing_a_write_is_not_a_write():
    """A SCANNER MUST NOT READ ITS OWN PROSE.

    This tool quotes `changes["route_degradation_since"] = (` in a comment as the example of the
    shape it matches, and the first grouped run duly listed the tool itself as a writer of that
    column. Third occurrence of this class in this repo, so it is pinned here.
    """
    corpus = D._corpus()
    assert not any(rel.endswith("dead_columns.py") for rel, _t, _l in corpus), \
        "the scanner must exclude its own source"
    hits = D.find_writers("route_degradation_since")
    assert hits, "the real writers must still be found"
    assert all("dead_columns" not in where for where, _s, _v in hits)


def test_a_commented_out_assignment_is_not_a_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "_CORPUS",
                        [("fake.py", "# col_x = value\n", ["# col_x = value"])])
    assert D.find_writers("col_x") == []


def test_groups_print_smallest_first_so_a_lone_writer_is_not_buried(conn, monkeypatch, capsys):
    """Grouping changes ordering; collapsing would destroy information. Only the first is safe.

    A column whose writer sits alone in some other file is exactly the case worth finding, so it
    must not sit behind a twenty-two-strong family.
    """
    conn.execute("ALTER TABLE memecoin_t ADD COLUMN lone TEXT")
    conn.commit()
    writers = {"never": [("big.py:1", "a", True)], "nulled": [("big.py:2", "b", True)],
               "reached": [("big.py:3", "c", True)], "lone": [("other.py:9", "d", True)]}
    monkeypatch.setattr(D, "find_writers", lambda col, limit=4: writers.get(col, []))
    monkeypatch.setattr(D, "column_age", lambda col: None)
    # main() opens its own connection from --db; hand it the fixture instead of a file.
    monkeypatch.setattr(D.sqlite3, "connect", lambda *a, **k: conn)
    D.main(["--like", "memecoin_t", "--min-rows", "200"])
    out = capsys.readouterr().out
    assert "3 columns, all writers in big.py" in out, "the family is grouped, not hidden"
    assert out.index(".lone") < out.index("3 columns, all writers in big.py"), \
        "the lone writer must print before the family, not behind it"
    for col in ("never", "nulled", "reached", "lone"):
        assert f".{col}" in out, f"{col} disappeared — grouping must not drop a row"
