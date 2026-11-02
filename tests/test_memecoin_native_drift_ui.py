"""The native price drift panel's rendering (operator 2026-10-08): a stale or failed price is never
shown as live, an unread chain is never $0, and the window switch changes every figure."""
import pytest

from test_memecoin_ui_render_runtime import browser, memecoin_page  # noqa: F401  (fixtures)

T0 = 1_791_500_000


def _snap(*, age_s=20, price_ok=True, eth_price=2400.0, unread_chain=False):
    chains = [{"chain": "4663", "asset": "ETH", "native": 0.05, "wrapped": 0.10, "units": 0.15, "age_s": 5, "source": "recorder"},
              {"chain": "1", "asset": "ETH", "native": None if unread_chain else 0.0402, "wrapped": None if unread_chain else 0.0,
               "units": None if unread_chain else 0.0402, "age_s": None if unread_chain else 60, "source": None if unread_chain else "public"}]
    ch = {"1h": {"pct": -0.5, "usd": -1.0}, "24h": {"pct": -4.66, "usd": -16.53},
          "7d": {"pct": -9.93, "usd": -37.30}}
    eth = {"price_usd": eth_price, "units": 0.1902, "value_usd": None if eth_price is None else 0.1902 * eth_price,
           "last_price_usd": 2400.0, "last_price_age_s": 0.0 if eth_price else 300.0,
           "chains": chains, "change": ch if eth_price else {k: None for k in ch},
           "sparkline": {"24h": [[T0 - 86400, 2517.0], [T0 - 3600, 2450.0], [T0, 2400.0]], "1h": [], "7d": []}}
    return {"available": True, "age_s": age_s, "stale": age_s > 180 or not price_ok, "price_ok": price_ok,
            "partial": unread_chain, "assets": {"ETH": eth},
            "headline": {"1h": {"usd": -1.0}, "24h": {"usd": -16.53}, "7d": {"usd": -37.30}}}


def _render(page, snap, win=None):
    # As the page's own fetch does: the snapshot and the time it arrived.
    return page.evaluate("""([snap, win]) => { const m = window.__memecoin; m._drift = snap;
        m._driftRecvAt = Date.now(); m._driftWindow = win || undefined; return m._driftPanel(); }""", [snap, win])


import re

NOTICES = ("nf-banner", "nf-hero-note", "nf-partial", "nf-px-off\">price unavailable")


def _visible(html):
    return re.sub(r"<[^>]*>", " ", html)


def _no_notice(html):
    """Operator 2026-10-08: no warning or notice line anywhere in the panel."""
    assert not any(n in html for n in NOTICES), [n for n in NOTICES if n in html]


def _cell_tip(html, cls):
    """(shown text, tooltip) of the first element carrying class `cls`."""
    got = re.search(r'class="[^"]*\b%s\b[^"]*"(?: title="([^"]*)")?[^>]*>([^<]*)<' % cls, html)
    return (got.group(2).strip(), got.group(1)) if got else (None, None)


def test_a_fresh_snapshot_shows_live_figures(memecoin_page):
    page, errors = memecoin_page
    html = _render(page, _snap())
    assert "−$16.53" in html and "$2,400.00" in html
    assert "Robinhood" in html and "Ethereum" in html
    _no_notice(html)
    assert not errors


@pytest.mark.parametrize("failed_path", ["/api/memecoin/real/status", "/api/memecoin/real/positions"])
@pytest.mark.parametrize("kind", ["fresh", "zero", "stale"])
def test_a_cockpit_failure_still_refreshes_drift_without_duplicate_reads(memecoin_page, failed_path, kind):
    page, errors = memecoin_page
    snap = _snap(age_s=300, eth_price=None) if kind == "stale" else _snap()
    if kind == "zero":
        snap["headline"]["24h"]["usd"] = 0
        snap["assets"]["ETH"].update(units=0, value_usd=0)
        snap["assets"]["ETH"]["change"]["24h"] = {"usd": 0, "pct": 0}
        for chain in snap["assets"]["ETH"]["chains"]:
            chain.update(native=0, wrapped=0, units=0)

    result = page.evaluate("""async ([snap, failedPath]) => {
        document.body.insertAdjacentHTML('beforeend', '<div id="mc-native-drift"></div>');
        const m = window.__memecoin, calls = [];
        let finishDrift;
        window.fetch = async input => {
            const path = new URL(String(input), 'http://sniper.test').pathname;
            calls.push(path);
            if (path === '/api/memecoin/real/native-drift') {
                await new Promise(resolve => { finishDrift = resolve; });
                return new Response(JSON.stringify(snap), {status: 200});
            }
            return new Response(JSON.stringify({}), {status: path === failedPath ? 503 : 200});
        };
        await Promise.all([m._loadReal(), m._loadReal()]);
        if (finishDrift) finishDrift();
        await m._driftLoad;
        await m._loadReal();
        return {calls, failed: m._realReadError,
            html: document.getElementById('mc-native-drift').innerHTML};
    }""", [snap, failed_path])

    assert result["failed"] is True
    assert result["calls"].count("/api/memecoin/real/native-drift") == 1
    html = result["html"]
    if kind == "stale":
        shown, tip = _cell_tip(html, "nf-px")
        assert shown == "—" and "5 min" in tip and "$2,400.00" not in html
    elif kind == "zero":
        assert _cell_tip(html, "nf-hero-num")[0] == "$0.00"
        assert _cell_tip(html, "nf-float")[0] == "$0.00"
        assert "0.0000 ETH" in html and "0.00%" in html
    else:
        assert "−$16.53" in html and "$2,400.00" in html
    _no_notice(html)
    assert not errors


