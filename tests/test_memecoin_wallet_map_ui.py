"""Wallet Map herders page: the pure text helpers run under node against real payload shapes."""
import json
import subprocess
from pathlib import Path


def _core(expr):
    js = ("const C = require('./static/wallet_map.js');"
          "process.stdout.write(JSON.stringify(%s));" % expr)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_names_and_html_are_escaped_and_long_addresses_shortened():
    assert _core("C.esc('<b>&\"')") == "&lt;b&gt;&amp;&quot;"
    assert _core("C.who('J2QQwDNYabcdefghijklmnopqrstuv', null)") == "J2QQ…stuv"
    assert _core("C.who('uid', 'frankdegods')") == "frankdegods"


def test_the_tab_is_wired_and_the_force_view_is_gone():
    html = Path("templates/index.html").read_text()
    app = Path("static/app.js").read_text()
    js = Path("static/wallet_map.js").read_text()
    css = Path("static/styles.css").read_text()
    assert 'data-tab="wallet-map"' in html and 'id="tab-wallet-map"' in html
    assert 'id="wallet-map-body"' not in html          # W3: the herder pane is absorbed into the summary
    assert "/static/wallet_map.js" in html and "wallet-map-canvas" not in html
    assert "'wallet-map': 'solana'" in app and "walletMap.load" in app
    assert "/api/memecoin/wallet-map/herders" in js and "/api/memecoin/wallet-map/wallet" in js
    assert "d3." not in js and "forceSimulation" not in js
    assert ".wm-" not in css.replace("#wm-tooltip", "")                # the world map keeps its own tooltip


# ---------------------------------------------------------------- one view at a time (operator 2026-10-02:
# "i cant be scrolling for hours, also super hard to read")
FUNNEL = {"funnel": [{"stage": "armed"}, {"stage": "armed"}, {"stage": "paper"}, {"stage": "tracked"},
                     {"stage": "curated"}],
          "solana_candidates": [{"address": "a"}], "rotation": [],
          "evm_candidates": {"56": {"candidates": [{"uid": "x"}, {"uid": "y"}]}, "4663": {"candidates": []}}}


def test_the_live_roster_tabs_list_every_view_with_rows_and_no_longer_hold_the_herders():
    # Wallet Map W2: the herder rows lead the section as its relationship table, so Live roster
    # controls keep only their own Armed/Watch-only/Roster views.
    tabs = _core("C.views(%s)" % json.dumps(FUNNEL))
    assert [(t["key"], t["n"]) for t in tabs] == [("armed", 2), ("paper", 1), ("tracked", 2)]
    assert tabs[1]['label'] == 'Watch only'
    assert _core("C.views(null)") == []


def test_sorting_puts_unknown_values_last_in_both_directions():
    vals = [3, None, -1, 7]
    asc = _core("%s.slice().sort((a, b) => C.cmpVal(a, b, 1))" % json.dumps(vals))
    desc = _core("%s.slice().sort((a, b) => C.cmpVal(a, b, -1))" % json.dumps(vals))
    assert asc == [-1, 3, 7, None] and desc == [7, 3, -1, None]


def test_the_14_day_gated_cell_names_only_chains_with_closes():
    assert _core("C.feasibleNonzero({'56': 0, '4663': 0, solana: 2})") == "solana 2"
    assert _core("C.feasibleNonzero({'56': 0, solana: 0})") == "0"
    assert _core("C.feasibleNonzero(null)") == "0"


def test_a_return_cell_is_coloured_and_keeps_its_sample():
    assert _core("C.retCell(0.192, 14, 0)") == (
        '<td data-v="0.192" title="+19.2% on 14"><span class="herd-pos">+19.2%</span>'
        ' <span class="herd-dim">on 14</span><span class="herd-cens"> · 0% cens</span></td>')
    assert 'class="herd-neg">-2.2%<' in _core("C.retCell(-0.022, 115, null)")
    assert "cens" not in _core("C.retCell(-0.022, 115, null)")
    assert _core("C.retCell(null, 0, null)") == '<td data-v=""><span class="herd-dim">none yet</span></td>'


def test_the_page_has_one_tab_bar_and_a_finder():
    html = Path("templates/index.html").read_text()
    js = Path("static/wallet_map.js").read_text()
    assert 'id="wallet-map-tabs"' in html and 'id="wallet-map-find"' in html
    assert "data-pane=" in js
    # W2: the herder rows lead the section; the old herder pane is parked outside Live roster controls
    assert 'data-pane="herders"' not in html and 'id="wallet-herder-pages"' not in html
    css = Path("static/styles.css").read_text()
    assert ".herd-pane-scroll" in css and "position: sticky" in css.split(".herd-pane-scroll", 1)[1][:600]


# ---- Gamma LOWs on e97f48575 ----

def test_a_sortable_header_is_a_keyboard_reachable_button():
    h = _core("C.sortTh('Drew', 'Read coins that drew a follower')")
    assert h.startswith('<th scope="col" data-sort title="Read coins that drew a follower">')
    assert '<button type="button" class="herd-sort">Drew</button></th>' in h
    assert _core("C.sortTh('<x>')") == '<th scope="col" data-sort><button type="button" class="herd-sort">&lt;x&gt;</button></th>'


def test_every_sort_value_is_escaped():
    import re
    js = Path("static/wallet_map.js").read_text()
    raw = re.findall(r'data-v="\$\{(?!esc\()[^}]*\}', js)
    assert raw == [], raw                                     # every data-v goes through esc()


def test_dossier_labels_unknown_and_escapes_research_notes():
    view = {'state': 'ok', 'wallet': 'alice', 'chain': '56', 'funnel': {'observed_buys': 2,
            'valid_entries': None, 'excluded_or_unknown': None, 'distinct_coins': 2},
            'coverage': {'days_observed': 1, 'stale': True}, 'notes': [{'at': 'today', 'text': '<script>alert(1)</script>'}],
            'entries': [{'state': 'valid', 'mint': 'coin', 'measurement_state': 'censored_or_pending',
                         'admission': {'validation': 'structural_only', 'digest': '<script>receipt</script>'}}],
            'rules': [], 'baseline': None}
    html = _core('C.dossierHTML(' + json.dumps(view) + ')')
    assert 'Structural receipts pass' in html and 'independent review' in html
    assert 'unknown' in html and 'No strategy supported' in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'structural_only' in html and '&lt;script&gt;receipt&lt;/script&gt;' in html
    assert 'Defaults not sealed' in html
    assert _core("C.dossierUrl('56','a/b')") == '/?tab=wallet-map&wm_chain=56&wm_wallet=a%2Fb'


def test_registered_size_comparison_is_visible_but_never_reads_as_a_supported_strategy():
    body = {'state': 'ok', 'cells': [], 'studies': [
        {'wallet': 'alice', 'chain': 'solana', 'analysis_scope': 'fixed_nominal_size_diagnostic_not_funded_policy',
         'policies': [{'clip_usd': 131.25}, {'clip_usd': 175.}]},
        {'wallet': 'other', 'chain': '56', 'analysis_scope': 'fixed_nominal_size_diagnostic_not_funded_policy',
         'policies': [{'clip_usd': 999.}]}]}
    html = _core('C.researchCoverageHTML(%s, "solana")' % json.dumps(body))
    assert '$131.25 vs $175.00' in html and '1 frozen wallet round' in html
    assert 'awaiting paired execution evidence' in html and 'No strategy supported' in html
    assert '$999' not in html
