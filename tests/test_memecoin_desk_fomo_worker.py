"""Offline browser boundary checks; only the intake databases are real."""
from importlib import import_module
import json
import os
from pathlib import Path
import select
import shlex
import signal
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest


SOL = "62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump"
AT = 1793059201.0
LABELS = ["En tendencia", "Blue chips", "Con más holders", "Graduados", "Bonding"]


def worker():
    return import_module("scripts.memecoin_desk_fomo_intake")


class Control:
    def __init__(self, page, label, period=False):
        self.page, self.label, self.period = page, label, period

    def count(self):
        return int(self.label in (["24h"] if self.period else self.page.labels))

    def wait_for(self, **kwargs):
        if not self.count():
            raise TimeoutError

    def get_attribute(self, name):
        selected = self.page.period if self.period else self.page.selected
        return "true" if selected == self.label else "false"

    def click(self, **kwargs):
        self.page.clicks.append(self.label)
        if self.period:
            self.page.period = self.label
        elif not self.page.stuck:
            self.page.selected = self.label


class Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    def filter(self, *, has_text):
        matches = [name for name in self.page.labels if has_text.search(name)]
        return Control(self.page, matches[0] if matches else "unknown")

    def count(self):
        if self.page.on_count:
            self.page.on_count()
        return int(self.page.rows != [])

    def evaluate_all(self, expression):
        assert self.selector == 'div[id^="thesis-trigger-default-thesis-"]'
        self.page.captures.append((self.page.selected, self.page.period))
        self.page.capture_times.append(self.page.elapsed)
        if self.page.on_capture:
            self.page.on_capture()
        if self.page.row_error:
            raise self.page.row_error
        return self.page.rows


class Page:
    def __init__(self):
        self.url = "https://fomo.family/discover"
        self.labels = LABELS[:]
        self.selected, self.period = "En tendencia", "1h"
        self.rows = [{"row_id": f"thesis-trigger-default-thesis-{SOL}:1399811149",
                      "href": f"/tokens/solana/{SOL}", "text": "CAPTCHA coin\n$0", "cells": ["CAPTCHA coin", "$0"]}]
        self.authenticated, self.login_control, self.notice = True, False, "Fomo"
        self.clicks, self.captures, self.waits, self.callbacks = [], [], [], {}
        self.elapsed, self.capture_times = 0.0, []
        self.on_capture, self.row_error, self.on_wait, self.on_goto, self.on_count = None, None, None, None, None
        self.navigation_status, self.stuck = None, False

    def on(self, name, callback):
        self.callbacks[name] = callback

    def emit(self, status, url="https://fomo.family/discovery-list"):
        self.callbacks["response"](SimpleNamespace(status=status, url=url))

    def goto(self, url, **kwargs):
        assert url == "https://fomo.family/discover"
        assert 0 < kwargs["timeout"] <= 15000
        if self.navigation_status:
            self.emit(self.navigation_status)
        if self.on_goto:
            self.on_goto()

    def evaluate(self, expression):
        assert "document.body" not in expression
        authenticated = True if self.authenticated else False if self.login_control else None
        return {"authenticated": authenticated, "text": self.notice}

    def locator(self, selector):
        assert selector in ('button[type="button"][aria-pressed]', 'div[id^="thesis-trigger-default-thesis-"]',
                            'div[id^="thesis-trigger-default-thesis-"]:visible')
        return Locator(self, selector)

    def get_by_role(self, role, *, name, exact):
        assert (role, name, exact) == ("tab", "24h", True)
        return Control(self, name, period=True)

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        self.elapsed += milliseconds / 1000
        if self.on_wait:
            self.on_wait(milliseconds)


def run(tmp_path, page, polls=5, clock=lambda: AT):
    return worker().collect(page, out_dir=tmp_path / "intake", clock_file=tmp_path / "clock.json",
                            halt_file=tmp_path / "halt.json", polls=polls, clock=clock, monotonic=lambda: page.elapsed)


