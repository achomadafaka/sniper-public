"""Paper step 2 (spec 2026-10-08 paper path without DexScreener, Gamma PASS f79ad24e3): the paper
price pass prices only the coins that open v0 paper positions hold.

- v1 positions are decided on their own ticks (step 1 writes their marks and path), and the sweep
  decides only v0 positions plus the 14-day clock backstop. With no open v0 position, the pass makes
  no DexScreener call, no GeckoTerminal batch and no blind-liquidity refresh.
- The quote ledger is retired for v1 coins: no row is written for them.
- Real positions were never in this pass (`list_open_positions` defaults to paper).
- The 14-day backstop still closes a v1 position. With no mid it uses `_timecap_no_quote`, i.e.
  the engine's last tick (last_quote_price).
"""
import pytest

from sniper.services import memecoin_paper_ticks as PT
from sniper.services import memecoin_runner as R
from test_memecoin_paper_ticks import MINT, _ledger, _open

OTHER = "0x" + "cd" * 20
NOW = "2026-10-08T07:01:00+00:00"


@pytest.fixture
def batch(monkeypatch):
    calls = []

    def fake(keys, *a, **kw):
        calls.append(sorted(keys))
        return {k: {"price": 1.05, "liquidity": 1e6} for k in keys}

    from sniper.execution import solana_quote
    monkeypatch.setattr(solana_quote, "dexscreener_stats_batch_keyed", fake)
    return calls


def _real(c, m, mint=OTHER):
    return m.open_position(
        c, source_user_id="u9", source_handle="h", mint=mint, ticker="R", entry_price=1.0,
        entry_notional=50.0, entry_fill_source="chain", entry_lag_s=0.0, their_size_usd=0.0,
        opened_at="2026-10-08T07:00:00+00:00", strategy="real", execution_mode="real",
        network_id="4663")


def _ledger_rows(c):
    return c.execute("SELECT COUNT(*) FROM memecoin_quote_ledger").fetchone()[0]


def test_no_open_v0_position_makes_no_provider_call(batch):
    c, m = _ledger()
    _open(c, m)                                    # v1
    _real(c, m)
    gt = []
    R.bracket_pass(c, now_fn=lambda: NOW, normalize=False,
                   evm_price_for=lambda *a: None, gt_batch_fn=lambda keys: gt.append(keys) or {})
    assert batch == [] and gt == []
    assert _ledger_rows(c) == 0


def test_only_the_v0_coins_are_priced(batch):
    c, m = _ledger()
    _open(c, m)                                    # v1 on MINT
    _real(c, m)                                    # real on OTHER
    v0 = _open(c, m, engine=PT.V0, arm="profile_matched_v1")
    c.execute("UPDATE memecoin_positions SET mint=? WHERE id=?", (OTHER.replace("cd", "ee"), v0))
    c.commit()
    R.bracket_pass(c, now_fn=lambda: NOW, normalize=False)
    assert batch == [[("4663", OTHER.replace("cd", "ee"))]]


def test_a_supplemental_own_mark_for_a_v1_coin_writes_no_ledger_row(batch):
    c, m = _ledger()
    _open(c, m)
    R.bracket_pass(c, now_fn=lambda: NOW, normalize=False,
                   supplemental_price_map={("4663", MINT): {"price": 1.1, "source": "fastlane_sol_swap"}})
    assert _ledger_rows(c) == 0


@pytest.mark.parametrize("tick_at,reason", [("2026-10-23T06:59:46+00:00", "max_hold_cap"),
                                            ("2026-10-22T07:00:00+00:00", "max_hold_cap_stale")])
def test_the_14_day_backstop_still_closes_a_v1_position_on_its_last_tick(batch, tick_at, reason):
    c, m = _ledger()
    pid = _open(c, m)
    m.update_position_last_quote(c, pid, 1.07, tick_at)
    R.bracket_pass(c, now_fn=lambda: "2026-10-23T07:00:00+00:00", normalize=False)
    row = dict(c.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert row["is_open"] == 0 and row["exit_reason"] == reason
    assert batch == []
