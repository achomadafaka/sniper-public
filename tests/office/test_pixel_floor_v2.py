"""Pixel floor is a visual replay of current-identity observations, never invented work."""
from pathlib import Path
import subprocess
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]


def test_recorded_station_model_freezes_unknown_and_ignores_old_or_future_tools():
    code=r'''
const assert=require('node:assert/strict'),F=require('./static/office-floor.js');
const seat={actor_id:'h',role:'head',epoch_id:'e',generation:1,incarnation:1};
const feed={as_of:100,roster:{state:'known',items:[seat]},seats:{state:'known',items:[{actor_id:'h',activity:{kind:'working',at:90},current_tool:'chain_transaction',last_thought:'<img>'}]},events:{items:[]}};
assert.equal(F.v2Scene(feed)[0].station,'map');
feed.seats.items[0].current_tool='browse';assert.equal(F.v2Scene(feed)[0].station,'browser');
feed.seats.items[0].activity.kind='sleeping';assert.equal(F.v2Scene(feed)[0].station,'desk');
feed.seats.items[0].activity.kind='parked';assert.equal(F.v2Scene(feed)[0].mode,'parked');
feed.roster.state='unknown';assert.equal(F.v2Scene(feed)[0].mode,'unknown');
feed.roster.state='known';feed.seats.items[0].activity={kind:'working',at:101};assert.equal(F.v2Scene(feed)[0].mode,'unknown');
'''
    r=subprocess.run(['node','-e',code],cwd=ROOT,capture_output=True,text=True)
    assert r.returncode==0,r.stderr


def test_original_pixel_map_sprite_chat_and_legacy_removal_desktop_phone():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce')
            page.set_content((ROOT/'sniper/web/templates/office.html').read_text().replace('<main class="layout"></main>','<article class="layout"></article>'))
            page.evaluate('window.module={exports:{}}');page.add_script_tag(path=str(ROOT/'static/office-floor.js'));page.add_script_tag(path=str(ROOT/'static/office-v2.js'))
            page.evaluate('''() => {window.ui=module.exports.mount(document,document.querySelector('article'),{id:()=> 'id',send:async()=>({})});ui.render({as_of:100,roster:{state:'known',items:[{actor_id:'h',role:'head'},{actor_id:'j',role:'judge'},{actor_id:'s',role:'scout'}]},seats:{state:'known',items:[{actor_id:'h',activity:{kind:'working',at:90},current_tool:'chain_transaction',last_thought:'<img src=x onerror=alert(1)>'},{actor_id:'j',activity:{kind:'sleeping',at:90,until:120},last_thought:'Waiting for facts'},{actor_id:'s',activity:{kind:'parked',at:90,reason:'quota_unknown'},last_thought:'Source unknown'}]},events:{items:[]},mail:{state:'known',items:[]}})}''')
            assert page.locator('#legacy-office').count()==0
            assert 'Stored paper equity' not in page.locator('body').inner_text()
            game=page.get_by_role('region',name='Recorded pixel office')
            assert game.is_visible() and game.locator('canvas').count()==1
            assert game.get_by_role('button',name='Chat with Head').get_attribute('data-station')=='map'
            assert game.get_by_role('button',name='Chat with Judge').get_attribute('data-mode')=='sleeping'
            assert game.get_by_role('button',name='Chat with Scout').get_attribute('data-mode')=='parked'
            game.get_by_role('button',name='Chat with Head').click()
            page.get_by_role('dialog',name='Seat details').get_by_role('button',name='Reply',exact=True).click()
            assert page.get_by_role('combobox',name='Message recipient').input_value()=='h'
            assert game.locator('img').count()==0 and '<img src=x' in game.get_by_role('log',name='Office dialogue',exact=True).inner_text()
            for width in (1440,390):
                page.set_viewport_size({'width':width,'height':1100})
                assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
            page.keyboard.press('Escape')
            ui_top=game.bounding_box()['y'];assert ui_top<250
        finally:browser.close()


def test_only_new_recorded_station_and_peer_mail_changes_drive_walks():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();page.set_default_timeout(4500);page.set_content('<article></article>')
            page.add_script_tag(path=str(ROOT/'static/office-floor.js'))
            page.evaluate("""() => {window.game=OfficeFloorV2.mountV2(document,document.querySelector('article'),()=>{});window.feed={as_of:100,roster:{state:'known',items:[{actor_id:'h',role:'head'},{actor_id:'j',role:'judge'}]},seats:{state:'known',items:[{actor_id:'h',activity:{kind:'working',at:90},current_tool:'book_view',last_thought:'Tx '+ 'A'.repeat(64)},{actor_id:'j',activity:{kind:'working',at:90},current_tool:'book_view'}]},events:{items:[]},mail:{state:'known',items:[]}};game.render(feed)}""")
            head=page.get_by_role('button',name='Chat with Head')
            assert head.get_attribute('data-moving')=='false'
            head.click()
            assert 'A'*64 not in page.get_by_role('log').inner_text()
            assert 'AAAAAA…AAAA' in page.get_by_role('log').inner_text()
            page.evaluate("feed.as_of=101;Object.assign(feed.seats.items[0],{current_tool:'chain_transaction',last_event_id:'real-tool-call',last_event_at:101,last_event_kind:'tool_call'});game.render(feed)")
            assert head.get_attribute('data-moving')=='true'
            page.wait_for_function("document.querySelector('[aria-label=\"Chat with Head\"]').dataset.moving==='false'")
            assert head.get_attribute('data-station')=='map'
            page.evaluate("feed.as_of=102;feed.mail.items=[{message_id:'new',created_at:102,from_actor_id:'h',to_actor_id:'j',text:'Recorded peer note'}];game.render(feed)")
            assert head.get_attribute('data-moving')=='true'
            page.evaluate('game.unavailable()')
            assert head.get_attribute('data-moving')=='false' and head.get_attribute('data-mode')=='unknown'
            assert 'Movement paused' in page.get_by_role('log').inner_text()
            page.evaluate('game.render(feed)')
            assert 'Movement paused' not in page.get_by_role('log').inner_text()
        finally:browser.close()


def test_dashboard_office_fragment_auto_mounts_one_current_map():
    import json,re
    template=(ROOT/'templates/index.html').read_text()
    fragment=re.search(r'<section id="tab-desk".*?</section>',template,re.S)[0].replace('class="tab-panel"','class="tab-panel active"')
    feed={'as_of':100,'roster':{'state':'known','items':[{'actor_id':'h','role':'head'}]},'seats':{'state':'known','items':[{'actor_id':'h','activity':{'kind':'sleeping','at':90},'last_thought':'A recorded note'}]},'mail':{'state':'known','items':[]},'events':{'items':[]}}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            def route(r):
                if r.request.url.endswith('/view'):return r.fulfill(content_type='text/html',body=fragment)
                if '/api/office/feed' in r.request.url:return r.fulfill(content_type='application/json',body=json.dumps(feed))
                return r.fulfill(status=503,content_type='application/json',body='{}')
            page.route('**/*',route);page.goto('http://office.test/view')
            page.add_script_tag(path=str(ROOT/'static/office-floor.js'));page.add_script_tag(path=str(ROOT/'static/office-v2.js'))
            page.wait_for_function("document.querySelectorAll('.floor-seat').length===1")
            assert page.get_by_role('region',name='Recorded pixel office').count()==1
            assert page.locator('#tab-desk canvas').count()==1
            assert 'Stored paper equity' not in page.locator('#tab-desk').inner_text()
            assert not errors
        finally:browser.close()