def daily(tmp_path, day="2026-10-27"):
    return sqlite3.connect(tmp_path / "intake" / "fomo" / (day + ".db"))


def test_profile_is_empty_private_outside_repo_and_never_adopts_operator_profile(tmp_path):
    w = worker()
    profile = tmp_path / "fomo_intake_profile"
    assert w.init_profile(profile) == profile.resolve()
    assert profile.stat().st_mode & 0o777 == 0o700
    assert not any(p.name in {"Cookies", "Local State"} for p in profile.iterdir())
    with w.profile_lock(profile):
        with pytest.raises((BlockingIOError, RuntimeError)):
            with w.profile_lock(profile):
                pytest.fail("second owner acquired profile")
    operator = tmp_path / "chrome"
    operator.mkdir(mode=0o755)
    (operator / "Local State").write_text("operator data")
    before = operator.stat().st_mode
    with pytest.raises(ValueError):
        w.init_profile(operator)
    assert operator.stat().st_mode == before
    for unsafe in (w.ROOT, w.ROOT / "profile", w.ROOT.parent):
        with pytest.raises(ValueError):
            w.init_profile(unsafe)


def test_five_lists_rotate_at_bounded_cadence_and_store_partial_raw_evidence(tmp_path):
    page = Page()
    result = run(tmp_path, page, polls=6)
    with daily(tmp_path) as db:
        assert db.execute("SELECT feed, filter, status, items FROM polls ORDER BY id").fetchall() == [
            ("fomo", name, "ok", 1) for name in
            ("trending24h", "blue_chips", "most_holders", "graduated", "bonding", "trending24h")]
        raw = json.loads(db.execute("SELECT raw_json FROM intake_rows LIMIT 1").fetchone()[0])
        assert raw == {"transport": "dom", "coverage": "partial", "item": page.rows[0]}
        assert db.execute("SELECT DISTINCT observed_at FROM intake_rows").fetchall() == [(None,)]
    assert page.captures == [(label, "24h") for label in LABELS + LABELS[:1]]
    assert page.capture_times == pytest.approx([0.8, 15.8, 30.8, 45.8, 60.8, 75.8], abs=0.01)
    assert result["polls"] == 6 and result["errors"] == 0


def test_receipt_and_offset_freeze_before_extraction_and_corrected_day_is_used(tmp_path):
    page = Page()
    clock_file = tmp_path / "clock.json"
    clock_file.write_text('{"offset_s": 3}')
    now = [AT]
    def during_capture():
        now[0] += 10
        clock_file.write_text('{"offset_s": 999}')
    page.on_capture = during_capture
    run(tmp_path, page, polls=1, clock=lambda: now[0])
    assert (tmp_path / "intake/fomo/2026-10-26.db").exists()
    with daily(tmp_path, "2026-10-26") as db:
        assert db.execute("SELECT observed_at, host_received_at, clock_offset_s FROM intake_rows").fetchone() == (AT - 3, AT, 3)


def test_receipt_is_taken_after_navigation_and_selection_instead_of_poll_start(tmp_path):
    page = Page()
    now = [AT]
    (tmp_path / "clock.json").write_text('{"offset_s": 0}')
    def during_navigation():
        now[0] += 30
        (tmp_path / "clock.json").write_text('{"offset_s": 7}')
    page.on_goto = during_navigation
    run(tmp_path, page, polls=1, clock=lambda: now[0])
    with daily(tmp_path) as db:
        assert db.execute("SELECT observed_at, host_received_at, clock_offset_s FROM intake_rows").fetchone() == (AT + 23, AT + 30, 7)


