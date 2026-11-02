"""An old dip row cannot retain average-price admission after the market-only cutover."""
import json

import pytest

from sniper.feeds.fomo_feed import CopyEvent
from sniper.services import memecoin_dip_retry as dip
from sniper.settings import memecoin as cfg
from test_memecoin_dip_retry import (_Quotes, _cfg, _evm_watch, _seed, _seed_evm, _watch)


@pytest.mark.parametrize("network", ["solana", "4663"])
def test_old_dip_rows_proceed_as_unknown_without_more_quotes(tmp_path, _cfg, monkeypatch, network):
    from sniper.execution import evm_v4_quote
    monkeypatch.setattr(evm_v4_quote, "pinned_buy_out", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", ("solana", "4663"))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_MAX_PREMIUM_PCT", 10.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    c = _seed(tmp_path) if network == "solana" else _seed_evm(tmp_path)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True)
    quotes, asked = _Quotes([1]), []
    watch = _watch(tmp_path, quotes) if network == "solana" else _evm_watch(tmp_path, asked)
    watch.tick()
    seen = []

    def attempt(_conn, event):
        seen.append(getattr(event, "dip_cap", None))
        return {"status": "filled", "position_id": 1}

    tally = dip.run_hits(c, attempt, "2026-09-30T12:00:05+00:00", watch=watch,
        event_from_json=lambda value: CopyEvent(**json.loads(value)))
    assert tally["hits"] == tally["filled"] == 1
    assert seen == [None] and not quotes.calls and not asked
    stat = next(iter(watch.stats.values()))
    assert stat["quotes"] == 0 and stat["unknown"] == 1
    assert stat["series"][-1]["state"] == "unknown"
    log = c.execute("SELECT * FROM memecoin_dip_retry_log").fetchone()
    assert log["cap_pct"] is None and log["watch_premium_pct"] is None
    assert log["cap_source"] == "intervening_market_unknown"
    c.close()


@pytest.mark.parametrize("network", ["solana", "4663"])
def test_market_mode_never_enrolls_an_average_quote_as_a_market_refusal(monkeypatch, network):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True)
    result = {"reason": "copy price minimum is +40.5% above source fill (cap 35.0%)",
              "quote_price": 1.4, "source_drift_pct": 40.0, "quote_chase_pct": 40.0,
              "gate_reference_price": 1.0, "gate_cap_pct": 35.0, "gate_premium_pct": 40.0,
              "dip_route": {"buy_atomic": "100", "min_atomic": "90"}}
    assert dip._reference_and_cap(network, result) is None
