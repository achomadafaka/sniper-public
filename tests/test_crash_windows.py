"""WO-9 — crash-injection harness for the funded money path.

Every P0 this session was a crash/timing-window bug: a process death (exception, kill, power cut)
BETWEEN two durable writes left a row in a state that read as safe but wasn't — a stamp with no
signature that looked confirmed, a landed sell with no stamp that re-fired, a reservation stuck
forever. Green unit tests missed all of them because they run the happy path start to finish.

This harness systematizes the adversarial review that caught them: it kills a money-path operation
at EVERY durable boundary and asserts the recovery invariants on a fresh connection — exactly what a
restart sees. The pattern is meant to be extended: add a new `_walk_*` for any new money-path op.

The core mechanism (`CrashConn`) is faithful because SQLite commits are the durability boundary:
a fresh connection to the same file after the crash sees precisely the committed prefix and nothing
the operation would have done next. Walking the crash point across 1..N visits every yield point the
WO names — "after claim, after sign, after broadcast, before stamp, between commit pairs".

Invariants asserted after recovery (money-path universals):
  INV-A  no false-confirmed:  `derisk_confirmed(pos)` is True only when a booked derisk fill exists.
  INV-B  no orphaned claim:   nothing sits in a recovery queue past its lease — it is released,
                              advanced to submitted-unknown, or terminally settled.
  INV-C  no double-submit:    a retry after recovery never broadcasts a second economic sell while
                              the first's signed identity is still durable.
  INV-D  no orphaned close:   a position flipped is_open=0 always carries close bookkeeping
                              (closed_at) OR is discoverable by a recovery queue.
"""
import sqlite3

import pytest

from sniper.execution.evm_exit_executor import execute_exit_evm
from sniper.execution.solana_exit_executor import execute_exit, execute_partial_exit
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R


# ── crash mechanism ──────────────────────────────────────────────────────────

class CrashInjected(RuntimeError):
    """Simulated process death at a money-path yield point."""


class CrashConn:
    """Wrap a real connection; perform the k-th commit, then raise CrashInjected.

    Durable prefix after the raise = commits 1..k, exactly what a restart would see. k=0 means
    raise on the very first commit attempt (before it lands) -> nothing durable.
    """
    def __init__(self, conn, crash_at):
        self._conn = conn
        self._crash_at = crash_at
        self.commits = 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, *a, **k):
        return self._conn.execute(*a, **k)

    def commit(self):
        if self.commits + 1 > self._crash_at:
            raise CrashInjected(f"process died before commit #{self.commits + 1}")
        self._conn.commit()
        self.commits += 1
        if self.commits >= self._crash_at:
            raise CrashInjected(f"process died right after commit #{self.commits}")


def _db(tmp_path, name):
    """A fresh FILE-backed DB (not :memory:) so a post-crash connection has real restart fidelity."""
    path = str(tmp_path / name)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    return path, c


def _reopen(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _open_real(c, mint="MintCW"):
    return m.open_position(
        c, source_user_id="u1", source_handle="h", mint=mint, ticker="CW",
        entry_price=0.0004, entry_notional=50.0, entry_fill_source="chain",
        entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-25T00:00:00+00:00",
        strategy=cfg_policy(), execution_mode="real", network_id="solana")


def cfg_policy():
    from sniper.settings import memecoin as cfg
    return cfg.MEMECOIN_REAL_POLICY


def _pos(c, pid):
    r = c.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def _derisk_fills(c, pid):
    return c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind='derisk'",
                     (pid,)).fetchone()[0]


# ── universal invariant checks ───────────────────────────────────────────────

FAR_FUTURE = "2099-01-01T00:00:00+00:00"


def _assert_no_false_confirmed(c, pid):
    """INV-A: derisk_confirmed True (stamp, no sig) is only legitimate with a booked fill."""
    pos = _pos(c, pid)
    if R.derisk_confirmed(pos):
        assert _derisk_fills(c, pid) >= 1, (
            f"false-confirmed derisk: derisked_at set, derisk_sig NULL, no fill — {pos}")


# ── partial-exit (de-risk) crash walk ────────────────────────────────────────

def _seams(broadcasts, sig, confirm="confirmed"):
    """Injected executor seams that record every broadcast so double-submit is observable."""
    return dict(
        quote_fn=lambda mint, tokens, slip: {
            "inAmount": "120000", "outAmount": "100", "otherAmountThreshold": "90"},
        sign_fn=lambda q: ("signed-tx", sig),
        send_fn=lambda tx: broadcasts.append(tx),
        confirm_fn=lambda s: confirm,
        usdc_fn=iter([100.0, 172.0, 172.0, 172.0]).__next__,
        token_balance_fn=lambda mint: 120000.0,
        slippage_ladder=[0.03], sleep_fn=lambda s: None)


