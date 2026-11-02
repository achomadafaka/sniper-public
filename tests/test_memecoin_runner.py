import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_runner as run


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); m.add_wallet(c, "u1", "alice"); return c


def _single_buy_feed():
    return {"responseObject": {"feed": [{
        "id": "e1", "type": "large_buy", "userId": "u1", "tokenAddress": "MINT",
        "createdAt": "2026-07-05T10:00:00Z",
        "body": {"userHandle": "alice", "ticker": "PEPE", "price": 0.005,
                 "marketCap": "1000000", "currentSizeUsd": 3000.0, "isFirstBuy": True},
    }]}}


class _Auth:
    access = "t"
    refresh = "r"
    ca_id = "c"

    def refresh_token(self, http=None):
        return "t"


def _single_buy_http(_method, _url, headers=None, body=None):
    return 200, _single_buy_feed(), "Sun, 05 Jul 2026 10:00:01 GMT"


def test_global_funded_hook_runs_before_paper_fanout(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "sniper.services.memecoin_copy_trader.copy_event_to_arms",
        lambda *_args, **_kwargs: calls.append("paper") or {"control": "ignored"},
    )

    run.run_once(
        _conn(), _Auth(), 100.0, http=_single_buy_http,
        real_hook=lambda *_args: calls.append("funded") or {"status": "filled"},
    )

    assert calls == ["funded", "paper"]


def test_global_paper_retry_does_not_repeat_the_funded_hook(monkeypatch):
    c = _conn()
    calls = []
    attempts = [0]

    def paper(*_args, **_kwargs):
        calls.append("paper")
        attempts[0] += 1
        if attempts[0] == 1:
            raise RuntimeError("injected paper failure")
        return {"control": "ignored"}

    monkeypatch.setattr(
        "sniper.services.memecoin_copy_trader.copy_event_to_arms", paper)
    real_hook = lambda *_args: calls.append("funded") or {"status": "filled"}

    run.run_once(c, _Auth(), 100.0, http=_single_buy_http, real_hook=real_hook)
    row = c.execute(
        "SELECT action FROM memecoin_events WHERE fomo_event_id='e1'").fetchone()
    assert row is not None and row["action"] == "paper_failed"

    run.run_once(c, _Auth(), 100.0, http=_single_buy_http, real_hook=real_hook)
    assert calls == ["funded", "paper", "paper"]


def test_run_once_opens_for_curated_only(monkeypatch):
    c = _conn()
    feed = {"responseObject": {"feed": [
        {"id": "e1", "type": "large_buy", "userId": "u1", "tokenAddress": "MINT",
         "createdAt": "2026-07-05T10:00:00Z",
         "body": {"userHandle": "alice", "ticker": "PEPE", "price": 0.005,
                  "marketCap": "1000000", "currentSizeUsd": 3000.0, "isFirstBuy": True}},
        {"id": "e2", "type": "large_buy", "userId": "u9", "tokenAddress": "OTHER",
         "createdAt": "2026-07-05T10:00:00Z",
         "body": {"userHandle": "stranger", "ticker": "X", "price": 1.0,
                  "marketCap": "1", "currentSizeUsd": 1.0, "isFirstBuy": True}},
        {"id": "e3", "type": "new_token_listing", "tokenAddress": "LISTED",
         "createdAt": "2026-07-05T10:00:00Z", "body": {"ticker": "NEW"}},
    ]}}

    def http(method, url, headers=None, body=None):
        return 200, feed, "Sun, 05 Jul 2026 10:00:01 GMT"

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"
        def refresh_token(self, http=None): return "t"

    from sniper.settings.memecoin import DEFAULT_MEMECOIN_ARMS
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ARMS", DEFAULT_MEMECOIN_ARMS)
    res = run.run_once(c, _Auth(), size_usd=100.0, http=http,
                       fill_fn=lambda *a, **k: (0.005, "jupiter"))
    assert res["opened"] == 1                       # ONE unique event opened (counter is per-event, not per-arm)
    assert c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1").fetchone()[0] == 8
    assert m.get_open_position(c, "u1", "MINT") is not None          # control arm
    assert m.get_open_position(c, "u1", "MINT", "momentum_a") is not None
    assert m.get_open_position(c, "u1", "MINT", "scalp_c") is not None
    assert m.get_open_position(c, "u1", "MINT", "scalp_c2") is not None
    assert m.get_open_position(c, "u1", "MINT", "peak_lock_e") is not None
    assert m.get_open_position(c, "u9", "OTHER") is None
    raw = [dict(r) for r in c.execute(
        "SELECT upstream_event_id, raw_type, was_curated FROM memecoin_feed_observations_v2 "
        "ORDER BY upstream_event_id",
    )]
    assert [(r["upstream_event_id"], r["raw_type"], r["was_curated"]) for r in raw] == [
        ("e1", "large_buy", 1), ("e2", "large_buy", 0), ("e3", "new_token_listing", 0),
    ]


