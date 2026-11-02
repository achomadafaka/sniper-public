"""Wallet-discovery funnel: candidate aggregate table + capture in run_once."""
import sqlite3
from datetime import datetime, timedelta, timezone

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_runner as run
from _web_csrf import csrf_client


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


class _Ev:
    def __init__(self, uid, mint, size, side="buy", handle="h", ticker="T", eid=None):
        self.source_user_id = uid; self.source_handle = handle; self.mint = mint
        self.ticker = ticker; self.side = side; self.their_price = 0.01
        self.their_market_cap = 1e6; self.their_size_usd = size; self.is_first_buy = True
        self.fomo_event_id = eid or f"{uid}-{mint}-{side}"
        self.created_at = "2026-07-06T10:00:00Z"; self.received_at = "2026-07-06T10:00:01Z"
        self.lag_s = 0.1


def test_upsert_candidate_increments_and_maxes():
    c = _conn()
    m.upsert_candidate(c, _Ev("u1", "MINTA", 3000))
    m.upsert_candidate(c, _Ev("u1", "MINTB", 5000))
    row = m.get_candidate(c, "u1")
    assert row["sighting_count"] == 2
    assert row["total_buy_usd"] == 8000
    assert row["max_buy_usd"] == 5000
    assert row["last_ticker"] == "T"
    assert row["status"] == "new"


def test_upsert_candidate_preserves_status_on_conflict():
    c = _conn()
    m.upsert_candidate(c, _Ev("u1", "MINTA", 3000))
    m.set_candidate_status(c, "u1", "ignored")
    m.upsert_candidate(c, _Ev("u1", "MINTB", 4000))   # re-sighting must NOT un-ignore
    assert m.get_candidate(c, "u1")["status"] == "ignored"


def test_list_candidates_filters_new_and_thresholds():
    c = _conn()
    m.upsert_candidate(c, _Ev("u1", "MINTA", 5000))
    m.upsert_candidate(c, _Ev("u2", "MINTB", 500))    # below any min_usd we set
    m.upsert_candidate(c, _Ev("u3", "MINTC", 9000))
    m.set_candidate_status(c, "u3", "ignored")         # excluded (not 'new')
    rows = m.list_candidates(c, min_sightings=1, min_usd=1000)
    ids = [r["source_user_id"] for r in rows]
    assert ids == ["u1"]                               # u2 sub-threshold, u3 ignored
    # analytics LEFT JOIN present as columns (None when unscreened)
    assert "realized_pnl_usd" in rows[0]


def _candidate_event(c, uid, *, created_at, size, mint, network="solana"):
    ev = _Ev(uid, mint, size, eid=f"{uid}-{created_at}")
    ev.created_at = created_at
    ev.received_at = created_at
    ev.network_id = network
    m.upsert_candidate(c, ev)
    payload = dict(ev.__dict__)
    payload["source_handle"] = ev.source_handle
    assert m.insert_event(c, payload)
    c.execute("UPDATE memecoin_events SET action='candidate' WHERE fomo_event_id=?",
              (ev.fomo_event_id,))
    c.commit()


def test_candidate_list_prioritizes_recent_activity_over_lifetime_accumulation():
    c = _conn()
    _candidate_event(c, "stale", created_at="2026-06-01T00:00:00Z",
                     size=1_000_000, mint="OLD")
    _candidate_event(c, "fresh", created_at="2026-07-28T10:00:00Z",
                     size=2_000, mint="NEW", network="4663")

    rows = m.list_candidates(c, min_usd=1_000, now_iso="2026-07-28T12:00:00Z")
    assert [r["source_user_id"] for r in rows] == ["fresh", "stale"]
    assert rows[0]["fresh_sightings_7d"] == 1
    assert rows[0]["fresh_buy_usd_7d"] == 2_000
    assert rows[0]["distinct_coins_7d"] == 1
    assert rows[0]["network_id"] == "4663"


