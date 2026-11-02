"""Operator-fixed Office cost: $800/month, charged once per completed UTC day."""
import calendar

RULE_ID = "aa0e9a583757cf929f3babbbb3b63f3c6bd67a49c3a4a63d3f9689a6eeb83290"
from datetime import date


def completed_day_cost(day, *, look_day):
    """USD for this completed UTC day; None while the day is incomplete."""
    day, look = date.fromisoformat(day), date.fromisoformat(look_day)
    return 800. / calendar.monthrange(day.year, day.month)[1] if day < look else None


def epoch_cost(*, opened_at, ended_at, look_at):
    """Evaluated epochs pay every touched day in full, including a day shared with another epoch."""
    import math
    end = look_at if ended_at is None else ended_at
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (opened_at, end, look_at)) or not opened_at < end <= look_at:
        raise ValueError('invalid_epoch_cost_interval')
    from sniper.office.readiness.verdict import epoch_days, flat_office_cost
    days = epoch_days(opened_at, end)
    known = flat_office_cost(days)
    return dict(state='pending' if ended_at is None else 'known', amount_usd=None if ended_at is None else known,
                known_cost_usd=known, days=days, allocation_rule_id=RULE_ID)
