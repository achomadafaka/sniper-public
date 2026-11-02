"""Unfilled orders have reasons, never fabricated fill accounting."""
from playwright.sync_api import sync_playwright
from office.test_communication_ui import start


def test_censored_and_pending_orders_do_not_display_fill_amounts_in_either_book():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();start(page)
            page.evaluate("()=>{const items=[{order_id:'c',ticker:'CUM',state:'censored',reason:'entry_timeout'},{order_id:'p',ticker:'WAIT',state:'pending',reason:null}];ui.renderReport({state:'known',epoch_id:'office-v2-explore-1',orders:{state:'known',items},separate_books:[{book_id:'office-v2-explore-gpt-1',epoch_id:'office-v2-explore-gpt-1',state:'known',orders:items}]})}")
            page.get_by_role('button',name='Enlarge ledger').click()
            page.evaluate("document.querySelectorAll('.pixel-ledger details').forEach(e=>e.open=true)")
            for selector in ('.paper-orders tbody tr','.gpt-orders>p'):
                rows=page.locator(selector);assert rows.count()==2
                assert 'Not filled: entry wait timed out' in rows.nth(0).inner_text()
                assert 'Not filled yet: reason not recorded' in rows.nth(1).inner_text()
                for row in rows.all():
                    body=row.inner_text().lower()
                    assert not any(word in body for word in ('bought','sold','net unknown'))
        finally:b.close()


def test_modelled_unit_prices_and_market_caps_are_on_both_ledgers_and_compact_charts():
    from pathlib import Path
    from office.test_chart_rendering import mount
    root=Path(__file__).resolve().parents[2]
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();start(page)
            order=dict(order_id='priced',ticker='PRICE',state='closed',filled_at=90,entry_unit_price_usd=1.1e-9,exit_usd=2.2,tokens_atomic='1000000000000000000',decimals=9,entry_mcap_usd=1.1,exit_mcap_usd=2.2)
            page.evaluate("order=>ui.renderReport({state:'known',epoch_id:'office-v2-explore-1',orders:{state:'known',items:[order]},separate_books:[{book_id:'office-v2-explore-gpt-1',epoch_id:'office-v2-explore-gpt-1',state:'known',orders:[order]}]})",order)
            page.get_by_role('button',name='Enlarge ledger').click();page.evaluate("document.querySelectorAll('.pixel-ledger details').forEach(e=>e.open=true)")
            for selector in ('.paper-orders tbody tr','.gpt-orders>p'):
                label=page.locator(selector).locator('.order-prices');assert label.is_visible()
                assert all(s in label.inner_text() for s in ('Modelled entry: $0.0000000011/token','mcap $1.1','exit: $0.0000000022/token','mcap $2.2'))
            page2=b.new_page();mount(page2)
            page2.evaluate("o=>{Object.assign(snapshot.items[0],o,{status:'closed',closed_at:99});view.render(snapshot)}",order)
            assert 'Modelled entry: $0.0000000011/token' in page2.locator('.position-chart .order-prices').inner_text()
            assert page2.locator('.position-chart .order-prices').is_visible()
            page2.evaluate("Object.assign(snapshot.items[0],{status:'open',state:'known',entry_unit_price_usd:0,entry_mcap_usd:null,exit_mcap_usd:null});view.render(snapshot)")
            body=page2.locator('.order-prices').inner_text();assert 'entry: $0/token' in body and 'exit: not sold' in body and 'supply at entry not recorded' in body
            page2.evaluate("Object.assign(snapshot.items[0],{status:'closed',decimals:null,exit_unit_price_usd:null});view.render(snapshot)")
            assert 'token quantity or decimals not recorded' in page2.locator('.order-prices').inner_text()
        finally:b.close()


