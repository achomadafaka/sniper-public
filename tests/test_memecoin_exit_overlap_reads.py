"""Solana exit reads overlap (2026-09-28, Alpha GO "Step 0" + "Step 1"; solana_exit_executor).

Each exit rung read the token balance three times and USDC once, serially, before broadcast.
MEMECOIN_EXIT_OVERLAP_READS (default on):
  - one snapshot per rung gives the human and the raw balance; the raw amount goes into the quote,
    so the quote seam does not read it again;
  - the USDC pre-read runs beside the quote; a raise still returns balance_unreadable before any
    signature;
  - the signing re-read runs beside the swap build; the seam's before_sign gate joins it and runs
    verify_exit_intent before the signature, so a changed balance supersedes unsigned (WO-6).
ExitTiming carries sub-marks (sub_ms) for each read, the quote call and the build/sign split.
"""
import json
import sqlite3
import threading
import time

import pytest

from sniper.execution import solana_exit_executor as ex
from sniper.execution.solana_wallet import TokenBalanceSnapshot
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg

READ_S = 0.15
QUOTE = {"inAmount": "500000000", "outAmount": "100", "otherAmountThreshold": "90"}


def _c():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u1", source_handle="(real)", mint="MINT", ticker="",
                          entry_price=0.02, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="t", strategy="alpha_b",
                          execution_mode="real")
    return c, m.get_position(c, pid)


class Deps:
    """Seams with timestamps. The snapshot answers 500 tokens at 6 decimals unless `balances`
    says otherwise; `usdc` answers 0 before the sale and 15 after it."""

    def __init__(self, *, balances=None, usdc_raises=False, gate_aware=True, build_hook=None):
        self.t, self.calls = {}, []
        self.balances = list(balances or [])
        self.usdc_raises, self.build_hook = usdc_raises, build_hook
        self.confirmed, self.signs, self.sends = False, 0, 0
        self.quote_kwargs = []
        self.lock = threading.Lock()
        if not gate_aware:
            self.sign_fn = self._sign_plain

    def snapshot(self, mint):
        with self.lock:
            self.calls.append("snapshot")
            atomic = self.balances.pop(0) if self.balances else 500_000_000
        time.sleep(READ_S)
        return TokenBalanceSnapshot(atomic, 6)

    def token_fn(self, mint):
        with self.lock:
            self.calls.append("token_fn")
        return 500.0

    def quote_fn(self, mint, tokens, slip, max_accounts=None, atomic=None):
        self.t["quote_start"] = time.monotonic()
        self.quote_kwargs.append(atomic)
        time.sleep(READ_S)
        self.t["quote_end"] = time.monotonic()
        return dict(QUOTE)

    def usdc_fn(self):
        with self.lock:
            self.calls.append("usdc")
        if not self.confirmed:
            self.t["usdc_start"] = time.monotonic()
            time.sleep(READ_S)
            if self.usdc_raises:
                raise RuntimeError("usdc read failed")
            return 0.0
        return 15.0

    def sign_fn(self, quote, before_sign=None):
        self.t["build_start"] = time.monotonic()
        if self.build_hook:
            self.build_hook()
        time.sleep(READ_S)                              # the /swap build
        self.t["build_end"] = time.monotonic()
        if before_sign is not None and not before_sign():
            return None
        self.signs += 1
        return (b"tx", "SIG%d" % self.signs)

    def _sign_plain(self, quote):
        self.signs += 1
        return (b"tx", "SIG%d" % self.signs)

    def send_fn(self, signed):
        self.sends += 1

    def confirm_fn(self, sig):
        self.confirmed = True
        return "confirmed"


def _run(c, pos, d, *, snapshot=True, flag=True, monkeypatch=None):
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_OVERLAP_READS", flag, raising=False)
    return ex.execute_exit(
        c, pos, "mirror", "2026-09-28T09:00:00+00:00", quote_fn=d.quote_fn, sign_fn=d.sign_fn,
        send_fn=d.send_fn, confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, usdc_checked_fn=d.usdc_fn,
        token_balance_fn=d.token_fn, slippage_ladder=[10.0],
        token_snapshot_fn=(d.snapshot if snapshot else None), sleep_fn=lambda _s: None)


def test_one_snapshot_per_rung_feeds_the_quote_and_the_duplicate_read_is_gone(monkeypatch):
    c, pos = _c()
    d = Deps()
    result = _run(c, pos, d, monkeypatch=monkeypatch)
    assert result["status"] == "closed", result
    assert d.quote_kwargs == [500_000_000], "the quote did not get the rung's raw balance"
    # rung top + signing re-read; token_balance_fn (the second, human-only read) never ran
    assert d.calls.count("snapshot") == 2 and "token_fn" not in d.calls, d.calls


def test_the_usdc_pre_read_rides_beside_the_quote(monkeypatch):
    c, pos = _c()
    d = Deps()
    _run(c, pos, d, monkeypatch=monkeypatch)
    assert d.t["usdc_start"] < d.t["quote_end"], d.t


def test_a_failed_usdc_pre_read_is_balance_unreadable_before_any_signature(monkeypatch):
    c, pos = _c()
    d = Deps(usdc_raises=True)
    result = _run(c, pos, d, monkeypatch=monkeypatch)
    assert result == {"status": "balance_unreadable", "reason": "usdc_balance_unreadable"}
    assert d.signs == 0 and d.sends == 0
    assert m.get_position(c, pos["id"])["close_claim_id"] is None, "the claim was not released"


