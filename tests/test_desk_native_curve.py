"""Desk native pump.fun curve rung (operator 2026-10-23: everything that helps execution goes on the desk)."""
import sqlite3
import time

import pytest
from solders.keypair import Keypair

from sniper.execution import desk_native_curve as D
from sniper.execution import pump_curve_buy as P

USER, MINT, CREATOR = (str(Keypair().pubkey()) for _ in range(3))
FEES = tuple(str(Keypair().pubkey()) for _ in range(3))
STATE = {"slot": 9, "virtual_token_reserves": 1_000_000_000_000_000, "virtual_sol_reserves": 30_000_000_000,
         "real_token_reserves": 793_100_000_000_000, "real_sol_reserves": 1, "fee_basis_points": 95,
         "creator_fee_basis_points": 30, "creator": CREATOR, "token_program": P.TOKEN_PROGRAM, "complete": False}
LEG = {"ixs": [], "tables": [], "usdc_atomic": 25_000_000, "min_out_lamports": 100_000_000}
BLOCKHASH = "11111111111111111111111111111111"


def price_usd(state, spend, usdc):
    return usdc / 1e6 / (P.expected_tokens_out(spend, state) / 1e6)


def test_signed_floor_is_the_desk_cap_on_the_whole_ticket():
    fair = price_usd(STATE, LEG["min_out_lamports"], LEG["usdc_atomic"])
    msg, info = D.build_desk_curve_message(STATE, LEG, mint=MINT, user=USER, fee_config=FEES,
                                           ref_price_usd=fair, cap_pct=5.0, blockhash=BLOCKHASH)
    assert info["min_tokens_out"] == P.source_price_floor_atomic(25_000_000, fair, 5.0)
    assert 0 < info["min_tokens_out"] < info["expected_tokens_out"] and info["spend_lamports"] == 100_000_000
    buy = msg.instructions[-1]
    assert bytes(buy.data) == P.buy_exact_sol_in_data(100_000_000, info["min_tokens_out"])


def test_a_curve_above_the_desk_cap_refuses_before_signing():
    fair = price_usd(STATE, LEG["min_out_lamports"], LEG["usdc_atomic"])
    with pytest.raises(ValueError, match="desk cap refuses"):
        D.build_desk_curve_message(STATE, LEG, mint=MINT, user=USER, fee_config=FEES,
                                   ref_price_usd=fair * 0.9, cap_pct=5.0, blockhash=BLOCKHASH)


@pytest.mark.parametrize("bad", [dict(complete=True), dict(reason="curve_complete"),
                                 dict(token_program="x")])
def test_unusable_state_or_fee_config_refuses(bad):
    with pytest.raises(ValueError):
        D.build_desk_curve_message({**STATE, **bad}, LEG, mint=MINT, user=USER, fee_config=FEES,
                                   ref_price_usd=1.0, cap_pct=5.0, blockhash=BLOCKHASH)
    with pytest.raises(ValueError):
        D.build_desk_curve_message(STATE, LEG, mint=MINT, user=USER, fee_config=None,
                                   ref_price_usd=1.0, cap_pct=5.0, blockhash=BLOCKHASH)


@pytest.mark.parametrize("quote,miss,cap,acc,want", [
    (None, {"code": "no_route"}, {"ref_price_usd": 1}, None, True),
    (None, {"code": "rate_limited"}, {"ref_price_usd": 1}, None, False),     # unknown is not no route
    (None, {"code": "no_route"}, None, None, False),                          # not a desk entry
    (None, {"code": "no_route"}, {"ref_price_usd": 1}, 48, False),            # the oversize retry
    ({"outAmount": "1"}, {"code": "no_route"}, {"ref_price_usd": 1}, None, False),
    (None, None, {"ref_price_usd": 1}, None, False)])
def test_the_rung_runs_only_after_a_typed_desk_no_route(quote, miss, cap, acc, want):
    assert D.desk_no_route(quote, miss, cap, acc) is want


def test_entry_maps_each_refusal_and_never_reads_past_a_refused_curve(monkeypatch):
    import sniper.services.memecoin_rabbit_signal as R
    calls = []
    monkeypatch.setattr(R, "read_curve", lambda mint, rpc, **kw: {"reason": "mayhem_or_fee_unmodelled"})
    monkeypatch.setattr(P, "prefetch_usdc_sol_leg", lambda *a, **k: calls.append("leg"))
    got = D.desk_curve_entry(MINT, 25.0, {"ref_price_usd": 1.0, "cap_pct": 5.0}, user=USER,
                             rpc=lambda *a: None, jup_call=None, fee_config_fn=lambda: FEES)
    assert got == (None, "desk_native_curve_mayhem_or_fee_unmodelled") and calls == []

    def boom(*a, **k):
        raise TimeoutError("rpc")
    monkeypatch.setattr(R, "read_curve", boom)
    assert D.desk_curve_entry(MINT, 25.0, {"ref_price_usd": 1, "cap_pct": 5}, user=USER, rpc=None, jup_call=None,
                              fee_config_fn=lambda: FEES)[1] == "desk_native_curve_read_failed: TimeoutError"


