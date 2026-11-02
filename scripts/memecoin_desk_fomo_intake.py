#!/usr/bin/env python3
"""Collect five Fomo discovery lists as partial DOM evidence, without trading.

  python3 scripts/memecoin_desk_fomo_intake.py --init-profile
  python3 scripts/memecoin_desk_fomo_intake.py --login
  python3 scripts/memecoin_desk_fomo_intake.py              # five polls, one list every 15s

--login prints a plain Chrome command for operator login. Close that Chrome before
collection. A saved halt.json blocks collection until the operator resolves the refusal
and removes that file; the login wrapper preserves it.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.feeds.fomo_intake import LIST_LABELS, halt_reason, parse_dom_rows  # noqa: E402
from sniper.services import memecoin_desk_intake as intake  # noqa: E402

URL = "https://fomo.family/discover"
PROFILE = Path.home() / ".local/share/sniper/fomo_intake_profile"
MARKER = ".sniper-fomo-intake-profile"
ROW_SELECTOR = 'div[id^="thesis-trigger-default-thesis-"]'
TIMEOUT_MS = 10000
CHROME_ARGS = ["--disable-gpu"]


def _profile_path(path):
    path = Path(path).expanduser().resolve()
    if path == ROOT or ROOT in path.parents or path in ROOT.parents:
        raise ValueError("profile must be separate from the repository")
    return path


def init_profile(path):
    """Create an empty private profile; never adopt or chmod an operator profile."""
    path = _profile_path(path)
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    if not path.is_dir() or path.stat().st_mode & 0o777 != 0o700:
        raise ValueError("dedicated profile must already be mode 0700")
    marker = path / MARKER
    if marker.is_file() and marker.read_text() == "fomo-intake-v1\n":
        return path
    if any(path.iterdir()):
        raise ValueError("refusing an existing nonempty browser profile")
    with os.fdopen(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write("fomo-intake-v1\n")
    return path


@contextmanager
def profile_lock(path):
    """Allow one collector; the operator must close manual Chrome first."""
    with (path / "collector.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _save_halt(path, reason, at):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        json.dump({"reason": reason, "host_received_at": at}, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class PollError(Exception):
    def __init__(self, reason, *, halt=False):
        self.reason, self.halt = reason, halt


def _stop_if_requested(stopping):
    if stopping is not None and stopping():
        raise KeyboardInterrupt


def _wait(page, milliseconds, stopping):
    if stopping is None:
        page.wait_for_timeout(milliseconds)
        return
    while milliseconds > 0:
        _stop_if_requested(stopping)
        chunk = min(250, milliseconds)
        page.wait_for_timeout(chunk)
        milliseconds -= chunk
    _stop_if_requested(stopping)


def _record(out_dir, filt, at, offset, rows, error):
    day = at if offset is None else at - offset
    conn = intake.open_day(out_dir, "fomo", day)
    try:
        return intake.record_poll(conn, "fomo", filt, at, rows, offset_s=offset, error=error)
    finally:
        conn.close()


class ResponseGuard:
    """Read only Fomo-domain response status and URL; never headers or payloads."""
    def __init__(self, clock, clock_file, out_dir, halt_file, stopping=None):
        self.reason = None
        self.receipt = None
        self.recorded = False
        self.filt = next(iter(LIST_LABELS))
        self.clock, self.clock_file = clock, clock_file
        self.out_dir, self.halt_file = out_dir, halt_file
        self.stopping = stopping

    def response(self, response):
        try:
            route = urlsplit(response.url)
            host = route.hostname or ""
            fomo_host = host == "fomo.family" or host.endswith(".fomo.family")
            same_origin = route.scheme == "https" and fomo_host and route.port in (None, 443)
        except ValueError:
            return
        if same_origin and response.status in (401, 403, 429) and self.reason is None:
            self.receipt = self.clock(), intake.clock_offset(self.clock_file)
            self.reason = halt_reason(URL, "", True, response.status)
            at, offset = self.receipt
            _save_halt(self.halt_file, self.reason, at)
            _record(self.out_dir, self.filt, at, offset, [], self.reason)
            self.recorded = True


def _check(page, guard, *, provisional=False):
    if guard.reason:
        raise PollError(guard.reason, halt=True)
    _stop_if_requested(getattr(guard, "stopping", None))
    state = page.evaluate("""() => {
        const visible = element => {
            const rect = element.getBoundingClientRect();
            return getComputedStyle(element).visibility === 'visible' && rect.width > 0 && rect.height > 0;
        };
        const header = Array.from(document.querySelectorAll('button a[href^="/profile/"]'))
            .some(visible);
        const buttons = document.evaluate('//button[normalize-space(.)="Log in" or normalize-space(.)="Sign in" '
            + 'or normalize-space(.)="Iniciar sesión" or normalize-space(.)="Inicia sesión"]', document,
            null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        const login = Array.from({length: buttons.snapshotLength}, (_, i) => buttons.snapshotItem(i))
            .some(visible);
        return {
            authenticated: header ? true : login ? false : null,
            text: [document.title, ...Array.from(document.querySelectorAll(
                'h1, [role="dialog"] h2, [role="dialog"] [role="heading"]'
            )).slice(0, 4).map(el => el.innerText.slice(0, 300))].join('\\n')
        };
    }""")
    reason = guard.reason or halt_reason(page.url, state["text"], state["authenticated"])
    if reason:
        if provisional and reason in {"login_required", "auth_unproven"}:
            return False
        raise PollError(reason, halt=True)
    _stop_if_requested(getattr(guard, "stopping", None))
    return True


def _wait_ready(page, guard, monotonic):
    """Bound auth hydration checks; renderer IPC itself has no hard deadline."""
    if _check(page, guard, provisional=True):
        return
    deadline = monotonic() + TIMEOUT_MS / 1000
    for _ in range(40):
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        _wait(page, min(250, max(1, int(remaining * 1000))), guard.stopping)
        if _check(page, guard, provisional=True):
            return
    _check(page, guard)


def _select(page, guard, control, attribute):
    if control.count() != 1:
        raise PollError("selector_changed")
    control.wait_for(state="visible", timeout=TIMEOUT_MS)
    if control.get_attribute(attribute) != "true":
        _check(page, guard)
        control.click(timeout=TIMEOUT_MS)
        _wait(page, 800, guard.stopping)
    _check(page, guard)
    if control.get_attribute(attribute) != "true":
        raise PollError("selector_changed")


def _wait_initial_rows(page, guard, monotonic):
    visible_rows = page.locator(ROW_SELECTOR + ":visible")
    deadline = None
    for attempt in range(41):
        _check(page, guard)
        ready = visible_rows.count() > 0
        _check(page, guard)
        if ready:
            return
        if deadline is None:
            deadline = monotonic() + TIMEOUT_MS / 1000
        remaining = deadline - monotonic()
        if remaining <= 0 or attempt == 40:
            raise PollError("empty_dom")
        _wait(page, min(250, max(1, int(remaining * 1000))), guard.stopping)


def collect(page, *, out_dir, clock_file, halt_file, polls=5, loop=False, clock=time.time, monotonic=time.monotonic, stopping=None):
    """One rendered list per poll, with no immediate retry or completeness claim."""
    stats = {"polls": 0, "items": 0, "malformed": 0, "errors": 0, "halted": None}
    if Path(halt_file).exists():
        return {**stats, "halted": "saved_halt"}
    guard = ResponseGuard(clock, clock_file, out_dir, halt_file, stopping)
    page.on("response", guard.response)
    filters = list(LIST_LABELS)
    while loop or stats["polls"] < polls:
        started = monotonic()
        filt = filters[stats["polls"] % len(filters)]
        guard.filt = filt
        error, halted, rows = None, False, []
        try:
            if stats["polls"] == 0:
                _stop_if_requested(stopping)
                page.goto(URL, wait_until="domcontentloaded", timeout=15000)
                _wait_ready(page, guard, monotonic)
            _check(page, guard)
            control = page.locator('button[type="button"][aria-pressed]').filter(
                has_text=re.compile(r"^\s*" + re.escape(LIST_LABELS[filt]) + r"\s*$"))
            _select(page, guard, control, "aria-pressed")
            if filt == "trending24h":
                _select(page, guard, page.get_by_role("tab", name="24h", exact=True), "aria-selected")
            if stats["polls"] == 0:
                _wait_initial_rows(page, guard, monotonic)
            # shortcut: selected controls bind an as-rendered snapshot; upgrade only with a verified response/data-version binding.
            _stop_if_requested(stopping)
            at, offset = clock(), intake.clock_offset(clock_file)
            raw = page.locator(ROW_SELECTOR).evaluate_all("""rows => rows.slice(0, 101).map(r => ({
                row_id: r.id, href: r.querySelector('a[href*="/tokens/"]')?.getAttribute('href'),
                text: r.innerText, cells: Array.from(r.children).map(c => c.innerText)
            }))""")
            _check(page, guard)
            if not isinstance(raw, list):
                raise PollError("malformed_dom")
            if not raw:
                raise PollError("empty_dom")
            if len(raw) > 100:
                raise PollError("unexpected_row_count")
            rows = parse_dom_rows(raw)
        except Exception as exc:
            if guard.reason:
                at, offset = guard.receipt
                error, halted = guard.reason, True
            else:
                at, offset = clock(), intake.clock_offset(clock_file)
                if isinstance(exc, PollError):
                    error, halted = exc.reason, exc.halt
                else:
                    error = "timeout" if type(exc).__name__ == "TimeoutError" else "browser_error:" + type(exc).__name__
        if guard.recorded:
            result = {"items": 0, "malformed": 0}
        else:
            if halted:
                _save_halt(halt_file, error, at)
            result = _record(out_dir, filt, at, offset, rows, error)
        stats["polls"] += 1
        stats["items"] += result["items"]
        stats["malformed"] += result["malformed"]
        stats["errors"] += int(error is not None)
        if halted:
            stats["halted"] = error
            return stats
        if loop or stats["polls"] < polls:
            wait_ms = max(1000, 15000 - int((monotonic() - started) * 1000))
            try:
                _wait(page, wait_ms, stopping)  # Pump events; an overrun never creates catch-up polls.
            except BaseException:
                if not guard.recorded:
                    raise
            if guard.recorded:
                return {**stats, "polls": stats["polls"] + 1, "errors": stats["errors"] + 1, "halted": guard.reason}
    return stats


def main(argv=None, *, stopping=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--init-profile", action="store_true")
    action.add_argument("--login", action="store_true")
    parser.add_argument("--profile-dir", type=Path, default=PROFILE)
    parser.add_argument("--polls", type=int, default=5)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--headed", action="store_true", help="show Chrome using X11")
    args = parser.parse_args(argv)
    if args.polls < 1:
        parser.error("--polls must be positive")
    profile = None
    try:
        if args.init_profile:
            init_profile(args.profile_dir)
            print("[fomo-intake] empty dedicated profile ready")
            return 0
        profile = _profile_path(args.profile_dir)
        if not (profile / MARKER).is_file():
            raise ValueError("initialize the dedicated profile with --init-profile first")
        profile = init_profile(profile)
        if args.login:
            print(shlex.join(["/usr/bin/google-chrome", "--user-data-dir=" + str(profile),
                              "--profile-directory=Default", *CHROME_ARGS, "--ozone-platform=x11",
                              "--password-store=basic", "https://fomo.family"]))
            print("Stop the collector and close Chrome using this profile first. Log in manually, then close that Chrome before collection. Existing halt.json is preserved.")
            return 0
        with profile_lock(profile):
            if (profile / "halt.json").exists():
                print("[fomo-intake] saved_halt; operator review required")
                return 2
            from sniper.util.state_paths import require_state_root_outside_release
            root = require_state_root_outside_release()
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                _stop_if_requested(stopping)
                context = playwright.chromium.launch_persistent_context(
                    str(profile), channel="chrome", locale="es-ES", headless=not args.headed,
                    args=CHROME_ARGS + (["--ozone-platform=x11"] if args.headed else []),
                    accept_downloads=False, timeout=15000)
                try:
                    _stop_if_requested(stopping)
                    page = context.pages[0] if context.pages else context.new_page()
                    for extra in context.pages[1:]:
                        extra.close()
                    stats = collect(page, out_dir=root / "data/desk/intake", clock_file=root / "data/clock_offset.json",
                                    halt_file=profile / "halt.json", polls=args.polls, loop=args.loop, stopping=stopping)
                    print(json.dumps(stats, sort_keys=True))
                    return 2 if stats["halted"] else 0
                finally:
                    context.close()
    except KeyboardInterrupt:
        return 2 if profile is not None and (profile / "halt.json").exists() else 0
    except Exception as exc:
        print("[fomo-intake] refused:" + type(exc).__name__)
        return 2


if __name__ == "__main__":
    # Playwright manager entry and renderer IPC still have no hard shutdown deadline.
    stop_requested = False
    def request_stop(_signum, _frame):
        global stop_requested
        stop_requested = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    raise SystemExit(main(stopping=lambda: stop_requested))
