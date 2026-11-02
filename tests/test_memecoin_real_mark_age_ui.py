import json
import shutil
import subprocess
from pathlib import Path


APP = Path("static/app.js").read_text()
NOW = "2026-08-19T12:00:00Z"


def _node(script):
    node = shutil.which("node")
    assert node is not None, "node must test the funded cockpit JavaScript"
    helpers = (APP[APP.index('    _operatorPrice('):APP.index('    _realTable(')]
               + APP[APP.index('    _supplyOf('):APP.index('    _fmtAgo(')])
    money = APP[APP.index('    _money(v) {'):APP.index('    _spanSeconds(')]
    script = script.replace('const view = {', 'const view = {' + helpers + money + '_renderAttention() {},', 1)
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60, check=True
    )
    return json.loads(result.stdout)


def _mark_tags(cases):
    start = APP.index("    _markAgeS(p) {")
    end = APP.index("    _markTagHtml(p) {")
    helper = APP[start:end]
    script = (
        f"const NOW = Date.parse({json.dumps(NOW)});\n"
        "const view = { MARK_STALE_S: 15, _now() { return NOW; },\n"
        f"{helper}}};\n"
        f"console.log(JSON.stringify({json.dumps(cases)}.map(p => view._markTag(p))));"
    )
    return _node(script)


def test_mark_tag_names_a_fresh_exact_route():
    tags = _mark_tags([
        {"mark_basis": "measured_route", "mark_observed_at": "2026-08-19T11:59:58Z"}
    ])

    assert tags == [{
        "txt": "2s · route",
        "stale": False,
        "title": "price is 2s old, from a real whole-bag route quote",
    }]


def test_mark_tag_flags_the_observed_46_second_case():
    tags = _mark_tags([
        {"mark_basis": "measured_route", "mark_observed_at": "2026-08-19T11:59:14.3Z"}
    ])

    assert tags[0]["txt"] == "46s · route · STALE"
    assert tags[0]["stale"] is True


def test_mark_tag_keeps_model_and_mid_distinct():
    tags = _mark_tags([
        {"mark_basis": "modeled_slippage", "mark_observed_at": "2026-08-19T11:59:59Z"},
        {"mark_basis": "mid", "mark_observed_at": "2026-08-19T11:59:59Z"},
    ])

    assert [tag["txt"] for tag in tags] == ["1s · model", "1s · mid"]


def test_mark_tag_treats_an_unknown_time_as_stale():
    tags = _mark_tags([
        {"mark_basis": "measured_route", "mark_observed_at": None},
        {"mark_basis": "mid", "mark_observed_at": "bad"},
    ])

    assert [tag["txt"] for tag in tags] == ["age unknown · route", "age unknown · mid"]
    assert all(tag["stale"] for tag in tags)


def test_ticker_dispatches_a_push_to_a_table_row():
    start = APP.index("    _applyRealTicker(payload) {")
    end = APP.index("    _ageRealMarks() {")
    helper = APP[start:end]
    script = f"""
const row = {{}};
let applied = null;
const el = {{
  querySelectorAll() {{ return []; }},
  querySelector(selector) {{
    if (selector === '[data-real-trow="7"]') return row;
    return null;
  }}
}};
global.document = {{ getElementById(id) {{ return id === 'mc-real' ? el : null; }} }};
const view = {{
  _realPositions: [{{ id: 7, last_quote_price: 1 }}],
  _applyRealTableRow(target, position) {{ applied = {{ target: target === row, position }}; }},
{helper}
}};
view._applyRealTicker({{ positions: [{{
  id: 7,
  last_quote_price: 2,
  last_quote_ts: '2026-08-19T12:00:00Z',
  mark_basis: 'mid',
  mark_observed_at: '2026-08-19T12:00:00Z'
}}] }});
console.log(JSON.stringify(applied));
"""

    applied = _node(script)

    assert applied["target"] is True
    assert applied["position"]["last_quote_price"] == 2
    assert applied["position"]["mark_basis"] == "mid"
    assert applied["position"]["mark_observed_at"] == "2026-08-19T12:00:00Z"


