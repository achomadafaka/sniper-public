"""The cockpit's shared fetchJson carries the CSRF token on every mutating memecoin call (2026-09-27).

The server now refuses every POST/PUT/PATCH/DELETE under /api/memecoin without X-CSRF-Token. Most UI
callers never sent one, so fetchJson attaches the last token any memecoin response carried, and on a
403 "csrf rejected" refreshes it from /api/memecoin/csrf-token and retries once, DELETE included.
Driven in Chromium against the real app.js.
"""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page()
        pg.route("**/*", lambda route: route.fulfill(status=200, body="")
                 if route.request.url == "http://sniper.test/" else route.abort())
        pg.goto("http://sniper.test/")
        source = APP_JS.read_text()
        marker = "  return {\n    state,\n"
        assert source.count(marker) == 1
        pg.add_script_tag(content=source.replace(marker, "  return {\n    fetchJson,\n    state,\n", 1)
                          + "\nwindow.__fetchJson = Sniper.fetchJson;")
        yield pg
        browser.close()


def _run(page, script):
    return page.evaluate("""async (script) => {
      const calls = [];
      let valid = 'T2';
      window.fetch = async (url, init) => {
        const method = (init && init.method) || 'GET';
        const token = (init && init.headers && init.headers['X-CSRF-Token']) || null;
        calls.push([method, url, token]);
        let status = 200, body = {ok: true};
        if (url === '/api/memecoin/holdings') body = {csrf_token: 'T1'};
        else if (url === '/api/memecoin/csrf-token') body = {csrf_token: valid};
        else if (method !== 'GET' && url.startsWith('/api/memecoin/') && token !== valid) {
          status = 403; body = {ok: false, error: 'csrf rejected'};
        }
        return new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
      };
      const f = window.__fetchJson;
      const out = await (new Function('f', 'return (async () => {' + script + '})()'))(f);
      return {calls, out};
    }""", script)


def test_a_cached_token_rides_on_calls_that_pass_none_and_a_stale_one_is_refreshed(page):
    r = _run(page, """
      await f('/api/memecoin/holdings');                                   // caches T1
      await f('/api/memecoin/wallets/u', {method: 'DELETE', suppressErrorToast: true});
      await f('/api/memecoin/real/kill', {method: 'POST', body: '{}', headers: {'X-CSRF-Token': ''}});
      await f('/api/memecoin/positions/1/close', {method: 'POST', body: '{}'});
      await f('/api/other/thing', {method: 'POST', body: '{}'});
      await f('/api/memecoin/holdings');
    """)
    assert r["calls"] == [
        ["GET", "/api/memecoin/holdings", None],
        ["DELETE", "/api/memecoin/wallets/u", "T1"],        # cached token attached
        ["GET", "/api/memecoin/csrf-token", None],          # stale: refreshed from the token route
        ["DELETE", "/api/memecoin/wallets/u", "T2"],        # and retried once, DELETE included
        ["POST", "/api/memecoin/real/kill", "T2"],          # '' from an unloaded _realStatus is replaced
        ["POST", "/api/memecoin/positions/1/close", "T2"],  # no extra round trip once cached
        ["POST", "/api/other/thing", None],                 # other APIs untouched
        ["GET", "/api/memecoin/holdings", None],            # GETs never carry it
    ], r["calls"]
