"""Behavior checks for operator display state. These fixtures do not submit orders."""
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(body):
    src = (ROOT / 'static/app.js').read_text()
    start = src.find('    _operatorPrice(')
    assert start >= 0, 'operator display helpers are absent'
    end = src.index('    _realTable(', start)
    script = "const assert=require('node:assert/strict'); const esc=v=>String(v??''); const mc={" + src[start:end] + "};\n" + body
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_small_prices_keep_significant_digits_and_unknown():
    # 2026-09-12 operator: "no decimal numbers like 0.000000627262" -- subscript zero count below
    # a tenth of a cent, four significant digits above it, cents from $1.
    run_js("assert.equal(mc._operatorPrice(0.00000004), '$0.0₇4'); assert.equal(mc._operatorPrice(null), 'Unknown');"
           "assert.equal(mc._operatorPrice(0.000000627262), '$0.0₆6273');"
           "assert.equal(mc._operatorPrice(0.4567), '$0.4567'); assert.equal(mc._operatorPrice(1234.567), '$1,234.57');")


def test_old_observations_do_not_replace_new_prices_or_book_values():
    run_js(r"""
    const newer={id:1,market_mid_price:4,market_mid_at:'2026-09-11T11:00:04Z',selected_exit_price:3,selected_exit_at:'2026-09-11T11:00:04Z',selected_exit_basis:'measured_route',remaining_value_usd:30,recovered_usd:10,total_pnl_usd:15};
    const older={id:1,market_mid_price:2,market_mid_at:'2026-09-11T11:00:02Z',selected_exit_price:1,selected_exit_at:'2026-09-11T11:00:02Z',selected_exit_basis:'mid',remaining_value_usd:10,total_pnl_usd:-15};
    const merged=mc._mergePosition(newer,older);
    assert.equal(merged.market_mid_price,4); assert.equal(merged.selected_exit_basis,'measured_route');
    assert.equal(merged.remaining_value_usd,30); assert.equal(merged.recovered_usd,10);
    """)


def test_missing_observation_keeps_value_and_its_original_timestamp():
    run_js(r"""
    const old={id:1,market_mid_price:4,market_mid_at:'2026-09-11T11:00:04Z'};
    const merged=mc._mergePosition(old,{id:1,market_mid_price:null,market_mid_at:'2026-09-11T11:01:00Z'});
    assert.equal(merged.market_mid_price,4); assert.equal(merged.market_mid_at,old.market_mid_at);
    assert.equal(merged.market_mid_unavailable,true);
    """)


def test_partial_sale_uses_backend_values_without_reconstructing_remaining_value():
    run_js(r"""
    mc._usd2=v=>v==null?'Unknown':'$'+v; mc._money=mc._usd2; mc._pct=v=>v==null?'Unknown':v+'%'; mc._col=()=>''; mc._quoteAge=()=>'';
    const html=mc._operatorValueRows({remaining_value_usd:8,recovered_usd:20,total_pnl_usd:3,total_pnl_pct:12,entry_notional:25,pnl_pct:12});
    assert.match(html,/Remaining value[\s\S]*?\$8/); assert.match(html,/Recovered cash[\s\S]*?\$20/); assert.doesNotMatch(html,/\$28/);
    """)


def test_links_preserve_exact_identity_and_use_explicit_labels():
    run_js(r"""
    const html=mc._operatorLinks({token:'https://axiom.trade/t/SoLaNaMint',entry:'/?tab=memecoin&mc_view=entries&mc_origin=origin%2F1'});
    assert.match(html,/SoLaNaMint/); assert.match(html,/>Axiom<\/a>/); assert.match(html,/title="Token on Axiom"/); assert.match(html,/origin%2F1/); assert.match(html,/>Entry result<\/a>/);
    """)


def test_full_and_ticker_snapshots_use_one_merge_path_and_reject_old_books():
    run_js(r"""
    mc._mergePositionBook({as_of:'2026-09-11T11:00:04Z',positions:[{id:1,market_mid_price:4,market_mid_at:'2026-09-11T11:00:04Z'}]});
    mc._mergePositionBook({as_of:'2026-09-11T11:00:02Z',positions:[]});
    assert.equal(mc._realPositions.length,1);
    mc._mergePositionBook({as_of:'2026-09-11T11:00:06Z',positions:[]});
    assert.equal(mc._realPositions.length,0);
    """)


from test_memecoin_ui_render_runtime import browser, memecoin_page, _set_api_payloads