@pytest.mark.parametrize("crash_at", range(1, 5))
def test_partial_exit_crash_walk(tmp_path, crash_at):
    """Kill after claim / quote-bound intent / signed identity / book, then recover on a
    fresh connection and assert the money-path invariants. crash_at spans every durable boundary."""
    path, c = _db(tmp_path, f"partial_{crash_at}.db")
    pid = _open_real(c)
    # backdate the claim so the stale-pending lease is already elapsed at recovery time
    old_now = "2026-07-25T00:00:00+00:00"
    broadcasts = []

    crash = CrashConn(c, crash_at)
    with pytest.raises(CrashInjected):
        execute_partial_exit(crash, _pos(c, pid), 0.5, old_now, **_seams(broadcasts, "SIG_A"))
    c.close()

    # ── restart: a fresh connection sees only the durable prefix ──
    rc = _reopen(path)
    rc.row_factory = sqlite3.Row
    _assert_no_false_confirmed(rc, pid)

    # recovery: release stale unsigned pendings; settle submitted-unknown against a "confirmed" chain
    now = "2026-07-25T01:00:00+00:00"       # 1h later -> stale lease elapsed
    # confirm_once="confirmed" simulates the sell HAVING landed: a submitted-unknown row then
    # settles to stamp+no-sig+no-fill, which is legitimate (proceeds via reconcile) — so INV-A is
    # asserted PRE-recovery only, where a false-confirmed durable state would be the actual bug.
    from sniper.execution.exec_lock import serialize_executor
    ctx = {"solana": {"confirm_once_fn": lambda s: "confirmed",
                      "derisk_fn": serialize_executor(execute_partial_exit, "crash-derisk")}}
    R.recover_unverified_derisks(rc, ctx, now)

    # INV-B: nothing dangling in the pending queue past its lease
    assert m.stale_derisk_pendings(rc, FAR_FUTURE) == [], "orphaned pending claim after recovery"
    if crash_at >= 3:
        status = rc.execute(
            "SELECT status FROM memecoin_exit_intents WHERE position_id=?", (pid,)).fetchone()[0]
        assert status == "confirmed"

    # INV-C: a retry never double-broadcasts while a signed identity is durable
    before = list(broadcasts)
    retry = execute_partial_exit(rc, _pos(rc, pid), 0.5, now, **_seams(broadcasts, "SIG_B"))
    if crash_at >= 3:
        # submit stamped a durable sig -> the one-shot is owned; retry must not sell again
        assert retry["status"] in ("already_derisked",), retry
        assert broadcasts == before, "double-submit: retry broadcast over a durable signed claim"
    else:
        # only the claim was durable (no sig) -> recovery released it, retry may proceed ONCE
        assert len(broadcasts) - len(before) <= 1
    rc.close()


def test_partial_exit_post_broadcast_crash_no_double_sell(tmp_path):
    """The classic window: crash INSIDE confirm, after the sell was physically broadcast. The signed
    identity is durable (mark_submitted committed before send), so recovery keeps it submitted-unknown
    and no retry re-sells."""
    path, c = _db(tmp_path, "postbroadcast.db")
    pid = _open_real(c)
    broadcasts = []

    def confirm_boom(sig):
        raise CrashInjected("died in confirm, after broadcast")

    seams = _seams(broadcasts, "SIG_CRASH")
    seams["confirm_fn"] = confirm_boom
    with pytest.raises(CrashInjected):
        execute_partial_exit(c, _pos(c, pid), 0.5, "2026-07-25T00:00:00+00:00", **seams)
    assert broadcasts == ["signed-tx"], "the sell WAS broadcast before the crash"
    c.close()

    rc = _reopen(path)
    _assert_no_false_confirmed(rc, pid)          # stamp+sig -> not confirmed, correct
    row = _pos(rc, pid)
    assert row["derisked_at"] and row["derisk_sig"] == "SIG_CRASH" and row["derisk_pending_at"] is None
    assert rc.execute(
        "SELECT status FROM memecoin_exit_intents WHERE position_id=?", (pid,)).fetchone()[0] \
        == "submitted_unknown"
    # recovery + retry never re-broadcasts
    R.recover_unverified_derisks(rc, {"solana": {"confirm_once_fn": lambda s: "pending"}},
                                 "2026-07-25T01:00:00+00:00")
    assert rc.execute(
        "SELECT status FROM memecoin_exit_intents WHERE position_id=?", (pid,)).fetchone()[0] \
        == "submitted_unknown"
    retry = execute_partial_exit(rc, _pos(rc, pid), 0.5, "2026-07-25T01:00:00+00:00",
                                 **_seams(broadcasts, "SIG_RETRY"))
    assert retry["status"] == "already_derisked"
    assert broadcasts == ["signed-tx"], "double-sell after post-broadcast crash"
    rc.close()


