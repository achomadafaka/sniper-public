from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "static" / "app.js"
STYLES_CSS = ROOT / "static" / "styles.css"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def memecoin_page(browser):
    page = browser.new_page(viewport={"width": 1365, "height": 768})
    page.route("**/*", lambda route: route.fulfill(status=200, body="")
               if route.request.url == "http://sniper.test/" else route.abort())
    page.goto("http://sniper.test/")
    page.set_content(
        f"""
        <style>{STYLES_CSS.read_text()}</style>
        <div class="content-area"></div>
        <section id="tab-memecoin">
          <div id="memecoin-board">
            <div id="mc-chain-filter"></div>
            <span id="mc-armed-chain"></span>
            <span id="mc-armed-meta"></span>
            <div id="mc-armed" class="table-wrapper"></div>
            <div id="mc-stats"></div>
            <div id="mc-real-buy"></div>
            <div id="mc-refusal-slots"></div>
            <div id="mc-money"></div>
            <div id="mc-real" class="table-wrapper"></div>
            <div id="mc-closed" class="table-wrapper"></div>
            <div id="mc-traders" class="table-wrapper"></div>
            <div id="mc-activity" class="table-wrapper"></div>
          </div>
        </section>
        """
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    source = APP_JS.read_text().replace(
        "  return {\n    state,\n",
        "  return {\n    memecoin,\n    systemStats,\n    operator,\n    shellCluster,\n    state,\n",
        1,
    )
    page.add_script_tag(
        content=source
        + "\nwindow.__memecoin = Sniper.memecoin;"
        + "\nwindow.__systemStats = Sniper.systemStats;"
        + "\nwindow.__operator = Sniper.operator;"
        + "\nwindow.__shellCluster = Sniper.shellCluster;"
    )
    yield page, errors
    page.close()


def _set_api_payloads(page, payloads):
    page.evaluate(
        """
        payloads => {
          window.__apiPayloads = payloads;
          window.fetch = async input => {
            const url = new URL(String(input), 'http://sniper.test');
            const key = url.pathname + url.search;
            if (!(key in window.__apiPayloads)) {
              return new Response(JSON.stringify({error: `missing test payload: ${key}`}), {
                status: 404,
                headers: {'Content-Type': 'application/json'},
              });
            }
            return new Response(JSON.stringify(window.__apiPayloads[key]), {
              status: 200,
              headers: {'Content-Type': 'application/json'},
            });
          };
        }
        """,
        payloads,
    )


def _record_api_calls(page, payloads, delayed_paths=None):
    page.evaluate(
        """
        ({payloads, delayedPaths}) => {
          window.__apiCalls = [];
          window.__apiPayloads = payloads;
          window.fetch = async input => {
            const url = new URL(String(input), 'http://sniper.test');
            const key = url.pathname + url.search;
            window.__apiCalls.push(key);
            if (delayedPaths.includes(key)) {
              await new Promise(resolve => setTimeout(resolve, 20));
            }
            if (!(key in window.__apiPayloads)) {
              return new Response(JSON.stringify({error: `missing test payload: ${key}`}), {
                status: 404,
                headers: {'Content-Type': 'application/json'},
              });
            }
            return new Response(JSON.stringify(window.__apiPayloads[key]), {
              status: 200,
              headers: {'Content-Type': 'application/json'},
            });
          };
        }
        """,
        {"payloads": payloads, "delayedPaths": delayed_paths or []},
    )


def test_the_research_workspace_is_gone_and_nothing_calls_it(memecoin_page):
    """Operator 2026-10-02: the Research section is archived (Wallet Map is the research now). No nav
    button, no subtab, no section, and no request to its old route."""
    page, errors = memecoin_page
    asked = []
    page.on("request", lambda r: asked.append(r.url))
    page.evaluate("""async () => {
      const mc = window.__memecoin; mc._loadHealth = () => {};
      await mc.load();
      clearInterval(mc._timer); clearInterval(mc._tickerTimer); mc._timer = null; mc._tickerTimer = null;
    }""")
    assert page.locator('[data-workspace-nav="research"]').count() == 0
    assert page.locator('[data-workspace="research"]').count() == 0
    assert page.locator('#mc-research').count() == 0
    assert not any("research-workspace" in u for u in asked)
    assert errors == []


def test_real_card_names_swap_and_provider_price_sources(memecoin_page):
    page, errors = memecoin_page
    shown = page.evaluate("""() => ({
      swap: window.__memecoin._marketMidCell(0.00012, 'market', 'fastlane_sol_swap'),
      dex: window.__memecoin._marketMidCell(0.00010, 'market', 'fastlane_mid'),
      route: window.__memecoin._marketMidCell(0.00009, 'route', null),
    })""")
    assert "swap" in shown["swap"]
    assert "DexScreener" in shown["dex"]
    assert "route" in shown["route"]
    assert not errors


def test_exit_intent_retry_clears_busy_state_and_unblocks_polling(memecoin_page):
    page, errors = memecoin_page
    result = page.evaluate("""async () => {
      const mc = window.__memecoin, calls = [];
      const intents = [{state: 'queued', attempts: 1}, {state: 'done', result_status: 'closed'}];
      mc._subtab = 'open'; mc._lastScrollTs = 0;
      window.fetch = async (input, init = {}) => {
        calls.push({url: String(input), method: init.method || 'GET'});
        return new Response(JSON.stringify({intent: intents.shift()}), {status: 200});
      };
      mc._mcSetBusy(71432, true, 'Closing…');
      let intent;
      try { intent = await mc._awaitExitIntent('retry-test', 'REF'); }
      finally { mc._mcSetBusy(71432, false); }
      return {state: intent.state, pending: [...mc._mcPending], blocked: mc._pollBlocked(), calls};
    }""")
    assert result['state'] == 'done'
    assert result['calls'] == [
        {'url': '/api/memecoin/real/exit_intent/retry-test', 'method': 'GET'},
        {'url': '/api/memecoin/real/exit_intent/retry-test', 'method': 'GET'},
    ]
    assert result['pending'] == []
    assert result['blocked'] is False
    assert errors == []


def test_real_card_ages_market_mark_separately_from_sell_value(memecoin_page):
    page, errors = memecoin_page
    html = page.evaluate("""() => window.__memecoin._realFactsLine({
      market_mid_basis: 'market', market_mid_source: 'fastlane_sol_swap',
      market_mid_at: '2026-08-12T16:53:20Z',
      selected_exit_at: '2026-08-12T16:53:22Z',
    })""")
    assert 'fastlane_sol_swap' not in html
    assert 'swap' in html
    assert 'data-observation-at="2026-08-12T16:53:20Z"' in html
    assert 'data-observation-at="2026-08-12T16:53:22Z"' not in html
    assert not errors


def test_long_hold_card_survives_active_ticker_and_keeps_manual_sell(memecoin_page):
    page, errors = memecoin_page
    html = page.evaluate("""() => {
      document.body.insertAdjacentHTML('beforeend', '<div id="mc-real"></div>');
      const mc = window.__memecoin;
      const p = {id: 91, mint: 'MINT', ticker: 'COIN', network_id: 'solana',
        venue: 'SOL', entry_notional: 25, tracking_paused_at: '2026-09-26T12:00:00Z'};
      mc._realPositions = [p];
      document.getElementById('mc-real').innerHTML = mc._longHoldCard(p);
      mc._applyRealTicker({positions: [], as_of: '2026-09-26T12:00:01Z'});
      return document.getElementById('mc-real').innerHTML;
    }""")
    assert 'Refresh price' in html and 'Resume live' in html and 'Sell all' in html
    assert 'data-long-hold-price="91"' in html
    assert 'data-real-ticker' not in html
    assert not errors


def test_health_displays_untrusted_source_clock(memecoin_page):
    page, errors = memecoin_page
    page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div id=mc-status></div>')")
    _set_api_payloads(page, {
        "/api/memecoin/health": {"runner_alive": True, "auth_ok": True,
            "feed_lag_status": "untrusted_source_clock", "last_event_age_s": 12},
        "/api/memecoin/real/status": {},
    })
    page.evaluate("window.__memecoin._loadHealth()")
    status = page.locator("#mc-status").inner_text()
    assert "source clock untrusted" in status
    assert "Last event: 12s ago" in status
    assert "runner live" in status
    assert errors == []


def test_real_status_reuses_one_request_across_the_same_refresh(memecoin_page):
    page, errors = memecoin_page
    _record_api_calls(
        page,
        {"/api/memecoin/real/status": {"armed": True}},
        ["/api/memecoin/real/status"],
    )

    values = page.evaluate(
        """
        async () => {
          const concurrent = await Promise.all([
            window.__memecoin._fetchRealStatus(false),
            window.__memecoin._fetchRealStatus(false),
          ]);
          const sequential = await window.__memecoin._fetchRealStatus(false);
          return [...concurrent, sequential];
        }
        """
    )

    calls = page.evaluate("window.__apiCalls")
    assert calls.count("/api/memecoin/real/status") == 1
    assert values == [{"armed": True}, {"armed": True}, {"armed": True}]
    assert errors == []


def test_missing_account_balance_retries_before_the_normal_accounting_ttl(memecoin_page):
    """A failed balance read must not leave the manual Robinhood sizes blank for one minute."""
    page, errors = memecoin_page
    payloads = {
        "/api/memecoin/real/status": {"armed": True, "venues": {}, "knobs": {}},
        "/api/memecoin/real/positions": {"positions": []},
        "/api/memecoin/real/closed": {"closed": []},
        "/api/memecoin/stuck-bags": {"rows": []},
        "/api/memecoin/engine-cockpit": {"positions": []},
    }
    _set_api_payloads(page, payloads)

    result = page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          const baseFetch = window.fetch;
          let accountingCalls = 0;
          window.fetch = async input => {
            const url = new URL(String(input), 'http://sniper.test');
            if (url.pathname !== '/api/memecoin/real/accounting') return baseFetch(input);
            accountingCalls += 1;
            const cash = accountingCalls === 1 ? null : 207.12;
            const presets = cash == null ? {} : {'0.13': 30.95, '0.26': 61.90};
            return new Response(JSON.stringify({
              by_network: {4663: {cash, manual_presets: presets}},
              slots: {used: 0, max: 2}, allowlist_by_chain: {}, totals: {},
            }), {status: 200, headers: {'Content-Type': 'application/json'}});
          };
          mc._loadV6Paper = () => {};
          await mc._loadReal();
          await new Promise(resolve => setTimeout(resolve, 20));
          mc._acctTs -= 6000;
          await mc._loadReal();
          await new Promise(resolve => setTimeout(resolve, 20));
          return {accountingCalls, cash: mc._acct.by_network['4663'].cash,
                  preset: mc._acct.by_network['4663'].manual_presets['0.13']};
        }
        """
    )

    assert result == {"accountingCalls": 2, "cash": 207.12, "preset": 30.95}
    assert errors == []


def test_filled_reservation_refreshes_accounting_before_the_normal_ttl(memecoin_page):
    page, errors = memecoin_page
    payloads = {
        "/api/memecoin/real/positions": {"positions": []},
        "/api/memecoin/real/closed": {"closed": []},
        "/api/memecoin/stuck-bags": {"rows": []},
        "/api/memecoin/engine-cockpit": {"positions": []},
    }
    _set_api_payloads(page, payloads)

    result = page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          const baseFetch = window.fetch;
          let filled = false;
          let accountingCalls = 0;
          window.fetch = async input => {
            const url = new URL(String(input), 'http://sniper.test');
            if (url.pathname === '/api/memecoin/real/status') {
              const book = filled
                ? {reservations: {count: 0, notional: 0},
                   real_open: {count: 1, notional: 139.70}}
                : {reservations: {count: 1, notional: 139.70},
                   real_open: {count: 0, notional: 0}};
              return new Response(JSON.stringify({armed: true, venues: {}, knobs: {}, ...book}), {
                status: 200, headers: {'Content-Type': 'application/json'},
              });
            }
            if (url.pathname === '/api/memecoin/real/accounting') {
              accountingCalls += 1;
              const reserved = filled ? 0 : 139.70;
              return new Response(JSON.stringify({
                by_network: {4663: {cash: 160.15, deployed: 139.70, reserved,
                                    open_n: filled ? 1 : 0, reserved_n: filled ? 0 : 1}},
                slots: {used: 1, max: 2}, allowlist_by_chain: {}, totals: {},
              }), {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            return baseFetch(input);
          };
          mc._loadV6Paper = () => {};
          await mc._loadReal();
          await new Promise(resolve => setTimeout(resolve, 20));
          filled = true;
          mc._realStatusTs = 0;
          await mc._loadReal();
          await new Promise(resolve => setTimeout(resolve, 20));
          return {accountingCalls, accounting: mc._acct.by_network['4663']};
        }
        """
    )

    assert result["accountingCalls"] == 2
    assert result["accounting"]["deployed"] == 139.70
    assert result["accounting"]["reserved"] == 0
    assert result["accounting"]["open_n"] == 1
    assert errors == []


def test_real_cockpit_keeps_live_reads_fast_and_bounds_history_reads(memecoin_page):
    page, errors = memecoin_page
    payloads = {
        "/api/memecoin/real/status": {"armed": True},
        "/api/memecoin/real/positions": {"positions": []},
        "/api/memecoin/real/closed": {"closed": []},
        "/api/memecoin/real/incoming": {"rows": []},
        "/api/memecoin/stuck-bags": {"rows": []},
        "/api/memecoin/engine-cockpit": {"positions": []},
    }
    _record_api_calls(page, payloads)

    page.evaluate(
        """
        async () => {
          await window.__memecoin._fetchRealCockpit(false, false);
          window.__memecoin._realStatusTs = 0;
          await window.__memecoin._fetchRealCockpit(false, false);
          window.__memecoin._realStatusTs = 0;
          await window.__memecoin._fetchRealCockpit(false, true);
        }
        """
    )

    calls = page.evaluate("window.__apiCalls")
    for path in (
        "/api/memecoin/real/status",
        "/api/memecoin/real/positions",
        "/api/memecoin/stuck-bags",
    ):
        assert calls.count(path) == 3, path
    # `/api/memecoin/real/incoming` left this batch in 652c408a, which deleted the Incoming strip
    # from the funded cockpit at the operator's request (5 parallel calls, not 6). Assert it is
    # GONE rather than dropping the line, so a silent re-introduction still fails here.
    assert calls.count("/api/memecoin/real/incoming") == 0
    assert calls.count("/api/memecoin/real/closed") == 2
    assert calls.count("/api/memecoin/engine-cockpit") == 2
    assert errors == []


def test_system_stats_consumers_share_one_concurrent_request(memecoin_page):
    page, errors = memecoin_page
    payloads = {
        "/api/system-stats": {"closed_trades": 4, "health_details": {}},
        "/api/ops/health": {"issues": []},
        "/api/agent/alerts?status=new,sent&limit=50": {"items": []},
        "/api/agent/mutes?limit=100": {"items": []},
        "/api/classification-diagnostics": {},
        "/api/strategies": {"items": []},
    }
    _record_api_calls(page, payloads, ["/api/system-stats"])

    page.evaluate(
        """
        async () => {
          window.__operator.render = () => {};
          await Promise.all([
            window.__systemStats.load(),
            window.__operator.load(),
            window.__shellCluster.update(),
          ]);
          clearTimeout(window.__systemStats._refreshTimer);
        }
        """
    )

    calls = page.evaluate("window.__apiCalls")
    assert calls.count("/api/system-stats") == 1
    assert errors == []


def test_armed_renderer_uses_the_active_api_row(memecoin_page):
    page, errors = memecoin_page
    _set_api_payloads(
        page,
        {
            "/api/memecoin/armed?chain=solana": {
                "chain": "solana",
                "copy_arm_policy": True,
                "armed_count": 1,
                "effective_count": 1,
                "approved_count": 1,
                "roster_count": 1,
                "gate": {"min_closes": 5, "min_pnl": 25},
                "rows": [
                    {
                        "source_user_id": "wallet-1",
                        "handle": "alice",
                        "relevant": True,
                        "real_approved": True,
                        "armed_chains": ["solana"],
                        "armed_chain_keys": ["solana"],
                        "armed_here": True,
                        "effective": True,
                        "paused": False,
                        "last_signal": "2026-08-06T12:00:00+00:00",
                        "gate": {"n_closes": 6, "pnl": 40},
                        "best_arm": {"arm": "trail_d", "pnl": 30, "closed": 4},
                        "paper_pnl": 30,
                        "paper_closed": 4,
                        "real": {
                            "open_count": 0,
                            "realized_pnl": 0,
                            "closed_count": 0,
                            "last_entry": None,
                        },
                        "funded_arm_available": True,
                        "funded_arm": {
                            "arm": "trail_d",
                            "pnl": 30,
                            "closed": 4,
                            "blocked_reason": None,
                        },
                    }
                ],
            }
        },
    )

    page.evaluate("async () => window.__memecoin._loadArmed()")

    assert page.locator("#mc-armed").get_by_text("alice").count() == 1
    assert "trail_d rails" in page.locator("#mc-armed td[title*='LIVE']").get_attribute("title")
    assert page.locator('#mc-armed [data-action="chainarm"]').count() == 1
    assert errors == []


