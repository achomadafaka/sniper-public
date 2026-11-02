import os
from pathlib import Path

import pytest
sync_api=pytest.importorskip('playwright.sync_api')
ROOT=Path(__file__).resolve().parents[2]
ROLES=['head','judge','scout','researcher','forensics','narrative']


@pytest.mark.parametrize('width',[1440,390])
def test_six_seats_all_render_inside_the_scene(width):
    with sync_api.sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page(viewport={'width':width,'height':1100},reduced_motion='reduce')
            page.set_content('<article></article>');page.evaluate('window.module={exports:{}}')
            page.add_script_tag(path=str(ROOT/'static/office-floor.js'));page.add_script_tag(path=str(ROOT/'static/office-v2.js'))
            page.evaluate('''roles=>{window.feed={as_of:100,roster:{state:'known',items:roles.map(role=>({actor_id:'a_'+role,role,epoch_id:'e',generation:1,incarnation:1}))},seats:{state:'known',items:roles.map(role=>({actor_id:'a_'+role,role,activity:{kind:'working',at:90},current_tool:'book_view',last_thought:'x',wakes_today:1,tokens_today:0}))},events:{items:[]},mail:{state:'known',items:[]},questions:{state:'known',items:[]}};window.ui=module.exports.mount(document,document.querySelector('article'),{id:()=>'id',send:async()=>({})});ui.render(feed)}''',ROLES)
            assert page.locator('.pixel-seat').count()==6
            boxes=page.locator('.pixel-seat').evaluate_all('es=>es.map(e=>[parseFloat(e.style.left),parseFloat(e.style.top)])')
            assert all(0<x<100 and 0<y<100 for x,y in boxes) and len({tuple(b) for b in boxes})==6
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
            out=os.environ.get('OFFICE_SHOT_DIR')
            if out:page.screenshot(path=str(Path(out)/('floor-%d.png'%width)))
        finally:browser.close()
