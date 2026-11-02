"""The REAL Ink executor over the scripted chain (conftest `ink_real`): a smoke test that the
harness drives production seams end to end before any scenario relies on it."""
from ink_constants import INK_TOKEN


def test_a_canonical_entry_fills_once_through_the_real_seams(ink_real):
    t = ink_real()
    pid = t.reserve()
    r = t.ex["execute_fn"](t.conn, pid, INK_TOKEN, 10.0, "2026-09-29T09:00:01Z")
    assert r["status"] == "filled", r
    assert t.acct.sign_calls == len(t.chain.sent) == 2          # approve + swap
    assert t.conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE network_id='57073' "
                          "AND is_open=1").fetchone()[0] == 1