def _live_payloads():
    return {
        '/api/memecoin/real/status': {'armed': True, 'venues': {}, 'knobs': {}, 'envelope': {}, 'real_open': {}},
        '/api/memecoin/real/positions': {'positions': []},
        '/api/memecoin/real/closed': {'closed': []},
        '/api/memecoin/stuck-bags': {'rows': []},
        '/api/memecoin/engine-cockpit': {'positions': []},
        '/api/memecoin/entries?limit=100': {'entries': [], 'as_of': '2026-09-11T12:00:00Z'},
    }


def test_open_system_edit_keeps_focus_while_the_live_book_changes(memecoin_page):
    page, errors = memecoin_page
    _set_api_payloads(page, _live_payloads())
    result = page.evaluate('''async () => {
      const mc=window.__memecoin;
      document.getElementById('memecoin-board').insertAdjacentHTML('afterbegin', '<details id="mc-system" open><summary>System</summary><div id="mc-system-body"><input id="draft-knob" value="13"></div></details>');
      mc._entryKnobs={}; mc._acct={by_network:{},totals:{}}; mc._acctTs=Date.now(); mc._loadRefusalSlots=()=>{};
      const input=document.getElementById('draft-knob'); input.value='17'; input.focus();
      await mc._loadReal();
      return {blocked:mc._pollBlocked(),focus:document.activeElement===input,value:input.value,live:document.getElementById('mc-real').textContent};
    }''')
    assert result['blocked'] is False
    assert result['focus'] is True
    assert result['value'] == '17'
    assert 'Nothing open' in result['live']
    assert errors == []


def test_empty_authoritative_ticker_removes_the_old_card_immediately(memecoin_page):
    page, errors = memecoin_page
    result = page.evaluate('''() => {
      const mc=window.__memecoin;
      mc._realPositions=[{id:7}];
      document.getElementById('mc-real').innerHTML='<article data-position-id="7"><div data-real-ticker="7">old position</div></article>';
      mc._applyRealTicker({positions:[],as_of:'2026-09-11T12:00:00Z'});
      return document.getElementById('mc-real').textContent;
    }''')
    assert 'old position' not in result
    assert errors == []


def test_operator_deep_link_keeps_chain_and_exact_origin(memecoin_page):
    page, errors = memecoin_page
    result = page.evaluate('''() => {
      const mc=window.__memecoin;
      mc._readOperatorLocation('?tab=memecoin&mc_view=entries&mc_chain=4663&mc_origin=Source%2F123');
      return {view:mc._workspace,chain:mc._auxChainFilter,origin:mc._focusedOrigin};
    }''')
    assert result == {'view':'entries','chain':'robin','origin':'Source/123'}
    assert errors == []


def test_a_links_chain_does_not_follow_the_operator_to_live(memecoin_page):
    """2026-09-14 (operator: "ui is still robin only"): a Robinhood link filtered every view."""
    page, errors = memecoin_page
    result = page.evaluate('''() => {
      const mc=window.__memecoin;
      mc._auxChainFilter='all'; mc._auxFromLink=false; mc._workspace='live';
      mc._readOperatorLocation('?tab=memecoin&mc_view=live&mc_chain=4663&mc_position=48515');
      const live=mc._auxChainFilter;
      mc._readOperatorLocation('?tab=memecoin&mc_view=entries&mc_chain=4663&mc_origin=o1');
      const entries=mc._auxChainFilter;
      mc._showWorkspace('live');
      const back=mc._auxChainFilter;
      mc._setAuxChainFilter('robin'); mc._showWorkspace('entries');
      return {live, entries, back, chosen: mc._auxChainFilter};
    }''')
    assert result == {'live':'all','entries':'robin','back':'all','chosen':'robin'}
    assert errors == []


def test_candle_chart_uses_actual_fill_time_and_utc_axis(memecoin_page):
    page, errors = memecoin_page
    _set_api_payloads(page, {
        '/api/memecoin/candles?chain_id=4663&mint=mint&interval=60&position_id=7': {
            'candles': [
                {'t':'2026-09-11T12:00:00Z','o':1,'h':2,'l':1,'c':2},
                {'t':'2026-09-11T12:01:00Z','o':1,'h':2,'l':1,'c':2},
                {'t':'2026-09-11T12:10:00Z','o':1,'h':2,'l':1,'c':2}],
            'fills':[{'t':'2026-09-11T12:05:00Z','price':1.5,'kind':'entry'}],
            'rails':{}, 'levels':{},
        }
    })
    html = page.evaluate('''async () => {
      const box=document.createElement('div');
      await window.__memecoin._renderCandleChart(7,'mint','4663',box,60);
      return box.innerHTML;
    }''')
    assert '12:00:00' in html and '12:10:00' in html and 'UTC' in html
    assert 'cx="333.0"' in html
    assert 'undefined' not in html
    assert errors == []


