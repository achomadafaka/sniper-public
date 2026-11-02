"""Wallet Map W1 readability (spec docs/superpowers/specs/2026-10-07-wallet-map-overhaul-design.md)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_page_wide_crt_overlay_returns():
    # The operator removed the CRT scanline/vignette from every tab: a body pseudo-element paints
    # over all text, including expanded Wallet Map.
    for css in sorted((ROOT / 'static').rglob('*.css')):
        # body::before, body.x::after, body:has(...)::before: any pseudo on body itself, not a descendant's
        found = re.findall(r'\bbody(?:[.:#\[][^\s{,]*)?::?(?:before|after)\b', css.read_text())
        assert not found, f'{css.relative_to(ROOT)} paints a page-wide body overlay: {found}'
