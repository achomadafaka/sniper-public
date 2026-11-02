"""4663 (Robinhood) real-entry routing is live end-to-end (2026-07-12). The WETH executor already
exists (test_memecoin_evm_quote_token); this proves a 4663 buy now ROUTES to it instead of being
dropped, once 4663 is in MEMECOIN_EVM_ALLOWLIST. Fail-safe unfunded (0 WETH bankroll -> 0 sizing)."""
import types

from sniper.services import memecoin_real_leg as RL


def _ev(mint="0xRobinToken", network_id="4663"):
    return types.SimpleNamespace(mint=mint, network_id=network_id,
                                 source_user_id="u1", side="buy",
                                 created_at="2026-07-12T00:00:00Z")


def test_4663_in_default_allowlist():
    from sniper.settings import memecoin as cfg
    assert "4663" in cfg.MEMECOIN_EVM_ALLOWLIST          # enabled by default now


def test_route_4663_evm_when_allowlisted():
    r = RL.route_real_entry(_ev(), {"8453", "4663"}, resolve_fn=lambda m: "4663")
    assert r == "evm:4663"
    # not allowlisted -> drop (held out)
    assert RL.route_real_entry(_ev(), {"8453"}, resolve_fn=lambda m: "4663") == "drop"
    # a Solana mint always routes to solana regardless
    assert RL.route_real_entry(_ev(mint="So1Base58Mint", network_id="solana"), {"8453"}) == "solana"


def test_real_entry_dispatches_4663_to_evm_leg(monkeypatch):
    monkeypatch.setattr(RL, "real_entries_allowed", lambda kill_path=None: True)   # armed
    seen = {}

    def evm_entry_fn(conn, ev, *, route, **kw):
        seen["route"] = route
        return {"status": "filled", "route": route}

    res = RL.real_entry(
        None, _ev(), day="2026-07-12", usdc_balance=0.0, sol_balance=0.0,
        now_iso="2026-07-12T00:00:00Z", trade_size=30.0,
        token_safety_fn=lambda *a, **k: (True, {}), execute_fn=lambda *a, **k: {},
        evm_allowlist={"8453", "4663"}, evm_entry_fn=evm_entry_fn, resolve_fn=lambda m: "4663")
    assert res["status"] == "filled" and res["route"] == "evm:4663"
    assert seen["route"] == "evm:4663"                  # 4663 buy reached the EVM (WETH) executor


def test_real_entry_4663_evm_unarmed_without_leg(monkeypatch):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(RL, "real_entries_allowed", lambda kill_path=None: True)
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)                       # record_network_drop needs the table
    res = RL.real_entry(
        conn, _ev(), day="d", usdc_balance=0.0, sol_balance=0.0,
        now_iso="2026-07-12T00:00:00Z", trade_size=30.0,
        token_safety_fn=lambda *a, **k: (True, {}), execute_fn=lambda *a, **k: {},
        evm_allowlist={"8453", "4663"}, evm_entry_fn=None, resolve_fn=lambda m2: "4663")
    assert res["status"] == "evm_unarmed"               # leg not wired -> clean pass, no send


def test_4663_entry_stops_for_an_unresolved_signed_transaction(monkeypatch):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(RL, "real_entries_allowed", lambda kill_path=None: True)
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pending_id = m.reserve_pending(
        conn, mint="0xprior", source_user_id="u1", strategy="alpha_b",
        notional=30.0, reserved_at="2026-07-12T00:00:00Z", network_id="4663",
    )
    m.set_pending_sig(conn, pending_id, "0xpriorhash", "2026-07-12T00:00:00Z")

    res = RL.real_entry(
        conn, _ev(), day="2026-07-12", usdc_balance=300.0, sol_balance=0.0,
        now_iso="2026-07-12T00:00:01Z", trade_size=30.0,
        token_safety_fn=lambda *_a, **_k: (True, {}), execute_fn=lambda *_a, **_k: {},
        evm_allowlist={"4663"}, evm_entry_fn=lambda *_a, **_k: {"status": "filled"},
    )

    assert res["status"] == "rejected"
    assert res["reason"] == "unresolved signed transaction on 4663"
