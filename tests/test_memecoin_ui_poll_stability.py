from pathlib import Path


APP_JS = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()


def test_memecoin_poll_defers_while_operator_is_scrolling():
    assert "this._lastScrollTs = Date.now()" in APP_JS
    assert "addEventListener('scroll', stamp, { passive: true })" in APP_JS
    poll_blocked = APP_JS.split("_pollBlocked() {", 1)[1].split("async _loadHealth()", 1)[0]
    assert "Date.now() - (this._lastScrollTs || 0) < 1400" in poll_blocked


def test_real_ticker_poll_is_fast_and_separate_from_full_cockpit_poll():
    memecoin = APP_JS.split("const memecoin = {", 1)[1].split("const xIntel =", 1)[0]
    assert "fetchJson('/api/memecoin/real/ticker'" in memecoin
    assert "this._tickerTimer = setInterval" in memecoin
    assert "}, 1000);" in memecoin
    assert "const cadence = 4000;" in memecoin


def test_wallet_sections_poll_at_30s_and_dedupe_inflight_loads():
    memecoin = APP_JS.split("const memecoin = {", 1)[1].split("const xIntel =", 1)[0]
    assert "WALLET_POLL_MS: 30000" in memecoin
    assert "this._sectionLoads.has(name)" in memecoin
    assert "this._sectionLoads.add(name)" in memecoin
    assert "this._sectionLoads.delete(name)" in memecoin
    assert "this._sectionReloads.add(name)" in memecoin
    assert "this._loadSection(name, true)" in memecoin


def test_paper_polling_is_bounded_and_has_one_inflight_guard():
    memecoin = APP_JS.split("const memecoin = {", 1)[1].split("const xIntel =", 1)[0]
    assert "PAPER_LAB_POLL_MS: 15000" in memecoin
    assert "PAPER_CLOSED_POLL_MS: 30000" in memecoin
    assert "_paperReportInFlight" in memecoin
    assert "this._loadSection(this._subtab)" in memecoin


def test_chain_filter_reloads_only_the_active_section():
    setter = APP_JS.split("_setChainFilter(v) {", 1)[1].split("_fillBadge", 1)[0]
    assert "this._loadSection(this._subtab, true)" in setter
    assert "this._loadOpen" not in setter
    assert "this._loadClosed" not in setter
