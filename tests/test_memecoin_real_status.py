"""Real-money cockpit endpoints: /api/memecoin/real/status and /real/positions. The operator surface
for the live leg — arm state, kill switch, daily breaker, reservations, real exposure, per-venue
readiness, and real open positions (mode-scoped, never mixed with paper)."""
import sqlite3
import json

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.settings import memecoin as cfg
from _web_csrf import csrf_client


def _client(monkeypatch, tmp_path, ctx_extra=None):
    # hermetic creds: point keypair paths at nonexistent files so status reports 'not present'
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", str(tmp_path / "nope_sol.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", str(tmp_path / "nope_evm.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453"})
    c = sqlite3.connect(":memory:", check_same_thread=False); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    ctx = {"conn": c}
    if ctx_extra:
        ctx.update(ctx_extra)
    app.register_blueprint(create_api_memecoin_blueprint(ctx))
    return csrf_client(app), c


def _open_real(c, mint, network, price=1.0, notional=10.0):
    return m.open_position(c, source_user_id="u", source_handle="(real)", mint=mint, ticker="T",
                           entry_price=price, entry_notional=notional, entry_fill_source="chain",
                           entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z",
                           strategy="alpha_b", execution_mode="real", network_id=network)


def _open_funded(c, *, malformed=False):
    from sniper.services.memecoin_funded_policy import FundedExitPolicy

    policy = FundedExitPolicy(
        policy_id="funded-small-v1", policy_version=1, coin_band="small",
        tp_pct=60.0, derisk_pct=40.0, derisk_mode="recover_entry",
        trail_pct=30.0, trail_arm_pct=15.0, trail_requires_derisk=False,
        stop_pct=50.0, catastrophe_pct=80.0, stop_debounce_sweeps=3,
        max_hold_s=604800.0, unquotable_exit_s=86400.0, mirror_on_sell=True,
    )
    contract = {
        "contract_version": 1,
        "identity": {"network_id": "solana", "mint": "FundedMint"},
        "classification": {
            "coin_band": "small", "lifecycle": "unknown",
            "liquidity_class": None, "momentum_phase": "climbing",
            "market_regime": "hot_narrow", "data_quality": "partial",
            "risk_scale": 0.75,
            "reason_codes": ["coin_band:small", "lifecycle:unknown"],
        },
        "sizing": {"base_clip_usd": 100.0, "risk_scale": 0.75,
                   "approved_clip_usd": 75.0},
        "policy": policy.to_dict(),
        "expectation": {"intent": "recover_principal_then_capture_trend"},
        "invalidations": ["post_peak_confirmed", "mirror_sell", "stop"],
    }
    payload, digest = m.canonical_funded_json(contract)
    if malformed:
        payload, digest = "{bad", "0" * 64
    return m.open_position(
        c, source_user_id="u", source_handle="(real)", mint="FundedMint", ticker="FUND",
        entry_price=1.0, entry_notional=75.0, entry_fill_source="chain",
        entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-18T12:00:00+00:00",
        strategy="alpha_b", execution_mode="real", network_id="solana",
        funded_contract_version=1, funded_contract_hash=digest,
        funded_contract_json=payload,
    )


def test_status_disarmed_reports_readiness(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", False)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_8453", "disabled")
    cl, c = _client(monkeypatch, tmp_path)
    d = cl.get("/api/memecoin/real/status").get_json()
    assert d["armed"] is False and d["kill_tripped"] is False
    assert d["real_open"]["count"] == 0 and d["real_open"]["notional"] == 0.0
    assert "solana" in d["real_open"]["by_network"]        # per-venue exposure breakdown present
    assert d["reservations"]["count"] == 0
    assert "solana" in d["venues"] and "8453" in d["venues"]
    assert d["venues"]["solana"]["keypair_present"] is False   # nonexistent path -> not present
    assert d["venues"]["solana"]["configured"] is False        # no rpc + no keypair
    assert d["venues"]["8453"]["execution_venue"] == "disabled"
    assert d["venues"]["8453"]["direct_configured"] is False
    assert d["venues"]["8453"]["configured"] is False
    assert "zerox_key_set" not in d["venues"]["8453"]
    assert d["envelope"]["trade_size"] == cfg.MEMECOIN_TRADE_SIZE_USD


def test_status_reports_the_global_slot_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 3)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_TOTAL", 6)
    cl, _c = _client(monkeypatch, tmp_path)

    envelope = cl.get("/api/memecoin/real/status").get_json()["envelope"]
    assert envelope["slot_scope"] == "global_cross_venue"
    assert envelope["max_concurrent"] == 6
    assert envelope["max_concurrent_by_chain"] == {"8453": 3, "solana": 3}