def test_real_table_renders_editable_tp_and_sl_controls(memecoin_page):
    page, errors = memecoin_page
    position = {
        "id": 36175,
        "ticker": "KAIJU",
        "mint": "mint-1",
        "network_id": "solana",
        "venue": "solana",
        "entry_notional": 36.95,
        "pnl_pct": 5.0,
        "held_s": 120,
        "default_tp_pct": 30,
        "chain_stop_pct": 50,
        "catastrophe_pct": 80,
        "effective_exit": {
            "tp_pct": 0,
            "tp_target_mcap": 80000,
            "sl_pct": -50,
            "sl_target_mcap": None,
        },
    }
    page.evaluate(
        """
        position => {
          const memecoin = window.__memecoin;
          document.querySelector('#mc-real').innerHTML = memecoin._realTable([position]);
        }
        """,
        position,
    )

    table = page.locator("#mc-real table")
    assert table.locator("thead").get_by_text("TP", exact=True).count() == 1
    assert table.locator("thead").get_by_text("SL", exact=True).count() == 1
    row = table.locator("tbody tr").first
    tp = row.locator('[data-action="edit-exit-leg"][data-leg="tp"]')
    sl = row.locator('[data-action="edit-exit-leg"][data-leg="sl"]')
    assert tp.get_attribute("data-pid") == "36175"
    assert sl.get_attribute("data-pid") == "36175"
    assert "80k" in tp.inner_text()
    assert "50%" in sl.inner_text()
    assert errors == []


def test_mcap_tp_opens_as_mcap_when_tp_pct_is_zero(memecoin_page):
    page, errors = memecoin_page

    raw = page.evaluate(
        """
        () => window.__memecoin._exitLegRaw({
          tp_pct: 0,
          tp_target_mcap: 80000,
        }, 'tp')
        """
    )

    assert raw == {"mode": "mcap", "value": "80000"}
    assert errors == []


@pytest.mark.parametrize("leg,mode,value,expected", [
    ("sl", "pct", "35.25", {"sl_pct": -35.25, "sl_target_mcap": None}),
    ("tp", "mcap", None, {"tp_pct": None, "tp_target_mcap": 1_234_567}),
    ("tp", "pct", "0.4", {"tp_pct": 0.4, "tp_target_mcap": None}),
    ("tp", "pct", None, {"tp_pct": 1e-7, "tp_target_mcap": None}),
    ("tp", "default", "", {"tp_pct": None, "tp_target_mcap": None}),
    ("tp", "none", "", {"tp_pct": 0, "tp_target_mcap": None}),
])
def test_exit_save_keeps_exact_other_rails_and_names_only_the_edited_leg(memecoin_page, leg, mode, value, expected):
    page, errors = memecoin_page
    payload = page.evaluate(
        """async ({leg, mode, value}) => {
          const mc = window.__memecoin;
          mc._realPositions = [{id: 1, ticker: 'TEST', cap_basis: 'circ',
            effective_exit: {tp_pct: 0, tp_target_mcap: 1234567,
              sl_pct: -30.125, sl_target_mcap: null, trail_pct: 25.5, derisk_pct: 0}}];
          if (mode === 'pct' && value === null) {
            mc._realPositions[0].effective_exit.tp_pct = 1e-7;
            mc._realPositions[0].effective_exit.tp_target_mcap = null;
          }
          mc._loadReal = () => {};
          let body;
          window.fetch = async (_url, init) => {
            body = JSON.parse(init.body);
            return new Response(JSON.stringify({ok: true}), {status: 200,
              headers: {'Content-Type': 'application/json'}});
          };
          mc._openExitEditor(1, leg);
          document.getElementById('mc-exit-mode').value = mode;
          if (value !== null) document.getElementById('mc-exit-val').value = value;
          await mc._saveExit();
          return body;
        }""", {"leg": leg, "mode": mode, "value": value})
    assert payload["edited_leg"] == leg
    assert "clear" not in payload
    for field, number in expected.items():
        assert payload[field] == number
    assert payload["trail_pct"] == 25.5 and payload["derisk_pct"] == 0
    if leg == "sl":
        assert payload["tp_target_mcap"] == 1_234_567 and payload["tp_pct"] is None
    else:
        assert payload["sl_pct"] == -30.125 and payload["sl_target_mcap"] is None
    from sniper.routes.api_memecoin import parse_exit_override
    parsed = parse_exit_override(payload)  # the previous server still accepts the compatibility body
    if leg == "sl":
        assert parsed["tp_pct"] == 0 and parsed["tp_target_mcap"] == 1_234_567
    assert errors == []


@pytest.mark.parametrize("other_mcap", [None, 100_000])
def test_fdv_exit_save_confirms_only_an_existing_opposite_mcap(memecoin_page, other_mcap):
    page, errors = memecoin_page
    result = page.evaluate(
        """async otherMcap => {
          const mc = window.__memecoin;
          mc._realPositions = [{id: 1, ticker: 'TEST', cap_basis: 'total', cap_supply: 1000000000,
            target_supply: 800000000, effective_exit: {tp_pct: 0, tp_target_mcap: 1234567,
              sl_pct: -30, sl_target_mcap: otherMcap, trail_pct: null, derisk_pct: 0}}];
          mc._loadReal = () => {};
          let body;
          window.fetch = async (_url, init) => {
            body = JSON.parse(init.body);
            return new Response(JSON.stringify({ok: true}), {status: 200,
              headers: {'Content-Type': 'application/json'}});
          };
          mc._openExitEditor(1, 'tp');
          const pending = mc._saveExit();
          const confirmation = document.querySelector('.confirm-modal');
          if (confirmation) confirmation.querySelector('[data-confirm-role="ok"]').click();
          await pending;
          return {asked: Boolean(confirmation), body};
        }""", other_mcap)
    assert result["asked"] is (other_mcap is not None)
    assert result["body"]["tp_target_mcap"] == 1_234_567
    assert result["body"]["sl_target_mcap"] == other_mcap
    assert errors == []


def test_armed_renderer_shows_paused_as_resume_not_armed(memecoin_page):
    page, errors = memecoin_page
    _set_api_payloads(
        page,
        {
            "/api/memecoin/armed?chain=solana": {
                "chain": "solana",
                "copy_arm_policy": False,
                "armed_count": 0,
                "effective_count": 0,
                "approved_count": 0,
                "roster_count": 1,
                "gate": {"min_closes": 5, "min_pnl": 25},
                "rows": [{
                    "source_user_id": "wallet-1",
                    "handle": "alice",
                    "relevant": True,
                    "real_approved": False,
                    "armed_chains": [],
                    "armed_chain_keys": [],
                    "armed_here": False,
                    "effective": False,
                    "paused": True,
                    "gate": {"n_closes": 6, "pnl": 40},
                    "paper_pnl": 40,
                    "paper_closed": 6,
                    "real": {},
                }],
            }
        },
    )

    page.evaluate("async () => window.__memecoin._loadArmed()")

    row = page.locator("#mc-armed tbody tr").first
    assert row.get_by_text("paused", exact=True).count() == 1
    assert row.locator('[data-action="chainarm"]').inner_text() == "resume + arm"
    assert "armed · paused" not in row.inner_text()
    assert errors == []


def test_closed_renderer_counts_only_closed_censored_arms(memecoin_page):
    page, errors = memecoin_page
    closed = [
        {
            "origin_event_id": "event-1",
            "mint": "mint-1",
            "ticker": "COIN",
            "network_id": "solana",
            "source_handle": "alice",
            "opened_at": "2026-08-06T12:00:00+00:00",
            "closed_at": "2026-08-06T13:00:00+00:00",
            "strategy": "control",
            "exit_reason": "trail",
            "full_book_pnl": 12.0,
        },
        {
            "origin_event_id": "event-1",
            "mint": "mint-1",
            "ticker": "COIN",
            "network_id": "solana",
            "source_handle": "alice",
            "opened_at": "2026-08-06T12:00:00+00:00",
            "closed_at": "2026-08-06T13:01:00+00:00",
            "strategy": "trail_d",
            "exit_reason": "trail",
            "full_book_pnl": -3.0,
        },
        {
            "origin_event_id": "event-1",
            "mint": "mint-1",
            "ticker": "COIN",
            "network_id": "solana",
            "source_handle": "alice",
            "opened_at": "2026-08-06T12:00:00+00:00",
            "closed_at": "2026-08-06T13:02:00+00:00",
            "strategy": "scalp_c2",
            "exit_reason": "no_quote",
            "full_book_pnl": None,
        },
    ]
    report = {
        "chain": "solana",
        "closed": closed,
        "episodes": [
            {
                "origin_event_id": "event-1",
                "arms": ["control", "trail_d", "scalp_c2", "peak_lock_e"],
            }
        ],
    }
    page.evaluate(
        """
        report => {
          window.__memecoin._fetchPaperReport = async () => report;
          window.__memecoin._renderPaperSummary = () => '';
        }
        """,
        report,
    )

    page.evaluate("async () => window.__memecoin._loadClosed()")

    known = page.locator("#mc-closed tbody tr").first.locator("td").nth(7).inner_text()
    assert "2/4" in known
    assert "1 cens" in known
    assert errors == []



def test_identical_cockpit_render_skips_the_dom_write(memecoin_page):
    """A 4s poll that changed nothing must not rebuild #mc-real.

    The wholesale innerHTML replace re-parses the whole section, resets scroll, and closes unkeyed
    disclosures — every 4 seconds, usually for a byte-identical result. The skip must be data-driven:
    identical payloads keep the live DOM (marker survives), a changed payload rebuilds it (marker
    gone), so a stale skip can never hide a real change.
    """
    page, errors = memecoin_page
    payloads = {
        "/api/memecoin/real/status": {
            "armed": True, "kill_tripped": False,
            "envelope": {"policy": "alpha_b", "max_concurrent": 7, "max_exposure": 700},
            "daily": {"limit": 150, "realized_today": 0, "tripped": False},
            "reservations": {"count": 0, "notional": 0, "orphaned": 0},
            "real_open": {"count": 0, "notional": 0, "by_network": {}},
            "venues": {}, "knobs": {},
        },
        "/api/memecoin/real/positions": {"positions": []},
        "/api/memecoin/real/closed": {"closed": []},
        "/api/memecoin/real/incoming": {"rows": []},
        "/api/memecoin/stuck-bags": {"rows": []},
        "/api/memecoin/engine-cockpit": {"positions": []},
        "/api/memecoin/real/accounting": {"slots": {"used": 0, "max": 7}, "allowlist_by_chain": {}},
    }
    _set_api_payloads(page, payloads)

    survived = page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          mc._loadV6Paper = () => {};   // not under test; the fixture page has no base URL for its EventSource
          mc._lastRealHtml = null;
          await mc._loadReal();
          // the accounting panel arrives from a BACKGROUND fetch after the first render — let it
          // land and render once, so the comparison below is between two settled, identical renders
          await new Promise(r => setTimeout(r, 50));
          await mc._loadReal();
          const el = document.getElementById('mc-real');
          const node = el.querySelector('.card');
          node.__probe = 'alive';
          const kBefore = mc._lastRealHtml;
          await mc._loadReal();
          const again = el.querySelector('.card');
          return {ok: !!(again && again.__probe === 'alive'), sameKey: kBefore === mc._lastRealHtml,
                  kb: (kBefore||'').length, ka: (mc._lastRealHtml||'').length};
        }
        """
    )
    assert survived["ok"], "identical data must keep the live DOM: %r" % survived

    rebuilt = page.evaluate(
        """
        async () => {
          // Today's P&L moved to #mc-money (2026-09-12); a venue appearing changes the
          // needs-action strip, which #mc-real still renders.
          window.__apiPayloads['/api/memecoin/real/status'].daily.realized_today = 42.5;
          window.__apiPayloads['/api/memecoin/real/status'].venues = {solana: {configured: true}};
          const mc = window.__memecoin;
          mc._realStatusTs = 0;             // bypass the status reuse cache
          await mc._loadReal();
          const again = document.getElementById('mc-real').querySelector('.card');
          const money = (document.getElementById('mc-money') || {}).innerText || '';
          return again && again.__probe !== 'alive' && money.includes('+$42.50');
        }
        """
    )
    assert rebuilt, "changed data must rebuild the section"
    assert errors == []


def test_quick_buy_ticket_keeps_the_order_draft_across_a_render(memecoin_page):
    """A cockpit refresh or an Advanced toggle must not erase a funded order draft."""
    page, errors = memecoin_page

    draft = page.evaluate(
        """
        () => {
          const mc = window.__memecoin;
          if (typeof mc._renderQuickBuy !== 'function') return {error: 'quick ticket missing'};
          mc._realStatus = {csrf_token: 'csrf-1', knobs: {tp_pct: 25, stop_pct: 35}};
          mc._acct = {by_network: {solana: {manual_presets: {'0.13': 50.7, '0.26': 101.4}}}};
          mc._renderQuickBuy();
          const set = (id, value) => {
            const el = document.getElementById(id);
            el.value = value;
            el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input',
                                       {bubbles: true}));
          };
          set('mc-rb-mint', 'So1DraftMint');
          set('mc-rb-size', '60');
          mc._renderQuickBuy();
          return {
            mint: document.getElementById('mc-rb-mint').value,
            size: document.getElementById('mc-rb-size').value,
            buttons: [...document.querySelectorAll('[data-action="rbpercent"]')].map(x => x.textContent.trim()),
            network: mc._realBuyDraft().network,   // no select since 2026-09-12: the address picks it
          };
        }
        """
    )

    assert draft == {
        "mint": "So1DraftMint", "size": "60", "network": "solana",
        # Two spans since 9d6a4483 — verb then amount, no separator in textContent because they
        # are separate lines in the rendered button.
        "buttons": ["Buy 13% equity≈$50.70", "Buy 26% equity≈$101.40"],
    }
    assert errors == []


def test_activity_buy_sends_the_selected_percent_without_a_modal(memecoin_page):
    """A feed action sends one money intent without a modal."""
    page, errors = memecoin_page

    result = page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          window.__buyCalls = [];
          window.fetch = async (input, init) => {
            window.__buyCalls.push({body: JSON.parse(init.body), headers: init.headers});
            return new Response(JSON.stringify({status: 'queued', intent_id: 'row-intent'}), {
              status: 200, headers: {'Content-Type': 'application/json'},
            });
          };
          mc._realStatus = {csrf_token: 'csrf-row'};
          Object.assign(mc._realBuyDraft(), {rails: 'custom', tp_pct: '60'});
          mc._pollIntent = () => {};
          await mc._realBuy({mint: 'So1FeedMint', uid: 'wallet-7', ticker: 'FAST',
                             network: 'solana', pct: 0.26});
          return window.__buyCalls;
        }
        """
    )

    assert result[0]["body"]["mint"] == "So1FeedMint"
    assert result[0]["body"]["source_user_id"] == "wallet-7"
    assert result[0]["body"]["size_pct_equity"] == 0.26
    assert "rails_mode" not in result[0]["body"]
    assert result[0]["headers"]["X-CSRF-Token"] == "csrf-row"
    assert errors == []


