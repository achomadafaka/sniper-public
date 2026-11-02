"""S6 (operator 2026-10-22, 'make sure it just won't happen'): a desk entry never fills above the price
its verdict saw. The queue freezes ref (U snapshot curve price) and cap = tolerance + modelled curve fee
+ modelled impact of the clip on the snapshot's real quote reserves into the desk metadata; the leg
refuses a desk entry without it; the executor refuses a quote above it and signs that minimum."""
import pytest

from sniper.execution import solana_swap_executor as ex
from sniper.services import memecoin_desk as D
from sniper.services import memecoin_origin as O
from sniper.services.memecoin_desk_universe import Field
from sniper.settings import memecoin as cfg
from test_memecoin_desk_contract import NOW, candidate, verdict
PLAN = {"inv": ["px", -12], "h_s": 300, "tz": [20, 60]}
from test_memecoin_desk_origin import VERDICT_PRICE
from test_memecoin_dip_retry_max_premium import _leg_entry  # noqa: F401
from test_memecoin_entry_chase_quote_recheck import _c, _Deps
from test_memecoin_desk_pipeline import databases  # noqa: F401  (fixture)


# ------------------------------------------------------------------ the queue's frozen cap

def test_the_cap_is_tolerance_plus_fee_plus_clip_impact_on_the_snapshot_reserves():
    vp = D.verdict_price(candidate(), clip_usd=25)
    assert vp["ref_price_usd"] == pytest.approx(41000.0 / 1e9)
    assert vp["impact_pct"] == pytest.approx(25 / 5000 * 100)
    assert vp["cap_pct"] == pytest.approx(3 + 1.25 + 0.5)
    assert vp["cap_price_usd"] == pytest.approx(vp["ref_price_usd"] * (1 + vp["cap_pct"] / 100))
    assert O.validate_verdict_price(vp) == (True, None)


@pytest.mark.parametrize("fields", [
    {"liq_usd": Field(None, "pumpfun_grpc:real_quote_reserves", NOW, 7, "missing")},
    {"liq_usd": Field(0.0, "pumpfun_grpc:real_quote_reserves", NOW, 7, "complete")},
    {"mcap_usd": Field(41000.0, "pumpfun_grpc:curve_reserves", NOW, 7, "partial")},
    {"mcap_usd": Field(41000.0, "dexscreener:fdv", NOW, 7, "complete")},      # not the curve: no Pump supply
])
def test_an_unpriceable_snapshot_has_no_cap(fields):
    assert D.verdict_price(candidate(**fields), clip_usd=25) is None


@pytest.mark.parametrize("change", [
    {"cap_pct": 0}, {"ref_price_usd": 0}, {"cap_price_usd": 1.0}, {"impact_pct": float("nan")},
    {"fee_pct": True}, {"source": ""}, {"extra": 1}])
def test_a_malformed_cap_is_refused_by_the_metadata_validator(change):
    meta = {"cohort_id": "c", "producer": "U", "opportunity_id": "o", "snapshot_hash": "a" * 64,
            "verdict_price": {**VERDICT_PRICE, **change}}
    assert O.validate_desk_metadata(meta)[0] is False


def test_metadata_without_a_cap_still_validates_so_the_leg_can_name_the_refusal():
    meta = {"cohort_id": "c", "producer": "U", "opportunity_id": "o", "snapshot_hash": "a" * 64}
    assert O.validate_desk_metadata(meta) == (True, None)


# ------------------------------------------------------------------ the leg never runs uncapped

class _Ev:
    def __init__(self, desk):
        self.desk = desk


@pytest.mark.parametrize("desk, want", [
    ({"ticket_id": "t"}, "desk_verdict_price_missing"),
    ({"ticket_id": "t", "verdict_price": {**VERDICT_PRICE, "cap_pct": -1}}, "desk_verdict_price_invalid"),
])
def test_a_desk_entry_without_a_valid_cap_is_refused(desk, want):
    from sniper.services.memecoin_real_leg import desk_price_cap
    assert desk_price_cap(_Ev(desk), lambda conn, **kw: None) == (want, None)


