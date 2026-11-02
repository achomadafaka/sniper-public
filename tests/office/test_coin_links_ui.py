"""Exact-mint links and sourced names in actual ledger/chart DOM; no external visits."""
from playwright.sync_api import sync_playwright
from office.test_communication_ui import start
from office.test_chart_rendering import mount,populated
MINT='3BSaLf3yCzW85CrfMHeBYDVYxSx8fywHQRHqGVaBpump'


def check_links(region):
    for label,prefix in [('GMGN','https://gmgn.ai/sol/token/'),('Dexscreener','https://dexscreener.com/solana/')]:
        link=region.get_by_role('link',name=label,exact=True)
        assert link.get_attribute('href')==prefix+MINT
        assert link.get_attribute('target')=='_blank'
        assert {'noopener','noreferrer'}<=set(link.get_attribute('rel').split())


def test_each_book_row_shows_sourced_name_and_safe_exact_mint_links():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();start(page)
            page.evaluate("mint=>{const order={order_id:'one',network_id:'solana',mint,display_name:'Funny Coin',ticker:'FUN',state:'open'};ui.renderReport({state:'known',epoch_id:'office-v2-explore-1',orders:{state:'known',items:[order]},separate_books:[{book_id:'office-v2-explore-gpt-1',epoch_id:'office-v2-explore-gpt-1',state:'known',orders:[order]}]})}",MINT)
            page.get_by_role('button',name='Enlarge ledger').click()
            for name in ('Exploratory paper book','Judge GPT paper ledger'):
                region=page.get_by_role('region',name=name)
                region.locator('details').first.locator('summary').first.click()
                assert 'Funny Coin' in region.inner_text()
                check_links(region)
        finally:b.close()


def test_chart_name_links_survive_enlarge_and_reject_malformed_mint():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();mount(page);populated(page)
            page.evaluate("mint=>{Object.assign(snapshot.items[0],{network_id:'solana',mint,display_name:'Funny Coin'});view.render(snapshot)}",MINT)
            card=page.locator('.position-chart');assert 'Funny Coin' in card.locator('h4').inner_text();check_links(card)
            card.get_by_role('button',name='Enlarge chart').click();check_links(page.get_by_role('dialog'))
            page.evaluate("snapshot.items[0].mint='javascript:alert(1)';view.render(snapshot)")
            assert card.get_by_role('link').count()==0
        finally:b.close()


def test_display_symbols_both_books_and_retained_rows_survive_omitted_metadata():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();start(page)
            page.evaluate("mint=>{const order={order_id:'one',network_id:'solana',mint,ticker:'OLD',display_ticker:'FUN',display_ticker_source:'x.token_meta',state:'closed'};ui.renderReport({state:'known',epoch_id:'office-v2-explore-1',orders:{state:'partial',count:8,items_omitted:7,recorded_fields_omitted:true,items:[order]},separate_books:[{book_id:'office-v2-explore-gpt-1',epoch_id:'office-v2-explore-gpt-1',state:'known',orders:[{...order,display_ticker:'ABCD…pump',display_ticker_source:'mint_short'}]}]})}",MINT)
            page.get_by_role('button',name='Enlarge ledger').click()
            book=page.get_by_role('region',name='Exploratory paper book');book.get_by_text('Recorded paper orders · partial',exact=True).click()
            assert 'FUN' in book.inner_text() and 'OLD' not in book.inner_text()
            assert '7 older rows omitted' in book.inner_text()
            assert book.locator('.paper-orders tbody tr').count()==1
            book.get_by_text('Details',exact=True).click()
            assert 'Recorded detail fields omitted by report limit' in book.inner_text()
            gpt=page.get_by_role('region',name='Judge GPT paper ledger');gpt.locator('summary').click()
            assert 'ABCD…pump' in gpt.inner_text() and 'ticker not found' in gpt.inner_text()
        finally:b.close()
