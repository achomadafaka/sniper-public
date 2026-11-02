"""A receipt domain identifies the host and the boot."""
from pathlib import Path


def test_clock_domain_uses_host_and_boot_and_fails_unknown(monkeypatch):
    from sniper.util import receipt_clock
    values = {'/etc/machine-id': 'host-a', '/proc/sys/kernel/random/boot_id': 'boot-a'}
    monkeypatch.setattr(Path, 'read_text', lambda p, **kw: values[str(p)])
    receipt_clock.clock_domain.cache_clear()
    first = receipt_clock.clock_domain()
    values['/proc/sys/kernel/random/boot_id'] = 'boot-b'
    receipt_clock.clock_domain.cache_clear()
    assert receipt_clock.clock_domain() != first
    values['/etc/machine-id'] = ''
    receipt_clock.clock_domain.cache_clear()
    assert receipt_clock.clock_domain() is None
    receipt_clock.clock_domain.cache_clear()
