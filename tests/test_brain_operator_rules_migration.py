"""F24: 042 timeout/rollback contract, offline and isolated PostgreSQL checks."""
import re
import subprocess

import pytest

from test_brain_graph_postgres import CONTAINER, ROOT, _command, pg, sql

NAMES = ("supabase_042_brain_operator_rules.sql", "supabase_042_rollback_brain_operator_rules.sql")


@pytest.mark.parametrize("name", NAMES)
def test_operator_rule_index_timeouts_are_inside_the_index_transaction(name):
    source = (ROOT / "migrations" / name).read_text()
    statements = [part.strip().upper() for part in re.sub(r"--[^\n]*", "", source).split(";")
                  if part.strip()]
    assert statements[0] == "BEGIN"
    assert statements[-1] == "COMMIT"
    assert statements[1] == "SET LOCAL LOCK_TIMEOUT = '1S'"
    assert statements[2] == "SET LOCAL STATEMENT_TIMEOUT = '15S'"
    assert statements[3].startswith("CREATE INDEX" if name == NAMES[0] else "DROP INDEX")


@pytest.mark.skipif(not CONTAINER, reason="requires labelled network-isolated disposable PostgreSQL")
def test_operator_rule_index_apply_rollback_and_lock_timeout_are_atomic(pg):
    forward, rollback = [(ROOT / "migrations" / name).read_text() for name in NAMES]
    present = "SELECT to_regclass('public.brain_facts_operator_rules') IS NOT NULL;"
    sql(pg, forward)
    assert sql(pg, present).stdout.strip() == "t"
    sql(pg, forward)  # idempotent rerun
    sql(pg, rollback)
    assert sql(pg, present).stdout.strip() == "f"
    sql(pg, rollback)
    for migration, before in [(forward, "f"), (rollback, "t")]:
        if before == "t":
            sql(pg, forward)
        blocker = subprocess.Popen(_command(pg), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            blocker.stdin.write("BEGIN; LOCK TABLE public.brain_facts IN ACCESS EXCLUSIVE MODE; "
                                "SELECT 'locked'; SELECT pg_sleep(2); COMMIT;")
            blocker.stdin.close()
            assert blocker.stdout.readline().strip() == "locked"
            refused = sql(pg, migration, ok=False)
            assert refused.returncode != 0 and "lock timeout" in refused.stderr
        finally:
            blocker.wait(timeout=10)
            assert blocker.returncode == 0, blocker.stderr.read()
        assert sql(pg, present).stdout.strip() == before
