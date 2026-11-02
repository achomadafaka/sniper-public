"""Real-arming audit trail + copy-PnL promote gate (2026-07-12).

Closes the governance gap found in the 20->48 roster jump: approvals had no audit trail and no
evidence bar (17/48 armed with ZERO alpha_b closes). Now every approve/revoke writes an append-only
audit row with the wallet's copy-PnL evidence at decision time, and approvals are gated on the
pre-registered bar (>=N alpha_b closes AND >=$X cum copy PnL) unless the operator forces."""
import sqlite3

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _seed_closes(c, uid, pnls, strategy="alpha_b"):
    for i, pnl in enumerate(pnls):
        pid = m.open_position(c, source_user_id=uid, source_handle=uid, mint=f"M{uid}{i}",
                              ticker="T", entry_price=1.0, entry_notional=10.0,
                              entry_fill_source="jupiter", entry_lag_s=0.1, their_size_usd=1.0,
                              opened_at=f"2026-07-0{(i % 8) + 1}T00:00:00Z", strategy=strategy)
        m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                      fill_source="jupiter", fraction=1.0,
                      created_at=f"2026-07-0{(i % 8) + 1}T00:00:00Z")
        m.insert_fill(c, position_id=pid, kind="mirror_exit", price=1.0 + pnl / 10.0,
                      qty=10.0, notional=10.0 + pnl, fill_source="jupiter", fraction=1.0,
                      created_at=f"2026-07-0{(i % 8) + 1}T01:00:00Z")
        m.close_position(c, pid, f"2026-07-0{(i % 8) + 1}T01:00:00Z", pnl, exit_reason="real_tp")


def test_audit_table_exists():
    c = _conn()
    cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_wallet_approvals)")}
    assert {"source_user_id", "action", "actor", "reason", "copy_pnl", "copy_closes",
            "forced", "created_at"} <= cols


def test_alpha_b_copy_stats_counts_paper_closes_only():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    _seed_closes(c, "u1", [10.0, -4.0, 6.0])
    _seed_closes(c, "u1", [99.0], strategy="control")          # other arm ignored
    s = m.alpha_b_copy_stats(c, "u1")
    assert s["n_closes"] == 3 and abs(s["pnl"] - 12.0) < 1e-9
    assert m.alpha_b_copy_stats(c, "ghost") == {"n_closes": 0, "pnl": 0.0}


def _seed_close_with_fills(c, uid, mint, pnl, *, entry_src, exit_src, exit_kind="mirror_exit"):
    """Seed one closed alpha_b position with explicit entry + exit fill sources."""
    pid = m.open_position(c, source_user_id=uid, source_handle=uid, mint=mint, ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source=entry_src,
                          entry_lag_s=0.1, their_size_usd=1.0, opened_at="2026-07-01T00:00:00Z",
                          strategy="alpha_b")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source=entry_src, fraction=1.0, created_at="2026-07-01T00:00:00Z")
    m.insert_fill(c, position_id=pid, kind=exit_kind, price=1.0 + pnl / 10.0, qty=10.0,
                  notional=10.0 + pnl, fill_source=exit_src, fraction=1.0,
                  created_at="2026-07-01T01:00:00Z")
    m.close_position(c, pid, "2026-07-01T01:00:00Z", pnl, exit_reason="b_mirror")
    return pid


def test_alpha_b_copy_stats_excludes_fomo_tainted_positions():
    """Fomo fills price at zero slippage (exit==mid==their_price) — a fantasy no real fill can hit.
    They inflate paper copy PnL (fomo exits avg +$12.91 vs honest +$0.97, 2026-07-15 measurement).
    The arming metric must count ONLY positions honest on BOTH legs so the promote gate reflects
    achievable real execution — a wallet whose whole edge is zero-slippage fantasy fills gets no
    credit for it."""
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    # 2 honest closes (dexscreener entry + exit), +$3 each = +$6 real evidence
    _seed_close_with_fills(c, "u1", "H0", 3.0, entry_src="dexscreener", exit_src="dexscreener")
    _seed_close_with_fills(c, "u1", "H1", 3.0, entry_src="dexscreener", exit_src="dexscreener")
    # 3 fomo-tainted closes (zero-impact sell at mid), +$100 each — the fantasy that inflates
    _seed_close_with_fills(c, "u1", "F0", 100.0, entry_src="fomo", exit_src="fomo")
    _seed_close_with_fills(c, "u1", "F1", 100.0, entry_src="dexscreener", exit_src="fomo")
    _seed_close_with_fills(c, "u1", "F2", 100.0, entry_src="fomo", exit_src="dexscreener")
    s = m.alpha_b_copy_stats(c, "u1")
    assert s["n_closes"] == 2, "only closes honest on BOTH legs count (%r)" % s
    assert abs(s["pnl"] - 6.0) < 1e-9, "clean PnL = 2x+$3, not inflated by 3x+$100 fomo (%r)" % s


