"""Venue placement for Hyperliquid microstructure and Polymarket crypto-5m surfaces."""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_polymarket_5m_maker_tab_archived_and_hyperliquid_edge_desk_wired():
    # crypto5m maker fleet ARCHIVED 2026-07-08 — no realizable edge (naive-fill fantasy, confirmed by the
    # cancel-aware queue replay). The "5m Binary" tab + maker desk are removed from the dashboard so the
    # failure isn't a visual reminder. The Hyperliquid microstructure surface is unaffected.
    html = (ROOT / "templates" / "index.html").read_text()
    js = (ROOT / "static" / "app.js").read_text()

    # 5m Binary / crypto5m maker tab is GONE
    assert 'data-tab="algo" data-venue="polymarket"' not in html
    assert 'id="crypto5m-maker-desk"' not in html
    assert "5m Binary maker tab ARCHIVED" in html          # archive marker left in place
    assert "ARCHIVED 2026-07-08" in js                      # 'algo' dropped from VALID_TABS

    # Hyperliquid now renders current local paper-research truth.
    assert 'data-tab="hl-algo" data-venue="hyperliquid"><span class="nav-label">Edge Desk</span>' in html
    assert 'id="hyperlive-board"' in html
    assert 'id="btc-algo-board"' in html
    assert "Hyperliquid tab EMPTY 2026-07-08" not in html
    assert "No active Hyperliquid lane" not in html
    assert "HL Edge Desk" in html
    assert 'id="hl-edge-verdict"' in html
    assert 'id="hl-edge-metrics"' in html
    assert 'id="hl-edge-rule"' in html
    assert 'id="hl-edge-runway"' not in html
    assert 'id="hl-edge-summary"' not in html
    assert "'hl-edge-runway'" not in js
    assert "'hl-edge-summary'" not in js
    assert 'id="hl-edge-audit"' in html
    assert 'id="hl-edge-audit-body"' in html
    assert 'id="hl-edge-updated"' in html
    assert "'hl-algo': 'hyperliquid'" in js
    assert "fetchJson('/api/hyperlive/edge-desk'" in js
    assert "cache: 'no-store'" in js
    assert "EDGE DESK DATA OUTDATED" in js
    assert "payload.schema_version" in js
    assert "_renderEdgeDesk" in js
    assert "evidence_debt" in js
    assert "_renderEdgeMetrics" in js
    assert "_renderEdgeRule" in js
    assert "LIVE EDGE PROOF" not in js
    assert "_edgeCollection" in js
    assert "FORWARD TEST RUNNING" in js
    assert "PROMOTION PAUSED" in js
    assert "ALT-SHORT PAUSED" not in js
    assert "HISTORY COVERAGE" in js
    assert "SOURCE TIMING" in js
    assert "source.source_events" in js
    assert "CAPACITY SCOPE" in js
    assert "FORWARD PROOF" in js
    primary = js.split("_renderVerdict(d, staleDisplay)", 1)[1].split("_renderAuditDetail(d)", 1)[0]
    assert "No qualifying setup yet" in primary
    assert "complete closes" in primary
    assert "COPY CONTROL · TOTAL BOOK SNAPSHOT" not in primary
    assert "WHAT CHANGES THE VERDICT" not in primary
    assert "if (value == null) return '—';" in js
    assert "Why not promoted" in html
    assert "hyperliquid: 'hl-algo'" in js
    assert "data-hl-edge-action" not in html
    assert 'id="hl-edge-status"' not in html
    assert 'id="hl-edge-positions"' not in html


def test_hyperliquid_analyzer_is_reachable_and_copy_controls_remain_wired():
    html = (ROOT / "templates" / "index.html").read_text()
    js = (ROOT / "static" / "app.js").read_text()

    assert 'data-tab="hl-analyzer" data-venue="hyperliquid"><span class="nav-label">Wallet Research</span>' in html
    assert 'data-tab="hl-analyzer" data-venue="hyperliquid" href="#" aria-label="HL Analyzer"' in html
    assert 'data-tab="hl-algo" data-venue="hyperliquid" href="#" aria-label="HL Edge Desk"' in html
    assert "'hl-analyzer'" in js
    assert "'hl-analyzer': 'hyperliquid'" in js
    assert "hyperliquid: 'hl-analyzer'" not in js
    assert "else if (tabId === 'hl-analyzer') hlAnalyzer.load(sig)" in js
    assert "fetchJson('/api/hyperlive/analyzer'" in js
    assert "fetchJson('/api/hyperlive/copy/set'" in js


