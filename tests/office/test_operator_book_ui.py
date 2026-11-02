"""Operator-only report transport and literal book rendering; disposable sources only."""
from pathlib import Path
from flask import Flask
from playwright.sync_api import sync_playwright
from sniper.routes import api_office
ROOT=Path(__file__).resolve().parents[2]
BANNER='Exploratory epoch. Modelled paper fills, no landing proof (b_unmeasured). Not evidence of skill. No inference.'


def test_report_auth_precedes_any_read_and_response_is_uncached(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(api_office,'read_operator_report',lambda root,now:calls.append((root,now)) or {'state':'unknown'},raising=False)
    for auth,status in [(False,401),(True,200)]:
        app=Flask(__name__);app.register_blueprint(api_office.create_api_office_blueprint(dict(state_root=tmp_path,clock=lambda:200.,operator_authenticated=lambda:auth)))
        response=app.test_client().get('/api/office/explore')
        assert response.status_code==status
        if auth:assert response.headers['Cache-Control']=='no-store'
        else:assert not calls
    assert calls==[(tmp_path,200.)]


def test_partial_book_never_displays_net_and_unknown_labels_stay_literal():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();page.set_content('<article></article>');page.evaluate('window.module={exports:{}}');page.add_script_tag(path=str(ROOT/'static/office-v2.js'))
            page.evaluate('''() => { window.ui=module.exports.mount(document,document.querySelector('article'),{id:()=> 'id',send:async()=>({})});ui.render({roster:{state:'known',items:[]},events:{items:[{event_id:'d1',kind:'thought',actor_id:'j',text:'WAIT: safety unknown',corrected_at:10}]}});
            window.report={state:'partial',epoch_id:'office-v2-explore-1',book:{state:'partial',now:{state:'partial',equity_usd:null,unpriced_count:2},office_net:999,open_positions:[],daily_realized_usd:-10,daily_loss_limit_usd:100},segments:{state:'known',items:[{segment:1,kind:'prompt',release_sha:'<img src=x>',at:10}]},orders:{state:'partial',items:[{order_id:'one',mint:'<img src=x>',state:'open',filled_at:10,requested_at:5,spend_usd:25,costs_usd:.5,source:'unknown',label_state:'label_missing',segment_id:1,operator_influenced:null,policy_identity_state:'absent_by_manifest'}]},null_arm:{state:'unknown'}};ui.renderReport(report); }''')
            book=page.get_by_role('region',name='Exploratory paper book')
            book.locator('.book-evidence summary').click()
            assert BANNER in book.inner_text() and 'NET withheld' in book.inner_text()
            assert 'Daily loss room: $90.00' in book.inner_text()
            page.evaluate("report.book.now.fx_source='watcher_heartbeat';report.book.now.fx_age_s=4;report.book.now.binding_kind_counts={create_pool:1};report.orders.items[0].exit_reason='rail_gap_exit';ui.renderReport(report)")
            assert 'FX watcher_heartbeat · age 4.0s' in book.inner_text()
            assert 'Priced open marks: create_pool 1' in book.inner_text()
            page.evaluate("report.book.state='unknown';report.book.now.legs=[{cause:'book_unknown',reason:'frontier_not_committed'}];ui.renderReport(report)")
            assert 'Book unknown: frontier_not_committed' in book.inner_text()
            assert '999' not in book.inner_text() and '2 unpriced' in book.inner_text()
            book.get_by_text('Recorded paper orders · partial',exact=True).click()
            book.get_by_text('Details',exact=True).click()
            assert 'label_missing' in book.inner_text() and 'absent_by_manifest' in book.inner_text()
            assert 'Exit reason: rail_gap_exit' in book.inner_text()
            assert 'Exit fill: none (open)' in book.inner_text()
            page.evaluate("report.orders.items[0].state='closed';report.orders.items[0].exit_fill_kind=null;ui.renderReport(report)")
            assert 'Exit fill: strict (closed before label)' in book.inner_text()
            page.evaluate("report.orders.items[0].exit_fill_kind='unexpected';ui.renderReport(report)")
            assert 'Exit fill: unlabelled' in book.inner_text()
            page.evaluate("report.orders.items[0].exit_fill_kind='estimated_terminal';report.orders.items[0].exit_trigger_kind='carried';ui.renderReport(report)")
            assert 'Exit fill: estimated_terminal · trigger: carried' in book.inner_text()
            assert 'estimated · upper-biased' in book.locator('.book-order').inner_text()
            assert 'and exit fills priced on an estimate' in book.inner_text()
            assert 'Estimated exit fills are upper-biased modelled cashflows, not outcome evidence.' in book.inner_text()
            page.evaluate("report.orders.items[0].exit_fill_kind='strict';report.orders.items[0].exit_trigger_kind='estimated';ui.renderReport(report)")
            assert 'Exit fill: strict · trigger: estimated' in book.inner_text()
            assert book.locator('img').count()==0
            assert 'WAIT' in page.get_by_role('region',name='Recent decisions').inner_text()
            page.evaluate("report.book.state='known';report.book.now.state='known';report.book.now.equity_usd=1000;report.book.now.unpriced_count=0;ui.renderReport(report)")
            assert '$999.00' in book.inner_text() and 'Segments' in book.inner_text()
            page.evaluate("report.epoch_id='old-epoch';ui.renderReport(report)")
            assert '$999.00' not in book.inner_text()
            page.set_viewport_size({'width':390,'height':1100})
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        finally:browser.close()


def test_report_sources_are_fixed_readonly_and_missing_never_creates_files(tmp_path,monkeypatch):
    import sqlite3
    import pytest
    from sniper.office import operator_view
    assert operator_view.read_operator_report(tmp_path,200.)['state']=='unknown'
    assert not list(tmp_path.rglob('*.db'))
    for name in ('office.db','v2/explore-ledger/ledger.db','v2/quota.db'):
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True)
        sqlite3.connect(path).close()
    def read(o,l,q,*,now):
        assert now==200.
        for conn in (o,l,q):
            with pytest.raises(sqlite3.OperationalError,match='readonly'):
                conn.execute('CREATE TABLE forbidden(x)')
        return dict(state='partial',book={'state':'unknown'},secret='must not export')
    monkeypatch.setattr(operator_view,'explore_report',read)
    result=operator_view.read_operator_report(tmp_path,200.)
    assert result['book']=={'state':'unknown'} and 'secret' not in result


