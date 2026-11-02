#!/usr/bin/env python3
"""Axiom browser session for the observation lanes.

  login              open a headed window on the profile; the OPERATOR logs in; press Enter to save
  probe              headless: report whether the profile is still logged in (no capture)
  capture-fixtures   headed: save page HTML + raw WS frames for the parser tests
  run                headless capture worker (needs AXIOM_CAPTURE_ENABLED=1)

The script never types a credential. The profile directory is outside the repository and 0700.
"""
from contextlib import contextmanager
import fcntl
import signal
import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
FIXTURES = REPO / "tests" / "fixtures" / "axiom"


@contextmanager
def capture_lock(path):
    """Allow one capture process for each state directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _profile(args):
    from sniper.feeds.axiom_browser import default_profile_dir
    p = Path(args.profile_dir).expanduser() if args.profile_dir else default_profile_dir()
    p = p.resolve()
    if p == REPO or REPO in p.parents:
        sys.exit("profile dir must be OUTSIDE the repository: %s" % p)
    return p


def cmd_login(args):
    from playwright.sync_api import sync_playwright
    from sniper.feeds.axiom_browser import PAGES
    profile = _profile(args)
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile.chmod(0o700)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile), headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(PAGES["trackers"])
        input("Log in to Axiom in the opened window (password, code, captcha). When the Trackers page "
              "shows your account, press Enter here to save the session and close... ")
        ctx.close()
    print("profile ready at", profile)


def _cdp(args):
    return args.cdp_url or os.environ.get("AXIOM_CDP_URL", "").strip() or None


def cmd_probe(args):
    from playwright.sync_api import sync_playwright
    from sniper.feeds.axiom_browser import PAGES, HALT_MARKERS, open_browser
    cdp = _cdp(args)
    with sync_playwright() as p:
        ctx, closer = open_browser(p, profile_dir=None if cdp else _profile(args), cdp_url=cdp, headless=True)
        page = ctx.new_page()
        page.goto(PAGES["trackers"], wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(8_000)
        url, html = page.url, page.content()
        page.close(); closer()
    blocked = any(m in url.lower() for m in HALT_MARKERS) or "challenges.cloudflare.com" in html
    print(json.dumps({"url": url, "logged_in": not blocked and "Trackers" in html, "bytes": len(html)}))


def cmd_capture_fixtures(args):
    from playwright.sync_api import sync_playwright
    from sniper.feeds.axiom_browser import PAGES
    from sniper.feeds.axiom_browser import open_browser
    cdp = _cdp(args)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    frames = []
    with sync_playwright() as p:
        ctx, closer = open_browser(p, profile_dir=None if cdp else _profile(args), cdp_url=cdp,
                                   headless=not args.headed)
        for name, url in PAGES.items():
            page = ctx.new_page()
            page.on("websocket", lambda ws, n=name: ws.on(
                "framereceived", lambda f: frames.append({"mono_ns": time.monotonic_ns(), "page": n,
                                                          "socket": ws.url.split("?")[0],
                                                          "text": f if isinstance(f, str) else f.decode("utf-8", "replace")})))
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(args.dwell_s * 1000)
            (FIXTURES / f"{name}.html").write_text(page.content())
            print("saved", name, "html")
            page.close()
        closer()
    with open(FIXTURES / "ws_frames.jsonl", "w") as fh:
        for fr in frames:
            fh.write(json.dumps(fr) + "\n")
    print("saved", len(frames), "ws frames")


def cmd_run(args):
    from sniper.util.envfiles import load_env_files
    load_env_files(str(REPO))               # the knobs live in .env, like the runner
    from sniper.feeds.axiom_browser import CaptureCore, run_capture, PAGES
    from sniper.persistence.db_core import get_connection
    from sniper.persistence.db_axiom import ensure_axiom_tables, attach_raw_store
    from sniper.services.memecoin_alerts import send_alert
    from sniper.settings import memecoin as cfg
    from sniper.util.clock import real_utc_now_iso
    from sniper.util.state_paths import state_path
    if not cfg.AXIOM_CAPTURE_ENABLED:
        sys.exit("AXIOM_CAPTURE_ENABLED is not set; nothing started")
    try:
        parsers = __import__("sniper.feeds.axiom_parse", fromlist=["x"])
    except ImportError:
        parsers = None
    with capture_lock(state_path('data', 'axiom_capture.lock')):
        conn = get_connection()
        ensure_axiom_tables(conn)
        attach_raw_store(conn, state_path("data", "axiom_raw.db"))   # raw frames: own file, not the ledger
        core = CaptureCore(conn, now_iso_fn=real_utc_now_iso,
                           alert_fn=lambda t: send_alert("AXIOM", t),
                           heartbeat_path=state_path("data", "axiom_capture.heartbeat"),
                           halt_path=state_path("data", "axiom_capture.halted"), parsers=parsers)
        def request_stop(_signum, _frame):
            core.stop_requested = True
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        pages = [x for x in (args.pages or "trackers,discover").split(",") if x in PAGES]
        cdp = _cdp(args) or cfg.AXIOM_CDP_URL or None
        reason = run_capture(core, profile_dir=None if cdp else _profile(args), cdp_url=cdp, pages=pages,
                             page_interval_s=cfg.AXIOM_PAGE_INTERVAL_S,
                             retention_days=cfg.AXIOM_RAW_RETENTION_DAYS)
        print("capture ended:", reason)


def cmd_reset_halt(args):
    from sniper.util.state_paths import state_path
    path = Path(state_path('data', 'axiom_capture.halted'))
    with capture_lock(state_path('data', 'axiom_capture.lock')):
        path.unlink(missing_ok=True)
    print('capture halt reset')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["login", "probe", "capture-fixtures", "run", "reset-halt"])
    ap.add_argument("--profile-dir")
    ap.add_argument("--cdp-url", help="attach to a running Chrome, e.g. http://127.0.0.1:9222")
    ap.add_argument("--pages")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--dwell-s", type=int, default=20)
    args = ap.parse_args()
    {"login": cmd_login, "probe": cmd_probe, "capture-fixtures": cmd_capture_fixtures, "run": cmd_run, "reset-halt": cmd_reset_halt}[args.cmd](args)


if __name__ == "__main__":
    main()