def test_candidate_hook_is_called_after_v2_capture_and_cannot_break_copy():
    c = _conn()
    feed = {"responseObject": {"feed": [{
        "id": "e", "type": "large_buy", "userId": "u1", "tokenAddress": "MINT",
        "createdAt": "2026-07-05T10:00:00Z",
        "body": {"userHandle": "alice", "ticker": "T", "price": 1.0,
                 "marketCap": 1_000_000, "currentSizeUsd": 3000},
    }]}}

    def http(method, url, headers=None, body=None):
        return 200, feed, "Sun, 05 Jul 2026 10:00:01 GMT"

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"
        def refresh_token(self, http=None): return "t"

    seen = []

    def hook(conn, now_iso):
        seen.append(conn.execute(
            "SELECT COUNT(*) FROM memecoin_feed_observations_v2",
        ).fetchone()[0])
        raise RuntimeError("paper candidate failure")

    result = run.run_once(
        c, _Auth(), 100, http=http, fill_fn=lambda *args, **kwargs: (1.0, "x"),
        candidate_hook=hook,
    )
    assert seen == [1]
    assert result["opened"] == 1


def test_run_once_does_not_rechurn_redelivered_buy_sell():
    """Root cause of the N=187->6 duplicate-event corruption: the fomo /feed re-delivers
    the same events every poll. With a buy+sell pair, poll 1 opens then mirror-closes;
    on poll 2 the re-delivered BUY finds the position CLOSED and re-opens it -> churn.
    The engine must process each fomo_event_id ONCE (gate on insert_event's new flag)."""
    c = _conn()

    def _item(eid, typ):
        return {"id": eid, "type": typ, "userId": "u1", "tokenAddress": "MINT",
                "createdAt": "2026-07-05T10:00:00Z",
                "body": {"userHandle": "alice", "ticker": "PEPE", "price": 0.005,
                         "marketCap": "1000000", "currentSizeUsd": 3000.0, "isFirstBuy": True}}
    feed = {"responseObject": {"feed": [_item("B", "large_buy"), _item("S", "large_sell")]}}

    def http(method, url, headers=None, body=None):
        return 200, feed, "Sun, 05 Jul 2026 10:00:01 GMT"

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"
        def refresh_token(self, http=None): return "t"

    def total():
        return c.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0]

    fill = lambda *a, **k: (0.005, "jupiter")   # == their_price, so the chase guard doesn't skip
    stats = lambda mint: {"price": 0.005, "liquidity": 1e15}   # sell mid for mirror close
    run.run_once(c, _Auth(), 100.0, http=http, fill_fn=fill, stats_fn=stats)   # poll 1: 3 arms open; control+alpha_b mirror-close
    assert total() == 3                          # one position per arm (fan-out), no churn
    run.run_once(c, _Auth(), 100.0, http=http, fill_fn=fill, stats_fn=stats)   # poll 2: re-delivery must NOT re-open
    assert total() == 3, f"re-delivered buy/sell churned to {total()} positions"