def test_table_and_cards_share_partial_sale_amounts(memecoin_page):
    page, errors = memecoin_page
    html = page.evaluate('''() => window.__memecoin._realTable([{id:7,mint:'mint',network_id:'4663',ticker:'TEST',entry_notional:25,entry_price:1,pnl_pct:500,total_pnl_pct:12,total_pnl_usd:3,remaining_value_usd:8,recovered_usd:20,market_mid_price:0.00000004,market_mid_at:'2026-09-11T12:00:00Z'}])''')
    assert 'Remaining value' in html and 'Recovered cash' in html
    assert '$8.00' in html and '$20.00' in html and '+$3.00' in html
    assert '+500.0%' not in html
    assert '$0.0₇4' in html
    assert errors == []


def test_the_table_shows_the_sell_quote_range_beside_the_market_peak(memecoin_page):
    """Exec-extremes slice C: the executable top and bottom get their own column; one header per
    cell; unknown is a dash, never 0%."""
    page, errors = memecoin_page
    out = page.evaluate('''() => {
      const base = {mint:'mint',network_id:'4663',entry_notional:25,entry_price:1,total_pnl_pct:12,
                    total_pnl_usd:3,max_gain_pct:50,peak_basis:'display'};
      const box = document.createElement('div');
      box.innerHTML = window.__memecoin._realTable([
        {...base, id:7, ticker:'KNOWN', exec_high_pct:13.72, exec_low_pct:-41.04, exec_samples:63},
        {...base, id:8, ticker:'BLANK', exec_high_pct:null, exec_low_pct:null, exec_samples:null}]);
      const heads = [...box.querySelectorAll('thead th')].map(th => th.innerText.trim());
      const rows = [...box.querySelectorAll('tr[data-real-trow]')].map(tr => [...tr.children].map(td => td.innerText.trim()));
      const at = heads.findIndex(h => h.startsWith('Sell-quote hi / lo'));
      const byTicker = Object.fromEntries(rows.map(r => [r[0], r]));
      return {heads: heads.length, cells: rows.map(r => r.length), peakAt: heads.findIndex(h => h.startsWith('Market peak')), at,
              known: byTicker.KNOWN[at], blank: byTicker.BLANK[at],
              title: box.querySelector('tr[data-real-trow="7"]').children[at].querySelector('span').title};
    }''')
    assert out["cells"] == [out["heads"], out["heads"]]
    assert out["at"] == out["peakAt"] + 1
    assert out["known"] == "+13.7% / -41.0%" and out["blank"] == "—"
    assert "63 quotes" in out["title"] and "not the market peak" in out["title"]
    assert errors == []


def test_entry_gate_values_show_units_and_source_authority(memecoin_page):
    page, errors = memecoin_page
    html = page.evaluate('''() => window.__memecoin._entryRows([{origin_event_id:'origin',network_id:'4663',label:'Refused',status:'refused',authority:{state:'unknown'},gate_evidence:[{name:'volume_gate',required_usd:27008,cost_pct:9.75,cap_pct:8,measurement_basis:'sequential_exact'}],attempts:[{attempt_id:'a1',terminal_status:'refused',terminal_reason:'cost_cap'}]}])''')
    assert 'Required volume' in html and '$27,008.00' in html
    assert '9.75%' in html and '8%' in html
    assert 'Unknown for this source event' in html
    assert 'sequential exact' in html
    assert '{"' not in html
    assert errors == []


def test_inline_market_chart_draws_known_effective_levels(memecoin_page):
    page, errors = memecoin_page
    html = page.evaluate('''() => {
      const box=document.createElement('div');
      window.__memecoin._markChartView(box,{points:[{t:'2026-09-11T12:00:00Z',mid:1,source:'dexscreener'},{t:'2026-09-11T12:01:00Z',mid:2,source:'dexscreener'}]},180,{effective_stop_price:.5,effective_trail_price:1.6,effective_catastrophe_price:.2});
      return box.innerHTML;
    }''')
    assert 'Active stop' in html and 'Active trail' in html and 'Catastrophe' in html
    assert errors == []


