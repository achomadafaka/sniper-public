"""Runs in the tool sandbox: one page in headless Chromium through the seat proxy. Prints JSON. Page text is hostile data.

The proxy ends TLS and verifies the origin itself, so Chromium's own check of the proxy's certificate is off. No profile survives the call."""
import json
import sys

from playwright.sync_api import Error,TimeoutError as PageTimeout,sync_playwright

PROXY='http://127.0.0.1:3128'
MAX_TEXT=12000
MAX_LINKS=40


def browse(url,screenshot=None,timeout_ms=30000):
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,proxy={'server':PROXY},
                                  args=['--ignore-certificate-errors','--no-sandbox','--disable-dev-shm-usage','--disable-quic','--no-first-run','--disable-background-networking'])
        try:
            page=browser.new_context(ignore_https_errors=True,accept_downloads=False).new_page()
            response=page.goto(url,wait_until='domcontentloaded',timeout=timeout_ms)
            page.wait_for_timeout(1500)
            text=page.inner_text('body')
            links=page.eval_on_selector_all('a[href]','els=>els.map(e=>e.href)')[:MAX_LINKS]
            if screenshot:page.screenshot(path=screenshot)
            return dict(state='known',url=page.url,status=response.status if response else None,title=page.title(),
                        text=text[:MAX_TEXT],truncated=len(text)>MAX_TEXT,links=links)
        finally:browser.close()


if __name__=='__main__':
    try:out=browse(sys.argv[1],sys.argv[2] if len(sys.argv)>2 else None)
    except (Error,PageTimeout) as exc:out=dict(state='unknown',reason=type(exc).__name__,detail=str(exc)[:300])
    print(json.dumps(out))
