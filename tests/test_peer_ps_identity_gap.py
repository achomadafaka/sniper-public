"""Offline eCat RC witnesses and reader-boundary regressions."""
import sqlite3

import pytest

from sniper.services import memecoin_desk_ps_tape as PT
from test_desk_risk_evidence_reader import NOW, ps, read, reader


@pytest.mark.parametrize("column", ["mint", "quote_mint"])
def test_explicit_conflicting_pool_row_cannot_prove_no_recent_swaps(reader, tmp_path, column):
    ps(tmp_path)
    with sqlite3.connect(PT.tape_path(tmp_path)) as c:
        c.execute("UPDATE swaps_ps SET %s='wrong_identity' WHERE received_at>?" % column, (NOW - 30,))
    ev = read(reader, tmp_path)
    assert ev["swaps_60s"] is None, ev


def test_individual_decode_loss_after_coalesced_gap_is_not_continuous(reader, tmp_path):
    ps(tmp_path, loss=(-32.1, None))
    with sqlite3.connect(PT.tape_path(tmp_path)) as c:
        c.executemany("INSERT INTO ps_decode_losses VALUES(?,?,?,?)", [
            ("lost1", "decode_loss", 8, NOW - 32.1), ("lost2", "decode_loss", 9, NOW - 31.9)])
    ev = read(reader, tmp_path)
    assert ev["buy_sol_30s"] is None, ev


def test_null_pool_identity_still_uses_the_canonical_binding(reader, tmp_path):
    ps(tmp_path)
    with sqlite3.connect(PT.tape_path(tmp_path)) as c:
        c.execute("UPDATE swaps_ps SET mint=NULL, quote_mint=NULL")
    ev = read(reader, tmp_path)
    assert ev["fresh"] is True and ev["buy_sol_30s"] == 4


@pytest.mark.parametrize("bad_schema", [False, True])
def test_missing_or_wrong_individual_loss_schema_is_unknown(reader, tmp_path, bad_schema):
    ps(tmp_path, loss=(-32.1, None))
    with sqlite3.connect(PT.tape_path(tmp_path)) as c:
        c.execute("DROP TABLE ps_decode_losses")
        if bad_schema:
            c.execute("CREATE TABLE ps_decode_losses(signature TEXT,cause TEXT,slot INTEGER)")
    ev = read(reader, tmp_path)
    assert ev["fresh"] is False and ev["buy_sol_30s"] is None


def test_missing_individual_loss_time_index_is_unknown(reader, tmp_path):
    ps(tmp_path)
    with sqlite3.connect(PT.tape_path(tmp_path)) as c:
        c.execute("DROP INDEX ps_decode_losses_received")
    ev = read(reader, tmp_path)
    assert ev["fresh"] is False and ev["buy_sol_30s"] is None


def test_coalesced_and_individual_losses_share_one_row_budget(reader, tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "MAX_ROWS", 4)
    ps(tmp_path, loss=(-100, None))
    with sqlite3.connect(PT.tape_path(tmp_path)) as c:
        c.executemany("INSERT INTO ps_decode_losses VALUES(?,?,?,?)", [
            ("lost:%s" % i, "decode_loss", i, NOW - 80 + i) for i in range(4)])
    ev = read(reader, tmp_path)
    assert ev["fresh"] is False and "row_limit" in ev["unknown"]