def test_manual_result_uses_final_projection_and_keeps_another_draft_focus(memecoin_page):
    page, errors = memecoin_page
    _set_api_payloads(page, {'/api/memecoin/real/intent/manual-1': {'intent': {'state':'unresolved','status':'transaction_failed','label':'Transaction failed','reason':'receipt_failed'}}})
    result = page.evaluate('''async () => {
      const mc=window.__memecoin, tasks=[], original=window.setTimeout;
      mc._renderQuickBuy();
      const input=document.getElementById('mc-rb-mint'); input.value='OtherMint'; input.focus();
      mc._loadReal=()=>{};
      window.setTimeout=fn=>{tasks.push(fn); return 1;};
      await mc._pollIntent('manual-1','FIRST');
      await tasks.shift()();
      window.setTimeout=original;
      return {rows:mc._realBuyIntents,focus:document.activeElement===input,mint:input.value};
    }''')
    assert result['rows'][0]['state'] == 'transaction_failed'
    assert result['rows'][0]['resultLabel'] == 'Transaction failed'
    assert result['focus'] is True
    assert result['mint'] == 'OtherMint'
    assert errors == []


def test_volume_ceiling_uses_a_market_cap_ratio(memecoin_page):
    page, errors = memecoin_page
    html = page.evaluate('''() => window.__memecoin._entryEvidence([{name:'volume_gate',effective_ceiling:.5,configured_ceiling:1,required_usd:27008}])''')
    assert '0.5 × market cap' in html
    assert '$0.50' not in html
    assert '$27,008.00' in html
    assert errors == []


def test_old_closed_position_link_loads_exact_readonly_detail(memecoin_page):
    page, errors = memecoin_page
    _set_api_payloads(page, {'/api/memecoin/real/positions/7': {'position': {'id':7,'ticker':'OLD','mint':'0xCase','network_id':'4663','is_open':False,'execution_mode':'real','realized_pnl':3,'entry_price':.00000004,'entry_notional':25,'closed_at':'2026-08-01T12:00:00Z', 'policy_source':'funded_contract', 'entry_contract': {'version':1, 'classification':{'market_regime':'thin'}, 'sizing':{'base_clip_usd':100., 'risk_scale':.75, 'approved_clip_usd':25.}}},'as_of':'2026-09-11T12:00:00Z'}})
    html = page.evaluate('''async () => {
      document.getElementById('memecoin-board').insertAdjacentHTML('afterbegin','<div id="mc-linked-position"></div>');
      const mc=window.__memecoin; mc._focusedPosition='7'; mc._auxChainFilter='robin';
      await mc._loadFocusedPosition();
      return document.getElementById('mc-linked-position').innerHTML;
    }''')
    assert 'OLD' in html and 'Closed' in html and '0xCase' in html
    assert '$100.00 base' in html and '$25.00 approved' in html and 'filled $25.00' in html
    assert '$0.0₇4' in html
    assert 'data-action="rclose"' not in html
    assert errors == []


def test_new_partial_sale_ledger_does_not_use_the_quote_clock():
    run_js(r"""
    mc._mergePositionBook({as_of:'2026-09-11T12:00:04Z',positions:[{id:1,selected_exit_price:2,selected_exit_at:'2026-09-11T12:00:04Z',remaining_value_usd:20,recovered_usd:0,open_qty:10,total_pnl_usd:10}]});
    mc._mergePositionBook({as_of:'2026-09-11T12:00:06Z',positions:[{id:1,selected_exit_price:1,selected_exit_at:'2026-09-11T12:00:02Z',remaining_value_usd:5,recovered_usd:6,open_qty:5,total_pnl_usd:1}]});
    const row=mc._realPositions[0];
    assert.equal(row.recovered_usd,6); assert.equal(row.open_qty,5);
    assert.equal(row.selected_exit_price,2); assert.equal(row.remaining_value_usd,null); assert.equal(row.total_pnl_usd,null);
    """)


def test_entry_delivery_uses_durable_delivery_state(memecoin_page):
    page, errors = memecoin_page
    html = page.evaluate('''() => window.__memecoin._entryRows([{origin_event_id:'o1',label:'Refused',status:'refused',notification_delivery:{available:true,source:{delivery_state:'delivered'},result:{delivery_state:'queued',updated_at:'2026-09-11T12:00:00Z'}}}])''')
    assert 'Source notice' in html and 'Result notice' in html
    assert 'queued' in html and 'delivered' in html
    assert errors == []