def test_manual_requests_allow_different_tokens_and_block_the_same_order(memecoin_page):
    page, errors = memecoin_page
    calls = page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          mc._realStatus = {csrf_token: 'csrf-many'};
          mc._pollIntent = () => {};
          window.__mints = [];
          window.fetch = async (input, init) => {
            const body = JSON.parse(init.body);
            window.__mints.push(body.mint);
            await new Promise(resolve => setTimeout(resolve, 25));
            return new Response(JSON.stringify({status: 'queued', intent_id: body.intent_id}), {
              status: 200, headers: {'Content-Type': 'application/json'},
            });
          };
          await Promise.all([
            mc._realBuy({mint: 'So1A', network: 'solana', pct: 0.13}),
            mc._realBuy({mint: 'So1B', network: 'solana', pct: 0.13}),
            mc._realBuy({mint: 'So1A', network: 'solana', pct: 0.13}),
          ]);
          await mc._realBuy({mint: 'So1A', network: 'solana', pct: 0.13});
          return window.__mints;
        }
        """
    )

    assert sorted(calls) == ["So1A", "So1B"]
    assert errors == []


def test_activity_rows_render_both_fast_buy_buttons(memecoin_page):
    page, errors = memecoin_page
    _set_api_payloads(page, {
        "/api/memecoin/feed-live": {"events": [{
            "side": "buy", "mint": "So1FeedMint", "uid": "wallet-7", "ticker": "FAST",
            "network_id": "solana", "created_at": "2026-08-29T12:00:00+00:00",
        }]},
        "/api/memecoin/wallets": {"wallets": []},
    })

    buttons = page.evaluate(
        """
        async () => {
          await window.__memecoin._loadEvents();
          return [...document.querySelectorAll('#mc-activity [data-action="realbuy-row"]')]
            .map(button => button.textContent.trim());
        }
        """
    )

    assert buttons == ["Buy 13%≈$—", "Buy 26%≈$—"]
    assert errors == []


def test_relay_source_failure_is_visible_in_health_and_activity(memecoin_page):
    page, errors = memecoin_page
    page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div id=mc-status></div>')")
    source = {"source": "j7", "status": "down", "last_event_at": None,
              "accounts": [{"status": "auth_error", "last_error": "Invalid token <img id=relay-error>"},
                           {"status": "connect_failed", "last_error": "Invalid token"}]}
    _set_api_payloads(page, {
        "/api/memecoin/health": {"runner_alive": True, "auth_ok": True, "source_health": source},
        "/api/memecoin/real/status": {},
        "/api/memecoin/feed-live": {"events": [], "snapshot_at": "2026-10-18T03:17:12+00:00",
                                    "error": None, "source_health": source},
        "/api/memecoin/wallets": {"wallets": []},
    })

    page.evaluate("async () => { await window.__memecoin._loadHealth(); await window.__memecoin._loadEvents(); }")

    for selector in ("#mc-status", "#mc-activity"):
        text = page.locator(selector).inner_text()
        assert "J7 relay: down" in text and "0/2" in text
        assert "Invalid token <img id=relay-error>" in text
        assert "last relay event: unknown" in text
    assert "runner live" in page.locator("#mc-status").inner_text()
    activity = page.locator("#mc-activity").inner_text()
    assert "tracked-wallet" in activity and "every trader" not in activity
    assert page.locator("#relay-error").count() == 0
    assert errors == []


@pytest.mark.parametrize("status,accounts,text", [
    ("degraded", ["connected", "auth_error"], "1/2"),
    ("connected", ["connected"], "1/1"),
    ("stale", ["connected", "connected"], "stale"),
    ("unknown", [], "unknown"),
])
def test_relay_source_reports_partial_quiet_stale_and_unknown(memecoin_page, status, accounts, text):
    page, errors = memecoin_page
    source = {"source": "j7", "status": status, "last_event_at": None,
              "accounts": [{"status": state} for state in accounts]}
    _set_api_payloads(page, {
        "/api/memecoin/feed-live": {"events": [], "error": None, "source_health": source},
        "/api/memecoin/wallets": {"wallets": []},
    })

    page.evaluate("async () => window.__memecoin._loadEvents()")

    shown = page.locator("#mc-activity").inner_text()
    assert f"J7 relay: {status}" in shown and text in shown
    assert "last relay event: unknown" in shown
    if status == "connected":
        assert "Invalid token" not in shown
        assert page.locator("#mc-activity .mc-read-error").count() == 0
    assert errors == []


def test_custom_dollar_button_sends_only_the_dollar_size(memecoin_page):
    page, errors = memecoin_page
    result = page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          window.__body = null;
          window.fetch = async (input, init) => {
            window.__body = JSON.parse(init.body);
            return new Response(JSON.stringify({status: 'queued', intent_id: 'custom-intent'}), {
              status: 200, headers: {'Content-Type': 'application/json'},
            });
          };
          mc._realStatus = {csrf_token: 'csrf-custom'};
          mc._pollIntent = () => {};
          mc._renderQuickBuy();
          const mint = document.getElementById('mc-rb-mint');
          mint.value = 'So1Custom';
          mint.dispatchEvent(new Event('input', {bubbles: true}));
          const size = document.getElementById('mc-rb-size');
          size.value = '30';
          size.dispatchEvent(new Event('input', {bubbles: true}));
          await mc._realBuy({custom: true});
          return window.__body;
        }
        """
    )

    assert result["size_usd"] == 30
    assert "size_pct_equity" not in result
    assert errors == []


def test_quick_buy_enter_queues_once_and_shows_the_intent(memecoin_page):
    """Enter is the submit action, and a second submit cannot duplicate the queued order."""
    page, errors = memecoin_page
    page.evaluate(
        """
        () => {
          window.__buyCalls = [];
          window.fetch = async (input, init) => {
            const url = new URL(String(input), 'http://sniper.test');
            if (url.pathname !== '/api/memecoin/real/buy') {
              return new Response(JSON.stringify({error: 'unexpected path'}), {
                status: 404, headers: {'Content-Type': 'application/json'},
              });
            }
            window.__buyCalls.push(JSON.parse(init.body));
            await new Promise(resolve => setTimeout(resolve, 25));
            return new Response(JSON.stringify({ok: false, status: 'queued', intent_id: 'intent-1'}), {
              status: 200, headers: {'Content-Type': 'application/json'},
            });
          };
        }
        """
    )

    result = page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          if (typeof mc._renderQuickBuy !== 'function') return {error: 'quick ticket missing'};
          mc._realStatus = {csrf_token: 'csrf-enter'};
          mc._pollIntent = () => {};
          mc._renderQuickBuy();
          const mint = document.getElementById('mc-rb-mint');
          mint.value = 'So1EnterMint';
          mint.dispatchEvent(new Event('input', {bubbles: true}));
          mint.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
          mint.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
          await new Promise(resolve => setTimeout(resolve, 50));
          return {
            calls: window.__buyCalls,
            status: document.getElementById('mc-real-buy').innerText,
            button: document.querySelector('[data-action="rbpercent"][data-pct="0.13"]').textContent,
          };
        }
        """
    )

    assert len(result["calls"]) == 1
    assert result["calls"][0]["mint"] == "So1EnterMint"
    assert result["calls"][0]["ticker"] == ""
    assert result["calls"][0]["size_pct_equity"] == 0.13
    assert result["calls"][0]["intent_id"]
    assert "Queued So1Ente" in result["status"]
    assert result["button"].startswith("QUEUED 13%")
    assert errors == []


def test_quick_buy_requires_an_evm_chain_for_a_0x_mint(memecoin_page):
    """The Solana default must not route an EVM mint."""
    page, errors = memecoin_page
    page.evaluate(
        """
        () => {
          window.__buyCalls = 0;
          window.fetch = async () => {
            window.__buyCalls += 1;
            return new Response(JSON.stringify({status: 'queued', intent_id: 'bad'}), {
              status: 200, headers: {'Content-Type': 'application/json'},
            });
          };
        }
        """
    )

    calls = page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          mc._realStatus = {csrf_token: 'csrf-evm'};
          mc._renderQuickBuy();
          const mint = document.getElementById('mc-rb-mint');
          mint.value = '0x1234567890abcdef';
          mint.dispatchEvent(new Event('input', {bubbles: true}));
          mint.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
          await new Promise(resolve => setTimeout(resolve, 20));
          return window.__buyCalls;
        }
        """
    )

    assert calls == 0
    assert errors == []


def test_robinhood_refusal_slot_names_price_basis_and_enters_without_confirm(memecoin_page):
    """The Robinhood action sends the selected percent without the source wallet."""
    page, errors = memecoin_page

    result = page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          mc._realStatus = {csrf_token: 'csrf-refusal'};
          mc._realBuyDraft().pct = '0.26';
          mc._pollIntent = () => {};
          window.__calls = [];
          window.fetch = async (input, init = {}) => {
            const path = new URL(String(input), 'http://sniper.test').pathname;
            const body = init.body ? JSON.parse(init.body) : null;
            window.__calls.push({path, body});
            let payload = {ok: true};
            if (path.endsWith('/refusal-slots')) payload = {slots: [{
              attempt_id: 'attempt-ui', origin_event_id: 'event-ui', network_id: '4663',
              mint: '0x1111111111111111111111111111111111111111', ticker: 'MOVE',
              source_user_id: 'wallet-source', price_basis: 'market',
              source_handle: 'alice', source_price: 0.00001234,
              refusal_price: 0.00001232, quote_price: 0.00001302, source_market_cap: 83000,
              source_size_usd: 224,
              refusal_reason: 'coin ran 4.2x above first-seen price (cap 3.0x)',
              price_drift_pct: 15.2, remaining_s: 91,
            }]};
            else if (path === '/api/memecoin/real/buy') {
              payload = {status: 'queued', intent_id: body.intent_id};
            } else if (path.endsWith('/decision')) {
              payload = {ok: true, slot: {operator_decision: body.decision}};
            } else if (path.endsWith('/shown')) {
              payload = {ok: true, slot: {slot_shown_at: 'now'}};
            }
            return new Response(JSON.stringify(payload), {
              status: 200, headers: {'Content-Type': 'application/json'},
            });
          };
          await mc._loadRefusalSlots();
          const text = document.getElementById('mc-refusal-slots').innerText;
          const tip = document.querySelector('#mc-refusal-slots .mc-refusal-drift').title;
          await mc._enterRefusal({attempt: 'attempt-ui'});
          return {text, tip, calls: window.__calls};
        }
        """
    )

    assert "price already ran 4.2x" in result["text"]   # short words, the deciding number kept
    assert "+15.2%" in result["text"]
    assert "@alice" in result["text"]
    # 2026-09-12 (operator: less text): the three prices and their basis moved to the tooltip on
    # the move-since-refusal figure; the card keeps who bought, how much, at what mcap, and why.
    # 2026-09-28 (operator): prices read as market caps; supply = source mcap / source price.
    assert "trader entry $83K" in result["tip"]
    assert "refused at $83K" in result["tip"]
    assert "now $88K" in result["tip"]
    assert "(market)" in result["tip"]
    assert "bought $224.00 at $83K" in result["text"]
    assert "ENTER 26%" in result["text"]
    buy = next(call["body"] for call in result["calls"] if call["path"] == "/api/memecoin/real/buy")
    assert buy["size_pct_equity"] == 0.26
    assert buy["origin_event_id"] == "event-ui"
    assert buy["network_id"] == "4663"
    assert "source_user_id" not in buy
    decision = next(call["body"] for call in result["calls"] if call["path"].endswith("/decision"))
    assert decision["decision"] == "enter"
    assert errors == []


def test_refusal_expiry_keeps_the_hidden_tab_label(memecoin_page):
    page, errors = memecoin_page
    decisions = page.evaluate(
        """
        () => {
          const mc = window.__memecoin;
          mc._refusalLoadedAt = 1000;
          const row = {remaining_s: 10};
          return [
            mc._refusalExpiryDecision(row, 9000, null),
            mc._refusalExpiryDecision(row, 12000, null),
            mc._refusalExpiryDecision(row, 12000, 5000),
          ];
        }
        """
    )

    assert decisions == [None, "expired_tab_visible", "expired_tab_hidden"]
    assert errors == []


def test_refusal_enter_needs_a_hold_on_a_coarse_pointer(memecoin_page):
    page, errors = memecoin_page

    result = page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          window.matchMedia = query => ({
            matches: query === '(pointer: coarse)',
            addEventListener() {}, removeEventListener() {},
          });
          window.fetch = async () => new Response(JSON.stringify({}), {
            status: 200, headers: {'Content-Type': 'application/json'},
          });
          await mc.load();
          await new Promise(resolve => setTimeout(resolve, 30));
          window.__refusalEntries = 0;
          mc._enterRefusal = () => { window.__refusalEntries += 1; };
          mc._realStatus = {csrf_token: 'csrf-hold'};
          mc._refusalSlots = [{
            attempt_id: 'hold-row', network_id: 'solana', mint: 'So1Hold',
            ticker: 'HOLD', remaining_s: 90,
          }];
          mc._refusalLoadedAt = Date.now();
          mc._renderRefusalSlots();
          const button = document.querySelector('[data-action="refusal-enter"]');
          button.click();
          const afterTap = window.__refusalEntries;
          button.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
          await new Promise(resolve => setTimeout(resolve, 500));
          return {afterTap, afterHold: window.__refusalEntries};
        }
        """
    )

    assert result == {"afterTap": 0, "afterHold": 1}
    assert errors == []


def test_hidden_tab_does_not_start_refusal_clock_and_loads_when_visible(memecoin_page):
    """A background tab must not spend the operator's full decision window."""
    page, errors = memecoin_page

    result = page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          let visibility = 'hidden';
          Object.defineProperty(document, 'visibilityState', {
            configurable: true, get: () => visibility,
          });
          Object.defineProperty(document, 'hidden', {
            configurable: true, get: () => visibility !== 'visible',
          });
          mc._realStatus = {csrf_token: 'csrf-visible'};
          window.__calls = [];
          window.fetch = async (input, init = {}) => {
            const path = new URL(String(input), 'http://sniper.test').pathname;
            window.__calls.push({path, body: init.body ? JSON.parse(init.body) : null});
            const payload = path.endsWith('/shown')
              ? {ok: true, slot: {slot_shown_at: 'now'}}
              : {slots: [{
                  attempt_id: 'visible-row', network_id: '4663',
                  mint: '0x1111111111111111111111111111111111111111',
                  ticker: 'VISIBLE', remaining_s: 120,
                }]};
            return new Response(JSON.stringify(payload), {
              status: 200, headers: {'Content-Type': 'application/json'},
            });
          };

          await mc._loadRefusalSlots();
          const hiddenCalls = window.__calls.length;
          visibility = 'visible';
          document.dispatchEvent(new Event('visibilitychange'));
          await new Promise(resolve => setTimeout(resolve, 30));
          return {
            hiddenCalls,
            paths: window.__calls.map(call => call.path),
            shownBody: window.__calls.find(call => call.path.endsWith('/shown'))?.body,
            button: document.querySelector('[data-action="refusal-enter"]')?.textContent,
          };
        }
        """
    )

    assert result["hiddenCalls"] == 0
    assert result["paths"] == [
        "/api/memecoin/real/refusal-slots",
        "/api/memecoin/real/refusal-slots/visible-row/shown",
    ]
    assert result["shownBody"] == {"visible": True}
    assert result["button"] == "ENTER 13%"
    assert errors == []


def _cockpit_payloads(stuck_bags):
    return {
        "/api/memecoin/real/status": {
            "armed": True, "kill_tripped": False,
            "envelope": {"policy": "alpha_b", "max_concurrent": 7, "max_exposure": 700},
            "daily": {"limit": 150, "realized_today": 0, "tripped": False},
            "reservations": {"count": 0, "notional": 0, "orphaned": 0},
            "real_open": {"count": 0, "notional": 0, "by_network": {}},
            "venues": {}, "knobs": {},
        },
        "/api/memecoin/real/positions": {"positions": []},
        "/api/memecoin/real/closed": {"closed": []},
        "/api/memecoin/stuck-bags": stuck_bags,
        "/api/memecoin/engine-cockpit": {"positions": []},
        "/api/memecoin/real/accounting": {"slots": {"used": 0, "max": 7}, "allowlist_by_chain": {}},
    }


def _render_cockpit(page, stuck_bags):
    _set_api_payloads(page, _cockpit_payloads(stuck_bags))
    return page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          mc._loadV6Paper = () => {};   // not under test; the fixture page has no base URL for its SSE
          mc._lastRealHtml = null;
          await mc._loadReal();
          return document.getElementById('mc-real').innerText;
        }
        """
    )


def test_the_cockpit_names_the_bags_it_cannot_price(memecoin_page):
    """An empty stuck list is only good news when the liquidity was READABLE.

    `position_verdict` calls DEAD only on a known positive-but-tiny liquidity, so a bag with no quote
    is neither stuck nor healthy. While the radar quote daemon was down the endpoint answered 200
    with an empty list for two days and the cockpit rendered nothing at all.
    """
    page, errors = memecoin_page
    text = _render_cockpit(page, {
        "stuck": [], "unpriced": [41, 42], "slots_used": 2, "slots_free": 0,
        "blocked_setup": None, "recommendation": None,
    })

    assert "2 open bags with no live liquidity" in text
    assert "#41, #42" in text
    assert "untradeable cannot be ruled out" in text
    assert errors == []


def test_a_fully_priced_book_shows_no_blindness_warning(memecoin_page):
    page, errors = memecoin_page
    text = _render_cockpit(page, {
        "stuck": [], "unpriced": [], "slots_used": 1, "slots_free": 6,
        "blocked_setup": None, "recommendation": None,
    })

    assert "no live liquidity" not in text
    assert errors == []


def test_entries_fold_one_coin_and_reason_inside_five_minutes(memecoin_page):
    """Repeats inside 5 minutes become one row with xN. A fill, a repeat outside the window and
    the focused event each keep their own row."""
    page, errors = memecoin_page
    rows = page.evaluate("""() => {
      const m = window.__memecoin;
      const row = (id, who, at, extra) => Object.assign({origin_event_id: id, ticker: 'BINGO',
        network_id: 'solana', status: 'refused', reason: 'wallet not_approved on solana',
        source_handle: who, status_updated_at: at}, extra || {});
      m._focusedOrigin = 'e7';
      const box = document.createElement('div');
      box.innerHTML = m._entryRows([
        row('e1', 'cissy', '2026-09-12T15:10:00Z'),
        row('e7', 'hopa', '2026-09-12T15:09:45Z'),
        row('e2', 'admiralfinest', '2026-09-12T15:08:00Z'),
        row('e3', 'cissy', '2026-09-12T15:06:00Z'),
        row('e4', 'frankdegods', '2026-09-12T15:04:30Z'),
        row('e5', 'cissy', '2026-09-12T15:09:00Z', {status: 'filled'}),
        row('e6', 'cissy', '2026-09-12T15:09:30Z', {status: 'filled'}),
      ]);
      return [...box.querySelectorAll('.mc-entry-result')].map(a => {
        const n = a.querySelector('.mc-line-n'), more = a.querySelector('.mc-entry-more');
        return {origin: a.dataset.origin, n: n ? n.textContent : '', tip: more ? more.title : ''};
      });
    }""")

    assert [r["origin"] for r in rows] == ["e1", "e7", "e4", "e5", "e6"]
    assert rows[0]["n"] == "×3"
    assert "admiralfinest" in rows[0]["tip"] and "cissy" not in rows[0]["tip"]
    assert [r["n"] for r in rows[1:]] == ["", "", "", ""]
    assert errors == []


