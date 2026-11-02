"""One compact floor, scoped canvases and only recorded request/mail motion."""
from pathlib import Path
import re
import pytest
from playwright.sync_api import sync_playwright, expect
from office.test_communication_ui import start
ROOT=Path(__file__).resolve().parents[2]


def packed(page,width):
    page.add_script_tag(path=str(ROOT/'static/vendor/lightweight-charts.standalone.production.js'))
    page.add_script_tag(path=str(ROOT/'static/office-charts.js'))
    start(page)
    css=re.search(r'<style>(.*?)</style>',(ROOT/'sniper/web/templates/office.html').read_text(),re.S).group(1)
    page.add_style_tag(content=css)
    page.locator('article').first.evaluate("e=>e.className='layout'")
    page.set_viewport_size({'width':width,'height':1100})
    page.evaluate("ui.renderCharts({state:'partial',epoch_id:'office-v2-explore-1',as_of:110,open_count:1,items:[{order_id:'one',state:'partial',status:'open',ticker:'FUN',filled_at:90,levels:{entry:0},tape_segments:[[{time:90,value:0},{time:100,value:8}]],mark_segments:[[{time:95,value:-2},{time:105,value:3}]],stats:{}}]})")


@pytest.mark.parametrize('width',[1920,390])
def test_rooms_share_the_floor_without_growing_the_seat_canvas(width):
    with sync_playwright() as p:
        browser=p.chromium.launch()
        try:
            page=browser.new_page(reduced_motion='reduce');packed(page,width)
            room=page.locator('.pixel-scene');desks=room.locator('.desk-room')
            expect(desks).to_have_count(1)
            page.wait_for_function("document.querySelector('.desk-room').getBoundingClientRect().height>100")
            assert page.locator('.chart-plot canvas').nth(1).evaluate('e=>getComputedStyle(e).imageRendering')=='auto'
            assert room.locator('.wall-screens').count()==1 and room.locator('.pixel-ledger').count()==1 and room.locator('.post-desk').count()==1
            if width>1000:
                assert room.bounding_box()['width']>width*.95
                tops=[page.locator(s).bounding_box()['y'] for s in ('.desk-room','.wall-screens')]
                assert max(tops)-min(tops)<35
                for sel in ('.pixel-ledger','.post-desk'):
                    board=page.locator(sel).bounding_box();floor=room.bounding_box()
                    assert board['width']>=250 and board['y']+board['height']<=floor['y']+floor['height']
            assert desks.bounding_box()['height']<750
            before=desks.bounding_box()['height']
            page.evaluate("document.querySelector('.inbox-items').appendChild(document.createElement('p')).textContent='retained '.repeat(2000)")
            assert abs(desks.bounding_box()['height']-before)<2
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        finally:browser.close()


def test_new_request_flies_once_to_operator_and_reduced_motion_stops_it():
    with sync_playwright() as p:
        browser=p.chromium.launch()
        try:
            page=browser.new_page(reduced_motion='no-preference');packed(page,1440)
            assert page.locator('.mail-sprite').count()==0
            page.evaluate("feed.questions.items.push({actor_id:'head',corrected_at:100,text:'Missing event identity'});ui.render(feed)")
            assert page.locator('.mail-sprite').count()==0
            page.evaluate("feed.questions.items=[{event_id:'future',actor_id:'head',corrected_at:101,text:'Future'}];ui.render(feed)")
            assert page.locator('.mail-sprite').count()==0
            page.evaluate("feed.questions.state='unknown';feed.questions.items=[{event_id:'unknown',actor_id:'head',corrected_at:100,text:'Unknown source'}];ui.render(feed)")
            assert page.locator('.mail-sprite').count()==0
            page.evaluate("feed.questions.state='known';feed.questions.items=[]")
            page.evaluate("feed.questions.items.push({event_id:'request-real',actor_id:'head',corrected_at:100,text:'Recorded operator request'});ui.render(feed)")
            expect(page.locator('.mail-sprite[data-message-id="request:request-real"]')).to_have_count(1)
            page.evaluate('ui.render(feed)')
            assert page.locator('.mail-sprite').count()==1
            page.emulate_media(reduced_motion='reduce')
            expect(page.locator('.mail-sprite')).to_have_count(0)
            page.evaluate("feed.questions.items.push({event_id:'future',actor_id:'head',corrected_at:101,text:'Future'});ui.render(feed)")
            assert page.locator('.mail-sprite').count()==0
        finally:browser.close()
