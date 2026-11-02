"""Exercise real DOM rendering and DM retry identity without network or model calls."""
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def test_hostile_feed_workspace_and_dm_retry_are_literal_and_bounded():
    code=r'''
const assert=require('node:assert/strict');
const U=require('./static/office-v2.js');
class Element {
 constructor(tag){this.tagName=tag;this.children=[];this.value='';this.listeners={};this.dataset={};}
 append(...xs){this.children.push(...xs);}
 prepend(...xs){this.children.unshift(...xs);}
 replaceChildren(...xs){this.children=xs;}
 addEventListener(k,f){this.listeners[k]=f;}
 setAttribute(k,v){this[k]=v;}
 set innerHTML(v){throw Error('unsafe HTML');}
 get textContent(){return this._text||this.children.map(x=>x.textContent||'').join('');}
 set textContent(v){this._text=String(v);this.children=[];}
}
const doc={createElement:t=>new Element(t),createDocumentFragment:()=>new Element('#fragment')}, parent=new Element('main');
const payload='<img src=x onerror=alert(1)>', sent=[];
let fail=true;
const ui=U.mount(doc,parent,{id:()=> 'stable-client',send:async body=>{sent.push(body);if(fail)throw Error('offline');return {state:'duplicate',message_id:'m'};}});
ui.render({state:'known',roster:{state:'known',items:[{actor_id:'judge',generation:1,incarnation:'i',epoch_id:'ep'}]},events:{state:'partial',limited:true,items:[{actor_id:'judge',kind:'thought',state:'known',text:payload,corrected_at:100},{actor_id:'judge',kind:'workspace',state:'known',refs:{files:[{path:payload,size_bytes:0}],notebook_text:payload}},{actor_id:'judge',kind:'standup',text:payload}]},mail:{state:'known',items:[]},tasks:{state:'unknown',items:[]}});
assert(parent.textContent.includes(payload));
assert(parent.textContent.includes('partial'));
assert(parent.textContent.includes('unknown'));
assert(parent.textContent.includes('No messages'));
assert(parent.textContent.toLowerCase().includes('standup'));
ui.input.value=payload;
(async()=>{
 await ui.submit({preventDefault(){}});
 assert.equal(ui.input.value,payload);
 fail=false;await ui.submit({preventDefault(){}});
 assert.equal(sent.length,2);assert.equal(sent[0].client_id,sent[1].client_id);
 assert.deepEqual(sent[0],{to_actor_id:'judge',client_id:'stable-client',text:payload});
 assert.equal(ui.input.value,'');
 ui.unavailable();assert(parent.textContent.includes('Connection lost'));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run(['node','-e',code],cwd=ROOT,capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_both_office_surfaces_load_v2_module():
    for path in ('templates/index.html','sniper/web/templates/office.html'):
        assert '/static/office-v2.js' in (ROOT/path).read_text()


def test_partial_workspace_event_never_displays_known_desk():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();page.set_content('<article></article>');page.evaluate('window.module={exports:{}}')
            page.add_script_tag(path=str(ROOT/'static/office-v2.js'))
            page.evaluate("""() => {window.ui=module.exports.mount(document,document.querySelector('article'),{});ui.render({roster:{state:'known',items:[{actor_id:'a',role:'judge'}]},events:{state:'partial',items:[{actor_id:'a',kind:'workspace',state:'partial',refs:{state:'known',notebook_text:'notes'}}]}})}""")
            assert page.locator('.coverage-body').text_content().find('Desk partial')>=0
            assert page.locator('.seat-state').inner_text()=='State unknown'
        finally:browser.close()


def test_real_browser_feed_is_bounded_collapsed_and_dm_is_above_it():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page(viewport={'width':900,'height':700})
            page.set_content('<article><div style="height:2000px">Legacy floor</div></article>')
            # Export mount without starting the production poller in this isolated DOM.
            page.evaluate("window.module={exports:{}}")
            page.add_script_tag(path=str(ROOT/'static/office-floor.js'))
            page.add_script_tag(path=str(ROOT/'static/office-v2.js'))
            page.evaluate(r'''() => {
              window.ui=module.exports.mount(document,document.querySelector('article'),{});
              window.feed={roster:{state:'known',items:[{actor_id:'head',role:'head',generation:1,incarnation:1}]},events:{state:'partial',limited:true,items:Array.from({length:64},(_,i)=>({event_id:'e'+i,actor_id:'head',kind:'tool_result',state:'known',text:'<img src=x onerror=alert(1)>\n'+('line\n'.repeat(100))}))}};
              ui.render(feed);
            }''')
            page.get_by_role('button',name='Chat with Head').click()
            page.get_by_role('dialog',name='Seat details').get_by_role('button',name='Reply',exact=True).click()
            dm=page.get_by_role('textbox',name='Operator message')
            feed=page.get_by_role('region',name='Floor timeline')
            assert dm.evaluate('e=>document.activeElement===e')
            assert feed.bounding_box()['height']<=500
            assert feed.locator('details').count()==64
            assert feed.locator('details[open]').count()==0
            assert page.locator('img').count()==0
            summary=feed.locator('details summary').first
            summary.focus();summary.press('Space')
            page.wait_for_function("document.querySelector('.floor-timeline details').open")
            dm.fill('Keep this draft')
            summary.focus()
            feed.evaluate('e=>e.scrollTop=100')
            page.evaluate('ui.render(feed)')
            assert feed.locator('details').first.get_attribute('open') is not None
            assert dm.input_value()=='Keep this draft'
            assert page.evaluate("document.activeElement.tagName")=='SUMMARY'
            assert feed.evaluate('e=>e.scrollTop')>0
            assert '<img src=x onerror=alert(1)>' in feed.locator('details pre').first.text_content()
            feed.focus()
            page.evaluate('ui.render(feed)')
            assert feed.evaluate('e=>document.activeElement===e')
        finally:
            browser.close()


def test_real_browser_role_error_coverage_and_desk_remain_readable():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page(viewport={'width':1000,'height':800});page.set_content('<article></article>');page.evaluate('window.module={exports:{}}')
            page.add_script_tag(path=str(ROOT/'static/office-v2.js'))
            page.evaluate("""() => {window.ui=module.exports.mount(document,document.querySelector('article'),{});ui.render({roster:{state:'known',items:[{actor_id:'opaque-id',role:'judge'}]},events:{state:'partial',items:[{actor_id:'opaque-id',kind:'tool_result',corrected_at:100,text:'{"reason":"sheet_too_large"}',refs:{tool:'sheet_read',is_error:true}}]},tasks:{state:'unknown',items:[]}})}""")
            assert page.locator('.floor-seat h3').inner_text()=='Judge'
            assert 'opaque-id' not in page.locator('.floor-seat').inner_text()
            summary=page.locator('.floor-chip summary').inner_text()
            assert 'Read a sheet' in summary and 'sheet too large' in summary and '00:01:40' in summary
            assert 'unknown' in page.locator('.coverage-body').text_content()
        finally:browser.close()


def test_standalone_page_removes_legacy_snapshots_and_keeps_current_floor():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content((ROOT/'sniper/web/templates/office.html').read_text())
            page.add_script_tag(path=str(ROOT/'static/office-v2.js'))
            legacy=page.locator('#legacy-office')
            assert legacy.count()==0
            assert 'Stored paper equity' not in page.locator('body').inner_text()
            assert page.locator('#office-v2-work').is_visible()
            page.evaluate("document.body.dataset.stale='true'")
            assert page.locator('#office-v2-work').evaluate('e=>getComputedStyle(e).opacity')=='1'
        finally:browser.close()