def test_desktop_layout_moves_live_nodes_and_restores_the_phone_order(memecoin_page):
    page, errors = memecoin_page
    out = page.evaluate("""() => {
      const m = window.__memecoin;
      ['mc-money', 'mc-refusal-slots'].forEach(id => document.getElementById(id).remove());
      document.getElementById('memecoin-board').insertAdjacentHTML('afterbegin',
        '<div id="mc-status"></div><nav id="mc-workspaces"><button class="mc-system-button"></button></nav>'
        + '<div class="mc-section" data-section="real"><div id="mc-money"></div><div id="mc-live-attention"></div>'
        + '<div id="mc-refusal-slots"></div><section id="mc-linked-position"></section><div id="mc-real-x"></div>'
        + '<section id="mc-manual-entry"></section><section class="recent"></section>'
        + '<section id="mc-live-balances"></section><div id="mc-notif"></div></div>');
      const s = document.querySelector('#memecoin-board .mc-section[data-section="real"]');
      const ids = el => [...el.children].map(c => c.id || c.className);
      const before = ids(s);
      m._desktopLayout(true);
      const wide = {cols: ids(s), rail: ids(s.querySelector('.mc-live-rail')),
        main: ids(s.querySelector('.mc-live-main')),
        status: document.getElementById('mc-status').parentElement.id};
      m._desktopLayout(true);
      const again = ids(s);
      m._desktopLayout(false);
      return {before, wide, again, after: ids(s),
        statusNext: document.getElementById('mc-status').nextElementSibling.id};
    }""")

    assert out["wide"]["cols"] == ["mc-live-main", "mc-live-rail"]
    assert out["wide"]["rail"] == ["mc-money", "mc-live-attention", "mc-refusal-slots",
                                   "mc-manual-entry", "mc-live-balances"]
    assert out["wide"]["main"] == ["mc-linked-position", "mc-real-x", "recent", "mc-notif"]
    assert out["wide"]["status"] == "mc-workspaces"
    assert out["again"] == ["mc-live-main", "mc-live-rail"]
    assert out["after"] == out["before"]
    assert out["statusNext"] == "mc-workspaces"
    assert errors == []


def test_a_fold_of_two_wallets_shows_plus_one_beside_the_source(memecoin_page):
    """Several wallets on one coin is a signal: the count is in the visible text, the names in
    its tooltip. A fold of one wallet's retries shows no count."""
    page, errors = memecoin_page
    rows = page.evaluate("""() => {
      const m = window.__memecoin;
      const row = (id, ticker, who, at) => ({origin_event_id: id, ticker, network_id: 'solana',
        status: 'refused', reason: 'wallet not_approved on solana', source_handle: who,
        status_updated_at: at});
      const box = document.createElement('div');
      box.innerHTML = m._entryRows([
        row('a1', 'BINGO', 'cissy', '2026-09-12T15:10:00Z'),
        row('a2', 'BINGO', 'admiralfinest', '2026-09-12T15:09:00Z'),
        row('b1', 'PERPSPAD', 'probablyinsider', '2026-09-12T15:08:00Z'),
        row('b2', 'PERPSPAD', 'probablyinsider', '2026-09-12T15:07:00Z'),
      ]);
      return [...box.querySelectorAll('.mc-entry-result')].map(a => {
        const more = a.querySelector('.mc-entry-more');
        return {source: a.querySelector('.mc-entry-source').textContent.trim(),
                n: a.querySelector('.mc-line-n').textContent, tip: more ? more.title : ''};
      });
    }""")

    assert rows == [
        {"source": "cissy +1", "n": "×2", "tip": "also admiralfinest"},
        {"source": "probablyinsider", "n": "×2", "tip": ""},
    ]
    assert errors == []


def test_the_token_name_opens_the_chart_inside_its_own_card(memecoin_page):
    """Operator, 2026-09-23: clicking the token name opened a shared chart at the bottom of the page,
    far from the card. It now toggles the card's own in-card chart and links the GMGN page."""
    page, errors = memecoin_page
    position = {"id": 36175, "ticker": "KAIJU", "mint": "0x" + "a" * 40, "network_id": "4663",
                "venue": "evm", "entry_notional": 36.95, "pnl_pct": 5.0, "held_s": 120,
                "source_handle": "frankdegods", "last_quote_price": 0.001}
    out = page.evaluate(
        """
        position => {
          const m = window.__memecoin;
          const real = document.querySelector('#mc-real');
          real.innerHTML = '<div class="mc-position-grid">' + m._realCard(position) + '</div>';
          const name = real.querySelector('.mc-pos-tk');
          const gmgn = real.querySelector('a[href^="https://gmgn.ai/"]');
          return {action: name.dataset.action, pid: name.dataset.pid, chartEl: name.dataset.chartEl || null,
                  gmgn: gmgn && gmgn.getAttribute('href'), dex: real.innerHTML.includes('dexscreener')};
        }
        """,
        position,
    )
    assert (out["action"], out["pid"], out["chartEl"]) == ("rchart-toggle", "36175", None)
    assert out["gmgn"] == "https://gmgn.ai/robinhood/token/0x" + "a" * 40
    assert out["dex"] is False
    assert errors == []


def test_a_lone_position_card_packs_into_three_lines_on_desktop(memecoin_page):
    """Operator, 2026-09-12: "too much wasted space". At >= 1280 px a lone card puts P&L, the
    exits and every sell button on one line, with Sell all beside Sell 75%. Two cards keep the
    normal card layout."""
    page, errors = memecoin_page
    position = {"id": 36175, "ticker": "KAIJU", "mint": "mint-1", "network_id": "solana",
                "venue": "solana", "entry_notional": 36.95, "pnl_pct": 5.0, "held_s": 120,
                "default_tp_pct": 30, "chain_stop_pct": 50, "catastrophe_pct": 80,
                "source_handle": "frankdegods", "last_quote_price": 0.001}
    out = page.evaluate(
        """
        position => {
          const m = window.__memecoin;
          const real = document.querySelector('#mc-real');
          const measure = () => {
            const box = s => real.querySelector(s).getBoundingClientRect();
            const mid = r => r.top + r.height / 2;
            const card = box('.mc-pos'), tiles = box('.mc-value-grid'), tp = box('[data-leg="tp"]');
            const s75 = box('[data-frac="0.75"]'), all = box('[data-action="rclose"]');
            const chart = box('button[data-action="rchart-toggle"]');   // the toggle button, not the token name (which toggles the same chart since 2026-09-23)
            return {height: card.height, rowSpread: Math.max(mid(tiles), mid(tp), mid(all)) - Math.min(mid(tiles), mid(tp), mid(all)),
                    sellGap: all.left - s75.right, chartBelow: chart.top >= all.bottom,
                    display: getComputedStyle(real.querySelector('.mc-pos')).display};
          };
          real.innerHTML = '<div class="mc-position-grid">' + m._realCard(position) + '</div>';
          const lone = measure();
          real.innerHTML = '<div class="mc-position-grid">' + m._realCard(position)
            + m._realCard({...position, id: 36176}) + '</div>';
          return {lone, twoDisplay: getComputedStyle(real.querySelector('.mc-pos')).display};
        }
        """,
        position,
    )

    assert out["lone"]["display"] == "flex"
    assert out["lone"]["rowSpread"] < 30
    assert 0 <= out["lone"]["sellGap"] < 40
    assert out["lone"]["chartBelow"] is True
    # 260 held the 2026-09-12 three-line layout. The 2026-09-23 redesign adds one deliberate full
    # line (the stop | entry | TP range bar with the live marker) plus a one-line facts row; the
    # P&L + exits + sell row is still one line (rowSpread above), so the budget grows by those lines.
    assert out["lone"]["height"] < 300
    assert out["twoDisplay"] != "flex"
    assert errors == []


# ---------------------------------------------------------------- other holdings (2026-09-13)
# Operator: "Maybe just an other holdings section is enough, also sell anything that we can."
def _holdings_fixture(observed_at, sells=None):
    sells = sells or {}
    rows = [
        {"mint": "So1Weth", "symbol": "WETH", "amount": 0.00123, "price_usd": 3765.2, "value_usd": 4.63,
         "liquidity_usd": 6574804, "sellable": True, "why_not": None, "sell": sells.get("So1Weth")},
        {"mint": "So1Big", "symbol": "BIG", "amount": 1250000, "price_usd": 0.0000061, "value_usd": 7.62,
         "liquidity_usd": 900, "sellable": False, "why_not": "no sell route", "sell": sells.get("So1Big")},
    ]
    return {"as_of": observed_at, "chains": {"solana": {"total_usd": 12.25, "observed_at": observed_at,
            "rows": rows}}, "unpriced_count": 20, "scanned_chains": ["solana"]}


def _render_holdings(page, data):
    return page.evaluate(
        """
        data => {
          const m = window.__memecoin;
          if (!document.getElementById('mc-live-holdings'))
            document.getElementById('memecoin-board').insertAdjacentHTML('beforeend', '<section id="mc-live-holdings"></section>');
          m._holdings = data;
          m._renderHoldings();
          const host = document.getElementById('mc-live-holdings');
          const fold = host.querySelector('details'), wasOpen = fold.open;
          fold.open = true;                  // a closed <details> has no innerText to read
          return {
            summary: host.querySelector('summary').innerText,
            open: wasOpen,
            rows: [...host.querySelectorAll('.mc-holding')].map(r => ({
              text: r.innerText, sell: !!r.querySelector('[data-action="holding-sell"]')})),
            foot: host.querySelector('.mc-holdings-foot').innerText,
          };
        }
        """, data)


def test_other_holdings_render_the_total_rows_and_footer(memecoin_page):
    from sniper.util.clock import real_utc_now_iso
    page, errors = memecoin_page
    out = _render_holdings(page, _holdings_fixture(real_utc_now_iso()))
    assert out["summary"].startswith("Other holdings · reference $12.25")
    assert out["open"] is False                                   # collapsed by default
    assert [r["text"].split()[0] for r in out["rows"]] == ["BIG", "WETH"]   # sorted by value
    big, weth = out["rows"]
    assert "$7.62" in big["text"] and "no sell route" in big["text"] and big["sell"] is False
    assert "$4.63" in weth["text"] and "0.00123" in weth["text"] and weth["sell"] is True
    assert "Ref $7.62" in big["text"] and "Ref $4.63" in weth["text"]
    assert "20 tokens have no market price; a Sell quotes them first" in out["foot"]
    assert "Solana only for now" in out["foot"]
    assert errors == []


def test_holdings_explain_unknown_liquidity_value_and_keep_sell(memecoin_page):
    from sniper.util.clock import real_utc_now_iso
    page, errors = memecoin_page
    data = _holdings_fixture(real_utc_now_iso())
    row = data['chains']['solana']['rows'][0]
    row.update(symbol='PLAGUE', amount=88, price_usd=None, value_usd=None,
               liquidity_usd=60.22,
               valuation_note='Reference value exceeds reported pool liquidity; sale value unknown')
    data['chains']['solana']['total_usd'] = 7.62
    data['unpriced_count'] = 1
    out = _render_holdings(page, data)
    assert out['summary'].startswith('Other holdings · reference $7.62')
    assert 'no reliable balance value' in out['foot']
    details = page.evaluate("""() => {
        const fold = document.querySelector('.mc-holdings-nomarket'); fold.open = true;
        const row = fold.querySelector('.mc-holding');
        return {text: row.innerText, title: row.querySelector('.mc-holding-val').title,
                sell: !!row.querySelector('[data-action="holding-sell"]')};
    }""")
    assert 'Value unknown' in details['text'] and '88' in details['text']
    assert details['sell'] and 'exceeds reported pool liquidity' in details['title']
    assert errors == []


def test_other_holdings_show_the_sell_intent_state(memecoin_page):
    from sniper.util.clock import real_utc_now_iso
    page, errors = memecoin_page
    now = real_utc_now_iso()
    queued = _render_holdings(page, _holdings_fixture(now, {"So1Weth": {"intent_id": "i1", "state": "queued"}}))
    assert "Queued · sells when no Solana trade is open" in queued["rows"][1]["text"]
    assert queued["rows"][1]["sell"] is False
    sold = _render_holdings(page, _holdings_fixture(now, {
        "So1Weth": {"intent_id": "i1", "state": "sold", "usd_received": 4.4, "resolved_at": now}}))
    assert "Sold $4.40" in sold["rows"][1]["text"]
    # 2026-09-14: PUMP sold on 09-13, rewards refilled it, and the row kept "Sold" with no button.
    refilled = _render_holdings(page, _holdings_fixture(now, {
        "So1Weth": {"intent_id": "i0", "state": "sold", "usd_received": 1.66,
                    "resolved_at": "2026-09-13T09:30:42+00:00"}}))
    assert refilled["rows"][1]["sell"] is True and "Sold" not in refilled["rows"][1]["text"]
    failed = _render_holdings(page, _holdings_fixture(now, {
        "So1Weth": {"intent_id": "i1", "state": "failed", "reason": "route vanished"}}))
    assert "Failed: route vanished" in failed["rows"][1]["text"] and failed["rows"][1]["sell"] is True
    assert errors == []


def test_holding_approval_stays_busy_until_its_own_receipt_resolves(memecoin_page):
    from sniper.util.clock import real_utc_now_iso
    page, errors = memecoin_page
    out = _render_holdings(page, _holdings_fixture(real_utc_now_iso(), {
        "So1Weth": {"intent_id": "i1", "state": "approval_submitted"}}))
    assert "Selling…" in out["rows"][1]["text"]
    assert out["rows"][1]["sell"] is False
    pending = page.evaluate("""async () => {
      const m = window.__memecoin;
      const pending = m._holdingsPending();
      m._holdingSales = {'solana:So1Weth': {state: 'approval_submitted', at: Date.now()}};
      const done = m._sellHolding({dataset: {network: 'solana', mint: 'So1Weth', sym: 'WETH'}});
      await new Promise(resolve => setTimeout(resolve, 20));
      const modal = !!document.querySelector('.confirm-modal');
      if (modal) document.querySelector('[data-confirm-role="cancel"]').click();
      await done;
      return {pending, modal};
    }""")
    assert pending == {"pending": True, "modal": False}
    assert errors == []


def test_holdings_scan_label_distinguishes_known_tokens_from_wallet_enumeration(memecoin_page):
    from sniper.util.clock import real_utc_now_iso
    page, errors = memecoin_page
    data = _holdings_fixture(real_utc_now_iso())
    data["chains"]["56"] = {"rows": [], "coverage": "known_inventory"}
    data["chains"]["4663"] = {"rows": [], "coverage": "wallet_enumeration"}
    data["scanned_chains"] = ["solana", "56", "4663"]
    out = _render_holdings(page, data)
    assert "Scanned: SOL, BSC known tokens, ROBIN" in out["foot"]
    assert errors == []


def _holdings_sell(page, *, confirm, post_status=200, post_body=None):
    return page.evaluate(
        """
        async ({confirm, postStatus, postBody}) => {
          const m = window.__memecoin;
          m._realStatus = {csrf_token: 'tok-1'};
          window.__posts = [];
          window.fetch = async (input, init) => {
            const url = new URL(String(input), 'http://sniper.test');
            if ((init || {}).method === 'POST') {
              window.__posts.push({path: url.pathname, body: JSON.parse(init.body),
                                   csrf: (init.headers || {})['X-CSRF-Token']});
              return new Response(JSON.stringify(postBody), {status: postStatus,
                                  headers: {'Content-Type': 'application/json'}});
            }
            return new Response(JSON.stringify(m._holdings), {status: 200, headers: {'Content-Type': 'application/json'}});
          };
          const button = document.querySelector('#mc-live-holdings [data-action="holding-sell"]');
          const done = m._sellHolding(button);
          await new Promise(r => setTimeout(r, 30));
          const modal = document.querySelector('.confirm-modal');
          const message = modal ? modal.innerText : null;
          document.querySelector('[data-confirm-role="' + (confirm ? 'ok' : 'cancel') + '"]').click();
          await done;
          const row = [...document.querySelectorAll('#mc-live-holdings .mc-holding')].find(r => r.innerText.startsWith('WETH'));
          return {message, posts: window.__posts, row: row.innerText};
        }
        """, {"confirm": confirm, "postStatus": post_status, "postBody": post_body or {}})


def test_selling_a_holding_confirms_then_posts_the_chain_and_mint(memecoin_page):
    from sniper.util.clock import real_utc_now_iso
    page, errors = memecoin_page
    _render_holdings(page, _holdings_fixture(real_utc_now_iso()))
    out = _holdings_sell(page, confirm=True, post_body={"ok": True, "intent_id": "i9", "state": "queued"})
    assert "Sell all of WETH for USDC? Real money." in out["message"]
    assert "$4.63" not in out["message"]
    assert "Sale proceeds are unknown until quoted" in out["message"]
    assert "Gas can cost more than the proceeds" in out["message"]
    assert out["posts"] == [{"path": "/api/memecoin/holdings/sell", "csrf": "tok-1",
                             "body": {"network_id": "solana", "mint": "So1Weth", "allow_small": True}}]
    assert "Selling…" in out["row"]
    assert errors == []


def test_a_cancelled_or_refused_holding_sale(memecoin_page):
    from sniper.util.clock import real_utc_now_iso
    page, errors = memecoin_page
    _render_holdings(page, _holdings_fixture(real_utc_now_iso()))
    assert _holdings_sell(page, confirm=False)["posts"] == []
    _render_holdings(page, _holdings_fixture(real_utc_now_iso()))
    out = _holdings_sell(page, confirm=True, post_status=409, post_body={"ok": False, "reason": "dust below the minimum"})
    assert len(out["posts"]) == 1 and "Failed: dust below the minimum" in out["row"]
    assert errors == []


