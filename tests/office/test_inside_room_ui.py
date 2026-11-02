"""Room containment, chart wall and recorded-event seat feedback in real Chromium."""
from pathlib import Path
from playwright.sync_api import sync_playwright
from office.test_communication_ui import start
from office.test_chart_rendering import mount
ROOT=Path(__file__).resolve().parents[2]


def test_chart_wall_shows_all_returned_open_recent_and_both_book_identities():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page(viewport={'width':1440,'height':1000});mount(page)
            page.evaluate("snapshot.as_of=1000;snapshot.items=Array.from({length:8},(_,i)=>({order_id:i<2?'shared':String(i),book_id:i%2?'office-v2-explore-gpt-1':'office-v2-explore-1',state:'known',status:i<2?'open':'closed',closed_at:i<2?null:950,filled_at:900,ticker:'COIN '+i,levels:{entry:0},tape_segments:[[{time:900,value:0},{time:940,value:10}]],mark_segments:[],stats:{}}));view.render(snapshot)")
            cards=page.locator('.position-chart');assert cards.count()==8
            a,bbox=cards.nth(0).bounding_box(),cards.nth(1).bounding_box();assert abs(a['y']-bbox['y'])<3 and a['width']<400
            cards.nth(0).get_by_role('button',name='Enlarge chart').click()
            assert page.get_by_role('dialog',name='Enlarged paper position chart').locator('.chart-plot').bounding_box()['height']>250
        finally:b.close()


def test_every_working_surface_and_expanded_board_stays_inside_rendered_room():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();page.set_content('<div></div>');page.add_script_tag(path=str(ROOT/'static/wallet_universe.js'));page.add_script_tag(path=str(ROOT/'static/office-network.js'));page.evaluate("()=>{window.fetch=async()=>({ok:true,json:async()=>({state:'known',nodes:[],edges:[],coverage:[]})})}");start(page)
            for selector in ('.floor-book','.floor-dm','.floor-work','.floor-decisions','.floor-coverage','.office-network-room','.post-desk'):
                assert page.locator(selector).count()>0
                assert page.locator(selector).evaluate_all("es=>es.every(e=>!!e.closest('.pixel-scene'))")
            page.locator('.seat-overview button').first.click()
            assert page.get_by_role('dialog',name='Seat details').evaluate("e=>!!e.closest('.pixel-scene')")
            page.keyboard.press('Escape')
            for name in ('Enlarge ledger','Open operator inbox','Expand wallet universe'):
                page.get_by_role('button',name=name,exact=True).click()
                assert page.locator('dialog[open]').count()==1
                assert page.locator('dialog[open]').evaluate("e=>!!e.closest('.pixel-scene')")
                page.keyboard.press('Escape')
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        finally:b.close()


def test_seat_age_tool_sleep_deadline_and_pulse_follow_recorded_data_only():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();start(page)
            page.evaluate("feed.as_of=200;Object.assign(feed.seats.items[0],{last_event_id:'e1',last_event_at:190,last_event_kind:'tool_result',last_tool:'chain_transaction',tool_event_state:'returned'});feed.seats.items[0].activity={kind:'working',at:180};Object.assign(feed.seats.items[1],{last_event_id:'sleep1',last_event_at:195,last_event_kind:'lifecycle'});feed.seats.items[1].activity={kind:'sleeping',at:195,until:250};ui.render(feed)")
            head=page.locator('.seat-overview button').first
            assert '10s ago' in head.inner_text() and 'chain transaction' in head.inner_text().lower()
            assert '00:04:10 UTC' in page.locator('.seat-overview button').nth(1).inner_text()
            page.wait_for_function("document.querySelector('.seat-overview button').innerText.includes('11s ago')",timeout=2500)
            page.evaluate("feed.as_of=201;feed.seats.items[0].last_event_id='e2';feed.seats.items[0].last_event_at=200;feed.seats.items[0].last_event_kind='tool_call';ui.render(feed)")
            actor=page.locator('.pixel-seat[data-actor=head]');assert actor.get_attribute('data-pulsing')=='true'
            page.emulate_media(reduced_motion='reduce');page.wait_for_function("document.querySelector('.pixel-seat[data-actor=head]').dataset.pulsing==='false'")
            page.evaluate("feed.as_of=202;feed.seats.items[0].last_event_id='e3';feed.seats.items[0].last_event_at=201;ui.render(feed)")
            assert actor.get_attribute('data-pulsing')=='false'
        finally:b.close()
