"""The paper mirror follows the funded one: a trim is not an exit.

Since 2026-09-24T06:46:19Z `MEMECOIN_REAL_MIRROR_SIZE_CHAINS=solana`, so funded mirrors a source
sell only when it is at least `MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION` (0.30) of the wallet's
holding; a smaller sell is a trim and the position keeps its rails. The paper mirror was
UNCONDITIONAL — any sell closed the whole paper position — so every paper mirror on that chain was
an exit funded would not have taken.

It resolves through funded's own `source_sell_share`, never a copy of its arithmetic, so a rule
change moves both sides together. UNKNOWN HOLDS, as on the funded side: a sell that cannot be sized
is not evidence that the trader left.
"""
import json
import sqlite3

import pytest

from sniper.feeds.fomo_feed import CopyEvent
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct


NOW = "2026-09-24T07:30:00+00:00"
FROZEN = '{"params": {"mirror_size_min_fraction": 0.3}}'


@pytest.fixture(autouse=True)
def _solana_is_size_scoped(monkeypatch):
    """⚠️ THE CHAIN SET COMES FROM THE PROCESS ENVIRONMENT, AND A TEST HAS NO `.env`.
    `MEMECOIN_REAL_MIRROR_SIZE_CHAINS` is built at import from `os.getenv`, so under pytest it is
    EMPTY and every one of these tests would return "no size rule on this chain" — passing or
    failing for a reason that has nothing to do with the fraction. Pinned here explicitly, the same
    way conftest pins the scoring knobs."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_CHAINS", frozenset({"solana"}))


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _pos(**over):
    # `experiment_status` is load-bearing: `paper_policy_value` returns the FALLBACK for any row
    # whose status is not eligible / excluded_fill / operator_modified, so without it the frozen
    # fraction reads as 0 and the check declines for a reason unrelated to the assertion.
    base = {"id": 1, "network_id": "solana", "mint": "MINT", "source_user_id": "u1",
            "opened_at": "2026-09-24T07:00:00+00:00", "experiment_status": "eligible",
            "assigned_policy_json": FROZEN}
    base.update(over)
    return base


def _ev(size=100.0):
    return CopyEvent("u1", "alice", "MINT", "TICK", "sell", 1.0, 1e6, size, False,
                     "sell-1", NOW, NOW, 0.1, network_id="solana")


def test_the_fixture_reaches_the_frozen_fraction():
    """Guards the trap above; if this fails every other test here proves nothing."""
    from sniper.services.memecoin_policy import paper_policy_value
    assert paper_policy_value(_pos(), "mirror_size_min_fraction", 0.0) == 0.3


def test_a_trim_below_the_fraction_holds(monkeypatch):
    """⚠️ THE REACH VALUE IS THE FRACTION: 0.10 against a 0.30 minimum. At or above 0.30 the same
    fixture mirrors, so a test using 0.5 would pass with no size condition at all."""
    monkeypatch.setattr("sniper.services.memecoin_real_runner.source_sell_share",
                        lambda *a, **k: (0.10, {}))
    assert ct._mirror_size_holds(_conn(), _ev(), _pos(), NOW, "funded_clone") is True


def test_a_sell_at_or_above_the_fraction_mirrors(monkeypatch):
    monkeypatch.setattr("sniper.services.memecoin_real_runner.source_sell_share",
                        lambda *a, **k: (0.30, {}))
    assert ct._mirror_size_holds(_conn(), _ev(), _pos(), NOW, "funded_clone") is False


def test_an_unsizeable_sell_holds_as_it_does_on_the_funded_side(monkeypatch):
    monkeypatch.setattr("sniper.services.memecoin_real_runner.source_sell_share",
                        lambda *a, **k: (None, {}))
    assert ct._mirror_size_holds(_conn(), _ev(), _pos(), NOW, "funded_clone") is True


def test_a_chain_outside_the_knob_still_mirrors_unconditionally(monkeypatch):
    """Funded only applies the size rule on MEMECOIN_REAL_MIRROR_SIZE_CHAINS; paper must match."""
    called = []
    monkeypatch.setattr("sniper.services.memecoin_real_runner.source_sell_share",
                        lambda *a, **k: called.append(1) or (0.01, {}))
    assert ct._mirror_size_holds(_conn(), _ev(), _pos(network_id="4663"), NOW, "funded_clone") is False
    assert called == [], "the share is not even computed off those chains"


def test_the_witness_is_persisted_under_its_own_reason(monkeypatch):
    monkeypatch.setattr("sniper.services.memecoin_real_runner.source_sell_share",
                        lambda *a, **k: (0.10, {}))
    conn = _conn()
    ct._mirror_size_holds(conn, _ev(), _pos(), NOW, "funded_clone")
    row = conn.execute("SELECT reason, allowed, detail FROM memecoin_exit_trigger_audit").fetchone()
    assert row["reason"] == "paper_mirror" and row["allowed"] == 0
    detail = json.loads(row["detail"])
    assert detail["code"] == "size_hold" and detail["anchor"] == "opened_at"
    assert detail["sold_fraction"] == 0.1 and detail["min_fraction"] == 0.3


def test_a_broken_size_check_holds_and_records_unknown(monkeypatch):
    """A measurement fault must not become an exit decision."""
    def boom(*a, **k):
        raise RuntimeError("no trades")
    monkeypatch.setattr("sniper.services.memecoin_real_runner.source_sell_share", boom)
    conn = _conn()
    assert ct._mirror_size_holds(conn, _ev(), _pos(), NOW, "funded_clone") is True
    row = conn.execute("SELECT allowed, detail FROM memecoin_exit_trigger_audit").fetchone()
    assert row["allowed"] == 0
    detail = json.loads(row["detail"])
    assert detail["code"] == "size_unknown"
    assert detail["error_type"] == "RuntimeError"
    assert detail["sold_fraction"] is None
    assert detail["mirror_policy_source"] == "frozen_paper_policy"


@pytest.mark.parametrize("failure_site", ["trades", "sizing", "sizing_and_audit"])
def test_size_failure_does_not_claim_or_quote_a_mirror_close(monkeypatch, failure_site):
    def boom(*a, **k):
        raise RuntimeError("unavailable")

    def forbidden(*a, **k):
        pytest.fail("unknown sizing reached claim or quote")

    if failure_site == "trades":
        monkeypatch.setattr(m, "source_trades_for_coin", boom)
    else:
        monkeypatch.setattr("sniper.services.memecoin_real_runner.source_sell_share", boom)
    if failure_site == "sizing_and_audit":
        monkeypatch.setattr(m, "record_exit_trigger_audit", boom)
    monkeypatch.setattr(m, "claim_close", forbidden)
    assert ct._mirror_close_arm(
        _conn(), _ev(), NOW, "funded_clone", pos=_pos(), quote_fn=forbidden,
    ) == "mirror_size_hold"


# --- the entry chase cap: paper was STRICTER than funded, not looser ---------------------------


def test_the_paper_chase_approximation_uses_funded_band_settings(monkeypatch):
    """Measured 2026-09-24: paper 15.0, funded 30.0, funded nano 35.0. Paper was refusing a class
    funded admits, which censored those origins out of every refusal family."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_4663_OVER_NANO_QUOTE_CHASE_MAX_PCT", 0.0)
    # ⚠️ THE REACH VALUE IS THE MARKET CAP: it decides the band, and the band decides the cap.
    # 30_000 is nano (< 50k) and 300_000 is not; a fixture on one side alone cannot tell the two
    # branches apart.
    assert ct.paper_chase_cap_pct(30_000.0, "solana") == 35.0
    assert ct.paper_chase_cap_pct(300_000.0, "solana") == 30.0


def test_the_robinhood_above_nano_ceiling_still_applies(monkeypatch):
    """Resolved through funded's own `over_nano_chase_cap`, never a copy of its rule."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_4663_OVER_NANO_QUOTE_CHASE_MAX_PCT", 12.0)
    assert ct.paper_chase_cap_pct(300_000.0, "4663") == 12.0
    assert ct.paper_chase_cap_pct(300_000.0, "solana") == 30.0, "the ceiling is 4663-only"