def test_a_desk_entry_is_refused_by_an_executor_that_cannot_enforce_the_cap():
    from sniper.services.memecoin_real_leg import desk_price_cap
    ev = _Ev({"ticket_id": "t", "verdict_price": VERDICT_PRICE})
    assert desk_price_cap(ev, lambda conn, *, pending_id=None: None) == ("desk_verdict_price_unsupported", None)
    assert desk_price_cap(ev, lambda conn, **kw: None) == (None, VERDICT_PRICE)


def test_a_non_desk_entry_carries_no_cap():
    from sniper.services.memecoin_real_leg import desk_price_cap
    assert desk_price_cap(type("E", (), {})(), lambda conn: None) == (None, None)


# ------------------------------------------------------------------ the executor enforces it

CAP = {**VERDICT_PRICE, "ref_price_usd": 1.0, "cap_pct": 4.75, "cap_price_usd": 1.0475}


def _sol_entry(monkeypatch, *, out, cap, movement_only=1, sign=None):
    """$10 buys out/1e6 tokens: quote price = 10 / (out / 1e6). No chase, no copy cap (desk)."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 0.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", movement_only, raising=False)
    c, pid = _c(source_price=None)
    d, signed = _Deps(), []
    kw = {"desk_price_cap": cap} if cap is not None else {}
    result = ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b", source_user_id=None,
        now_iso="2026-10-22T10:40:00+00:00",
        quote_fn=lambda *_: {"inAmount": "10000000", "outAmount": str(out),
                             "otherAmountThreshold": str(int(out * 0.85)), "slippageBps": 1500,
                             "priceImpactPct": "0.001"},
        reverse_quote_fn=lambda _, amount: {"inAmount": str(amount), "outAmount": "9900000",
                                            "otherAmountThreshold": "9900000"},
        usdc_fn=d.usdc_fn, token_fn=lambda _: (0.0, 6), sign_fn=sign or (lambda q: signed.append(q)),
        send_fn=d.send_fn, confirm_fn=d.confirm_fn, **kw)
    row = c.execute("SELECT status, resolution_reason FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone()
    c.close()
    return result, signed, d, row


@pytest.mark.parametrize("movement_only", [0, 1])
def test_a_quote_above_the_cap_is_refused_before_signing_and_releases(monkeypatch, movement_only):
    result, signed, d, row = _sol_entry(monkeypatch, out=9_000_000, cap=CAP, movement_only=movement_only)  # +11.1 %
    assert result["status"] == "skipped_chase" and result["reason"].startswith("desk verdict price:")
    assert not signed and "send" not in d.calls
    assert tuple(row) == ("failed", "desk_verdict_price")


def test_a_quote_inside_the_cap_signs_a_minimum_at_the_cap(monkeypatch):
    result, signed, _d, _row = _sol_entry(monkeypatch, out=9_708_737, cap=CAP)                # +3.0 %
    assert len(signed) == 1
    assert 10.0 / (int(signed[0]["otherAmountThreshold"]) / 1e6) <= 1.0475 + 1e-9


def test_without_a_cap_the_same_quote_signs_its_own_slippage_minimum(monkeypatch):
    _result, signed, _d, _row = _sol_entry(monkeypatch, out=9_000_000, cap=None)
    assert len(signed) == 1 and 10.0 / (int(signed[0]["otherAmountThreshold"]) / 1e6) > 1.0475


def test_an_unpriceable_quote_is_refused_under_a_cap(monkeypatch):
    result, signed, _d, _row = _sol_entry(monkeypatch, out=0, cap=CAP)
    assert result["status"] != "filled" and not signed


def test_the_funded_solana_binding_forwards_the_cap(monkeypatch):
    import sqlite3
    import time
    from solders.keypair import Keypair
    from sniper.execution import solana_real_seams as S
    from sniper.execution.submission_transport import AttemptState
    from sniper.persistence import db_memecoin as m
    from test_memecoin_fast_submit_wiring import StubTransport
    seen = []
    monkeypatch.setattr(ex, "execute_entry",
                        lambda conn, **kw: seen.append(kw.get("desk_price_cap")) or {"status": "aborted", "reason": "stub"})
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.reserve_pending(conn, mint="MINT", source_user_id="u", strategy="alpha_b",
                            notional=10.0, reserved_at="t", network_id="solana", attempt_id="a")
    ex_ = S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://rpc",
                                       http=lambda *a, **k: (200, {"result": None}, None),
                                       submission_transport=StubTransport(AttemptState.ACCEPTED),
                                       usdc_fn=lambda: 10.0, token_fn=lambda _m: 0.0)
    ex_["execute_fn"](conn, pid, "MINT", 10.0, "t", desk_price_cap=CAP,
                      entry_min_slot=100, entry_max_slot=101, entry_head_fn=lambda: (100, time.monotonic()))
    assert seen == [CAP]


# ------------------------------------------------------------------ stale_on_enter still comes first

def test_an_expired_request_never_reaches_the_ledger_even_with_a_cap(databases):  # noqa: F811
    from test_memecoin_desk_pipeline import ready, decide, LATER
    from sniper.services import memecoin_manual_intents as intents
    from sniper.persistence import db_memecoin_desk as store
    _cohort, ticket = ready(databases)
    assert decide(databases, ticket)["action"] == "ENTER"
    [request] = store.list_outbox(databases[0])
    assert "verdict_price" in request["payload"]["desk_metadata"]
    [out] = D.dispatch_outbox(*databases, now_iso=LATER)
    assert out["state"] == "expired" and intents.pending_count(databases[1]) == 0


# ------------------------------------------------------------------ Gamma's pins (S6 review LOW)

def test_the_pre_sign_refusal_stands_on_its_own_with_a_permissive_floor(monkeypatch):
    monkeypatch.setattr(ex, "cap_entry_quote_floor", lambda quote, *a, **k: quote)
    result, signed, d, row = _sol_entry(monkeypatch, out=9_000_000, cap=CAP)              # +11.1 %
    assert result["reason"].startswith("desk verdict price: exact quote is +") and not signed
    assert tuple(row) == ("failed", "desk_verdict_price")


def test_the_unknown_quote_refusal_stands_on_its_own_with_a_permissive_floor(monkeypatch):
    monkeypatch.setattr(ex, "cap_entry_quote_floor", lambda quote, *a, **k: quote)
    monkeypatch.setattr(ex, "positive_price", lambda v: None)                              # unpriceable quote
    result, signed, _d, _row = _sol_entry(monkeypatch, out=9_708_737, cap=CAP)
    assert "unknown over the verdict price" in result.get("reason", "") and not signed


def test_an_oversize_retry_still_carries_the_cap(monkeypatch):
    seen = []
    real = ex.execute_entry

    def spy(conn, **kw):
        seen.append(kw.get("desk_price_cap"))
        return real(conn, **kw)
    import base64
    from sniper.execution import submission_wiring
    monkeypatch.setattr(submission_wiring, "SOLANA_PACKET_LIMIT", 1)                       # every tx is oversize
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS", 20)
    monkeypatch.setattr(ex, "execute_entry", spy)
    _sol_entry(monkeypatch, out=9_708_737, cap=CAP,
               sign=lambda q: (base64.b64encode(b"x" * 64).decode(), "sig"))
    assert len(seen) >= 2 and all(s == CAP for s in seen)          # the first call and its retry


def test_the_cap_prices_the_as_of_candidate_the_judge_saw(databases, monkeypatch):  # noqa: F811
    """With lambda/judge-asof-refresh the vet stage carries inputs['asof']['candidate']; the cap must
    read that one, never the emission snapshot."""
    from test_memecoin_desk_pipeline import ready, decide
    from sniper.persistence import db_memecoin_desk as store
    _cohort, ticket = ready(databases)
    refreshed = candidate(mcap_usd=Field(50000.0, "pumpfun_grpc:curve_reserves", NOW, 7, "complete"))
    real = store.record_stage

    def with_asof(conn, **kw):
        if kw.get("stage") == "vet":
            kw["inputs"] = {**kw["inputs"], "asof": {"candidate": refreshed}}
        return real(conn, **kw)
    monkeypatch.setattr(store, "record_stage", with_asof)
    assert decide(databases, ticket)["action"] == "ENTER"
    [request] = store.list_outbox(databases[0])
    vp = request["payload"]["desk_metadata"]["verdict_price"]
    assert vp["basis"] == "asof" and vp["ref_price_usd"] == pytest.approx(50000.0 / 1e9)
    sheet = D.contract.render_judge_sheet(refreshed)
    assert sheet["mcap_usd"] / 1e9 == pytest.approx(vp["ref_price_usd"])       # the price the JUDGE saw
    assert O.validate_verdict_price(vp) == (True, None)


@pytest.mark.parametrize("basis", ["bogus", None, "missing"])
def test_a_bad_or_missing_basis_is_refused_by_the_validator_and_the_leg(basis):
    from sniper.services.memecoin_real_leg import desk_price_cap
    vp = dict(VERDICT_PRICE)
    if basis == "missing":
        vp.pop("basis")
    else:
        vp["basis"] = basis
    assert O.validate_verdict_price(vp)[0] is False
    assert desk_price_cap(_Ev({"ticket_id": "t", "verdict_price": vp}), lambda conn, **kw: None) == (
        "desk_verdict_price_invalid", None)


def test_a_quote_inside_the_cap_but_without_headroom_is_not_sent(monkeypatch):
    """T/ACC (16469fe1, 2026-10-23): 0.19% under the cap, landed past the floor (0x1771)."""
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CAP_MIN_HEADROOM_PCT", 0.5)
    result, signed, d, row = _sol_entry(monkeypatch, out=9_569_378, cap=CAP)                 # +4.5 % of 4.75
    assert result["status"] == "skipped_chase" and "min headroom 0.50%" in result["reason"]
    assert not signed and "send" not in d.calls
    assert tuple(row) == ("failed", "desk_verdict_price")
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CAP_MIN_HEADROOM_PCT", 0.0)
    _result, signed, _d, _row = _sol_entry(monkeypatch, out=9_569_378, cap=CAP)
    assert len(signed) == 1, "knob 0 keeps the old cap-only behaviour"


# ------------------------------------------------------------------ fill drift (operator 2026-10-23, pos 74173 +6.7%)

DRIFT = {"drift_k": 2.0, "noise_pct": 0.5, "drift_limit_pct": 1.0, "drift_enforce": 1}            # fee 1.25 + impact 0.5 are netted out of the move


def test_a_move_over_k_noise_net_of_fee_and_impact_is_refused_before_signing_and_releases(monkeypatch):
    result, signed, d, row = _sol_entry(monkeypatch, out=9_708_737, cap={**CAP, **DRIFT})    # +3.0 % quote, drift 1.25 > 1.0
    assert result["status"] == "skipped_chase" and result["reason"].startswith("desk drift:")
    assert result["desk_drift_pct"] == pytest.approx(1.25, abs=1e-3) and result["desk_drift_limit_pct"] == 1.0
    assert not signed and "send" not in d.calls
    assert tuple(row) == ("failed", "desk_drift")


def test_a_move_inside_the_limit_or_a_falling_price_or_no_drift_keys_still_signs(monkeypatch):
    _r, signed, _d, _row = _sol_entry(monkeypatch, out=9_708_737, cap={**CAP, **DRIFT, "noise_pct": 1.0, "drift_limit_pct": 2.0})
    assert len(signed) == 1
    _r, signed, _d, _row = _sol_entry(monkeypatch, out=10_500_000, cap={**CAP, **DRIFT})     # price fell 4.8 %: drift negative
    assert len(signed) == 1
    _r, signed, _d, _row = _sol_entry(monkeypatch, out=9_708_737, cap=CAP)                   # unknown noise: fixed cap only
    assert len(signed) == 1


@pytest.mark.parametrize("change", [{"drift_limit_pct": 3.0}, {"noise_pct": 0.0, "drift_limit_pct": 0.0}, {"drift_k": float("nan")}])
def test_a_malformed_or_inconsistent_drift_block_is_refused_by_the_validator(change):
    assert O.validate_verdict_price({**VERDICT_PRICE, **DRIFT, **change})[0] is False
    assert O.validate_verdict_price({**VERDICT_PRICE, "drift_k": 2.0})[0] is False           # all three keys or none
    assert O.validate_verdict_price({**VERDICT_PRICE, **DRIFT}) == (True, None)


def _standing_ticket(databases, monkeypatch, **extra):
    from test_memecoin_desk_pipeline import AT, POLICY, api
    from test_memecoin_desk_contract import evidence, policy
    standing = {"version": "watch-v1", "watch_max_s": 1800, "graduation_guard_pct": 90, "dead_tape_noise_k": 3, "px_hold_max_noise": 2,
                "px_hold_hold_s": 5, "px_hold_max_wait_s": 60, **extra}
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_STANDING_ORDERS", True)
    cohort = api().provision_cohort(*databases, producer="U", policy={**POLICY, "standing_orders": standing},
                                    prompt_version="desk-judge-v22", created_at=AT)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], ticket, evidence=evidence(), policy=policy(), now_iso=AT)
    return ticket


def _structure(tr=0.4, swing=None, bars=20):
    from test_desk_structure_sheet import real_structure
    st = real_structure()
    st["volatility"] = {"median_5s_swing_pct": swing, "bars": bars, "median_5s_true_range_pct": tr, "true_range_bars": bars}
    return st


def test_the_cohort_k_times_the_sheets_true_range_noise_is_frozen_into_the_cap(databases, monkeypatch):  # noqa: F811
    from test_memecoin_desk_pipeline import decide
    from sniper.persistence import db_memecoin_desk as store
    ticket = _standing_ticket(databases, monkeypatch, drift_k=2.0)
    assert decide(databases, ticket, {**verdict(), "p": PLAN}, structure=_structure())["action"] == "ENTER"
    vp = store.list_outbox(databases[0])[0]["payload"]["desk_metadata"]["verdict_price"]
    assert (vp["drift_k"], vp["noise_pct"], vp["drift_limit_pct"]) == (2.0, 0.4, pytest.approx(0.8))
    assert O.validate_verdict_price(vp) == (True, None)


@pytest.mark.parametrize("extra, structure, enabled", [({}, _structure(), 1),                                   # cohort has no drift_k
                                                       ({"drift_k": 2.0}, _structure(tr=None), 1),              # unknown noise
                                                       ({"drift_k": 2.0}, _structure(bars=5), 1),               # < 12 bars
                                                       ({"drift_k": 2.0}, _structure(), 0)])                    # global kill
def test_no_drift_k_unknown_noise_or_the_kill_leaves_the_cap_without_a_drift_gate(databases, monkeypatch, extra, structure, enabled):  # noqa: F811
    from test_memecoin_desk_pipeline import decide
    from sniper.persistence import db_memecoin_desk as store
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_DRIFT_ENABLED", enabled)
    ticket = _standing_ticket(databases, monkeypatch, **extra)
    assert decide(databases, ticket, {**verdict(), "p": PLAN}, structure=structure)["action"] == "ENTER"
    vp = store.list_outbox(databases[0])[0]["payload"]["desk_metadata"]["verdict_price"]
    assert not (set(vp) & {"drift_k", "noise_pct", "drift_limit_pct"})


def test_px_hold_depth_reads_the_cohort_frozen_noise_basis(databases, monkeypatch):  # noqa: F811
    st = _structure(tr=0.4, swing=3.0)
    assert D._range5_pct(st) == 3.0 and D._range5_pct(st, "swing") == 3.0 and D._range5_pct(st, "true_range") == 0.4
    assert D._range5_pct(_structure(tr=0.4, swing=None)) is None                # an old cohort never reads the new field


def test_log_only_drift_records_the_figure_and_refuses_nothing(monkeypatch):
    seen = []
    monkeypatch.setattr(ex, "_trace_substage", lambda trace, name, **kw: seen.append((name, kw)))
    _r, signed, _d, _row = _sol_entry(monkeypatch, out=9_708_737, cap={**CAP, **DRIFT, "drift_enforce": 0})   # drift 1.25 > limit 1.0
    assert len(signed) == 1
    [(_n, kw)] = [x for x in seen if x[0] == "desk_drift"]
    assert kw["drift_pct"] == pytest.approx(1.25, abs=1e-3) and kw["enforce"] == 0 and kw["quote_vs_verdict_pct"] == pytest.approx(3.0, abs=1e-2)


def test_the_cohort_floor_and_log_only_flag_are_frozen_into_the_cap(databases, monkeypatch):  # noqa: F811
    from test_memecoin_desk_pipeline import decide
    from sniper.persistence import db_memecoin_desk as store
    ticket = _standing_ticket(databases, monkeypatch, drift_k=2.0, drift_enforce=False, drift_noise_floor_pct=0.5)
    assert decide(databases, ticket, {**verdict(), "p": PLAN}, structure=_structure(tr=0.3))["action"] == "ENTER"
    vp = store.list_outbox(databases[0])[0]["payload"]["desk_metadata"]["verdict_price"]
    assert (vp["noise_pct"], vp["drift_limit_pct"], vp["drift_enforce"]) == (0.5, 1.0, 0) and O.validate_verdict_price(vp) == (True, None)


# ------------------------------------------------------------------ limit arm (operator 2026-10-23, choice B)

def test_limit_arm_above_the_sheet_price_is_a_typed_watch_refusal_and_never_signs(monkeypatch):
    seen = []
    real = ex._trace_substage
    monkeypatch.setattr(ex, "_trace_substage", lambda trace, name, **kw: (seen.append((name, kw)), real(trace, name, **kw))[1])
    result, signed, d, row = _sol_entry(monkeypatch, out=9_708_737, cap={**CAP, "limit_arm": 1})     # quote +3.0 %, net 1.25 > 0
    assert result["status"] == "skipped_chase" and result["refusal_code"] == "desk_limit_watch"
    assert result["desk_limit_watch"]["level_price_usd"] == CAP["ref_price_usd"] and result["desk_limit_watch"]["net_drift_pct"] == pytest.approx(1.25, abs=1e-3)
    assert not signed and "send" not in d.calls and tuple(row) == ("failed", "desk_limit_watch")
    assert [kw["watch"] for n, kw in seen if n == "desk_limit_arm"] == [True]


def test_limit_arm_over_the_cap_is_still_a_watch_not_a_cap_refusal(monkeypatch):
    result, signed, _d, row = _sol_entry(monkeypatch, out=9_000_000, cap={**CAP, "limit_arm": 1})   # +11.1 %: over the cap too
    assert result["refusal_code"] == "desk_limit_watch" and tuple(row) == ("failed", "desk_limit_watch") and not signed


def test_limit_arm_at_or_below_the_sheet_price_and_the_control_arm_behave_as_today(monkeypatch):
    _r, signed, _d, _row = _sol_entry(monkeypatch, out=10_000_000 + 175_000, cap={**CAP, "limit_arm": 1})   # quote ~ ref 1.0 -> net drift < 0
    assert len(signed) == 1
    for arm in (None, 0):
        cap = CAP if arm is None else {**CAP, "limit_arm": 0}
        r, signed, _d, row = _sol_entry(monkeypatch, out=9_000_000, cap=cap)                           # over the cap: today's refusal, byte for byte
        assert r["reason"].startswith("desk verdict price:") and "refusal_code" not in r and tuple(row) == ("failed", "desk_verdict_price")
        _r, signed, _d, _row = _sol_entry(monkeypatch, out=9_708_737, cap=cap)
        assert len(signed) == 1                                                                         # above sheet, inside the cap: still buys


def test_limit_arm_with_an_unknown_quote_falls_through_to_todays_refusal(monkeypatch):
    result, signed, _d, _row = _sol_entry(monkeypatch, out=0, cap={**CAP, "limit_arm": 1})
    assert result["status"] != "filled" and not signed and "refusal_code" not in result


@pytest.mark.parametrize("arm", [2, -1, 1.0, True, "1"])
def test_a_malformed_limit_arm_is_refused_by_the_validator(arm):
    assert O.validate_verdict_price({**VERDICT_PRICE, "limit_arm": arm})[0] is False
    assert O.validate_verdict_price({**VERDICT_PRICE, "limit_arm": 1}) == (True, None)
    assert O.validate_verdict_price({**VERDICT_PRICE, **DRIFT, "limit_arm": 0}) == (True, None)


def test_the_arm_is_the_frozen_low_bit_of_sha256_and_only_for_limit_arm_cohorts(databases, monkeypatch):  # noqa: F811
    import hashlib
    from test_memecoin_desk_pipeline import decide
    from sniper.persistence import db_memecoin_desk as store
    assert D.limit_arm_bit("t") == hashlib.sha256(b"t").digest()[-1] & 1
    ticket = _standing_ticket(databases, monkeypatch, limit_arm=True)
    assert decide(databases, ticket, {**verdict(), "p": PLAN})["action"] == "ENTER"
    vp = store.list_outbox(databases[0])[0]["payload"]["desk_metadata"]["verdict_price"]
    assert vp["limit_arm"] == D.limit_arm_bit(ticket) and O.validate_verdict_price(vp) == (True, None)


@pytest.mark.parametrize("extra, enabled", [({}, 1), ({"limit_arm": False}, 1), ({"limit_arm": True}, 0)])
def test_no_cohort_flag_or_the_kill_means_no_ticket_carries_an_arm(databases, monkeypatch, extra, enabled):  # noqa: F811
    from test_memecoin_desk_pipeline import decide
    from sniper.persistence import db_memecoin_desk as store
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_LIMIT_ARM_ENABLED", enabled)
    ticket = _standing_ticket(databases, monkeypatch, **extra)
    assert decide(databases, ticket, {**verdict(), "p": PLAN})["action"] == "ENTER"
    assert "limit_arm" not in store.list_outbox(databases[0])[0]["payload"]["desk_metadata"]["verdict_price"]


FIRED = {**VERDICT_PRICE, "ref_price_usd": 1.0, "tolerance_pct": 0.0, "cap_pct": 1.75, "cap_price_usd": 1.0175, "basis": "trigger"}


def test_a_fired_limit_entry_capped_at_the_frozen_usd_level_refuses_a_fx_risen_quote(monkeypatch):
    """eCat FX RC: the same SOL price after SOL/USD +10% is a quote 10% over the ORIGINAL USD ceiling (ref 1.0, tolerance 0, cap = fee + impact)."""
    assert O.validate_verdict_price(FIRED) == (True, None)
    result, signed, d, row = _sol_entry(monkeypatch, out=9_090_909, cap=FIRED)                  # quote price 1.1 = +10 %
    assert result["status"] == "skipped_chase" and result["reason"].startswith("desk verdict price:")
    assert not signed and "send" not in d.calls and tuple(row) == ("failed", "desk_verdict_price")
    _r, signed, _d, _row = _sol_entry(monkeypatch, out=9_900_990, cap=FIRED)                    # +1.0 %: inside fee+impact less headroom
    assert len(signed) == 1 and 10.0 / (int(signed[0]["otherAmountThreshold"]) / 1e6) <= 1.0175 + 1e-9
