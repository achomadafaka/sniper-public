"""N18 (Codex follow-up review 2026-10-07, source 1fce36294): a shared RH route event carries the
lane's pilot clip as `entry_size_cap_usd` in its 4663 context (memecoin_rh_fast_lane._handle_shared),
and the ordinary EVM adapter dropped it: `evm_real_entry` got size_cap_usd=None, so automatic
bankroll sizing could reserve more than the pilot clip (Codex: $250 against a $175 pilot).

The adapter now merges the context cap DOWNWARD into the caller's cap before any read, and every
`evm_real_entry` call it makes (first attempt, provider retry, route-capacity retry) carries it.
That the reservation honours a forwarded cap is test_memecoin_evm_leg's
test_a_hard_size_cap_bounds_the_reservation_and_the_executor.

Masked on 2026-10-07: MEMECOIN_EVM_TIER_CLIP_USD_4663=175,175,175 equals the pilot clip 175.
"""
import math
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_leg as evm_leg
from sniper.services import memecoin_real_runner as R


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _entry(monkeypatch, results, *, cap="absent", reads=None):
    """The adapter over a fake leg. `results` are what successive evm_real_entry calls return."""
    calls = []
    reads = reads if reads is not None else []

    def fake_entry(*_a, **kw):
        calls.append(kw)
        return results[min(len(calls), len(results)) - 1]
    monkeypatch.setattr(evm_leg, "evm_real_entry", fake_entry)
    ctx = {"usdc_fn": lambda: reads.append("usdc") or 1000.0,
           "native_fn": lambda: reads.append("native") or 1.0,
           "entry_quote_probe_fn": lambda _mint, clip: None,
           "token_safety_fn": lambda _mint: (True, "ok"),
           "execute_fn": lambda *_a, **_k: {"status": "filled"}}
    if cap != "absent":
        ctx["entry_size_cap_usd"] = cap
    return R._make_evm_entry_fn({"4663": ctx}), calls


def _call(entry, **kw):
    ev = SimpleNamespace(mint="0xpilot", source_user_id="u1", network_id="4663",
                         signal_source="rh_fast_lane", fomo_event_id="rhfast:0xabc")
    return entry(_conn(), ev, route="evm:4663", day="2026-10-07", now_iso="2026-10-07T08:00:00+00:00",
                 trade_size=250.0, **kw)


def test_a_shared_route_reservation_carries_the_pilot_clip(monkeypatch):
    entry, calls = _entry(monkeypatch, [{"status": "filled"}], cap=175.0)
    assert _call(entry) == {"status": "filled"}
    assert calls[0]["size_cap_usd"] == 175.0


def test_a_tighter_caller_cap_stands(monkeypatch):
    entry, calls = _entry(monkeypatch, [{"status": "filled"}], cap=175.0)
    _call(entry, size_cap_usd=50.0)
    assert calls[0]["size_cap_usd"] == 50.0


@pytest.mark.parametrize("caller", [math.nan, 0.0, -5.0])
def test_an_unusable_caller_cap_never_widens_past_the_pilot_clip(monkeypatch, caller):
    """memecoin_evm_risk ignores a caller cap that is not > 0 (NaN > 0 is False): uncapped."""
    entry, calls = _entry(monkeypatch, [{"status": "filled"}], cap=175.0)
    _call(entry, size_cap_usd=caller)
    assert calls[0]["size_cap_usd"] == 175.0


@pytest.mark.parametrize("cap", [0, -1.0, math.nan, math.inf, True, "175", None])
def test_an_unusable_pilot_clip_refuses_before_any_read(monkeypatch, cap):
    """An unknown pilot clip is not an uncapped one. A present key that is not a finite positive
    number refuses before the balance read and before the leg."""
    reads = []
    entry, calls = _entry(monkeypatch, [{"status": "filled"}], cap=cap, reads=reads)
    assert _call(entry) == {"status": "rejected", "reason": "entry size cap unavailable"}
    assert calls == [] and reads == []


def test_an_ordinary_context_is_unchanged(monkeypatch):
    entry, calls = _entry(monkeypatch, [{"status": "filled"}])
    _call(entry)
    assert calls[0]["size_cap_usd"] is None
    entry, calls = _entry(monkeypatch, [{"status": "filled"}])
    _call(entry, size_cap_usd=50.0)
    assert calls[0]["size_cap_usd"] == 50.0


def test_the_provider_retry_carries_the_pilot_clip(monkeypatch):
    entry, calls = _entry(monkeypatch, [{"status": "aborted", "quote_miss_code": "provider_timeout"},
                                        {"status": "filled"}], cap=175.0)
    assert _call(entry) == {"status": "filled"}
    assert [c["size_cap_usd"] for c in calls] == [175.0, 175.0]


def test_the_route_capacity_retry_carries_the_pilot_clip(monkeypatch):
    entry, calls = _entry(monkeypatch, [{"status": "aborted", "quote_miss_code": "route_capacity",
                                         "reason": "no quote (route_capacity)",
                                         "approved_clip_usd": 175.0},
                                        {"status": "filled", "pending_id": 2}], cap=175.0)
    assert _call(entry) == {"status": "filled", "pending_id": 2}
    assert len(calls) == 2 and [c["size_cap_usd"] for c in calls] == [175.0, 175.0]
    assert calls[1]["capacity_clip"] < 175.0
