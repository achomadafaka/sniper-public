"""Room carries seat detail and operator chart monitors; mocked data only."""
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]


def test_room_monitor_and_sprite_panel_replace_external_card_strip():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page(viewport={'width':1440,'height':1100},reduced_motion='reduce');page.set_default_timeout(3000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.set_content('<article></article>');page.evaluate('window.module={exports:{}}')
            for name in ('vendor/lightweight-charts.standalone.production.js','office-charts.js','office-floor.js','office-v2.js'):page.add_script_tag(path=str(ROOT/'static'/name))
            page.evaluate('''()=>{window.sent=[];window.ui=module.exports.mount(document,document.querySelector('article'),{id:()=>"test",send:async b=>{sent.push(b);return {}}});window.feed={as_of:100,roster:{state:'known',items:[{actor_id:'h',role:'head'},{actor_id:'j',role:'judge'}]},seats:{state:'known',items:[{actor_id:'h',activity:{kind:'working',at:99},current_tool:'chain_transaction',last_thought:'<img src=x> Checking evidence',wakes_today:3,tokens_today:null,tokens_known:53000,tokens_unknown_wakes:1},{actor_id:'j',activity:{kind:'parked',at:99,reason:'quota_unknown'},last_thought:'Waiting'}]},events:{items:[]},mail:{state:'known',items:[]}};ui.render(feed);ui.renderCharts({state:'known',epoch_id:'office-v2-explore-1',as_of:100,open_count:1,items:[{order_id:'one',status:'open',ticker:'<img> COIN',filled_at:90,age_s:10,levels:{entry:0,stop:-30,host_peak:20},tape_segments:[[{time:90,value:0},{time:100,value:10}]],mark_segments:[],anchor:null,stats:{null_marks:1},model_fill_sol_per_token:0.01,model_fill_to_tape_factor:null}]})}''')
            game=page.get_by_role('region',name='Recorded pixel office')
            assert game.get_by_role('region',name='Office paper position charts').is_visible()
            assert page.locator('.floor-seat:visible').count()==0
            assert 'partial: no points rendered · series_empty · sent 0' in game.locator('.chart-badge').inner_text()
            assert 'Chart data unknown: source unavailable' in game.locator('.chart-empty').inner_text()
            assert 'Parked: quota unknown' in game.inner_text()
            game.get_by_role('button',name='Chat with Head').click()
            panel=page.get_by_role('dialog',name='Seat details')
            assert panel.is_visible() and 'Working now' in panel.inner_text()
            assert '53,000' in panel.inner_text() and 'Usage not captured for 1 wake' in panel.inner_text()
            assert 'Last tool:' in panel.inner_text() and panel.locator('img').count()==0
            panel.get_by_role('button',name='Close seat details').focus()
            page.evaluate('ui.render(feed)')
            assert panel.get_by_role('button',name='Close seat details').evaluate('e=>e===document.activeElement')
            page.evaluate("feed.roster.state='unknown';ui.render(feed)")
            assert 'State unknown' in panel.inner_text()
            page.evaluate("feed.roster.state='known';ui.render(feed)")
            panel.get_by_role('button',name='Reply',exact=True).click()
            message=page.get_by_role('textbox',name='Operator message')
            assert message.evaluate('e=>e===document.activeElement')
            assert page.get_by_role('combobox',name='Message recipient').input_value()=='h'
            message.fill('Preserve this draft')
            page.keyboard.press('Escape')
            game.get_by_role('button',name='Chat with Head').click()
            page.evaluate("feed.roster.items=feed.roster.items.filter(a=>a.actor_id!=='h');ui.render(feed)")
            assert not panel.is_visible()
            page.get_by_role('button',name='Open operator inbox').click()
            assert message.input_value()=='Preserve this draft'
            assert page.get_by_role('combobox',name='Message recipient').input_value()=='h'
            assert page.get_by_role('button',name='Send operator DM').is_disabled()
            page.keyboard.press('Escape')
            game.get_by_role('button',name='Enlarge chart').click()
            chart=page.get_by_role('dialog',name='Enlarged paper position chart')
            assert chart.is_visible() and '<img> COIN' in chart.inner_text() and chart.locator('img').count()==0
            assert page.evaluate('''()=>{const h=document.querySelector('.office-position-charts');h.style.position='absolute';h.style.top='-10000px';const visible=ui.chartsVisible();h.style.position='';h.style.top='';return visible}''')
            page.evaluate('ui.chartsUnavailable()')
            assert 'Charts unavailable; previous readings may be stale.' in chart.inner_text()
            assert 'stale · state unknown' in chart.locator('.chart-badge').inner_text()
            page.keyboard.press('Escape');assert not chart.is_visible()
            assert game.get_by_role('button',name='Enlarge chart').evaluate('e=>e===document.activeElement')
            game.get_by_role('button',name='Enlarge chart').click()
            page.evaluate('ui.renderCharts({state:"known",epoch_id:"office-v2-explore-1",as_of:101,open_count:0,items:[{order_id:"one",status:"closed",ticker:"COIN",filled_at:90,closed_at:100,levels:{entry:0},exit:{time:100,value:-10,reason:"trail"},tape_segments:[],mark_segments:[],stats:{}}]})')
            assert 'closed' in chart.locator('.chart-badge').inner_text()
            assert chart.get_by_text('Exploratory epoch. Modelled paper fills, no landing proof (b_unmeasured). Not evidence of skill. No inference.',exact=True).is_visible()
            page.evaluate('ui.renderCharts({state:"known",epoch_id:"office-v2-explore-1",as_of:101,open_count:0,items:[]})')
            assert not chart.is_visible()
            assert page.evaluate('sent.length')==0
            page.set_viewport_size({'width':390,'height':1100})
            page.wait_for_function('document.documentElement.scrollWidth<=innerWidth')
            assert not errors
        finally:browser.close()


def test_missing_canvas_keeps_accessible_seat_details_and_reply():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page();page.set_default_timeout(3000);page.set_content('<article></article>')
            page.evaluate('window.module={exports:{}};HTMLCanvasElement.prototype.getContext=()=>null')
            page.add_script_tag(path=str(ROOT/'static/office-floor.js'));page.add_script_tag(path=str(ROOT/'static/office-v2.js'))
            page.evaluate('''()=>{const ui=module.exports.mount(document,document.querySelector('article'),{});ui.render({as_of:100,roster:{state:'known',items:[{actor_id:'h',role:'head'}]},seats:{state:'known',items:[{actor_id:'h',activity:{kind:'parked',at:99,reason:'<img> quota'},last_thought:'No evidence'}]},events:{items:[]},mail:{items:[]}})}''')
            assert page.get_by_role('region',name='Exploratory paper book').is_visible()
            page.get_by_role('button',name='Chat with Head').click()
            panel=page.get_by_role('dialog',name='Seat details')
            assert 'Parked: <img> quota' in panel.inner_text() and panel.locator('img').count()==0
            panel.get_by_role('button',name='Reply',exact=True).click()
            assert page.get_by_role('textbox',name='Operator message').evaluate('e=>e===document.activeElement')
        finally:browser.close()