def test_candidate_list_supports_status_pagination_and_total_count():
    c = _conn()
    for index in range(3):
        _candidate_event(
            c, f"u{index}", created_at=f"2026-07-28T0{index}:00:00Z",
            size=2_000 + index, mint=f"M{index}",
        )
    m.set_candidate_status(c, "u0", "ignored")

    first = m.list_candidates(
        c, min_usd=1_000, status="new", limit=1, offset=0,
        now_iso="2026-07-28T12:00:00Z",
    )
    second = m.list_candidates(
        c, min_usd=1_000, status="new", limit=1, offset=1,
        now_iso="2026-07-28T12:00:00Z",
    )
    assert [r["source_user_id"] for r in first] == ["u2"]
    assert [r["source_user_id"] for r in second] == ["u1"]
    assert m.count_candidates(c, min_usd=1_000, status="new") == 2
    assert [r["source_user_id"] for r in m.list_candidates(
        c, min_usd=1_000, status="ignored", now_iso="2026-07-28T12:00:00Z",
    )] == ["u0"]


def test_candidates_endpoint_returns_page_metadata():
    c = _conn()
    for index in range(3):
        _candidate_event(
            c, f"u{index}", created_at=f"2026-07-28T0{index}:00:00Z",
            size=2_000, mint=f"M{index}",
        )
    cl = _client(c)

    payload = cl.get("/api/memecoin/candidates?min_usd=1000&limit=1&offset=1").get_json()
    assert payload["total"] == 3
    assert payload["limit"] == 1
    assert payload["offset"] == 1
    assert [r["source_user_id"] for r in payload["rows"]] == ["u1"]


def test_excluded_candidate_page_only_surfaces_fresh_activity():
    c = _conn()
    for uid in ("fresh", "stale"):
        m.add_wallet(c, uid, uid)
        m.set_wallet_excluded(c, uid, True)
    # The endpoint's freshness window is a rolling `-7 days` from now, so absolute fixture
    # dates rot: "2026-07-28" was fresh when written and silently aged out of the window,
    # failing this test — and the funded deploy gate, which runs this whole file — on a
    # date rather than on a defect. Anchor both fixtures to now instead.
    now = datetime.now(timezone.utc)
    fresh_at = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_at = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _candidate_event(c, "fresh", created_at=fresh_at,
                     size=4_000, mint="LIVE", network="4663")
    _candidate_event(c, "stale", created_at=stale_at,
                     size=50_000, mint="OLD")

    payload = _client(c).get(
        "/api/memecoin/candidates?status=excluded&min_usd=1000"
    ).get_json()
    assert payload["total"] == 1
    assert [r["source_user_id"] for r in payload["rows"]] == ["fresh"]
    assert payload["rows"][0]["wallet_excluded"] == 1


def test_run_once_captures_noncurated_large_buys_only():
    c = _conn()
    m.add_wallet(c, "cur", "curated")                  # curated wallet -> copied, not a candidate
    feed = {"responseObject": {"feed": [
        {"id": "e1", "type": "large_buy", "userId": "cur", "tokenAddress": "M1",
         "createdAt": "2026-07-06T10:00:00Z",
         "body": {"userHandle": "curated", "ticker": "A", "price": 0.01,
                  "currentSizeUsd": 3000, "isFirstBuy": True}},
        {"id": "e2", "type": "large_buy", "userId": "stranger", "tokenAddress": "M2",
         "createdAt": "2026-07-06T10:00:00Z",
         "body": {"userHandle": "whale", "ticker": "B", "price": 0.02,
                  "currentSizeUsd": 8000, "isFirstBuy": True}},
        {"id": "e3", "type": "large_buy", "userId": "smallfry", "tokenAddress": "M3",
         "createdAt": "2026-07-06T10:00:00Z",
         "body": {"userHandle": "small", "ticker": "C", "price": 0.001,
                  "currentSizeUsd": 50, "isFirstBuy": True}},
    ]}}

    def http(method, url, headers=None, body=None):
        return 200, feed, "Sun, 06 Jul 2026 10:00:01 GMT"

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"
        def refresh_token(self, http=None): return "t"

    run.run_once(c, _Auth(), 100.0, http=http, fill_fn=lambda *a, **k: (0.02, "j"),
                 min_discovery_usd=1000)
    cands = {r["source_user_id"] for r in m.list_candidates(c)}
    assert cands == {"stranger"}          # curated 'cur' copied not candidate; 'smallfry' sub-threshold
    assert m.get_candidate(c, "stranger")["max_buy_usd"] == 8000


