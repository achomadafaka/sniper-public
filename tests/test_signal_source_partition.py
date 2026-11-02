"""A funded aggregate must be able to split operator buys from copy signals.

Funded entries ran 0% manual through 2026-08-23 and 89% manual on 08-28/08-29, and the two lanes
point opposite ways on realized PnL. Any unpartitioned "recent funded closes" number averages them
at a weight that inverted inside the window.
"""
import json
import sqlite3

import pytest

from sniper.persistence.db_memecoin import copy_only_sql, signal_source_sql

DDL = """CREATE TABLE memecoin_positions (
  id INTEGER PRIMARY KEY, execution_mode TEXT, source_user_id TEXT,
  source_handle TEXT, funded_contract_json TEXT)"""


def _contract(**source):
    return json.dumps({"source": source, "policy_id": "follow-30-50-v1"})


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(DDL)
    rows = [
        (1, "real", None,     "(real)", _contract(signal_source="manual")),
        (2, "real", "wallet", "alice",  _contract(signal_source="fomo_follow")),
        (3, "real", "wallet", "bob",    None),                       # pre-contract regime
        (4, "real", "wallet", "carol",  _contract(decision_at="x")),  # contract, no key
        # NULL uid + placeholder handle, but the contract says COPY. The consequence must not
        # override the record: this is the row a uid-based heuristic would misfile.
        (5, "real", None,     "(real)", _contract(signal_source="fomo_follow")),
    ]
    c.executemany("INSERT INTO memecoin_positions VALUES (?,?,?,?,?)", rows)
    c.commit()
    return c


def _label(conn, pid):
    return conn.execute(
        "SELECT %s FROM memecoin_positions WHERE id=?" % signal_source_sql(), (pid,)).fetchone()[0]


def test_a_manual_contract_reads_manual(conn):
    assert _label(conn, 1) == "manual"


def test_a_copy_contract_reads_copy(conn):
    assert _label(conn, 2) == "copy"


def test_a_missing_contract_is_unknown_not_copy(conn):
    """Absence tracks the funded-contract regime boundary, so it is not missing at random.
    Folding it into either lane would be an inference presented as data."""
    assert _label(conn, 3) == "unknown"
    assert _label(conn, 4) == "unknown"


def test_null_source_user_id_does_NOT_decide_the_label(conn):
    """NULL uid is the CONSEQUENCE of a manual buy, not the test for one. Reading the consequence
    would also catch a future copy path that lost its attribution — two facts, one label."""
    assert _label(conn, 5) == "copy", "a uid-based heuristic misfiles this row"


def test_copy_only_excludes_manual_AND_unknown(conn):
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM memecoin_positions WHERE %s ORDER BY id" % copy_only_sql())]
    assert ids == [2, 5]


def test_the_partition_is_exhaustive(conn):
    """Every row lands in exactly one bucket — no row silently vanishes from a grouped report."""
    total = conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0]
    grouped = conn.execute(
        "SELECT SUM(n) FROM (SELECT COUNT(*) n FROM memecoin_positions GROUP BY %s)"
        % signal_source_sql()).fetchone()[0]
    assert grouped == total == 5


# --- the live consumer: profile_matched_v1's fit population -----------------------------------

def test_the_profile_fit_excludes_manual_but_keeps_pre_contract_history():
    """The fit's estimand is a WALLET's prior closes, so an operator buy is not evidence about it.

    Position 39545 is the case that makes this real: a manual buy recorded against a wallet UUID
    rather than a typed nickname, sitting inside that wallet's 29-close fit. Most manual rows
    cannot reach a fit because they have no uid — but that is an accident of the keying, not a
    guard, and it fails the moment a manual buy carries a wallet id.
    """
    from sniper.services.memecoin_profile_fit import observations_sql
    sql = observations_sql()
    assert "<> 'manual'" in sql, "manual buys can still enter a wallet's copy fit"
    # and NOT a blanket copy-only filter: pre-contract rows record no opinion and are genuine
    # history, so excluding them would trade one bias for another
    assert "='copy'" not in sql, "pre-contract history must not be dropped to avoid manual rows"