def test_status_publishes_fastlane_status(monkeypatch, tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    fastlane = {"status": "stale", "failure": None, "fallback_active": True}
    heartbeat.write_text(json.dumps({"ts": 1.0, "auth_ok": True, "fastlane": fastlane}))
    cl, _c = _client(
        monkeypatch, tmp_path, {"heartbeat_path": str(heartbeat)})

    assert cl.get("/api/memecoin/real/status").get_json()["fastlane"] == fastlane


def test_status_reads_lifi_only_from_runner_heartbeat_and_marks_stale(monkeypatch, tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    lifi = {
        "corrected_at": "2020-01-01T00:00:00+00:00",
        "runner_heartbeat_at": "2020-01-01T00:00:00+00:00",
        "release_sha": "a" * 40, "manifest_hash": "b" * 64,
        "chain_id": "4663", "custody_wallet": "0x" + "1" * 40,
        "mode": "authenticated_funded", "authenticated": True,
        "effective_limit": 12000, "remaining": 11999, "reset_s": 7200,
        "in_flight": 0, "exit_reserve": 7206, "slots_used": 1, "slot_limit": 1,
        "entry_admitted": False, "disable_reasons": ["slot_limit"],
        "unresolved_approvals": 0, "unresolved_swaps": 0,
        "unresolved_revokes": 0, "unresolved_finality": 0,
    }
    heartbeat.write_text(json.dumps({"ts": 1.0, "auth_ok": True, "lifi": lifi}))
    monkeypatch.setenv("MEMECOIN_LIFI_MODE", "disabled")
    cl, _c = _client(monkeypatch, tmp_path, {"heartbeat_path": str(heartbeat)})

    result = cl.get("/api/memecoin/real/status").get_json()["lifi"]

    assert result["mode"] == "authenticated_funded"
    assert result["stale"] is True
    assert result["disable_reasons"] == ["slot_limit"]


def test_status_does_not_infer_lifi_from_web_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_LIFI_MODE", "authenticated_funded")
    cl, _c = _client(monkeypatch, tmp_path)

    assert cl.get("/api/memecoin/real/status").get_json()["lifi"] is None


def test_status_reports_direct_4663_ready_only_with_route_rpc_and_wallet(monkeypatch, tmp_path):
    from sniper.execution.evm_wallet import generate_keypair_file

    cl, _c = _client(monkeypatch, tmp_path)
    keypair = str(tmp_path / "evm.json")
    generate_keypair_file(keypair)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663"})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", keypair)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://rpc")
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")

    venue = cl.get("/api/memecoin/real/status").get_json()["venues"]["4663"]

    assert venue["execution_venue"] == "direct"
    assert venue["direct_configured"] is True
    assert venue["configured"] is True
    assert venue["address"].startswith("0x")


def test_status_reflects_reservations_and_real_open(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    cl, c = _client(monkeypatch, tmp_path)
    _open_real(c, "So1Live", "solana", notional=10.0)
    m.reserve_pending(c, mint="So1Pend", source_user_id="u", strategy="alpha_b", notional=10.0,
                      reserved_at="t", network_id="solana")
    d = cl.get("/api/memecoin/real/status").get_json()
    assert d["armed"] is True
    assert d["real_open"]["count"] == 1 and abs(d["real_open"]["notional"] - 10.0) < 1e-9
    assert d["reservations"]["count"] == 1 and d["reservations"]["by_network"] == {"solana": 1}


def test_status_route_drops_json_serializable(monkeypatch, tmp_path):
    # network_drop_counts is keyed by (network, reason) tuples — the route MUST stringify them or
    # jsonify 500s. This regression was caught in a live preview, not the original unit test.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    cl, c = _client(monkeypatch, tmp_path)
    m.record_network_drop(c, "56", "0xbsc", "excluded", "t")
    m.record_network_drop(c, "8453", "0xnochain", "evm_unarmed", "t")
    r = cl.get("/api/memecoin/real/status")
    assert r.status_code == 200                                # not a 500 from tuple keys
    drops = r.get_json()["route_drops"]
    assert drops == {"56:excluded": 1, "8453:evm_unarmed": 1}  # string keys, "network:reason"


def test_status_shows_orphaned_reservation(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    cl, c = _client(monkeypatch, tmp_path)
    pid = m.reserve_pending(c, mint="M", source_user_id="u", strategy="alpha_b", notional=10.0,
                            reserved_at="t", network_id="solana")
    m.mark_pending_orphaned(c, pid)
    d = cl.get("/api/memecoin/real/status").get_json()
    assert d["reservations"]["orphaned"] == 1                  # needs-attention count surfaced


def test_real_positions_scoped_and_decorated(monkeypatch, tmp_path):
    from sniper.routes import api_memecoin as api
    now = "2026-07-09T00:00:00Z"
    monkeypatch.setattr(api, "_now_iso", lambda: now)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    cl, c = _client(monkeypatch, tmp_path)
    sol = _open_real(c, "So1Live", "solana", price=1.0)
    evm = _open_real(c, "0xLive", "8453", price=1.0)
    m.update_position_last_quote(c, sol, 1.5, now)            # +50% MTM from a timed quote
    m.set_position_last_executable(c, sol, 1.5, now)
    # a PAPER position must NOT appear in the real board
    m.open_position(c, source_user_id="u", source_handle="h", mint="PaperM", ticker="T",
                    entry_price=1.0, entry_notional=10.0, entry_fill_source="j", entry_lag_s=0.1,
                    their_size_usd=0.0, opened_at="t", strategy="alpha_b")
    d = cl.get("/api/memecoin/real/positions").get_json()
    mints = {p["mint"]: p for p in d["positions"]}
    assert set(mints) == {"So1Live", "0xLive"}                 # paper excluded
    assert mints["So1Live"]["venue"] == "SOL" and abs(mints["So1Live"]["pnl_pct"] - 50.0) < 1e-6
    assert mints["0xLive"]["venue"] == "EVM"


def test_real_positions_surfaces_frozen_contract_and_separate_effective_overlay(
    monkeypatch, tmp_path
):
    from sniper.routes import api_memecoin as api

    cl, c = _client(monkeypatch, tmp_path)
    pid = _open_funded(c)
    m.set_position_exit_override(c, pid, "operator", tp_pct=25.0, actor="operator")
    monkeypatch.setattr(api, "dexscreener_stats_batch_keyed", lambda _keys: {})
    monkeypatch.setattr(api, "gt_stats_batch_keyed", lambda _keys, **_kw: {})

    position = cl.get("/api/memecoin/real/positions").get_json()["positions"][0]

    contract = position["entry_contract"]
    assert position["policy_source"] == "funded_contract"
    assert position["operator_override_active"] is True
    assert contract["version"] == 1 and len(contract["hash"]) == 64
    assert contract["classification"]["lifecycle"] == "unknown"
    assert contract["classification"]["liquidity_class"] is None
    assert contract["sizing"] == {
        "base_clip_usd": 100.0, "risk_scale": 0.75, "approved_clip_usd": 75.0}
    assert contract["policy"]["tp_pct"] == 60.0
    assert position["effective_exit_policy"]["tp_pct"] == 25.0
    assert json.loads(c.execute(
        "SELECT funded_contract_json FROM memecoin_positions WHERE id=?", (pid,),
    ).fetchone()[0])["policy"]["tp_pct"] == 60.0


def test_real_positions_handles_malformed_contract_and_legacy_row_safely(monkeypatch, tmp_path):
    from sniper.routes import api_memecoin as api

    cl, c = _client(monkeypatch, tmp_path)
    _open_funded(c, malformed=True)
    _open_real(c, "LegacyMint", "solana")
    monkeypatch.setattr(api, "dexscreener_stats_batch_keyed", lambda _keys: {})
    monkeypatch.setattr(api, "gt_stats_batch_keyed", lambda _keys, **_kw: {})

    response = cl.get("/api/memecoin/real/positions")
    assert response.status_code == 200
    positions = {row["mint"]: row for row in response.get_json()["positions"]}
    malformed = positions["FundedMint"]
    assert malformed["policy_source"] == "funded_contract"
    assert malformed["entry_contract"]["version"] == 1
    assert malformed["entry_contract"]["hash"] == "0" * 64
    assert malformed["entry_contract"]["parse_error"]
    assert malformed["effective_exit_policy"] is None
    legacy = positions["LegacyMint"]
    assert legacy["policy_source"] == "legacy"
    assert legacy["entry_contract"] is None


def test_kill_switch_trip_and_clear(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_KILL_SWITCH_FILE", str(tmp_path / "kill"))
    cl, c = _client(monkeypatch, tmp_path)
    assert cl.post("/api/memecoin/real/kill", json={"action": "trip"}).get_json()["tripped"] is True
    assert cl.get("/api/memecoin/real/status").get_json()["kill_tripped"] is True
    assert cl.post("/api/memecoin/real/kill", json={"action": "clear",
        "expected_token": cl.get("/api/memecoin/real/status").get_json()["kill_token"]}).get_json()["tripped"] is False
    assert cl.post("/api/memecoin/real/kill", json={"action": "wat"}).status_code == 400


def test_real_closed_ledger_scoped(monkeypatch, tmp_path):
    cl, c = _client(monkeypatch, tmp_path)
    rp = _open_real(c, "0xClosed", "8453")
    m.close_position(c, rp, "2026-07-09T00:05:00Z", realized_pnl=7.0)
    pp = m.open_position(c, source_user_id="u", source_handle="h", mint="PaperC", ticker="T",
                         entry_price=1.0, entry_notional=10.0, entry_fill_source="j", entry_lag_s=0.1,
                         their_size_usd=0.0, opened_at="t", strategy="alpha_b")
    m.close_position(c, pp, "2026-07-09T00:06:00Z", realized_pnl=1.0)
    d = cl.get("/api/memecoin/real/closed").get_json()["closed"]
    assert [r["mint"] for r in d] == ["0xClosed"] and d[0]["venue"] == "EVM" and d[0]["realized_pnl"] == 7.0


def test_health_open_count_is_paper_scoped(monkeypatch, tmp_path):
    cl, c = _client(monkeypatch, tmp_path)
    _open_real(c, "So1Real", "solana")   # real
    m.open_position(c, source_user_id="u", source_handle="h", mint="PaperO", ticker="T",
                    entry_price=1.0, entry_notional=10.0, entry_fill_source="j", entry_lag_s=0.1,
                    their_size_usd=0.0, opened_at="t", strategy="alpha_b")  # paper
    assert cl.get("/api/memecoin/health").get_json()["open_count"] == 1   # paper only, not 2


def test_real_open_snapshot_network_filter(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)  # sets cfg; we use a raw conn for the db-level assert
    import sqlite3
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; m.ensure_memecoin_tables(c)
    _open_real(c, "So1", "solana"); _open_real(c, "0xE", "8453")
    assert m.real_open_snapshot(c)["count"] == 2                       # all venues
    assert m.real_open_snapshot(c, network="solana")["count"] == 1     # Solana only (gas accounting)
    assert m.real_open_snapshot(c, network="8453")["count"] == 1


def test_closed_positions_paper_scoped_by_default(monkeypatch, tmp_path):
    cl, c = _client(monkeypatch, tmp_path)
    rp = _open_real(c, "RealClosed", "solana")
    m.close_position(c, rp, "2026-07-09T00:05:00Z", realized_pnl=5.0)
    pp = m.open_position(c, source_user_id="u", source_handle="h", mint="PaperClosed", ticker="T",
                         entry_price=1.0, entry_notional=10.0, entry_fill_source="j", entry_lag_s=0.1,
                         their_size_usd=0.0, opened_at="t", strategy="alpha_b")
    m.close_position(c, pp, "2026-07-09T00:06:00Z", realized_pnl=1.0)
    # the paper closed view (default mode) must NOT show the real close
    assert {r["mint"] for r in m.list_closed_positions(c)} == {"PaperClosed"}
    assert {r["mint"] for r in m.list_closed_positions(c, mode="real")} == {"RealClosed"}


def test_real_positions_derisk_state_honest(monkeypatch, tmp_path):
    """The flat 'derisked' boolean lied for pos 6605 (unverified partial shown as recovered).
    derisk_state: confirmed (fill booked) / unknown (sig pending verify) / unbooked / None."""
    cl, c = _client(monkeypatch, tmp_path)
    p_none = _open_real(c, "MintA", "solana")
    p_conf = _open_real(c, "MintB", "solana")
    m.set_position_derisked(c, p_conf, "t1")
    m.insert_fill(c, position_id=p_conf, kind="derisk", price=0.5, qty=10.0, notional=5.0,
                  fill_source="chain", fraction=0.5, created_at="t1", tx_sig="SC")
    p_unk = _open_real(c, "MintC", "solana")
    m.set_position_derisked(c, p_unk, "t2")
    m.set_position_derisk_sig(c, p_unk, "SU")
    p_unb = _open_real(c, "MintD", "solana")
    m.set_position_derisked(c, p_unb, "t3")
    rows = {r["mint"]: r for r in cl.get("/api/memecoin/real/positions").get_json()["positions"]}
    assert rows["MintA"]["derisk_state"] is None
    assert rows["MintB"]["derisk_state"] == "confirmed"
    assert rows["MintC"]["derisk_state"] == "unknown"
    assert rows["MintD"]["derisk_state"] == "unbooked"


def test_real_positions_makes_no_external_price_call(monkeypatch, tmp_path):
    """SUPERSEDES test_real_positions_4663_uses_live_geckoterminal_over_stale_mark (operator,
    2026-08-28).

    That test pinned the endpoint fetching live GeckoTerminal for 4663. It was right in 2026-07-18,
    when the stored quote came from a 15s sweep and the operator's price lagged a minute. It is
    wrong now: the endpoint polls every 4s for a price the ledger already holds, and the fetch
    contends with the funded runner on a single-flight gate for ~0.5s per poll — its own comment
    said the real fix was the runner keeping the tape fresh.

    THE COST IS REAL AND IS ACCEPTED: 4663 market cap and liquidity now come from the mark tape,
    which is slower than a live GT fetch. Price freshness is unaffected because the runner's own
    stored quote is now primary; only the ENRICHMENT is slower, and it belongs off this path.
    """
    from sniper.routes import api_memecoin as api
    cl, c = _client(monkeypatch, tmp_path)
    _open_real(c, "0xRH", "4663", price=1.0, notional=50.0)
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, liquidity, mcap, quality) "
              "VALUES ('4663','0xRH','2026-07-09T00:00:00Z', 0.50, 1e9, 500000, 'live')")
    c.commit()

    def network_call(*_a, **_k):
        raise AssertionError("/real/positions must not call an external price provider")
    monkeypatch.setattr(api, "dexscreener_stats_batch_keyed", network_call)
    monkeypatch.setattr(api, "gt_stats_batch_keyed", network_call)

    rows = cl.get("/api/memecoin/real/positions").get_json()["positions"]
    p = {x["mint"]: x for x in rows}["0xRH"]
    # 2026-10-08 (operator via Alpha, Douyin 72229): an unlabelled provider row no longer prices or
    # sizes a funded card, so with no price of our own the market cap is unknown, not the provider's.
    assert p["market_cap"] is None


def test_real_positions_recovers_a_local_event_ticker(monkeypatch, tmp_path):
    cl, c = _client(monkeypatch, tmp_path)
    position_id = _open_real(c, "LocalNameMint", "solana")
    c.execute("UPDATE memecoin_positions SET ticker=NULL WHERE id=?", (position_id,))
    c.execute(
        "INSERT INTO memecoin_events"
        "(fomo_event_id,mint,network_id,ticker,side,created_at) "
        "VALUES('named-event','LocalNameMint','solana','NAMED','buy','2026-07-09T00:00:00Z')"
    )
    c.commit()

    row = cl.get("/api/memecoin/real/positions").get_json()["positions"][0]

    assert row["ticker"] == "NAMED"


def test_real_positions_4663_falls_back_to_mark_when_gt_misses(monkeypatch, tmp_path):
    from sniper.routes import api_memecoin as api
    cl, c = _client(monkeypatch, tmp_path)
    _open_real(c, "0xRH2", "4663", price=1.0, notional=50.0)
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, liquidity, mcap, quality) "
              "VALUES ('4663','0xRH2','2026-07-09T00:00:00Z', 0.50, 1e9, 500000, 'live')")
    c.commit()
    monkeypatch.setattr(api, "dexscreener_stats_batch_keyed", lambda keys: {})
    monkeypatch.setattr(api, "gt_stats_batch_keyed", lambda keys, **kw: {})       # GT misses
    p = {x["mint"]: x for x in cl.get("/api/memecoin/real/positions").get_json()["positions"]}["0xRH2"]
    # 2026-10-08: the mark-tape fallback reads only our own marks for a funded card; a provider row
    # is not one, so the market cap is unknown rather than the provider's.
    assert p["market_cap"] is None