def test_no_fill_does_not_use_receipt_time_as_result_time(memecoin_page):
    page, errors = memecoin_page
    html = page.evaluate('''() => window.__memecoin._entryRows([{origin_event_id:'o1',label:'No fill',status:'no_fill',observed_at:'2026-09-11T12:00:00Z'}])''')
    assert 'Result Unknown' in html
    assert 'mc-entry-result refused' in html
    assert errors == []


@pytest.mark.parametrize('width', [1280, 1600, 390])
def test_closed_timing_stays_inside_main_column(memecoin_page, width):
    page, errors = memecoin_page
    page.set_viewport_size({'width': width, 'height': 900})
    payloads = _live_payloads()
    payloads['/api/memecoin/real/closed'] = {'closed': [
        {'id': 91, 'mint': 'MINT', 'ticker': 'COIN', 'network_id': 'solana',
         'source_handle': 'alice', 'entry_notional': 25, 'realized_pnl': 2,
         'entry_price': 1, 'opened_at': '2026-09-20T12:00:00Z', 'closed_at': '2026-09-20T12:01:00Z',
         'fill_timing': {'line': 'Landed S+4 · slot lane refused: ' + 'long_reason_' * 40,
                         'stages': {'detect': 0, 'decide_ms': 12, 'land_slots': 2, 'land_ms': 314}}},
        {'id': 92, 'mint': 'OTHER', 'network_id': '56', 'fill_timing': None},
    ]}
    _set_api_payloads(page, payloads)
    result = page.evaluate('''async () => {
      const mc=window.__memecoin;
      const board=document.getElementById('memecoin-board');
      board.innerHTML='<div id="mc-status"></div><div id="mc-workspaces"></div><div class="mc-section" data-section="real"><div id="mc-money">Balance rail</div><div id="mc-real"></div></div>';
      mc._desktopLayout(window.innerWidth>=1280);
      mc._entryKnobs={}; mc._acct={by_network:{},totals:{}}; mc._acctTs=Date.now(); mc._loadRefusalSlots=()=>{};
      await mc._loadReal();
      const fold=board.querySelector('[data-dk="real-closed"]'); fold.open=true;
      const scroll=fold.querySelector('.mc-closed-scroll');
      const rail=board.querySelector('.mc-live-rail');
      if(window.innerWidth>=1280 && !rail) throw new Error('Desktop rail missing');
      const row=fold.querySelector('[data-position-id="91"]');
      const missing=fold.querySelector('[data-position-id="92"]');
      const timing=fold.querySelector('[data-fill-timing="91"]');
      const visible=timing && timing.getBoundingClientRect();
      return {text:row.textContent+(timing?timing.textContent:''), missing:missing.textContent+fold.querySelector('[data-fill-timing="92"]').textContent, rowHeight:row.getBoundingClientRect().height,
        timingVisible:!!visible && visible.left>=scroll.getBoundingClientRect().left && visible.right<=scroll.getBoundingClientRect().right && [...timing.children].every(el=>{const r=el.getBoundingClientRect();return r.left>=visible.left && r.right<=visible.right+1;}),
        timingText:timing?timing.textContent:'',
        right:scroll.getBoundingClientRect().right,
        bound:rail?rail.getBoundingClientRect().left:window.innerWidth,
        chartSpan:fold.querySelector('[data-chart-keep]').parentElement.colSpan,
        pageOverflow:document.documentElement.scrollWidth>window.innerWidth};
    }''')
    assert 'S+4' in result['text'] and 'slot lane refused' in result['text']
    # 47d4fefc4: the third cell is the confirmation observation (land_ms), not landing slots.
    assert '0 slots' in result['text'] and '12 ms' in result['text'] and '314 ms' in result['text']
    assert 'Unknown' in result['missing']
    assert result['right'] <= result['bound']
    assert result['pageOverflow'] is False
    assert result['chartSpan'] == 11
    assert result['timingVisible'] is True
    assert all(label in result['timingText'] for label in
               ['Landed', 'Why', 'Feed head − source', 'Detect → submit', 'Confirmation observed'])
    if width >= 1280:
        assert result['rowHeight'] < 160
    assert errors == []


def test_unresolved_evm_landing_chip_uses_blocks(memecoin_page):
    page, errors = memecoin_page
    result=page.evaluate("() => window.__memecoin._landedPill('Landed +? blocks (source block resolving)')")
    assert '+? blk' in result and 'S+?' not in result
    assert '--text-muted' in result
    assert errors==[]
