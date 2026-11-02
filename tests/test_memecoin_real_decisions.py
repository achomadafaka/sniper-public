"""WS2 + WS3 (2026-07-15l audit).

WS2 — nothing the real algo does should be invisible. Real-path entry outcomes (unroutable_chain,
not_approved, skipped_mcap, skipped_chase, stale, rejected, aborted, unsafe, filled, …) were never
written to memecoin_events, so the Activity tab showed the raw fomo firehose with no indication of
what WE actually did with each buy. A separate `real_decision`/`real_decision_reason` pair records
the real outcome WITHOUT clobbering the paper `action` (both legs read the same event row).

WS3 — every real position had origin_event_id=NULL, making real-vs-shadow matched comparison
impossible. The real entry path now stamps origin_event_id from the feed event, exactly like the
paper `_open_arm` does. No backfill of old rows.
"""
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m

APP_JS = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()


def test_ui_has_a_decisions_surface_consuming_events_recent():
    # WS2 UI: the real algo's per-event decisions must be VISIBLE (events/recent was rendered nowhere)
    assert "/api/memecoin/events/recent" in APP_JS
    assert 'data-subtab="decisions"' in APP_JS
    assert "_loadDecisions" in APP_JS
    assert "real_decision" in APP_JS
from sniper.services import memecoin_arming
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg


class _EvObj:
    def __init__(self, fid, uid="u1", mint="So1mint", network_id="solana", side="buy",
                 created_at="2026-07-15T13:00:00Z"):
        self.fomo_event_id = fid; self.source_user_id = uid; self.source_handle = uid
        self.mint = mint; self.ticker = "T"; self.side = side; self.network_id = network_id
        self.created_at = created_at; self.received_at = created_at; self.their_market_cap = 1.0
        self.their_price = 1.0; self.their_size_usd = 100.0; self.signal_source = "global_feed"


def _sol_ctx(execute_fn=None):
    return {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
            "token_safety_fn": lambda mint, **kw: (True, "ok"),
            "execute_fn": execute_fn or (lambda *a, **k: {"status": "filled"}),
            "kill_path": None}


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _ev(fid="alice-1", uid="alice", mint="0xcoin", side="buy"):
    return {"fomo_event_id": fid, "source_user_id": uid, "source_handle": uid, "mint": mint,
            "ticker": "T", "side": side, "their_price": 1.0, "their_market_cap": 1.0,
            "their_size_usd": 100.0, "created_at": "2026-07-15T13:00:00.000Z",
            "received_at": "2026-07-15T13:00:01.000Z", "lag_s": 1.0, "network_id": "solana"}


# ---- WS2: real decision recorded separately from paper action -----------------------------------

def test_set_event_real_decision_writes_decision_reason_and_time():
    c = _conn()
    m.insert_event(c, _ev())
    m.set_event_real_decision(c, "alice-1", "skipped_mcap", reason="mcap $42M > $30M",
                              now_iso="2026-07-15T13:00:02.000Z")
    row = c.execute("SELECT * FROM memecoin_events WHERE fomo_event_id='alice-1'").fetchone()
    assert row["real_decision"] == "skipped_mcap"
    assert row["real_decision_reason"] == "mcap $42M > $30M"
    assert row["real_decision_at"] == "2026-07-15T13:00:02.000Z"


def test_set_event_real_decision_defaults_to_corrected_real_utc(monkeypatch):
    monkeypatch.setattr(
        m, "real_utc_now_iso", lambda: "2026-07-15T17:00:00+00:00", raising=False,
    )
    c = _conn()
    m.insert_event(c, _ev())

    m.set_event_real_decision(c, "alice-1", "not_approved")

    stamp = c.execute(
        "SELECT real_decision_at FROM memecoin_events WHERE fomo_event_id='alice-1'"
    ).fetchone()[0]
    assert stamp == "2026-07-15T17:00:00+00:00"


