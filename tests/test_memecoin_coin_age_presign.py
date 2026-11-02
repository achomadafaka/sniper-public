"""The coin-age floor for an UNKNOWN pair age, decided from the mint just before signing
(spec 2026-09-28-memecoin-unknown-pair-age-mint-evidence-design.md; operator yes 2026-09-28).

The mint's first-tx lookup starts at classification WITHOUT waiting; the executor reads its cache
after the quote and before the swap build. Not back, failed, or a known pair age: admitted as today."""
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_coin_age as CA
from sniper.services import memecoin_risk as risk
from sniper.settings import memecoin as cfg

sys.path.insert(0, str(Path(__file__).parent))      # `tests.` resolves to a stray package here
MINT = "C1ankMint1111111111111111111111111111111pump"


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    monkeypatch.setattr(CA, "_FIRST_TS", {})
    monkeypatch.setattr(CA, "_IN_FLIGHT", {})
    monkeypatch.setenv("MEMECOIN_REAL_MIN_COIN_AGE_S", "120")
    monkeypatch.delenv("MEMECOIN_REAL_MIN_COIN_AGE_S_solana", raising=False)
    monkeypatch.delenv("MEMECOIN_COIN_AGE_MINT_EVIDENCE", raising=False)
    monkeypatch.setenv("MEMECOIN_COIN_AGE_UNKNOWN_PAIR_CHECK", "1")


def seed(age_s, mint=MINT):
    CA.warm("solana", mint, wait_s=2.0, lookup=lambda chain, key: time.time() - age_s)


def test_the_knob_defaults_off_and_needs_the_mint_evidence_switch():
    assert CA.unknown_pair_check_enabled({}) is False
    assert CA.unknown_pair_check_enabled({"MEMECOIN_COIN_AGE_UNKNOWN_PAIR_CHECK": "1"}) is True
    assert CA.unknown_pair_check_enabled({"MEMECOIN_COIN_AGE_UNKNOWN_PAIR_CHECK": "1",
                                          "MEMECOIN_COIN_AGE_MINT_EVIDENCE": "0"}) is False


def test_a_young_mint_with_no_pair_age_is_refused_before_signing():
    seed(52)                                               # Clank 66478 was 52 s old
    reason, fields = risk.coin_age_presign_check("solana", MINT, None)
    assert reason.startswith("coin_too_young: mint 52s old (pair age unknown), floor 120s")
    assert fields["resolved"] is True and fields["mint_age_s"] == pytest.approx(52, abs=2)


@pytest.mark.parametrize("setup,pair_age,expect_fields", [
    ("old", None, {"resolved": True}),        # the mint is old enough
    ("empty", None, {"resolved": False}),     # the lookup is not back: admitted, and recorded
    ("young", 300.0, None),                   # a KNOWN pair age: the reservation gate decided
    ("young", 0.0, None),                     # 0 is a known age, not an unknown one
])
def test_everything_else_admits(setup, pair_age, expect_fields):
    if setup == "old":
        seed(500)
    elif setup == "young":
        seed(30)
    reason, fields = risk.coin_age_presign_check("solana", MINT, pair_age)
    assert reason is None
    if expect_fields is None:
        assert fields is None
    else:
        assert fields["resolved"] is expect_fields["resolved"]


def test_no_floor_no_check(monkeypatch):
    monkeypatch.setenv("MEMECOIN_REAL_MIN_COIN_AGE_S", "0")
    seed(5)
    assert risk.coin_age_presign_check("solana", MINT, None) == (None, None)


def _classification(pair_age):
    return SimpleNamespace(facts={"pair_age_s": pair_age, "mint": MINT}, coin_band="micro")


