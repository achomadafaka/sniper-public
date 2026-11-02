import sqlite3
from sniper.persistence import db_memecoin as m
import scripts.analyze_fomo_accounts as cli


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u1", "alice", source="follow")
    m.add_wallet(c, "u2", "bob", source="manual")
    return c


def test_resolve_targets_all_returns_curated():
    c = _conn()
    targets = cli.resolve_targets(c, uids=None, handles=None, all_curated=True)
    assert {t[0] for t in targets} == {"u1", "u2"}


def test_resolve_targets_candidates_pulls_top_discovery():
    c = _conn()

    class _Ev:
        source_user_id = "cand1"; source_handle = "whale"; mint = "M"; ticker = "T"
        their_size_usd = 9000.0; created_at = "2026-07-06T10:00:00Z"

    m.upsert_candidate(c, _Ev())
    targets = cli.resolve_targets(c, candidates=5)
    assert ("cand1", "whale") in targets


def test_resolve_targets_handle_uses_db_lookup():
    c = _conn()
    targets = cli.resolve_targets(c, uids=None, handles=["alice"], all_curated=False)
    assert targets == [("u1", "alice")]


def test_run_analyzes_and_persists():
    c = _conn()

    def fake_analyze(auth, uid, **kw):
        return {"source_user_id": uid, "handle": "x", "realized_pnl_usd": 10.0,
                "chain_mix": "{}", "provider_mix": "{}", "metrics_json": "{}"}

    rows = cli.run(c, None, [("u1", "alice")], now_iso="2026-07-06T00:00:00Z",
                   sleep_s=0.0, analyze=fake_analyze)
    assert len(rows) == 1
    assert m.get_trader_analytics(c, "u1")["realized_pnl_usd"] == 10.0


def test_run_retries_on_429_then_succeeds():
    c = _conn()
    calls = {"u1": 0}

    def flaky(auth, uid, **kw):
        calls[uid] += 1
        if calls[uid] < 3:
            raise RuntimeError("HTTP Error 429: Too Many Requests")
        return {"source_user_id": uid, "realized_pnl_usd": 7.0,
                "chain_mix": "{}", "provider_mix": "{}", "metrics_json": "{}"}

    rows = cli.run(c, None, [("u1", "alice")], now_iso="2026-07-06T00:00:00Z",
                   sleep_s=0.0, analyze=flaky, sleep_fn=lambda s: None)
    assert len(rows) == 1
    assert calls["u1"] == 3          # failed twice with 429, succeeded on 3rd
    assert m.get_trader_analytics(c, "u1")["realized_pnl_usd"] == 7.0


def test_run_gives_up_after_max_retries_on_persistent_429():
    c = _conn()

    def always_429(auth, uid, **kw):
        raise RuntimeError("HTTP Error 429")

    rows = cli.run(c, None, [("u1", "alice")], now_iso="2026-07-06T00:00:00Z",
                   sleep_s=0.0, analyze=always_429, sleep_fn=lambda s: None, max_retries=3)
    assert rows == []                # dropped after exhausting retries, no crash


def test_run_survives_one_bad_trader():
    c = _conn()

    def flaky(auth, uid, **kw):
        if uid == "u1":
            raise RuntimeError("boom")
        return {"source_user_id": uid, "realized_pnl_usd": 5.0,
                "chain_mix": "{}", "provider_mix": "{}", "metrics_json": "{}"}

    rows = cli.run(c, None, [("u1", "alice"), ("u2", "bob")],
                   now_iso="2026-07-06T00:00:00Z", sleep_s=0.0, analyze=flaky)
    assert len(rows) == 1                       # u1 failed, u2 survived
    assert m.get_trader_analytics(c, "u2") is not None
