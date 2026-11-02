"""Real Chromium and DOM events; fixtures only, no live Office starts or sends."""
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[2]


def start(page):
    page.set_viewport_size({'width':1440,'height':1200});page.set_default_timeout(4000)
    page.set_content('<article></article>');page.evaluate('window.module={exports:{}}')
    for name in ('office-order-display.js','office-floor.js','office-communications.js','office-v2.js'):page.add_script_tag(path=str(ROOT/'static'/name))
    page.evaluate('''()=>{const roles=['head','judge','judge_gpt','scout','researcher','forensics','narrative'];window.feed={as_of:100,roster:{state:'known',items:roles.map(role=>({actor_id:role,role,epoch_id:'e',generation:1,incarnation:1}))},seats:{state:'known',items:roles.map(role=>({actor_id:role,activity:{kind:'idle',at:90},last_thought:'last '+role,messages:[{event_id:'thought-'+role,actor_id:role,kind:'thought',text:'Long <img src=x onerror=alert(1)> '+role+' '.repeat(20),corrected_at:96,model:'requested-gpt',model_basis:'recorded_wake_requested'}]}))},events:{state:'known',items:[]},mail:{state:'known',items:[{message_id:'old',from_actor_id:'head',to_actor_id:'operator',created_at:95,text:'Older operator note',model:'requested-opus',model_basis:'recorded_wake_requested'}]},questions:{state:'known',items:[]}};window.ui=module.exports.mount(document,document.querySelector('article'),{id:()=>'fixture',send:async()=>{window.sent=(window.sent||0)+1;return {}}});ui.render(feed)}''')


def test_seven_seats_and_literal_expanding_history_survive_refresh():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();start(page)
            assert page.locator('.pixel-seat').count()==7
            page.get_by_role('button',name='Expand Narrative message').click()
            dialog=page.get_by_role('dialog',name='Seat messages');expect(dialog).to_be_visible()
            assert 'Narrative' in dialog.inner_text() and '<img src=x' in dialog.inner_text()
            assert dialog.locator('img').count()==0 and 'requested-gpt' in dialog.inner_text()
            page.evaluate("feed.seats.items[6].messages.unshift({event_id:'new',actor_id:'narrative',kind:'thought',text:'Newest message',corrected_at:99});ui.render(feed)")
            assert 'Newest message' not in dialog.locator('.message-body').inner_text()
            page.get_by_role('button',name='Next message',exact=True).click()
            assert 'Newest message' in dialog.locator('.message-body').inner_text()
            page.get_by_role('button',name='Previous message',exact=True).click()
            assert '<img src=x' in dialog.locator('.message-body').inner_text()
            page.keyboard.press('Escape');expect(dialog).not_to_be_visible()
            assert page.get_by_role('button',name='Expand Narrative message').evaluate('e=>e===document.activeElement')
            page.set_viewport_size({'width':390,'height':1100})
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
            boxes=page.locator('.pixel-seat').evaluate_all('es=>es.map(e=>[Math.round(e.getBoundingClientRect().x),Math.round(e.getBoundingClientRect().y)])')
            assert len(set(map(tuple,boxes)))==7
        finally:browser.close()