def test_the_signing_re_read_rides_beside_the_build(monkeypatch):
    c, pos = _c()
    d = Deps()
    started = []
    real = d.snapshot

    def snap(mint):
        started.append(time.monotonic())
        return real(mint)
    d.snapshot = snap
    result = _run(c, pos, d, monkeypatch=monkeypatch)
    assert result["status"] == "closed"
    assert started[1] < d.t["build_end"], "the signing re-read waited for the build"
    timing = json.loads(m.get_position(c, pos["id"])["exit_timing_json"])
    assert timing["sub_ms"]["signing_read_wait"] < READ_S * 1000 / 2, timing


def test_a_changed_balance_supersedes_unsigned_and_unsent(monkeypatch):
    c, pos = _c()
    d = Deps(balances=[500_000_000, 100_000_000])     # rung top 500, signing re-read 100
    result = _run(c, pos, d, monkeypatch=monkeypatch)
    assert result == {"status": "superseded", "reason": "balance_changed"}, result
    assert d.signs == 0 and d.sends == 0
    row = m.get_position(c, pos["id"])
    assert row["close_sig"] is None and row["close_claim_id"] is None


def test_a_revision_change_during_the_build_is_refused_unsigned(monkeypatch):
    c, pos = _c()
    d = Deps(build_hook=lambda: m.persist_controller_state_cas(
        c, pos["id"], 0, consecutive_break_events=1))
    result = _run(c, pos, d, monkeypatch=monkeypatch)
    assert result["status"] == "superseded", result
    assert d.signs == 0 and d.sends == 0


def test_a_sign_fn_without_the_gate_keeps_the_serial_verify(monkeypatch):
    c, pos = _c()
    d = Deps(balances=[500_000_000, 100_000_000], gate_aware=False)
    result = _run(c, pos, d, monkeypatch=monkeypatch)
    assert result == {"status": "superseded", "reason": "balance_changed"}
    assert d.signs == 0


def test_flag_off_is_the_serial_order(monkeypatch):
    c, pos = _c()
    d = Deps()
    result = _run(c, pos, d, flag=False, monkeypatch=monkeypatch)
    assert result["status"] == "closed"
    assert d.quote_kwargs == [None] and "snapshot" not in d.calls
    assert d.t["usdc_start"] > d.t["quote_end"], "flag off must read USDC after the quote"


def test_the_timing_records_the_sub_marks(monkeypatch):
    c, pos = _c()
    d = Deps()
    _run(c, pos, d, monkeypatch=monkeypatch)
    timing = json.loads(m.get_position(c, pos["id"])["exit_timing_json"])
    assert {"claim_db", "balance_read", "quote_call", "usdc_pre_read", "usdc_pre_wait",
            "sign_call", "signing_read", "signing_read_wait"} <= set(timing["sub_ms"]), timing
    assert set(timing["stages_ms"]) == {"quote", "sign", "submit", "confirm"}


def test_the_seam_gates_the_signature_after_the_build(monkeypatch):
    from solders.keypair import Keypair
    from sniper.execution import solana_real_seams as S
    built, signed = [], []
    monkeypatch.setattr(S, "jupiter_swap_tx", lambda *a, **k: built.append(1) or {"swapTransaction": "x"})
    monkeypatch.setattr(S, "sign_swap_tx", lambda swap, kp: signed.append(1) or ("tx", "SIG"))
    seen = {}

    def fake_execute_exit(conn, position, exit_reason, now_iso, **kw):
        seen.update(kw)
        return {"status": "stub"}
    monkeypatch.setattr(ex, "execute_exit", fake_execute_exit)
    exe = S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://rpc",
                                       http=lambda *a, **k: (200, {"result": None}, None))
    c, pos = _c()
    exe["exit_fn"](c, pos, "mirror", "t")
    sign_fn = seen["sign_fn"]
    assert sign_fn({"outAmount": "1"}, before_sign=lambda: False) is None
    assert built == [1] and signed == [], "the gate must run after the build and before the signature"
    assert sign_fn({"outAmount": "1"}, before_sign=lambda: True) == ("tx", "SIG")
    split = sign_fn.sign_timing()
    assert split["builder"] == "jupiter_swap" and split["build_ms"] is not None
    assert seen["token_snapshot_fn"] is not None


def test_an_injected_token_fn_keeps_the_old_reads(monkeypatch):
    from solders.keypair import Keypair
    from sniper.execution import solana_real_seams as S
    seen = {}
    monkeypatch.setattr(ex, "execute_exit", lambda conn, p, r, n, **kw: seen.update(kw) or {})
    exe = S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://rpc",
                                       http=lambda *a, **k: (200, {"result": None}, None),
                                       usdc_fn=lambda: 1.0, token_fn=lambda _m: 1.0)
    c, pos = _c()
    exe["exit_fn"](c, pos, "mirror", "t")
    assert seen["token_snapshot_fn"] is None


def test_the_quote_seam_skips_its_read_when_given_the_raw_balance(monkeypatch):
    from solders.keypair import Keypair
    from sniper.execution import solana_real_seams as S
    reads, quoted = [], []
    monkeypatch.setattr(S, "token_balance_raw", lambda *a, **k: reads.append(1) or 7)
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda mint, atomic, slip, **k: quoted.append(atomic) or {"outAmount": "1", "routePlan": []})
    seen = {}
    monkeypatch.setattr(ex, "execute_exit", lambda conn, p, r, n, **kw: seen.update(kw) or {})
    exe = S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://rpc",
                                       http=lambda *a, **k: (200, {"result": None}, None))
    c, pos = _c()
    exe["exit_fn"](c, pos, "mirror", "t")
    seen["quote_fn"]("MINT", 1.0, 10.0, atomic=123)
    assert reads == [] and quoted == [123]
    seen["quote_fn"]("MINT", 1.0, 10.0)
    assert reads == [1] and quoted == [123, 7]
