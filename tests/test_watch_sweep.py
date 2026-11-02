"""The 15-minute sweep is the overnight guarantee, so its failure modes matter more than its checks.

A monitor that pages every quarter hour about the same thing is one nobody reads, which is the same
outcome as not paging. A monitor that claims a check it never runs is worse than one that claims
less.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import time

import pytest

SCRIPT = pathlib.Path("scripts/watch_sweep.py")


@pytest.fixture
def sweep(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("watch_sweep", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "HEARTBEAT", str(tmp_path / "hb.json"))
    monkeypatch.setattr(mod, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(mod, "check_rpc_cold", lambda problems: None)
    monkeypatch.setattr(mod, "check_gas", lambda problems: None)
    return mod


# ------------------------------------------------------------------ the dedup defect

def test_a_persistent_problem_pages_once_then_only_after_the_repage_window(sweep, monkeypatch):
    """REGRESSION 2026-08-26. Dedup keyed on the whole message, and every message embedded a
    drifting measurement ("heartbeat STALE 412s", then "...1312s"), so the key never matched: a
    persistent problem paged EVERY 15 MINUTES instead of every 2 hours, and the state file grew a
    dead entry per sweep. Verified by simulation before the fix — 4 sweeps, 4 pages, 4 keys."""
    sent = []
    monkeypatch.setattr(sweep, "page", lambda text: sent.append(text) or True)
    ages = [412.0, 1312.0, 2213.0, 3114.0]

    def run(age, now):
        monkeypatch.setattr(sweep, "check_sweep_gap", lambda p: None)
        monkeypatch.setattr(sweep, "check_runner", lambda p: p.append(
            ("runner_heartbeat_stale", "runner heartbeat STALE %.0fs" % age)))
        monkeypatch.setattr(sweep, "check_web", lambda p: None)
        monkeypatch.setattr(sweep, "check_db", lambda p: None)
        monkeypatch.setattr(sweep.time, "time", lambda: now)
        sweep.main()

    base = 1_000_000.0
    for i, age in enumerate(ages):
        run(age, base + i * 900)            # four sweeps, 15 minutes apart
    assert len(sent) == 1, f"one page for one ongoing condition, got {len(sent)}: {sent}"

    state = json.load(open(sweep.STATE))
    assert list(state["last_paged"]) == ["runner_heartbeat_stale"], \
        "the dedup key must not carry the measured value"

    run(4000.0, base + 4 * 900 + sweep.REPAGE_S)   # past the repage window
    assert len(sent) == 2, "an ongoing problem must repage after REPAGE_S"


def test_the_page_carries_the_measurement_even_though_the_key_does_not():
    """The human needs the number; the dedup must not see it."""
    body = SCRIPT.read_text()
    assert '"; ".join(d for _k, d in to_page)' in body
    assert "last_paged.get(k, 0)" in body


def test_a_resolved_problem_prunes_so_a_recurrence_pages_fresh(sweep, monkeypatch):
    sent = []
    monkeypatch.setattr(sweep, "page", lambda text: sent.append(text) or True)
    for fn in ("check_sweep_gap", "check_web", "check_db"):
        monkeypatch.setattr(sweep, fn, lambda p: None)
    monkeypatch.setattr(sweep, "check_runner",
                        lambda p: p.append(("runner_auth", "runner auth_ok FALSE")))
    sweep.main()
    monkeypatch.setattr(sweep, "check_runner", lambda p: None)      # resolved
    sweep.main()
    assert json.load(open(sweep.STATE))["last_paged"] == {}
    monkeypatch.setattr(sweep, "check_runner",
                        lambda p: p.append(("runner_auth", "runner auth_ok FALSE")))
    sweep.main()
    assert len(sent) == 2, "a recurrence after a resolution must page again"


def test_a_failed_page_is_retried_rather_than_marked_sent(sweep, monkeypatch):
    monkeypatch.setattr(sweep, "page", lambda text: False)          # telegram down
    for fn in ("check_sweep_gap", "check_web", "check_db"):
        monkeypatch.setattr(sweep, fn, lambda p: None)
    monkeypatch.setattr(sweep, "check_runner",
                        lambda p: p.append(("runner_auth", "runner auth_ok FALSE")))
    sweep.main()
    assert json.load(open(sweep.STATE))["last_paged"] == {}, \
        "an unsent page must not be recorded as sent"


# ------------------------------------------------------------------ the promised self-check

def test_the_sweep_notices_that_it_stopped_running(sweep, monkeypatch, tmp_path):
    """It cannot page about its own death — nothing in a dead process runs. It CAN notice on the
    run that comes back. The docstring promised this from the first version and main() never
    called it."""
    hb = pathlib.Path(sweep.HEARTBEAT)
    hb.write_text("{}")
    os.utime(hb, (time.time() - 7200, time.time() - 7200))
    problems = []
    sweep.check_sweep_gap(problems)
    assert problems and problems[0][0] == "sweep_gap"
    assert "cron gap, not a quiet system" in problems[0][1]


def test_a_normal_cadence_is_not_reported_as_a_gap(sweep):
    hb = pathlib.Path(sweep.HEARTBEAT)
    hb.write_text("{}")
    os.utime(hb, (time.time() - 900, time.time() - 900))
    problems = []
    sweep.check_sweep_gap(problems)
    assert problems == []


def test_the_first_ever_run_is_not_a_gap(sweep):
    problems = []
    sweep.check_sweep_gap(problems)     # no heartbeat file exists yet
    assert problems == []


def test_the_gap_is_checked_before_the_heartbeat_is_rewritten():
    """Rewriting first would erase the evidence the check reads."""
    body = SCRIPT.read_text()
    assert body.index("check_sweep_gap(problems)") < body.index("os.replace(tmp, HEARTBEAT)")


# ------------------------------------------------------------------ it must not become the risk

def test_the_sweep_never_writes_the_funded_database():
    body = SCRIPT.read_text()
    assert "mode=ro" in body, "the ledger handle must be read-only"
    for forbidden in ("INSERT", "UPDATE ", "DELETE", "systemctl restart", "kill "):
        assert forbidden not in body, f"an observer must not {forbidden.strip()}"


def test_the_heartbeat_is_written_atomically_and_is_readable_cold(sweep, monkeypatch):
    for fn in ("check_sweep_gap", "check_runner", "check_web", "check_db"):
        monkeypatch.setattr(sweep, fn, lambda p: None)
    sweep.main()
    hb = json.load(open(sweep.HEARTBEAT))
    assert {"ts", "problems", "keys", "paged"} <= set(hb)
    assert "os.replace(tmp, HEARTBEAT)" in SCRIPT.read_text()


def test_the_cron_line_uses_an_absolute_directory():
    """A relative path under cron's own cwd is the bare-launch class; it bit twice today."""
    body = SCRIPT.read_text()
    assert "os.chdir(ROOT)" in body, "the script must anchor itself regardless of the caller's cwd"


