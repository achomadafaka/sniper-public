"""The funded cockpit's money formatters, run under node against static/app.js.

Same harness as tests/test_memecoin_real_mark_age_ui.py: slice the helper out of the
shipped source so the test cannot drift from what the browser runs.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path


APP = Path("static/app.js").read_text()


def _fmt(cases):
    node = shutil.which("node")
    assert node is not None, "node must test the funded cockpit JavaScript"
    start = APP.index("    _money(v) {")
    end = APP.index("    _pct(v, d = 0) {")
    script = (
        "const view = {\n" + APP[start:end] + "};\n"
        f"console.log(JSON.stringify({json.dumps(cases)}"
        ".map(v => [view._usd2(v), view._money(v)])));"
    )
    # pin the locale so the decimal separator is not the CI host's
    env = dict(os.environ, LC_ALL="en_US.UTF-8")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         timeout=60, check=True, env=env)
    return json.loads(out.stdout)


def _acct_panel(accounting):
    node = shutil.which("node")
    assert node is not None, "node must test the funded cockpit JavaScript"
    start = APP.index("    _acctPanel() {")
    end = APP.index("    _realBuyDraft() {", start)
    script = (
        "const esc = v => String(v); const chainLabel = v => v; "
        "const chainOf = (_mint, network) => network; const view = {\n"
        f"_acct: {json.dumps(accounting)}, _acctTs: Date.now(),\n"
        "_usd2(v) { return v == null ? '—' : '$' + Number(v).toFixed(2); },\n"
        "_money(v) { return v == null ? '—' : (v >= 0 ? '+' : '-') + '$' + Math.abs(Number(v)).toFixed(2); },\n"
        "_n(v, d) { return Number(v).toFixed(d); },\n"
        "_col() { return '#fff'; }, _netLabel(n) { return String(n); },\n"
        + APP[start:end]
        + "};\nconsole.log(view._acctPanel());"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         timeout=60, check=True)
    return out.stdout


def test_a_balance_always_carries_cents_and_no_sign():
    # `_n` sets only a MAXIMUM, so the old `'$' + _n(v, 2)` rendered `$0` beside `$45.47`
    # on one line. A balance is unsigned: `+$45.47` for cash is wrong.
    assert _fmt([0, 45.47, 0.5, 1234.5]) == [
        ["$0.00", "+$0.00"],
        ["$45.47", "+$45.47"],
        ["$0.50", "+$0.50"],
        ["$1,234.50", "+$1,234.50"],
    ]


def test_a_missing_value_never_grows_a_sign():
    # `null >= 0` is TRUE in JavaScript, so the old `v >= 0 ? '+' : ''` on unrealized
    # rendered `+—`. Both formatters must return the bare em dash.
    assert _fmt([None]) == [["—", "—"]]


def test_the_cockpit_routes_every_balance_through_the_shared_formatter():
    # Guards the fix at the call sites: a balance rebuilt inline brings `$0` back.
    load = APP[APP.index('    async _loadReal('):APP.index('    _mcSetBusy(')]
    assert 'this._acctPanel()' in load

    panel = APP[APP.index("    _acctPanel() {"):APP.index("    _toggleInlineChart(")]
    assert ": this._usd2(v);" in panel          # the `?`-on-read-failure wrapper
    assert "this._money(b.unrealized)" in panel  # signed PnL keeps the signed formatter


def test_the_cockpit_prices_native_gas_in_each_accounting_view():
    panel = _acct_panel({
        "by_network": {"4663": {
            "cash": 204.01, "gas": 0.0424, "gas_usd": 103.61,
            "deployed": 0, "open_n": 0, "reserved_n": 0,
            "unrealized": 0, "unquotable_n": 0, "equity": 307.62,
            "clip_next": 102.00,
        }},
        "totals": {
            "cash": 204.01, "gas_usd": 103.61, "deployed": 0,
            "unrealized": 0, "equity": 307.62,
        },
        "allowlist_by_chain": {}, "slots": {"used": 0, "max": 2},
        "robot_realized_today": 0, "robot_realized_total": 0,
        "manual_realized_today": 0, "manual_realized_total": 0,
        "realized_today": 0, "realized_total": 0,
    })
    assert panel.count("$103.61") == 2
    assert "0.042400 ETH available" in panel


def test_the_cockpit_shows_the_active_entry_gas_reserve():
    panel = _acct_panel({
        "by_network": {"4663": {
            "cash": 234.83, "gas": 0.002015, "gas_usd": 4.93,
            "gas_required": 0.00021, "gas_headroom": 0.001805, "gas_ok": True,
            "deployed": 0, "open_n": 0, "reserved_n": 0,
            "unrealized": 0, "unquotable_n": 0, "equity": 239.76,
            "clip_next": 117.42,
        }},
        "totals": {
            "cash": 234.83, "gas_usd": 4.93, "deployed": 0,
            "unrealized": 0, "equity": 239.76,
        },
        "allowlist_by_chain": {}, "slots": {"used": 0, "max": 2},
        "robot_realized_today": 0, "robot_realized_total": 0,
        "manual_realized_today": 0, "manual_realized_total": 0,
        "realized_today": 0, "realized_total": 0,
    })
    assert "0.002015 ETH available · 0.000210 ETH required" in panel
    assert "OK" in panel


def test_accounting_separates_open_cost_from_an_in_flight_reservation():
    panel = _acct_panel({
        "by_network": {"4663": {
            "cash": 300.00, "gas": 0.01, "gas_usd": 20.00,
            "deployed": 117.98, "reserved": 117.98,
            "open_n": 0, "reserved_n": 1, "unrealized": 0,
            "unquotable_n": 0, "equity": 320.00, "clip_next": 0,
        }},
        "totals": {
            "cash": 300.00, "gas_usd": 20.00, "deployed": 117.98,
            "unrealized": 0, "equity": 320.00,
        },
        "allowlist_by_chain": {}, "slots": {"used": 1, "max": 2},
        "robot_realized_today": 0, "robot_realized_total": 0,
        "manual_realized_today": 0, "manual_realized_total": 0,
        "realized_today": 0, "realized_total": 0,
    })
    assert "open cost <b>$0.00</b> · in flight <b>$117.98</b> (0 open +1 resv)" in panel


def _money_hero(accounting):
    node = shutil.which("node")
    assert node is not None, "node must test the funded cockpit JavaScript"
    start = APP.index("    _moneyHero(today) {")
    end = APP.index("    _renderRecentTrades() {", start)
    script = (
        "const esc = v => String(v); const chainLabel = v => v; "
        "const chainOf = (_mint, network) => network; const view = {\n"
        f"_acct: {json.dumps(accounting)},\n"
        "_money(v) { return String(v); }, _col() { return '#fff'; },\n"
        + APP[start:end]
        + "};\nconsole.log(view._moneyHero(0));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         timeout=60, check=True, env=dict(os.environ, LC_ALL="en_US.UTF-8"))
    return out.stdout


def test_a_chain_holding_only_its_gas_asset_still_shows_its_equity():
    """2026-09-26: BSC held 0.1912 BNB (~$148) and $0.003 of its WBNB quote token. The chip row
    filtered on quote cash >= $1, so the BSC chip vanished while the total still counted it."""
    out = _money_hero({"totals": {"equity": 600.0}, "by_network": {
        "solana": {"cash": 400.0, "equity": 452.0, "gas_ok": True},
        "56": {"cash": 0.0035, "equity": 147.82, "gas_ok": True},
        "8453": {"cash": 0.0, "equity": 0.21, "gas_ok": True}}})
    assert "56 $148" in out and "solana $452" in out
    assert "8453" not in out                       # dust under $1 stays hidden