def test_robinhood_holding_confirmation_names_weth_and_small_sales(memecoin_page):
    from sniper.util.clock import real_utc_now_iso
    page, errors = memecoin_page
    data = _holdings_fixture(real_utc_now_iso())
    data['chains']['4663'] = data['chains'].pop('solana')
    data['scanned_chains'] = ['4663']
    _render_holdings(page, data)
    out = _holdings_sell(page, confirm=True, post_body={'ok': True, 'state': 'queued'})
    assert 'for WETH?' in out['message'] and 'Small sales are allowed.' in out['message']
    assert out['posts'][0]['body'] == {'network_id': '4663', 'mint': 'So1Weth', 'allow_small': True}
    assert errors == []


@pytest.mark.parametrize("network,quote_symbol,output", [
    ("56", None, "WBNB"), ("8453", None, "WETH"), ("1", None, "WETH"),
    ("57073", None, "WETH"), ("5042", None, "USDC"), ("56", "BNB", "BNB"),
])
def test_holding_confirmation_uses_the_chain_settlement_asset(memecoin_page, network, quote_symbol, output):
    from sniper.util.clock import real_utc_now_iso
    page, errors = memecoin_page
    data = _holdings_fixture(real_utc_now_iso())
    data["chains"][network] = data["chains"].pop("solana")
    data["chains"][network]["rows"][0]["quote_symbol"] = quote_symbol
    data["scanned_chains"] = [network]
    _render_holdings(page, data)
    out = _holdings_sell(page, confirm=False)
    assert f"for {output}?" in out["message"]
    assert out["posts"] == []
    assert errors == []


def test_money_hero_names_ethereum_and_keeps_dollar_units_and_unknowns(memecoin_page):
    page, errors = memecoin_page
    out = page.evaluate("""() => {
      const m = window.__memecoin;
      m._acct = {totals: {equity: 301}, by_network: {
        solana: {cash: 200, equity: 200}, 1: {cash: 101, equity: 101},
        56: {cash: null, equity: null}, 8453: {cash: 0, equity: 0, gas_ok: false}}};
      const host = document.getElementById('mc-money');
      host.innerHTML = m._moneyHero(0);
      return {total: host.querySelector('.mc-money-total').innerText,
        chips: [...host.querySelectorAll('.mc-chip')].map(e => e.innerText)};
    }""")
    assert out["total"] == "$301 USD"
    assert "⧫ Ethereum (1) $101 USD" in out["chips"]
    assert "BSC ?" in out["chips"]
    assert "BASE $0 USD gas low" in out["chips"]
    assert errors == []


# ---------------------------------------------------------------- Codex audit #6 #7 #8 (2026-09-13)
SOL_MINT = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"
SOL_POOL = "GDFfXyHBGy3fLpRC1BAZLvtmAhYtPB45NV2uFNidEYrs"
EVM_TOKEN = "0x2170ed0880ac9a755fd29b2688956bd959f933f8"
EVM_POOL = "0x9ee0e829fcdec07734e5e669f6292440b102e49d"


def _paste(page, text, *, before="solana", responses=None):
    """Paste into the quick-buy box. `responses`: URL prefix -> JSON body (anything else 404s)."""
    return page.evaluate(
        """
        async ({text, before, responses}) => {
          const m = window.__memecoin;
          window.__fetched = [];
          window.fetch = async input => {
            const url = String(input);
            window.__fetched.push(url);
            const hit = Object.keys(responses).find(k => url.startsWith(k));
            return new Response(JSON.stringify(hit ? responses[hit] : {pairs: null}), {
              status: hit ? 200 : 404, headers: {'Content-Type': 'application/json'}});
          };
          m._realStatus = {csrf_token: 'csrf-paste'};
          const d = m._realBuyDraft();
          d.network = before; d.mint = ''; d.linkError = null;
          m._renderQuickBuy();
          const box = document.getElementById('mc-rb-mint');
          box.value = text;
          box.dispatchEvent(new Event('input', {bubbles: true}));
          await new Promise(r => setTimeout(r, 80));
          const host = document.getElementById('mc-real-buy');
          const buy = host.querySelector('[data-action="rbpercent"]');
          return {mint: d.mint, network: d.network, error: d.linkError || null,
                  buyDisabled: buy ? buy.disabled : null, text: host.innerText, fetched: window.__fetched};
        }
        """, {"text": text, "before": before, "responses": responses or {}})


def test_axiom_links_keep_their_chain(memecoin_page):
    page, errors = memecoin_page
    bsc = _paste(page, "https://axiom.trade/t/%s?chain=bnb" % EVM_TOKEN)
    assert (bsc["mint"], bsc["network"], bsc["error"]) == (EVM_TOKEN, "56", None)
    sol = _paste(page, "https://axiom.trade/t/%s" % SOL_MINT, before="4663")
    assert (sol["mint"], sol["network"]) == (SOL_MINT, "solana")
    assert errors == []


def test_a_gmgn_token_link_names_the_coin_and_its_chain(memecoin_page):
    # 2026-09-23: Telegram and the cockpit link GMGN token pages, so a pasted alert link must buy.
    page, errors = memecoin_page
    rh = _paste(page, "https://gmgn.ai/robinhood/token/%s" % EVM_TOKEN)
    assert (rh["mint"], rh["network"], rh["error"]) == (EVM_TOKEN, "4663", None)
    sol = _paste(page, "https://gmgn.ai/sol/token/%s" % SOL_MINT, before="4663")
    assert (sol["mint"], sol["network"], sol["error"]) == (SOL_MINT, "solana", None)
    eth = _paste(page, "https://gmgn.ai/eth/token/%s" % EVM_TOKEN, before="4663")
    assert eth["error"] and eth["buyDisabled"] is True
    assert errors == []


def test_a_link_naming_an_unsupported_chain_is_an_error_not_the_previous_chain(memecoin_page):
    page, errors = memecoin_page
    for url in ("https://axiom.trade/t/%s?chain=eth" % EVM_TOKEN,
                "https://dexscreener.com/arbitrum/%s" % EVM_POOL):
        out = _paste(page, url, before="4663")
        assert out["error"] and out["buyDisabled"] is True, url
        assert out["mint"] != EVM_TOKEN or out["error"], url
    assert errors == []


def test_a_pool_link_buys_the_pools_token_never_the_pool(memecoin_page):
    page, errors = memecoin_page
    ds = _paste(page, "https://dexscreener.com/solana/%s" % SOL_POOL, responses={
        "https://api.dexscreener.com/latest/dex/pairs/solana/%s" % SOL_POOL: {
            "pairs": [{"chainId": "solana", "pairAddress": SOL_POOL, "baseToken": {"address": SOL_MINT}}]}})
    assert (ds["mint"], ds["network"], ds["error"]) == (SOL_MINT, "solana", None)
    gt = _paste(page, "https://www.geckoterminal.com/bsc/pools/%s" % EVM_POOL, responses={
        "https://api.geckoterminal.com/api/v2/networks/bsc/pools/%s" % EVM_POOL: {
            "data": {"relationships": {"base_token": {"data": {"id": "bsc_%s" % EVM_TOKEN}}}}}})
    assert (gt["mint"], gt["network"], gt["error"]) == (EVM_TOKEN, "56", None)
    # A token-address DexScreener link (our Telegram "chart:" link) is a token, verified by DS.
    tok = _paste(page, "https://dexscreener.com/bsc/%s" % EVM_TOKEN, before="solana", responses={
        "https://api.dexscreener.com/latest/dex/tokens/%s" % EVM_TOKEN: {
            "pairs": [{"chainId": "bsc", "pairAddress": EVM_POOL, "baseToken": {"address": EVM_TOKEN}}]}})
    assert (tok["mint"], tok["network"], tok["error"]) == (EVM_TOKEN, "56", None)
    # No verified mapping: the pool address never becomes the mint, and nothing can be bought.
    miss = _paste(page, "https://dexscreener.com/solana/%s" % SOL_POOL)
    assert miss["mint"] != SOL_POOL and miss["error"] and miss["buyDisabled"] is True
    assert errors == []


def test_recent_trades_follow_the_chain_filter_and_keep_unknown_pnl_unknown(memecoin_page):
    page, errors = memecoin_page
    out = page.evaluate(
        """
        () => {
          const m = window.__memecoin;
          document.getElementById('memecoin-board').insertAdjacentHTML('beforeend', '<div id="mc-recent-entries"></div>');
          m._realOpenCache = [
            {ticker: 'SOLOPEN', mint: 'So1Open', network_id: 'solana', total_pnl_usd: 1.5, held_s: 60},
            {ticker: 'BSCOPEN', mint: '0xaa', network_id: '56', total_pnl_usd: 2.5, held_s: 60}];
          m._realClosedCache = [
            {ticker: 'BSCWIN', mint: '0xbb', network_id: '56', realized_pnl: 5, entry_notional: 50,
             closed_at: '2026-09-13T01:00:00Z', exit_reason: 'real_tp'},
            {ticker: 'SOLNULL', mint: 'So1Null', network_id: 'solana', realized_pnl: null, entry_notional: 50,
             closed_at: '2026-09-13T00:00:00Z', exit_reason: 'manual'}];
          m._entries = [];
          m._auxChainFilter = 'solana';
          m._renderRecentTrades();
          const lines = [...document.querySelectorAll('#mc-recent-entries .mc-line')];
          const nul = lines.find(l => l.innerText.includes('SOLNULL'));
          return {tickers: lines.map(l => l.innerText.split(/\\s/)[0]), nullText: nul && nul.innerText,
                  nullClass: nul && nul.className};
        }
        """)
    assert out["tickers"] == ["SOLOPEN", "SOLNULL"]
    assert "PnL unknown" in out["nullText"] and "$0.00" not in out["nullText"]
    assert "filled" not in out["nullClass"] and "failed" not in out["nullClass"]
    assert errors == []


def test_last_trade_age_ticks_between_full_renders(memecoin_page):
    page, errors = memecoin_page
    out = page.evaluate(
        """
        () => {
          const m = window.__memecoin;
          const real = document.querySelector('#mc-real');
          const t0 = Date.parse('2026-09-13T08:00:00Z');
          m._now = () => t0 + 1000;
          real.innerHTML = m._liveActivityLine({live_activity: {volume_m5: 500, txns_m5: 3,
                                                  last_trade_at: '2026-09-13T08:00:00Z'}});
          const first = real.innerText;
          m._now = () => t0 + 30000;
          m._ageRealMarks();
          return {first, later: real.innerText};
        }
        """)
    assert "last trade 1s ago" in out["first"]
    assert "last trade 30s ago" in out["later"]
    assert errors == []


def test_open_card_shows_the_latest_source_sell_under_its_prices(memecoin_page):
    """Alpha, 2026-09-15: an open card names the trader's latest sell and what we did about it."""
    page, errors = memecoin_page
    out = page.evaluate(
        """
        () => {
          const m = window.__memecoin;
          const real = document.querySelector('#mc-real');
          const base = {id: 1, ticker: 'KAIJU', mint: 'mint-1', network_id: 'solana', last_quote_price: 0.001};
          const card = sell => {
            real.innerHTML = '<div class="mc-position-grid">' + m._realCard({...base, source_sell: sell}) + '</div>';
            const line = real.querySelector('.mc-pos-srcsell');
            const prices = real.querySelector('[data-real-ticker]');
            return line ? {text: line.innerText.trim(),
                           below: !!(prices.compareDocumentPosition(line) & Node.DOCUMENT_POSITION_FOLLOWING)} : null;
          };
          return {
            hold: card({code: 'size_hold', allowed: false, sold_fraction: 0.18, min_fraction: 0.3}),
            sold: card({code: 'size_mirror', allowed: true, sold_fraction: 0.62, min_fraction: 0.3}),
            unknown: card({code: 'size_unknown', allowed: false, sold_fraction: null, min_fraction: 0.3}),
            unreadable: card({code: 'size_inconsistent', allowed: false, sold_fraction: null, min_fraction: 0.3}),
            old: card({code: 'source_still_holds', allowed: false}),
            none: card(null),
          };
        }
        """)
    assert out["hold"] == {"text": "Trader sold 18% · holding (under 30%)", "below": True}
    assert out["sold"]["text"] == "Trader sold 62% · exit requested"
    assert out["unknown"]["text"] == "Trader sold (share unknown) · holding"
    assert out["unreadable"]["text"] == "Trader sold (share unreadable) · holding"
    assert out["old"] is None and out["none"] is None
    assert errors == []


# ---------------------------------------------------------------- withdraw USDC (2026-09-13)
WD_ADDR = "EYdRmMXUEjfRpfBSGJ7aBuJeLj28FXBASc3zZRbeuEr8"


def _withdraw_fixture(intents=None, configured=True):
    return {"ok": True, "configured": configured, "network_id": "solana", "asset": "USDC",
            "to_address": WD_ADDR if configured else None, "intents": intents or [],
            "csrf_token": "csrf-wd"}


def _render_withdraw(page, data):
    return page.evaluate(
        """
        data => {
          const m = window.__memecoin;
          if (!document.getElementById('mc-live-withdraw'))
            document.getElementById('memecoin-board').insertAdjacentHTML('beforeend', '<section id="mc-live-withdraw" hidden></section>');
          m._withdraw = data;
          m._withdrawLocal = null;
          m._renderWithdraw();
          const host = document.getElementById('mc-live-withdraw');
          const to = host.querySelector('.mc-wd-to');
          const btn = host.querySelector('[data-action="withdraw-usdc"]');
          const link = host.querySelector('a[href^="https://solscan.io/tx/"]');
          return {hidden: host.hidden, text: host.innerText, toTitle: to ? to.title : null,
                  button: btn ? !btn.disabled : null, link: link ? link.href : null};
        }
        """, data)


def test_withdraw_panel_renders_the_destination_and_the_form(memecoin_page):
    page, errors = memecoin_page
    out = _render_withdraw(page, _withdraw_fixture())
    assert out["hidden"] is False and "Withdraw USDC" in out["text"]
    assert "To EYdR…uEr8 on Solana" in out["text"] and out["toTitle"] == WD_ADDR
    assert out["button"] is True
    off = _render_withdraw(page, _withdraw_fixture(configured=False))
    assert "No withdrawal address configured" in off["text"] and off["button"] is None
    assert errors == []


def test_withdraw_panel_hides_when_the_read_fails(memecoin_page):
    page, errors = memecoin_page
    hidden = page.evaluate(
        """
        async () => {
          const m = window.__memecoin;
          document.getElementById('memecoin-board').insertAdjacentHTML('beforeend', '<section id="mc-live-withdraw"></section>');
          window.fetch = async () => new Response('{}', {status: 500, headers: {'Content-Type': 'application/json'}});
          await m._loadWithdraw();
          return document.getElementById('mc-live-withdraw').hidden;
        }
        """)
    assert hidden is True
    assert errors == []


def test_withdraw_panel_shows_the_intent_state(memecoin_page):
    page, errors = memecoin_page
    base = {"intent_id": "w1", "amount_usdc": 300, "requested_at": "2026-09-13T09:00:00Z", "reason": None,
            "sig": None, "resolved_at": None}
    busy = _render_withdraw(page, _withdraw_fixture([dict(base, state="submitted")]))
    assert "Sending…" in busy["text"] and busy["button"] is False
    sent = _render_withdraw(page, _withdraw_fixture([dict(base, state="sent", sig="5igSig")]))
    assert "Sent $300.00" in sent["text"] and sent["link"] == "https://solscan.io/tx/5igSig"
    assert sent["button"] is True
    failed = _render_withdraw(page, _withdraw_fixture([dict(base, state="failed", reason="balance too low")]))
    assert "Failed: balance too low" in failed["text"]
    expired = _render_withdraw(page, _withdraw_fixture([dict(base, state="expired")]))
    assert "Expired" in expired["text"]
    assert errors == []


def _withdraw_send(page, *, amount, confirm, post_status=202, post_body=None):
    return page.evaluate(
        """
        async ({amount, confirm, postStatus, postBody}) => {
          const m = window.__memecoin;
          m._realStatus = {csrf_token: 'csrf-status'};
          window.__posts = [];
          window.fetch = async (input, init) => {
            const url = new URL(String(input), 'http://sniper.test');
            if ((init || {}).method === 'POST') {
              window.__posts.push({path: url.pathname, body: JSON.parse(init.body),
                                   csrf: (init.headers || {})['X-CSRF-Token']});
              return new Response(JSON.stringify(postBody), {status: postStatus, headers: {'Content-Type': 'application/json'}});
            }
            return new Response(JSON.stringify(m._withdraw), {status: 200, headers: {'Content-Type': 'application/json'}});
          };
          document.getElementById('mc-wd-amount').value = amount;
          const done = m._withdrawUsdc();
          await new Promise(r => setTimeout(r, 30));
          const modal = document.querySelector('.confirm-modal');
          const message = modal ? modal.innerText : null;
          if (modal) document.querySelector('[data-confirm-role="' + (confirm ? 'ok' : 'cancel') + '"]').click();
          await done;
          return {message, posts: window.__posts, text: document.getElementById('mc-live-withdraw').innerText};
        }
        """, {"amount": amount, "confirm": confirm, "postStatus": post_status, "postBody": post_body or {}})


