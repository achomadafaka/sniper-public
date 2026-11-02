"""Actual host event schema through the operator projection and room; no live store."""
import json,os
from pathlib import Path
import pytest
from sniper.office import seat_events as E
from office.test_v2_feed import fixture,read
ROOT=Path(__file__).resolve().parents[2]


def _parked(tmp_path):
    conn=fixture(tmp_path)
    conn.execute("UPDATE office_v2_roster SET role='researcher',epoch_id='office-v2-explore-1'");conn.commit()
    E.record_event(conn,epoch_id='office-v2-explore-1',actor_id='a',generation=2,incarnation=3,at=100.,kind='lifecycle',text='parked',refs=dict(reason='account_window_expired'))
    folder=tmp_path/'v2';folder.mkdir()
    state=folder/'explore-researcher.json';state.write_text(json.dumps(dict(incarnation=3,sleep_until=400.,parked_reason=None)));os.utime(state,(100.,100.))
    return conn


def _clear(conn,refs):
    E.record_event(conn,epoch_id='office-v2-explore-1',actor_id='a',generation=2,incarnation=3,at=150.,kind='lifecycle',text='unparked',refs=dict(was='account_window_expired',**refs))


@pytest.mark.parametrize('refs,kind,until',[(dict(sleep_until=300.),'sleeping',300.),({},'sleeping',400.),(dict(sleep_until=0.),'idle',None),(dict(sleep_until=190.),'idle',None)])
def test_clear_event_replaces_stale_park_without_inventing_work(tmp_path,refs,kind,until):
    conn=_parked(tmp_path)
    assert read(tmp_path)['seats']['items'][0]['activity']['kind']=='parked'
    _clear(conn,refs);conn.close()
    seat=read(tmp_path)['seats']['items'][0]
    assert seat['activity']==dict(kind=kind,until=until,reason=None,at=150.)
    assert seat['current_tool'] is None


@pytest.mark.parametrize('deadline,mode,label',[(300.,'sleeping','Sleeping until 00:05'),(0.,'idle','Idle · waiting for work')])
def test_room_clears_red_park_and_opens_current_details_without_send(tmp_path,deadline,mode,label):
    from playwright.sync_api import sync_playwright
    conn=_parked(tmp_path);before=read(tmp_path);_clear(conn,dict(sleep_until=deadline));conn.close();after=read(tmp_path)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page(reduced_motion='reduce');page.set_default_timeout(3000);page.set_content('<article></article>');page.evaluate('window.module={exports:{}}')
            page.add_script_tag(path=str(ROOT/'static/office-floor.js'));page.add_script_tag(path=str(ROOT/'static/office-v2.js'))
            page.evaluate('()=>{window.sent=[];window.ui=module.exports.mount(document,document.querySelector("article"),{id:()=>"test",send:async b=>sent.push(b)})}')
            page.evaluate('d=>ui.render(d)',before)
            sprite=page.get_by_role('button',name='Chat with Researcher')
            assert sprite.get_attribute('data-mode')=='parked'
            assert sprite.locator('.pixel-seat-status').is_visible()
            page.evaluate('d=>ui.render(d)',after)
            assert sprite.get_attribute('data-mode')==mode
            assert not sprite.locator('.pixel-seat-status').is_visible()
            sprite.click();panel=page.get_by_role('dialog',name='Seat details')
            assert label in panel.inner_text() and 'Parked:' not in panel.inner_text() and 'Working now' not in panel.inner_text()
            assert page.evaluate('sent.length')==0
        finally:browser.close()