def test_first_sighting_survives_daily_rotation_and_filter_changes(tmp_path):
    (tmp_path / "clock.json").write_text('{"offset_s": 0}')
    run(tmp_path, Page(), polls=1)
    run(tmp_path, Page(), polls=2, clock=lambda: AT + 86400)
    with sqlite3.connect(tmp_path / "intake/fomo/first_sightings.db") as db:
        assert db.execute("SELECT feed, network_id, mint, observed_at FROM first_sightings").fetchall() == [("fomo", "solana", SOL, AT)]


@pytest.mark.parametrize("kind,reason", [("status", "http_429"), ("login", "login_required"), ("challenge", "challenge")])
def test_refusal_writes_one_error_and_durable_halt_before_any_click(tmp_path, kind, reason):
    page = Page()
    if kind == "status":
        page.navigation_status = 429
    elif kind == "login":
        page.authenticated, page.login_control = False, True
    else:
        page.notice = "Verify you are human"
    result = run(tmp_path, page)
    with daily(tmp_path) as db:
        assert db.execute("SELECT status, error FROM polls").fetchall() == [("error", reason)]
        assert db.execute("SELECT COUNT(*) FROM intake_rows").fetchone()[0] == 0
    assert json.loads((tmp_path / "halt.json").read_text())["reason"] == reason
    assert result["halted"] == reason and page.clicks == [] and page.captures == []


def test_pending_same_host_http_refusal_overrides_healthy_dom_without_reading_body(tmp_path):
    page = Page()
    page.on_capture = lambda: page.emit(403)
    run(tmp_path, page)
    with daily(tmp_path) as db:
        assert db.execute("SELECT status, error FROM polls").fetchall() == [("error", "http_403")]
    assert len(page.captures) == 1


def test_fomo_api_subdomain_refusal_uses_its_own_receipt_and_halts_cached_dom(tmp_path):
    page = Page()
    now = [AT]
    (tmp_path / "clock.json").write_text('{"offset_s": 0}')
    def refused_response():
        now[0] += 20
        (tmp_path / "clock.json").write_text('{"offset_s": 4}')
        page.emit(401, "https://prod-api.fomo.family/list")
    page.on_capture = refused_response
    run(tmp_path, page, clock=lambda: now[0])
    with daily(tmp_path) as db:
        assert db.execute("SELECT received_at, status, error FROM polls").fetchall() == [(AT + 20, "error", "login_expired")]
        assert db.execute("SELECT COUNT(*) FROM intake_rows").fetchone()[0] == 0
    assert json.loads((tmp_path / "halt.json").read_text())["host_received_at"] == AT + 20


def test_refusal_during_paced_wait_stops_before_next_click_or_capture(tmp_path):
    page = Page()
    page.on_wait = lambda milliseconds: page.emit(401) if milliseconds >= 10000 else None
    run(tmp_path, page)
    assert page.captures == [("En tendencia", "24h")]
    assert "Blue chips" not in page.clicks
    with daily(tmp_path) as db:
        assert db.execute("SELECT status, error FROM polls ORDER BY id").fetchall() == [("ok", None), ("error", "login_expired")]


@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_refusal_is_durable_even_when_paced_wait_is_interrupted(tmp_path, failure):
    page, saved_in_callback = Page(), []
    def interrupted_wait(milliseconds):
        if milliseconds >= 10000:
            page.emit(429)
            saved_in_callback.append((tmp_path / "halt.json").exists())
            page.emit(403)
            raise failure()
    page.on_wait = interrupted_wait
    try:
        result = run(tmp_path, page)
    except (KeyboardInterrupt, RuntimeError):
        result = None
    assert saved_in_callback == [True]
    assert result is not None and result["halted"] == "http_429"
    assert page.captures == [("En tendencia", "24h")] and "Blue chips" not in page.clicks
    with daily(tmp_path) as db:
        assert db.execute("SELECT filter, status, error FROM polls ORDER BY id").fetchall() == [
            ("trending24h", "ok", None), ("trending24h", "error", "http_429")]