@pytest.mark.parametrize("pair_age,knob,expected_wait", [
    (None, "1", 0.0),            # unknown pair age: start the lookup, do not wait
    (None, "0", None),           # knob off: today's behaviour, no lookup
    (60.0, "0", CA.WAIT_S),      # a known young pair age keeps today's bounded wait
    (600.0, "1", None),          # a known old pair age: nothing to do
])
def test_the_hook_starts_the_lookup_without_waiting_only_on_an_unknown_pair_age(
        monkeypatch, pair_age, knob, expected_wait):
    from sniper.services import memecoin_real_runner as R
    monkeypatch.setenv("MEMECOIN_COIN_AGE_UNKNOWN_PAIR_CHECK", knob)
    calls = []
    monkeypatch.setattr(CA, "warm", lambda net, mint, wait_s=CA.WAIT_S, lookup=None: calls.append(wait_s))
    R._warm_mint_age(None, _classification(pair_age), "solana", MINT)
    assert calls == ([] if expected_wait is None else [expected_wait])


def test_evm_never_starts_an_unknown_age_lookup(monkeypatch):
    from sniper.services import memecoin_real_runner as R
    calls = []
    monkeypatch.setattr(CA, "warm", lambda *a, **k: calls.append(k.get("wait_s")))
    R._warm_mint_age(None, _classification(None), "4663", "0xabc")
    assert calls == []


@pytest.mark.parametrize("knob,passed", [("1", True), ("0", False)])
def test_the_leg_hands_the_executor_the_check(monkeypatch, tmp_path, knob, passed):
    # No classification = no pair age: the case the check exists for.
    from test_memecoin_entry_drift import _conn
    from sniper.services import memecoin_arming
    from sniper.services.memecoin_real_leg import real_entry
    monkeypatch.setenv("MEMECOIN_COIN_AGE_UNKNOWN_PAIR_CHECK", knob)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    marks = []
    trace = SimpleNamespace(attempt_id="attempt-age", mark=lambda *a, **k: True,
                            mark_substage=lambda name, **k: marks.append((name, k)) or True)
    ev = SimpleNamespace(fomo_event_id="onchain:s", source_user_id="trader", source_handle="alice",
                         signal_source="onchain_watch", network_id="solana", mint=MINT, ticker="M",
                         side="buy", their_price=0.02, their_market_cap=1e6,
                         created_at="2026-07-15T11:59:59+00:00")
    seen = {}

    def execute_fn(conn, pid, mint, usd, now_iso, impact_cap_pct=None, trace=None, presign_refusal_fn=None):
        seen["fn"] = presign_refusal_fn
        return {"status": "aborted", "reason": "stub"}
    real_entry(_conn(), ev, day="2026-07-15", usdc_balance=300.0, sol_balance=1.0,
               now_iso="2026-07-15T12:00:00+00:00", trade_size=10.0, token_safety_fn=lambda _m: (True, "ok"),
               execute_fn=execute_fn, kill_path=str(tmp_path / "kill"), trace=trace)
    assert "fn" in seen, "the entry never reached the executor"
    assert (seen["fn"] is not None) is passed
    if passed:
        seed(52)
        assert seen["fn"]().startswith("coin_too_young: mint 52s old")
        assert [n for n, _ in marks].count("coin_age_presign") == 1


@pytest.mark.parametrize("pair_age,network,passed", [(None, "solana", True), (45.0, "solana", False),
                                                     (0.0, "solana", False), (None, "4663", False)])
def test_the_check_applies_only_to_an_unknown_solana_pair_age(pair_age, network, passed):
    from sniper.services.memecoin_real_leg import _coin_age_presign_fn
    ev = SimpleNamespace(network_id=network, mint=MINT)
    assert (_coin_age_presign_fn(ev, _classification(pair_age), None) is not None) is passed


def _execute(presign):
    from test_memecoin_entry_chase_quote_recheck import _c, _Deps
    from sniper.execution import solana_swap_executor as ex
    c, pid = _c(source_price=0.01)
    d = _Deps()
    r = ex.execute_entry(c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
                         source_user_id="u1", now_iso="2026-08-19T00:00:00Z", quote_fn=d.quote_fn,
                         reverse_quote_fn=d.reverse_quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
                         confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_fn=d.token_fn,
                         impact_cap_pct=12.0, chase_max_pct=30.0, chase_reference_price=0.018,
                         presign_refusal_fn=presign)
    return r, d, c, pid


