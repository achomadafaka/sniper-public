"""A confirm dialog with form controls hands its values to the caller (2026-09-27, operator bug).

"Custom per wallet strategies don't work yet." confirmModal removed its backdrop BEFORE resolving,
so every caller that read its own inputs after `await confirmModal(...)` found nothing:
- the wallet exit-rails panel read #wexit-tp / #wexit-sl as null and said "No change", so no
  override was ever posted (web.log: exit-rails GETs, zero POSTs; override table empty);
- the gate-exemption panel read no ticked boxes, so Apply DEACTIVATED every current exemption.
Driven in Chromium against the real app.js; every network call is stubbed and recorded.
"""
import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page()
        pg.route("**/*", lambda route: route.fulfill(status=200, body="<body></body>")
                 if route.request.url == "http://sniper.test/" else route.abort())
        pg.goto("http://sniper.test/")
        source = APP_JS.read_text()
        marker = "  return {\n    state,\n"
        assert source.count(marker) == 1
        pg.add_script_tag(content=source.replace(marker, "  return {\n    memecoin,\n    state,\n", 1)
                          + "\nwindow.__memecoin = Sniper.memecoin;")
        yield pg
        browser.close()


# Stubs every call; records [method, path, body]. `script` opens a panel, fills its form and
# clicks the dialog's confirm button, all while the panel's own promise is pending.
_DRIVE = """async ({payloads, script}) => {
  window.__calls = [];
  window.fetch = async (input, init) => {
    const url = new URL(String(input), 'http://sniper.test');
    const key = url.pathname + url.search;
    const method = (init && init.method) || 'GET';
    window.__calls.push([method, key, init && init.body ? JSON.parse(init.body) : null]);
    const body = payloads[method + ' ' + key] || {ok: true, rows: []};
    return new Response(JSON.stringify(body), {status: 200,
                                               headers: {'Content-Type': 'application/json'}});
  };
  const mc = window.__memecoin;
  mc._chainFilter = 'solana';
  const waitFor = async (sel) => {
    for (let i = 0; i < 200; i++) {
      const el = document.querySelector(sel);
      if (el) return el;
      await new Promise(r => setTimeout(r, 5));
    }
    throw new Error('never rendered: ' + sel);
  };
  const run = (new Function('mc', 'waitFor', 'return (async () => {' + script + '})()'))(mc, waitFor);
  // A panel that never finishes fails here instead of hanging the suite.
  await Promise.race([run, new Promise((_, no) => setTimeout(
    () => no(new Error('panel never finished')), 3000))]);
  return window.__calls.filter(c => c[0] !== 'GET');
}"""


def test_saving_a_wallet_take_profit_posts_it(page):
    rails = {"default": {"policy_id": "follow-30-50-v2", "tp_pct": 25, "stop_pct": 35},
             "override": None, "effective": {"tp_pct": 25, "stop_pct": 35},
             "bounds": {"tp_pct": [5, 500], "stop_pct": [10, 80]}, "audit": [],
             "csrf_token": "T"}
    posts = page.evaluate(_DRIVE, {
        "payloads": {
            "GET /api/memecoin/wallets/U1/exit-rails?chain=solana": rails,
            "POST /api/memecoin/wallets/U1/exit-rails": {
                "ok": True, "source": "override", "effective": {"tp_pct": 15, "stop_pct": 35}},
        },
        "script": """
          const done = mc._walletExitRails({uid: 'U1', chain: 'solana', handle: 'limfork'});
          (await waitFor('#wexit-tp')).value = '15';
          document.querySelector('[data-confirm-role="ok"]').click();
          await done;
        """})
    assert [(m, path) for m, path, _b in posts] == [
        ("POST", "/api/memecoin/wallets/U1/exit-rails")], posts
    body = posts[0][2]
    assert body["tp_pct"] == 15 and body["stop_pct"] is None and body["chain"] == "solana", body


def test_applying_the_exemption_panel_keeps_a_ticked_exemption(page):
    """One exemption is active and stays ticked; the operator ticks a second and applies. Only the
    new one is written. Before the fix the ticked box was unreadable and both came back inactive."""
    posts = page.evaluate(_DRIVE, {
        "payloads": {
            "GET /api/memecoin/exemptible-gates": {"families": ["source_buy_floor",
                                                                 "reentry_cooldown"]},
            "GET /api/memecoin/armed?chain=solana": {"rows": [
                {"source_user_id": "U1", "gate_exemptions": ["reentry_cooldown"]}]},
        },
        "script": """
          const done = mc._walletExemptions({uid: 'U1', handle: 'limfork'});
          (await waitFor('[data-exempt-family="source_buy_floor"]')).checked = true;
          document.querySelector('[data-confirm-role="ok"]').click();
          await done;
        """})
    writes = [(b["gate_family"], b["active"]) for _m, _p, b in posts]
    assert writes == [("source_buy_floor", True)], json.dumps(posts)


def test_a_closed_dialog_leaves_no_backdrop_behind(page):
    """The backdrop is hidden at once and removed a tick later, so nothing lingers on screen."""
    left = page.evaluate("""async () => {
      window.fetch = async () => new Response('{}', {status: 200});
      const done = window.__memecoin._walletExitRails({uid: 'U2', chain: 'solana'});
      for (let i = 0; i < 200 && !document.querySelector('.confirm-backdrop'); i++)
        await new Promise(r => setTimeout(r, 5));
      document.querySelector('[data-confirm-role="cancel"]').click();
      await Promise.race([done, new Promise((_, no) => setTimeout(
        () => no(new Error('dialog never finished')), 3000))]);
      await new Promise(r => setTimeout(r, 20));
      return document.querySelectorAll('.confirm-backdrop').length;
    }""")
    assert left == 0
