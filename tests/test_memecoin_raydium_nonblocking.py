"""The entry never waits for a late Raydium second opinion (operator yes via Alpha, 2026-10-02).

With MEMECOIN_ENTRY_RAYDIUM_PARALLEL the thin-route Raydium quote runs beside Jupiter's. With
MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING it counts only if it is already done when Jupiter lands;
otherwise Jupiter decides and the late answer is recorded as a shadow, never touching the entry."""
import json
import time

import pytest

from sniper.execution import solana_real_seams as S
from sniper.settings import memecoin as cfg

THIN = {"outAmount": "100", "otherAmountThreshold": "90", "priceImpactPct": "0.20",
        "routePlan": [{"swapInfo": {"label": "Meteora DLMM"}}]}


@pytest.fixture(autouse=True)
def _knobs(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_VENUE_FALLBACK", True, raising=False)
    shadow = S.LateRaydiumShadow(path=str(tmp_path / "late.jsonl"))
    monkeypatch.setattr(S, "LATE_RAYDIUM_SHADOW", shadow)
    S._jup_host_cooldown.clear()
    yield shadow
    S._jup_host_cooldown.clear()


def _providers(monkeypatch, *, jup=THIN, jup_s=0.0, ray=None, ray_s=0.0, status=200):
    calls = []

    def jupiter(*a, **k):
        time.sleep(jup_s)
        return (dict(jup) if jup is not None else None), status

    def raydium(*a, **k):
        calls.append(time.monotonic())
        time.sleep(ray_s)
        return dict(ray) if ray is not None else None
    monkeypatch.setattr(S, "_jup_quote_status", jupiter)
    monkeypatch.setattr(S, "_ray_quote", raydium)
    return calls


def _quote():
    started = time.monotonic()
    q = S._quote_with_fallback(S.USDC_MINT, "MINT", 1_000_000, 300, None, tag_evidence=True)
    return q, time.monotonic() - started


def _rows(shadow, n=1, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while shadow.stats["written"] < n and time.monotonic() < deadline:
        time.sleep(0.01)
    with open(shadow.path) as f:
        return [json.loads(line) for line in f]


def test_a_slow_raydium_no_longer_delays_the_entry(monkeypatch, _knobs):
    _providers(monkeypatch, ray={"outAmount": "110", "_venue": "raydium"}, ray_s=0.4)
    q, took = _quote()
    assert took < 0.2, took
    assert q["outAmount"] == "100" and q.get("_venue") is None, "Jupiter decided"
    tag = q["_sniper_fallback"]
    assert (tag["branch"], tag["raydium"], tag["raydium_wait_ms"]) == ("thin", "late", 0.0)
    assert tag["late_shadow_id"]
    _rows(_knobs)  # Finish this test's callback before its patched shadow/provider are restored.


def test_a_late_raydium_win_is_recorded_as_a_shadow_only(monkeypatch, _knobs):
    _providers(monkeypatch, ray={"outAmount": "110", "_venue": "raydium"}, ray_s=0.15)
    q, _took = _quote()
    snapshot = json.dumps(q, sort_keys=True)
    (row,) = _rows(_knobs)
    assert row["would"] == "won" and row["shadow_id"] == q["_sniper_fallback"]["late_shadow_id"]
    assert (row["jupiter_out_atomic"], row["raydium_out_atomic"]) == ("100", "110")
    assert row["jupiter_route_labels"] == ["Meteora DLMM"] and row["late_ms"] > 0
    assert json.dumps(q, sort_keys=True) == snapshot, "the entry's quote is never touched after"


def test_a_raydium_already_done_when_jupiter_lands_still_wins_as_today(monkeypatch, _knobs):
    _providers(monkeypatch, jup_s=0.2, ray={"outAmount": "110", "_venue": "raydium"}, ray_s=0.0)
    q, _took = _quote()
    assert q["_venue"] == "raydium" and q["_sniper_fallback"]["raydium"] == "won"
    assert _knobs.stats["offered"] == 0


def test_knob_off_is_todays_blocking_comparison(monkeypatch, _knobs):
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False, raising=False)
    _providers(monkeypatch, ray={"outAmount": "110", "_venue": "raydium"}, ray_s=0.3)
    q, took = _quote()
    assert took >= 0.25 and q["_venue"] == "raydium" and q["_sniper_fallback"]["raydium"] == "won"


def test_both_workers_busy_goes_on_jupiter_without_todays_serial_call(monkeypatch, _knobs):
    calls = _providers(monkeypatch, ray={"outAmount": "110"}, ray_s=0.3)
    monkeypatch.setattr(S, "_ray_parallel_start", lambda *a, **k: None)
    q, took = _quote()
    assert calls == [] and took < 0.2
    assert q["_sniper_fallback"]["raydium"] == "not_started" and "late_shadow_id" not in q["_sniper_fallback"]


def test_a_jupiter_miss_still_waits_for_raydium_the_only_answer(monkeypatch, _knobs):
    _providers(monkeypatch, jup=None, status=400, ray={"outAmount": "500", "_venue": "raydium"},
               ray_s=0.2)
    q, took = _quote()
    assert took >= 0.15 and q["_venue"] == "raydium"
    assert (q["_sniper_fallback"]["branch"], q["_sniper_fallback"]["raydium"]) == ("miss", "won")


@pytest.mark.parametrize("ray", [None, {"outAmount": "150", "_venue": "raydium"}])
def test_entry_reverse_proof_returns_before_optional_raydium_finishes(monkeypatch, _knobs, ray):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    started, release = Event(), Event()
    monkeypatch.setattr(S, "_jup_quote_status", lambda *a, **k: (dict(THIN), 200))

    def raydium(*a, **k):
        started.set()
        assert release.wait(3), "test must release its provider"
        return ray

    monkeypatch.setattr(S, "_ray_quote", raydium)
    recorder = S.EntryQuoteMissRecorder(None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(recorder.reverse, "MINT", 270602262558, 30)
        try:
            assert started.wait(1)
            q = future.result(timeout=0.2)
            assert q["outAmount"] == "100"
            assert q["_sniper_fallback"]["raydium_wait_ms"] == 0.0
        finally:
            release.set()
            future.result(timeout=1)
    before = json.dumps(q, sort_keys=True)
    (row,) = _rows(_knobs)
    assert row["amount_atomic"] == "270602262558"
    assert row["would"] == ("no_quote" if ray is None else "won")
    assert json.dumps(q, sort_keys=True) == before


def test_busy_raydium_workers_cannot_block_entry_reverse_proof(monkeypatch, _knobs):
    calls = _providers(monkeypatch, ray={"outAmount": "150"})
    monkeypatch.setattr(S, "_ray_parallel_start", lambda *a, **k: None)
    q = S.EntryQuoteMissRecorder(None).reverse("MINT", 270602262558, 30)
    assert q["outAmount"] == "100" and calls == []
    assert q["_sniper_fallback"]["raydium"] == "not_started"


@pytest.mark.parametrize("evidence", [False, True])
def test_thin_held_position_exit_keeps_its_better_raydium_quote(monkeypatch, _knobs, evidence):
    _providers(monkeypatch, ray={"outAmount": "150", "_venue": "raydium"})
    q = S.jupiter_exit_quote("MINT", 270602262558, 30, evidence=evidence)
    assert q["outAmount"] == "150" and q["_venue"] == "raydium"
    assert _knobs.stats["offered"] == 0


@pytest.mark.parametrize("status", [400, 429, 503])
def test_entry_reverse_without_jupiter_keeps_typed_fallback(monkeypatch, _knobs, status):
    _providers(monkeypatch, jup=None, status=status,
               ray={"outAmount": "150", "_venue": "raydium"})
    recorder = S.EntryQuoteMissRecorder(None)
    q = recorder.reverse("MINT", 270602262558, 30)
    if status == 503:
        assert q is None
        assert recorder.reverse_miss()["code"] == "provider_rpc_transport"
    else:
        assert q["outAmount"] == "150" and q["_venue"] == "raydium"
        assert recorder.reverse_miss() is None
    assert _knobs.stats["offered"] == 0


def test_entry_reverse_nonblocking_rollback_keeps_second_opinion(monkeypatch, _knobs):
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False)
    _providers(monkeypatch, ray={"outAmount": "150", "_venue": "raydium"})
    q = S.EntryQuoteMissRecorder(None).reverse("MINT", 270602262558, 30)
    assert q["outAmount"] == "150" and q["_venue"] == "raydium"


def test_low_impact_entry_reverse_starts_no_optional_provider(monkeypatch, _knobs):
    calls = _providers(monkeypatch, jup=dict(THIN, priceImpactPct="0.01"))
    q = S.EntryQuoteMissRecorder(None).reverse("MINT", 270602262558, 30)
    assert q["outAmount"] == "100" and calls == []


@pytest.mark.parametrize("row,expected", [
    ({"raydium_out_atomic": None, "jupiter_out_atomic": "100"}, "no_quote"),
    ({"raydium_out_atomic": "90", "jupiter_out_atomic": "100"}, "lost"),
    ({"error": "TimeoutError"}, "error"),
    ({"raydium_out_atomic": "500", "jupiter_out_atomic": "100", "jupiter_impact_pct": 0.2}, "distrusted"),
])
def test_the_shadow_names_what_todays_rule_would_have_done(row, expected):
    assert S.LateRaydiumShadow.would(dict(row, input_mint=S.USDC_MINT)) == expected


def test_a_shadow_that_cannot_write_never_raises(tmp_path):
    from concurrent.futures import Future
    (tmp_path / "blocker").write_text("a file where the directory should be")
    shadow = S.LateRaydiumShadow(path=str(tmp_path / "blocker" / "late.jsonl"))
    fut = Future()
    fut.set_result(({"outAmount": "1"}, 5.0))
    shadow.offer({"shadow_id": "x", "input_mint": S.USDC_MINT, "jupiter_out_atomic": "2"}, fut)
    deadline = time.monotonic() + 3
    while shadow.stats["errors"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert shadow.stats["errors"] == 1 and shadow.stats["written"] == 0


def test_the_late_shadow_counters_reach_the_heartbeat(tmp_path):
    # Gamma LOW on 87ac7c0ba: a broken writer showed only as per-row log warnings.
    from sniper.services import memecoin_runner as run
    path = str(tmp_path / "hb.json")
    run.write_heartbeat({"events": 1, "opened": 0}, auth_ok=True, path=path,
                        raydium_late_shadow={"offered": 4, "written": 2, "dropped": 1, "errors": 1,
                                             "private": "never published"})
    got = run.read_heartbeat(path)["raydium_late_shadow"]
    assert got == {"offered": 4, "written": 2, "dropped": 1, "errors": 1}


def test_the_runner_passes_the_live_counters():
    import pathlib
    body = pathlib.Path("scripts/memecoin_runner_loop.py").read_text()
    assert "raydium_late_shadow=_raydium_late_shadow_stats()" in body
    block = body[body.index("    def _raydium_late_shadow_stats("):]
    block = block[:block.index("\n    def ", 10)]
    assert "LATE_RAYDIUM_SHADOW.stats" in block
