"""Shadow of the impact cap: our OWN round trip at OUR clip on every impact refusal (2026-09-28).

Operator order via Alpha: "we need to be able to fill here, not by removing the blocker but by making
a good blocker; how we measure is no good". The gate reads Jupiter's priceImpactPct, which prices the
output at Jupiter's reference; on 137 attempts with both numbers, an impact of 20-30% came with our
own measured round trip at p50 15%, and 30.1% came with a round trip that sold back for MORE. BYTE
(7823e47f) was refused at 39.12%; the same route replayed later cost 6.84% round trip at the clip,
4.53% of it fixed (a $1 probe), 2.3 points our size.

SHADOW ONLY: the refusal is unchanged. The executor hands the refused quote to a bounded background
worker and returns. The worker quotes on the CAPTURE key with its own HTTP client (never the funded
Jupiter seam, whose process-wide host cooldown a 429 would trip) and writes one row to its own table in
data/memecoin_refusal_captures.db.
"""
import json
import sqlite3
import threading
import time

import pytest

from sniper.execution import solana_swap_executor as X
from sniper.persistence import db_memecoin_captures as store
from sniper.services import memecoin_impact_shadow as S

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MINT = "CyovMnzMaKGApYsnm55d8rNWhEkskPSidKswnhgzMkGm"
JOB = {"attempt_id": "7823e47f", "origin_event_id": "onchain:SIG", "network_id": "solana", "mint": MINT,
       "clip_usd": 102.23, "buy_out_atomic": "1737179722160", "jupiter_impact_pct": 39.12,
       "impact_cap_pct": 30.0, "roundtrip_cap_pct": 10.0, "refused_at": "2026-09-28T06:52:32+00:00",
       "quote_observed_at": "2026-09-28T06:52:33+00:00",
       "hops": [{"label": "Raydium CP", "amm_key": "EaBZ", "in_mint": "Xs3o", "out_mint": MINT,
                 "in_amount": "68645092", "out_amount": "1737179722160"}]}


def _http(answers, seen=None):
    """answers: (input_mint, output_mint, amount) -> outAmount, or an exception to raise."""
    def http(url, headers, timeout):
        from urllib.parse import parse_qs, urlsplit
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        if seen is not None:
            seen.append((url, dict(headers)))
        out = answers[(q["inputMint"], q["outputMint"], int(q["amount"]))]
        if isinstance(out, Exception):
            raise out
        return 200, {"outAmount": str(out), "routePlan": [{"swapInfo": {"label": "Raydium CP",
                                                                          "ammKey": "EaBZ"}}]}
    return http


ANSWERS = {(MINT, USDC, 1737179722160): 95_241_458,          # our clip, sold back
           (USDC, MINT, 1_000_000): 34_871_516_951,          # the $1 probe buy
           (MINT, USDC, 34_871_516_951): 954_661}            # the probe sold back


def test_the_row_splits_our_round_trip_into_fixed_and_size():
    shadow = S.ImpactShadow(db_path=None, api_key="k", http=_http(ANSWERS), clock=lambda: 1790578400.0)
    row = shadow.measure(JOB)
    assert row["rt_cost_pct"] == pytest.approx((1 - 95.241458 / 102.23) * 100)
    assert row["probe_rt_cost_pct"] == pytest.approx((1 - 0.954661) * 100)
    assert row["size_cost_pct"] == pytest.approx(row["rt_cost_pct"] - row["probe_rt_cost_pct"])
    assert row["would_pass_roundtrip"] == 1 and row["quality"] == "ok"
    assert json.loads(row["hops_json"])[0]["amm_key"] == "EaBZ"


def test_the_capture_key_travels_and_the_funded_cooldown_is_untouched():
    from sniper.execution import solana_real_seams as seams
    before = dict(seams._jup_host_cooldown)
    seen = []
    answers = dict(ANSWERS)
    answers[(MINT, USDC, 1737179722160)] = OSError("HTTP 429")
    row = S.ImpactShadow(db_path=None, api_key="capture-key", http=_http(answers, seen)).measure(JOB)
    assert row["quality"] == "rt_unavailable" and row["rt_cost_pct"] is None
    assert all(h.get("x-api-key") == "capture-key" for _u, h in seen)
    assert seams._jup_host_cooldown == before


def test_an_unknown_round_trip_is_unknown_never_a_pass():
    answers = dict(ANSWERS)
    answers[(MINT, USDC, 1737179722160)] = OSError("down")
    row = S.ImpactShadow(db_path=None, api_key="k", http=_http(answers)).measure(JOB)
    assert row["would_pass_roundtrip"] is None


