"""Unknown-confirm derisk recovery (2026-07-17, pos 6605: the partial sell's confirm came
back unknown, the one-shot burned silently, the sig was dropped, and the coin rode full
size off a +204% peak while the UI showed 'derisked'). Contracts:

  - the executor's unknown branch persists the sig (verifiable later);
  - recovery re-arms the rail ONLY on proof of non-landing (reverted, or repeated
    explicit not_found past the blockhash window) — never on ambiguity;
  - a landed-late sig keeps the one-shot and stops rechecking (proceeds via reconcile).
"""
import os
import sqlite3

import pytest

from sniper.execution.solana_exit_executor import execute_partial_exit
from sniper.execution.exec_lock import serialize_executor
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _open_real(c, mint="MintRR", derisked_at=None, sig=None):
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint=mint, ticker="RR",
                          entry_price=0.0004, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-07-17T14:00:00+00:00",
                          strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real",
                          network_id="solana")
    if derisked_at:
        m.set_position_derisked(c, pid, derisked_at)
    if sig:
        m.set_position_derisk_sig(c, pid, sig)
    return pid


def _pos(c, pid):
    return dict(c.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())


# ── executor: unknown branch persists the sig ────────────────────────────────

def test_partial_exit_unknown_persists_sig():
    c = _c(); pid = _open_real(c)
    res = execute_partial_exit(
        c, _pos(c, pid), 0.58, "2026-07-17T14:47:58+00:00",
        quote_fn=lambda mint, tokens, slip: {
            "inAmount": "66649", "outAmount": "100", "otherAmountThreshold": "90"},
        sign_fn=lambda q: ("tx", "SIG_UNKNOWN_1"),
        send_fn=lambda tx: None,
        confirm_fn=lambda sig: "pending",            # non-terminal -> unknown branch
        usdc_fn=lambda: 100.0,
        token_balance_fn=lambda mint: 114912.0,
        slippage_ladder=[0.03], sleep_fn=lambda s: None)
    assert res["status"] == "unknown" and res["sig"] == "SIG_UNKNOWN_1"
    row = _pos(c, pid)
    assert row["derisked_at"] and row["derisk_sig"] == "SIG_UNKNOWN_1"


def test_partial_exit_confirmed_books_fill_no_sig():
    c = _c(); pid = _open_real(c)
    res = execute_partial_exit(
        c, _pos(c, pid), 0.58, "t0",
        quote_fn=lambda mint, tokens, slip: {
            "inAmount": "66649", "outAmount": "100", "otherAmountThreshold": "90"},
        sign_fn=lambda q: ("tx", "SIG_OK"),
        send_fn=lambda tx: None,
        confirm_fn=lambda sig: "confirmed",
        usdc_fn=iter([100.0, 186.0]).__next__,       # pre 100 -> post 186 = $86 received
        token_balance_fn=lambda mint: 114912.0,
        slippage_ladder=[0.03], sleep_fn=lambda s: None)
    assert res["status"] == "derisked" and abs(res["received"] - 86.0) < 1e-9
    row = _pos(c, pid)
    assert row["derisked_at"] and row["derisk_sig"] is None
    assert m.derisk_proceeds(c, pid) == 86.0