def test_entry_builds_with_a_leg_and_blockhash(monkeypatch):
    import sniper.services.memecoin_rabbit_signal as R
    def read_ok(mint, rpc, raw_out=None, **kw):
        import base64
        from sniper.execution.solana_native_entry_quote import accounts_for
        raw_out[accounts_for(mint)["curve"]] = {"data": [base64.b64encode(bytes(166)).decode(), "base64"]}
        return dict(STATE)
    monkeypatch.setattr(R, "read_curve", read_ok)
    monkeypatch.setattr(P, "prefetch_usdc_sol_leg", lambda usdc, bps, **k: dict(LEG, usdc_atomic=usdc))
    rpc = lambda method, params: {"value": {"blockhash": BLOCKHASH, "lastValidBlockHeight": 7}}  # noqa: E731
    fair = price_usd(STATE, LEG["min_out_lamports"], LEG["usdc_atomic"])
    msg, validity = D.desk_curve_entry(MINT, 25.0, {"ref_price_usd": fair, "cap_pct": 5.0}, user=USER,
                                       rpc=rpc, jup_call=None, fee_config_fn=lambda: FEES)
    assert msg is not None and validity["last_valid_height"] == 7 and validity["target_mint"] == MINT
    assert set(validity["native_curve_ms"]) == {"curve_read_ms", "leg_source", "leg_ms", "blockhash_ms", "total_ms"}
    assert validity["native_curve_ms"]["leg_source"] == "cold"


def _seam(monkeypatch, miss_code):
    from sniper.execution import solana_real_seams as S
    from sniper.execution import solana_swap_executor
    from sniper.execution.submission_transport import AttemptState
    from sniper.persistence import db_memecoin as m
    from test_memecoin_fast_submit_wiring import StubTransport
    monkeypatch.setattr(S.EntryQuoteMissRecorder, "quote", lambda self, *a, **k: None)
    monkeypatch.setattr(S.EntryQuoteMissRecorder, "miss", lambda self: {"code": miss_code, "detail": "jup"})
    monkeypatch.setattr(D, "desk_curve_entry", lambda *a, **k: (None, "desk_native_curve_curve_complete"))

    def fake_execute_entry(conn, **kw):
        if not kw["quote_fn"]("MINT", 10.0):
            m.release_pending(conn, kw["pending_id"], "quote_unavailable")
            return {"status": "aborted", "reason": "no quote (executor)"}
        return {"status": "filled"}
    monkeypatch.setattr(solana_swap_executor, "execute_entry", fake_execute_entry)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.reserve_pending(conn, mint="MINT", source_user_id="u", strategy="desk", notional=10.0,
                            reserved_at="t", network_id="solana", attempt_id="a")
    ex = S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://rpc",
                                      http=lambda *a, **k: (200, {"result": None}, None),
                                      submission_transport=StubTransport(AttemptState.ACCEPTED),
                                      usdc_fn=lambda: 10.0, token_fn=lambda _m: 0.0)
    return conn, pid, ex, m


def test_seam_desk_no_route_takes_the_rung_and_a_refusal_releases_the_reservation(monkeypatch):
    conn, pid, ex, m = _seam(monkeypatch, "no_route")
    out = ex["execute_fn"](conn, pid, "MINT", 10.0, "t", desk_price_cap={"ref_price_usd": 1.0, "cap_pct": 5.0})
    assert out["desk_native_curve"] == "desk_native_curve_curve_complete"
    assert out["reason"].startswith("no quote (no_route: jup; native curve:")
    row = m.pending_row(conn, pid)
    assert row["status"] == "failed" and row["tx_sig"] is None and row["signed_payload_b64"] is None


@pytest.mark.parametrize("code,cap", [("rate_limited", {"ref_price_usd": 1.0, "cap_pct": 5.0}),
                                      ("no_route", None)])
def test_seam_other_misses_and_non_desk_entries_keep_todays_abort(monkeypatch, code, cap):
    conn, pid, ex, _m = _seam(monkeypatch, code)
    out = ex["execute_fn"](conn, pid, "MINT", 10.0, "t", desk_price_cap=cap)
    assert out == {"status": "aborted", "reason": "no quote (executor)"}


