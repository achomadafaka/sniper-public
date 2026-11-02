"""Every setting a test monkeypatches must actually exist.

`monkeypatch.setattr(cfg, "NAME", value, raising=False)` will happily create a name nothing reads.
On 2026-09-08 that hid a live defect for months: `MEMECOIN_REAL_MIN_SOURCE_BUY_USD` was added in
8659c818, REPLACED by the market-cap ratio in 556d50d8, and `test_memecoin_source_buy_floor.py` kept
setting it with `raising=False` the whole time. The tests asserted ratio-only behaviour underneath a
dollar floor that did not exist. The moment `a80b17b5` gave the name a consumer again, three tests
refused — not because the new gate was wrong, but because they had been green for a reason that had
stopped being true.

`raising=False` is legitimate for a setting that exists but may be absent on an older module, and
for genuinely optional attributes. It is not legitimate as a way to invent one. This guard reads
every name the suite patches and asserts the settings module defines it, so the next resurrection
fails at the moment the patch is written rather than months later when someone wires the name.

Checked 2026-09-08: 44 names patched with raising=False, 0 undefined.
2026-10-14: the scan only matched double quotes, so single-quoted invented names
(MEMECOIN_SLOT_LANE_GRADUATION, MEMECOIN_REAL_ONCHAIN_BUY_SIZE_MCAP_PCT) slipped past; it reads both now.
"""
import pathlib
import re

from sniper.settings import memecoin as cfg

REPO = pathlib.Path(__file__).resolve().parents[1]
_PATCH = re.compile(
    r'setattr\(\s*[A-Za-z_][A-Za-z_0-9.]*\s*,\s*["\'](MEMECOIN_[A-Z_0-9]+)["\'][^)]*raising\s*=\s*False',
    re.S)


def _patched_names():
    found = {}
    for path in sorted((REPO / "tests").rglob("test_*.py")):
        try:
            text = path.read_text()
        except OSError:
            continue
        for m in _PATCH.finditer(text):
            found.setdefault(m.group(1), path.name)
    return found


def test_the_scanner_actually_finds_patches():
    """A guard that matches nothing would pass forever."""
    names = _patched_names()
    assert len(names) >= 20, "the raising=False scanner stopped matching: %d" % len(names)


def test_every_raising_false_setting_exists():
    missing = {n: f for n, f in _patched_names().items() if not hasattr(cfg, n)}
    assert not missing, (
        "these names are invented by a test and read by nothing — either wire them or stop "
        "patching them: %s" % missing)
