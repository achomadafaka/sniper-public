"""Real DOM proof of the at-a-glance floor; no network, model or DM writes."""
import subprocess
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]


def render(page):
    page.set_default_timeout(3000);page.set_content('<article></article>');page.evaluate('window.module={exports:{}}')
    page.add_script_tag(path=str(ROOT/'static/office-floor.js'));page.add_script_tag(path=str(ROOT/'static/office-v2.js'))
    page.evaluate('''() => {
      window.feed={as_of:120,roster:{state:'known',items:[{actor_id:'ag_head',role:'head'},{actor_id:'ag_judge',role:'judge'},{actor_id:'ag_scout',role:'scout'}]},
      seats:{state:'known',items:[{actor_id:'ag_head',activity:{kind:'working',at:100},current_tool:'book_view',last_thought:'**Reviewing the book**',wakes_today:2,tokens_today:340},
      {actor_id:'ag_judge',activity:{kind:'sleeping',at:100,until:180},last_thought:'Waiting for facts.',wakes_today:1,tokens_today:null,tokens_known:53000,tokens_unknown_wakes:1},
      {actor_id:'ag_scout',activity:{kind:'parked',at:100,reason:'account_unknown'},last_thought:'Source needs a check.',wakes_today:null,tokens_today:null}]},
      events:{state:'partial',limited:true,items:[{event_id:'h',actor_id:'ag_head',kind:'thought',state:'known',corrected_at:100,text:'**Reviewing the book**\\n- One fact\\n<img src=x onerror=alert(1)>'},
      {event_id:'s',actor_id:'ag_scout',kind:'thought',corrected_at:99,text:'**Found a lead**'},
      {event_id:'j',actor_id:'ag_judge',kind:'tool_result',corrected_at:101,text:'{"state":"submitted","reason":"known facts"}',refs:{tool:'paper_order',is_error:false}}]},tasks:{state:'unknown',items:[]},mail:{state:'known',items:[]}};
      window.sent=[];window.ui=module.exports.mount(document,document.querySelector('article'),{id:()=> 'stable',send:async body=>{sent.push(body);return {state:'sent',message_id:'m'}}});ui.render(feed);
    }''')


def test_three_named_sprite_panels_timeline_filters_and_safe_markdown_desktop_phone():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page(viewport={'width':1440,'height':1000});render(page)
            assert page.locator('.floor-seat').count()==3
            assert 'no task lists published' in page.locator('.coverage-body').text_content()
            assert page.locator('.floor-seat h3').all_inner_texts()==['Head','Judge','Scout']
            assert page.locator('.floor-seat:visible').count()==0
            assert page.get_by_role('button',name='Chat with Head').is_visible()
            page.get_by_role('button',name='Chat with Judge').click()
            panel=page.get_by_role('dialog',name='Seat details')
            assert '53,000' in panel.inner_text() and 'Usage not captured for 1 wake' in panel.inner_text()
            panel.get_by_role('button',name='Close seat details').click()
            assert 'ag_' not in page.locator('.floor-cards').inner_text()
            assert page.locator('.seat-state').all_inner_texts()==['Working now','Sleeping until 00:03:00 UTC','Parked: account unknown']
            assert page.locator('.seat-stats').first.inner_text().find('340')>=0
            assert '53,000' in page.locator('.seat-stats').nth(1).inner_text() and 'Usage not captured for 1 wake' in page.locator('.seat-stats').nth(1).inner_text()
            page.get_by_role('button',name='Open operator inbox',exact=True).click()
            timeline=page.get_by_role('region',name='Floor timeline')
            assert timeline.locator('time').all_inner_texts()==['00:01:41','00:01:40','00:01:39']
            assert timeline.locator('strong').count()==2 and timeline.locator('li').count()==1
            assert timeline.locator('img').count()==0 and timeline.locator('script').count()==0
            assert '<img src=x onerror=alert(1)>' in timeline.inner_text()
            page.get_by_role('combobox',name='Timeline view').select_option('thoughts')
            assert timeline.locator('.floor-note').count()==2 and timeline.locator('.floor-chip').count()==0
            page.get_by_role('combobox',name='Timeline seat').select_option('ag_head')
            assert timeline.locator('.floor-note').count()==1
            page.get_by_role('combobox',name='Timeline seat').select_option('all')
            page.get_by_role('combobox',name='Timeline view').select_option('decisions')
            assert timeline.locator('.floor-chip').count()==1 and 'submitted' in timeline.inner_text()
            page.set_viewport_size({'width':390,'height':1100})
            assert page.locator('.floor-seat:visible').count()==0
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            page.keyboard.press('Escape')
            page.get_by_role('button',name='Chat with Scout').click()
            page.get_by_role('dialog',name='Seat details').get_by_role('button',name='Reply',exact=True).click()
            page.get_by_role('textbox',name='Operator message').fill('A draft only')
            page.evaluate('ui.render(feed)')
            assert page.get_by_role('textbox',name='Operator message').input_value()=='A draft only'
            assert page.evaluate('sent.length')==0
        finally:browser.close()


def test_dm_draft_never_silently_retargets_when_a_seat_leaves_the_roster():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page();render(page)
            page.get_by_role('button',name='Chat with Head').click()
            page.get_by_role('dialog',name='Seat details').get_by_role('button',name='Reply',exact=True).click()
            page.get_by_role('textbox',name='Operator message').fill('Only for Head')
            page.evaluate("feed.roster.items=feed.roster.items.filter(a=>a.role!=='head');ui.render(feed)")
            assert page.get_by_role('combobox',name='Message recipient').input_value()=='ag_head'
            assert page.get_by_role('button',name='Send operator DM').is_disabled()
            assert page.get_by_role('textbox',name='Operator message').input_value()=='Only for Head'
            assert page.evaluate('sent.length')==0
        finally:browser.close()