def test_real_positions_does_not_retimestamp_a_stored_fallback(monkeypatch, tmp_path):
    from sniper.routes import api_memecoin as api

    cl, c = _client(monkeypatch, tmp_path)
    _open_real(c, "0xOld", "4663", price=1.0, notional=50.0)
    c.execute(
        "INSERT INTO memecoin_position_marks"
        "(chain_id,mint,sweep_ts,mid,liquidity,mcap,source,quality) "
        "VALUES('4663','0xOld','2026-07-09T00:00:00Z',0.50,1000,500,'fastlane_rpc','exact_route')"
    )
    c.commit()
    monkeypatch.setattr(api, "_now_iso", lambda: "2026-07-09T00:10:00+00:00")
    monkeypatch.setattr(api, "_cached_dex_batch", lambda _keys: {})
    monkeypatch.setattr(api, "gt_stats_batch_keyed", lambda _keys, **_kw: {})
    monkeypatch.setattr(api, "_WEB_MARK_WRITE", {"ts": 0.0})

    assert cl.get("/api/memecoin/real/positions").status_code == 200

    stamps = [row[0] for row in c.execute(
        "SELECT sweep_ts FROM memecoin_position_marks WHERE chain_id='4663' AND mint='0xOld'"
    )]
    assert stamps == ["2026-07-09T00:00:00Z"]
    ticker = cl.get("/api/memecoin/real/ticker").get_json()["positions"][0]
    # 2026-10-08: a ten-minute-old exact price is no current price for a funded EVM card; the card
    # says what it last knew and keeps the stored stamp, never re-dated.
    assert ticker["last_quote_price"] is None
    assert (ticker["last_known_price"], ticker["last_known_at"]) == (0.50, "2026-07-09T00:00:00Z")