def test_submit_never_blocks_and_drops_when_full():
    gate = threading.Event()

    class Slow(S.ImpactShadow):
        def measure(self, job):
            gate.wait(2)
            return None
    shadow = Slow(db_path=None, api_key="k", http=_http(ANSWERS), queue_size=1)
    t = time.monotonic()
    results = [shadow.submit(dict(JOB)) for _ in range(5)]
    assert time.monotonic() - t < 0.05
    assert results[0] is True and False in results and shadow.dropped >= 1
    gate.set()


def test_a_failing_job_does_not_stop_the_worker(tmp_path):
    calls = []

    class Flaky(S.ImpactShadow):
        def measure(self, job):
            calls.append(job["attempt_id"])
            if job["attempt_id"] == "bad":
                raise RuntimeError("boom")
            return {"attempt_id": job["attempt_id"]}
    written = []
    shadow = Flaky(db_path=None, api_key="k", http=_http(ANSWERS), record_fn=written.append)
    shadow.submit({**JOB, "attempt_id": "bad"})
    shadow.submit({**JOB, "attempt_id": "good"})
    for _ in range(100):
        if written:
            break
        time.sleep(0.01)
    assert calls == ["bad", "good"] and written == [{"attempt_id": "good"}]


def test_the_row_lands_in_its_own_table_not_the_capture_series(tmp_path):
    path = tmp_path / "captures.db"
    shadow = S.ImpactShadow(db_path=str(path), api_key="k", http=_http(ANSWERS))
    shadow.record(shadow.measure(JOB))
    conn = store.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_refusal_captures").fetchone()[0] == 0
    row = dict(conn.execute("SELECT * FROM memecoin_impact_shadow").fetchone())
    assert row["attempt_id"] == "7823e47f" and row["would_pass_roundtrip"] == 1
    assert store.observation_counts(conn, before_iso="2099") == {}


@pytest.mark.parametrize("env,expected", [({}, False), ({"MEMECOIN_CAPTURE_JUPITER_API_KEY": "k"}, True),
                                          ({"MEMECOIN_CAPTURE_JUPITER_API_KEY": "k",
                                            "MEMECOIN_IMPACT_SHADOW": "0"}, False)])
def test_from_env_needs_the_capture_key_and_the_flag(env, expected):
    assert (S.from_env(env, db_path="x") is not None) is expected


# --- the executor -------------------------------------------------------------------------------

def _refuse(monkeypatch, *, impact="0.3912", shadow_fn=None, reverse=None, planned="1"):
    released = []
    # A size-blind fixture (every quote the same at any clip) would "resize" into a fill; the resize
    # has its own tests (test_memecoin_entry_roundtrip_resize.py).
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ENTRY_ROUNDTRIP_RESIZE", False)
    monkeypatch.setattr(X.m, "release_pending", lambda c, p, r: released.append(r))
    monkeypatch.setattr(X.m, "pending_row", lambda c, p: {"network_id": "solana"})
    monkeypatch.setattr(X, "completed_curve_in_route", lambda *a, **k: None)
    marks = []

    class Trace:
        def mark_substage(self, name, **kw):
            marks.append((name, kw))

        def mark(self, *a, **k):
            pass

        def mark_submission(self, *a, **k):
            return None
    quote = {"outAmount": "1737179722160", "otherAmountThreshold": planned, "priceImpactPct": impact,
             "routePlan": [{"swapInfo": {"label": "Raydium CP", "ammKey": "EaBZ", "inputMint": "Xs3o",
                                         "outputMint": MINT, "inAmount": "68645092",
                                         "outAmount": "1737179722160"}}]}
    res = X.execute_entry(
        None, pending_id=1, mint=MINT, usd_amount=102.23, strategy="s", source_user_id="u",
        now_iso="2026-09-28T06:52:32+00:00", quote_fn=lambda *a, **k: quote,
        reverse_quote_fn=reverse or (lambda *a, **k: pytest.fail("a refusal takes no funded reverse quote")),
        sign_fn=None, send_fn=None, confirm_fn=None, usdc_fn=lambda: 1000.0,
        token_fn=lambda m_: (0.0, 6), impact_cap_pct=30.0, trace=Trace(),
        pending={"network_id": "solana", "attempt_id": "7823e47f", "origin_event_id": "onchain:SIG"},
        impact_shadow_fn=shadow_fn)
    return res, released, dict(marks)


