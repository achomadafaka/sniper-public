from sniper.analysis.winning_wallet_thesis import ledger as L


def _act(typ, **kw):
    base = {"type": typ, "conditionId": "0xc", "timestamp": 1, "eventSlug": "e",
            "title": "t", "size": 0, "usdcSize": 0}
    base.update(kw)
    return base


def test_normalize_maps_trade_redeem_split():
    acts = [
        _act("TRADE", side="BUY", outcomeIndex=0, price=0.4, size=10, usdcSize=4),
        _act("TRADE", side="SELL", outcomeIndex=0, price=0.6, size=10, usdcSize=6),
        _act("REDEEM"),
        _act("SPLIT", size=100, usdcSize=100),
        _act("REWARD", usdcSize=3),
    ]
    rows = L.normalize(acts)
    kinds = [r.kind for r in rows]
    assert kinds == ["buy", "sell", "redeem", "split", "reward"]
    assert rows[0].price == 0.4 and rows[0].usdc == 4 and rows[0].outcome_index == 0


def test_normalize_assigns_increasing_seq():
    rows = L.normalize([_act("TRADE", side="BUY"), _act("TRADE", side="SELL")])
    assert [r.seq for r in rows] == [0, 1]


def test_normalize_keeps_conditionless_reward_row():
    rows = L.normalize([{"type": "REWARD", "usdcSize": 5}])   # no conditionId
    assert len(rows) == 1 and rows[0].kind == "reward" and rows[0].usdc == 5


def test_normalize_skips_trade_without_condition():
    assert L.normalize([{"type": "TRADE", "side": "BUY"}]) == []


def test_incremental_refresh_merges_new_activity_from_current_run_cursor(monkeypatch, tmp_path):
    wallet = "0x" + "1" * 40
    monkeypatch.setattr(L, "CACHE_DIR", str(tmp_path))
    old = [
        {"type": "TRADE", "side": "BUY", "conditionId": "old", "outcomeIndex": 0,
         "timestamp": 100, "transactionHash": "0xold", "asset": "a", "price": 0.4,
         "size": 10, "usdcSize": 4},
        {"type": "REWARD", "timestamp": 90, "transactionHash": "0xreward", "usdcSize": 1},
    ]
    (tmp_path / f"{wallet}_activity.json").write_text(__import__("json").dumps(old), encoding="utf-8")

    from sniper.analysis.winning_wallet_thesis import db_store
    cursor_ts = 1_700_000_000
    monkeypatch.setattr(db_store, "analysis_state", lambda w: {
        "wallet": w.lower(), "has_store": True, "run_id": "r1", "max_activity_ts": cursor_ts,
    })
    monkeypatch.setattr(L.P, "build_session", lambda: object())

    calls = {}

    def fake_fetch(_session, got_wallet, **kwargs):
        calls["wallet"] = got_wallet
        calls["kwargs"] = kwargs
        return [
            {"type": "TRADE", "side": "BUY", "conditionId": "new", "outcomeIndex": 0,
             "timestamp": 120, "transactionHash": "0xnew", "asset": "n", "price": 0.5,
             "size": 20, "usdcSize": 10},
            old[0],
        ]

    monkeypatch.setattr(L.P, "fetch_user_activity", fake_fetch)

    merged = L.load_activity(wallet, refresh=True, incremental=True)

    assert calls["wallet"] == wallet
    # start_ts is the stored cursor MINUS an overlap window (guards Polymarket backfills landing
    # just behind the newest stored event); dedup collapses the re-fetched overlap. (review #MED)
    assert calls["kwargs"]["start_ts"] == cursor_ts - L._INCREMENTAL_OVERLAP_S
    assert calls["kwargs"]["walk_full_history"] is True
    assert calls["kwargs"]["activity_type"] is None
    assert [r["transactionHash"] for r in merged] == ["0xnew", "0xold", "0xreward"]
    cached = __import__("json").loads((tmp_path / f"{wallet}_activity.json").read_text(encoding="utf-8"))
    assert [r["transactionHash"] for r in cached] == ["0xnew", "0xold", "0xreward"]


def test_incremental_refresh_without_store_falls_back_to_full_refresh(monkeypatch, tmp_path):
    wallet = "0x" + "2" * 40
    monkeypatch.setattr(L, "CACHE_DIR", str(tmp_path))
    (tmp_path / f"{wallet}_activity.json").write_text("[]", encoding="utf-8")

    from sniper.analysis.winning_wallet_thesis import db_store
    monkeypatch.setattr(db_store, "analysis_state", lambda _w: {"has_store": False, "max_activity_ts": None})
    monkeypatch.setattr(L.P, "build_session", lambda: object())

    calls = {}

    def fake_fetch(_session, _wallet, **kwargs):
        calls["kwargs"] = kwargs
        return [{"type": "REWARD", "timestamp": 10, "transactionHash": "0xfull", "usdcSize": 1}]

    monkeypatch.setattr(L.P, "fetch_user_activity", fake_fetch)

    rows = L.load_activity(wallet, refresh=True, incremental=True)

    assert calls["kwargs"]["start_ts"] is None
    assert rows[0]["transactionHash"] == "0xfull"