def test_crash_after_broadcast_stays_submitted_unknown_and_never_double_sells():
    """The signed identity exists before broadcast. A process failure after send but before confirm
    must therefore leave recoverable submitted-unknown state, never an unsigned stale pending claim
    that the recovery pass can release into a second partial sell."""
    c = _c(); pid = _open_real(c)
    broadcasts = []

    def send_once(tx):
        broadcasts.append(tx)

    with pytest.raises(RuntimeError, match="crash after broadcast"):
        execute_partial_exit(
            c, _pos(c, pid), 0.58, "2026-07-17T14:47:58+00:00",
            quote_fn=lambda mint, tokens, slip: {
                "inAmount": "66649", "outAmount": "100", "otherAmountThreshold": "90"},
            sign_fn=lambda q: ("tx-1", "SIG_CRASHED"),
            send_fn=send_once,
            confirm_fn=lambda sig: (_ for _ in ()).throw(RuntimeError("crash after broadcast")),
            usdc_fn=lambda: 100.0,
            token_balance_fn=lambda mint: 114912.0,
            slippage_ladder=[0.03], sleep_fn=lambda s: None,
        )

    row = _pos(c, pid)
    assert row["derisked_at"] == "2026-07-17T14:47:58+00:00"
    assert row["derisk_sig"] == "SIG_CRASHED"
    assert row["derisk_pending_at"] is None

    # Past the unsigned-pending lease: submitted-unknown is confirmed, not released.
    R.recover_unverified_derisks(c, _ctx("pending"), "2026-07-17T15:30:00+00:00")
    assert m.stale_derisk_pendings(c, "2026-07-17T15:30:00+00:00") == []

    retry = execute_partial_exit(
        c, _pos(c, pid), 0.58, "2026-07-17T15:31:00+00:00",
        quote_fn=lambda mint, tokens, slip: {"q": 2},
        sign_fn=lambda q: ("tx-2", "SIG-2"),
        send_fn=lambda tx: broadcasts.append(tx),
        confirm_fn=lambda sig: "confirmed", usdc_fn=lambda: 100.0,
        token_balance_fn=lambda mint: 114912.0,
        slippage_ladder=[0.03], sleep_fn=lambda s: None,
    )
    assert retry["status"] == "already_derisked"
    assert broadcasts == ["tx-1"]


# ── recovery pass transitions ────────────────────────────────────────────────

def _ctx(state):
    return {"solana": {"confirm_once_fn": lambda sig: state,
                        "derisk_fn": serialize_executor(execute_partial_exit, f"test-derisk-live-{os.getpid()}")}}


NOW = "2026-07-17T15:30:00+00:00"                    # 42 min after the stamp below
STAMP = "2026-07-17T14:47:58+00:00"


def test_recovery_failed_rearms():
    c = _c(); pid = _open_real(c, derisked_at=STAMP, sig="S1")
    R.recover_unverified_derisks(c, _ctx("failed"), NOW)
    row = _pos(c, pid)
    assert row["derisked_at"] is None and row["derisk_sig"] is None


def test_recovery_confirmed_keeps_oneshot_stops_recheck():
    c = _c(); pid = _open_real(c, derisked_at=STAMP, sig="S2")
    R.recover_unverified_derisks(c, _ctx("confirmed"), NOW)
    row = _pos(c, pid)
    assert row["derisked_at"] == STAMP and row["derisk_sig"] is None


def test_recovery_not_found_needs_three_hits_and_age():
    c = _c(); pid = _open_real(c, derisked_at=STAMP, sig="S3")
    R.recover_unverified_derisks(c, _ctx("not_found"), NOW)
    R.recover_unverified_derisks(c, _ctx("not_found"), NOW)
    assert _pos(c, pid)["derisked_at"] == STAMP       # 2 hits: still armed-off
    R.recover_unverified_derisks(c, _ctx("not_found"), NOW)
    row = _pos(c, pid)
    assert row["derisked_at"] is None                 # 3rd hit + >20min -> re-armed


def test_recovery_not_found_young_stamp_never_rearms():
    c = _c(); pid = _open_real(c, derisked_at=STAMP, sig="S4")
    young = "2026-07-17T14:52:00+00:00"               # 4 min after stamp — inside the window
    for _ in range(5):
        R.recover_unverified_derisks(c, _ctx("not_found"), young)
    assert _pos(c, pid)["derisked_at"] == STAMP


def test_recovery_pending_and_unavailable_wait():
    c = _c(); pid = _open_real(c, derisked_at=STAMP, sig="S5")
    R.recover_unverified_derisks(c, _ctx("pending"), NOW)
    R.recover_unverified_derisks(c, _ctx("unavailable"), NOW)
    row = _pos(c, pid)
    assert row["derisked_at"] == STAMP and row["derisk_sig"] == "S5"