def test_real_decision_does_not_clobber_paper_action():
    """Both legs process the SAME event row — the real decision must be a separate column so the
    paper `action` (opened/exit/…) is never overwritten (and vice versa)."""
    c = _conn()
    m.insert_event(c, _ev())
    m.set_event_action(c, "alice-1", "opened")           # paper opened
    m.set_event_real_decision(c, "alice-1", "not_approved", reason="wallet not armed")
    row = c.execute("SELECT * FROM memecoin_events WHERE fomo_event_id='alice-1'").fetchone()
    assert row["action"] == "opened"                     # paper untouched
    assert row["real_decision"] == "not_approved"


def test_list_recent_events_surfaces_the_real_decision():
    c = _conn()
    m.insert_event(c, _ev())
    m.set_event_real_decision(c, "alice-1", "filled", reason="on-chain $10.12")
    ev = next(e for e in m.list_recent_events(c) if e["fomo_event_id"] == "alice-1")
    assert ev["real_decision"] == "filled" and ev["real_decision_reason"] == "on-chain $10.12"


def test_set_event_real_decision_noop_when_event_absent():
    # a follow-activity event may not be in memecoin_events; recording must be a harmless no-op
    c = _conn()
    m.set_event_real_decision(c, "ghost-999", "stale", reason="x")   # must not raise
    assert c.execute("SELECT COUNT(*) FROM memecoin_events").fetchone()[0] == 0


# ---- WS3: origin_event_id stamped on real positions ---------------------------------------------

def _open_real(c, uid="alice", mint="0xcoin"):
    return m.open_position(c, source_user_id=uid, source_handle="(real)", mint=mint, ticker="",
                           entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                           entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-15T13:00:00Z",
                           strategy="alpha_b", execution_mode="real", network_id="solana")


def test_set_position_identity_stamps_origin_event_id():
    c = _conn()
    pid = _open_real(c)                                   # executor opens with origin_event_id NULL
    assert c.execute("SELECT origin_event_id FROM memecoin_positions WHERE id=?", (pid,)).fetchone()[0] is None
    m.set_position_identity(c, pid, ticker="T", source_handle="alice", origin_event_id="alice-1")
    row = c.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
    assert row["origin_event_id"] == "alice-1"
    assert row["ticker"] == "T" and row["source_handle"] == "alice"


