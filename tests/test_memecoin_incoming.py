"""Pre-trade board: recent large buys from ARMED wallets, grouped by coin (2026-07-18).

recent_armed_signals() and GET /api/memecoin/real/incoming surface 'what are my wallets buying right
now' — armed (real_approved, not paused/excluded) wallets only, distinct from the full Radar firehose.
"""
import sqlite3

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint


def _obs(uid, mint, ts, *, size=1000.0, mcap=500000.0, ticker="T", net="4663"):
    return {"upstream_event_id": f"{uid}-{mint}-{ts}", "raw_type": "large_buy", "normalized_side": "buy",
            "source_user_id": uid, "source_handle": uid, "network_id": net, "mint": mint, "ticker": ticker,
            "their_price": 1.0, "their_market_cap": mcap, "their_size_usd": size, "source_ts_raw": ts,
            "source_ts": ts, "observed_at": ts, "lag_s": 0.0, "raw_payload": {}}


def _db():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    for uid in ("armed1", "armed2", "cold"):
        m.add_wallet(c, uid, uid)
    m.set_wallet_real_approved(c, "armed1", True, force=True)
    m.set_wallet_real_approved(c, "armed2", True, force=True)   # 'cold' stays not-real-approved
    c.commit()
    return c


def test_groups_by_coin_and_counts_only_armed_buyers():
    c = _db()
    # COIN X bought by 2 armed + 1 cold -> one row, consensus 2 (armed only)
    for uid in ("armed1", "armed2", "cold"):
        m.insert_feed_observation_v2(c, _obs(uid, "0xX", "2026-07-18T10:00:00+00:00", ticker="XCOIN"),
                                     was_curated=True, was_real_approved=True)
    # COIN Y bought ONLY by a cold (non-armed) wallet -> excluded entirely
    m.insert_feed_observation_v2(c, _obs("cold", "0xY", "2026-07-18T10:05:00+00:00"),
                                 was_curated=True, was_real_approved=True)
    rows = m.recent_armed_signals(c, since_iso="2026-07-18T09:00:00+00:00", limit=15)
    assert len(rows) == 1
    r = rows[0]
    assert r["mint"] == "0xX" and r["ticker"] == "XCOIN"
    assert r["consensus"] == 2 and r["last_handle"] in ("armed1", "armed2", "cold")
    assert r["mcap"] == 500000.0


def test_window_excludes_old_observations():
    c = _db()
    m.insert_feed_observation_v2(c, _obs("armed1", "0xOld", "2026-07-18T08:00:00+00:00"),
                                 was_curated=True, was_real_approved=True)
    rows = m.recent_armed_signals(c, since_iso="2026-07-18T09:00:00+00:00", limit=15)
    assert rows == []


def test_incoming_endpoint_flags_held_positions():
    c = _db()
    # RELATIVE to the live clock — the endpoint filters by `?mins=360` off real-now, so a fixed stamp
    # ages out of the window as wall-clock advances (this test used to fail ~6h into any UTC day).
    from datetime import datetime, timedelta
    from sniper.util.clock import real_utc_now_iso
    recent = (datetime.fromisoformat(real_utc_now_iso()) - timedelta(minutes=5)).isoformat()
    m.insert_feed_observation_v2(c, _obs("armed1", "0xHELD", recent, ticker="HELD"),
                                 was_curated=True, was_real_approved=True)
    # we already hold 0xHELD (open real position)
    c.execute("INSERT INTO memecoin_positions(id, mint, ticker, entry_price, network_id, is_open, "
              "execution_mode, opened_at) VALUES(1,'0xHELD','HELD',0.001,'4663',1,'real','2026-07-18T00:00:00+00:00')")
    c.commit()
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    cli = app.test_client()
    r = cli.get("/api/memecoin/real/incoming?mins=360")
    assert r.status_code == 200
    rows = r.get_json()["rows"]
    held = next((x for x in rows if x["mint"] == "0xHELD"), None)
    assert held is not None and held["held"] is True
