"""Daemon auto-triage: cheap no-op without an LLM key; runs + persists when configured."""
import sqlite3

from sniper.persistence import db_memecoin as m
from scripts.memecoin_discovery_worker import _maybe_triage

NOW = "2026-07-19T12:00:00+00:00"


def _seed(c):
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "w1", "W1")
    m.insert_feed_observation_v2(c, {
        "upstream_event_id": "e1", "raw_type": "large_buy", "normalized_side": "buy",
        "source_user_id": "w1", "source_handle": "w1", "network_id": "solana", "mint": "COIN",
        "ticker": "CN", "their_price": 1.0, "their_market_cap": 1.0, "their_size_usd": 1.0,
        "source_ts_raw": NOW, "source_ts": NOW, "observed_at": NOW, "lag_s": 0.0, "raw_payload": {}},
        was_curated=True, was_real_approved=False)
    c.commit()


def test_maybe_triage_noop_without_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    c = sqlite3.connect(":memory:"); _seed(c)
    # (count, state) since the daemon started reporting WHY a pass produced nothing: a
    # permanently-disabled feature must read as `unconfigured`, not as a stream of
    # zero-verdict passes indistinguishable from "the model had nothing to say".
    assert _maybe_triage(c, NOW) == (0, "unconfigured")   # no key -> cheap no-op, nothing persisted
    assert m.radar_triage_map(c, now_iso=NOW) == {}


def test_maybe_triage_runs_and_persists_when_configured(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    import sniper.services.memecoin_radar_triage as T
    monkeypatch.setattr(T, "_openai_compatible_llm", lambda sheets: (
        [{"mint": sheets[0]["mint"], "network_id": sheets[0]["network_id"],
          "verdict": "promising", "reason": "stub"}] if sheets else []))
    c = sqlite3.connect(":memory:"); _seed(c)
    n, state = _maybe_triage(c, NOW)
    assert n == 1
    assert state != "unconfigured"
    tm = m.radar_triage_map(c, now_iso=NOW)
    assert tm[m.accounting_mark_key("solana", "COIN")]["verdict"] == "promising"