def test_real_positions_reports_the_age_of_the_served_model(monkeypatch, tmp_path):
    from sniper.routes import api_memecoin as api

    cl, c = _client(monkeypatch, tmp_path)
    pid = _open_real(c, "0xAge", "4663", price=1.0, notional=50.0)
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=50.0,
                  notional=50.0, fill_source="chain", fraction=1.0,
                  created_at="2026-07-09T00:00:00Z")
    m.set_position_last_executable(c, pid, 0.6, "2026-07-09T00:00:00+00:00")
    monkeypatch.setattr(api, "_now_iso", lambda: "2026-07-09T00:10:00+00:00")
    monkeypatch.setattr(api, "_cached_dex_batch", lambda _keys: {
        ("4663", "0xage"): {
            "price": 0.8,
            "liquidity": 1000.0,
            "market_cap": 800.0,
            "source": "dexscreener",
        }
    })
    monkeypatch.setattr(api, "_WEB_MARK_WRITE", {"ts": float("inf")})

    row = cl.get("/api/memecoin/real/positions").get_json()["positions"][0]

    # UPDATED 2026-08-28: the injected `_cached_dex_batch` is no longer called, so the served model
    # is the last EXACT route quote — recorded at 00:00:00 and now ten minutes old. The point of
    # this test is unchanged and better demonstrated: the endpoint must report the age of what it
    # actually served. A fresh-looking 0.0s over a ten-minute-old route is the lie it guards.
    assert row["mark_basis"] == "measured_route_stale"
    assert row["mark_observed_at"] == "2026-07-09T00:00:00+00:00"
    assert row["mark_age_s"] == 600.0