def test_room_inbox_authorship_unread_filter_and_separate_gpt_book():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();start(page)
            inbox=page.get_by_role('region',name='Operator inbox')
            assert inbox.locator('xpath=ancestor::*[contains(@class,"pixel-scene")]').count()==1
            assert 'Older operator note' in inbox.inner_text() and 'requested-opus' in inbox.inner_text()
            page.get_by_role('button',name='Open operator inbox',exact=True).click()
            page.get_by_role('combobox',name='Inbox seat').select_option('head')
            assert 'Narrative' not in inbox.locator('.inbox-items').inner_text()
            unread=inbox.locator('[data-message-id="mail:old"]')
            assert unread.get_attribute('data-unread')=='true'
            unread.get_by_role('button',name='Read message').click()
            page.keyboard.press('Escape');assert inbox.locator('[data-message-id="mail:old"]').get_attribute('data-unread')=='false'
            page.evaluate('ui.render(feed)');assert inbox.locator('[data-message-id="mail:old"]').get_attribute('data-unread')=='false'
            page.evaluate("ui.renderReport({state:'unknown',epoch_id:'office-v2-explore-1',separate_books:[{book_id:'office-v2-explore-gpt-1',epoch_id:'office-v2-explore-gpt-1',state:'known',cash_usd:777,equity_usd:800,unpriced_count:0,orders:[{order_id:'g1',actor_id:'judge_gpt',ticker:'REAL_TICKER',mint:'mint',state:'open',spend_usd:25,requested_at:90}]}]})")
            gpt=page.get_by_role('region',name='Judge GPT paper ledger')
            page.keyboard.press('Escape')
            page.get_by_role('button',name='Enlarge ledger',exact=True).click()
            gpt.get_by_text('Judge GPT recorded orders',exact=True).click()
            assert 'Cash: $777.00' in gpt.inner_text() and 'REAL_TICKER' in gpt.inner_text()
            assert '777' not in page.get_by_role('region',name='Exploratory paper book').inner_text()
            page.keyboard.press('Escape')
            page.get_by_role('button',name='Open operator inbox',exact=True).click()
            page.get_by_role('combobox',name='Inbox seat').select_option('judge_gpt')
            assert 'REAL_TICKER' in inbox.inner_text() and 'Judge GPT' in inbox.inner_text()
        finally:browser.close()


def test_real_new_mail_animates_once_even_when_both_seats_idle():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page(reduced_motion='no-preference');start(page)
            assert page.locator('.mail-sprite').count()==0
            page.evaluate("feed.mail.items.push({message_id:'peer',from_actor_id:'head',to_actor_id:'judge_gpt',created_at:100,text:'Real peer event'});ui.render(feed)")
            expect(page.locator('.mail-sprite[data-message-id="peer"]')).to_have_count(1)
            page.evaluate('ui.render(feed)');assert page.locator('.mail-sprite').count()==1
            page.emulate_media(reduced_motion='reduce')
            page.evaluate("feed.mail.items.push({message_id:'peer2',from_actor_id:'narrative',to_actor_id:'judge',created_at:100,text:'Reduced event'});ui.render(feed)")
            expect(page.locator('.mail-sprite')).to_have_count(0)
            assert 'Narrative' in page.locator('.mail-status').inner_text()
        finally:browser.close()


def test_unread_storage_and_unattributed_ticker_are_explicit():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();page.route('http://office.test/**',lambda r:r.fulfill(content_type='text/html',body='<article></article>'));page.goto('http://office.test/');start(page)
            item=page.get_by_role('region',name='Operator inbox').locator('[data-message-id="mail:old"]')
            item.get_by_role('button',name='Read message').click();page.keyboard.press('Escape')
            page.reload();start(page)
            assert page.get_by_role('region',name='Operator inbox').locator('[data-message-id="mail:old"]').get_attribute('data-unread')=='false'
            page.evaluate("ui.renderReport({state:'unknown',epoch_id:'office-v2-explore-1',orders:{items:[{order_id:'unknown',ticker:'<img src=x>',requested_at:90,state:'closed'}]}})")
            page.get_by_role('button',name='Open operator inbox',exact=True).click()
            page.get_by_role('combobox',name='Inbox seat').select_option('unknown')
            inbox=page.get_by_role('region',name='Operator inbox')
            assert 'Maker unknown' in inbox.inner_text() and '<img src=x>' in inbox.inner_text() and inbox.locator('img').count()==0
        finally:browser.close()


def test_explicit_recorded_replay_is_labelled_and_never_sends():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page(reduced_motion='no-preference');start(page)
            page.evaluate("window.sent=0;ui=ui;feed.mail.items.push({message_id:'old-peer',from_actor_id:'narrative',to_actor_id:'judge',created_at:60,text:'Historical real event'});ui.render(feed)")
            assert page.locator('.mail-sprite').count()==0
            page.get_by_role('button',name='Expand Narrative message').click()
            page.get_by_role('button',name='Previous message',exact=True).click()
            assert 'Historical real event' in page.locator('.message-body').inner_text()
            page.get_by_role('button',name='Replay recorded mail',exact=True).click()
            expect(page.locator('.mail-sprite[data-message-id="old-peer"][data-replay="true"]')).to_have_count(1)
            assert 'Replay of recorded mail' in page.locator('.mail-status').inner_text()
            assert page.evaluate('sent')==0
        finally:browser.close()