@pytest.fixture
def gas_sweep(monkeypatch):
    import io
    import urllib.request
    spec = importlib.util.spec_from_file_location("watch_sweep_gas", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from sniper.execution import solana_wallet
    from sniper.settings import memecoin as cfg
    from types import SimpleNamespace
    monkeypatch.setattr(solana_wallet, "load_keypair",
                        lambda path: SimpleNamespace(pubkey=lambda: "owner"))
    monkeypatch.setattr(cfg, "solana_funded_rpc_urls",
                        lambda: ("https://first.invalid", "https://fallback.invalid"))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_POOL_URLS", ("https://first.invalid",))
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_TOPUP_LOW_SOL", 0.02)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, **kw: io.BytesIO(
        json.dumps(solana_wallet._http("POST", req.full_url, body=req.data)[1]).encode()))
    return mod


@pytest.mark.parametrize("failure", ["http429", "credits", "timeout"])
def test_gas_check_rotates_safe_read_to_working_fallback(gas_sweep, monkeypatch, failure):
    import io
    import urllib.error
    from sniper.execution import solana_wallet
    calls = []

    def answer(url):
        calls.append(url)
        if url == "https://first.invalid":
            if failure == "http429":
                raise urllib.error.HTTPError(url, 429, "limited", {},
                                             io.BytesIO(b"max usage reached"))
            if failure == "timeout":
                raise TimeoutError()
            return {"error": {"code": -32429, "message": "exhausted"}}
        return {"result": {"value": 50_000_000}}

    monkeypatch.setattr(solana_wallet, "_http",
                        lambda method, url, **kw: (200, answer(url), None))
    problems = []
    gas_sweep.check_gas(problems)
    assert problems == []
    assert calls == ["https://first.invalid", "https://fallback.invalid"]