def test_an_old_snapshot_shows_a_dash_with_its_age_as_a_tooltip(memecoin_page):
    page, _errors = memecoin_page
    html = _render(page, _snap(age_s=300, eth_price=None))
    _no_notice(html)
    shown, tip = _cell_tip(html, "nf-px")
    assert shown == "—" and tip and "5 min" in tip and "$2,400.00" not in html


def test_a_failed_price_is_a_dash_with_the_reason(memecoin_page):
    page, _errors = memecoin_page
    html = _render(page, _snap(price_ok=False, eth_price=None))
    _no_notice(html)
    shown, tip = _cell_tip(html, "nf-px")
    assert shown == "—" and tip and "$2,400.00" not in html


def test_an_unread_chain_is_a_dash_never_zero_and_the_float_total_is_unknown(memecoin_page):
    page, _errors = memecoin_page
    html = _render(page, _snap(unread_chain=True))
    _no_notice(html)
    assert 'title="not read"' in html and ">unread<" not in html
    shown, tip = _cell_tip(html, "nf-float")
    assert shown == "—" and "Ethereum" in tip


def test_no_snapshot_yet_says_when_it_arrives(memecoin_page):
    page, _errors = memecoin_page
    html = _render(page, {"available": False, "stale": True, "age_s": None})
    assert "No drift data yet." in html


def test_the_window_switch_changes_every_figure(memecoin_page):
    page, _errors = memecoin_page
    html = _render(page, _snap(), win="7d")
    assert "−$37.30" in html and "−9.93%" in html and "7 days" in html and "−$16.53" not in html
    # Operator 2026-10-08: no baseline window and no reset button.
    assert "Since baseline" not in html and "Reset baseline" not in html and 'data-action="drift-reset"' not in html


def test_a_partial_headline_is_a_dash_naming_the_missing_asset(memecoin_page):
    page, _errors = memecoin_page
    snap = _snap()
    snap["headline"]["24h"] = {"usd": -16.53, "partial": True, "missing": ["BNB"]}
    html = _render(page, snap)
    _no_notice(html)
    shown, tip = _cell_tip(html, "nf-hero-num")
    assert shown == "—" and "BNB" in tip
    assert _cell_tip(_render(page, _snap()), "nf-hero-num")[0] == "−$16.53"


def test_the_backfill_note_is_a_tooltip_only(memecoin_page):
    page, _errors = memecoin_page
    snap = _snap()
    snap["ts"] = T0
    snap["assets"]["ETH"]["units_backfilled_from"] = T0 - 3600              # inside the 24h window
    html = _render(page, snap)
    _no_notice(html)
    assert "balances recorded from 10-08 21:53 UTC" in html and "balances recorded" not in _visible(html)


# SF09 (engineering review 2026-10-08): a cached snapshot ages on the page's clock, and a failed
# refresh never leaves an old price presented as live; it is a dash with a tooltip, not a banner.

def _render_received(page, snap, received_ago_s, *, failed):
    return page.evaluate("""([snap, ago, failed]) => { const m = window.__memecoin; m._drift = snap;
        m._driftRecvAt = Date.now() - ago * 1000; m._driftFailed = failed; m._driftWindow = undefined;
        return m._driftPanel(); }""", [snap, received_ago_s, failed])


def test_review_sf09_a_failed_refresh_ages_the_cached_snapshot_and_takes_its_price_off_live(memecoin_page):
    page, _errors = memecoin_page
    html = _render_received(page, _snap(age_s=20), 3600, failed=True)
    _no_notice(html)
    assert "$2,400.00" not in html and "−$16.53" not in html and "20 s ago" not in html
    shown, tip = _cell_tip(html, "nf-px")
    assert shown == "—" and "60 min" in tip


def test_the_live_bound_is_on_the_effective_age(memecoin_page):
    page, _errors = memecoin_page
    assert "$2,400.00" in _render_received(page, _snap(age_s=20), 159, failed=False)       # 179 s
    assert "$2,400.00" not in _render_received(page, _snap(age_s=20), 161, failed=False)   # 181 s


def test_a_recovered_refresh_is_live_again(memecoin_page):
    page, _errors = memecoin_page
    _render_received(page, _snap(age_s=20), 3600, failed=True)
    html = _render_received(page, _snap(age_s=20), 0, failed=False)
    assert "$2,400.00" in html
    _no_notice(html)


def test_the_backfill_tooltip_names_the_latest_first_balance(memecoin_page):
    # Gamma 2026-10-08: an asset whose rows start later is backfilled past the earliest start.
    page, _errors = memecoin_page
    snap = _snap()
    snap["ts"] = T0
    snap["assets"]["ETH"]["units_backfilled_from"] = T0 - 3600
    snap["assets"]["BNB"] = dict(snap["assets"]["ETH"], units_backfilled_from=T0 - 1800, chains=[])
    html = _render(page, snap)
    assert "balances recorded from 10-08 22:23 UTC" in html


def test_a_snapshot_without_its_receipt_time_is_never_live(memecoin_page):
    # Gamma LOW on 68c8f1e07 (rule 4): an unknown arrival time is an unknown age, never 0 s.
    page, _errors = memecoin_page
    html = page.evaluate("""snap => { const m = window.__memecoin; m._drift = snap; m._driftRecvAt = undefined;
        m._driftFailed = true; return m._driftPanel(); }""", _snap(age_s=20))
    _no_notice(html)
    assert "$2,400.00" not in html and "20 s ago" not in html