def test_slow_positive_header_outlasts_transient_login_placeholder(tmp_path):
    page = Page()
    page.authenticated, page.login_control = False, True
    def hydrate(milliseconds):
        if page.elapsed >= 2:
            page.authenticated, page.login_control = True, False
    page.on_wait = hydrate
    result = run(tmp_path, page, polls=1)
    assert result["errors"] == 0 and len(page.captures) == 1
    assert page.capture_times[0] >= 2
    assert not (tmp_path / "halt.json").exists()


@pytest.mark.parametrize("explicit_login,reason", [(False, "auth_unproven"), (True, "login_required")])
def test_initial_auth_wait_reaches_deadline_before_naming_missing_proof(tmp_path, explicit_login, reason):
    page = Page()
    page.authenticated, page.login_control = False, explicit_login
    result = run(tmp_path, page, polls=1)
    assert page.elapsed == 10
    assert page.clicks == [] and page.captures == [] and result["halted"] == reason
    with daily(tmp_path) as db:
        assert db.execute("SELECT status, error FROM polls").fetchall() == [("error", reason)]


def test_http_refusal_during_initial_readiness_is_immediate_and_durable(tmp_path):
    page = Page()
    page.authenticated = False
    def refused(milliseconds):
        if page.elapsed >= 0.5:
            page.emit(403, "https://prod-api.fomo.family/list")
            assert (tmp_path / "halt.json").exists()
    page.on_wait = refused
    result = run(tmp_path, page, polls=1)
    assert result["halted"] == "http_403" and page.elapsed == 0.5
    assert page.clicks == [] and page.captures == []
    with daily(tmp_path) as db:
        assert db.execute("SELECT status, error FROM polls").fetchall() == [("error", "http_403")]


@pytest.mark.parametrize("kind,reason", [("challenge", "challenge"), ("origin", "unexpected_origin")])
def test_initial_readiness_never_defers_challenge_or_wrong_origin(tmp_path, kind, reason):
    page = Page()
    page.authenticated = False
    if kind == "challenge":
        page.notice = "Verify you are human"
    else:
        page.url = "https://other.example/login"
    result = run(tmp_path, page, polls=1)
    assert result["halted"] == reason and page.elapsed == 0
    assert page.clicks == [] and page.captures == []


@pytest.mark.parametrize("url", ["https://other.example/account", "https://fomo.family.evil.example/list"])
def test_foreign_response_and_token_named_captcha_do_not_halt(tmp_path, url):
    page = Page()
    page.on_capture = lambda: page.emit(429, url)
    assert run(tmp_path, page, polls=1)["errors"] == 0
    assert not (tmp_path / "halt.json").exists()


@pytest.mark.parametrize("impostor", ["Buy En tendencia", "En tendencia BUY"])
def test_partial_list_label_never_becomes_a_click_target(tmp_path, impostor):
    page = Page()
    page.labels[0], page.selected = impostor, "Bonding"
    run(tmp_path, page, polls=1)
    assert page.clicks == [] and page.captures == []
    with daily(tmp_path) as db:
        assert db.execute("SELECT status, error FROM polls").fetchall() == [("error", "selector_changed")]


def test_cadence_counts_poll_work_and_never_catches_up_in_a_burst(tmp_path):
    times = iter([0, 2, 15, 40, 41])
    page = Page()
    worker().collect(page, out_dir=tmp_path / "intake", clock_file=tmp_path / "clock.json",
                     halt_file=tmp_path / "halt.json", polls=3, clock=lambda: AT,
                     monotonic=lambda: next(times))
    assert 13000 in page.waits and 1000 in page.waits


@pytest.mark.parametrize("kind,reason", [("empty", "empty_dom"), ("shape", "malformed_dom"), ("timeout", "timeout"), ("selection", "selector_changed")])
def test_ordinary_failure_is_named_and_not_retried_immediately(tmp_path, kind, reason):
    page = Page()
    if kind == "empty":
        page.rows = []
    elif kind == "shape":
        page.rows = {}
    elif kind == "timeout":
        page.row_error = TimeoutError()
    else:
        page.period, page.selected, page.stuck = "24h", "Bonding", True
    run(tmp_path, page, polls=1)
    with daily(tmp_path) as db:
        assert db.execute("SELECT status, error FROM polls").fetchall() == [("error", reason)]
    assert not (tmp_path / "halt.json").exists()
    assert len(page.captures) <= 1