def test_withdraw_confirms_then_posts_the_amount_once(memecoin_page):
    page, errors = memecoin_page
    _render_withdraw(page, _withdraw_fixture())
    out = _withdraw_send(page, amount="300", confirm=True,
                         post_body={"ok": True, "intent_id": "w9", "state": "queued"})
    assert "Send $300.00 USDC to EYdR…uEr8? Real money, cannot be undone." in out["message"]
    assert out["posts"][0]["body"]["amount_usdc"] == 300
    assert out["posts"][0]["body"]["request_id"]
    assert len(out["posts"]) == 1 and out["posts"][0]["csrf"] == "csrf-wd"
    assert "Sending…" in out["text"]
    assert errors == []


def test_withdraw_cancel_bad_amount_and_refusal(memecoin_page):
    page, errors = memecoin_page
    _render_withdraw(page, _withdraw_fixture())
    assert _withdraw_send(page, amount="300", confirm=False)["posts"] == []
    bad = _withdraw_send(page, amount="0", confirm=True)
    assert bad["message"] is None and bad["posts"] == []
    _render_withdraw(page, _withdraw_fixture())
    out = _withdraw_send(page, amount="25.5", confirm=True, post_status=409,
                         post_body={"ok": False, "reason": "a withdrawal is already in progress"})
    assert out["posts"][0]["body"]["amount_usdc"] == 25.5
    assert "Failed: a withdrawal is already in progress" in out["text"]
    assert errors == []


def test_an_active_chain_filter_always_shows_a_way_back(memecoin_page):
    """2026-09-14 (operator: "why is the ui robin only"). A Telegram link set ROBIN, nothing was
    open, the one-chain rule hid the chip bar, and there was no All chip to clear it."""
    page, errors = memecoin_page
    out = page.evaluate(
        """
        () => {
          const m = window.__memecoin;
          const read = () => [...document.querySelectorAll('#mc-chain-filter [data-mc-chain-aux]')]
            .map(c => [c.dataset.mcChainAux, c.classList.contains('active')]);
          m._auxChainFilter = 'robin'; m._lastChainSig = null;
          m._syncChainFilter('mc-chain-filter', []);
          const filtered = read();
          m._auxChainFilter = 'all'; m._lastChainSig = null;
          m._syncChainFilter('mc-chain-filter', [{mint: 'So1', network_id: 'solana'}]);
          return {filtered, unfiltered: read()};
        }
        """)
    assert out["filtered"] == [["all", False], ["robin", True]]
    assert out["unfiltered"] == []                  # one chain and no filter: nothing to choose
    assert errors == []


def test_no_market_holdings_fold_into_one_line(memecoin_page):
    """2026-09-14: 25 of 38 Robinhood holdings were spam airdrops with no price."""
    from sniper.util.clock import real_utc_now_iso
    page, errors = memecoin_page
    data = _holdings_fixture(real_utc_now_iso())
    data["chains"]["solana"]["rows"].append({"mint": "Spam1", "symbol": "Jacob", "amount": 30506,
                                             "price_usd": None, "value_usd": None,
                                             "sellable": True, "sell": None})
    out = page.evaluate(
        """
        data => {
          const m = window.__memecoin;
          if (!document.getElementById('mc-live-holdings'))
            document.getElementById('memecoin-board').insertAdjacentHTML('beforeend', '<section id="mc-live-holdings"></section>');
          m._holdings = data; m._renderHoldings();
          const fold = document.querySelector('#mc-live-holdings .mc-holdings-nomarket');
          return {summary: fold && fold.querySelector('summary').textContent,
                  inside: fold ? [...fold.querySelectorAll('.mc-holding-sym')].map(e => e.textContent) : [],
                  top: [...document.querySelectorAll('#mc-live-holdings > details > .mc-holdings-list .mc-holding-sym')].map(e => e.textContent)};
        }
        """, data)
    assert out["summary"] == "1 with no market price · Sell checks the route"
    assert out["inside"] == ["Jacob"] and "Jacob" not in out["top"]
    assert errors == []


def test_holdings_refresh_without_waiting_for_a_cockpit_render(memecoin_page):
    page, errors = memecoin_page
    out = page.evaluate("""async () => {
      const m = window.__memecoin;
      const callbacks = [];
      const oldInterval = window.setInterval;
      window.setInterval = fn => { callbacks.push(fn); return callbacks.length; };
      m._loadHealth = () => {};
      m._loadSection = () => {};
      m._ensureRealStream = () => {};
      await m.load();
      window.setInterval = oldInterval;
      Sniper.state.route.tab = 'memecoin'; m._subtab = 'real';
      m._realStreamHealthy = () => true; m._pollBlocked = () => false;
      m._cockpitDirty = false; m._lastSectionPoll = Date.now();
      m._holdingsAt = Date.now() - 16000;
      let reads = 0; m._loadHoldings = () => { reads++; m._holdingsAt = Date.now(); };
      callbacks[0](); callbacks[0]();
      return {reads};
    }""")
    assert out['reads'] == 1
    assert errors == []


def test_unquoted_holdings_do_not_offer_to_check_a_refused_sell(memecoin_page):
    page, errors = memecoin_page
    data = _holdings_fixture('2026-10-03T09:00:00+00:00')
    data['chains']['solana']['rows'] = [{
        'mint': 'DEAD', 'symbol': 'Agrippa', 'amount': 60000,
        'price_usd': None, 'value_usd': None, 'sellable': False,
        'why_not': 'no market — no venue quotes it',
        'sell': {'state': 'failed', 'reason': 'no route: both refused'},
    }]
    data['chains']['solana']['total_usd'] = 0
    data['unpriced_count'] = 1
    out = _render_holdings(page, data)
    assert out['summary'].startswith('Other holdings · reference $0.00')
    assert 'a Sell quotes' not in out['foot']
    assert page.locator('.mc-holdings-nomarket > summary').text_content() == (
        '1 with no market price · No sell route')
    assert page.locator('[data-action="holding-sell"]').count() == 0
    assert errors == []


def test_effective_size_mirror_keeps_the_operator_hold_control(memecoin_page):
    page, errors = memecoin_page
    out = page.evaluate("""() => {
      const m = window.__memecoin;
      const p = {id: 1, ticker: 'T', mint: 'M', network_id: 'solana',
        effective_exit_policy: {mirror_on_sell: false},
        effective_mirror: {source: 'size_aware', enabled: true, operator_hold: false}};
      const host = document.querySelector('#mc-real');
      host.innerHTML = m._realCard(p);
      const button = host.querySelector('[data-action="toggle-mirror"]');
      return {text: host.textContent, button: button && button.textContent};
    }""")
    assert out["button"] == "Size mirror on"
    assert "no mirror" not in out["text"]
    assert errors == []


def test_active_drawdown_pause_never_shows_trading(memecoin_page):
    page, errors = memecoin_page
    out = page.evaluate("""() => window.__memecoin._armState({armed: true, runner_alive: true,
      venues: {solana: {configured: true}}, daily: {drawdown_pause_until: '2026-09-16T10:00:00+00:00'}})""")
    assert out["key"] == "DRAWDOWN_PAUSE"
    assert "2026-09-16T10:00:00+00:00" in out["label"]
    assert errors == []


def test_lost_withdraw_response_keeps_identity_and_reconciles(memecoin_page):
    page, errors = memecoin_page
    _render_withdraw(page, _withdraw_fixture())
    out = page.evaluate("""async () => {
      const m = window.__memecoin;
      window.__posts = [];
      window.fetch = async (url, init) => {
        if ((init || {}).method === 'POST') {
          window.__posts.push(JSON.parse(init.body));
          throw new TypeError('Failed to fetch');
        }
        return new Response(JSON.stringify(m._withdraw), {status: 200});
      };
      document.getElementById('mc-wd-amount').value = '300';
      const done = m._withdrawUsdc();
      await new Promise(r => setTimeout(r, 30));
      document.querySelector('[data-confirm-role="ok"]').click();
      await done;
      await m._loadWithdraw();
      const request = window.__posts[0];
      const unknown = {state: m._withdrawCurrent().state, busy: m._withdrawBusy(),
                       text: document.getElementById('mc-live-withdraw').innerText};
      // A reload must recover the same identity before another submission is possible.
      m._withdrawLocal = null;
      const restored = m._withdrawCurrent();
      document.getElementById('mc-wd-amount').value = '999';
      const retried = m._withdrawUsdc();
      await new Promise(r => setTimeout(r, 30));
      document.querySelector('[data-confirm-role="ok"]').click();
      await retried;
      await m._loadWithdraw();
      m._withdraw = {...m._withdraw, intents: [{intent_id: request.request_id, state: 'sent', amount_usdc: 300}]};
      await m._loadWithdraw();
      clearTimeout(m._withdrawTimer);
      return {request, unknown, restored, final: m._withdrawCurrent(), posts: window.__posts};
    }""")
    import uuid
    uuid.UUID(out["request"]["request_id"])
    assert out["unknown"]["state"] == "unknown" and out["unknown"]["busy"]
    assert "Status unknown" in out["unknown"]["text"]
    assert out["restored"]["intent_id"] == out["request"]["request_id"]
    assert out["final"]["state"] == "sent"
    assert out["posts"] == [out["request"], out["request"]]
    assert errors == []


def test_other_tab_active_withdrawal_wins_over_local_terminal_state(memecoin_page):
    page, errors = memecoin_page
    _render_withdraw(page, _withdraw_fixture())
    out = page.evaluate("""() => {
      const m = window.__memecoin;
      m._withdrawLocal = {intent_id: 'old', state: 'sent', amount_usdc: 20};
      m._withdraw.intents = [{intent_id: 'new', state: 'submitted', amount_usdc: 50}, m._withdrawLocal];
      m._renderWithdraw();
      return {current: m._withdrawCurrent().intent_id, busy: m._withdrawBusy(),
              disabled: document.querySelector('[data-action="withdraw-usdc"]').disabled};
    }""")
    assert out == {"current": "new", "busy": True, "disabled": True}
    assert errors == []


def _refusal_card(page, slot):
    return page.evaluate(
        """
        async slot => {
          const mc = window.__memecoin;
          mc._realStatus = {csrf_token: 'csrf-refusal'};
          window.fetch = async (input) => {
            const path = new URL(String(input), 'http://sniper.test').pathname;
            const payload = path.endsWith('/refusal-slots') ? {slots: [slot]} : {ok: true, slot: {}};
            return new Response(JSON.stringify(payload), {status: 200, headers: {'Content-Type': 'application/json'}});
          };
          if (slot.quote_age_s != null) slot.quote_observed_at = new Date(mc._now() - slot.quote_age_s * 1000).toISOString();
          await mc._loadRefusalSlots();
          return {text: document.getElementById('mc-refusal-slots').innerText,
                  tip: document.querySelector('#mc-refusal-slots .mc-refusal-drift').title};
        }
        """,
        slot,
    )


_SLOT = {"attempt_id": "attempt-basis", "origin_event_id": "event-basis", "network_id": "4663",
         "mint": "0x1111111111111111111111111111111111111111", "ticker": "MOVE",
         "source_handle": "alice", "source_price": 0.00001234, "refusal_price": 0.00001232,
         "quote_price": 0.00001302, "source_market_cap": 83000, "source_size_usd": 512.5,
         "source_size_basis": "event", "refusal_reason": "their buy too small",
         "price_drift_pct": 5.7, "remaining_s": 91}


def test_refusal_card_uses_its_width_for_readable_facts_and_controls(memecoin_page):
    page, errors = memecoin_page
    _refusal_card(page, {**_SLOT, "ticker": "NEARPAD", "refusal_reason":
                         "re-entry cooldown: a real position on this coin closed within the window"})
    for width in (719, 320):
        layout = page.evaluate("""width => {
          const host = document.getElementById('mc-refusal-slots');
          host.style.width = `${width}px`;
          const row = host.querySelector('.mc-refusal-row').getBoundingClientRect();
          const facts = host.querySelector('.mc-refusal-facts');
          const factBox = facts.getBoundingClientRect();
          const controls = [...host.querySelectorAll('.mc-refusal-actions > *')]
            .map(el => el.getBoundingClientRect());
          return {row, factBox, fontSize: parseFloat(getComputedStyle(facts).fontSize),
                  controlsInside: controls.every(box => box.left >= row.left && box.right <= row.right),
                  controlsBelow: controls.every(box => box.top >= factBox.bottom)};
        }""", width)
        assert layout["factBox"]["width"] >= width - 40
        assert layout["fontSize"] >= 13
        assert layout["controlsInside"] and layout["controlsBelow"]
    assert errors == []


def test_a_display_priced_refusal_card_says_chart_and_shows_the_age(memecoin_page):
    """Alpha 2026-09-17: a display price must read differently from a route price, and a "now" price
    that is not a route quote carries its age (live: up to 94 s old, shown as "now")."""
    page, errors = memecoin_page
    out = _refusal_card(page, {**_SLOT, "price_basis": "display", "quote_age_s": 44})
    assert "(chart" in out["tip"] and "(route" not in out["tip"]
    assert "44s" in out["tip"]
    assert "+5.7% · 44s" in out["text"]
    assert "bought $512.50" in out["text"]
    assert errors == []


def test_a_market_priced_refusal_card_shows_the_age_and_a_route_card_does_not(memecoin_page):
    page, errors = memecoin_page
    market = _refusal_card(page, {**_SLOT, "price_basis": "market", "quote_age_s": 30})
    route = _refusal_card(page, {**_SLOT, "attempt_id": "attempt-route", "price_basis": "route",
                                 "quote_age_s": 30})
    assert "(market" in market["tip"] and "30s" in market["tip"] and "+5.7% · 30s" in market["text"]
    assert "(route)" in route["tip"] and "· 30s" not in route["text"]
    assert errors == []


def test_a_route_market_price_is_labelled_on_the_card_and_the_table(memecoin_page):
    """Exact-only chains (5042, 56, most 4663) show the fresh route price as the market price. It must
    never read as a market mid without the label."""
    page, errors = memecoin_page
    base = {"id": 42, "ticker": "ARC", "mint": "0x2222222222222222222222222222222222222222",
            "network_id": "5042", "entry_notional": 25, "entry_price": 1, "held_s": 60,
            "last_quote_price": 1.1, "market_mid_price": 1.1, "market_mid_at": "2026-09-17T08:00:00Z"}
    out = page.evaluate(
        """
        base => {
          const m = window.__memecoin;
          const card = p => { const d = document.createElement('div'); d.innerHTML = m._realTickerRows(p); return d.innerText; };
          const table = p => { const d = document.createElement('div'); d.innerHTML = m._realTable([p]); return d.innerText; };
          const merged = m._mergePosition({...base, market_mid_basis: 'route'}, {id: 42, market_mid_price: null, market_mid_at: null, market_mid_basis: null});
          return {routeCard: card({...base, market_mid_basis: 'route'}), marketCard: card({...base, market_mid_basis: 'market'}),
                  routeTable: table({...base, market_mid_basis: 'route'}), marketTable: table({...base, market_mid_basis: 'market'}),
                  mergedBasis: merged.market_mid_basis};
        }
        """,
        base,
    )
    assert "Route quote, no market mark" in out["routeCard"]
    assert "Route quote" not in out["marketCard"] and "Market mark" in out["marketCard"]
    assert "route" in out["routeTable"] and "route" not in out["marketTable"]
    assert out["mergedBasis"] == "route"      # a failed read keeps the last price WITH its basis
    assert errors == []


def test_bsc_live_pool_price_is_numeric_and_named_on_card(memecoin_page):
    page, errors = memecoin_page
    shown = page.evaluate("""() => {
      const p = {id: 42, mint: '0x2222222222222222222222222222222222222222',
        network_id: '56', entry_price: 0.00001, entry_notional: 25,
        market_mid_price: 0.00002, market_mid_at: '2026-09-26T18:00:00Z',
        market_mid_basis: 'market', market_mid_source: 'bsc_v2_sync',
        selected_exit_price: 0.00001, total_pnl_usd: 0, total_pnl_pct: 0};
      return window.__memecoin._realTickerRows(p);
    }""")
    assert "Market price" in shown and "BSC pool Sync" in shown
    assert page.evaluate("window.__memecoin._operatorPrice(0.00002)") in shown
    assert not errors


@pytest.mark.parametrize("supply,label", [(None, "Sell route price"), (1e9, "Sell route cap")])
def test_route_card_headline_names_the_sell_basis_and_keeps_the_quote_clock(memecoin_page, supply, label):
    page, errors = memecoin_page
    shown = page.evaluate("""supply => {
      const p = {id:42, network_id:'56', entry_price:0.0001, entry_notional:25,
        market_mid_price:0.00012, market_mid_at:'2026-10-10T17:21:28.390697+00:00',
        market_mid_basis:'route', cap_supply:supply, display_supply:supply,
        selected_exit_price:0.00012, total_pnl_usd:5, total_pnl_pct:20};
      const node = document.createElement('div');
      node.innerHTML = window.__memecoin._realTickerRows(p);
      return {labels:Array.from(node.querySelectorAll('.mc-quote > span')).map(n => n.innerText),
              text:node.innerText, clock:node.querySelector('.mc-quote time').getAttribute('datetime')};
    }""", supply)
    assert label in shown["labels"]
    assert "Market cap" not in shown["labels"] and "Market price" not in shown["labels"]
    assert "sell route, no market mark" in shown["text"]
    assert shown["clock"] == "2026-10-10T17:21:28.390697+00:00"
    assert errors == []