def test_real_ticker_uses_fresh_local_mark_without_network_calls(monkeypatch, tmp_path):
    """The 1s card path must follow the runner tape, not the slower external display caches."""
    from sniper.routes import api_memecoin as api

    cl, c = _client(monkeypatch, tmp_path)
    pid = _open_real(c, "0xFast", "4663", price=1.0, notional=50.0)
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=50.0, notional=50.0,
                  fill_source="chain", fraction=1.0, created_at="2026-07-09T00:00:00Z")
    m.update_position_last_quote(c, pid, 0.6, "2026-07-09T00:00:00Z")
    # 2026-10-08 (operator via Alpha): a funded EVM card prices only from our own marks, so the fresh
    # price here is an exact route mark, and the peak is the executable peak.
    c.execute("UPDATE memecoin_positions SET entry_mcap=1000, peak_price=1.25, "
              "executable_peak_price=1.25, max_gain_pct=NULL WHERE id=?", (pid,))
    c.execute("INSERT INTO memecoin_position_marks"
              "(chain_id, mint, sweep_ts, mid, liquidity, mcap, quality, source) VALUES "
              "('4663','0xFast','2026-07-09T00:00:01Z',0.7,1000,700,'live','old'),"
              "('4663','0xFast','2026-07-09T00:00:02Z',1.2,1000,NULL,'exact_route','fastlane_rpc')")
    c.commit()

    def network_call(*_args, **_kwargs):
        raise AssertionError("ticker endpoint must not call external price providers")

    monkeypatch.setattr(api, "dexscreener_stats_batch_keyed", network_call)
    monkeypatch.setattr(api, "gt_stats_batch_keyed", network_call)
    monkeypatch.setattr(api, "_now_iso", lambda: "2026-07-09T00:00:03Z")
    response = cl.get("/api/memecoin/real/ticker")

    assert response.status_code == 200
    ticker = response.get_json()["positions"][0]
    expected_exit = 1.2                      # an exact route price is measured: no model on top
    # THE RUNNER'S STORED QUOTE IS PRIMARY (operator, 2026-08-28). This asserted the modelled exit
    # under the name `last_quote_price`, which is how live position 44794 displayed
    # 0.000188049267086 while the runner was deciding on 0.000195614571079. The fixture's runner
    # quote is 0.6 and the tape implies 1.128; the operator must see 0.6, because that is the
    # number the rails compare against. The modelled figure is still published, under its own name.
    expected = {
        "id": pid,
        "last_quote_price": 0.6,
        "price_source": "runner",
        # EACH PRICE WITH ITS OWN CLOCK AND BASIS. The runner committed 0.6 at 00:00:00; the tape's
        # selection is a modelled 1.128 observed at 00:00:02. Publishing the runner number under
        # the tape's timestamp was the fault the operator caught in the first pass.
        "runner_decision_price": 0.6,
        "runner_decision_at": "2026-07-09T00:00:00Z",
        "market_mid_price": 1.2,
        "market_mid_at": "2026-07-09T00:00:02Z",
        "selected_exit_price": expected_exit,
        "selected_exit_basis": "measured_route",
        "selected_exit_at": "2026-07-09T00:00:02Z",
        "last_executable_price": None,
        "last_executable_at": None,
        "modeled_exit_price": None,              # an exact selection is not modelled
        "pnl_price_basis": "measured_route",
        # THE LEGACY PAIR NOW AGREES WITH ITSELF. last_quote_price is the runner's 0.6, so
        # last_quote_ts is the runner's 00:00:00 — not the tape's 00:00:02. A reader that knows
        # only the legacy pair gets one quantity with its own clock.
        "last_quote_ts": "2026-07-09T00:00:00Z",
        # The basis and the observation time of THIS price ride with it, so the client cannot
        # merge a tape mid under the previous poll's `measured_route` label and age.
        "mark_basis": "measured_route",
        "mark_observed_at": "2026-07-09T00:00:02Z",
        "mark_age_s": 1.0,
        "liquidity": 1000.0,
        "market_cap": 1200.0,
        "max_gain_pct": 25.0,
        "pnl_pct": (expected_exit - 1.0) * 100.0,
    }
    assert {key: ticker[key] for key in expected} == expected


