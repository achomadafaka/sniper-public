"""Regressions from the authenticated Office desktop/phone audit; no live writes."""
import pytest
from playwright.sync_api import sync_playwright, expect
from office.test_communication_ui import start


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_halt_banner_does_not_cover_seat_messages(width):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000}, reduced_motion="reduce")
            start(page)
            page.set_viewport_size({"width": width, "height": 1000})
            page.evaluate("ui.renderReport({state:'unknown',head_halt:{state:'known',halted:true,actor_id:'head',at:99,reason:'Hold for source evidence. '.repeat(30)}})")
            banner = page.get_by_role("region", name="Recorded paper entry halt")
            head = page.get_by_role("button", name="Expand Head message", exact=True)
            expect(head).to_be_visible()
            assert head.evaluate("e=>parseFloat(getComputedStyle(e).fontSize)") >= 16
            page.wait_for_function("document.querySelector('.pixel-speech').getBoundingClientRect().top >= document.querySelector('.pixel-entry-halt').getBoundingClientRect().bottom")
            assert banner.bounding_box()["y"] + banner.bounding_box()["height"] <= head.bounding_box()["y"]
            head.click()
            expect(page.get_by_role("dialog", name="Seat messages")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            browser.close()


def test_all_seat_states_are_visible_without_opening_each_desk():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            start(page)
            activity = page.get_by_role("region", name="Seat activity")
            expect(activity).to_be_visible()
            assert activity.get_by_role("button").count() == 7
            expect(activity.get_by_role("button", name="Head · Idle", exact=False)).to_be_visible()
            activity.get_by_role("button", name="Head · Idle", exact=False).click()
            expect(page.get_by_role("dialog", name="Seat details")).to_be_visible()
            page.keyboard.press("Escape")
            page.evaluate("ui.unavailable()")
            assert "Idle" not in activity.inner_text()
            assert activity.inner_text().count("State unknown") == 7
        finally:
            browser.close()


def test_enlarged_ledger_status_stays_readable_and_phone_columns_have_hint():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            start(page)
            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("ui.renderReport({state:'partial',epoch_id:'office-v2-explore-1',now:100,book:{},segments:{},orders:{state:'known',items:[]}})")
            page.get_by_role("button", name="Enlarge ledger").click()
            dialog = page.get_by_role("dialog", name="Enlarged paper ledger")
            assert dialog.locator('.book-report-status').evaluate("e=>getComputedStyle(e).color") == 'rgb(128, 74, 50)'
            dialog.get_by_text("Recorded paper orders · known", exact=True).click()
            expect(dialog.get_by_text("Scroll sideways for Sold, Net and Exit.", exact=True)).to_be_visible()
        finally:
            browser.close()