def test_the_executor_refuses_before_signing_and_releases_the_reservation():
    r, d, c, pid = _execute(lambda: "coin_too_young: mint 52s old (pair age unknown), floor 120s")
    assert r["status"] == "rejected" and r["reason"].startswith("coin_too_young: mint 52s old")
    assert "sign" not in d.calls and "quote" in d.calls
    row = m.pending_row(c, pid)                            # the slot is freed, and says why
    assert (row["status"], row["resolution_reason"]) == ("failed", "coin_age")


@pytest.mark.parametrize("presign", [None, lambda: None])
def test_the_executor_signs_when_the_check_admits_or_is_absent(presign):
    r, d, _, _ = _execute(presign)
    assert "sign" in d.calls and r["status"] != "rejected"


def test_a_check_that_raises_admits():
    def boom():
        raise RuntimeError("cache broke")
    r, d, _, _ = _execute(boom)
    assert "sign" in d.calls


def test_a_coin_too_young_refusal_is_never_retried():
    from sniper.services import memecoin_real_runner as R
    assert R._is_transient_entry_miss(
        {"status": "rejected", "reason": "coin_too_young: mint 52s old (pair age unknown), floor 120s"}) is False


def test_the_oversize_retry_keeps_the_check():
    import ast
    import inspect
    from sniper.execution import solana_swap_executor as ex
    tree = ast.parse(inspect.getsource(ex.execute_entry))
    inner = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "execute_entry"]
    assert inner and all("presign_refusal_fn" in {k.arg for k in call.keywords} for call in inner)


def test_the_funded_binding_passes_the_check_to_the_executor(monkeypatch):
    from solders.keypair import Keypair
    from sniper.execution import solana_real_seams as S
    from sniper.execution import solana_swap_executor
    from sniper.execution.submission_transport import AttemptState
    from test_memecoin_fast_submit_wiring import StubTransport
    seen = []

    def fake_execute_entry(conn, **kw):
        seen.append(kw.get("presign_refusal_fn"))
        return {"status": "aborted", "reason": "stub"}
    monkeypatch.setattr(solana_swap_executor, "execute_entry", fake_execute_entry)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.reserve_pending(conn, mint="MINT", source_user_id="u", strategy="alpha_b",
                            notional=10.0, reserved_at="t", network_id="solana", attempt_id="a")
    transport = StubTransport(AttemptState.ACCEPTED)
    ex = S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://rpc",
                                      http=lambda *a, **k: (200, {"result": None}, None),
                                      submission_transport=transport,
                                      usdc_fn=lambda: 10.0, token_fn=lambda _m: 0.0)
    check = lambda: None  # noqa: E731
    ex["execute_fn"](conn, pid, "MINT", 10.0, "t", presign_refusal_fn=check,
                     entry_min_slot=100, entry_max_slot=101,
                     entry_head_fn=lambda: (100, time.monotonic()))
    assert seen == [check]
    assert transport.calls == 0


def test_the_mint_lookup_reads_at_confirmed_not_the_finalized_default(monkeypatch):
    # Lambda's review F1: with no commitment the method reads FINALIZED, ~10 s behind (live
    # 2026-09-28 18:45Z, research key, pump.fun program newest row: default median 9.9 s, finalized
    # 10.4 s, confirmed 1.1 s). A mint younger than that returned no rows and was ADMITTED.
    import io
    import json
    sent = []

    def urlopen(req, timeout=None):
        sent.append(json.loads(req.data))
        return io.BytesIO(json.dumps({"result": {"data": [{"blockTime": 1_790_000_000, "err": None}]}}).encode())
    monkeypatch.setenv("ONCHAIN_WATCH_HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=test")
    monkeypatch.setattr(CA.urllib.request, "urlopen", urlopen)
    assert CA._helius_first_ts(MINT) == 1_790_000_000.0
    (body,) = sent
    assert body["method"] == "getTransactionsForAddress" and body["params"][0] == MINT
    assert body["params"][1]["commitment"] == "confirmed" and body["params"][1]["sortOrder"] == "asc"