def test_new_network_tripwire_pages_once(monkeypatch):
    """First feed event on a never-seen network id -> one alert + warning; repeats are silent.
    (Robinhood Chain 4663 readiness, 2026-07-10: silently dropping a new venue for weeks is how
    BSC volume was missed.)"""
    import sqlite3
    from types import SimpleNamespace
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_runner as R
    from sniper.services import memecoin_alerts as A

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    monkeypatch.setattr(R, "_seen_networks", None)   # fresh process-lifetime cache
    paged = []
    monkeypatch.setattr(A, "alert_new_network",
                        lambda n, t, mint, **kw: paged.append(n) or (True, 0.0))

    ev = SimpleNamespace(network_id="4663", ticker="CASHCAT", mint="0x020bfC650A36")
    R._flag_new_networks(c, [ev])
    R._flag_new_networks(c, [ev])                    # same network again -> no second page
    assert paged == ["4663"]

    sol = SimpleNamespace(network_id="solana", ticker="x", mint="So1")
    c.execute("INSERT INTO memecoin_events(fomo_event_id, network_id) VALUES ('e1','solana')")
    monkeypatch.setattr(R, "_seen_networks", None)   # re-seed: solana now known from the DB
    R._flag_new_networks(c, [sol])
    assert paged == ["4663"]                         # known network never pages


def test_zero_evm_price_period_disables_fallback():
    from scripts.memecoin_runner_loop import evm_price_due

    assert evm_price_due(cycle=1_000.0, last_run=0.0, period_s=0.0) is False
    assert evm_price_due(cycle=1_000.0, last_run=950.0, period_s=60.0) is False
    assert evm_price_due(cycle=1_011.0, last_run=950.0, period_s=60.0) is True


# --- roster-gate-off funded widening (2026-08-14) ----------------------------------------------

def _uncurated_buy_http(_method, _url, headers=None, body=None):
    feed = {"responseObject": {"feed": [{
        "id": "ex1", "type": "large_buy", "userId": "stranger9", "tokenAddress": "MINTX",
        "createdAt": "2026-07-05T10:00:00Z",
        "body": {"userHandle": "whale_stranger", "ticker": "COCO", "price": 0.005,
                 "marketCap": "150000", "currentSizeUsd": 7500.0, "isFirstBuy": True},
    }]}}
    return 200, feed, "Sun, 05 Jul 2026 10:00:01 GMT"


def test_gate_off_evaluates_non_curated_buys_once(monkeypatch):
    """COCO/AI/XST/PONS 2026-08-14: with the roster gate OFF, big non-curated feed buys sat with
    real_decision NULL forever because `mine` is curated-only. The widened lane runs the funded
    hook exactly once per event; the hook's own real_decision stamp is the dedup."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", False)
    monkeypatch.setattr(
        "sniper.services.memecoin_copy_trader.copy_event_to_arms",
        lambda *_a, **_k: {"control": "ignored"})
    c = _conn()
    calls = []

    def hook(conn, ev):
        calls.append(ev.fomo_event_id)
        m.set_event_real_decision(conn, ev.fomo_event_id, "filled")
        return {"status": "filled"}

    run.run_once(c, _Auth(), 100.0, http=_uncurated_buy_http, real_hook=hook)
    run.run_once(c, _Auth(), 100.0, http=_uncurated_buy_http, real_hook=hook)  # redelivery
    assert calls == ["ex1"], "one funded evaluation per event, ever"


def test_gate_on_keeps_non_curated_invisible(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", True)
    monkeypatch.setattr(
        "sniper.services.memecoin_copy_trader.copy_event_to_arms",
        lambda *_a, **_k: {"control": "ignored"})
    calls = []
    run.run_once(_conn(), _Auth(), 100.0, http=_uncurated_buy_http,
                 real_hook=lambda *_a: calls.append(1) or {"status": "filled"})
    assert calls == []


def test_gate_off_paper_engine_is_byte_unchanged(monkeypatch):
    """The widened lane must never leak into the paper fan-out — non-curated buys stay paper-invisible."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", False)
    paper = []
    monkeypatch.setattr(
        "sniper.services.memecoin_copy_trader.copy_event_to_arms",
        lambda *_a, **_k: paper.append(1) or {"control": "ignored"})
    run.run_once(_conn(), _Auth(), 100.0, http=_uncurated_buy_http,
                 real_hook=lambda conn, ev: {"status": "filled"})
    assert paper == []