def test_price_formatter_preserves_zero_and_rejects_invalid_quantities():
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();page.add_script_tag(path=str(root/'static/office-order-display.js'))
            assert page.evaluate("OfficeOrderDisplay.prices({state:'censored'})")==''
            value=page.evaluate("OfficeOrderDisplay.prices({state:'closed',filled_at:0,entry_unit_price_usd:0,exit_unit_price_usd:0,entry_mcap_usd:0,exit_mcap_usd:0})")
            assert value.count('$0')==4 and 'unavailable' not in value
            for qty,dec in [('0',9),('-1',9),('1e18',9),('1000',None),('1000',19),('9'*81,9)]:
                value=page.evaluate("([qty,dec])=>OfficeOrderDisplay.prices({state:'closed',exit_usd:1,tokens_atomic:qty,decimals:dec})",[qty,dec])
                assert 'token quantity or decimals not recorded' in value and 'NaN' not in value and 'Infinity' not in value
        finally:b.close()


def test_recorded_reason_and_plan_are_literal_and_honest_in_both_books():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();start(page)
            items=[dict(order_id='planned',ticker='PLAN',state='open',entry_reason='<img src=x> evidence',plan=dict(thesis='<script>thesis</script>',disproof='Loss of demand',exit_plan='Recorded exit rule')),
                   dict(order_id='legacy',ticker='OLD',state='closed',entry_reason=None,plan=None),
                   dict(order_id='partial',ticker='PART',state='pending',entry_reason={'bad':'shape'},plan=dict(thesis='Partial thesis',disproof=0,exit_plan=None))]
            page.evaluate("items=>ui.renderReport({state:'known',epoch_id:'office-v2-explore-1',orders:{state:'known',items},separate_books:[{book_id:'office-v2-explore-gpt-1',epoch_id:'office-v2-explore-gpt-1',state:'known',orders:items}]})",items)
            page.get_by_role('button',name='Enlarge ledger').click();page.evaluate("document.querySelectorAll('.pixel-ledger details').forEach(e=>e.open=true)")
            for selector in ('.paper-orders tbody tr','.gpt-orders>p'):
                rows=page.locator(selector)
                assert rows.nth(0).locator('.order-plan').is_visible()
                body=rows.nth(0).inner_text()
                assert all(s in body for s in ('Entry reason: <img src=x> evidence','Thesis: <script>thesis</script>','Disproof: Loss of demand','Exit plan: Recorded exit rule'))
                assert 'Entry reason not recorded' in rows.nth(1).inner_text() and 'Plan not recorded' in rows.nth(1).inner_text()
                assert 'Thesis: Partial thesis' in rows.nth(2).inner_text() and 'Disproof: not recorded' in rows.nth(2).inner_text()
                assert 'Exit plan: not recorded' in rows.nth(2).inner_text() and '[object Object]' not in rows.nth(2).inner_text()
                assert rows.locator('img,script').count()==0
        finally:b.close()


def test_request_estimates_are_labelled_upper_biased_and_separate_from_strict():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();start(page)
            page.get_by_role('button',name='Enlarge ledger').click()
            for kind in ('estimated_at_request','estimated_terminal','strict','unexpected',None):
                order=dict(order_id='exit',ticker='EXIT',state='closed',exit_fill_kind=kind,modelled_net_usd=5)
                page.evaluate("o=>ui.renderReport({state:'known',epoch_id:'office-v2-explore-1',orders:{state:'known',items:[o]},separate_books:[{book_id:'office-v2-explore-gpt-1',epoch_id:'office-v2-explore-gpt-1',state:'known',orders:[o]}]})",order)
                page.evaluate("document.querySelectorAll('.pixel-ledger details').forEach(e=>e.open=true)")
                main=page.locator('.paper-orders tbody tr');gpt=page.locator('.gpt-orders>p')
                estimated=kind in ('estimated_at_request','estimated_terminal')
                for row in (main,gpt):
                    assert ('estimated · upper-biased' in row.inner_text()) is estimated
                assert ('Net +$5.00 (upper-biased)' in main.inner_text()) is estimated
                label=kind if kind in ('strict','estimated_at_request','estimated_terminal') else 'strict (closed before label)' if kind is None else 'unlabelled'
                assert 'Exit fill: '+label in main.inner_text()
        finally:b.close()
