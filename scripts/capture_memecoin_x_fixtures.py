#!/usr/bin/env python3
"""Manual, headed X card fixture capture through a dedicated Playwright profile.

Read-only: goto / reload / DOM reads / bounded wheel scroll only. Never automates credentials,
2FA, challenges, or any interaction (no fill/type/press/click/submit). Writes sanitized public
DOM fragments + a public sidecar into tests/fixtures/memecoin_x/dom only. No DB access.

    login   --profile-dir ABSOLUTE_DIR
    capture --profile-dir ABSOLUTE_DIR --kind {direct,repost,quote,reply} [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.analysis.memecoin_x.dom_fixture import sanitize_card_html

PROFILE_ENV = "MEMECOIN_X_PROFILE_DIR"
KINDS = ("direct", "repost", "quote", "reply")
HOME = "https://x.com/home"
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "memecoin_x" / "dom"
SCHEMA_VERSION = 1

# In-page extraction of enumeration facts and the chosen card's canonical id. Playwright
# evaluate returns raw values (unlike redaction-prone extension tools).
_CARD_INFO_JS = """node => {
  const txt = node.querySelector('[data-testid="tweetText"]');
  const nameA = node.querySelector('[data-testid="User-Name"] a[href^="/"]');
  const timeA = Array.from(node.querySelectorAll('a[href*="/status/"]'))
      .find(x => x.querySelector('time'));
  const href = timeA ? timeA.getAttribute('href') : '';
  const m = href.match(/\\/status\\/(\\d+)/);
  return {
    author: (nameA ? nameA.getAttribute('href') : '').replace(/^\\//, ''),
    status_id: m ? m[1] : '',
    preview: txt ? txt.textContent.slice(0, 120) : '',
  };
}"""


def _profile_dir(arg) -> Path:
    p = Path(arg or os.environ.get(PROFILE_ENV, "")).expanduser()
    if not str(p):
        sys.exit(f"profile dir required (--profile-dir or ${PROFILE_ENV})")
    if not p.is_absolute():
        sys.exit("profile-dir must be an absolute path")
    resolved = p.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        sys.exit("profile-dir must be OUTSIDE the repository")
    return resolved


def _out_dir(arg) -> Path:
    out = Path(arg).resolve() if arg else FIXTURE_DIR
    if not (out == FIXTURE_DIR or FIXTURE_DIR in out.parents):
        sys.exit(f"--out-dir must resolve inside {FIXTURE_DIR}")
    return out


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _open(p, profile: Path):
    ctx = p.chromium.launch_persistent_context(str(profile), headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def cmd_login(args) -> None:
    from playwright.sync_api import sync_playwright
    profile = _profile_dir(args.profile_dir)
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile.chmod(0o700)
    with sync_playwright() as p:
        ctx, page = _open(p, profile)
        page.goto(HOME)
        input("Log the burner in and select the Following tab in the opened window, "
              "then press Enter here to save the session and close... ")
        ctx.close()
    print(f"Profile ready at {profile}")


def cmd_capture(args) -> None:
    from playwright.sync_api import sync_playwright
    profile = _profile_dir(args.profile_dir)
    out = _out_dir(args.out_dir)
    kind = args.kind
    with sync_playwright() as p:
        ctx, page = _open(p, profile)
        page.goto(HOME)
        page.wait_for_timeout(3000)
        if "x.com/home" not in page.url:
            ctx.close()
            sys.exit(f"expected to be on {HOME}, got {page.url}")
        tab = page.query_selector('[role="tab"][aria-selected="true"]')
        if not tab or "Following" not in (tab.inner_text() or ""):
            ctx.close()
            sys.exit("select the Following tab in the browser, then re-run capture")

        cards = page.locator('article[data-testid="tweet"]')
        count = cards.count()
        if not count:
            ctx.close()
            sys.exit("no cards visible; scroll the feed and retry")
        infos = []
        for i in range(count):
            info = cards.nth(i).evaluate(_CARD_INFO_JS)
            infos.append(info)
            print(f"[{i:2d}] @{info['author']:<20} status={info['status_id'] or '-':<20} "
                  f":: {info['preview']}")

        idx = int(input(f"index of the {kind} card to capture: ").strip())
        if not (0 <= idx < count):
            ctx.close()
            sys.exit("index out of range")
        ref_visible = input(
            "Is the quoted/replied SOURCE post id visibly present in this card? [y/N]: "
        ).strip().lower() == "y"
        reviewed = input(
            "Confirm this fragment is PUBLIC-only (no DMs / private content)? [y/N]: "
        ).strip().lower() == "y"
        if not reviewed:
            ctx.close()
            sys.exit("public-only confirmation required; nothing written")

        raw_html = cards.nth(idx).evaluate("node => node.outerHTML")
        clean = sanitize_card_html(raw_html)  # raises ValueError on any secret material
        sidecar = {
            "schema_version": SCHEMA_VERSION,
            "kind": kind,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source_post_id": infos[idx]["status_id"] or None,
            "expected_relationship": kind,
            "referenced_post_visible": ref_visible,
            "reviewed_public_only": reviewed,
        }
        _atomic_write(out / f"{kind}.html", clean)
        _atomic_write(out / f"{kind}.json", json.dumps(sidecar, indent=2) + "\n")
        ctx.close()
    print(f"wrote {out / (kind + '.html')} and {out / (kind + '.json')}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login")
    lg.add_argument("--profile-dir")
    lg.set_defaults(func=cmd_login)
    cap = sub.add_parser("capture")
    cap.add_argument("--profile-dir")
    cap.add_argument("--kind", required=True, choices=KINDS)
    cap.add_argument("--out-dir")
    cap.set_defaults(func=cmd_capture)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
