import json
from test_desk_watch_v22_pipeline import (
    _book, _tr, BASIS, T_RESP, RESPONSE, iso, waiting, databases, fire, api, store,
)


def test_gap_reload_requires_new_confirmations():
    b = _book(kind='px_up', level=BASIS, confirm=2)
    assert _tr(b, BASIS * 1.1, T_RESP + 1) == []
    assert _tr(b, BASIS * 1.1, T_RESP + 2, gap=True) == []
    trig = {'kind': 'px_up', 'level_price_sol': BASIS, 'confirm': 2, 'expires_at': iso(T_RESP + 600)}
    b.load([('t1', 'M', json.dumps(trig), RESPONSE)], T_RESP + 3)
    assert _tr(b, BASIS * 1.1, T_RESP + 4) == []


def test_accepted_on_time_fire_is_not_expired_before_courier(waiting):
    db, ticket = waiting
    api().record_fire(db[0], ticket, fire(block_time=T_RESP+59, received_at=T_RESP+59),
                      now_iso=iso(T_RESP+61))
    assert api().expire_watches(db[0], now_iso=iso(T_RESP+61)) == []


def test_stale_worklist_recovery_discards_unobserved_hold():
    from sniper.services.memecoin_desk_universe import StandingBook
    b = StandingBook()
    trig = {'kind': 'px_hold', 'level_price_sol': BASIS * .92, 'confirm': 1,
            'hold_s': 5, 'expires_at': iso(T_RESP + 600)}
    rows = [('t1', 'M', json.dumps(trig), RESPONSE)]
    b.load(rows, T_RESP)
    assert _tr(b, BASIS * .91, T_RESP + 1) == []
    assert _tr(b, BASIS * .93, T_RESP + 2) == []
    b.failed(T_RESP + 32)
    assert _tr(b, BASIS * .90, T_RESP + 33) == []
    b.load(rows, T_RESP + 34)
    assert _tr(b, BASIS * .94, T_RESP + 34) == []
