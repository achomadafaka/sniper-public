""""Next in list" steps through the list the table shows (2026-10-08 review SF04).

stepWallet indexed `source`, ordered for the map metric, while the table showed its own filtered and
sorted `shown`; so after a sort or a filter, Next jumped to a wallet that was not the next row, or to
one the filter hid. Both now read one visible-row list. A hidden selection steps to the first (Next)
or last (Previous) visible row; with one visible row the steps are disabled.
"""
from test_wallet_universe_navigation import _draw, universe_page  # noqa: F401


def _rows(page):
    return page.locator('.wu-table tbody tr[data-uid]').evaluate_all('rs=>rs.map(r=>r.dataset.uid)')


def _selected(page):
    return page.locator('.wu-table tbody tr[aria-selected="true"]').get_attribute('data-uid')


def _sort_desc(page):
    page.locator('.wu-table [data-sort-key="wallet"]').click()
    page.locator('.wu-table [data-sort-key="wallet"]').click()


def test_next_and_previous_follow_the_sorted_table(universe_page):
    page, _ = universe_page
    _draw(page, view=None)
    _sort_desc(page)
    shown = _rows(page)
    page.locator('.wu-table tr[data-uid="B"] td[data-col="n"]').click()
    i = shown.index("B")
    page.locator('.inspector #next-wallet').click()
    assert _selected(page) == shown[(i + 1) % len(shown)], shown
    page.locator('.inspector #previous-wallet').click()
    page.locator('.inspector #previous-wallet').click()
    assert _selected(page) == shown[(i - 1) % len(shown)], shown


def test_next_never_lands_on_a_filtered_out_wallet(universe_page):
    page, _ = universe_page
    _draw(page, view=None)
    page.locator('#wu-f-measured').select_option('yes')
    shown = _rows(page)
    assert "C" not in shown and len(shown) == 2
    page.locator(f'.wu-table tr[data-uid="{shown[-1]}"] td[data-col="n"]').click()
    page.locator('.inspector #next-wallet').click()
    assert _selected(page) == shown[0]


def test_a_hidden_selection_steps_into_the_visible_list(universe_page):
    page, _ = universe_page
    _draw(page, view=None)
    page.locator('.wu-table tr[data-uid="C"] td[data-col="n"]').click()
    page.locator('#wu-f-measured').select_option('yes')
    shown = _rows(page)
    page.locator('.inspector #next-wallet').click()
    assert _selected(page) == shown[0]


def test_one_visible_wallet_disables_the_steps(universe_page):
    page, _ = universe_page
    _draw(page, view=None)
    page.locator('#wu-f-measured').select_option('no')
    assert _rows(page) == ["C"]
    page.locator('.wu-table tr[data-uid="C"] td[data-col="n"]').click()
    assert page.locator('.inspector #next-wallet').is_disabled()
    assert page.locator('.inspector #previous-wallet').is_disabled()


def test_the_position_counter_counts_the_visible_list(universe_page):
    page, _ = universe_page
    _draw(page, view=None)
    page.locator('#wu-f-measured').select_option('yes')
    shown = _rows(page)
    page.locator(f'.wu-table tr[data-uid="{shown[1]}"] td[data-col="n"]').click()
    assert page.locator('.inspector .wallet-steps span').inner_text() == "2 / 2 wallets"