def test_real_price_surfaces_ignore_the_solana_state_estimate(monkeypatch, tmp_path):
    """The reserve estimate is evidence, but it is not a runner or UI price."""
    from sniper.routes import api_memecoin as api

    cl, c = _client(monkeypatch, tmp_path)
    pid = _open_real(c, "StateEstimate", "solana", price=0.001, notional=10.0)
    m.insert_fill(c, position_id=pid, kind="entry", price=0.001, qty=10_000.0,
                  notional=10.0, fill_source="chain", fraction=1.0,
                  created_at="2026-08-28T12:00:00Z")
    m.update_position_last_quote(c, pid, 0.0009562, "2026-08-28T12:00:02Z")
    c.execute("UPDATE memecoin_positions SET entry_mcap=1000000 WHERE id=?", (pid,))
    c.execute(
        "INSERT INTO memecoin_position_marks"
        "(chain_id,mint,sweep_ts,mid,mcap,source,quality) VALUES"
        "('solana','StateEstimate','2026-08-28T12:00:02Z',0.0009562,956200,"
        " 'fastlane_mid','live'),"
        "('solana','StateEstimate','2026-08-28T12:00:03Z',0.00325,NULL,"
        " 'fastlane_sol_state','live')"
    )
    c.commit()
    monkeypatch.setattr(api, "_now_iso", lambda: "2026-08-28T12:00:04Z")

    ticker = cl.get("/api/memecoin/real/ticker").get_json()["positions"][0]
    position = cl.get("/api/memecoin/real/positions").get_json()["positions"][0]

    assert ticker["last_quote_price"] == 0.0009562
    assert ticker["selected_exit_price"] == 0.0009562
    # The state estimate is still not a price. Since 2026-09-27 the DexScreener mid (and the market
    # cap it carried) is not a Solana market price either: no on-chain market price, no market cap.
    assert ticker["market_cap"] is None
    assert position["last_quote_price"] == 0.0009562
    assert position["market_cap"] == ticker["market_cap"]
    # The sweep timestamp is not a price observation. With no eligible mark or exact quote,
    # retain its decision price as evidence but do not present current PnL.
    assert ticker["pnl_pct"] is position["pnl_pct"] is None