# ── unverified debt must survive the position's close (2026-07-22, pos 9003) ─────
#
# 9003: derisk unknown-confirm at 00:53:04 -> sig persisted; the recovery pass read
# not_found 20x but the 20-min blockhash window had not elapsed when the TRAIL closed
# the position 371s later. `unverified_derisks` filtered is_open=1, so the close
# stranded the unverified sig permanently — no recovery, no audit resolution. The
# on-chain forensic (2026-07-22) later proved the derisk never landed: the exit sold
# the full entry quantity (292581.799277 -> 0) and getTransaction returned null.
# A closed position cannot re-arm a rail, but its audit debt must still be settled.

def _close(c, pid, realized=-32.70, reason="real_trail"):
    m.claim_close(c, pid)
    m.finalize_close(c, pid, "2026-07-17T14:54:00+00:00", realized, exit_reason=reason)


def test_unverified_derisk_survives_position_close():
    c = _c(); pid = _open_real(c, derisked_at=STAMP, sig="S7")
    _close(c, pid)
    assert [p["id"] for p in m.unverified_derisks(c)] == [pid]


def test_closed_position_not_found_resolves_debt():
    c = _c(); pid = _open_real(c, derisked_at=STAMP, sig="S8")
    _close(c, pid)
    for _ in range(3):
        R.recover_unverified_derisks(c, _ctx("not_found"), NOW)
    row = _pos(c, pid)
    assert row["derisk_sig"] is None                   # debt resolved, out of the queue
    assert row["derisked_at"] is None                  # it never landed — record made honest
    assert row["realized_pnl"] == -32.70               # PnL untouched: no proceeds existed


def test_closed_position_confirmed_keeps_stamp_and_settles():
    c = _c(); pid = _open_real(c, derisked_at=STAMP, sig="S9")
    _close(c, pid)
    R.recover_unverified_derisks(c, _ctx("confirmed"), NOW)
    row = _pos(c, pid)
    assert row["derisk_sig"] is None                   # stop rechecking
    assert row["derisked_at"] == STAMP                 # it DID land; proceeds unbooked
    assert row["realized_pnl"] == -32.70               # reconcile/operator books the drift


def test_closed_position_pending_stays_queued():
    c = _c(); pid = _open_real(c, derisked_at=STAMP, sig="S10")
    _close(c, pid)
    R.recover_unverified_derisks(c, _ctx("pending"), NOW)
    assert [p["id"] for p in m.unverified_derisks(c)] == [pid]


def test_recovery_ignores_booked_derisks():
    c = _c(); pid = _open_real(c, derisked_at=STAMP, sig="S6")
    m.insert_fill(c, position_id=pid, kind="derisk", price=0.0007, qty=60000.0,
                  notional=42.0, fill_source="chain", fraction=0.58, created_at=STAMP,
                  tx_sig="S6")
    calls = []
    R.recover_unverified_derisks(
        c, {"solana": {"confirm_once_fn": lambda sig: calls.append(sig) or "failed"}}, NOW)
    assert calls == []                                # booked fill -> not in the work queue
    assert _pos(c, pid)["derisked_at"] == STAMP


# ── de-risk state must be ATOMIC (2026-07-22 review blocker 1) ───────────────
#
# derisk_confirmed() reads "stamp AND no signature" as proof the principal came back. That
# is only sound if the stamp and the signature are written together. They were not: the
# unknown branch called set_position_derisked() then set_position_derisk_sig(), each with
# its own commit, so a crash between them left derisked_at set with derisk_sig NULL — which
# reads as CONFIRMED and recreates 9003's false trail arm exactly.
#
# Symmetrically, the confirmed branch booked the fill and stamped the position in two
# commits; a crash between them leaves a landed de-risk unstamped, so the one-shot can fire
# a second time into an already-sold bag.

def test_unknown_derisk_writes_stamp_and_sig_atomically():
    c = _c(); pid = _open_real(c)
    m.set_position_derisk_unknown(c, pid, "2026-07-17T14:47:58+00:00", "SIG_ATOMIC")
    row = _pos(c, pid)
    assert row["derisked_at"] == "2026-07-17T14:47:58+00:00"
    assert row["derisk_sig"] == "SIG_ATOMIC"
    assert row["derisk_recheck_hits"] == 0