def test_full_solana_exit_post_broadcast_crash_stays_submitted_unknown(tmp_path):
    """A signed full sell survives a crash after broadcast and can never be reclaimed/rebroadcast."""
    path, c = _db(tmp_path, "full_sol_postbroadcast.db")
    pid = _open_real(c)
    broadcasts = []

    def confirm_boom(_sig):
        raise CrashInjected("died after broadcast")

    seams = _exit_seams()
    seams["send_fn"] = lambda tx: broadcasts.append(tx)
    seams["confirm_fn"] = confirm_boom
    with pytest.raises(CrashInjected):
        execute_exit(c, _pos(c, pid), "real_trail", "2026-07-25T00:00:00+00:00", **seams)
    assert broadcasts == ["signed-tx"]
    c.close()

    rc = _reopen(path); rc.row_factory = sqlite3.Row
    row = _pos(rc, pid)
    assert row["is_open"] == 1
    assert row["close_sig"] == "SIG_X"
    assert row["close_claim_id"]
    assert m.close_submission_state(row) == "submitted_unknown"
    assert rc.execute(
        "SELECT status FROM memecoin_exit_intents WHERE position_id=?", (pid,)).fetchone()[0] \
        == "submitted_unknown"
    R.recover_unverified_closes(rc, {}, "2026-07-25T01:00:00+00:00")
    retry_seams = _exit_seams()
    retry_seams["send_fn"] = lambda tx: broadcasts.append(tx)
    retry = execute_exit(
        rc, _pos(rc, pid), "real_trail", "2026-07-25T01:00:00+00:00", **retry_seams)
    assert retry["status"] == "already_closing"
    assert broadcasts == ["signed-tx"]
    rc.close()


def test_full_evm_exit_post_broadcast_crash_stays_submitted_unknown(tmp_path):
    """EVM full exits use the same write-ahead identity and refuse every restart retry."""
    path, c = _db(tmp_path, "full_evm_postbroadcast.db")
    pid = _open_real(c, mint="0xMintCW")
    broadcasts = []

    def confirm_boom(_sig):
        raise CrashInjected("died after broadcast")

    with pytest.raises(CrashInjected):
        execute_exit_evm(
            c, _pos(c, pid), "real_trail", "2026-07-25T00:00:00+00:00",
            quote_fn=lambda *_a: {"sellAmount": "10", "minBuyAmount": "9",
                                  "transaction": {"to": "0xr"}},
            approve_fn=lambda *_a: True,
            sign_fn=lambda _tx: ("raw", "0xSIG", 7),
            send_fn=lambda raw: broadcasts.append(raw),
            confirm_fn=confirm_boom,
            usdc_fn=lambda: 100.0,
            token_balance_fn=lambda _mint: 10.0,
            slippage_ladder=[0.03], sleep_fn=lambda _s: None)
    assert broadcasts == ["raw"]
    c.close()

    rc = _reopen(path); rc.row_factory = sqlite3.Row
    row = _pos(rc, pid)
    assert row["is_open"] == 1
    assert row["close_sig"] == "0xSIG"
    assert m.close_submission_state(row) == "submitted_unknown"
    assert rc.execute(
        "SELECT status FROM memecoin_exit_intents WHERE position_id=?", (pid,)).fetchone()[0] \
        == "submitted_unknown"
    R.recover_unverified_closes(rc, {}, "2026-07-25T01:00:00+00:00")
    retry = execute_exit_evm(
        rc, _pos(rc, pid), "real_trail", "2026-07-25T01:00:00+00:00",
        quote_fn=lambda *_a: {"sellAmount": "10", "minBuyAmount": "9",
                              "transaction": {"to": "0xr"}},
        approve_fn=lambda *_a: True,
        sign_fn=lambda _tx: ("raw2", "0xSIG2", 8),
        send_fn=lambda raw: broadcasts.append(raw),
        confirm_fn=lambda _sig: "confirmed", usdc_fn=lambda: 100.0,
        token_balance_fn=lambda _mint: 10.0,
        slippage_ladder=[0.03], sleep_fn=lambda _s: None)
    assert retry["status"] == "already_closing"
    assert broadcasts == ["raw"]
    rc.close()


