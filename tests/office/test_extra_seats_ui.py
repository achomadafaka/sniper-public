"""New role display and host-only notification markers; no seat/runtime actions."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright
from office.test_v2_feed import fixture,read,event
from sniper.office import seat_events
ROOT=Path(__file__).resolve().parents[2]


def test_notification_marker_is_whitelisted_and_raw_refs_never_export(tmp_path):
    conn=fixture(tmp_path)
    conn.execute("INSERT INTO office_dm_events(message_id,from_actor_id,from_generation,to_actor_id,to_generation,created_at,text,refs_json,receipt_id) VALUES('m','a',2,'operator',2,100,'Unverified case <img>',?,'seat:m')",(json.dumps(['notify_operator','<img src=x>']),))
    conn.execute("INSERT INTO office_v2_mail_bind VALUES('m','ep',3,3)")
    conn.commit();conn.close()
    mail=read(tmp_path)['mail']['items'][0]
    assert mail['notify_operator'] is True and 'refs_json' not in mail
    assert '<img src=x>' not in json.dumps(mail)


def test_new_role_state_files_are_fixed_whitelisted_paths(tmp_path):
    from sniper.office.floor_view import _host_state
    for role in ('researcher','forensics'):
        path=tmp_path/'v2'/('explore-'+role+'.json');path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(incarnation=1,planned_sleep_until=150)))
        import os
        os.utime(path,(100,100))
        seat=dict(epoch_id='office-v2-explore-1',actor_id=role,role=role,incarnation=1)
        assert _host_state(tmp_path,seat,200)['planned_sleep_until']==150


def test_five_roles_cards_sprites_and_notification_text_desktop_phone():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page(viewport={'width':1440,'height':1100},reduced_motion='reduce')
            page.set_content('<article></article>');page.evaluate('window.module={exports:{}}')
            page.add_script_tag(path=str(ROOT/'static/office-floor.js'));page.add_script_tag(path=str(ROOT/'static/office-v2.js'))
            page.evaluate('''() => {const roles=['head','judge','scout','researcher','forensics'];window.feed={as_of:100,roster:{state:'known',items:roles.map(role=>({actor_id:'a_'+role,role}))},seats:{state:'known',items:roles.map(role=>({actor_id:'a_'+role,activity:{kind:'working',at:90},current_tool:'book_view',last_thought:'Facts for '+role,wakes_today:1,tokens_today:0}))},events:{items:[]},mail:{state:'known',items:[{message_id:'alert',from_actor_id:'a_forensics',to_actor_id:'operator',created_at:99,text:'<img src=x> Unverified',notify_operator:true}]},questions:{state:'known',items:[]}};window.ui=module.exports.mount(document,document.querySelector('article'),{id:()=> 'id',send:async()=>({})});ui.render(feed)}''')
            assert page.locator('.floor-seat h3').all_inner_texts()==['Head','Judge','Scout','Researcher','Forensics']
            assert page.locator('.pixel-seat').evaluate_all('es=>es.every(e=>parseFloat(e.style.left)>0&&parseFloat(e.style.left)<100)')
            page.evaluate("feed.seats.items[3].current_tool='browse';feed.seats.items[4].current_tool='chain_transaction';ui.render(feed)")
            assert page.get_by_role('button',name='Chat with Researcher').get_attribute('data-station')=='browser'
            assert page.get_by_role('button',name='Chat with Forensics').get_attribute('data-station')=='map'
            page.get_by_role('button',name='Open operator inbox').click()
            inbox=page.get_by_role('region',name='Questions for the operator')
            assert 'Seat alert · unverified' in inbox.inner_text() and inbox.locator('img').count()==0
            inbox.get_by_role('button',name='Reply to Forensics').click()
            assert page.get_by_role('combobox',name='Message recipient').input_value()=='a_forensics'
            for width in (1440,390):
                page.set_viewport_size({'width':width,'height':1100})
                assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
                boxes=page.locator('.pixel-seat').evaluate_all('es=>es.map(e=>{let r=e.getBoundingClientRect();return {x:r.x,y:r.y}})')
                assert len({(round(b['x']),round(b['y'])) for b in boxes})==5
                if width==390:assert page.locator('.pixel-scene canvas').evaluate('c=>c.height')>=460
        finally:browser.close()


def test_notification_marker_rejects_unbounded_or_wrong_shape_metadata():
    from sniper.office.seat_feed import _notify_marker
    assert _notify_marker('[]') is False
    assert _notify_marker('["Notify_operator"]') is False
    for raw in ('[',json.dumps({'notify_operator':True}),json.dumps(['notify_operator']*65),'['*4097):
        assert _notify_marker(raw) is None