class _CommitSpy:
    """Wraps a connection and snapshots the durable state at every commit, so a test can
    assert what a crash could ever observe."""
    def __init__(self, conn, snapshot):
        self._conn, self._snapshot, self.seen = conn, snapshot, []

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, *a, **k):
        return self._conn.execute(*a, **k)

    def commit(self):
        self._conn.commit()
        self.seen.append(self._snapshot(self._conn))


def test_unknown_derisk_leaves_no_confirmed_looking_intermediate_state():
    """There must be no committed moment where the stamp is set and the signature is not,
    because that state is indistinguishable from a settled de-risk."""
    c = _c(); pid = _open_real(c)
    def snap(conn):
        r = conn.execute("SELECT derisked_at, derisk_sig FROM memecoin_positions WHERE id=?",
                         (pid,)).fetchone()
        return (r["derisked_at"], r["derisk_sig"])
    spy = _CommitSpy(c, snap)
    m.set_position_derisk_unknown(spy, pid, "2026-07-17T14:47:58+00:00", "SIG_ATOMIC")
    assert spy.seen, "expected at least one commit"
    assert not any(at and sig is None for at, sig in spy.seen), spy.seen


def test_confirmed_derisk_books_fill_and_stamp_atomically():
    c = _c(); pid = _open_real(c)
    m.book_derisk_fill(c, position_id=pid, at="2026-07-17T14:47:58+00:00", price=0.0007,
                       qty=60000.0, notional=42.0, fraction=0.58, tx_sig="SIG_OK")
    row = _pos(c, pid)
    assert row["derisked_at"] == "2026-07-17T14:47:58+00:00"
    assert row["derisk_sig"] is None                  # settled: nothing to recover
    assert m.derisk_proceeds(c, pid) == 42.0


def test_confirmed_derisk_never_commits_a_fill_without_its_stamp():
    """A landed de-risk with no stamp lets the one-shot fire again into an already-sold bag."""
    c = _c(); pid = _open_real(c)
    def snap(conn):
        r = conn.execute("SELECT derisked_at FROM memecoin_positions WHERE id=?",
                         (pid,)).fetchone()
        n = conn.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? "
                         "AND kind='derisk'", (pid,)).fetchone()[0]
        return (r["derisked_at"], n)
    spy = _CommitSpy(c, snap)
    m.book_derisk_fill(spy, position_id=pid, at="2026-07-17T14:47:58+00:00", price=0.0007,
                       qty=60000.0, notional=42.0, fraction=0.58, tx_sig="SIG_OK")
    assert spy.seen, "expected at least one commit"
    assert not any(n > 0 and at is None for at, n in spy.seen), spy.seen


# ── book_derisk_fill must be all-or-nothing under statement failure (review 3) ──
#
# insert + stamp share a transaction, but with no rollback a failing stamp UPDATE leaves the
# fill INSERT pending on the connection. The next commit anywhere flushes an orphaned fill
# with no stamp — the exact "landed de-risk, no stamp" state that lets the one-shot re-fire.

def test_book_derisk_fill_rolls_back_when_the_stamp_fails():
    c = _c(); pid = _open_real(c)
    # A trigger that rejects the stamp UPDATE, simulating a mid-transaction statement failure.
    c.execute("CREATE TRIGGER reject_stamp BEFORE UPDATE OF derisked_at ON memecoin_positions "
              "BEGIN SELECT RAISE(ABORT, 'stamp rejected'); END")
    try:
        m.book_derisk_fill(c, position_id=pid, at="2026-07-17T14:47:58+00:00", price=0.0007,
                           qty=60000.0, notional=42.0, fraction=0.58, tx_sig="SIG")
        assert False, "expected the stamp failure to propagate"
    except sqlite3.Error:
        pass
    assert c.in_transaction is False, "connection left mid-transaction after a failed stamp"
    c.execute("DROP TRIGGER reject_stamp")
    c.commit()                                    # any later commit must NOT flush an orphan
    n = c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind='derisk'",
                  (pid,)).fetchone()[0]
    assert n == 0, "fill persisted without its stamp"
    assert _pos(c, pid)["derisked_at"] is None