def test_the_refusal_is_unchanged_and_the_shadow_gets_the_refused_quote(monkeypatch):
    jobs = []
    res, released, marks = _refuse(monkeypatch, shadow_fn=jobs.append)
    assert res["status"] == "aborted" and res["reason"] == "price impact 39.12% over 30.0% cap"
    assert released == ["slippage_cap"]
    job, = jobs
    assert job["attempt_id"] == "7823e47f" and job["buy_out_atomic"] == "1737179722160"
    assert job["clip_usd"] == 102.23 and job["impact_cap_pct"] == 30.0 and job["roundtrip_cap_pct"] > 0
    assert marks["impact_cap_route"]["hops"][0]["in_amount"] == "68645092"


def test_a_failing_shadow_never_changes_the_refusal(monkeypatch):
    def boom(job):
        raise RuntimeError("shadow down")
    res, released, _ = _refuse(monkeypatch, shadow_fn=boom)
    assert res["reason"] == "price impact 39.12% over 30.0% cap" and released == ["slippage_cap"]


def test_no_shadow_below_the_cap(monkeypatch):
    jobs = []
    _refuse(monkeypatch, impact="0.05", shadow_fn=jobs.append, reverse=lambda *a, **k: None)
    assert jobs == []


# --- the round-trip gate's refusals too (2026-09-28, Alpha) -------------------------------------
# About 33 a week instead of 9, and both sides of the replacement question: the impact refusals the
# round trip would admit, and the round-trip refusals the impact cap passed.

def test_impact_refusals_are_labelled_with_their_gate(monkeypatch):
    jobs = []
    _refuse(monkeypatch, shadow_fn=jobs.append)
    assert jobs[0]["gate"] == "impact_cap" and jobs[0]["entry_rt_cost_pct"] is None


def test_a_round_trip_refusal_hands_its_own_measurement_to_the_shadow(monkeypatch):
    jobs = []
    # $102.23 in; the planned (minimum) sell returns $20, scaled by expected/planned 1.737: a 66% cost.
    res, released, _ = _refuse(monkeypatch, impact="0.05", shadow_fn=jobs.append,
                               reverse=lambda mint, atomic, *a, **k: {"inAmount": str(atomic), "outAmount": "20000000",
                                                                        "otherAmountThreshold": "1"},
                               planned="1000000000000")
    assert res["reason"].startswith("round trip ") and released == ["roundtrip_cap"]
    job, = jobs
    assert job["gate"] == "roundtrip_cap" and job["jupiter_impact_pct"] == pytest.approx(5.0)
    assert job["entry_rt_cost_pct"] == pytest.approx(res["roundtrip_cost_pct"])
    assert job["roundtrip_cap_pct"] > 0 and job["buy_out_atomic"] == "1737179722160"


def test_a_failing_shadow_never_changes_a_round_trip_refusal(monkeypatch):
    def boom(job):
        raise RuntimeError("shadow down")
    res, released, _ = _refuse(monkeypatch, impact="0.05", shadow_fn=boom,
                               reverse=lambda mint, atomic, *a, **k: {"inAmount": str(atomic), "outAmount": "20000000",
                                                                        "otherAmountThreshold": "1"},
                               planned="1000000000000")
    assert res["reason"].startswith("round trip ") and released == ["roundtrip_cap"]


def test_the_gate_columns_are_added_to_a_table_created_before_them(tmp_path):
    path = tmp_path / "captures.db"
    old = sqlite3.connect(path)
    # 2e5e6664's table: every column before `gate` and `entry_rt_cost_pct`.
    old.execute("CREATE TABLE memecoin_impact_shadow (id INTEGER PRIMARY KEY, %s)" % ", ".join(
        c + (" TEXT NOT NULL" if c in ("network_id", "mint", "observed_at") else "")
        for c in store.IMPACT_SHADOW_COLUMNS if c not in ("gate", "entry_rt_cost_pct")))
    old.commit()
    old.close()
    conn = store.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_impact_shadow)")}
    assert {"gate", "entry_rt_cost_pct"} <= cols


def test_the_row_carries_its_gate_and_the_entrys_own_round_trip(tmp_path):
    path = tmp_path / "captures.db"
    shadow = S.ImpactShadow(db_path=str(path), api_key="k", http=_http(ANSWERS))
    shadow.record(shadow.measure({**JOB, "gate": "roundtrip_cap", "entry_rt_cost_pct": 12.5}))
    row = dict(store.connect(path).execute("SELECT gate, entry_rt_cost_pct FROM memecoin_impact_shadow").fetchone())
    assert row == {"gate": "roundtrip_cap", "entry_rt_cost_pct": 12.5}
