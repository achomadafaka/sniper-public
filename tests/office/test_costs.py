from sniper.office.costs import completed_day_cost


def test_fixed_monthly_charge_uses_utc_calendar_and_completed_days():
    assert completed_day_cost('2026-10-01', look_day='2026-10-02') == 800. / 31
    assert completed_day_cost('2026-02-01', look_day='2026-02-02') == 800. / 28
    assert completed_day_cost('2028-02-29', look_day='2028-03-01') == 800. / 29
    assert completed_day_cost('2026-10-02', look_day='2026-10-02') is None
    assert completed_day_cost('2026-10-03', look_day='2026-10-02') is None


def test_epoch_charges_each_touched_day_in_full_and_restart_cannot_drop_day():
    from datetime import datetime, timezone
    from sniper.office.costs import epoch_cost
    def t(day, hour):
        return datetime(2026, 10, day, hour, tzinfo=timezone.utc).timestamp()
    a = epoch_cost(opened_at=t(1, 12), ended_at=t(2, 12), look_at=t(3, 0))
    b = epoch_cost(opened_at=t(2, 12), ended_at=t(2, 20), look_at=t(3, 0))
    assert a['amount_usd'] == 2 * 800. / 31
    assert b['amount_usd'] == 800. / 31
    assert epoch_cost(opened_at=t(1, 12), ended_at=t(2, 0), look_at=t(3, 0))['days'] == ['2026-10-01']
    pending = epoch_cost(opened_at=t(2, 12), ended_at=t(2, 20), look_at=t(2, 21))
    assert pending['amount_usd'] == 800. / 31
    assert epoch_cost(opened_at=t(2, 12), ended_at=None, look_at=t(2, 21))['amount_usd'] is None
