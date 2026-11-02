#!/usr/bin/env python3
"""
browse.py — Headless browser screenshot helper for research tasks.

Usage:
    python3 scripts/browse.py <url> [--out /tmp/shot.png] [--width 1600] [--height 1400] [--wait 6]

Playwright + Chromium, headless, no-sandbox (VPS-compatible).
Claude reads the output PNG natively (multimodal — no OCR, actual image vision).

Notes:
  - Do NOT pass --full on React SPAs (Polymarket, etc.) — infinite DOM causes timeout.
  - Use a tall --height (1400+) to capture more content in one viewport.
  - Run sequentially, not in parallel — VPS memory is limited.
"""
import argparse
import os
import time
from playwright.sync_api import sync_playwright


def screenshot(url: str, out: str, width: int = 1600, height: int = 1400,
               wait: float = 6.0) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with sync_playwright() as p:
        browser = ctx = None
        try:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = browser.new_context(
                viewport={"width": width, "height": height},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()
            page.set_default_timeout(60000)
            page.goto(url, wait_until="domcontentloaded")
            time.sleep(wait)  # let React hydrate and render
            page.screenshot(path=out, full_page=False, timeout=60000)
        finally:
            if ctx is not None:
                ctx.close()
            if browser is not None:
                browser.close()
    size_kb = os.path.getsize(out) // 1024
    print(f"✓ {out}  ({size_kb} KB)")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", default="/tmp/browse_shot.png")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1400)
    ap.add_argument("--wait", type=float, default=6.0)
    args = ap.parse_args()
    screenshot(args.url, args.out, args.width, args.height, args.wait)