# ── paper partial must be all-or-nothing too (2026-07-23 review P2 #7a) ─────────
#
# combo_f / real_shadow booked the partial fill then stamped derisked_at in TWO commits — a
# crash between them duplicates the partial (fill booked, stamp not -> next sweep re-fires it)
# and corrupts paper evidence. book_partial_and_stamp claims the stamp and books the fill in
# ONE transaction, with the stamp as the atomic one-shot claim.

def test_book_partial_and_stamp_never_commits_a_fill_without_its_stamp():
    c = _c(); pid = _open_real(c)
    def snap(conn):
        r = conn.execute("SELECT derisked_at FROM memecoin_positions WHERE id=?",
                         (pid,)).fetchone()
        n = conn.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? "
                         "AND kind='partial_exit'", (pid,)).fetchone()[0]
        return (r["derisked_at"], n)
    spy = _CommitSpy(c, snap)
    assert m.book_partial_and_stamp(spy, position_id=pid, at="2026-07-17T14:47:58+00:00",
                                    price=0.0007, qty=30000.0, notional=21.0, fraction=0.5) is True
    assert spy.seen
    assert not any(n > 0 and at is None for at, n in spy.seen), spy.seen


def test_book_partial_and_stamp_is_a_one_shot_claim():
    """A second call on an already-derisked position books NOTHING (the claim is lost)."""
    c = _c(); pid = _open_real(c)
    assert m.book_partial_and_stamp(c, position_id=pid, at="t1", price=0.0007, qty=30000.0,
                                    notional=21.0, fraction=0.5) is True
    assert m.book_partial_and_stamp(c, position_id=pid, at="t2", price=0.0007, qty=15000.0,
                                    notional=10.0, fraction=0.5) is False
    n = c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? "
                  "AND kind='partial_exit'", (pid,)).fetchone()[0]
    assert n == 1


# ── a LIVE executor is not an orphan (2026-09-25, pos 64934) ─────────────────
def _race(c, pid, recovery_now, *, locked=True, phase="confirm"):
    """Run a de-risk whose confirm wait is interrupted by the fast lane's recovery pass.

    The real serialization wrapper holds its wallet lock through confirmation and booking.
    Recovery runs inside either phase, including after the age threshold has elapsed.
    """
    submitted = "2026-09-25T10:23:32.596919+00:00"

    def recover():
        R.recover_unverified_derisks(c, {"solana": {"confirm_once_fn": lambda s: "confirmed",
                                     "derisk_fn": executor}}, recovery_now)

    def confirm_during_recovery(sig):
        if phase == "confirm":
            recover()
        return "confirmed"

    executor = serialize_executor(execute_partial_exit, f"test-derisk-live-{os.getpid()}") if locked else execute_partial_exit
    usdc = iter([100.0, 175.69])                       # pre, then post: $75.69 received
    def balance():
        value = next(usdc)
        if value > 100 and phase == "book":
            recover()
        return value

    return executor(
        c, _pos(c, pid), 0.5, submitted,
        quote_fn=lambda mint, tokens, slip: {
            "inAmount": "1148549", "outAmount": "75690071", "otherAmountThreshold": "75690071"},
        sign_fn=lambda q: ("tx", "SIG_LIVE_64934"),
        send_fn=lambda tx: None,
        confirm_fn=confirm_during_recovery,
        usdc_fn=balance,
        token_balance_fn=lambda mint: 2297098.088931,
        slippage_ladder=[0.03], sleep_fn=lambda s: None)


def test_a_recovery_pass_inside_the_executors_confirm_wait_leaves_the_booking_intact():
    c = _c(); pid = _open_real(c, mint="MintLIVE")
    res = _race(c, pid, "2026-09-25T10:23:33.317830+00:00")        # 64934's 0.72 s
    assert res["status"] == "derisked", res
    fills = [dict(r) for r in c.execute(
        "SELECT kind, qty, notional, tx_sig FROM memecoin_fills WHERE position_id=?", (pid,))]
    derisk = [f for f in fills if f["kind"] == "derisk"]
    assert len(derisk) == 1 and derisk[0]["tx_sig"] == "SIG_LIVE_64934"
    assert derisk[0]["notional"] == pytest.approx(75.69)
    assert m.derisk_proceeds(c, pid) == pytest.approx(75.69)