def test_display_fx_uses_watcher_heartbeat_bounds_and_corrected_clock(tmp_path,monkeypatch):
    import json
    from sniper.office import operator_view
    root=tmp_path/'office';root.mkdir();desk=tmp_path/'desk';desk.mkdir()
    monkeypatch.setattr(operator_view,'_host_now',lambda:1000.)
    monkeypatch.delenv('SNIPER_CLOCK_OFFSET_FILE',raising=False)
    clock=tmp_path/'clock_offset.json';clock.write_text(json.dumps(dict(offset_s=10.,at=1000.)))
    beat=desk/'universe.heartbeat'
    beat.write_text(json.dumps(dict(connected=True,ts=999.,sol_age_s=2.,sol_usd=150.)))
    assert operator_view._display_fx(root)==dict(sol_usd=150.,sol_at=987.)
    for change in [dict(connected=False),dict(ts=994.),dict(sol_age_s=120.),dict(sol_usd=0),dict(sol_usd=True)]:
        data=dict(connected=True,ts=999.,sol_age_s=2.,sol_usd=150.);data.update(change);beat.write_text(json.dumps(data))
        assert operator_view._display_fx(root)=={}
    clock.write_text('{}');assert operator_view._display_fx(root)=={}
    clock.unlink();assert operator_view._display_fx(root)=={}