@pytest.mark.parametrize("raiser", ["leg", "exit"])
def test_seam_any_rung_exception_releases_the_reservation_and_signs_nothing(monkeypatch, raiser):
    conn, pid, ex, m = _seam(monkeypatch, "no_route")
    if raiser == "leg":
        def boom(*a, **k):
            raise TimeoutError("jupiter")
        monkeypatch.setattr(D, "desk_curve_entry", boom)
    else:
        from sniper.execution import pump_native_exit
        monkeypatch.setattr(D, "desk_curve_entry", lambda *a, **k: ("MSG", {"native_curve": {"min_tokens_out": 5},
                                                                           "native_curve_ms": {}}))
        monkeypatch.setattr(pump_native_exit, "build_native_exit", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    out = ex["execute_fn"](conn, pid, "MINT", 10.0, "t", desk_price_cap={"ref_price_usd": 1.0, "cap_pct": 5.0})
    assert out["status"] == "aborted" and "native curve" in out["reason"]
    row = m.pending_row(conn, pid)
    assert row["status"] == "failed" and row["tx_sig"] is None and row["signed_payload_b64"] is None


@pytest.mark.parametrize("flag,want", [(1, "desk_native_curve: mayhem until sell feasibility"),
                                        (None, "desk_native_curve_mayhem_unknown")])
def test_a_mayhem_or_unreadable_curve_is_refused_before_any_leg(monkeypatch, flag, want):
    import base64
    import sniper.services.memecoin_rabbit_signal as R
    from sniper.execution.solana_native_entry_quote import accounts_for
    calls = []

    def read(mint, rpc, raw_out=None, **kw):
        data = bytearray(166)
        if flag is not None:
            data[81] = flag
        raw_out[accounts_for(mint)["curve"]] = ({"data": [base64.b64encode(bytes(data[:81] if flag is None else data)).decode(), "base64"]})
        return dict(STATE)
    monkeypatch.setattr(R, "read_curve", read)
    monkeypatch.setattr(P, "prefetch_usdc_sol_leg", lambda *a, **k: calls.append("leg"))
    got = D.desk_curve_entry(MINT, 25.0, {"ref_price_usd": 1.0, "cap_pct": 5.0}, user=USER,
                             rpc=lambda *a: None, jup_call=None, fee_config_fn=lambda: FEES)
    assert got == (None, want) and calls == []


def test_the_rung_message_carries_exactly_the_desk_sender_tip_and_the_guard_accepts_it():
    from sniper.execution import solana_sender_ab as S
    fair = price_usd(STATE, LEG["min_out_lamports"], LEG["usdc_atomic"])
    arm = S.assign_desk_fast_send("attempt-x")
    tip = (arm["tip_account"], arm["sender_tip_lamports"])
    msg, _ = D.build_desk_curve_message(STATE, LEG, mint=MINT, user=USER, fee_config=FEES,
                                        ref_price_usd=fair, cap_pct=5.0, blockhash=BLOCKHASH, tip=tip)
    assert S.tip_transfers(msg) == [tip] and S.tip_transfers(msg, S._NOZOMI_SET) == []
    assert S.guard_refusal(msg, 5_000, 0) is None and S.guard_refusal(msg, 0, 0) is not None
    plain, _ = D.build_desk_curve_message(STATE, LEG, mint=MINT, user=USER, fee_config=FEES,
                                          ref_price_usd=fair, cap_pct=5.0, blockhash=BLOCKHASH)
    assert S.tip_transfers(plain) == []


def test_entry_passes_the_tip_into_the_message_and_its_validity(monkeypatch):
    import base64
    import sniper.services.memecoin_rabbit_signal as R
    from sniper.execution import solana_sender_ab as S
    from sniper.execution.solana_native_entry_quote import accounts_for

    def read_ok(mint, rpc, raw_out=None, **kw):
        raw_out[accounts_for(mint)["curve"]] = {"data": [base64.b64encode(bytes(166)).decode(), "base64"]}
        return dict(STATE)
    monkeypatch.setattr(R, "read_curve", read_ok)
    monkeypatch.setattr(P, "prefetch_usdc_sol_leg", lambda usdc, bps, **k: dict(LEG, usdc_atomic=usdc))
    rpc = lambda method, params: {"value": {"blockhash": BLOCKHASH, "lastValidBlockHeight": 7}}  # noqa: E731
    fair = price_usd(STATE, LEG["min_out_lamports"], LEG["usdc_atomic"])
    tip = (S.TIP_ACCOUNTS[0], S.TIP_LAMPORTS)
    msg, validity = D.desk_curve_entry(MINT, 25.0, {"ref_price_usd": fair, "cap_pct": 5.0}, user=USER,
                                       rpc=rpc, jup_call=None, fee_config_fn=lambda: FEES, tip=tip)
    assert S.tip_transfers(msg) == [tip] and validity["sender_tip_lamports"] == S.TIP_LAMPORTS
    _msg2, v2 = D.desk_curve_entry(MINT, 25.0, {"ref_price_usd": fair, "cap_pct": 5.0}, user=USER,
                                   rpc=rpc, jup_call=None, fee_config_fn=lambda: FEES)
    assert v2["sender_tip_lamports"] == 0


def test_a_holder_reward_curve_is_refused_before_any_leg(monkeypatch):
    import base64
    import sniper.services.memecoin_rabbit_signal as R
    from sniper.execution.solana_native_entry_quote import accounts_for
    calls = []

    def read(mint, rpc, raw_out=None, **kw):
        raw_out[accounts_for(mint)["curve"]] = {"data": [base64.b64encode(bytes(166)).decode(), "base64"]}
        return dict(STATE, is_holder_reward=True)
    monkeypatch.setattr(R, "read_curve", read)
    monkeypatch.setattr(P, "prefetch_usdc_sol_leg", lambda *a, **k: calls.append("leg"))
    got = D.desk_curve_entry(MINT, 25.0, {"ref_price_usd": 1.0, "cap_pct": 5.0}, user=USER,
                             rpc=lambda *a: None, jup_call=None, fee_config_fn=lambda: FEES)
    assert got == (None, "desk_native_curve_holder_reward_unmodelled") and calls == []


class _T:
    def __init__(self, attempt_id):
        self.attempt_id, self.stages = attempt_id, {}

    def mark_substage(self, name, **kw):
        self.stages[name] = kw

    def mark(self, *a, **k):
        pass


@pytest.mark.parametrize("trace_id,want", [("trace-attempt", "trace-attempt"), (None, None)])
def test_the_rung_s_tip_arm_comes_from_the_trace_first_and_never_skips_silently(monkeypatch, trace_id, want):
    from sniper.execution import solana_sender_ab as S
    conn, pid, ex, m = _seam(monkeypatch, "no_route")
    conn.execute("UPDATE memecoin_pending_entries SET attempt_id=NULL WHERE id=?", (pid,))   # NULL before link
    trace = _T(trace_id)
    ex["execute_fn"](conn, pid, "MINT", 10.0, "t", trace=trace, desk_price_cap={"ref_price_usd": 1.0, "cap_pct": 5.0})
    stage = trace.stages["desk_fast_send"]
    if want:
        assert stage["attempt_hash"] == S.assign_desk_fast_send(want)["attempt_hash"]
    else:
        assert stage["arm"] == "untipped_no_attempt_id"


def test_a_fault_in_the_rung_s_fast_send_prep_releases_the_reservation(monkeypatch):
    conn, pid, ex, m = _seam(monkeypatch, "no_route")

    class Boom(_T):
        def mark_substage(self, name, **kw):
            if name == "desk_fast_send" and kw.get("path") == "native_curve":
                raise RuntimeError("trace sink down")
            super().mark_substage(name, **kw)
    out = ex["execute_fn"](conn, pid, "MINT", 10.0, "t", trace=Boom("a1"),
                           desk_price_cap={"ref_price_usd": 1.0, "cap_pct": 5.0})
    assert out["status"] == "aborted" and "native curve error: RuntimeError" in out["reason"]
    row = m.pending_row(conn, pid)
    assert row["status"] == "failed" and row["tx_sig"] is None and row["signed_payload_b64"] is None


def test_a_curve_inside_the_cap_but_without_headroom_refuses(monkeypatch):
    """T/ACC (2026-10-23): no-headroom-no-send on the native rung too; the signed floor stays at the cap."""
    from sniper.settings import memecoin as cfg
    fair = price_usd(STATE, LEG["min_out_lamports"], LEG["usdc_atomic"])
    ref = fair / 1.048                                   # the curve sits +4.8 % over the reference, cap 5 %
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CAP_MIN_HEADROOM_PCT", 0.5)
    with pytest.raises(ValueError, match="headroom"):
        D.build_desk_curve_message(STATE, LEG, mint=MINT, user=USER, fee_config=FEES,
                                   ref_price_usd=ref, cap_pct=5.0, blockhash=BLOCKHASH)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CAP_MIN_HEADROOM_PCT", 0.0)
    _msg, info = D.build_desk_curve_message(STATE, LEG, mint=MINT, user=USER, fee_config=FEES,
                                            ref_price_usd=ref, cap_pct=5.0, blockhash=BLOCKHASH)
    assert info["min_tokens_out"] == P.source_price_floor_atomic(25_000_000, ref, 5.0)
