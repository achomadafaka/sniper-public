"""Disposable chart math and source guards; never a provider or live order."""
import hashlib,json
import pytest
from office.test_ledger import policy
from sniper.office import position_charts as C


def row(**kw):
    frozen=policy(trail_pct=20).frozen()
    value=dict(order_id='one',epoch_id='office-v2-explore-1',book_kind='evaluated',network_id='solana',mint='M',pool='P',state='open',ticker='<img src=x>',decimals=0,filled_at=100.,closed_at=None,spend_lamports='1000000000',tokens_atomic='100',peak_lamports='1500000000',proceeds_lamports=None,exit_reason=None,policy_json=frozen,policy_hash=hashlib.sha256(frozen.encode()).hexdigest(),fill_proof=json.dumps({'pool':'P','proof':{'slot':10}}),exit_proof=None,entry_unit_price_usd=1.)
    value.update(kw);return value


def swap(at,slot=10,quote='1000000000',**kw):
    value=dict(pool='P',mint='M',quote_mint='So11111111111111111111111111111111111111112',slot=slot,tx_index=1,ordinal=0,signature='sig'+str(slot),block_time=at,joined_block_time=None,pre_base_reserve_atomic='100',pre_quote_reserve_atomic=quote,received_at=999999.)
    value.update(kw);return value


def test_tape_and_marks_have_independent_scales_host_peak_and_real_gaps():
    tape=[swap(99),swap(101,11,'2000000000'),swap(140,12,'1500000000')]
    marks=[dict(id=1,at=100.,lamports='1200000000',reason=None),dict(id=2,at=101.,lamports=None,reason='frontier_not_committed'),dict(id=3,at=102.,lamports='1100000000',reason=None)]
    out=C.project_order(row(),marks,tape,now=200.)
    assert out['anchor']['time']==99 and out['anchor']['slot']==10
    assert out['model_fill_sol_per_token']==.01 and out['model_fill_to_tape_factor']==1.
    assert out['levels']['host_peak']==50 and abs(out['levels']['trail']-20)<1e-8
    assert out['levels']['stop']==-30 and out['levels']['take_profit']==100
    assert len(out['tape_segments'])==2 and len(out['mark_segments'])==2
    assert out['mark_segments'][0][0]['value']==pytest.approx(20) and out['stats']['null_marks']==1
    assert out['tape_ended_at']==140


def test_missing_clock_is_not_receipt_time_and_future_anchor_is_refused():
    out=C.project_order(row(),[],[swap(None),swap(101,11)],now=200.)
    assert out['stats']['swaps_without_block_time']==1
    assert out['anchor'] is None and out['tape_segments']==[]
    assert out['reason']=='entry_not_on_tape'


def test_zero_mark_is_real_loss_and_closed_marker_is_modelled():
    out=C.project_order(row(state='closed',closed_at=160.,proceeds_lamports='1100000000',exit_reason='trail'),[dict(id=1,at=120.,lamports='0',reason=None)],[swap(99)],now=200.)
    assert out['mark_segments'][0][0]['value']==-100
    assert out['exit']['time']==160 and out['exit']['reason']=='trail'
    assert out['exit']['model_price_sol_per_token']==.011


def test_zero_tape_reserve_is_not_null_and_policy_mismatch_hides_rails():
    out=C.project_order(row(policy_hash='wrong'),[],[swap(99),swap(110,11,'0')],now=120.)
    assert out['tape_segments'][0][-1]['value']==-100
    assert out['policy_state']=='unknown' and 'stop' not in out['levels']


def test_missing_ledger_is_unknown_not_empty_and_memo_returns_independent_objects(tmp_path,monkeypatch):
    reader=C.OfficeCharts(tmp_path,monotonic=lambda:0.)
    assert reader.read(now=200.)['state']=='unknown'
    assert not list(tmp_path.rglob('*.db'))
    calls=[];reader=C.OfficeCharts(tmp_path,monotonic=lambda:0.)
    def load(*,now):calls.append(now);return dict(state='known',epoch_id=C.EPOCH,as_of=now,open_count=0,items=[])
    monkeypatch.setattr(reader,'_load',load)
    first=reader.read(now=200.);first['items'].append('foreign mutation')
    assert reader.read(now=201.)['items']==[] and calls==[200.]
    assert reader.read(now=199.)['reason']=='chart_clock_rolled_back'


