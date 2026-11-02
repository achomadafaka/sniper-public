"""CEO chat DOM and idempotent send controls; transport is a local mock, never a live DM."""
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]


def start(page):
    page.set_default_timeout(3000)
    page.set_content('<article></article>');page.evaluate('window.module={exports:{}}')
    page.add_script_tag(path=str(ROOT/'static/office-v2.js'))
    page.evaluate('''() => {
      window.feed={roster:{state:'known',items:[{actor_id:'h',role:'head'},{actor_id:'j',role:'judge'},{actor_id:'s',role:'scout'}]},events:{state:'known',items:[]},
      questions:{state:'known',items:[{event_id:'q1',actor_id:'h',corrected_at:10,text:'**Need a ruling** <img src=x onerror=alert(1)>'}]},
      mail:{state:'known',items:[{message_id:'m1',from_actor_id:'operator',to_actor_id:'h',created_at:10,text:'Check funding'},
      {message_id:'m2',from_actor_id:'h',to_actor_id:'operator',created_at:11,text:'Here is my reply'},
      {message_id:'m3',from_actor_id:'crew',to_actor_id:'h',created_at:12,text:'Crew note, not CEO'},
      {message_id:'m4',from_actor_id:'s',to_actor_id:'j',created_at:13,text:'Peer evidence'}]},tasks:{state:'unknown',items:[]}};
      window.sent=[];window.fail=false;window.response={state:'sent',message_id:'m',delivery:'delivered, seat waking',parked_reason:null};
      window.ui=module.exports.mount(document,document.querySelector('article'),{id:()=> 'stable-client-id',send:async body=>{sent.push({...body});if(fail)throw Error('offline');return response}});ui.render(feed);
    }''')


def test_operator_inbox_reply_threads_and_crew_are_literal_and_distinct():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();start(page)
            assert page.get_by_role('heading',name='CEO chat').is_visible()
            inbox=page.get_by_role('region',name='Questions for the operator')
            assert 'Need a ruling' in inbox.inner_text() and 'Here is my reply' in inbox.inner_text()
            assert inbox.locator('img').count()==0
            inbox.get_by_role('button',name='Reply to Head').first.click()
            assert page.get_by_role('combobox',name='Message recipient').input_value()=='h'
            page.get_by_role('textbox',name='Operator message').fill('A reply')
            page.get_by_role('button',name='Send operator DM').click()
            page.wait_for_function('sent.length===1')
            assert page.evaluate('sent[0].to_actor_id')=='h'
            assert page.evaluate('Object.keys(sent[0]).sort().join(",")')=='client_id,text,to_actor_id'
            assert 'seat waking' in page.get_by_role('status').inner_text()
            page.get_by_role('combobox',name='Conversation type').select_option('crew')
            thread=page.get_by_role('log',name='Conversation')
            assert 'Crew note' in thread.inner_text() and 'You →' not in thread.inner_text()
            page.get_by_role('combobox',name='Message recipient').select_option('all')
            page.get_by_role('combobox',name='Conversation type').select_option('seats')
            assert 'Scout → Judge' in thread.inner_text() and 'Peer evidence' in thread.inner_text()
        finally:browser.close()


def test_all_hands_retry_keeps_identity_and_delivery_remains_a_snapshot():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();start(page)
            page.get_by_role('combobox',name='Message recipient').select_option('all')
            box=page.get_by_role('textbox',name='Operator message');box.fill('All hands')
            page.evaluate('fail=true')
            page.get_by_role('button',name='Send operator DM').click()
            page.wait_for_function('sent.length===1')
            assert box.input_value()=='All hands'
            page.evaluate('''fail=false;response={state:'sent',seats:{h:{state:'duplicate',message_id:'h1',delivery:'delivered, seat parked: <img src=x>',parked_reason:'<img src=x>'},j:{state:'sent',message_id:'j1',delivery:'delivered, seat waking'},s:{state:'sent',message_id:'s1',delivery:'delivered, seat state unknown'}}}''')
            page.get_by_role('button',name='Send operator DM').click()
            page.wait_for_function('sent.length===2 && document.querySelector("textarea").value===""')
            assert page.evaluate('sent[0].client_id===sent[1].client_id && sent[1].to_actor_id==="all"')
            status=page.get_by_role('status')
            assert 'snapshot' in status.inner_text().lower() and 'seat parked' in status.inner_text()
            assert status.locator('img').count()==0
            page.set_viewport_size({'width':390,'height':1100})
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        finally:browser.close()


def test_malformed_broadcast_receipt_keeps_draft_and_retry_identity():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();start(page)
            page.get_by_role('combobox',name='Message recipient').select_option('all')
            box=page.get_by_role('textbox',name='Operator message');box.fill('Keep this')
            page.evaluate("response={state:'sent',seats:{}}")
            page.get_by_role('button',name='Send operator DM').click()
            page.wait_for_function('sent.length===1')
            assert box.input_value()=='Keep this'
            assert 'unconfirmed' in page.get_by_role('status').inner_text()
            page.get_by_role('button',name='Send operator DM').click()
            page.wait_for_function('sent.length===2')
            assert page.evaluate('sent[0].client_id===sent[1].client_id')
        finally:browser.close()


def test_watch_alert_is_distinct_from_operator_crew_and_peer_mail():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page();start(page)
            page.evaluate("feed.as_of=20;feed.mail.items.push({message_id:'watch1',from_actor_id:'watch',to_actor_id:'h',created_at:20,text:'<img src=x onerror=alert(1)> owner jump'});ui.render(feed)")
            view=page.get_by_role('combobox',name='Conversation type')
            view.select_option('watch')
            thread=page.get_by_role('log',name='Conversation')
            assert 'Watch alert → Head' in thread.inner_text()
            assert '<img src=x' in thread.inner_text() and thread.locator('img').count()==0
            assert thread.locator('[data-sender=watch]').count()==1
            assert thread.locator('[data-sender=operator],[data-sender=crew],[data-sender=seat]').count()==0
            view.select_option('seats');assert 'owner jump' not in thread.inner_text()
            view.select_option('operator');assert 'owner jump' not in thread.inner_text()
            assert page.evaluate('sent.length')==0
            page.add_script_tag(path=str(ROOT/'static/office-floor.js'))
            page.evaluate("()=>{const host=document.createElement('div');document.body.append(host);const game=OfficeFloorV2.mountV2(document,host,()=>{});game.render(feed)}")
            game=page.get_by_role('region',name='Recorded pixel office')
            assert 'Watch alert' in game.get_by_role('log').inner_text()
            assert game.locator('img').count()==0
        finally:browser.close()