# ── full-exit (close) crash walk ─────────────────────────────────────────────

def _exit_seams():
    return dict(
        quote_fn=lambda mint, tokens, slip: {
            "inAmount": "120000", "outAmount": "100", "otherAmountThreshold": "90"},
        sign_fn=lambda q: ("signed-tx", "SIG_X"),
        send_fn=lambda tx: None,
        confirm_fn=lambda s: "confirmed",
        usdc_fn=iter([100.0, 190.0, 190.0, 190.0]).__next__,
        token_balance_fn=lambda mint: 120000.0,
        slippage_ladder=[0.03], sleep_fn=lambda s: None)


@pytest.mark.parametrize("crash_at", range(1, 5))
def test_full_exit_crash_walk_no_orphaned_close(tmp_path, crash_at):
    """Walk claim, quote-bound intent, signed identity, and atomic fill/finalize.

    INV-D: no committed prefix may strand is_open=0 without close bookkeeping or recovery.
    """
    path, c = _db(tmp_path, f"fullexit_{crash_at}.db")
    pid = _open_real(c)
    crash = CrashConn(c, crash_at)
    with pytest.raises(CrashInjected):
        execute_exit(crash, _pos(c, pid), "real_trail", "2026-07-25T00:00:00+00:00", **_exit_seams())
    c.close()

    rc = _reopen(path)
    row = _pos(rc, pid)
    # If the close was claimed (is_open flipped) it must be recoverable — either finalized bookkeeping
    # is present, OR a recovery queue surfaces it. Otherwise it is a silent orphaned close.
    if row["is_open"] == 0:
        recoverable = (
            row["closed_at"] is not None
            or bool(m.orphaned_closes(rc))          # WO-9 fix: the recovery queue for this window
        )
        assert recoverable, (
            f"orphaned close: is_open=0, closed_at=NULL, invisible to every recovery queue — {row}")
        status = rc.execute(
            "SELECT status FROM memecoin_exit_intents WHERE position_id=?", (pid,)).fetchone()[0]
        assert status == "confirmed"
    rc.close()


@pytest.mark.parametrize("crash_at", range(1, 5))
def test_full_evm_exit_crash_walk_all_durable_boundaries(tmp_path, crash_at):
    path, c = _db(tmp_path, f"full_evm_{crash_at}.db")
    pid = _open_real(c, mint="0xMintCW")
    broadcasts = []
    crash = CrashConn(c, crash_at)
    with pytest.raises(CrashInjected):
        execute_exit_evm(
            crash, _pos(c, pid), "real_trail", "2026-07-25T00:00:00+00:00",
            quote_fn=lambda *_a: {"sellAmount": "10", "minBuyAmount": "9",
                                  "transaction": {"to": "0xr"}},
            approve_fn=lambda *_a: True,
            sign_fn=lambda _tx: ("raw", "0xSIG", 7),
            send_fn=lambda raw: broadcasts.append(raw),
            confirm_fn=lambda _sig: "confirmed",
            usdc_fn=iter([100.0, 190.0, 190.0, 190.0]).__next__,
            token_balance_fn=lambda _mint: 10.0,
            slippage_ladder=[0.03], sleep_fn=lambda _s: None)
    c.close()

    rc = _reopen(path); rc.row_factory = sqlite3.Row
    row = _pos(rc, pid)
    if crash_at >= 3:
        # Signed identity committed before send. Even crash_at=3 (never broadcast) stays
        # submitted_unknown; safety prefers manual reconciliation over a possible double-sell.
        assert row["close_sig"] == "0xSIG"
        retry = execute_exit_evm(
            rc, row, "real_trail", "2026-07-25T01:00:00+00:00",
            quote_fn=lambda *_a: {"sellAmount": "10", "minBuyAmount": "9",
                                  "transaction": {"to": "0xr"}},
            approve_fn=lambda *_a: True, sign_fn=lambda _tx: ("raw2", "0xSIG2", 8),
            send_fn=lambda raw: broadcasts.append(raw), confirm_fn=lambda _sig: "confirmed",
            usdc_fn=lambda: 190.0, token_balance_fn=lambda _mint: 10.0,
            slippage_ladder=[0.03], sleep_fn=lambda _s: None)
        assert retry["status"] in ("already_closing", "already_closed")
        assert broadcasts == ([] if crash_at == 3 else ["raw"])
        if crash_at == 4:
            status = rc.execute(
                "SELECT status FROM memecoin_exit_intents WHERE position_id=?", (pid,)).fetchone()[0]
            assert status == "confirmed"
    else:
        # No signed identity exists. Stale recovery may release the exact unsigned owner.
        R.recover_unverified_closes(rc, {}, "2026-07-25T01:00:00+00:00")
        assert _pos(rc, pid)["close_sig"] is None
    rc.close()