@pytest.mark.parametrize("headed", [False, True])
def test_saved_halt_blocks_browser_launch_and_main_closes_context(tmp_path, monkeypatch, capsys, headed):
    w = worker()
    profile = w.init_profile(tmp_path / "fomo_intake_profile")
    page, calls = Page(), []
    ctx = SimpleNamespace(pages=[page], close=lambda: calls.append("close"))
    def launch(path, **kwargs):
        calls.append("launch")
        assert Path(path) == profile and kwargs["locale"] == "es-ES"
        assert kwargs["channel"] == "chrome"
        assert kwargs["headless"] is not headed
        assert kwargs.get("args") == ["--disable-gpu"] + (["--ozone-platform=x11"] if headed else [])
        return ctx
    pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    class Manager:
        def __enter__(self):
            return pw
        def __exit__(self, *args):
            return False
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=Manager))
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    argv = ["--profile-dir", str(profile), "--polls", "1"] + (["--headed"] if headed else [])
    (profile / "halt.json").write_text('{"reason":"http_429"}')
    assert w.main(argv) != 0
    assert calls == []
    (profile / "halt.json").unlink()
    monkeypatch.setattr(w, "collect", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert w.main(argv) == 0
    assert calls == ["launch", "close"]
    monkeypatch.setattr(w, "collect", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private-auth-token")))
    assert w.main(argv) == 2
    output = capsys.readouterr().out
    assert "RuntimeError" in output and "private-auth-token" not in output
    assert calls == ["launch", "close", "launch", "close"]
    def interrupted_after_halt(*args, **kwargs):
        (profile / "halt.json").write_text('{"reason":"http_429"}')
        raise KeyboardInterrupt()
    monkeypatch.setattr(w, "collect", interrupted_after_halt)
    assert w.main(argv) == 2
    assert calls == ["launch", "close", "launch", "close", "launch", "close"]


def test_login_prints_plain_chrome_command_without_import_lock_input_or_halt_clear(tmp_path, monkeypatch, capsys):
    w = worker()
    profile = w.init_profile(tmp_path / "fomo profile's private")
    halt = profile / "halt.json"
    halt.write_text('{"reason":"login_required"}')
    before = halt.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("login wrapper attempted browser, profile lock or input")
    monkeypatch.setattr(w, "profile_lock", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    original_import = __import__
    def guarded_import(name, *args, **kwargs):
        if name.startswith("playwright"):
            forbidden()
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr("builtins.__import__", guarded_import)
    assert w.main(["--profile-dir", str(profile), "--login"]) == 0
    output = capsys.readouterr().out
    assert shlex.split(output.splitlines()[0]) == [
        "/usr/bin/google-chrome", "--user-data-dir=" + str(profile),
        "--profile-directory=Default", "--disable-gpu", "--ozone-platform=x11",
        "--password-store=basic", "https://fomo.family"]
    assert "close" in output.lower() and "before collection" in output.lower()
    assert halt.read_bytes() == before


def test_actual_dom_uses_any_visible_known_login_button_and_positive_header():
    from playwright.sync_api import sync_playwright
    w = worker()
    hidden = '<button style="display:none">Log in</button>'
    fixtures = [(hidden + '<button>' + label + '</button>', "login_required")
                for label in ("Log in", "Sign in", "Iniciar sesión", "Inicia sesión")]
    fixtures += [
        (hidden + '<button>Inicia sesión</button><button><a href="/profile/fixture">Account</a></button>', None),
        ('<div>Log in</div><button>Log in now</button>', "auth_unproven"),
        ('<button style="visibility:hidden"><a href="/profile/stale">Account</a></button><button>Log in</button>', "login_required"),
    ]
    for style in ("visibility:hidden", "visibility:collapse", "display:none",
                  "display:inline-block;width:0;height:0;padding:0;border:0;overflow:hidden"):
        fixtures += [
            ('<button><a href="/profile/stale" style="' + style + '">Account</a></button><button>Log in</button>', "login_required"),
            ('<button style="' + style + '">Log in</button>', "auth_unproven"),
        ]
    got = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True,
            args=[*w.CHROME_ARGS, "--disable-background-networking", "--host-resolver-rules=MAP * ~NOTFOUND"])
        try:
            context = browser.new_context(service_workers="block")
            context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body="<title>Fomo fixture</title>"))
            page = context.new_page()
            page.goto("https://fomo.family/discover", wait_until="domcontentloaded", timeout=10000)
            for html, _ in fixtures:
                page.set_content('<title>Fomo fixture</title>' + html)
                try:
                    assert w._check(page, SimpleNamespace(reason=None)) is True
                    got.append(None)
                except w.PollError as exc:
                    got.append(exc.reason)
        finally:
            browser.close()
    assert got == [reason for _, reason in fixtures]