@pytest.mark.parametrize("supply_fields,market,cap,want", [
    ({"cap_supply": 1e9, "display_supply": 1e9, "target_supply": 2e9}, None, None, "$120K"),
    ({"display_supply": 1e9, "target_supply": 2e9}, None, None, "$120K"),
    ({"cap_supply": 1e9, "display_supply": None, "target_supply": 2e9}, .0001, 500000, "$120K"),
    ({"cap_supply": None, "display_supply": None, "target_supply": 2e9}, .0001, 200000, None),
    ({"target_supply": 2e9}, None, None, "$240K"),
    ({}, None, None, None),
])
def test_initial_table_and_ticker_preserve_supply_precedence_and_explicit_unknown(
        memecoin_page, supply_fields, market, cap, want):
    page, errors = memecoin_page
    page.add_script_tag(path=str(ROOT / "static" / "mc-range.js"))
    shown = page.evaluate("""({supplyFields,market,cap}) => {
      const p = {id:42, ticker:'ARC', mint:'0x2222222222222222222222222222222222222222',
        network_id:'56', entry_price:0.0001, entry_notional:25, held_s:60,
        market_mid_price:market, market_cap:cap, selected_exit_price:0.00012, ...supplyFields};
      const node = document.createElement('div');
      const m = window.__memecoin;
      node.innerHTML = m._realTable([p]);
      const row = node.querySelector('[data-real-trow="42"]');
      const text = () => row.querySelector('[data-rt="42:selected_exit_price"]').innerText;
      const initial = text();
      m._applyRealTableRow(row, p);
      return {initial,updated:text()};
    }""", {"supplyFields": supply_fields, "market": market, "cap": cap})
    if want is None:
        assert "0." in shown["initial"] and "K" not in shown["initial"]
    else:
        assert shown["initial"] == want
    assert shown["updated"] == shown["initial"]
    assert errors == []


@pytest.mark.parametrize('failure', ['csrf', 'csrf_twice', 'network', 'other_403'])
def test_money_request_refreshes_only_an_explicit_csrf_rejection(memecoin_page, failure):
    from sniper.util.clock import real_utc_now_iso
    page, errors = memecoin_page
    data = _holdings_fixture(real_utc_now_iso())
    data['csrf_token'] = 'old-token'
    _render_holdings(page, data)
    result = page.evaluate('''async failure => {
      const m = window.__memecoin;
      const posts = [];
      window.fetch = async (input, init = {}) => {
        if (init.method === 'POST') {
          posts.push(init.headers['X-CSRF-Token']);
          if (failure === 'network') throw new Error('network lost');
          const rejected = posts.length === 1 || failure === 'csrf_twice';
          const body = rejected ? {ok: false, error: failure === 'other_403' ? 'forbidden' : 'csrf rejected'}
                                : {ok: true, intent_id: 'test', state: 'queued'};
          return new Response(JSON.stringify(body), {status: rejected ? 403 : 202});
        }
        return new Response(JSON.stringify({...m._holdings, csrf_token: 'new-token'}), {status: 200});
      };
      const done = m._sellHolding(document.querySelector('[data-action="holding-sell"]'));
      await new Promise(r => setTimeout(r, 30));
      document.querySelector('[data-confirm-role="ok"]').click();
      await done;
      return {posts, text: document.getElementById('mc-live-holdings').innerText};
    }''', failure)
    expected = ['old-token', 'new-token'] if failure.startswith('csrf') else ['old-token']
    assert result['posts'] == expected
    assert ('Selling…' in result['text']) == (failure == 'csrf')
    assert errors == []


@pytest.mark.parametrize('supplied', ['empty', 'absent'])
def test_money_request_refreshes_a_csrf_token_it_never_had(memecoin_page, supplied):
    """A STALE token recovered and an ABSENT one died, because the retry was gated on the supplied
    header being TRUTHY. `_csrf_ok` refuses an empty token exactly as it refuses a wrong one
    (api_memecoin.py, `if not supplied`), and all eight routes returning 'csrf rejected' call it as
    the first statement of the handler, so this 403 is pre-mutation by construction and retrying an
    empty token is exactly as safe as retrying a stale one.

    The live shape: any money button clicked before `_realStatus` holds a token -- first load, or a
    holdings fetch that failed -- sent the empty string and had no recovery. That is the operator's
    "Release failed: csrf rejected"; a646120d fixed one phantom read of the store, this fixes the
    class. The neighbouring parametrized test still pins that a non-csrf 403, a network error and a
    second rejection never loop.
    """
    page, errors = memecoin_page
    result = page.evaluate(
        """
        async supplied => {
          const posts = [];
          window.fetch = async (input, init = {}) => {
            if (init.method === 'POST') {
              posts.push((init.headers || {})['X-CSRF-Token']);
              const rejected = posts.length === 1;
              return new Response(
                JSON.stringify(rejected ? {ok: false, error: 'csrf rejected'} : {ok: true}),
                {status: rejected ? 403 : 200, headers: {'Content-Type': 'application/json'}});
            }
            return new Response(JSON.stringify({csrf_token: 'fresh-token'}),
                                {status: 200, headers: {'Content-Type': 'application/json'}});
          };
          const options = {method: 'POST', body: '{}', suppressErrorToast: true};
          if (supplied === 'empty') options.headers = {'X-CSRF-Token': ''};
          let body = null, error = null;
          try { body = await Sniper.fetchJson('/api/memecoin/holdings/sell', options); }
          catch (err) { error = String(err && err.message); }
          return {posts, body, error};
        }
        """,
        supplied,
    )
    assert result['posts'] == ['' if supplied == 'empty' else None, 'fresh-token']
    assert result['body'] == {'ok': True} and result['error'] is None
    assert errors == []


def test_the_risk_release_sends_a_real_token_and_names_what_is_holding(memecoin_page):
    """`this._real` is never assigned anywhere in this file -- the store is `this._realStatus`.
    Three sites read the phantom, and two of them are this button: the CSRF header was always the
    empty string, so the server refused every Release with `csrf rejected` and `fetchJson`'s retry
    skipped it because the retry is gated on a TRUTHY token. The confirm dialog read the same
    phantom for `daily`, so it told an operator nothing was holding while a pause and a latched
    breaker both were."""
    page, errors = memecoin_page
    page.evaluate("""() => {
        window.sentPosts = [];
        window.fetch = async (url, options = {}) => {
            if (options.method === 'POST') {
                window.sentPosts.push({url: String(url), token: options.headers['X-CSRF-Token']});
                return new Response(JSON.stringify({ok: true, after: {}}), {status: 200});
            }
            return new Response(JSON.stringify({}), {status: 200});
        };
        window.__memecoin._loadReal = () => {};
        window.__memecoin._realStatus = {csrf_token: 'live-token',
            daily: {drawdown_pause_until: '2026-09-23T19:00:00Z', tripped: true}};
        window.releaseFinished = false;
        window.__memecoin._riskReset().then(() => {window.releaseFinished = true;});
    }""")
    body = page.locator('.confirm-body').inner_text()
    assert 'drawdown pause until 2026-09-23T19:00:00Z' in body, body
    assert 'daily loss breaker latched' in body, body
    assert 'Nothing is holding right now' not in body, body
    page.locator('[data-confirm-role="ok"]').click()
    page.wait_for_function('window.releaseFinished')
    assert page.evaluate('window.sentPosts') == [
        {'url': '/api/memecoin/real/risk-reset', 'token': 'live-token'}]
    assert errors == []


def test_deploy_clear_requires_second_confirmation_and_sends_original_token(memecoin_page):
    page, errors = memecoin_page
    page.evaluate("""() => {
        window.sentClears = [];
        window.fetch = async (url, options) => {
            window.sentClears.push(JSON.parse(options.body));
            return new Response(JSON.stringify({ok:true, tripped:false}), {status:200});
        };
        window.__memecoin._loadReal = () => {};
        // `_realStatus`, not `_real`: this test used to invent `window.__memecoin._real`, a shape
        // production never produces, so it passed while the phantom read was broken.
        window.__memecoin._realStatus = {kill_tripped:true, kill_source:'runtime-deploy',
            kill_token:'original-token', kill_deploy_held:true};
        window.clearFinished = false;
        window.__memecoin._killSwitch('clear').then(() => {window.clearFinished = true;});
    }""")
    page.locator('[data-confirm-role="ok"]').click()
    assert 'Override the deploy pause?' in page.locator('.confirm-body').inner_text()
    assert page.evaluate('window.sentClears') == []
    page.evaluate("window.__memecoin._realStatus.kill_token = 'new-token'")
    page.locator('[data-confirm-role="ok"]').click()
    page.wait_for_function('window.clearFinished')
    assert page.evaluate('window.sentClears') == [{
        'action':'clear', 'expected_token':'original-token', 'override_deploy':True}]
    assert errors == []


@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("failure", ["none", "csrf", "csrf_twice", "network", "other_403"])
def test_knob_ui_sends_csrf_and_retries_only_one_explicit_rejection(memecoin_page, active, failure):
    page, errors = memecoin_page
    result = page.evaluate("""async ({active, failure}) => {
      const m = window.__memecoin;
      m._entryKnobs = {knobs: [], csrf_token: 'old-token'};
      const posts = [];
      window.fetch = async (input, init = {}) => {
        if (init.method === 'POST') {
          posts.push({token: init.headers['X-CSRF-Token'], body: JSON.parse(init.body)});
          if (failure === 'network') throw new Error('network lost');
          const rejected = failure !== 'none' && (posts.length === 1 || failure === 'csrf_twice');
          return new Response(JSON.stringify(rejected
            ? {ok: false, error: failure === 'other_403' ? 'forbidden' : 'csrf rejected'}
            : {ok: true, value: 0, source: 'override'}), {status: rejected ? 403 : 200});
        }
        return new Response(JSON.stringify({knobs: [], csrf_token: 'new-token'}), {status: 200});
      };
      await m._setEntryKnob('MEMECOIN_DAILY_LOSS_LIMIT_USD', 0, active);
      return posts;
    }""", {"active": active, "failure": failure})
    tokens = ["old-token", "new-token"] if failure.startswith("csrf") else ["old-token"]
    assert [post.get("token") for post in result] == tokens
    assert all(post["body"]["active"] is active for post in result)
    assert errors == []


def test_attention_keeps_unknown_market_age_with_stale_route(memecoin_page):
    page, errors = memecoin_page
    text = page.evaluate("""() => {
      const m = window.__memecoin;
      document.body.insertAdjacentHTML('beforeend', '<div id="mc-live-attention"></div>');
      m._now = () => Date.parse('2026-09-18T12:00:00Z');
      m._entries = [];
      m._realPositions = [{ticker: 'MISSING', selected_exit_basis: 'measured_route_stale',
        selected_exit_price: 1, selected_exit_at: '2026-09-18T11:50:00Z',
        market_mid_at: null, runner_decision_at: null}];
      m._renderAttention();
      return document.getElementById('mc-live-attention').innerText;
    }""")
    assert "market mark fresh" not in text
    assert "stale route quote" in text
    assert "sell value 600s old" in text
    assert "market unknown" in text
    assert "runner unknown" in text
    assert errors == []


def test_quick_buy_states_the_tradable_basis_and_the_idle_sol(memecoin_page):
    """Operator 2026-09-25: 13%/26% looked wrong because they are % of USDC + positions, while
    $223 of the Solana book sat in native SOL. The ticket must say which base it uses."""
    page, errors = memecoin_page
    out = page.evaluate(
        r"""
        () => {
          const mc = window.__memecoin;
          mc._realStatus = {csrf_token: 'csrf-1', knobs: {tp_pct: 25, stop_pct: 35}};
          mc._acct = {by_network: {solana: {manual_presets: {'0.13': 39.21, '0.26': 78.43},
                                            sizing_equity: 301.68, native_keep: 0.14,
                                            native_swappable_usd: 206.24}}};
          mc._renderQuickBuy();
          const basis = document.querySelector('.mc-qb-basis');
          const panel = sol => {
            mc._acct = {by_network: {solana: {cash: 262.47, gas: 1.8558, gas_usd: 223.07,
                                              deployed: 39.21, open_n: 1, reserved_n: 0,
                                              unrealized: 1.85, unquotable_n: 0, equity: 526.6,
                                              clip_next: 100.56, sizing_equity: 301.68,
                                              fast_lane_funds_usd: 0, fast_lane_tradable_usd: 301.68,
                                              native_reserve_usd: 1, fast_lane_headroom_usd: 0,
                                              fast_lane_enabled: false,
                                              native_keep: 0.14, native_swappable_usd: sol}},
                        totals: {}, allowlist_by_chain: {}, slots: {used: 1, max: 2}};
            const host = document.createElement('div');
            host.innerHTML = mc._acctPanel();
            return host.textContent.replace(/\s+/g, ' ');
          };
          return {basis: basis && basis.textContent.trim(), quiet: panel(12.0), hint: panel(206.24)};
        }
        """
    )
    assert "% of $301.68 tradable (USDC + positions)" in out["basis"]
    assert "SOL $206.24 held separately" in out["basis"]
    assert "swap to USDC" not in out["quiet"]       # under ~$20 of idle SOL: no nag
    assert "tradable $301.68 (USDC + positions)" in out["hint"]
    assert "equity $526.60 (cash + native assets + positions)" in out["hint"]
    assert "excess SOL $206.24 beyond reserves and the fast-lane target" in out["hint"]
    assert "available to convert to USDC" in out["hint"]
    assert errors == []


def test_an_exit_shows_the_exact_quote_that_decided_it_and_labels_the_stale_mid(memecoin_page):
    """65922 (2026-09-27): stopped on an exact route quote of -35.4% while the DexScreener mid the
    screen showed read -2.0%. The exact decision leads; the mid is labelled display with its ages."""
    page, errors = memecoin_page
    shown = page.evaluate("""() => ({
      labelled: window.__memecoin._exitDecisionText({rail: 'b_stop', decision_basis: 'route',
        decision_pnl_pct: -35.43, display_basis: 'market', display_source: 'fastlane_mid',
        display_pnl_pct: -2.0, display_age_s: 41, decided_at: '2026-09-27T02:06:23.810Z',
        display_checked_at: '2026-09-27T02:06:22.810Z'}),
      legacy: window.__memecoin._exitDecisionText({rail: 'b_stop', decision_basis: 'route',
        decision_pnl_pct: -35.43, display_basis: null, display_pnl_pct: -2.0}),
      onchain: window.__memecoin._exitDecisionText({rail: 'vol_death', decision_basis: 'onchain',
        decision_detail: 'measured_onchain_still silence=30.0s'}),
      none: window.__memecoin._exitDecisionText(null),
    })""")
    assert shown["labelled"] == ("exact route quote -35.4% · market -2.0% "
                                 "(DexScreener, changed 41s · checked 1s before; display only)")
    assert shown["legacy"] == ("exact route quote -35.4% · shown mid -2.0% "
                               "(source and age not recorded; display only)")
    assert shown["onchain"] == "on-chain silence (measured_onchain_still silence=30.0s)"
    assert shown["none"] == ""
    assert not errors


def test_recent_trades_name_the_trader_and_both_buy_prices(memecoin_page):
    """Operator, 2026-09-28: who we copied, their buy price and ours, on every recent trade."""
    page, errors = memecoin_page
    out = page.evaluate(
        """
        () => {
          const m = window.__memecoin;
          document.getElementById('memecoin-board').insertAdjacentHTML('beforeend', '<div id="mc-recent-entries"></div>');
          m._realOpenCache = [
            {ticker: 'FUND', mint: 'So1Open', network_id: 'solana', total_pnl_usd: 1.5, held_s: 60,
             source_handle: 'probablyinsider', their_price: 0.000042958, entry_price: 0.00005659,
             display_supply: 1e9}];
          m._realClosedCache = [
            {ticker: null, mint: 'FSJPGs5c8FYvNameless', network_id: 'solana', realized_pnl: 5,
             entry_notional: 50, closed_at: '2026-09-28T10:00:00Z', exit_reason: 'real_tp',
             source_handle: 'redemptionjizzy', their_price: null, entry_price: 0.0001441},
            {ticker: 'MINE', mint: 'So1Mine', network_id: 'solana', realized_pnl: -2, entry_notional: 50,
             closed_at: '2026-09-28T09:00:00Z', exit_reason: 'manual', manual: true,
             source_handle: null, their_price: null, entry_price: 0.5}];
          m._entries = [];
          m._auxChainFilter = 'all';
          m._renderRecentTrades();
          const lines = [...document.querySelectorAll('#mc-recent-entries .mc-line')];
          return lines.map(l => ({text: l.innerText.replace(/\\s+/g, ' '),
                                  sub: (l.querySelector('.mc-line-sub') || {}).textContent,
                                  title: (l.querySelector('.mc-trade b') || {}).title}));
        }
        """)
    open_line, unnamed, manual = out
    assert "from probablyinsider" in open_line["text"]
    # A known supply reads as market caps ("34k, not 0.0000343", operator 2026-09-28).
    assert "they bought at $43K mcap · we at $57K · entry premium +32% vs source" in open_line["text"]
    assert unnamed["text"].startswith("FSJPGs5c") and unnamed["title"] == "FSJPGs5c8FYvNameless"
    assert "from redemptionjizzy" in unnamed["text"] and "they bought at unknown" in unnamed["text"]
    assert unnamed["sub"] == ("from redemptionjizzy · they bought at unknown · we at $0.0₃1441"
                              " · entry comparison unknown")   # no supply: the price
    assert "your buy" in manual["text"] and "they bought" not in manual["text"]
    assert errors == []


