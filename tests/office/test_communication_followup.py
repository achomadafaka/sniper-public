"""Display follow-up: frozen orders stay frozen; exact-mint symbols and a recorded halt only."""
import json
import sqlite3
from pathlib import Path
from playwright.sync_api import sync_playwright
from office.test_communication_ui import start
from sniper.office import operator_view as V


def symbols(root):
    desk=root.parent/'desk';(desk/'narrative_guess').mkdir(parents=True)
    c=sqlite3.connect(desk/'narrative_guess/narrative_guess.db')
    c.execute('CREATE TABLE launches(network_id TEXT,mint TEXT,symbol TEXT,first_seen_known_at REAL,PRIMARY KEY(network_id,mint))')
    c.executemany('INSERT INTO launches VALUES(?,?,?,?)',[('solana','exact','CAT',180),('solana','nearby','WRONG',100),('56','evm','WRONG',100),('solana','future','FUTURE',300)])
    c.commit();c.close()
    (root.parent/'clock_offset.json').write_text(json.dumps({'offset_s':10,'at':200}))
    return desk


def test_symbols_are_exact_mint_asof_sourced_and_never_replace_recorded_ticker(tmp_path,monkeypatch):
    monkeypatch.delenv('SNIPER_CLOCK_OFFSET_FILE',raising=False)
    root=tmp_path/'office';root.mkdir();symbols(root)
    rows=[dict(network_id='solana',mint=m,ticker=None) for m in ('exact','exa','evm','future')]
    rows.append(dict(network_id='solana',mint='exact',ticker='RECORDED'))
    V._display_symbols(root,rows,200.)
    assert rows[0]['display_ticker']=='CAT' and rows[0]['ticker'] is None
    assert rows[0]['display_ticker_source']=='narrative_guess.launches'
    assert all(row['display_ticker'] is None for row in rows[1:4])
    assert rows[4]['display_ticker']=='RECORDED' and rows[4]['display_ticker_source']=='orders.ticker'


def test_symbol_fallback_uses_only_matching_solana_token_metadata(tmp_path,monkeypatch):
    monkeypatch.delenv('SNIPER_CLOCK_OFFSET_FILE',raising=False)
    root=tmp_path/'office';root.mkdir();desk=symbols(root);(desk/'x').mkdir()
    c=sqlite3.connect(desk/'x/1970-01-01.db')
    c.execute('CREATE TABLE token_meta(id INTEGER PRIMARY KEY,mint TEXT,chain TEXT,symbol TEXT,received_at REAL)')
    c.executemany('INSERT INTO token_meta VALUES(?,?,?,?,?)',[(1,'fallback',None,'META',150),(2,'fallback','eth','WRONG',160),(3,'fallback',None,'FUTURE',201),(4,'other',None,'OTHER',190)])
    c.commit();c.close();rows=[dict(network_id='solana',mint='fallback',ticker=None)]
    V._display_symbols(root,rows,200.)
    assert rows[0]['display_ticker']=='META' and rows[0]['display_ticker_source']=='x.token_meta'
    assert rows[0]['display_ticker_at']==150


def test_head_halt_is_latest_recorded_action_asof_and_missing_stays_unknown(tmp_path):
    assert V._head_halt(tmp_path,200.)['state']=='unknown'
    c=sqlite3.connect(tmp_path/'office.db')
    c.execute('CREATE TABLE office_v2_halts(seq INTEGER PRIMARY KEY,epoch_id TEXT,action TEXT,actor_id TEXT,reason TEXT,at REAL)')
    c.executemany('INSERT INTO office_v2_halts VALUES(?,?,?,?,?,?)',[(1,'office-v2-explore-1','halt','head','Check evidence <img>',100),(2,'other','resume','operator','Foreign',110),(3,'office-v2-explore-1','resume','head','Later',300)])
    c.commit();c.close()
    out=V._head_halt(tmp_path,200.)
    assert out['state']=='known' and out['halted'] is True and out['reason']=='Check evidence <img>'
    assert V._head_halt(tmp_path,300.)['halted'] is False


def test_messages_default_empty_thoughts_dropped_expand_clear_and_halt_literal():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();start(page)
            inbox=page.get_by_role('region',name='Operator inbox')
            page.get_by_role('button',name='Open operator inbox',exact=True).click()
            assert page.get_by_role('combobox',name='Inbox items').input_value()=='messages'
            assert 'Older operator note' in inbox.inner_text() and 'Long <img' not in inbox.inner_text()
            page.evaluate("feed.seats.items[0].messages.push({event_id:'blank',actor_id:'head',kind:'thought',text:'  \\n ',corrected_at:99});ui.render(feed)")
            page.get_by_role('combobox',name='Inbox items').select_option('thought')
            assert inbox.locator('[data-message-id="thought:blank"]').count()==0
            page.keyboard.press('Escape')
            bubble=page.get_by_role('button',name='Expand Narrative message')
            assert 'Expand' in bubble.inner_text() and bubble.get_attribute('aria-haspopup')=='dialog'
            page.evaluate("ui.renderReport({state:'unknown',epoch_id:'office-v2-explore-1',head_halt:{state:'known',halted:true,actor_id:'head',reason:'Stop <img src=x>',at:95}})")
            banner=page.get_by_role('region',name='Recorded paper entry halt')
            assert banner.is_visible() and 'Stop <img src=x>' in banner.inner_text()
            assert banner.locator('img').count()==0 and 'paper entries' in banner.inner_text().lower()
        finally:browser.close()


def test_gpt_order_window_limit_is_visible_without_pooling():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();start(page)
            page.evaluate("ui.renderReport({state:'unknown',epoch_id:'office-v2-explore-1',separate_books:[{book_id:'office-v2-explore-gpt-1',epoch_id:'office-v2-explore-gpt-1',state:'known',cash_usd:1000,equity_usd:1000,unpriced_count:0,limited:true,orders:[]}]})")
            page.get_by_role('button',name='Enlarge ledger',exact=True).click()
            assert 'latest 200' in page.get_by_role('region',name='Judge GPT paper ledger').inner_text()
        finally:browser.close()


def test_halt_report_staleness_survives_feed_polls_until_known_halt_report():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();start(page)
            for halted in (False,True):
                page.evaluate("halted=>{ui.renderReport({state:'unknown',epoch_id:'office-v2-explore-1',head_halt:{state:'known',halted,actor_id:'head',reason:'Recorded action',at:95}});ui.reportFailed('network',new Error('offline'));ui.reportFailed('timeout',new Error('timeout'));}",halted)
                banner=page.get_by_role('region',name='Recorded paper entry halt')
                for _ in range(3):
                    page.evaluate('feed.as_of++;ui.render(feed)')
                    assert 'stale' in banner.inner_text().lower()
                    assert banner.get_attribute('data-stale')=='true'
                    assert banner.inner_text().lower().count('stale')==1
                    assert banner.inner_text().lower().startswith('stale')
                page.evaluate("ui.renderReport({state:'unknown',epoch_id:'office-v2-explore-1',reason:'source_unavailable'})")
                assert 'stale' in banner.inner_text().lower()
                page.evaluate("ui.renderReport({state:'unknown',epoch_id:'office-v2-explore-1',head_halt:{state:'known',halted:false,reason:'no_recorded_halt',at:100}})")
                assert 'stale' not in banner.inner_text().lower()
                assert banner.get_attribute('data-stale')=='false'
        finally:browser.close()