def test_first_list_waits_for_delayed_visible_rows_in_actual_local_dom(tmp_path):
    from playwright.sync_api import sync_playwright
    w = worker()
    html = ('<title>Fomo fixture</title><button><a href="/profile/fixture">Account</a></button>'
            '<button type="button" aria-pressed="true">En tendencia</button>'
            '<button role="tab" aria-selected="true">24h</button>'
            f'<div id="thesis-trigger-default-thesis-{SOL}:1399811149" style="display:none">'
            f'<a href="/tokens/solana/{SOL}">TOKEN</a><div>0</div></div>'
            '<script>setTimeout(() => {document.querySelector("div[id]").style.display="block";'
            'window.rowsReady=true;}, 500);</script>')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True,
            args=[*w.CHROME_ARGS, "--disable-background-networking", "--host-resolver-rules=MAP * ~NOTFOUND"])
        try:
            context = browser.new_context(service_workers="block")
            context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
            page = context.new_page()
            result = w.collect(page, out_dir=tmp_path / "intake", clock_file=tmp_path / "clock.json",
                               halt_file=tmp_path / "halt.json", polls=1, clock=lambda: AT)
            assert page.evaluate("window.rowsReady === true")
            assert result["items"] == 1 and result["errors"] == 0
            with daily(tmp_path) as db:
                assert db.execute("SELECT status, items FROM polls").fetchall() == [("ok", 1)]
        finally:
            browser.close()


def test_initial_rows_present_adds_no_wait_and_later_empty_list_is_not_retried(tmp_path):
    page = Page()
    page.period = "24h"
    run(tmp_path, page, polls=1)
    assert page.waits == []
    second = tmp_path / "second"
    second.mkdir()
    def empty_later(milliseconds):
        if milliseconds >= 10000:
            page.rows = []
    page.on_wait = empty_later
    run(second, page, polls=2)
    assert page.elapsed < 16
    with daily(second) as db:
        assert db.execute("SELECT status, error FROM polls ORDER BY id").fetchall() == [("ok", None), ("error", "empty_dom")]


def test_initial_rows_absent_reaches_deadline_with_typed_unknown_counts(tmp_path):
    page = Page()
    page.rows, page.period = [], "24h"
    result = run(tmp_path, page, polls=1)
    assert page.elapsed == 10 and page.captures == []
    assert result["errors"] == 1 and not (tmp_path / "halt.json").exists()
    with daily(tmp_path) as db:
        assert db.execute("SELECT status, error, items, malformed FROM polls").fetchall() == [("error", "empty_dom", None, None)]


