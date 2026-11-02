"""desk-risk-live-v4 (2026-10-23): RISK's pnl_pct_exact is the exact whole-bag exit quote (memecoin_route_series,
seam exit_decision) over entry notional. v3 divided memecoin_position_marks.executable_exit_value -- an alias of
`mid`, a per-token PRICE -- by the notional and read -99.99% (position 74118, three cycles)."""
import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_desk_risk as R

spec = importlib.util.spec_from_file_location("rs", Path(__file__).resolve().parents[1] / "scripts/memecoin_desk_risk_shadow.py")
RS = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RS)

T0 = datetime(2026, 10, 23, 8, 36, 19, tzinfo=timezone.utc)
POS = {"id": 74135, "entry_notional": 25.0, "opened_at": T0.isoformat()}


@pytest.fixture
def ledger():
    c = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(c)
    # the v3 trap: a fresh fastlane mark whose executable_exit_value is the per-token price
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, executable_exit_value, source) "
              "VALUES('solana','M',?,3.45e-5,3.45e-5,'fastlane_sol_curve')", ((T0 + timedelta(seconds=9)).isoformat(),))
    return c


def _route(c, at, usd):
    c.execute("INSERT INTO memecoin_route_series(position_id, mint, network_id, seam, observed_at, expected_usdc) "
              "VALUES(74135,'M','solana','exit_decision',?,?)", ((T0 + timedelta(seconds=at)).isoformat(), usd))


def _pnl(c, now_s):
    mark = RS.exact_mark(c, 74135)
    now = (T0 + timedelta(seconds=now_s)).timestamp()
    f = R.facts(POS, {}, mark=mark, bars=[], boundary="cycle", now=now)
    return f["pnl_pct_exact"], R.pnl_basis(POS, mark, now=now)


def test_exact_route_quote_is_the_pnl_never_the_per_token_price(ledger):
    _route(ledger, 8, 24.0)
    _route(ledger, 10, 27.5)                           # newest wins
    assert _pnl(ledger, 11) == (10.0, "exact")


def test_no_route_quote_is_null_with_its_reason_never_minus_100(ledger):
    assert _pnl(ledger, 11) == (None, "no_exact_quote")


def test_a_zero_quote_is_unknown_not_a_known_total_loss(ledger):
    _route(ledger, 10, 0.0)
    assert _pnl(ledger, 11) == (None, "zero_exact_quote")


def test_a_stale_quote_is_null(ledger):
    _route(ledger, 10, 27.5)
    assert _pnl(ledger, 10 + R.MARK_MAX_AGE_S + 1) == (None, "stale_exact_quote")


def test_no_notional_is_null():
    assert R.pnl_basis({"entry_notional": None}, {"executable_exit_value": 1.0, "sweep_ts": T0.isoformat()},
                       now=T0.timestamp()) == "no_entry_notional"