def test_status_knobs_strip_reports_the_effective_override_not_the_raw_setting(monkeypatch, tmp_path):
    """Live 2026-09-11: the strip read "follow <=86,400s" while the runner enforced a 60 s override
    set in the knobs panel directly below it."""
    client, c = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FOLLOW_MAX_AGE_S", 86400.0)
    m.set_entry_knob_override(c, "MEMECOIN_REAL_FOLLOW_MAX_AGE_S", 60, active=True,
                              actor="t", reason="", now_iso="2026-09-11T00:00:00+00:00")
    knobs = client.get("/api/memecoin/real/status").get_json()["knobs"]
    assert knobs["follow_max_age_s"] == 60
    assert knobs["entry_max_age_s"] == cfg.MEMECOIN_REAL_ENTRY_MAX_AGE_S


def test_status_exposes_the_drawdown_pause(monkeypatch, tmp_path):
    cl, conn = _client(monkeypatch, tmp_path)
    until = "2026-09-16T10:00:00+00:00"
    monkeypatch.setattr(m, "drawdown_pause_until", lambda *a: until)
    daily = cl.get("/api/memecoin/real/status").get_json()["daily"]
    assert daily["drawdown_pause_until"] == until
    assert daily["pause_reason"] == "drawdown"


def test_position_view_exposes_size_mirror_overlay_and_operator_hold(monkeypatch):
    from sniper.routes.api_memecoin import _position_contract_view
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_CHAINS", {"solana"})
    p = {"network_id": "solana", "mirror_disabled": False}
    active = _position_contract_view(p, None)["effective_mirror"]
    assert active == {"source": "size_aware", "enabled": True, "operator_hold": False}
    p["mirror_disabled"] = True
    assert _position_contract_view(p, None)["effective_mirror"] == {
        "source": "size_aware", "enabled": False, "operator_hold": True}