def test_real_card_range_uses_the_same_sell_price_as_total_pnl():
    # d13d1126 (2026-09-23): the ticker rows are the range bar plus the facts line; extract all three.
    start = APP.index("    _realRangeBar(p) {")
    end = APP.index("    _cardSig(p) {")
    helper = APP[start:end]
    script = f"""
const esc = x => String(x);
const chainOf = () => 'robin';
global.window = {{ mcRange: require(process.cwd() + '/static/mc-range.js') }};
const view = {{
  _mergeLastKnown(p) {{ return p; }},
  _markDotHtml() {{ return ''; }},
  _markLabelHtml() {{ return ''; }},
  _quoteAge() {{ return '2s'; }},
  _n(n, d) {{ return Number(n).toFixed(d); }},
  _fmtMcapShort(v) {{ return v == null ? '' : '$' + Number(v).toFixed(2); }},
  _durTxt(s) {{ return Number(s).toFixed(0) + 's'; }},
  _usd2(v) {{ return '$' + Number(v).toFixed(2); }},
  _usd(n) {{ return '$' + Number(n).toFixed(2); }},
  _money(n) {{ return '$' + Number(n).toFixed(2); }},
  _col() {{ return ''; }},
{helper}
}};
const position = {{
  id: 7, mint: '0xtoken', network_id: '4663', entry_notional: 100,
  entry_price: 0.000379, market_mid_price: 0.000400,
  market_mid_at: '2026-08-19T12:00:00Z', selected_exit_price: 0.000382032,
  selected_exit_basis: 'measured_route', total_pnl_usd: 1.46, total_pnl_pct: 0.8,
  remaining_value_usd: 176.46, market_cap: 240000, chain_stop_pct: 35, max_gain_pct: 0
}};
console.log(JSON.stringify({{
  current: view._realTickerRows(position),
  missing: view._realRangeBar({{...position, selected_exit_price: null}}),
  belowStop: view._realRangeBar({{...position, selected_exit_price: 0.000200}}),
}}));
"""

    rendered = _node(script)
    row = rendered["current"]

    # The headline and range marker use the selected exit; the market mark is higher.
    # 2026-09-28 (operator): prices read as market caps once the supply is known (600M here,
    # from the market cap over the market mark). The sell value is still the SELECTED exit
    # (0.000382032 x 600M), not the higher market mark (0.0004 x 600M = $240000).
    assert "entry $227400.00" in row
    assert "sell value $229219.20" in row
    assert "spot cap $240000.00" in row
    assert "sell equivalent $229219.20 · +0.8%" in row
    assert "no sell value" in rendered["missing"]
    assert "mc-range-dot" not in rendered["missing"]
    assert "sell price past SL" in rendered["belowStop"]
    assert "stop reached" not in rendered["belowStop"]


def test_ticker_row_separates_mark_age_and_keeps_entry_volume_visible():
    # d13d1126 (2026-09-23): the ticker rows are the range bar plus the facts line; extract all three.
    start = APP.index("    _realRangeBar(p) {")
    end = APP.index("    _cardSig(p) {")
    helper = APP[start:end]
    script = f"""
const esc = x => String(x);
const chainOf = () => 'robin';
global.window = {{ mcRange: require(process.cwd() + '/static/mc-range.js') }};
const view = {{
  _mergeLastKnown(p) {{ return p; }},
  _markDotHtml() {{ return ''; }},
  _markLabelHtml() {{ return '<span>1s · route</span>'; }},
  _quoteAge() {{ return '2s'; }},
  _n(n, d) {{ return Number(n).toFixed(d); }},
  _fmtMcapShort(v) {{ return v == null ? '' : '$' + Number(v).toFixed(2); }},
  _durTxt(s) {{ return Number(s).toFixed(0) + 's'; }},
  _usd2(v) {{ return '$' + Number(v).toFixed(2); }},
  _usd(n) {{ return n == null ? '—' : '$' + Number(n).toFixed(2); }},
  _money(n) {{ return '$' + Number(n).toFixed(2); }},
  _col() {{ return ''; }},
{helper}
}};
console.log(JSON.stringify(view._realTickerRows({{
  id: 7, mint: '0xtoken', network_id: '4663', entry_notional: 100,
  entry_price: 0.000379, market_mid_price: 0.000400,
  market_mid_at: '2026-08-19T12:00:00Z', selected_exit_price: 0.000300,
  selected_exit_basis: 'measured_route', market_mid_basis: 'route', pnl_pct: -20, max_gain_pct: 0,
  market_cap: 228242, liquidity: 119111, volume_24h: null,
  entry_contract: {{classification: {{facts: {{volume_m5: 610.4, volume_h1: 18323}}}}}}
}})));
"""

    row = _node(script)

    assert "Route quote, no market mark" in row
    assert "5m vol at entry" in row
    assert "$610.40" in row
    assert "1h vol at entry" in row
    assert "$18323.00" in row