def test_promote_gate_refuses_wallet_qualified_only_by_fomo_fills():
    """A wallet that clears 5-closes/$25 ONLY because of fomo-fantasy fills must be REFUSED —
    on honest evidence it has 2 closes, below the bar. This is the 0.4 governance fix: Magneto
    (16/$108 all-fills -> 0/$0 honest) and peers were armed on inflated paper PnL."""
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    _seed_close_with_fills(c, "u1", "H0", 3.0, entry_src="dexscreener", exit_src="dexscreener")
    _seed_close_with_fills(c, "u1", "H1", 3.0, entry_src="dexscreener", exit_src="dexscreener")
    for i in range(3):
        _seed_close_with_fills(c, "u1", f"F{i}", 100.0, entry_src="fomo", exit_src="fomo")
    # all-fills view would be 5 closes / +$306 (qualifies); honest view is 2 closes (refused)
    res = m.set_wallet_real_approved(c, "u1", True, min_closes=5, min_pnl=25.0)
    assert res["ok"] is False and "closes" in res["reason"], res
    assert "u1" not in m.real_approved_user_ids(c)


def test_approve_below_bar_is_refused_and_audited():
    c = _conn()
    m.add_wallet(c, "u1", "h1")                                # zero closes
    res = m.set_wallet_real_approved(c, "u1", True, actor="ui", reason="test",
                                     min_closes=5, min_pnl=25.0)
    assert res["ok"] is False and "closes" in res["reason"]
    assert "u1" not in m.real_approved_user_ids(c)             # flag unchanged
    row = c.execute("SELECT * FROM memecoin_wallet_approvals").fetchone()
    assert row["action"] == "approve_refused" and row["actor"] == "ui"
    assert row["copy_closes"] == 0


def test_real_approval_preview_is_read_only_and_matches_gate():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    _seed_closes(c, "u1", [10.0, 8.0, 6.0, 9.0, 7.0])

    preview = m.real_approval_preview(
        c, "u1", reason="qualified", min_closes=5, min_pnl=25.0,
    )

    assert preview == {
        "ok": True,
        "action": "approve",
        "stats": {"n_closes": 5, "pnl": 40.0},
        "reason": "qualified",
        "forced": False,
    }
    assert "u1" not in m.real_approved_user_ids(c)
    assert c.execute("SELECT COUNT(*) FROM memecoin_wallet_approvals").fetchone()[0] == 0


def test_real_approval_preview_reports_refusal_and_force_without_writes():
    c = _conn()
    m.add_wallet(c, "u1", "h1")

    refused = m.real_approval_preview(
        c, "u1", reason="operator", min_closes=5, min_pnl=25.0,
    )
    forced = m.real_approval_preview(
        c, "u1", reason="override", force=True, min_closes=5, min_pnl=25.0,
    )

    assert refused["ok"] is False and refused["action"] == "approve_refused"
    assert "0 alpha_b closes" in refused["reason"]
    assert refused["forced"] is False
    assert forced["ok"] is True and forced["action"] == "approve"
    assert forced["forced"] is True and forced["reason"] == "override"
    assert "u1" not in m.real_approved_user_ids(c)
    assert c.execute("SELECT COUNT(*) FROM memecoin_wallet_approvals").fetchone()[0] == 0


def test_fomo_follow_refusal_audit_keeps_evidence_and_bounds_failure():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    stats = {"n_closes": 7, "pnl": 31.25}

    result = m.record_wallet_real_follow_refusal(
        c,
        "u1",
        actor="ui",
        reason="x" * 800,
        force=True,
        stats=stats,
        now_iso="2026-07-16T12:00:00+00:00",
    )

    assert result["ok"] is False
    assert result["action"] == "approve_refused_fomo_follow"
    assert result["reason"] == "x" * 500
    assert "u1" not in m.real_approved_user_ids(c)
    row = c.execute("SELECT * FROM memecoin_wallet_approvals").fetchone()
    assert row["action"] == "approve_refused_fomo_follow"
    assert row["actor"] == "ui" and row["forced"] == 1
    assert row["copy_closes"] == 7 and row["copy_pnl"] == 31.25
    assert row["reason"] == "x" * 500