def test_concurrent_reader_does_not_duplicate_or_wait_on_source(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    entered=threading.Event();finish=threading.Event();calls=[]
    reader=C.OfficeCharts(tmp_path,monotonic=lambda:0.)
    def load(*,now):
        calls.append(now);entered.set();assert finish.wait(2)
        return dict(state='known',epoch_id=C.EPOCH,as_of=now,open_count=0,items=[])
    monkeypatch.setattr(reader,'_load',load)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running=pool.submit(reader.read,now=200.);assert entered.wait(1)
        try:assert reader.read(now=200.)['reason']=='chart_read_in_progress'
        finally:finish.set()
        assert running.result()['state']=='known'
    assert calls==[200.]


def test_reader_uses_actual_producer_schema_and_readonly_handles(tmp_path,monkeypatch):
    import ast,sqlite3
    from pathlib import Path
    from contextlib import contextmanager
    from sniper.office import ledger as L
    from office.test_ledger import setup
    root=tmp_path/'data/office';ledger_path=root/'v2/explore-ledger/ledger.db'
    ledger=L.open_ledger(ledger_path);setup(ledger,C.EPOCH,epoch=C.EPOCH)
    order=row(book_id=C.EPOCH,decision_bundle_hash='d'*64,fill_model_version='office-fill-v1',requested_at=98.);order.pop('entry_unit_price_usd')
    ledger.execute('INSERT INTO orders('+','.join(order)+') VALUES('+','.join('?' for _ in order)+')',tuple(order.values()))
    ledger.execute("INSERT INTO marks(order_id,at,lamports,source) VALUES('one',100,'1000000000','fixture')");ledger.commit();ledger.close()
    # Read producer DDL via AST: importing its script would load .env at module scope.
    source=ast.parse((Path(__file__).resolve().parents[2]/'scripts/memecoin_desk_universe_worker.py').read_text())
    cls=next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=='DayFiles')
    ddl=next(ast.literal_eval(n.value) for n in cls.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='DDL' for t in n.targets))
    indexes=next(ast.literal_eval(n.value) for n in cls.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='INDEXES' for t in n.targets))
    path=tmp_path/'data/desk/pumpswap/tape.db';path.parent.mkdir(parents=True)
    tape=sqlite3.connect(path)
    for table in ('swaps_ps','pools'):
        tape.execute(ddl[table])
        for sql in indexes.get(table,()):tape.execute(sql)
    clock_path=tmp_path/'data/desk/blocks/1970-01-01.db';clock_path.parent.mkdir(parents=True)
    blocks=sqlite3.connect(clock_path);blocks.execute(ddl['blocks'])
    for at,slot,quote in [(99,10,'1000000000'),(110,11,'1500000000')]:
        tape.execute('INSERT INTO swaps_ps(pool,mint,quote_mint,slot,signature,ordinal,tx_index,pre_base_reserve_atomic,pre_quote_reserve_atomic,received_at) VALUES(?,?,?,?,?,0,1,?,?,?)',('P','M',C.WSOL,slot,'s'+str(slot),'100',quote,999999.))
        blocks.execute('INSERT INTO blocks VALUES(?,?,?)',(slot,at,999999.))
    tape.commit();tape.close();blocks.commit();blocks.close()
    original=C._ro;paths=[]
    @contextmanager
    def checked(path):
        with original(path) as conn:
            paths.append(str(path))
            with pytest.raises(sqlite3.OperationalError,match='readonly'):conn.execute('CREATE TABLE forbidden(x)')
            yield conn
    monkeypatch.setattr(C,'_ro',checked)
    out=C.OfficeCharts(root).read(now=200.)
    assert out['open_count'] is None and out['books_unavailable']==[C.GPT_EPOCH] and len(paths)==3
    item=out['items'][0]
    assert item['anchor']['time']==99 and item['anchor']['slot']==10
    assert item['tape_segments'][0][-1]['time']==110
    assert item['mark_binding_kind_counts']=={'not_recorded':1}