def test_lagging_entry_volume_ticker_row_separates_mark_age_and_keeps_entry_volume_visible():
    # d13d1126 (2026-09-23): the ticker rows are the range bar plus the facts line; extract all three.
    start = APP.index("    _realRangeBar(p) {")
    end = APP.index("    _cardSig(p) {")
    helper = APP[start:end]
    script = f"""
const esc = x => String(x);
const chainOf = () => 'robin';
global.window = {{ mcRange: require(process.cwd() + '/static/mc-range.js') }};
const view = {{
  _mergeLastKnown(p) {{ return p; }},
  _markDotHtml() {{ return ''; }},
  _markLabelHtml() {{ return '<span>1s · route</span>'; }},
  _quoteAge() {{ return '2s'; }},
  _n(n, d) {{ return Number(n).toFixed(d); }},
  _fmtMcapShort(v) {{ return v == null ? '' : '$' + Number(v).toFixed(2); }},
  _durTxt(s) {{ return Number(s).toFixed(0) + 's'; }},
  _usd2(v) {{ return '$' + Number(v).toFixed(2); }},
  _usd(n) {{ return n == null ? '—' : '$' + Number(n).toFixed(2); }},
  _money(n) {{ return '$' + Number(n).toFixed(2); }},
  _col() {{ return ''; }},
{helper}
}};
console.log(JSON.stringify(view._realTickerRows({{
  id: 7, mint: '0xtoken', network_id: '4663', entry_notional: 100,
  entry_price: 0.000379, market_mid_price: 0.000400,
  market_mid_at: '2026-08-19T12:00:00Z', selected_exit_price: 0.000300,
  selected_exit_basis: 'measured_route', pnl_pct: -20, max_gain_pct: 0,
  market_cap: 228242, liquidity: 119111, volume_24h: null,
  their_size_usd: 2508.70, entry_contract: {{classification: {{facts: {{volume_m5: 11.41, volume_h1: 2031.55}}}}}}
}})));
"""

    row = _node(script)

    # LOOT 2026-09-17 09:43Z: the provider snapshot said $11.41 of 5-minute volume while the copied
    # trader had just bought $2,508.70. The card must say the number lags, not show a dead coin.
    assert "5m vol at entry" in row
    assert "lagging" in row
    assert "$2508.70" in row



def _stale_lines(cases):
    start = APP.index("    _staleSellLine(p) {")
    end = APP.index("    _renderAttention() {")
    script = (
        f"const NOW = Date.parse({json.dumps(NOW)});\n"
        "const esc = x => String(x);\n"
        "const view = { MARK_STALE_S: 15, _now() { return NOW; },\n"
        f"{APP[start:end]}}};\n"
        f"console.log(JSON.stringify({json.dumps(cases)}.map(p => view._staleSellLine(p))));"
    )
    return json.loads(subprocess.run([shutil.which("node"), "-e", script], capture_output=True,
                                      text=True, timeout=60, check=True).stdout)


def test_stale_banner_names_the_cause_and_every_clock():
    # 2026-09-17: the operator saw "1 position with an old or unavailable sell value basis" and
    # nothing said which clock was old. The line now names the basis and the three ages.
    lines = _stale_lines([
        {"ticker": "SME", "selected_exit_basis": "measured_route_stale", "selected_exit_price": 1.0,
         "selected_exit_at": "2026-08-19T11:54:00Z", "market_mid_at": "2026-08-19T11:59:58Z",
         "runner_decision_at": "2026-08-19T11:59:59Z"},
        {"ticker": "LOOT", "selected_exit_basis": None, "selected_exit_price": None,
         "selected_exit_at": None, "market_mid_at": None, "runner_decision_at": None},
    ])
    assert lines[0] == "SME · stale route quote · sell value 360s old · market 2s · runner 1s"
    assert lines[1] == "LOOT · no sell value · sell value unknown · market unknown · runner unknown"


def _attention_lines(state, positions):
    start = APP.index("    _staleSellLine(p) {")
    end = APP.index("    _renderAttention() {")
    helper = APP[APP.index("    _staleAttentionLines() {"):APP.index("    _staleSellLine(p) {")]
    script = (
        f"const NOW = Date.parse({json.dumps(NOW)});\n"
        "const esc = x => String(x);\n"
        "const view = { MARK_STALE_S: 15, _now() { return NOW; },\n"
        f"_realReadError: {json.dumps(state['read_error'])}, _realReadAt: NOW - 90000,\n"
        f"_realStreamHealthy() {{ return {json.dumps(state['stream'])}; }},\n"
        f"_realPositions: {json.dumps(positions)},\n"
        f"{helper}{APP[start:end]}}};\n"
        "console.log(JSON.stringify(view._staleAttentionLines()));"
    )
    return json.loads(subprocess.run([shutil.which("node"), "-e", script], capture_output=True,
                                     text=True, timeout=60, check=True).stdout)


OLD = {"ticker": "DOGGO", "selected_exit_basis": "measured_route", "selected_exit_price": 1.0,
       "selected_exit_at": "2026-08-19T11:59:10Z", "market_mid_at": "2026-08-19T11:58:44Z",
       "runner_decision_at": "2026-08-19T11:59:10Z"}


def test_a_lost_connection_is_named_instead_of_blaming_the_position():
    # DOGGO 2026-09-17 13:05Z: the phone's tunnel dropped the live stream, the page kept old data,
    # and the banner read "route quote · sell value 50s old" while the runner quoted every 2.5 s.
    lines = _attention_lines({"read_error": True, "stream": False}, [OLD])
    assert lines == ["Live data is not updating (last successful read 90s ago). Position ages are from that read."]


def test_stale_lines_still_show_when_the_connection_is_live():
    lines = _attention_lines({"read_error": False, "stream": True}, [OLD])
    assert lines and lines[0].startswith("DOGGO · route quote · sell value 50s old")