def test_size_mirror_overlay_does_not_rewrite_the_frozen_contract(monkeypatch, tmp_path):
    from sniper.routes.api_memecoin import _position_contract_view
    cl, conn = _client(monkeypatch, tmp_path)
    pid = _open_funded(conn)
    position = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    contract = json.loads(position["funded_contract_json"])
    contract["policy"]["mirror_on_sell"] = False
    payload, digest = m.canonical_funded_json(contract)
    position.update(funded_contract_json=payload, funded_contract_hash=digest)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_CHAINS", {"solana"})
    view = _position_contract_view(position, None)
    assert view["effective_mirror"]["enabled"] is True
    assert view["effective_exit_policy"]["mirror_on_sell"] is False
    assert view["frozen_policy"]["mirror_on_sell"] is False
    assert position["funded_contract_json"] == payload


@pytest.mark.parametrize("default_limit,override_limit", [(250, 0), (0, 250)])
def test_status_daily_breaker_uses_effective_limit_and_preserves_latch(
        monkeypatch, tmp_path, default_limit, override_limit):
    client, conn = _client(monkeypatch, tmp_path)
    day = "2026-09-18"
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: day + "T09:56:00+00:00")
    monkeypatch.setattr(cfg, "MEMECOIN_RISK_DAY_START_AT", "")
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", default_limit)
    pid = _open_real(conn, "loss", "solana")
    m.close_position(conn, pid, day + "T09:55:00+00:00", realized_pnl=-300)
    assert m.check_and_latch_daily_loss(conn, day, 250)
    m.set_entry_knob_override(conn, "MEMECOIN_DAILY_LOSS_LIMIT_USD", override_limit,
                             active=True, actor="test", reason="", now_iso="now")
    daily = client.get("/api/memecoin/real/status").get_json()["daily"]
    assert daily["limit"] == override_limit
    assert daily["tripped"] is (override_limit > 0)
    # THE LATCH STAYS VISIBLE EVEN WHEN THE BREAKER IS OFF (Alpha, B2). "tripped" above correctly
    # ignores the latch once the limit is 0, but that must not make the latch itself disappear
    # from the cockpit -- a stale trip from earlier today should read as un-flagged, not absent.
    assert daily["latched_today"] is True
    assert m.is_daily_loss_tripped(conn, day)
    assert cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD == default_limit


@pytest.mark.parametrize("default_drop,drop,pause_s,expected", [
    (100, 0, 3600, None), (0, 100, 1800, "2026-09-18T10:25:00+00:00"),
    (100, 100, 0, None),
])
def test_status_drawdown_uses_effective_amount_and_duration(
        monkeypatch, tmp_path, default_drop, drop, pause_s, expected):
    client, conn = _client(monkeypatch, tmp_path)
    day = "2026-09-18"
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: day + "T09:56:00+00:00")
    monkeypatch.setattr(cfg, "MEMECOIN_RISK_DAY_START_AT", "")
    monkeypatch.setattr(cfg, "MEMECOIN_DRAWDOWN_PAUSE_USD", default_drop)
    monkeypatch.setattr(cfg, "MEMECOIN_DRAWDOWN_PAUSE_S", 3600)
    pid = _open_real(conn, "loss", "solana")
    m.close_position(conn, pid, day + "T09:55:00+00:00", realized_pnl=-150)
    for name, value in (("MEMECOIN_DRAWDOWN_PAUSE_USD", drop), ("MEMECOIN_DRAWDOWN_PAUSE_S", pause_s)):
        m.set_entry_knob_override(conn, name, value, active=True, actor="test", reason="", now_iso="now")
    daily = client.get("/api/memecoin/real/status").get_json()["daily"]
    assert daily["drawdown_pause_until"] == expected
    assert daily["pause_reason"] == ("drawdown" if expected else None)
    assert cfg.MEMECOIN_DRAWDOWN_PAUSE_USD == default_drop
    assert cfg.MEMECOIN_DRAWDOWN_PAUSE_S == 3600
