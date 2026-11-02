"""Corrected-clock utility: learn host->real-UTC offset from a trusted time, correct now."""
import time

from sniper.util import clock


def test_offset_corrects_a_skewed_host_clock(tmp_path):
    p = str(tmp_path / "off.json")
    # simulate the real bug: host clock is ~7h AHEAD of real UTC. A trusted real-UTC epoch is
    # therefore ~25200s behind host now. After recording it, real_utc_now must return ~real.
    real_epoch = time.time() - 25200
    clock.record_real_utc(real_epoch, path=p)
    assert abs(clock.offset_s(path=p) - 25200) < 5          # learned ~+7h offset
    assert abs(clock.real_utc_now(path=p) - real_epoch) < 5  # corrected now ≈ real, not host


def test_absurd_offset_rejected(tmp_path):
    # a >2-day "offset" means a malformed/stub Date header (the bug that polluted prod) -> don't write
    p = str(tmp_path / "off.json")
    clock.record_real_utc(time.time() - 300000, path=p)   # ~3.5 days -> rejected
    assert clock.offset_s(path=p) == 0.0                   # nothing written


def test_no_offset_file_falls_back_to_host(tmp_path):
    p = str(tmp_path / "missing.json")
    assert clock.offset_s(path=p) == 0.0
    assert abs(clock.real_utc_now(path=p) - time.time()) < 5   # no correction -> host now