def test_the_closed_table_shows_the_trader_and_both_buy_prices(memecoin_page):
    page, errors = memecoin_page
    _set_api_payloads(page, {
        "/api/memecoin/real/status": {"armed": True, "venues": {}, "knobs": {}},
        "/api/memecoin/real/positions": {"positions": []},
        "/api/memecoin/real/closed": {"closed": [
            {"id": 7, "mint": "FSJPGs5c8FYvNameless", "ticker": "VBUCKS", "network_id": "solana",
             "entry_price": 0.00005659, "their_price": 0.000042958, "entry_notional": 100,
             "realized_pnl": 7, "exit_reason": "real_tp", "manual": False,
             "source_handle": "probablyinsider", "signal_source": "slot_lane", "display_supply": 1e9,
             "opened_at": "2026-09-28T09:00:00Z", "closed_at": "2026-09-28T09:05:00Z"}]},
        "/api/memecoin/stuck-bags": {"rows": []},
        "/api/memecoin/engine-cockpit": {"positions": []},
    })
    out = page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          mc._loadV6Paper = () => {};
          await mc._loadReal();
          await new Promise(resolve => setTimeout(resolve, 20));
          const table = document.querySelector('#mc-real details[data-dk="real-closed"] table');
          const heads = [...table.querySelectorAll("thead th")].map(th => th.textContent.trim());
          const row = table.querySelector('tbody tr[data-position-id="7"]');
          const cells = [...row.querySelectorAll("td")].map(td => td.textContent.trim());
          const chartRow = table.querySelector('[data-chart-keep]').closest('td');
          return {heads, cells, span: chartRow.colSpan};
        }
        """)
    assert out["heads"][:5] == ["Token", "Chain", "Trader", "Their entry", "Our entry"]
    assert out["cells"][0] == "VBUCKS" and out["cells"][2] == "probablyinsider"
    assert out["cells"][3] == "$43K" and out["cells"][4].startswith("$57K")
    assert "+32%" in out["cells"][4]
    assert out["span"] == len(out["heads"]) == 11
    assert errors == []


_ENTRY_COMPARISON_CASES = [
    (0.00004, 0.0000501, "entry premium +25% vs source", "var(--loss)"),
    (0.00004, 0.00003, "entry discount -25% vs source", "var(--gain)"),
    (0.00004, 0.00004, "same entry price", "var(--text-3)"),
    (None, 0.00005, "entry comparison unknown", "var(--text-3)"),
    (0, 0.00005, "entry comparison unknown", "var(--text-3)"),
    (0.00004, None, "entry comparison unknown", "var(--text-3)"),
]


@pytest.mark.parametrize("theirs,ours,label,tone", _ENTRY_COMPARISON_CASES)
def test_recent_trade_labels_entry_premium_discount_and_unknown(
        memecoin_page, theirs, ours, label, tone):
    page, errors = memecoin_page
    out = page.evaluate("""({theirs, ours}) => {
      const host = document.createElement('div');
      host.innerHTML = window.__memecoin._tradeSub({source_handle: 'loganlim_x',
        their_price: theirs, entry_price: ours, display_supply: 1e9});
      const comparison = host.querySelector('[style*="color"]');
      return {text: host.textContent, label: comparison && comparison.textContent,
        tone: comparison && comparison.style.color, title: comparison && comparison.title};
    }""", {"theirs": theirs, "ours": ours})
    assert out["label"] == label
    assert out["tone"] == tone
    assert "PnL" in out["title"] and "source" in out["title"]
    assert errors == []


@pytest.mark.parametrize("theirs,ours,label,tone", _ENTRY_COMPARISON_CASES)
def test_closed_trade_labels_entry_premium_discount_and_unknown(
        memecoin_page, theirs, ours, label, tone):
    page, errors = memecoin_page
    _set_api_payloads(page, {
        "/api/memecoin/real/status": {"armed": True, "venues": {}, "knobs": {}},
        "/api/memecoin/real/positions": {"positions": []},
        "/api/memecoin/real/closed": {"closed": [{
            "id": 73295, "mint": "0x624b777357d6f9adf7311dfafdf67566315a7777",
            "ticker": "吉祥羊", "network_id": "56", "source_handle": "loganlim_x",
            "their_price": theirs, "entry_price": ours, "display_supply": 1e9,
            "entry_notional": 50, "realized_pnl": -19.49, "exit_reason": "b_stop",
            "manual": False, "opened_at": "2026-10-11T03:00:00Z",
            "closed_at": "2026-10-11T03:00:05Z",
        }]},
        "/api/memecoin/stuck-bags": {"rows": []},
        "/api/memecoin/engine-cockpit": {"positions": []},
    })
    out = page.evaluate("""async () => {
      const m = window.__memecoin;
      m._loadV6Paper = () => {};
      await m._loadReal();
      const row = document.querySelector('#mc-real tr[data-position-id="73295"]');
      const cells = [...row.querySelectorAll('td')];
      const comparison = cells[4].querySelector('[style*="color"]');
      return {entry: cells[4].textContent, label: comparison && comparison.textContent,
        tone: comparison && comparison.style.color, title: comparison && comparison.title,
        pnl: cells[5].textContent, money: cells[6].textContent};
    }""")
    assert out["label"] == label
    assert out["tone"] == tone
    assert "PnL" in out["title"] and "source" in out["title"]
    assert out["pnl"] == "-39.0%" and "19.49" in out["money"]
    assert errors == []


def test_a_held_anchor_row_shows_the_hold_and_sells_at_any_price(memecoin_page):
    """Anchor-checked EVM exit (spec 2026-10-01): a row the runner is holding shows the hold and a
    "Sell at any price" button in the table and the card; the button posts any_price=true."""
    page, errors = memecoin_page
    held = {"id": 67676, "ticker": "HOMER", "mint": "0xmint", "network_id": "56", "venue": "evm",
            "entry_notional": 117.09, "pnl_pct": -5.0, "held_s": 60, "default_tp_pct": 30,
            "chain_stop_pct": 50, "catastrophe_pct": 80, "effective_exit": None,
            "anchor_hold": {"observed_at": "2026-10-01T12:00:00+00:00", "value_usd": 0.01,
                            "anchor_price": 0.2, "qty": 500.0, "held_s": 1.2, "route": "uniswap_v2",
                            "exit_reason": "b_stop"}}
    plain = dict(held, id=1, anchor_hold=None)
    out = page.evaluate(
        """
        ([held, plain]) => {
          const mc = window.__memecoin;
          const root = document.querySelector('#mc-real');
          root.innerHTML = mc._realTable([held, plain]);
          const row = root.querySelector('[data-real-trow="67676"]');
          const table = {text: row.innerText, buttons: root.querySelectorAll('[data-action="rclose-any"]').length,
                         pid: root.querySelector('[data-action="rclose-any"]').dataset.pid};
          root.innerHTML = mc._realCard(held);
          const card = {text: root.innerText,
                        buttons: root.querySelectorAll('[data-action="rclose-any"]').length};
          root.innerHTML = mc._realCard(plain);
          return {table, card, plainCard: root.querySelectorAll('[data-action="rclose-any"]').length};
        }
        """,
        [held, plain],
    )
    assert out["table"]["buttons"] == 1 and out["table"]["pid"] == "67676"
    assert "HELD $0.01 vs $100.00" in out["table"]["text"]
    assert out["card"]["buttons"] == 1 and "HELD $0.01 vs $100.00" in out["card"]["text"]
    assert out["plainCard"] == 0

    sent = page.evaluate(
        """
        async () => {
          const mc = window.__memecoin;
          const posts = [];
          window.fetch = async (input, init) => {
            if (init && init.method === 'POST') posts.push({url: String(input), body: init.body});
            return new Response(JSON.stringify({ok: true}), {status: 200,
                                headers: {'Content-Type': 'application/json'}});
          };
          mc._loadReal = async () => {};
          const done = mc._closeRealPos(67676, {ticker: 'HOMER', mint: '0xmint'}, null, true);
          await new Promise(resolve => setTimeout(resolve, 20));
          const text = document.querySelector('.confirm-modal').innerText;
          document.querySelector('[data-confirm-role="ok"]').click();
          await done;
          return {posts, text};
        }
        """
    )
    assert "ANY price" in sent["text"]
    assert sent["posts"] == [{"url": "/api/memecoin/positions/67676/close", "body": '{"any_price":true}'}]
    assert errors == []


def test_wallet_universe_renders_chain_and_supporting_coin_without_html_injection(browser):
    import json
    page=browser.new_page()
    page.set_content('<div id="wallet-universe"></div>')
    # localStorage requires an origin; tests never send a real request.
    page.route('http://sniper.test/**',lambda r:r.fulfill(body='<div id="wallet-universe"></div>'))
    page.goto('http://sniper.test/')
    page.add_script_tag(content=(ROOT/'static/wallet_universe.js').read_text())
    page.add_script_tag(content=(ROOT/'static/wallet_map.js').read_text())
    body=dict(chain='4663',chains=[dict(id='4663',label='Robinhood'),dict(id='56',label='BSC')],
              stamp={'authority_state':'ok'},graph_state='not_collected',generated_at=None,
              wallets=[dict(uid='A',handle='<img src=x onerror=alert(1)>',armed=True,effective=True,coverage={},
                            paper=None,funded=None,strict_policy=None)],edges=[])
    page.evaluate('body=>walletMap.drawUniverse(body)',body)
    assert page.locator('.chain[data-chain="4663"]').get_attribute('aria-pressed')=='true'
    assert page.locator('#wallet-universe img').count()==0
    assert page.locator('a.wu-armed-link').get_attribute('href')=='/?tab=memecoin&mc_view=wallets&mc_chain=4663'
    assert 'Relationships not measured' in page.locator('.wu-live-status').inner_text()
    page.close()


def test_wallet_universe_link_reads_its_coverage_from_the_deduplicated_refs(browser):
    """Wallet Map D1 moved each edge's coverage into body.coverage_by_ref (one copy per type). Until
    W5 the inspector must still print the real coverage, never "unknown" for a measured link."""
    page=browser.new_page()
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('console',lambda m:errors.append(m.text) if m.type=='error' else None)
    page.route('http://sniper.test/**',lambda r:r.fulfill(body='<div id="wallet-universe"></div>'))
    page.goto('http://sniper.test/')
    page.add_script_tag(content=(ROOT/'static/wallet_universe.js').read_text())
    page.add_script_tag(content=(ROOT/'static/wallet_map.js').read_text())
    body=dict(chain='4663',chains=[dict(id='4663',label='Robinhood')],stamp={'authority_state':'ok'},
              graph_state='not_measured',wallets=[dict(uid='A',handle='a',coverage={}),dict(uid='B',handle='b',coverage={})],
              edges=[dict(src_uid='A',dst_uid='B',kind='follows',direction='ordered',coins=['M'],
                          coverage_ref='follows',provenance={'method_version':'v'})],
              coverage_by_ref={'follows':{'source':'memecoin_events j7_wallets'}})
    page.evaluate('body=>walletMap.drawUniverse(body)',body)
    text=page.locator('#wallet-universe').text_content()          # the evidence sits in closed <details>
    assert 'Observed neighborhood · 1 links' in text
    assert 'Coverage: {"source":"memecoin_events j7_wallets"}' in text and 'Coverage: unknown' not in text
    assert errors==[]
    page.close()


def test_wallet_map_live_actions_survive_missing_measurements_then_fail_closed_on_refresh(browser):
    page=browser.new_page()
    page.route('http://sniper.test/**',lambda r:r.fulfill(content_type='text/html',body='<section id="tab-wallet-map"><div id="wallet-universe"></div><div id="follow-funnel-body"></div><div id="follow-funnel-stamp"></div><div id="wallet-map-action-status"></div></section>'))
    page.goto('http://sniper.test/')
    page.add_script_tag(content=(ROOT/'static/wallet_universe.js').read_text())
    page.add_script_tag(content=(ROOT/'static/wallet_map.js').read_text())
    page.evaluate("window.confirms=0;window.confirm=()=>{window.confirms+=1;return false;};void 0")
    funnel=dict(stamp={'error':'not_generated','authority_state':'ok'},funnel=[],clips={},evm_candidates={},solana_candidates=[],rotation=[])
    universe=dict(chain='4663',stamp={'authority_state':'ok'},wallets=[dict(uid='A',from_roster=True,coverage={})],edges=[],graph_state='not_collected')
    page.evaluate('b=>{walletMap.drawFunnel(b.f);walletMap.drawUniverse(b.u)}',dict(f=funnel,u=universe))
    page.locator('#wallet-universe button[data-key="arm"]').click()
    assert page.evaluate('window.confirms')==1
    page.evaluate("window.fetch=async()=>{throw new Error('offline')};walletMap.load();")
    page.wait_for_function("document.getElementById('follow-funnel-stamp').textContent.includes('offline')")
    page.evaluate('b=>walletMap.drawUniverse(b)',universe)
    page.locator('#wallet-universe button[data-key="arm"]').click()
    assert page.evaluate('window.confirms')==1
    assert 'unavailable' in page.locator('#wallet-map-action-status').inner_text()
    page.close()


@pytest.mark.parametrize('regime,scale,approved', [('thin', .75, 131.25), ('unknown', .75, 131.25),
                                                  ('normal', .375, 65.62)])
def test_frozen_size_breakdown_is_visible_and_does_not_promise_a_full_fill(memecoin_page, regime, scale, approved):
    page, errors = memecoin_page
    result = page.evaluate('''p => {
      const host = document.createElement('div');
      host.innerHTML = window.__memecoin._entryContractDetails(p);
      return {summary: host.querySelector('summary').textContent, text: host.textContent};
    }''', {'id': 70823, 'policy_source': 'funded_contract', 'entry_notional': approved, 'entry_contract': {'version': 1, 'hash': 'abc',
          'classification': {'market_regime': regime, 'coin_band': 'micro'},
          'sizing': {'base_clip_usd': 175., 'risk_scale': scale, 'approved_clip_usd': approved}}})
    assert '$175.00 base' in result['summary'] and regime in result['summary']
    assert f'×{scale * 100:g}%' in result['summary']
    assert f'${approved:.2f} approved' in result['summary']
    assert f'filled ${approved:.2f}' in result['text']
    assert errors == []


def test_bad_sizing_values_remain_unknown_instead_of_becoming_zero(memecoin_page):
    page, errors = memecoin_page
    text = page.evaluate('''() => {
      const host = document.createElement('div');
      host.innerHTML = window.__memecoin._entryContractDetails({id: 1, policy_source: 'funded_contract', entry_contract: {
        version: 1, classification: {}, sizing: {base_clip_usd: false, risk_scale: '', approved_clip_usd: null}}});
      return host.textContent;
    }''')
    assert 'unknown base' in text and 'scale unknown' in text and 'unknown approved' in text
    assert '$0' not in text and errors == []


def test_partial_intent_waits_for_recovered_fill(memecoin_page):
    page, errors = memecoin_page
    result = page.evaluate("""async () => {
      const mc = window.__memecoin;
      const intents = [
        {state: 'done', action: 'partial', result_status: 'unknown', sig: 'SellSig'},
        {state: 'done', action: 'partial', result_status: 'derisked', sig: 'SellSig'}
      ];
      let calls = 0;
      window.fetch = async () => {
        calls++;
        return new Response(JSON.stringify({intent: intents.shift()}), {status: 200});
      };
      const intent = await mc._awaitExitIntent('partial-recovery', 'TEST');
      return {status: intent.result_status, calls};
    }""")
    assert result == {'status': 'derisked', 'calls': 2}
    assert errors == []


@pytest.mark.parametrize('ancestor,funded,known',[(True,False,True),(True,True,True),(True,False,False),(False,False,True),(True,None,True)])
def test_release_drift_quiet_note_requires_proven_nonfunded_ancestor(memecoin_page,ancestor,funded,known):
    page,errors=memecoin_page
    payloads=_cockpit_payloads({'rows':[]})
    payloads['/api/memecoin/real/status']['release_drift']=dict(web='b'*40,runner='a'*40,match=False,relationship=dict(state='known' if known else 'unknown',runner_is_ancestor=ancestor,web_is_ancestor=False,funded_paths_changed=funded))
    _set_api_payloads(page,payloads)
    result=page.evaluate('''async()=>{const m=window.__memecoin;m._loadV6Paper=()=>{};await m._loadReal();return {note:m._releaseNote(m._realStatus),needs:m._needsAction(m._realStatus),shown:document.getElementById('mc-real').innerText};}''')
    quiet=ancestor and funded is False and known
    assert bool(result['note'])==quiet
    assert any('DASHBOARD IS ON A DIFFERENT RELEASE' in x for x in result['needs']) is not quiet
    if quiet:assert result['note'] in result['shown']
    if ancestor and funded and known:assert 'runner forward' in result['shown']
    assert 'bash scripts/deploy_web_runtime.sh' not in result['shown']
    assert errors==[]


def test_release_drift_behind_advice_only_moves_dashboard_forward(memecoin_page):
    page,errors=memecoin_page
    r=page.evaluate("""()=>window.__memecoin._needsAction({release_drift:{web:'a'.repeat(40),runner:'b'.repeat(40),match:false,relationship:{state:'known',runner_is_ancestor:false,web_is_ancestor:true,funded_paths_changed:false}}})""")
    warning=next(x for x in r if 'DASHBOARD IS ON A DIFFERENT RELEASE' in x)
    assert 'dashboard forward' in warning and 'deploy_web_runtime.sh' not in warning
    assert errors==[]
