"""Paper step 3 (spec 2026-10-08 paper path without DexScreener, Gamma PASS f79ad24e3): a paper
entry takes its market facts from the funded decision on the same origin, not from a second GET.

- The funded attempt's classification facts land under features_json["funded_facts"]. The
  DexScreener payload's keys are ABSENT on new rows, so a reader that does not know the change sees
  unknown, never another basis under the old name.
- No funded attempt means no funded_facts. A DexScreener fill-in is never used.
- entry_liquidity and entry_token_age_s are NULL: our liquidity and the pair age are not the
  provider's quantities.
- entry_mcap is their_market_cap, then the funded effective_mcap (its basis rides in funded_facts),
  then the feed resolver.
- For one day the provider payload is still fetched, under features_json["provider_shadow"] only,
  so Gamma can compare the paper policy band on both values before the GET is dropped.
"""
import json
import sqlite3

import pytest

from sniper.feeds.fomo_feed import CopyEvent
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct

ARMS = ["funded_clone"]
MID = 7.128e-05
PROVIDER = {"price": MID, "liquidity": 21974.46, "market_cap": 61_000.0, "pair_address": "PAIR",
            "volume_m5": 3000.0, "volume_h1": 9000.0, "pair_created_at": 1_790_000_000_000,
            "symbol": "DEX", "chain": "solana"}
FACTS = {"effective_mcap": 50_000.0, "mcap_basis": "onchain_source_fill", "liquidity": 12_000.0,
         "liquidity_source": "implied_source_trade:wsol", "pair_age_s": 300.0, "volume_m5": 900.0,
         "volume_h1": 5000.0, "volume_mcap": 48_000.0}
PROVIDER_NAMES = ("price", "market_cap", "pair_address", "volume_m5", "volume_h1",
                  "pair_created_at", "symbol", "dex_market_cap")


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u1", "alice")
    return c


def _ev(eid="origin-1", ticker="MANLETS", mcap=0.0):
    return CopyEvent("u1", "alice", "MANLETSMINT", ticker, "buy", 8.9e-05, mcap, 250.0, True,
                     eid, "t", "t", 0.3, network_id="solana")


def _attempt(c, eid="origin-1", ticker="FUNDEDTICK", facts=FACTS):
    m.start_entry_attempt(c, attempt_id="att-1", decision_started_at="2026-10-09T00:00:00+00:00",
                          origin_event_id=eid, network_id="solana", mint="MANLETSMINT",
                          ticker=ticker)
    payload, digest = m.canonical_funded_json(
        {"classification_version": 1, "coin_band": "micro", "facts": facts})
    m.set_entry_attempt_classification(c, "att-1", classification_json=payload,
                                       classification_hash=digest, classification_version=1)


@pytest.fixture
def stats(monkeypatch):
    calls = []

    def fn(*a, **k):
        calls.append(a)
        return dict(PROVIDER)
    return calls, fn


def _run(c, ev, stats_fn):
    return ct.copy_event_to_arms(
        c, ev, 100.0, "2026-10-09T00:00:01+00:00", arms=ARMS, fill_fn=ct.honest_fill,
        stats_fn=stats_fn, quote_fn=lambda mint, network_id, size_usd: (9.1e-05, "route_quote"))


def _features(c):
    row = c.execute("SELECT features_json FROM memecoin_entry_snapshots ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()
    return json.loads(row[0])


def _position(c):
    return dict(c.execute("SELECT * FROM memecoin_positions ORDER BY id DESC LIMIT 1").fetchone())


def test_a_funded_attempt_gives_the_entry_its_facts_and_no_provider_names(stats, monkeypatch):
    monkeypatch.setattr(ct, "PROVIDER_SHADOW", False)
    calls, fn = stats
    c = _conn()
    _attempt(c)
    _run(c, _ev(), fn)
    assert calls == []
    f = _features(c)
    assert f["funded_facts"] == {"attempt_id": "att-1", "coin_band": "micro", **FACTS}
    assert not [k for k in PROVIDER_NAMES if k in f]
    assert f["liquidity"] is None and f["token_age_s"] is None
    pos = _position(c)
    assert pos["entry_liquidity"] is None and pos["entry_token_age_s"] is None
    assert pos["entry_mcap"] == pytest.approx(50_000.0)


def test_the_event_mcap_still_comes_first():
    c = _conn()
    _attempt(c)
    ct.PROVIDER_SHADOW, keep = False, ct.PROVIDER_SHADOW
    try:
        _run(c, _ev(mcap=70_000.0), None)
    finally:
        ct.PROVIDER_SHADOW = keep
    assert _position(c)["entry_mcap"] == pytest.approx(70_000.0)


def test_no_funded_attempt_means_no_facts_and_no_provider_fill_in(stats, monkeypatch):
    monkeypatch.setattr(ct, "PROVIDER_SHADOW", False)
    calls, fn = stats
    c = _conn()
    _run(c, _ev(mcap=70_000.0), fn)          # a band needs an mcap; the event carries one here
    assert calls == []
    f = _features(c)
    assert "funded_facts" not in f and not [k for k in PROVIDER_NAMES if k in f]
    assert _position(c)["entry_mcap"] == pytest.approx(70_000.0)


def test_the_provider_shadow_is_namespaced(stats, monkeypatch):
    monkeypatch.setattr(ct, "PROVIDER_SHADOW", True)
    calls, fn = stats
    c = _conn()
    _attempt(c)
    _run(c, _ev(), fn)
    assert len(calls) == 1
    f = _features(c)
    assert f["provider_shadow"]["price"] == MID and f["provider_shadow"]["market_cap"] == 61_000.0
    assert not [k for k in PROVIDER_NAMES if k in f]
    assert _position(c)["entry_mcap"] == pytest.approx(50_000.0)       # the shadow decides nothing


def test_a_symbol_less_event_takes_the_funded_attempts_ticker(monkeypatch):
    monkeypatch.setattr(ct, "PROVIDER_SHADOW", False)
    c = _conn()
    _attempt(c, ticker="FUNDEDTICK")
    _run(c, _ev(ticker=""), None)
    assert _position(c)["ticker"] == "FUNDEDTICK"


def test_the_m5_evidence_reads_the_funded_volumes_not_a_provider_payload(monkeypatch):
    monkeypatch.setattr(ct, "PROVIDER_SHADOW", True)
    seen = []
    monkeypatch.setattr(ct, "_paper_m5_gate", lambda ev, st, mcap, now: seen.append(dict(st)) or {})
    c = _conn()
    _attempt(c)
    _run(c, _ev(), lambda *a, **k: dict(PROVIDER))
    (st,) = seen
    assert st["volume_m5"] == 900.0 and st["volume_h1"] == 5000.0
    assert st["market_cap"] == 48_000.0 and st["pair_age_s"] == 300.0


def test_expired_provider_comparison_does_not_fetch_on_paper_entry(stats):
    calls, fn = stats
    c = _conn()
    _attempt(c)
    _run(c, _ev(), fn)
    assert calls == [], 'the one-day provider comparison must not run indefinitely'
    facts = _features(c)
    assert facts['funded_facts']['effective_mcap'] == 50_000.0
    assert 'provider_shadow' not in facts
