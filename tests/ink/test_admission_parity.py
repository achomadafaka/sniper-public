from sniper.services import memecoin_evm_risk
from sniper.settings import memecoin as cfg
import sqlite3


def test_ink_requires_exact_clip_reverse_quote():
    assert "57073" in cfg.MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS


def test_ink_losing_reentry_refuses_before_reservation(monkeypatch, ink_flow):
    from sniper.persistence import db_memecoin as db
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BLOCK_LOSING_REENTRY", True)
    monkeypatch.setattr(db, "losing_real_coin_ids",
                        lambda *_args, **_kwargs: frozenset({("57073", "0x" + "b" * 40)}))
    r = ink_flow("losing_reentry")
    assert r["result"]["status"] == "rejected"
    assert "losing re-entry" in r["result"]["reason"]
    assert (r["approve_calls"], r["sign_calls"], r["send_calls"]) == (0, 0, 0)
    assert r["pending_rows"] == 0


def test_ink_unknown_age_recheck_refuses_before_approval(monkeypatch, ink_flow):
    from sniper.services import memecoin_coin_age
    monkeypatch.setattr(memecoin_coin_age, "unknown_pair_check_enabled", lambda: True)
    monkeypatch.setattr(memecoin_coin_age, "cached_age_s", lambda *_args: 1.0)
    monkeypatch.setattr(cfg, "min_coin_age_s_for_chain", lambda _chain: 120.0)
    r = ink_flow("coin_age_presign")
    assert r["result"]["status"] in ("unsafe", "aborted")
    assert "coin_too_young" in r["result"]["reason"]
    assert (r["approve_calls"], r["sign_calls"], r["send_calls"]) == (0, 0, 0)


def test_real_evm_executor_rechecks_after_approval_before_swap_sign():
    from sniper.execution.evm_swap_executor import execute_entry_evm
    from sniper.persistence import db_memecoin as db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    pending_id = db.reserve_pending(
        conn, mint="0x" + "b" * 40, source_user_id="source", strategy="alpha_b",
        notional=10.0, reserved_at="2026-09-29T09:00:00Z", network_id="57073")
    calls = {"approval": 0, "sign": 0, "send": 0, "age": 0}
    def approve(_spender, _amount):
        calls["approval"] += 1
        return True
    def sign(_tx):
        calls["sign"] += 1
        return ("0xraw", "0x" + "a" * 64, 1)
    def send(_raw):
        calls["send"] += 1
        return "accepted"
    def age():
        calls["age"] += 1
        return "coin_too_young" if calls["age"] == 2 else None
    quote = {"sellAmount": "10000000", "transaction": {"to": "0xrouter", "data": "0xdead"},
             "issues": {"allowance": {"spender": "0xspender"}}}
    result = execute_entry_evm(
        conn, pending_id=pending_id, mint="0x" + "b" * 40, network_id="57073",
        usd_amount=10.0, strategy="alpha_b", source_user_id="source",
        now_iso="2026-09-29T09:00:01Z", quote_fn=lambda *_a, **_k: quote,
        approve_fn=approve, sign_fn=sign, send_fn=send,
        confirm_fn=lambda _hash: "confirmed", usdc_fn=lambda: 100.0,
        token_fn=lambda _mint: 0.0, presign_check_fn=age)
    assert result["status"] == "unsafe_gate"
    assert calls == {"approval": 1, "sign": 0, "send": 0, "age": 2}


def test_enabled_cluster_gate_cannot_be_skipped_by_ink_route(monkeypatch):
    from types import SimpleNamespace
    from sniper.services import memecoin_real_leg
    from sniper.persistence import db_memecoin as db
    monkeypatch.setattr(memecoin_real_leg, "_cluster_gate_enabled", lambda: True)
    monkeypatch.setattr(memecoin_real_leg, "real_entries_allowed", lambda _path: True)
    monkeypatch.setattr(memecoin_real_leg._arming, "in_quiet_hours", lambda: False)
    monkeypatch.setattr(cfg, "ink_capability_reason", lambda: None)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    ev = SimpleNamespace(mint="0x" + "b" * 40, network_id="57073",
                         signal_source="copy", source_user_id="source")
    called = []
    result = memecoin_real_leg.real_entry(
        conn, ev, day="2026-09-29", usdc_balance=0, sol_balance=0,
        now_iso="2026-09-29T09:00:00Z", trade_size=10,
        token_safety_fn=lambda _mint: (True, "ok"), execute_fn=lambda *_a: None,
        evm_allowlist={"57073"}, evm_entry_fn=lambda *_a, **_k: called.append(True),
        resolved_identity=SimpleNamespace(network_id="57073"))
    assert result["status"] == "rejected"
    assert "cluster" in result["reason"]
    assert called == []
