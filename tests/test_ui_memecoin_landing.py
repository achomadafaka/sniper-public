"""The landing screen is Memecoin live (operator 2026-10-08): venues ordered Memecoin, Polymarket,
Hyperliquid, then Kalshi and Intel; a fresh load and a reload with no `tab` open the Memecoin Copy
tab, whose own default view is `live`. The template, the router and the chrome must agree."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates" / "index.html").read_text()
JS = (ROOT / "static" / "app.js").read_text()


def _venue_nav():
    return HTML.split('<nav class="venue-nav"', 1)[1].split("</nav>", 1)[0]


def test_the_venues_are_memecoin_polymarket_hyperliquid_then_kalshi_and_intel():
    venues = re.findall(r'data-venue="([a-z]+)"', _venue_nav())
    assert venues == ["solana", "polymarket", "hyperliquid", "kalshi", "intel"]
    labels = re.findall(r'<span class="venue-label">([^<]+)</span>', _venue_nav())
    assert labels == ["Memecoin", "Polymarket", "Hyperliquid", "Kalshi", "Intel"]


def test_memecoin_is_the_active_venue_section_and_panel_in_the_markup():
    """No flash of Polymarket before the router runs."""
    assert re.findall(r'class="venue-item active" data-venue="([a-z]+)"', _venue_nav()) == ["solana"]
    section_nav = HTML.split('id="section-nav"', 1)[1].split("</nav>", 1)[0]
    assert re.findall(r'class="nav-item active" data-tab="([a-z-]+)"', section_nav) == ["memecoin"]
    assert re.findall(r'data-tab="([a-z-]+)"', section_nav)[:2] == ["memecoin", "wallet-map"]
    assert re.findall(r'<section id="tab-([a-z-]+)" class="tab-panel active"', HTML) == ["memecoin"]
    mobile = HTML.split('id="mobile-bottom-nav"', 1)[1].split("</nav>", 1)[0]
    assert "mob-nav-item active" not in mobile


def test_the_router_defaults_to_memecoin():
    """A URL with no tab, an unknown tab and the initial state all land on memecoin."""
    defaults = JS.split("DEFAULTS: {", 1)[1].split("}", 1)[0]
    assert "tab: 'memecoin'" in defaults
    route = JS.split("route: {", 1)[1].split("}", 1)[0]
    assert "tab: 'memecoin'" in route
    assert "if (!VALID_TABS.has(state.route.tab)) state.route.tab = 'memecoin';" in JS
    assert "const venue = TAB_VENUE[tabId] || 'solana';" in JS


def test_the_memecoin_tab_opens_on_its_live_view():
    assert "_workspace: 'live'," in JS
    assert "VENUE_HOME = { polymarket: 'operator', hyperliquid: 'hl-algo', intel: 'intelligence', solana: 'memecoin' }" in JS


def test_polymarket_stays_reachable_by_its_own_tab():
    assert "'operator'" in JS.split("const VALID_TABS = new Set([", 1)[1].split("]", 1)[0]
    assert re.search(r'data-tab="operator" data-venue="polymarket"><span class="nav-label">Operations', HTML)