def test_orphaned_close_recovery_finalizes_with_booked_fills(tmp_path):
    """The recovery helper WO-9 adds: finalize a claimed-but-unfinalized close from its booked fills
    (realized = fills_received + derisk_proceeds - entry_notional), or reopen it if nothing was
    booked (the claim raced ahead of any sell)."""
    # case 1: fill booked, finalize never ran -> recovery finalizes from the fill
    path, c = _db(tmp_path, "orphan_fill.db")
    pid = _open_real(c)
    m.claim_close(c, pid)
    # the real full-exit fill kind for a 'real_trail' close (full_exit_fill_kind -> 'bracket_exit')
    m.insert_fill(c, position_id=pid, kind=m.full_exit_fill_kind("real_trail"), price=0.0016,
                  qty=120000.0, notional=190.0, fill_source="chain", fraction=1.0,
                  created_at="2026-07-25T00:00:00+00:00", tx_sig="SIG_X")
    # (finalize_close never ran — the orphan)
    assert [p["id"] for p in m.orphaned_closes(c)] == [pid]
    n = R.recover_orphaned_closes(c, "2026-07-25T00:05:00+00:00")
    assert n == 1
    row = _pos(c, pid)
    assert row["is_open"] == 0 and row["closed_at"] is not None
    assert abs(row["realized_pnl"] - (190.0 - 50.0)) < 1e-9   # received - entry_notional
    assert m.orphaned_closes(c) == []
    c.close()

    # case 2: a legacy claim with no fill is ambiguous: quarantine it, never risk a second sell
    path2, c2 = _db(tmp_path, "orphan_nofill.db")
    pid2 = _open_real(c2)
    m.claim_close(c2, pid2)
    assert [p["id"] for p in m.orphaned_closes(c2)] == [pid2]
    n2 = R.recover_orphaned_closes(c2, "2026-07-25T00:05:00+00:00")
    assert n2 == 1
    row2 = _pos(c2, pid2)
    assert row2["is_open"] == 0 and row2["closed_at"] is None
    c2.close()


# ── reservation (entry) crash walk ───────────────────────────────────────────

def test_reservation_crash_walk_recovers(tmp_path):
    """A crash after reserve_pending (and optionally after set_pending_broadcast) must never strand a
    'reserved' row forever. recover_stale_reservations resolves it against chain."""
    def confirm_for(_net, sig):
        return {"SIG_OK": "confirmed", "SIG_BAD": "failed"}.get(sig, "unavailable")

    # crash after reserve only (no sig): recovery frees the slot (never broadcast)
    path, c = _db(tmp_path, "resv_nosig.db")
    pid = m.reserve_pending(c, mint="RM1", source_user_id="u", strategy="alpha_b", notional=10.0,
                            reserved_at="2026-07-25T00:00:00+00:00", network_id="solana")
    out = R.recover_stale_reservations(c, confirm_for=confirm_for, now_iso="2026-07-25T01:00:00+00:00")
    assert (pid, "released_never_broadcast") in out
    assert m.pending_totals(c) == (0, 0.0)
    c.close()

    # crash after sig write-ahead, tx failed on chain: recovery frees the slot
    path2, c2 = _db(tmp_path, "resv_sig.db")
    pid2 = m.reserve_pending(c2, mint="RM2", source_user_id="u", strategy="alpha_b", notional=10.0,
                             reserved_at="2026-07-25T00:00:00+00:00", network_id="solana")
    m.set_pending_broadcast(c2, pid2, "SIG_BAD", 7, "2026-07-25T00:00:00+00:00")
    R.recover_stale_reservations(c2, confirm_for=confirm_for, now_iso="2026-07-25T01:00:00+00:00")
    assert m.pending_row(c2, pid2)["status"] == "failed"
    assert m.pending_totals(c2) == (0, 0.0)
    c2.close()