@pytest.mark.parametrize("status", [403, 429])
def test_initial_row_wait_keeps_http_refusal_immediate_and_durable(tmp_path, status):
    page = Page()
    page.rows, page.period = [], "24h"
    def refused(milliseconds):
        if page.elapsed >= 0.5:
            page.emit(status)
            assert (tmp_path / "halt.json").exists()
    page.on_wait = refused
    result = run(tmp_path, page, polls=1)
    assert page.elapsed == 0.5 and page.captures == []
    assert result["halted"] == "http_" + str(status)
    with daily(tmp_path) as db:
        assert db.execute("SELECT status, error FROM polls").fetchall() == [("error", "http_" + str(status))]


def test_initial_row_count_refusal_blocks_capture_even_when_rows_are_present(tmp_path):
    page = Page()
    page.period = "24h"
    page.on_count = lambda: page.emit(429)
    result = run(tmp_path, page, polls=1)
    assert result["halted"] == "http_429" and page.captures == [] and page.waits == []
    with daily(tmp_path) as db:
        assert db.execute("SELECT status, error FROM polls").fetchall() == [("error", "http_429")]


SIGNAL_PROBE = r'''
from pathlib import Path
from types import SimpleNamespace
import json, os, runpy, signal, subprocess, sys

repo, scratch, phase = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path[:0] = [str(repo), str(repo / "tests")]
from test_memecoin_desk_fomo_worker import Page, LABELS, SOL
from scripts import memecoin_desk_fomo_intake as w

profile = w.init_profile(scratch / "profile")
os.environ["SNIPER_STATE_ROOT"] = str(scratch)
signal.signal(signal.SIGINT, signal.SIG_IGN)  # Model a nohup background job.
page, child, real_manager = Page(), None, None
if phase == "rows":
    page.rows, page.period = [], "24h"
closed, captures, clicks = False, [], []

def ready(pid):
    print(json.dumps({"ready": True, "owned_pid": pid}), flush=True)

def reap():
    if child is not None:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)

def fake_close():
    global closed
    reap()
    closed = True

def fake_launch(path, **kwargs):
    global child
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(scratch)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    if phase == "startup":
        ready(child.pid)
        signal.pause()
    return SimpleNamespace(pages=[page], close=fake_close)

def fake_wait(milliseconds):
    if page.captures or phase == "rows":
        if phase == "refusal":
            page.emit(429)
        ready(child.pid)
        signal.pause()
page.on_wait = fake_wait

if phase == "chrome":
    from playwright.sync_api import sync_playwright as real_factory

def real_launch(path, **kwargs):
    global page
    kwargs["args"] = [*kwargs["args"], "--disable-background-networking", "--host-resolver-rules=MAP * ~NOTFOUND"]
    context = real_playwright.chromium.launch_persistent_context(path, service_workers="block", **kwargs)
    html = '<title>Fomo fixture</title><button><a href="/profile/fixture">Account</a></button>'
    html += ''.join('<button type="button" aria-pressed="%s" onclick="console.log(\'CLICK\')">%s</button>' %
                    ("true" if i == 0 else "false", label) for i, label in enumerate(LABELS))
    html += '<button role="tab" aria-selected="true">24h</button>'
    html += '<div id="thesis-trigger-default-thesis-%s:1399811149"><a href="/tokens/solana/%s">TOKEN</a><div>0</div></div>' % (SOL, SOL)
    context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
    page = context.pages[0]
    pid = int((profile / "SingletonLock").readlink().name.rsplit("-", 1)[1])
    page.on("console", lambda message: ready(pid) if message.text == "READY" else clicks.append("click") if message.text == "CLICK" else None)
    locator, wait, close = page.locator, page.wait_for_timeout, context.close
    def tracked_locator(selector, *args, **kwargs):
        loc = locator(selector, *args, **kwargs)
        if selector == w.ROW_SELECTOR:
            evaluate = loc.evaluate_all
            def capture(*args, **kwargs):
                captures.append(1)
                return evaluate(*args, **kwargs)
            loc.evaluate_all = capture
        return loc
    signaled = False
    def paced_wait(milliseconds):
        nonlocal signaled
        if captures and not signaled:
            signaled = True
            page.evaluate("setTimeout(() => console.log('READY'), 50)")
        wait(milliseconds)
    def tracked_close():
        global closed
        close()
        closed = True
    page.locator, page.wait_for_timeout, context.close = tracked_locator, paced_wait, tracked_close
    return context

class Manager:
    def __enter__(self):
        global real_manager, real_playwright
        if phase == "chrome":
            real_manager = real_factory()
            real_playwright = real_manager.__enter__()
        return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=real_launch if phase == "chrome" else fake_launch))
    def __exit__(self, *args):
        try:
            if real_manager is not None:
                real_manager.__exit__(*args)
            else:
                reap()
        finally:
            (scratch / "closed.json").write_text(json.dumps({"context_closed": closed, "manager_closed": True,
                "captures": len(captures) if phase == "chrome" else len(page.captures),
                "clicks": clicks if phase == "chrome" else page.clicks,
                "child_reaped": child is None or child.returncode is not None}))

sys.modules["playwright.sync_api"] = SimpleNamespace(sync_playwright=Manager)
sys.argv = [str(repo / "scripts/memecoin_desk_fomo_intake.py"), "--profile-dir", str(profile), "--loop"]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def _signal_probe(tmp_path, phase, stop_signal=signal.SIGTERM):
    proc = subprocess.Popen([sys.executable, "-c", SIGNAL_PROBE, str(worker().ROOT), str(tmp_path), phase],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    owned_pid = None
    try:
        assert select.select([proc.stdout], [], [], 15)[0], "collector never reached signal barrier"
        line = proc.stdout.readline()
        assert line, proc.stderr.read()
        owned_pid = json.loads(line)["owned_pid"]
        proc.send_signal(stop_signal)
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == (2 if phase == "refusal" else 0), (stdout, stderr)
        assert not stderr
        state = json.loads((tmp_path / "closed.json").read_text())
        assert state["manager_closed"] and state["child_reaped"]
        assert state["context_closed"]
        assert state["captures"] == (0 if phase in {"startup", "rows"} else 1)
        assert "Blue chips" not in state["clicks"]
        if phase == "chrome":
            assert state["clicks"] == []
        assert not Path("/proc", str(owned_pid)).exists()
        with worker().profile_lock(tmp_path / "profile"):
            pass
        if phase == "refusal":
            assert json.loads((tmp_path / "profile/halt.json").read_text())["reason"] == "http_429"
        stores = [p for p in (tmp_path / "data/desk/intake/fomo").iterdir()
                  if p.suffix == ".db" and p.name.startswith("20")] if phase not in {"startup", "rows"} else []
        if stores:
            with sqlite3.connect(stores[0]) as db:
                expected = [("ok", None)] + ([("error", "http_429")] if phase == "refusal" else [])
                assert db.execute("SELECT status, error FROM polls ORDER BY id").fetchall() == expected
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        if owned_pid is not None:
            cmdline = Path("/proc", str(owned_pid), "cmdline")
            try:
                ours = str(tmp_path).encode() in cmdline.read_bytes()
            except OSError:
                ours = False
            if ours:
                try:
                    os.killpg(owned_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.parametrize("phase", ["paced", "startup", "refusal", "rows"])
def test_sigterm_unwinds_owned_resources_without_next_capture(tmp_path, phase):
    _signal_probe(tmp_path, phase)


def test_sigterm_closes_isolated_local_chrome_profile(tmp_path):
    _signal_probe(tmp_path, "chrome")


def test_sigint_uses_safe_shutdown_instead_of_inherited_nohup_ignore(tmp_path):
    _signal_probe(tmp_path, "paced", signal.SIGINT)
