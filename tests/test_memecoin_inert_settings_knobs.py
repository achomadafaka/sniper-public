"""Twelve settings names were read through a fallback the settings module never defined (2026-09-01).

Every call site looked tunable:

    getattr(cfg, "MEMECOIN_EXIT_STALE_S", 180.0)
    _cfg(cfg, "MEMECOIN_EXIT_STALE_S", _STALE_S)

But `sniper/settings/memecoin.py` never defined the name, so the fallback was not a default that
`.env` could override — it was the ONLY reachable value. Setting `MEMECOIN_EXIT_STALE_S=60` in
`.env` changed nothing and raised no error. The knob reads as operator-tunable in three call sites
and is inert. `MEMECOIN_ROUTE_DEGRADATION_PCT` and `MEMECOIN_COPY_ARM_MIN_CLOSES` are on funded
surfaces, which is what makes a silent no-op dangerous rather than merely untidy.

Defining them with EXACTLY the fallback each call site already used is a behavioural no-op today —
none of the twelve appears in `.env` — while making the surface real. Any change of default here is
a change of live behaviour and must be deliberate, so the expected values are asserted literally.
"""
import pathlib
import re

import pytest

from sniper.settings import memecoin as cfg

# name -> the fallback its call sites used before this change. Changing a value here changes
# funded behaviour; it is not a cosmetic table.
EXPECTED = {
    "MEMECOIN_CLOSE_CLAIM_HELD_ALERT_S": 600.0,
    "MEMECOIN_COPY_ARM_MIN_CLOSES": 3,
    "MEMECOIN_EXIT_ARRIVAL_WINDOW_S": 60.0,
    "MEMECOIN_EXIT_DUST_ATOMIC": 0,
    "MEMECOIN_EXIT_MIN_STRUCTURE_POINTS": 5,
    "MEMECOIN_EXIT_PIVOT_CONFIRM": 2,
    "MEMECOIN_EXIT_STALE_S": 180.0,
    "MEMECOIN_EXIT_VOL_WINDOW_S": 300.0,
    "MEMECOIN_ROUTE_DEGRADATION_PCT": None,     # None disables the degradation check
    "MEMECOIN_STRUCTURE_BREAK_WINDOW_S": None,  # None disables the persistence window
    "MEMECOIN_TICKSTREAM_SKIP_DISARMED_CHAINS": "1",
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_knob_is_defined_on_the_settings_module(name):
    assert hasattr(cfg, name), (
        f"{name} is read via getattr(cfg, ...) but never defined, so .env cannot reach it")


@pytest.mark.parametrize("name,expected", sorted(EXPECTED.items()))
def test_the_default_matches_the_fallback_it_replaced(name, expected):
    """A no-op today. If one of these drifts, funded behaviour changed silently."""
    assert getattr(cfg, name) == expected


def test_multihop_bridges_is_a_per_chain_mapping():
    """Structured config, so it cannot come from one scalar env read. Empty default = the rung
    finds no bridge and declines, which is what it did while the name was inert."""
    assert hasattr(cfg, "MEMECOIN_EVM_MULTIHOP_BRIDGES")
    assert isinstance(cfg.MEMECOIN_EVM_MULTIHOP_BRIDGES, dict)
    assert cfg.MEMECOIN_EVM_MULTIHOP_BRIDGES.get("4663", ()) == ()


def test_no_OTHER_memecoin_settings_name_is_read_through_an_undefined_fallback():
    """The guard that stops this class recurring.

    Scoped to files that actually import the memecoin settings module. An earlier sweep of mine
    flagged HL_REAL_ALT_SHORT_ONLY as inert by checking it against THIS module, when its call site
    reads `sniper.settings.hl_real`, where it is defined. Checking a name against the wrong module
    manufactures a false positive, so the scope matters as much as the pattern.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    imports = re.compile(r"from sniper\.settings import memecoin as (?:cfg|_cfg)"
                         r"|import sniper\.settings\.memecoin as (?:cfg|_cfg)")
    reads = re.compile(r'getattr\(\s*_?cfg\s*,\s*["\'](MEMECOIN_[A-Z0-9_]+)["\']')
    missing = []
    for path in list((root / "sniper").rglob("*.py")) + list((root / "scripts").rglob("*.py")):
        text = path.read_text(errors="ignore")
        if not imports.search(text):
            continue
        for name in set(reads.findall(text)):
            if not hasattr(cfg, name):
                missing.append(f"{name} <- {path.relative_to(root)}")
    assert not missing, "read through a fallback the settings module never defines:\n  " + \
        "\n  ".join(sorted(missing))