def test_set_position_identity_never_overwrites_existing_origin():
    c = _conn()
    pid = m.open_position(c, source_user_id="a", source_handle="(real)", mint="M", ticker="",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="t", strategy="alpha_b",
                          execution_mode="real", network_id="solana", origin_event_id="orig-1")
    m.set_position_identity(c, pid, origin_event_id="orig-2")   # later stamp must not clobber
    row = c.execute("SELECT origin_event_id FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
    assert row[0] == "orig-1"


# ---- WS2: the hook records the decision onto the event (single choke point, both call sites) ------

def test_hook_records_unknown_source_decision_on_the_event():
    c = _conn()
    m.insert_event(c, _ev(fid="e1", uid="nobody", mint="So1mint"))     # wallet never armed
    hook = R.make_real_hook(None, {"solana": _sol_ctx()},
                            now_fn=lambda: "2026-07-15T13:00:00Z", trade_size=10.0, max_age_s=120)
    res = hook(c, _EvObj("e1", uid="nobody"))
    assert res["status"] == "unknown_source"
    row = c.execute("SELECT real_decision, real_decision_reason FROM memecoin_events "
                    "WHERE fomo_event_id='e1'").fetchone()
    assert row["real_decision"] == "unknown_source"
    assert row["real_decision_reason"] == "source identity not found"


def test_hook_reuses_decision_start_for_real_decision_timestamp():
    c = _conn()
    m.insert_event(c, _ev(fid="e-clock", uid="nobody", mint="So1mint"))
    hook = R.make_real_hook(
        None, {"solana": _sol_ctx()},
        now_fn=lambda: "2026-07-15T17:00:01+00:00",
        trade_size=10.0, max_age_s=120,
    )

    res = hook(c, _EvObj("e-clock", uid="nobody"))

    assert res["status"] == "unknown_source"
    stamp = c.execute(
        "SELECT real_decision_at FROM memecoin_events WHERE fomo_event_id='e-clock'"
    ).fetchone()[0]
    assert stamp == "2026-07-15T17:00:01+00:00"


def test_hook_records_unroutable_chain_and_counts_a_route_drop():
    c = _conn()
    m.insert_event(c, _ev(fid="e2", uid="u1", mint="0xEvmMint"))
    hook = R.make_real_hook(None, {"solana": _sol_ctx()},   # no 56 leg -> unroutable
                            now_fn=lambda: "2026-07-15T13:00:00Z", trade_size=10.0, max_age_s=120)
    res = hook(c, _EvObj("e2", uid="u1", mint="0xEvmMint", network_id="56"))
    assert res["status"] == "unroutable_chain"
    row = c.execute("SELECT real_decision FROM memecoin_events WHERE fomo_event_id='e2'").fetchone()
    assert row["real_decision"] == "unroutable_chain"
    # parity with the leg's dropped_network counter — the cockpit route-drops KPI must see it too
    drops = m.network_drop_counts(c)
    assert drops.get(("56", "unroutable_chain")) == 1


# ---- WS3: a real fill through the entry hook is stamped with origin_event_id ----------------------

def test_real_entry_hook_stamps_origin_event_id_on_fill(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _conn()
    opened = {}

    def ex(conn, pid, mint, size, now, impact_cap_pct=None,
           entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        rpid = _open_real(c, uid="u1", mint="Mfresh")   # executor opens the real position
        opened["pid"] = rpid
        opened["entry_window"] = (entry_min_slot, entry_max_slot, entry_head_fn)
        return {"status": "filled", "position_id": rpid}
    rc = _sol_ctx(execute_fn=ex); rc["kill_path"] = str(tmp_path / "k")
    ev = _EvObj("e3", uid="u1", mint="Mfresh")
    ev.source_block_number = 100
    r = R.real_entry_hook(c, ev, {"solana": rc}, now_fn=lambda: "2026-07-15T13:00:00Z",
                          trade_size=10.0, max_age_s=120)
    assert r["status"] == "filled"
    got = c.execute("SELECT origin_event_id FROM memecoin_positions WHERE id=?",
                    (opened["pid"],)).fetchone()[0]
    assert got == "e3", "the real fill must carry its originating fomo_event_id (WS3)"
    assert opened["entry_window"][:2] == (None, None)
    assert callable(opened["entry_window"][2])


def test_a_virtual_depth_reading_never_nulls_a_real_price(monkeypatch):
    """F6 recheck, 2026-09-23, and this one is a decision. `_snapshot_priced` blanks price, market
    cap and fdv when the pair behind them is dust, and the price gates then fail open to the
    source fill. When the provider returned NO pair, the liquidity number is our own virtual
    2*L*sqrt(P) reading of a pool we have not measured -- a median 0.27 of the provider's TVL
    where both existed -- so it must not be the thing that condemns a real price. The reading is
    still recorded, under a key that does not claim we acted on it."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SNAPSHOT_MIN_PAIR_LIQUIDITY_USD", 500.0, raising=False)
    out = R._snapshot_priced({"price": 1.5, "market_cap": 10_000.0, "fdv": 12_000.0,
                              "liquidity": 12.18, "liquidity_source": "pool_store"})
    assert out["price"] == 1.5 and out["market_cap"] == 10_000.0 and out["fdv"] == 12_000.0
    assert out["dust_pair_liquidity_unverified_usd"] == 12.18
    assert "dust_pair_liquidity_usd" not in out


def test_a_provider_measured_dust_pair_still_blanks_the_price(monkeypatch):
    """The ESKER case this guard was built for is unchanged: a $12 pair the PROVIDER published is
    still not a price, and the gates still see unknown."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SNAPSHOT_MIN_PAIR_LIQUIDITY_USD", 500.0, raising=False)
    out = R._snapshot_priced({"price": 1.5, "market_cap": 10_000.0, "fdv": 12_000.0,
                              "liquidity": 12.18})
    assert out["price"] is None and out["market_cap"] is None and out["fdv"] is None
    assert out["dust_pair_liquidity_usd"] == 12.18
