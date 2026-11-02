"""Guards against host-local-time leaking into UTC comparisons and labels.

The host runs TZ=America/New_York while the operator is in Dublin, so a naive
`datetime.now()` is 4h behind real UTC. Its UTC clock itself is correct (0s skew,
NTP synced) — the bugs here are all naive-vs-aware handling, not a bad clock.

See docs/audits/2026-07-29-intel-geopolitics-end-to-end-audit.md for how the
clock came under review.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Bug 1 — classify_freshness raised TypeError on every dated signal
# ---------------------------------------------------------------------------

def test_classify_freshness_does_not_raise_on_a_dated_signal():
    """`aware - naive` is a TypeError, so this crashed for any entry_anchor_ts
    above the sentinel — i.e. every real signal, in the real-money path."""
    import time

    from sniper.services.real_trader import RealTrader

    now = int(time.time())
    trader = RealTrader.__new__(RealTrader)  # no DB/creds needed for pure classification

    tier, label, hours = trader.classify_freshness({"entry_anchor_ts": now - 3600})
    assert (tier, label) == (1, "fresh")
    assert hours == pytest.approx(1.0, abs=0.2)


def test_classify_freshness_tiers_and_unknown():
    import time

    from sniper.services.real_trader import RealTrader, FRESH_HOURS, RECENT_HOURS

    now = int(time.time())
    trader = RealTrader.__new__(RealTrader)

    assert trader.classify_freshness({"entry_anchor_ts": now - int((FRESH_HOURS + 1) * 3600)})[:2] \
        == (2, "recent")
    assert trader.classify_freshness({"entry_anchor_ts": now - int((RECENT_HOURS + 1) * 3600)})[:2] \
        == (3, "stale")
    # Missing / sentinel timestamps stay unknown rather than being dated from now
    assert trader.classify_freshness({})[:2] == (4, "unknown")
    assert trader.classify_freshness({"entry_anchor_ts": 0})[:2] == (4, "unknown")


def test_classify_freshness_age_is_measured_against_real_utc():
    """A naive fromtimestamp() under TZ=America/New_York would read 4h off.
    Pin the age so a reintroduced naive conversion fails loudly."""
    import time

    from sniper.services.real_trader import RealTrader

    trader = RealTrader.__new__(RealTrader)
    _, _, hours = trader.classify_freshness({"entry_anchor_ts": int(time.time()) - 7200})
    assert hours == pytest.approx(2.0, abs=0.2), f"expected ~2h, got {hours}h"


# ---------------------------------------------------------------------------
# Bug 2 — local time printed with a "UTC" label
# ---------------------------------------------------------------------------

_SRC_DIRS = ("sniper", "scripts", "agents", "bot")
_NOW_LABELLED_UTC = re.compile(r"datetime\.now\(\)\s*\.strftime\([^)]*UTC")


def _python_files():
    for d in _SRC_DIRS:
        for path in (REPO / d).rglob("*.py"):
            if "__pycache__" not in path.parts:
                yield path
    for name in ("brain.py", "db.py"):
        p = REPO / name
        if p.exists():
            yield p


def test_no_naive_now_is_labelled_utc():
    """`datetime.now().strftime('... UTC')` stamps EDT and calls it UTC — 4h wrong,
    and it can name the wrong day near midnight. These strings are fed to analyst
    agents as 'Today is ...', so the error propagates into their reasoning."""
    offenders = []
    for path in _python_files():
        for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if _NOW_LABELLED_UTC.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{i}")
    assert not offenders, "naive local time labelled UTC: " + ", ".join(offenders)


# ---------------------------------------------------------------------------
# Bug 3 — utcnow() is banned by CLAUDE.md (and deprecated since 3.12)
# ---------------------------------------------------------------------------

def test_no_utcnow_calls():
    offenders = []
    for path in _python_files():
        for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            stripped = line.split("#", 1)[0]
            if "utcnow()" in stripped:
                offenders.append(f"{path.relative_to(REPO)}:{i}")
    assert not offenders, (
        "datetime.utcnow() is banned (CLAUDE.md) — use datetime.now(timezone.utc): "
        + ", ".join(offenders)
    )


def test_utcnow_emits_a_deprecation_warning_on_this_interpreter():
    """Documents why the ban exists, so the rule is not cargo-culted."""
    proc = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c",
         "import datetime; datetime.datetime.utcnow()"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    assert "DeprecationWarning" in proc.stderr