@pytest.mark.parametrize("payload", [
    {"error": {"code": -32000}}, {}, {"result": {"value": None}},
    {"result": {"value": -1}}, {"result": {"value": True}},
])
def test_failed_gas_read_is_unknown_never_zero(gas_sweep, monkeypatch, payload):
    from sniper.execution import solana_wallet
    monkeypatch.setattr(solana_wallet, "_http", lambda *a, **kw: (200, payload, None))
    problems = []
    gas_sweep.check_gas(problems)
    assert [key for key, _ in problems] == ["gas_check_failed"]
    assert "balance unknown" in problems[0][1]


def test_real_zero_gas_still_pages(gas_sweep, monkeypatch):
    from sniper.execution import solana_wallet
    monkeypatch.setattr(solana_wallet, "_http",
                        lambda *a, **kw: (200, {"result": {"value": 0}}, None))
    problems = []
    gas_sweep.check_gas(problems)
    assert [key for key, _ in problems] == ["funded_gas_low"]


def test_all_gas_endpoints_unavailable_still_pages(gas_sweep, monkeypatch):
    from sniper.execution import solana_wallet
    calls = []

    def unavailable(method, url, **kw):
        calls.append(url)
        raise TimeoutError()

    monkeypatch.setattr(solana_wallet, "_http", unavailable)
    problems = []
    gas_sweep.check_gas(problems)
    assert calls == ["https://first.invalid", "https://fallback.invalid"]
    assert [key for key, _ in problems] == ["gas_check_failed"]


def test_missing_gas_rpc_configuration_still_pages(gas_sweep, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "solana_funded_rpc_urls", lambda: ())
    problems = []
    gas_sweep.check_gas(problems)
    assert [key for key, _ in problems] == ["gas_check_failed"]


def test_gas_check_supports_legacy_endpoint_without_pool(gas_sweep, monkeypatch):
    from sniper.execution import solana_wallet
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "solana_funded_rpc_urls", lambda: ("https://fallback.invalid",))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_POOL_URLS", ())
    monkeypatch.setattr(solana_wallet, "_http",
                        lambda *a, **kw: (200, {"result": {"value": 50_000_000}}, None))
    problems = []
    gas_sweep.check_gas(problems)
    assert problems == []


@pytest.mark.parametrize('failure', ['http429', 'http401', 'http403', 'http503',
                                   'credits', 'timeout', 'rpc_error', 'malformed'])
def test_cold_pool_probe_uses_working_alternative(gas_sweep, monkeypatch, failure):
    import io
    import urllib.error
    from sniper.execution import solana_wallet
    calls = []

    def cold(method, url, **kw):
        assert json.loads(kw.get('body') or '{}').get('method') == 'getHealth'
        calls.append(url)
        if url == 'https://first.invalid':
            if failure.startswith('http'):
                raise urllib.error.HTTPError(url, int(failure[4:]), 'unavailable', {},
                                             io.BytesIO(b'max usage reached'))
            if failure == 'timeout':
                raise TimeoutError()
            payload = {'credits': {'error': {'code': -32429}},
                       'rpc_error': {'error': {'code': -32005}},
                       'malformed': {}}[failure]
            return 200, payload, None
        return 200, {'result': 'ok'}, None

    monkeypatch.setattr(solana_wallet, '_http', cold)
    problems = []
    gas_sweep.check_rpc_cold(problems)
    assert problems == []
    assert calls == ['https://first.invalid', 'https://fallback.invalid']


@pytest.mark.parametrize('payload', [{}, {'error': {'code': -32005}}, {'result': 'behind'},
                                     {'result': 'ok', 'error': {'code': -32005}}])
def test_cold_pool_probe_rejects_http_success_with_bad_health(gas_sweep, monkeypatch, payload):
    from sniper.execution import solana_wallet
    calls = []

    def cold(method, url, **kw):
        calls.append(url)
        return 200, payload, None

    monkeypatch.setattr(solana_wallet, '_http', cold)
    problems = []
    gas_sweep.check_rpc_cold(problems)
    assert calls == ['https://first.invalid', 'https://fallback.invalid']
    assert [k for k, _ in problems] == ['rpc_cold_unreachable']


