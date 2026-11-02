import json
from datetime import datetime, timezone
from sniper.analysis.memecoin_x import clock_health as ch


def _write(tmp_path, offset_s, at):
    p = tmp_path / "clock_offset.json"
    p.write_text(json.dumps({"offset_s": offset_s, "at": at}))
    return str(p)


def test_healthy_recent_offset(tmp_path):
    now = 1_000_000.0
    p = _write(tmp_path, offset_s=25200.0, at=now - 60)   # 1 min old, within bounds
    h = ch.clock_health(path=p, now=now)
    assert h["healthy"] is True and h["reason"] == "ok"


def test_missing_offset_file_is_unhealthy(tmp_path):
    p = str(tmp_path / "does_not_exist.json")
    h = ch.clock_health(path=p, now=1_000_000.0)
    assert h["healthy"] is False and h["reason"] == "missing"


def test_stale_offset_is_unhealthy(tmp_path):
    now = 1_000_000.0
    p = _write(tmp_path, offset_s=0.0, at=now - 1000)     # >900s old
    h = ch.clock_health(path=p, now=now)
    assert h["healthy"] is False and h["reason"] == "stale"


def test_out_of_bounds_offset_is_unhealthy(tmp_path):
    now = 1_000_000.0
    p = _write(tmp_path, offset_s=999999.0, at=now - 10)  # >2 days
    h = ch.clock_health(path=p, now=now)
    assert h["healthy"] is False and h["reason"] == "out_of_bounds"


def test_non_finite_offset_is_unhealthy(tmp_path):
    now = 1_000_000.0
    p = _write(tmp_path, offset_s=float("nan"), at=now - 10)
    h = ch.clock_health(path=p, now=now)
    assert h["healthy"] is False and h["reason"] == "non_finite"


def test_non_finite_offset_timestamp_is_unhealthy(tmp_path):
    p = _write(tmp_path, offset_s=0.0, at=float("inf"))
    h = ch.clock_health(path=p, now=1_000_000.0)
    assert h["healthy"] is False and h["reason"] == "non_finite"


def test_observed_at_uses_the_checked_snapshot(tmp_path):
    now = 1_000_000.0
    p = _write(tmp_path, offset_s=25_200.0, at=now - 60)
    iso, h = ch.x_observed_at_or_none(path=p, now=now)
    assert h["healthy"] is True
    assert iso == datetime.fromtimestamp(974_800.0, tz=timezone.utc).isoformat()


def test_x_observed_at_none_when_unhealthy(tmp_path):
    p = str(tmp_path / "missing.json")
    iso, h = ch.x_observed_at_or_none(path=p, now=1_000_000.0)
    assert iso is None and h["healthy"] is False
