"""Read-only Office graph contract; actual DOM and existing Wallet Map camera helpers."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]
A='A'*32;B='B'*32;C='C'*32;D='D'*32
BODY=dict(state='partial',as_of=100,truncated=True,note='Evidence, not control.',nodes=[dict(address=a,labels=[dict(label=k,source='seat',known_at=90)]) for a,k in [(A,'developer'),(B,'trader'),(C,'funder'),(D,'wallet')]],edges=[dict(edge_id=1,network_id='solana',src=A,dst=B,type='traded_with',source='seat',actor_id='scout',observed_at=None,known_at=90,proof_kind='receipt',proof_ref='<img src=x>',proof_state='claimed',method_version='v1',stale=True),dict(edge_id=2,network_id='solana',src=C,dst=A,type='funded_by',source='host',observed_at=80,known_at=90,proof_state='verified',stale=False)],coverage=[dict(address=A,check_name='trace',state='unknown',reason='timeout',at=90,fetch_id='f1')])


def mount(page,body=BODY,*,pending=False):
    page.set_default_timeout(3000);page.set_content('<main><div class="pixel-scene"><div class="pixel-world"></div></div></main>')
    page.add_script_tag(path=str(ROOT/'static/wallet_universe.js'))
    page.add_script_tag(path=str(ROOT/'static/office-network.js'))
    page.evaluate("()=>{WalletUniverse.mount=()=>{throw Error('authority renderer must not mount')};const fit=WalletUniverse.fitPoints;window.fitCalls=0;WalletUniverse.fitPoints=(...args)=>{fitCalls++;return fit(...args)}}")
    page.evaluate("v=>{window.body=v;window.calls=[];window.pending=[];window.fetch=async(url,options)=>{calls.push({url,method:options?.method||'GET'});return window.delay?new Promise(resolve=>pending.push(resolve)):{ok:true,json:async()=>body}}}",body)
    page.evaluate("v=>{window.delay=v;window.room=OfficeNetwork.mount(document,document.querySelector('.pixel-world'))}",pending)


def test_typed_network_inside_room_evidence_and_wallet_links_are_readonly():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page(viewport={'width':390,'height':844});mount(page)
            page.wait_for_selector('.network-node')
            assert page.locator('.pixel-world > .office-network-room').count()==1
            assert page.locator('.network-node').count()==4 and page.locator('.network-edge').count()==2
            assert page.locator('.network-edge').first.get_attribute('marker-end')
            page.get_by_role('combobox',name='Stored addresses').select_option(A)
            assert page.locator('.network-node[data-kind=developer]').count()==1
            assert page.evaluate('fitCalls')>0
            assert all(c['method']=='GET' and c['url'].startswith('/api/office/network?') for c in page.evaluate('calls'))
            page.get_by_role('button',name='Select '+A,exact=True).locator('circle').click()
            panel=page.get_by_role('region',name='Network selection')
            assert panel.get_by_role('link',name='Wallet',exact=True).get_attribute('href')=='https://solscan.io/account/'+A
            assert panel.get_by_role('link',name='GMGN',exact=True).get_attribute('href')=='https://gmgn.ai/sol/address/'+A
            assert 'unknown' in panel.inner_text() and 'timeout' in panel.inner_text()
            page.locator('.network-edge').first.click(force=True)
            assert panel.locator('h4').first.inner_text()=='traded with'
            assert all(x in panel.inner_text() for x in ('seat','claimed','not recorded','stale','<img src=x>'))
            assert page.locator('img').count()==0
            assert 'truncated' in page.get_by_role('status').first.inner_text().lower()
            before=page.locator('.network-world').get_attribute('transform');page.get_by_role('button',name='Zoom in',exact=True).click();assert page.locator('.network-world').get_attribute('transform')!=before
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not page.get_by_role('button',name='Arm',exact=True).count()
        finally:b.close()


def test_missing_store_is_unavailable_and_bad_seed_makes_no_request():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();mount(page,dict(state='unknown',reason='store_missing',nodes=[],edges=[],coverage=[]))
            page.wait_for_function("document.querySelector('.network-status').textContent.includes('store missing')")
            assert 'No relationships' not in page.locator('.network-status').inner_text()
            page.evaluate("room.render({state:'unknown',reason:'store_read_budget'})")
            assert 'store read budget' in page.locator('.network-status').inner_text()
            page.get_by_role('searchbox',name='Wallet or mint address').fill('javascript:alert(1)');page.get_by_role('button',name='Explore',exact=True).click()
            assert len(page.evaluate('calls'))==1
            assert page.get_by_role('link').count()==0
        finally:b.close()


def test_late_response_cannot_replace_newer_seed_or_hide_source_failure():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();mount(page,pending=True);page.wait_for_function('pending.length===1')
            page.get_by_role('searchbox',name='Wallet or mint address').fill(A);page.get_by_role('button',name='Explore',exact=True).click();page.wait_for_function('pending.length===2')
            newer=dict(BODY,nodes=[BODY['nodes'][0]],edges=[])
            page.evaluate("v=>pending[1]({ok:true,json:async()=>v})",newer);page.wait_for_selector('.network-node')
            page.evaluate("v=>pending[0]({ok:true,json:async()=>v})",BODY);assert page.locator('.network-node').count()==1
            before=page.locator('.network-world').evaluate(r"e=>Number(e.getAttribute('transform').match(/scale\(([^)]+)\)/)[1])")
            page.get_by_role('button',name='Zoom in',exact=True).click()
            after=page.locator('.network-world').evaluate(r"e=>Number(e.getAttribute('transform').match(/scale\(([^)]+)\)/)[1])")
            assert after>before
            page.evaluate("()=>{window.fetch=async()=>{throw Error('offline')}}");page.get_by_role('button',name='Refresh network',exact=True).click()
            page.wait_for_function("document.querySelector('.network-status').textContent.includes('stale')")
            assert page.locator('.network-node').count()==1
        finally:b.close()


def test_office_and_dashboard_wire_existing_camera_before_readonly_room():
    standalone=(ROOT/'sniper/web/templates/office.html').read_text()
    dashboard=(ROOT/'templates/index.html').read_text()
    for html in (standalone,dashboard):
        assert html.index('wallet_universe.js')<html.index('office-network.js')<html.index('office-v2.js')
    assert 'wallet_map.js' not in standalone
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            from office.test_communication_ui import start
            page=b.new_page();page.set_content('<div></div>');page.add_script_tag(path=str(ROOT/'static/wallet_universe.js'));page.add_script_tag(path=str(ROOT/'static/office-network.js'))
            page.evaluate("v=>{window.fetch=async()=>({ok:true,json:async()=>v})}",BODY)
            start(page)
            assert page.locator('.pixel-scene > .office-network-room').count()==1
        finally:b.close()


def test_unmatched_and_hostile_labels_stay_literal_and_wrong_chain_edges_are_omitted():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            body=json.loads(json.dumps(BODY));body['nodes'][0]['labels']=[dict(label='<img src=x onerror=alert(1)>',source='seat',known_at=None),dict(label={},source='seat')];body['edges'].append(dict(body['edges'][0],network_id='56'))
            page=b.new_page();mount(page,body);page.wait_for_selector('.network-node')
            assert page.locator('.network-node[data-kind=address]').count()==1
            assert '<img src=x onerror=alert(1)>' in page.get_by_role('region',name='Network selection').inner_text()
            assert page.locator('img').count()==0 and page.locator('.network-edge').count()==2
            assert 'invalid records omitted' in page.locator('.network-status').inner_text()
            page.evaluate("room.render({state:'known',nodes:[{address:'invalid'}],edges:[],coverage:[]})")
            assert 'No valid addresses in this response.' in page.get_by_role('region',name='Network selection').inner_text()
        finally:b.close()


def test_hidden_room_defers_its_first_read_until_visible():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();page.set_content('<div style="display:none" id="parent"></div>');page.add_script_tag(path=str(ROOT/'static/wallet_universe.js'));page.add_script_tag(path=str(ROOT/'static/office-network.js'))
            page.evaluate("v=>{window.calls=[];window.fetch=async(url,opts)=>{calls.push(url);return {ok:true,json:async()=>v}};OfficeNetwork.mount(document,document.querySelector('#parent'))}",BODY)
            page.wait_for_timeout(100);assert page.evaluate('calls.length')==0
            page.evaluate("document.querySelector('#parent').style.display='block'");page.wait_for_selector('.network-node');assert page.evaluate('calls.length')==1
        finally:b.close()


def test_network_caption_and_evidence_keep_aa_contrast_under_dashboard_css():
    from office.test_ledger_contrast_ui import CONTRAST
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();mount(page)
            page.add_style_tag(content='header p{color:#97a6b5}td{color:#eef2f6}')
            page.wait_for_selector('.network-node');page.locator('.network-evidence').evaluate('e=>e.open=true')
            samples=page.get_by_role('region',name='Wallet universe room').evaluate(CONTRAST)
            assert samples and not [s for s in samples if s['ratio']<4.5]
        finally:b.close()


def test_office_graph_assets_have_independent_mtime_versions(tmp_path,monkeypatch):
    import os
    from flask import Flask
    from sniper.routes import api_office as api
    assets=tmp_path/'static';assets.mkdir()
    for i,name in enumerate(('office-v2.js','office-communications.js','office-network.js','wallet_universe.js')):
        path=assets/name;path.write_text('');os.utime(path,(1700000000+i,1700000000+i))
    app=Flask(__name__,static_folder=str(assets));app.register_blueprint(api.create_api_office_blueprint(dict(state_root=tmp_path,clock=lambda:100,operator_authenticated=lambda:True)))
    monkeypatch.setattr(api,'render_template',lambda name,**kwargs:kwargs)
    first=app.test_client().get('/office').get_json()
    assert first['office_network_js']==1700000002 and first['wallet_universe_js']==1700000003
    os.utime(assets/'office-network.js',(1700000010,1700000010))
    second=app.test_client().get('/office').get_json()
    assert second['office_network_js']==1700000010 and second['office_v2_js']==first['office_v2_js']


def test_missing_camera_dependency_does_not_fetch_or_mount_authority_controls():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();page.set_content('<div id="room"></div>');page.add_script_tag(path=str(ROOT/'static/office-network.js'))
            page.evaluate("()=>{window.calls=[];window.fetch=async url=>{calls.push(url);return {ok:true,json:async()=>({state:'unknown'})}};OfficeNetwork.mount(document,document.querySelector('#room'))}")
            page.get_by_role('button',name='Refresh network',exact=True).click()
            assert 'renderer unavailable' in page.locator('.network-status').inner_text()
            assert page.evaluate('calls.length')==0
        finally:b.close()