def test_missing_symbol_keeps_recorded_coin_as_mint_with_author():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();start(page)
            page.evaluate("ui.renderReport({state:'unknown',epoch_id:'office-v2-explore-1',orders:{items:[{order_id:'mint-only',mint:'RecordedMintAddress',actor_id:'judge',requested_at:90,state:'closed',ticker:null}]}})")
            page.get_by_role('button',name='Open operator inbox',exact=True).click()
            page.get_by_role('combobox',name='Inbox items').select_option('coin')
            inbox=page.get_by_role('region',name='Operator inbox')
            assert 'Ticker unknown' in inbox.inner_text() and 'Judge' in inbox.inner_text() and 'Model not recorded on order' in inbox.inner_text()
        finally:browser.close()


def test_decision_coin_uses_exact_recorded_order_when_tool_result_has_only_order_id():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();start(page)
            page.evaluate("feed.events.items=[{event_id:'order-result',actor_id:'judge',kind:'tool_result',corrected_at:90,refs:{tool:'paper_order'},text:JSON.stringify({state:'submitted',order_id:'exact-order'})}];ui.render(feed);ui.renderReport({state:'known',epoch_id:'office-v2-explore-1',orders:{items:[{order_id:'exact-order',mint:'ExactMint',display_ticker:'FUN',state:'open'}]}})")
            page.get_by_role('button',name='Enlarge ledger',exact=True).click()
            assert 'coin FUN' in page.get_by_role('region',name='Recent decisions').inner_text()
        finally:browser.close()


def test_gpt_unmeasured_usage_open_net_and_heartbeat_have_plain_labels():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();start(page)
            page.evaluate("feed.seats.items=[{actor_id:'judge_gpt',activity:{kind:'working',at:90},tokens_today:null,tokens_known:0,tokens_unknown_wakes:1}];ui.render(feed);ui.renderReport({state:'known',epoch_id:'office-v2-explore-1',separate_books:[{book_id:'office-v2-explore-gpt-1',epoch_id:'office-v2-explore-gpt-1',state:'unknown',marked_state:'known',marked_unpriced_count:0,marked_equity_usd:1009.32,watcher_heartbeat:{state:'known',watcher_state:'managed',fx_state:'known',age_s:0,last_good_utc:100},orders:[{order_id:'g',state:'open',ticker:'FUN'}]}]})")
            assert 'not captured for GPT' in page.locator('.tokens-note').all_text_contents()
            page.get_by_role('button',name='Enlarge ledger',exact=True).click()
            book=page.get_by_role('region',name='Judge GPT paper ledger')
            assert 'Watcher: managed' in book.inner_text() and 'Marked equity: $1009.32' in book.inner_text()
            assert 'needs proven marks; marked estimate shown' in book.text_content()
        finally:browser.close()


def test_proven_open_book_does_not_call_unrealized_net_missing_proof():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();start(page)
            page.evaluate("ui.renderReport({state:'known',epoch_id:'office-v2-explore-1',book:{state:'known',now:{state:'known',unpriced_count:0,equity_usd:1000},open_positions:[{}]},orders:{state:'known',items:[{order_id:'m',state:'open',ticker:'FUN',modelled_net_usd:null}]},separate_books:[{book_id:'office-v2-explore-gpt-1',epoch_id:'office-v2-explore-gpt-1',state:'known',unpriced_count:0,equity_usd:1000,orders:[{order_id:'g',state:'open',ticker:'FUN',modelled_net_usd:null}]}]})")
            for name in ('Judge GPT paper ledger','Exploratory paper book'):
                body=page.get_by_role('region',name=name).text_content()
                assert 'needs proven marks' not in body
                assert 'not realized' in body
        finally:browser.close()