def test_run_once_candidate_dedup_no_double_count():
    c = _conn()
    feed = {"responseObject": {"feed": [
        {"id": "e2", "type": "large_buy", "userId": "stranger", "tokenAddress": "M2",
         "createdAt": "2026-07-06T10:00:00Z",
         "body": {"userHandle": "whale", "ticker": "B", "price": 0.02,
                  "currentSizeUsd": 8000, "isFirstBuy": True}},
    ]}}

    def http(method, url, headers=None, body=None):
        return 200, feed, "Sun, 06 Jul 2026 10:00:01 GMT"

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"
        def refresh_token(self, http=None): return "t"

    run.run_once(c, _Auth(), 100.0, http=http, fill_fn=lambda *a, **k: (0.02, "j"), min_discovery_usd=1000)
    run.run_once(c, _Auth(), 100.0, http=http, fill_fn=lambda *a, **k: (0.02, "j"), min_discovery_usd=1000)
    assert m.get_candidate(c, "stranger")["sighting_count"] == 1   # re-delivery deduped
    # candidate events are excluded from the copy Recent-activity feed
    assert all(e.get("action") != "candidate" for e in m.list_recent_events(c))


def test_run_once_alerts_fresh_noncurated_consensus_candidate(monkeypatch):
    """The advisory predicate already accepts rank-2 discovery buys; run_once must reach it."""
    from sniper.settings import memecoin as cfg

    c = _conn()
    prior = _Ev("prior", "SoConsensus", 2_000, eid="prior-buy")
    prior.created_at = "2026-07-17T10:00:00Z"
    prior.their_market_cap = 900_000
    prior.network_id = "solana"
    prior.received_at = prior.created_at
    m.insert_event(c, prior.__dict__)
    feed = {"responseObject": {"feed": [{
        "id": "rank-2", "type": "large_buy", "userId": "rank2",
        "tokenAddress": "SoConsensus", "createdAt": "2026-07-17T10:10:00Z",
        "body": {"userHandle": "rank2", "ticker": "CONS", "price": 0.02,
                 "marketCap": 900_000, "currentSizeUsd": 8_000, "isFirstBuy": True},
    }]}}
    monkeypatch.setattr(cfg, "MEMECOIN_DISCOVERY_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MAX_LAG_S", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_CONSENSUS_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_CONSENSUS_MAX_MCAP", 2_000_000.0)
    sent = []

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"
        def refresh_token(self, http=None): return "t"

    result = run.run_once(
        c, _Auth(), 100.0,
        http=lambda *_a, **_k: (200, feed, "Fri, 17 Jul 2026 10:10:01 GMT"),
        alert_fn=lambda msg: sent.append(msg) or (True, 1.0), min_discovery_usd=1_000,
    )

    assert result["alerts"] == 1
    assert len(sent) == 1 and "CONS" in sent[0]
    assert m.get_open_position(c, "rank2", "SoConsensus") is None


# ---- endpoints ----
def _client(c):
    from flask import Flask
    import sniper.routes.api_memecoin as api
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    return csrf_client(app)


def test_candidates_endpoints_list_ignore_promote():
    import json
    c = _conn()
    m.upsert_candidate(c, _Ev("u1", "MINTA", 9000, handle="whale"))
    cl = _client(c)

    rows = json.loads(cl.get("/api/memecoin/candidates?min_usd=1000").data)["rows"]
    assert [r["source_user_id"] for r in rows] == ["u1"]

    # ignore -> drops off the list
    assert cl.post("/api/memecoin/candidates/u1/ignore").status_code == 200
    assert json.loads(cl.get("/api/memecoin/candidates").data)["rows"] == []
    assert m.get_candidate(c, "u1")["status"] == "ignored"

    # promote a fresh candidate -> added to curated + marked promoted
    m.upsert_candidate(c, _Ev("u2", "MINTB", 7000, handle="sharp"))
    assert cl.post("/api/memecoin/candidates/u2/promote").status_code == 200
    assert "u2" in m.curated_user_ids(c)
    assert m.get_candidate(c, "u2")["status"] == "promoted"


def test_candidate_mutations_reject_missing_or_empty_identity_without_writes():
    c = _conn()
    cl = _client(c)

    assert cl.post("/api/memecoin/candidates/missing/promote").status_code == 404
    assert cl.post("/api/memecoin/candidates/missing/ignore").status_code == 404
    assert cl.post(
        "/api/memecoin/wallets", json={"source_user_id": "   ", "handle": "blank"},
    ).status_code == 400
    assert m.list_wallets(c) == []


def test_excluded_candidate_requires_explicit_watch_only_reobserve():
    c = _conn()
    m.upsert_candidate(c, _Ev("u1", "MINTA", 9_000, handle="whale"))
    m.add_wallet(c, "u1", "whale")
    c.execute("UPDATE memecoin_wallets SET excluded=1,paused=1,real_approved=1 "
              "WHERE source_user_id='u1'")
    c.commit()
    cl = _client(c)

    refused = cl.post("/api/memecoin/candidates/u1/promote")
    assert refused.status_code == 409
    assert refused.get_json()["error"] == "excluded_requires_reobserve"
    assert m.get_candidate(c, "u1")["status"] == "new"
    assert "u1" not in m.curated_user_ids(c)

    restored = cl.post("/api/memecoin/wallets/u1/reobserve")
    assert restored.status_code == 200
    row = c.execute("SELECT paused,excluded,real_approved FROM memecoin_wallets "
                    "WHERE source_user_id='u1'").fetchone()
    assert tuple(row) == (0, 0, 0)
    assert m.get_candidate(c, "u1")["status"] == "promoted"
    assert "u1" in m.curated_user_ids(c)


def test_ignored_candidate_can_only_return_by_explicit_reconsider():
    c = _conn()
    m.upsert_candidate(c, _Ev("u1", "MINTA", 9_000))
    m.set_candidate_status(c, "u1", "ignored")
    cl = _client(c)

    assert cl.post("/api/memecoin/candidates/u1/reconsider").status_code == 200
    assert m.get_candidate(c, "u1")["status"] == "new"


def test_manual_wallet_delete_retires_row_and_revokes_real_approval():
    c = _conn()
    m.add_wallet(c, "u1", "whale")
    c.execute("UPDATE memecoin_wallets SET real_approved=1 WHERE source_user_id='u1'")
    c.commit()
    cl = _client(c)

    assert cl.delete("/api/memecoin/wallets/u1").status_code == 200
    row = c.execute("SELECT paused,excluded,real_approved FROM memecoin_wallets "
                    "WHERE source_user_id='u1'").fetchone()
    assert row is not None and tuple(row) == (1, 1, 0)


def test_manual_add_does_not_silently_revive_excluded_wallet():
    c = _conn()
    m.add_wallet(c, "u1", "whale")
    m.set_wallet_excluded(c, "u1", True)
    cl = _client(c)

    response = cl.post(
        "/api/memecoin/wallets", json={"source_user_id": "u1", "handle": "whale"},
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == "excluded_requires_reobserve"
    assert "u1" not in m.curated_user_ids(c)


def test_resume_of_excluded_wallet_is_watch_only():
    c = _conn()
    m.add_wallet(c, "u1", "whale")
    c.execute("UPDATE memecoin_wallets SET excluded=1,paused=1,real_approved=1 "
              "WHERE source_user_id='u1'")
    c.commit()

    response = _client(c).post("/api/memecoin/wallets/u1/pause", json={"paused": False})
    assert response.status_code == 200
    row = c.execute("SELECT paused,excluded,real_approved FROM memecoin_wallets "
                    "WHERE source_user_id='u1'").fetchone()
    assert tuple(row) == (0, 0, 0)