def test_hyperliquid_analyzer_names_watch_fallback_copy_explicitly():
    js = (ROOT / "static" / "app.js").read_text()

    assert "watch_fallback" in js
    assert "Watch fallback paper copy" in js
    assert "No proven wallets" in js
    assert "runner offline" in js


def test_hyperliquid_analyzer_splits_copy_book_tabs_and_cleaner_controls():
    html = (ROOT / "templates" / "index.html").read_text()
    js = (ROOT / "static" / "app.js").read_text()
    css = (ROOT / "static" / "styles.css").read_text()

    assert 'data-hl-subtab="analyzer"' in html
    assert 'data-hl-subtab="open"' in html
    assert 'data-hl-subtab="closed"' in html
    assert 'data-hl-subtab="cleaner"' in html
    assert 'id="hl-copy-open-panel"' in html
    assert 'id="hl-copy-closed-panel"' in html
    assert 'id="hl-cleaner-panel"' in html
    assert 'id="hl-analyzer-search"' in html
    assert 'id="hl-analyzer-view"' in html
    assert '<option value="priority">Priority wallets</option>' in html
    assert '<option value="watchlist">Active watchlist</option>' in html
    assert 'id="hl-analyzer-verdict-filter"' in html
    assert 'id="hl-analyzer-prev"' in html
    assert 'id="hl-analyzer-next"' in html
    assert 'id="hl-analyzer-page"' in html
    assert 'id="hl-cleaner-wallet"' in html
    assert "fetchJson('/api/hyperlive/copy?include_open=0'" in js
    assert "_renderCopyPositionTab" in js
    assert "Classified open ledger" in js
    assert "Closed paper positions" in js
    assert "new URLSearchParams" in js
    assert "params.set('page_size', String(this._pageSize))" in js
    assert "_pageSize: 25" in js
    assert "_view: 'priority'" in js
    assert "priority ${counts.priority || 0}" in js
    assert "_renderEdgeLegacyArchive" in js
    assert "Legacy rows stay in the research ledger" in js
    assert "pick one to mirror 24/7" not in html
    assert "fetchJson('/api/hyperlive/cleaner/manual'" in js
    assert "fetchJson('/api/hyperlive/cleaner/system'" in js
    # The destructive cleaner actions must still be confirmed — but via the in-app modal, not the
    # native dialog. 452f02f1 ("Phase 4 — in-app confirm modal") replaced window.confirm with
    # confirmModal() repo-wide; this assertion still pinned the old API and so failed by design.
    assert "confirmModal(" in js
    assert "confirm: true" in js
    assert "body && body.reason" in js
    # Cache-busted app.js must be present — but do NOT pin the version. This asserted v=158 while
    # the template shipped v=162: the number is bumped on every UI change, so a hardcoded pin turns
    # an unrelated frontend edit into a red suite (which is exactly what it did). Assert the
    # contract (a versioned app.js tag), not today's value.
    assert re.search(
        r'/static/app\.js\?v=(?:\d+|\{\{\s*asset_v\.app_js\s*\}\})',
        html,
    ), "versioned app.js script tag missing"
    assert '<script src="/static/app.js"' not in html
    assert "hl-analyzer-console" in css
    assert "max-height: calc(100vh - 260px)" in css
    assert "copy_tier" in js
    assert "['operator', 'signals', 'positions', 'wallets', 'strategies', 'hl-analyzer', 'intelligence']" in js
    assert "else if (tabId === 'hl-analyzer') hlAnalyzer.load()" in js
    assert "else if (tab === 'hl-analyzer' && typeof hlAnalyzer !== 'undefined') hlAnalyzer.load()" in js