def test_chart_route_authenticates_before_clock_or_reader(tmp_path,monkeypatch):
    from flask import Flask
    from sniper.routes import api_office as A
    calls=[]
    class Reader:
        def __init__(self,root):pass
        def read(self,*,now):calls.append('read');return dict(state='unknown')
    monkeypatch.setattr(A,'OfficeCharts',Reader,raising=False)
    for auth in (False,True):
        app=Flask(__name__)
        def clock():calls.append('clock');return 200.
        app.register_blueprint(A.create_api_office_blueprint(dict(state_root=tmp_path,clock=clock,operator_authenticated=lambda:auth)))
        response=app.test_client().get('/api/office/positions/charts')
        assert response.status_code==(200 if auth else 401)
        if auth:assert response.headers['Cache-Control']=='no-store'
        else:assert calls==[]
    assert calls==['clock','read']


def test_vendored_browser_chart_literal_labels_close_transition_and_phone():
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    root=Path(__file__).resolve().parents[2]
    tape=[swap(99),swap(101,11,'2000000000'),swap(140,12,'1500000000')]
    marks=[dict(id=1,at=100.,lamports='1200000000',reason=None),dict(id=2,at=101.,lamports=None,reason='frontier_not_committed'),dict(id=3,at=102.,lamports='1100000000',reason=None)]
    payload=dict(state='partial',epoch_id=C.EPOCH,as_of=150.,open_count=1,items=[C.project_order(row(),marks,tape,now=150.)])
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=["--disable-gpu","--renderer-process-limit=2"])
        try:
            page=browser.new_page(viewport={'width':1200,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.set_content('<article></article>');page.add_script_tag(path=str(root/'static/vendor/lightweight-charts.standalone.production.js'))
            page.add_script_tag(path=str(root/'static/office-charts.js'))
            page.evaluate("() => {window.charts=OfficePositionCharts.mount(document,document.querySelector('article'),()=>{});}")
            page.evaluate('data=>charts.render(data)',payload)
            region=page.get_by_role('region',name='Office paper position charts')
            assert '<img src=x>' in region.inner_text() and region.locator('img').count()==0
            page.get_by_role('button',name='Enlarge chart').click()
            assert 'Recorded OHLC · USD/token · SOL volume' in region.inner_text()
            region.get_by_text('Source and coverage',exact=True).click()
            assert 'data/desk/pumpswap/tape.db' in region.inner_text()
            assert region.locator('canvas').count()>0
            closed=C.project_order(row(state='closed',closed_at=160.,proceeds_lamports='1100000000',exit_reason='<img> trail'),marks,tape,now=200.)
            page.evaluate('data=>charts.render(data)',dict(payload,as_of=200.,open_count=0,items=[closed]))
            assert 'Modelled exit' in region.inner_text() and '<img> trail' in region.inner_text()
            page.set_viewport_size({'width':390,'height':900})
            page.wait_for_function('document.documentElement.scrollWidth<=innerWidth')
            page.evaluate('data=>charts.render(data)',dict(payload,as_of=230.,open_count=0,items=[closed]))
            assert page.locator('.position-chart').count()==1  # API still returns this recent close.
            page.evaluate('data=>charts.render(data)',dict(payload,as_of=230.,open_count=0,items=[]))
            assert 'No open or recent paper positions.' in region.inner_text()
            template=(root/'sniper/web/templates/office.html').read_text().replace('<main class="layout"></main>','<article class="layout"></article>')
            import re
            page.set_content(re.sub(r'<script.*?</script>','',template,flags=re.S))
            page.evaluate('window.module={exports:{}}')
            page.add_script_tag(path=str(root/'static/office-floor.js'));page.add_script_tag(path=str(root/'static/office-v2.js'))
            page.evaluate('()=>window.floorUI=module.exports.mount(document,document.querySelector("article"),{id:()=>"fixture",send:async()=>({})})')
            page.evaluate('d=>floorUI.renderCharts(d)',payload)
            before=page.locator('.floor-stage > :first-child').bounding_box()['width']
            page.evaluate('d=>floorUI.renderCharts(d)',dict(payload,open_count=0,items=[]))
            assert page.locator('.floor-stage > :first-child').bounding_box()['width']==before  # New operator order keeps the room large even when empty.
            page.wait_for_function('document.documentElement.scrollWidth<=innerWidth')
            assert not errors
        finally:browser.close()


def test_dashboard_actual_script_order_loads_vendor_before_chart_mount():
    import re
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    root=Path(__file__).resolve().parents[2];template=(root/'templates/index.html').read_text()
    fragment=re.search(r'<section id="tab-desk".*?</section>',template,re.S)[0].replace('class="tab-panel"','class="tab-panel active"')
    wanted={'office-floor.js','office-charts.js','office-v2.js','lightweight-charts.standalone.production.js'}
    sources=[s for s in re.findall(r'<script[^>]+src="([^"]+)"',template) if s.split('?')[0].rsplit('/',1)[-1] in wanted]
    html=fragment+''.join('<script defer src="'+s+'"></script>' for s in sources)
    calls=[];payload=dict(state='known',epoch_id=C.EPOCH,as_of=150.,open_count=1,items=[C.project_order(row(),[dict(at=100.,lamports='1000000000')],[swap(99)],now=150.)])
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            def route(r):
                path=r.request.url.split('?')[0].split('office.test')[-1]
                if path=='/view':return r.fulfill(content_type='text/html',body=html)
                if path.endswith('.ttf'):return r.fulfill(content_type='font/ttf',path=str(root/path.lstrip('/')))
                if path.startswith('/static/'):return r.fulfill(content_type='text/javascript',body=(root/path.lstrip('/')).read_text())
                if path=='/api/office/positions/charts':calls.append(path);return r.fulfill(content_type='application/json',body=json.dumps(payload))
                return r.fulfill(status=503,content_type='application/json',body='{}')
            page.route('**/*',route);page.goto('http://office.test/view')
            page.wait_for_function("document.querySelectorAll('.chart-plot canvas').length>0",timeout=2000)
            assert calls and not errors
        finally:browser.close()


def test_charts_read_both_books_and_a_missing_second_book_is_not_empty(tmp_path):
    from sniper.office import ledger as L
    from office.test_ledger import setup
    root=tmp_path/'data/office'
    def book(path,epoch,order_id):
        ledger=L.open_ledger(root/path);setup(ledger,epoch,epoch=epoch)
        order=row(order_id=order_id,epoch_id=epoch,book_id=epoch,decision_bundle_hash='d'*64,fill_model_version='office-fill-v1',requested_at=98.);order.pop('entry_unit_price_usd')
        ledger.execute('INSERT INTO orders('+','.join(order)+') VALUES('+','.join('?' for _ in order)+')',tuple(order.values()))
        ledger.execute("INSERT INTO marks(order_id,at,lamports,source,binding_kind) VALUES(?,100,'1000000000','fixture',?)",(order_id,'carried' if epoch==C.EPOCH else 'estimated'))
        ledger.commit();ledger.close()
    book(C.BOOKS[0][1],C.EPOCH,'main')
    out=C.OfficeCharts(root).read(now=200.)
    assert out['books_unavailable']==[C.GPT_EPOCH] and out['state']=='partial' and [i['order_id'] for i in out['items']]==['main']
    assert out['items'][0]['mark_binding_kind_counts']=={'carried':1}
    book(C.BOOKS[1][1],C.GPT_EPOCH,'gpt')
    out=C.OfficeCharts(root).read(now=200.)
    assert out['books_unavailable']==[] and out['open_count']==2
    assert {i['order_id']:i['book_id'] for i in out['items']}=={'main':C.EPOCH,'gpt':C.GPT_EPOCH}
    assert all(i['status']=='open' and 'ticker_source' in i for i in out['items'])

    assert {i['book_id']:i['mark_binding_kind_counts'] for i in out['items']}=={C.EPOCH:{'carried':1},C.GPT_EPOCH:{'estimated':1}}

def test_recorded_ticker_is_the_display_ticker_and_absent_stays_unknown(tmp_path):
    from sniper.office import ledger as L
    from office.test_ledger import setup
    root=tmp_path/'data/office';ledger=L.open_ledger(root/C.BOOKS[0][1]);setup(ledger,C.EPOCH,epoch=C.EPOCH)
    for order_id,ticker,mint in (('a','ABC','M1'),('b',None,'MintAbcdefWxyz')):
        order=row(order_id=order_id,ticker=ticker,mint=mint,book_id=C.EPOCH,decision_bundle_hash='d'*64,fill_model_version='office-fill-v1',requested_at=98.);order.pop('entry_unit_price_usd')
        ledger.execute('INSERT INTO orders('+','.join(order)+') VALUES('+','.join('?' for _ in order)+')',tuple(order.values()))
    ledger.commit();ledger.close()
    items={i['order_id']:i for i in C.OfficeCharts(root).read(now=200.)['items']}
    assert items['a']['ticker']=='ABC' and items['a']['ticker_source']=='orders.ticker'
    assert items['b']['ticker']=='Mint…Wxyz' and items['b']['ticker_source']=='mint_short'


def test_candles_use_the_fill_basis_buckets_keep_gaps_and_unproven_rows_are_counted():
    v=json.dumps(dict(virtual_quote_atomic='1000',virtual_quote_reserves=1000,virtual_quote_source=C.__dict__.get('X','x'),ix_name='buy'))
    def s(t,slot,q,buy=1,proof=False):return dict(slot=slot,tx_index=1,ordinal=0,block_time=t,post_quote_reserve_atomic=str(q),post_base_reserve_atomic='1000',is_buy=buy,quote_atomic='1000000000',payload_json=None)
    out=C.candles([s(100,1,1000),s(102,2,3000,0),s(160,3,1000)],decimals=0,sol_usd=100.,sol_usd_at=1.,supply_tokens=10.,seed_virtual=1000)
    assert out['state']=='known' and [c[0] for c in out['5s']]==[100,160]      # 105..155 absent: a gap
    o,h,l,c=out['5s'][0][1:5]
    assert o==pytest.approx(2000/1000/1e9*100)
    assert h==pytest.approx(o*2) and c==h and l==o and out['5s'][0][5:]==[1.,1.,1,1]
    assert [c[0] for c in out['1m']]==[60,120]
    none=C.candles([s(100,1,1000)],decimals=0,sol_usd=100.,sol_usd_at=1.,supply_tokens=None)
    assert none['no_virtual_rows']==1 and none['5s']==[] and none['state']=='partial'
    assert C.candles([],decimals=None,sol_usd=100.,sol_usd_at=1.,supply_tokens=None)['state']=='unknown'


def test_price_levels_scale_the_fill_and_absent_stays_absent():
    item=dict(model_fill_sol_per_token=.01,levels=dict(stop=-30,take_profit=20,entry=0.),exit=dict(model_price_sol_per_token=.02))
    assert C.price_levels(item,100.)==dict(entry=1.,stop=pytest.approx(.7),take_profit=pytest.approx(1.2),exit=2.)
    assert C.price_levels(dict(),100.)=={} and C.price_levels(item,None)=={}


def test_candles_stop_at_the_cut_and_missing_volume_is_flagged():
    def s(t,slot,q='1000'):return dict(slot=slot,tx_index=1,ordinal=0,block_time=t,post_quote_reserve_atomic='1000',post_base_reserve_atomic='1000',is_buy=1,quote_atomic=q)
    out=C.candles([s(100,1),s(300,2)],decimals=0,sol_usd=1.,sol_usd_at=1.,supply_tokens=None,seed_virtual=1,now=200.)
    assert [c[0] for c in out['5s']]==[100] and out['state']=='known'
    out=C.candles([s(100,1,None)],decimals=0,sol_usd=1.,sol_usd_at=1.,supply_tokens=None,seed_virtual=1)
    assert out['state']=='partial' and out['unknown_volume_rows']==1


def test_slimming_keeps_every_order():
    out=C._slim(dict(items=[dict(candles={'5s':[[i] for i in range(300)],'1m':[]},tape_segments=[[1],[2]],mark_segments=[])]))
    assert len(out['items'][0]['candles']['5s'])==60 and out['items'][0]['candles_limited'] and out['items'][0]['tape_segments']==[[2]]
