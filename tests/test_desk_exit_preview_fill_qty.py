"""Rails blind after every desk buy (2026-10-23, positions 74130..74142).

Right after our own fill the owner token index answered an empty list, the exit preview had no
balance to quote, and stop / trail / catastrophe were blind for 2-10 s (QUOTE-INFRA and RAILS-BLIND
pages on pos 74142). Inside the window after a confirmed desk fill, before any exit or de-risk, the
preview sizes the bag from that fill. Outside it the empty index stays infrastructure.
"""
from datetime import datetime, timedelta, timezone

import pytest
from solders.keypair import Keypair

from sniper.analysis.memecoin_quote_failures import QuoteFailure
from sniper.execution import solana_real_seams as S
from sniper.settings import memecoin as cfg

MINT = "A3WqngzJwDfeuhd7vRtw9w367pGRF4tH4KVdmh34pump"


def _position(age_s=2.0, **over):
    opened = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    pos = {"id": 74142, "mint": MINT, "signal_source": "desk", "entry_fill_source": "chain",
           "entry_notional": 25.0, "entry_price": 3.11774551924461e-05,
           "opened_recorded_at": opened.isoformat()}
    pos.update(over)
    return pos


@pytest.fixture
def preview(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_READ_RPC_URL", "")
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_PREVIEW_FILL_QTY_S", 30.0)
    monkeypatch.setattr(S, "_sleep", lambda _s: None)
    asked = {}
    monkeypatch.setattr(S, "jupiter_exit_quote",
                        lambda mint, atomic, slip, http=None: asked.update(atomic=atomic) or {"outAmount": "22500000"})

    def empty_index(method, url, headers=None, body=None):
        import json
        if json.loads(body)["method"] == "getMultipleAccounts":     # our derived ATAs are not visible either
            return 200, {"result": {"context": {"slot": 1}, "value": [None, None]}}, None
        return 200, {"result": {"value": []}}, None
    ex = S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://custody", http=empty_index,
                                      usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0)
    return ex["preview_exit_fn"], asked


def test_fresh_desk_fill_is_quoted_while_the_index_is_empty(preview):
    fn, asked = preview
    res = fn(_position())
    assert res.failure is None and res.preview is not None
    assert asked["atomic"] == 801861468349, "the confirmed fill's raw atoms (pos 74142) sized the quote"
    assert res.provider_id == "solana:exact_quote(entry_fill_qty)"


@pytest.mark.parametrize("pos", [
    _position(age_s=31.0),                              # window passed: a real zero is possible
    _position(signal_source="global_feed"),            # copy lane: unchanged
    _position(entry_fill_source="quote"),              # fill not proven on chain
    _position(mint="So11111111111111111111111111111111111111112"),   # not a pump.fun mint
    _position(derisk_sig="sig"),                       # a partial sell happened: fill qty is stale
    _position(close_submitted_at="2026-10-23T13:18:51+00:00"),
    _position(entry_price=0.0),
])
def test_outside_the_fresh_fill_the_empty_index_stays_infra(preview, pos):
    fn, asked = preview
    res = fn(pos)
    assert res.failure is QuoteFailure.MALFORMED_RESPONSE
    assert "atomic" not in asked


def test_knob_zero_restores_the_old_behaviour(preview, monkeypatch):
    fn, asked = preview
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_PREVIEW_FILL_QTY_S", 0.0)
    assert fn(_position()).failure is QuoteFailure.MALFORMED_RESPONSE