def test_cold_pool_probe_all_timeouts_still_alerts_without_credentials(gas_sweep, monkeypatch):
    from sniper.execution import solana_wallet
    from sniper.settings import memecoin as cfg
    urls = ('https://first.invalid/?api-key=secret-one',
            'https://fallback.invalid/?api-key=secret-two')
    monkeypatch.setattr(cfg, 'solana_funded_rpc_urls', lambda: urls)
    calls = []

    def cold(method, url, **kw):
        calls.append(url)
        raise TimeoutError(url)

    monkeypatch.setattr(solana_wallet, '_http', cold)
    problems = []
    gas_sweep.check_rpc_cold(problems)
    assert calls == list(urls)
    assert [k for k, _ in problems] == ['rpc_cold_unreachable']
    assert 'secret-' not in problems[0][1]


def test_cold_pool_probe_stops_after_first_healthy_endpoint(gas_sweep, monkeypatch):
    from sniper.execution import solana_wallet
    calls = []

    def cold(method, url, **kw):
        calls.append(url)
        return 200, {'result': 'ok'}, None

    monkeypatch.setattr(solana_wallet, '_http', cold)
    problems = []
    gas_sweep.check_rpc_cold(problems)
    assert problems == []
    assert calls == ['https://first.invalid']


# --- exotic CU blind hop labels (2026-10-29): the runner counts, the sweep pages, off the fill path --

def _blind_run(sweep, monkeypatch, tmp_path, counts, sent):
    hb = tmp_path / "runner.hb"
    hb.write_text(json.dumps({"slot_lane": {"exotic_cu_blind": counts}}))
    monkeypatch.setattr(sweep, "RUNNER_HEARTBEAT", str(hb))
    for name in ("check_sweep_gap", "check_runner", "check_web", "check_db"):
        monkeypatch.setattr(sweep, name, lambda p: None)
    monkeypatch.setattr(sweep, "page", lambda text: sent.append(text) or True)
    sweep.main()


def test_a_new_blind_label_pages_once_after_n_builds_and_never_again(sweep, monkeypatch, tmp_path):
    sent = []
    n = sweep.BLIND_MIN_BUILDS
    _blind_run(sweep, monkeypatch, tmp_path, {"NewAmm": n - 1}, sent)
    assert sent == [], "below N builds is not yet a page"
    _blind_run(sweep, monkeypatch, tmp_path, {"NewAmm": n}, sent)
    assert len(sent) == 1 and "NewAmm" in sent[0]
    for _ in range(3):                                       # still blind, ever more builds
        _blind_run(sweep, monkeypatch, tmp_path, {"NewAmm": n + 50}, sent)
    _blind_run(sweep, monkeypatch, tmp_path, {}, sent)        # runner restart resets its counter
    _blind_run(sweep, monkeypatch, tmp_path, {"NewAmm": n}, sent)
    assert len(sent) == 1, "a label pages once, ever"
    _blind_run(sweep, monkeypatch, tmp_path, {"NewAmm": n, "OtherAmm": n}, sent)
    assert len(sent) == 2 and "OtherAmm" in sent[1] and "NewAmm" not in sent[1]


def test_a_failed_blind_page_is_retried(sweep, monkeypatch, tmp_path):
    sent = []
    _blind_run(sweep, monkeypatch, tmp_path, {"NewAmm": 9}, sent)
    monkeypatch.setattr(sweep, "page", lambda text: False)
    state = json.load(open(sweep.STATE))
    state["blind_labels_paged"] = []
    json.dump(state, open(sweep.STATE, "w"))
    sweep.main()
    assert json.load(open(sweep.STATE))["blind_labels_paged"] == []


def test_an_unreadable_or_odd_blind_counter_never_breaks_the_sweep(sweep, monkeypatch, tmp_path):
    sent = []
    for counts in (None, [], {"NewAmm": "x"}, {"NewAmm": True}):
        _blind_run(sweep, monkeypatch, tmp_path, counts, sent)
    assert sent == []
