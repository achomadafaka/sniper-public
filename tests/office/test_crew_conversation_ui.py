from playwright.sync_api import sync_playwright
from office.test_ceo_chat_ui import start


def test_role_crew_mail_is_excluded_from_seat_only_conversation():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page();start(page)
            page.evaluate("feed.mail.items.push({message_id:'crew-role',from_actor_id:'crew:lambda',to_actor_id:'h',created_at:20,text:'Crew boundary witness'});ui.render(feed)")
            page.get_by_role('combobox',name='Conversation type').select_option('crew')
            thread=page.get_by_role('log',name='Conversation')
            assert 'Crew boundary witness' in thread.inner_text()
            page.get_by_role('combobox',name='Conversation type').select_option('seats')
            assert 'Crew boundary witness' not in thread.inner_text()
        finally:browser.close()