def test_the_bound_still_lets_the_recovery_settle_a_real_orphan():
    """The bound must not quietly disable the recovery: an executor that died leaves the same
    row, and 200 s later it is the recovery's to settle."""
    c = _c()
    pid = _open_real(c, mint="MintORPHAN", derisked_at="2026-09-25T10:23:32.596919+00:00",
                     sig="SIG_ORPHAN")
    young = m.unverified_derisks(c, submitted_before_iso="2026-09-25T10:22:00+00:00")
    old = m.unverified_derisks(c, submitted_before_iso="2026-09-25T10:24:00+00:00")
    assert [r["id"] for r in young] == [] and [r["id"] for r in old] == [pid]
    R.recover_unverified_derisks(c, _ctx("confirmed"), "2026-09-25T10:26:52+00:00")   # +200 s
    assert _pos(c, pid)["derisk_sig"] is None, "a landed orphan past the bound is settled"


def test_zero_age_bound_still_cannot_take_a_live_executors_claim(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DERISK_RECOVERY_MIN_AGE_S", 0.0)
    c = _c(); pid = _open_real(c, mint="MintROLLBACK")
    assert _race(c, pid, "2026-09-25T10:23:33.317830+00:00")["status"] == "derisked"


@pytest.mark.parametrize("phase", ["confirm", "book"])
def test_live_executor_past_recovery_age_books_once(phase):
    c = _c(); pid = _open_real(c)
    result = _race(c, pid, "2026-09-25T10:25:33.596919+00:00", locked=True, phase=phase)
    assert result["status"] == "derisked"
    assert m.derisk_proceeds(c, pid) == pytest.approx(75.69)
    assert c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE kind='derisk'").fetchone()[0] == 1


def test_recovery_rpc_does_not_own_execution_lock():
    c = _c(); pid = _open_real(c, derisked_at=STAMP, sig="ORPHAN")
    ctx = _ctx("confirmed")
    executor = ctx["solana"]["derisk_fn"]
    def confirm(sig):
        with executor.recovery_lock() as held:
            assert held, "network read blocks funded execution"
        return "confirmed"
    ctx["solana"]["confirm_once_fn"] = confirm
    R.recover_unverified_derisks(c, ctx, NOW)
    assert _pos(c, pid)["derisk_sig"] is None


def test_unknown_executor_identity_preserves_claim_and_signature():
    c = _c(); pid = _open_real(c, derisked_at=STAMP, sig="UNKNOWN_OWNER")
    R.recover_unverified_derisks(c, {"solana": {"confirm_once_fn": lambda sig: "confirmed"}}, NOW)
    assert _pos(c, pid)["derisk_sig"] == "UNKNOWN_OWNER"


def test_live_unsigned_executor_claim_survives_stale_threshold():
    c = _c(); pid = _open_real(c)
    claim = m.claim_position_derisk(c, pid, STAMP)
    ctx = _ctx("confirmed")
    with ctx["solana"]["derisk_fn"].recovery_lock() as held:
        assert held
        R.recover_unverified_derisks(c, ctx, NOW)
        assert _pos(c, pid)["derisk_claim_id"] == claim
    R.recover_unverified_derisks(c, ctx, NOW)
    assert _pos(c, pid)["derisk_claim_id"] is None


@pytest.mark.parametrize("name", ["clear_position_derisk", "settle_position_derisk_sig", "bump_derisk_recheck"])
def test_null_claim_compare_and_set_cannot_take_replaced_owner(name):
    c = _c(); pid = _open_real(c, derisked_at=STAMP, sig="SAME_SIGNATURE")
    c.execute("UPDATE memecoin_positions SET derisk_claim_id='new-owner' WHERE id=?", (pid,))
    c.commit()
    assert not getattr(m, name)(c, pid, claim_id=None, sig="SAME_SIGNATURE", exact_claim=True)
    assert _pos(c, pid)["derisk_claim_id"] == "new-owner"
    assert _pos(c, pid)["derisk_sig"] == "SAME_SIGNATURE"
