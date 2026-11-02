"""The presence of a vet row does not prove its missing decision timestamp."""
import sqlite3

from test_desk_lessons import W, T0, _fixture


def test_missing_verdict_response_time_is_unknown_instead_of_request_time(tmp_path):
    _desk, ledger, deskdb = _fixture(tmp_path)
    c = sqlite3.connect(deskdb)
    c.execute("UPDATE desk_tickets SET response_at=NULL WHERE ticket_id='desk:U:a1' AND stage='vet'")
    c.commit()
    c.close()
    units, _ = W.discover(ledger, deskdb, T0 + 6000, W.Clock(0), 100, set())
    actual = next(u for u in units if u['key'] == 'pos:1')
    assert actual['t_verdict'] is None, actual['t_verdict']