def test_approve_above_bar_succeeds_with_evidence_snapshot():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    _seed_closes(c, "u1", [10.0, 8.0, 6.0, 9.0, 7.0])          # 5 closes, +$40
    res = m.set_wallet_real_approved(c, "u1", True, actor="ui", reason="qualified",
                                     min_closes=5, min_pnl=25.0)
    assert res["ok"] is True
    # The approval FLAG is set. Entry authority additionally needs a chain list (2026-07-30).
    assert c.execute("SELECT real_approved FROM memecoin_wallets "
                     "WHERE source_user_id='u1'").fetchone()[0] == 1
    m.set_wallet_real_chains(c, "u1", ["solana"])
    assert m.real_approved_user_ids(c, network_id="solana") == {"u1"}
    row = c.execute("SELECT * FROM memecoin_wallet_approvals "
                    "WHERE action='approve' ORDER BY id DESC").fetchone()
    assert row["action"] == "approve" and row["copy_closes"] == 5 and row["copy_pnl"] == 40.0
    assert row["forced"] == 0


def test_force_approve_below_bar_is_allowed_but_marked():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    res = m.set_wallet_real_approved(c, "u1", True, actor="operator", reason="manual override",
                                     force=True, min_closes=5, min_pnl=25.0)
    assert res["ok"] is True
    assert c.execute("SELECT real_approved FROM memecoin_wallets "
                     "WHERE source_user_id='u1'").fetchone()[0] == 1
    row = c.execute("SELECT * FROM memecoin_wallet_approvals ORDER BY id DESC").fetchone()
    assert row["action"] == "approve" and row["forced"] == 1


def test_revoke_always_allowed_and_audited():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_real_approved(c, "u1", True, force=True)      # arm first
    res = m.set_wallet_real_approved(c, "u1", False, actor="auto_demote", reason="-100 realized")
    assert res["ok"] is True and "u1" not in m.real_approved_user_ids(c)
    row = c.execute("SELECT * FROM memecoin_wallet_approvals ORDER BY id DESC").fetchone()
    assert row["action"] == "revoke" and row["actor"] == "auto_demote"


def test_arming_is_per_chain_not_global():
    """Arming is PER CHAIN (2026-07-30), reversing the 2026-07-15 global decision. An approved
    wallet with no chain list is armed NOWHERE, and one listing solana is refused everywhere else."""
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_real_approved(c, "u1", True, force=True)
    for nid in ("solana", "4663", "8453", "56", "1", "evm-unknown"):
        assert m.real_approved_user_ids(c, network_id=nid) == set(), f"NULL armed on {nid}"
    m.set_wallet_real_chains(c, "u1", ["solana"])
    assert m.real_approved_user_ids(c, network_id="solana") == {"u1"}
    for nid in ("4663", "8453", "56", "1", "evm-unknown"):
        assert m.real_approved_user_ids(c, network_id=nid) == set(), f"leaked onto {nid}"


def test_legacy_positional_call_still_works():
    """The release-pinned runner calls set_wallet_real_approved(conn, uid, False) positionally."""
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_real_approved(c, "u1", True, force=True)
    m.set_wallet_real_approved(c, "u1", False)                 # bare legacy revoke
    assert "u1" not in m.real_approved_user_ids(c)


def test_the_arming_stat_keys_on_uid_not_the_handle():
    """A wallet's identity is source_user_id. The handle is a display string that is frequently the
    placeholder '(real)': the executor opens a real position before it knows who signalled it, and
    set_position_identity backfills the true handle afterwards. When that backfill does not happen
    the row keeps '(real)' forever. On 2026-08-28 a handle-keyed read of yayayayayey dropped one such
    row (+$135.11) and reported the wallet at -$27.34/14 closes — past the exclusion bar — when it
    was really +$107.77/15 and clean. An empty handle is worse than a missing one: it POOLS, because
    other wallets' rows also carry ''. Key on the uid and both failures are impossible."""
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    _seed_closes(c, "u1", [10.0, -4.0])
    c.execute("UPDATE memecoin_positions SET source_handle='(real)' WHERE id=1")   # never backfilled
    c.execute("UPDATE memecoin_positions SET source_handle='' WHERE id=2")         # pooling handle
    c.commit()

    assert m.alpha_b_copy_stats(c, "u1") == {"n_closes": 2, "pnl": 6.0}
    assert m.alpha_b_copy_stats_all(c)["u1"] == {"n_closes": 2, "pnl": 6.0}

    # the handle-keyed read this replaces: it sees neither row, so the wallet reads as no-evidence.
    missed = c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE source_handle='h1'").fetchone()[0]
    assert missed == 0
