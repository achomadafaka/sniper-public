import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.feeds.fomo_feed import CopyEvent
from sniper.services import memecoin_copy_trader as eng


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _buy(uid="u1", mint="MINT"):
    return CopyEvent(uid, "alice", mint, "PEPE", "buy", 0.0047, 4_700_000, 3318.0,
                     True, "e1", "2026-07-05T10:00:00Z", "2026-07-05T10:00:00Z", 0.3)


def test_entry_opens_position_with_honest_fill():
    c = _conn()
    fill = lambda mint, usd, side, fallback, http=None: (0.005, "jupiter")
    r = eng.on_copy_event(c, _buy(), size_usd=100.0, now_iso="2026-07-05T10:00:01Z", fill_fn=fill)
    assert r == "opened"
    pos = m.get_open_position(c, "u1", "MINT")
    assert pos["entry_price"] == 0.005 and pos["entry_notional"] == 100.0
    assert pos["entry_fill_source"] == "jupiter" and pos["their_size_usd"] == 3318.0


def test_legacy_provider_shadow_keeps_pair_age_out_of_entry_age(monkeypatch):
    # P2 (deep review) computed entry_token_age_s from DexScreener pairCreatedAt. Paper step 3
    # (spec 2026-10-08 paper path without DexScreener) leaves the column NULL: the pair age is not the
    # token age, and the funded decision's own pair_age_s rides funded_facts. The provider's age is
    # kept only under features_json["provider_shadow"] for the one-day comparison.
    monkeypatch.setattr(eng, "PROVIDER_SHADOW", True)
    monkeypatch.setattr("sniper.util.clock.real_utc_now", lambda *a, **k: 1_000_000.0)
    c = _conn()
    stats = lambda mint, **k: {"price": 0.005, "liquidity": 1e15,
                               "pair_created_at": (1_000_000.0 - 3600.0) * 1000.0}   # 1h old (ms)
    eng.on_copy_event(c, _buy(), 100.0, "t", fill_fn=lambda *a, **k: (0.005, "dexscreener"),
                      stats_fn=stats)
    pos = m.get_open_position(c, "u1", "MINT")
    assert pos["entry_token_age_s"] is None
    import json
    f = json.loads(c.execute("SELECT features_json FROM memecoin_entry_snapshots "
                             "WHERE action='opened'").fetchone()[0])
    assert abs(f["provider_shadow"]["token_age_s"] - 3600.0) < 1.0 and "pair_created_at" not in f


def test_retired_provider_shadow_does_not_fetch_or_supply_token_age():
    assert eng.PROVIDER_SHADOW is False
    c = _conn()
    calls = []
    def stats(*a, **k):
        calls.append(1)
        return {"pair_created_at": 1_000_000.0}
    eng.on_copy_event(c, _buy(), 100.0, "t", fill_fn=lambda *a, **k: (0.005, "dexscreener"),
                      stats_fn=stats)
    assert calls == [] and m.get_open_position(c, "u1", "MINT")["entry_token_age_s"] is None
    import json
    features = json.loads(c.execute("SELECT features_json FROM memecoin_entry_snapshots "
                                   "WHERE action='opened'").fetchone()[0])
    assert "provider_shadow" not in features and "pair_created_at" not in features


def test_entry_dedup_when_position_open():
    c = _conn()
    fill = lambda *a, **k: (0.005, "jupiter")
    eng.on_copy_event(c, _buy(), 100.0, "t", fill_fn=fill)
    assert eng.on_copy_event(c, _buy(), 100.0, "t", fill_fn=fill) == "skipped_dup"
