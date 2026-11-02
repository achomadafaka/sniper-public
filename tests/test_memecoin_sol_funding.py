"""Native SOL entry evidence is tied to one signed receipt and keeps fees out of token cost."""

from sniper.execution import solana_swap_executor as executor
from sniper.persistence import db_memecoin as m


def test_reservations_keep_usdc_and_sol_encumbrance_separate(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(conn)
    common = dict(mint="coin-a", source_user_id="wallet", strategy="real",
                  reserved_at="2026-09-27T15:00:00Z", network_id="solana")
    m.reserve_pending(conn, **common, notional=100.0, funding_asset="sol",
                      funding_amount_atomic=1_000_000_000, funding_sol_usd=100.0,
                      funding_overhead_atomic=5_000_000)
    m.reserve_pending(conn, **dict(common, mint="coin-b"), notional=50.0)
    assert m.pending_funding_totals(conn, "solana") == (50.0, 1_005_000_000)
    assert m.pending_totals(conn, "solana") == (2, 150.0)
    from sniper.services.memecoin_gas_topup import free_usdc
    assert free_usdc(conn, 200.0) == 150.0


def test_old_gas_signature_does_not_encumber_fresh_usdc_forever(tmp_path):
    import sqlite3
    from sniper.util.clock import real_utc_now_iso
    conn = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(conn)
    kw = dict(usdc_spent=1.0, sol_received=None, sol_before=0.1,
              status="submitted", purpose="gas")
    m.record_gas_topup(conn, **kw, tx_sig="old", day="2026-09-01",
                       now_iso="2026-09-01T00:00:00+00:00")
    assert m.pending_funding_totals(conn, "solana") == (0.0, 0)
    now = real_utc_now_iso()
    m.record_gas_topup(conn, **kw, tx_sig="recent", day=now[:10], now_iso=now)
    assert m.pending_funding_totals(conn, "solana") == (1.0, 0)


def test_buffer_topup_ledger_is_separate_and_unresolved_submission_blocks_replenishment(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(conn)
    kw = dict(now_iso="2026-09-27T15:00:00Z", day="2026-09-27", usdc_spent=100,
              sol_received=None, sol_before=0.1, tx_sig="sig", status="submitted")
    m.record_gas_topup(conn, **kw, purpose="buffer")
    assert m.gas_topup_spent_today(conn, "2026-09-27", purpose="buffer") == 100
    assert m.gas_topup_spent_today(conn, "2026-09-27", purpose="gas") == 0
    assert m.unresolved_gas_topup(conn, purpose="buffer") == "sig"
    assert m.pending_funding_totals(conn, "solana") == (100.0, 0)
    m.record_gas_topup(conn, **{**kw, "usdc_spent": 0, "status": "confirmed",
                                "sol_received": 2.0}, purpose="buffer")
    assert m.unresolved_gas_topup(conn, purpose="buffer") is None
    assert m.pending_funding_totals(conn, "solana") == (0.0, 0)


def test_proven_failed_buffer_swap_releases_daily_budget_but_unknown_does_not(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(conn)
    kw = dict(now_iso="2026-09-27T15:00:00Z", day="2026-09-27", usdc_spent=100,
              sol_received=None, sol_before=0.1, status="submitted", purpose="buffer")
    m.record_gas_topup(conn, **kw, tx_sig="failed_sig")
    m.record_gas_topup(conn, **{**kw, "usdc_spent": 0, "status": "failed"}, tx_sig="failed_sig")
    m.record_gas_topup(conn, **kw, tx_sig="unknown_sig")
    assert m.gas_topup_spent_today(conn, "2026-09-27", purpose="buffer") == 100


def test_buffer_replenishment_prepares_off_lock_and_does_not_repeat_unknown_send(tmp_path):
    import sqlite3
    from sniper.execution.exec_lock import wallet_exec_lock
    from sniper.services.memecoin_gas_topup import buffer_topup_once
    conn = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(conn)
    observed = []

    def quote(*_args):
        with wallet_exec_lock("sol-owner", blocking=False, lock_dir=str(tmp_path)) as held:
            observed.append(held)
        return {"outAmount": "1000000000"}

    kw = dict(sol_balance=0.1, usdc_balance=300.0, sol_usd=100.0,
              keypair=object(), pubkey="owner", rpc_url="rpc", now_iso="2026-09-27T15:00:00Z",
              cap_usd=150.0, topup_usd=100.0, daily_cap_usd=200.0,
              min_usdc_remaining_usd=150.0, lock_dir=str(tmp_path),
              sol_balance_fn=lambda: 0.1, usdc_balance_fn=lambda: 300.0,
              quote_fn=quote, build_fn=lambda *_a: "tx", sign_fn=lambda *_a: ("signed", "sig"),
              send_fn=lambda *_a: "sig", confirm_fn=lambda *_a: "unknown")
    first = buffer_topup_once(conn, **kw)
    assert observed == [True] and first["state"] == "unknown"
    assert m.pending_funding_totals(conn, "solana") == (100.0, 0)
    second = buffer_topup_once(conn, **kw)
    assert second["state"] == "unresolved_topup" and observed == [True]


def test_confirmed_buffer_topup_stays_encumbered_until_usdc_read_catches_up(tmp_path):
    import sqlite3
    from sniper.services.memecoin_gas_topup import buffer_topup_once
    conn = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(conn)
    reads = iter([300.0, 300.0, 200.0])
    kw = dict(sol_balance=0.1, usdc_balance=300.0, sol_usd=100.0,
              keypair=object(), pubkey="owner", rpc_url="rpc", now_iso="2026-09-27T15:00:00Z",
              cap_usd=150.0, topup_usd=100.0, daily_cap_usd=200.0,
              min_usdc_remaining_usd=150.0, lock_dir=str(tmp_path),
              sol_balance_fn=lambda: 0.1, usdc_balance_fn=lambda: next(reads),
              quote_fn=lambda *_a: {"outAmount": "1000000000"},
              build_fn=lambda *_a: "tx", sign_fn=lambda *_a: ("signed", "sig"),
              send_fn=lambda *_a: "sig", confirm_fn=lambda *_a: "confirmed")
    first = buffer_topup_once(conn, **kw)
    assert first["state"] == "confirmed_balance_pending"
    assert m.pending_funding_totals(conn, "solana") == (100.0, 0)
    second = buffer_topup_once(conn, **kw)
    assert second["state"] == "prior_confirmed"
    assert m.pending_funding_totals(conn, "solana") == (0.0, 0)


def test_explicit_buffer_preflight_rejection_closes_only_that_signed_attempt(tmp_path):
    import sqlite3
    from sniper.services.memecoin_gas_topup import buffer_topup_once
    conn = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(conn)
    result = buffer_topup_once(
        conn, sol_balance=0.1, usdc_balance=300.0, sol_usd=100.0,
        keypair=object(), pubkey="owner", rpc_url="rpc", now_iso="2026-09-27T15:00:00Z",
        cap_usd=150.0, topup_usd=100.0, daily_cap_usd=200.0,
        min_usdc_remaining_usd=150.0, lock_dir=str(tmp_path),
        sol_balance_fn=lambda: 0.1, usdc_balance_fn=lambda: 300.0,
        quote_fn=lambda *_a: {"outAmount": "1000000000"},
        build_fn=lambda *_a: "tx", sign_fn=lambda *_a: ("signed", "sig"),
        send_fn=lambda *_a: "explicit_rejected", confirm_fn=lambda *_a: "not_found")
    assert result["state"] == "failed"
    assert m.unresolved_gas_topup(conn, purpose="buffer") is None
    assert m.pending_funding_totals(conn, "solana") == (0.0, 0)


def test_buffer_replenishment_preserves_usdc_majority_and_cap(tmp_path):
    import sqlite3
    from sniper.services.memecoin_gas_topup import buffer_topup_once
    conn = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(conn)
    kw = dict(sol_balance=0.1, usdc_balance=200.0, sol_usd=100.0,
              keypair=None, pubkey="owner", rpc_url="rpc", now_iso="2026-09-27T15:00:00Z",
              cap_usd=120.0, topup_usd=100.0, daily_cap_usd=200.0,
              min_usdc_remaining_usd=120.0)
    assert buffer_topup_once(conn, **kw)["state"] == "insufficient_free_usdc"
    # CAP is spendable buy capital; the native balance must also cover fees/rent.
    assert buffer_topup_once(conn, **{**kw, "usdc_balance": 300.0, "sol_balance": 0.4,
                                   "cap_usd": 30.0})["state"] == "buffer_cap"


def test_buffer_refill_uses_available_headroom_after_native_buy(tmp_path):
    import sqlite3
    from sniper.services.memecoin_gas_topup import buffer_topup_once
    conn = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(conn)
    quoted = []
    result = buffer_topup_once(
        conn, sol_balance=0.63, usdc_balance=400.0, sol_usd=100.0,
        keypair=object(), pubkey="owner", rpc_url="rpc", now_iso="2026-09-27T15:00:00Z",
        cap_usd=175.0, topup_usd=130.0, daily_cap_usd=260.0,
        min_usdc_remaining_usd=175.0, lock_dir=str(tmp_path),
        sol_balance_fn=lambda: 0.63, usdc_balance_fn=lambda: 400.0,
        quote_fn=lambda *_a: quoted.append(_a[2]) or {"outAmount": "1000000000"},
        build_fn=lambda *_a: "tx", sign_fn=lambda *_a: ("signed", "sig"),
        send_fn=lambda *_a: "sig", confirm_fn=lambda *_a: "unknown")
    assert result["state"] == "unknown"
    # $175 buy capital + $2.211784 default fees/rent + $5 cushion, less $63 held.
    assert quoted == [119_220_000]
    assert m.pending_funding_totals(conn, "solana") == (119.22, 0)


def test_two_entries_cannot_reserve_the_same_native_balance(tmp_path, monkeypatch):
    import sqlite3
    from sniper.services.memecoin_risk import reserve_entry
    from sniper.settings import memecoin as cfg
    conn = sqlite3.connect(tmp_path / "ledger.db")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    params = dict(source_user_id="wallet", strategy="alpha_b", day="2026-09-27",
                  usdc_balance=200.0, sol_balance=0.7, now_iso="2026-09-27T15:00:00Z",
                  size_override=50.0, funding_asset="sol", funding_sol_usd=100.0,
                  sol_buffer_cap_usd=175.0)
    first, first_id = reserve_entry(conn, mint="coin-a", **params)
    second, second_id = reserve_entry(conn, mint="coin-b", **params)
    assert first.allowed and first_id is not None
    assert m.pending_row(conn, first_id)["funding_amount_atomic"] == "500000000"
    assert not second.allowed and second_id is None
    assert "SOL buffer" in second.reason


def _receipt():
    return {
        "transaction": {"signatures": ["sig"], "message": {"accountKeys": [
            {"pubkey": "owner", "signer": True}, {"pubkey": "coin-ata", "signer": False}]}},
        "meta": {"err": None, "fee": 100_000,
                 "preBalances": [2_000_000_000, 0],
                 "postBalances": [1_200_000_000, 2_039_280],
                 "preTokenBalances": [],
                 "postTokenBalances": [{"accountIndex": 1, "mint": "coin", "owner": "owner",
                                        "uiTokenAmount": {"amount": "1000000", "decimals": 6}}]},
    }


def test_native_fill_excludes_network_fee_and_new_token_account_rent():
    fill = executor.receipt_sol_entry_fill(_receipt(), signature="sig", owner="owner", mint="coin",
                                  maximum_lamports=800_000_000)
    assert fill == {"sol_spent_lamports": 797_860_720, "got": 1.0, "decimals": 6,
                    "fee_lamports": 100_000, "rent_lamports": 2_039_280}


def test_native_fill_requires_complete_signed_evidence_and_bound():
    tx = _receipt()
    assert executor.receipt_sol_entry_fill(tx, signature="other", owner="owner", mint="coin",
                                  maximum_lamports=800_000_000) is None
    assert executor.receipt_sol_entry_fill(tx, signature="sig", owner="owner", mint="coin",
                                  maximum_lamports=700_000_000) is None
    tx["meta"].pop("fee")
    assert executor.receipt_sol_entry_fill(tx, signature="sig", owner="owner", mint="coin",
                                  maximum_lamports=800_000_000) is None


def test_native_fill_keeps_new_wsol_rent_out_of_token_cost_and_refuses_missing_prestate():
    tx = _receipt()
    tx["transaction"]["message"]["accountKeys"].append({"pubkey": "wsol-ata", "signer": False})
    tx["meta"]["preBalances"].append(0)
    tx["meta"]["postBalances"].append(2_039_280)
    tx["meta"]["postTokenBalances"].append({
        "accountIndex": 2, "mint": "So11111111111111111111111111111111111111112",
        "owner": "owner", "uiTokenAmount": {"amount": "0", "decimals": 9}})
    fill = executor.receipt_sol_entry_fill(tx, signature="sig", owner="owner", mint="coin",
                                          maximum_lamports=800_000_000)
    assert fill["sol_spent_lamports"] == 795_821_440
    assert fill["rent_lamports"] == 4_078_560
    tx["meta"]["preBalances"][1] = 1_000
    assert executor.receipt_sol_entry_fill(tx, signature="sig", owner="owner", mint="coin",
                                           maximum_lamports=800_000_000) is None


def test_priority_fee_is_decoded_from_the_signed_compute_budget():
    import base64
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    signer = Keypair()
    message = MessageV0.try_compile(signer.pubkey(), [
        set_compute_unit_limit(400_000), set_compute_unit_price(5_000)], [], Hash.default())
    payload = base64.b64encode(bytes(VersionedTransaction(message, [signer]))).decode()
    assert executor.signed_priority_fee_lamports(payload) == 2_000
    assert executor.signed_priority_fee_lamports(None) is None


def test_restart_recovery_uses_original_sol_funding_contract(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "ledger.db")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.start_entry_attempt(conn, attempt_id="attempt", decision_started_at="2026-09-27T15:00:00Z",
                          network_id="solana", mint="coin")
    pid = m.reserve_pending(conn, mint="coin", source_user_id="wallet", strategy="alpha_b",
                            notional=80.0, reserved_at="2026-09-27T15:00:00Z", network_id="solana",
                            attempt_id="attempt", funding_asset="sol",
                            funding_amount_atomic=800_000_000, funding_sol_usd=100.0)
    m.set_pending_sig(conn, pid, "sig", "2026-09-27T15:00:01Z")
    tx = _receipt()
    tx["blockTime"] = 1790521200
    from sniper.execution.solana_swap_executor import recover_confirmed_entry
    out = recover_confirmed_entry(conn, m.pending_row(conn, pid), tx, owner="owner",
                                  now_iso="2026-09-27T15:00:02Z")
    assert out is not None
    import pytest
    assert conn.execute("SELECT entry_notional FROM memecoin_positions").fetchone()[0] \
        == pytest.approx(79.786072)
    fee, rent, atomic, asset, price = conn.execute(
        "SELECT network_fee_lamports, rent_lamports, funding_spent_atomic, funding_asset, "
        "funding_sol_usd FROM memecoin_fills WHERE kind='entry'").fetchone()
    assert (fee, rent, atomic, asset, price) == (100_000, 2_039_280, "797860720", "sol", 100.0)
    assert m.pending_row(conn, pid)["status"] == "confirmed"


def test_dropped_buffer_signature_resolves_after_expiry_but_keeps_its_daily_spend(tmp_path):
    """Live 2026-09-28 row 169: a buffer swap was signed and written ahead, then never landed.
    `not_found` was never terminal, so the buffer stayed blocked for 55 h and the write-ahead
    $1.26 stayed encumbered in funded sizing. Past the blockhash lifetime (~60-90 s) a
    not_found read is a drop; a young one must still block."""
    import sqlite3
    from sniper.services.memecoin_gas_topup import buffer_topup_once
    conn = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(conn)
    m.record_gas_topup(conn, now_iso="2026-09-28T00:36:42+00:00", day="2026-09-28",
                       usdc_spent=1.26, usdc_before=188.1, sol_received=None, sol_before=1.4,
                       tx_sig="dropped_sig", status="submitted", purpose="buffer")
    kw = dict(sol_balance=0.1, usdc_balance=300.0, sol_usd=100.0,
              keypair=object(), pubkey="owner", rpc_url="rpc",
              cap_usd=150.0, topup_usd=100.0, daily_cap_usd=200.0,
              min_usdc_remaining_usd=150.0, lock_dir=str(tmp_path),
              confirm_fn=lambda *_a: "not_found")
    young = buffer_topup_once(conn, now_iso="2026-09-28T00:40:00+00:00", **kw)
    assert young["state"] == "unresolved_topup"
    assert m.unresolved_gas_topup(conn, purpose="buffer") == "dropped_sig"
    for ambiguous in ("unavailable", "pending"):
        held = buffer_topup_once(conn, now_iso="2026-09-30T08:00:00+00:00",
                                 **{**kw, "confirm_fn": lambda *_a, s=ambiguous: s})
        assert held["state"] == "unresolved_topup"
    old = buffer_topup_once(conn, now_iso="2026-09-30T08:00:00+00:00", **kw)
    assert old["state"] == "prior_dropped"
    assert m.unresolved_gas_topup(conn, purpose="buffer") is None
    assert m.pending_funding_totals(conn, "solana") == (0.0, 0)
    assert m.gas_topup_spent_today(conn, "2026-09-28", purpose="buffer") == 1.26


def test_unreadable_buffer_submit_age_never_drops(tmp_path):
    """Fail closed: a write-ahead row whose age cannot be read stays unresolved on not_found."""
    import sqlite3
    from sniper.services.memecoin_gas_topup import buffer_topup_once
    conn = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(conn)
    m.record_gas_topup(conn, now_iso="garbled", day="2026-09-28", usdc_spent=1.26,
                       usdc_before=188.1, sol_received=None, sol_before=1.4,
                       tx_sig="sig", status="submitted", purpose="buffer")
    result = buffer_topup_once(
        conn, sol_balance=0.1, usdc_balance=300.0, sol_usd=100.0, keypair=object(),
        pubkey="owner", rpc_url="rpc", now_iso="2026-09-30T08:00:00+00:00", cap_usd=150.0,
        topup_usd=100.0, daily_cap_usd=200.0, min_usdc_remaining_usd=150.0,
        lock_dir=str(tmp_path), confirm_fn=lambda *_a: "not_found")
    assert result["state"] == "unresolved_topup"
    assert m.unresolved_gas_topup(conn, purpose="buffer") == "sig"
