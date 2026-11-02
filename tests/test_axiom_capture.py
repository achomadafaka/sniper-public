"""Axiom capture worker: local clock, fail-closed halts, heartbeat, and the funded-path guard."""
import json
import re
import sqlite3
from pathlib import Path

import pytest

from sniper.feeds import axiom_browser as AB
from sniper.persistence import db_axiom as dba

REPO = Path(__file__).resolve().parents[1]
T0 = "2026-09-16T10:00:00+00:00"


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    dba.ensure_axiom_tables(c); return c


class _Clock:
    def __init__(self): self.mono = 1_000_000_000; self.iso = T0
    def mono_ns(self): return self.mono
    def now_iso(self): return self.iso


def _core(tmp_path, parsers=None, alerts=None):
    clk = _Clock()
    core = AB.CaptureCore(_c(), now_iso_fn=clk.now_iso, mono_ns_fn=clk.mono_ns,
                          alert_fn=(alerts.append if alerts is not None else None),
                          heartbeat_path=tmp_path / "hb.json", halt_path=tmp_path / "halt.json",
                          parsers=parsers)
    return core, clk


def test_two_frames_50ms_apart_land_50ms_apart_on_the_local_clock_not_axioms(tmp_path):
    core, clk = _core(tmp_path)
    core.on_ws_frame("wss://socket8.axiom.trade/?tok=SECRET", '{"ts":"2020-01-01"}', page="trackers")
    clk.mono += 50_000_000
    core.on_ws_frame("wss://socket8.axiom.trade/", '{"ts":"2030-01-01"}', page="trackers")
    rows = core.conn.execute("SELECT received_mono_ns, source FROM axiom_raw_messages ORDER BY id").fetchall()
    assert rows[1][0] - rows[0][0] == 50_000_000
    assert rows[0][1] == "wss://socket8.axiom.trade/"          # query stripped: no token in the ledger


def test_a_403_halts_writes_the_file_and_pages_once(tmp_path):
    alerts = []
    core, _ = _core(tmp_path, alerts=alerts)
    core.on_rest_response("https://api.axiom.trade/x?y=1", 403, "", page="trackers")
    core.on_rest_response("https://api.axiom.trade/x", 403, "", page="trackers")
    assert core.halted.startswith("http_403")
    assert json.loads((tmp_path / "halt.json").read_text())["reason"].startswith("http_403")
    assert len(alerts) == 1


def test_a_login_or_challenge_page_halts(tmp_path):
    core, _ = _core(tmp_path)
    core.on_page_loaded("trackers", "https://axiom.trade/login?next=/trackers", "<html>",
                        frames_since_reload=3, responses_since_reload=1)
    assert core.halted.startswith("challenge_or_login")
    core2, _ = _core(tmp_path)
    core2.on_page_loaded("trackers", "https://axiom.trade/trackers",
                         '<iframe src="https://challenges.cloudflare.com/turnstile"></iframe>',
                         frames_since_reload=0, responses_since_reload=0)
    assert core2.halted.startswith("challenge_or_login")


def test_three_empty_reloads_halt_but_a_live_one_resets_the_count(tmp_path):
    core, _ = _core(tmp_path)
    for _ in range(2):
        core.on_page_loaded("discover", "https://axiom.trade/discover", "<html>", frames_since_reload=0,
                            responses_since_reload=0)
    core.on_page_loaded("discover", "https://axiom.trade/discover", "<html>", frames_since_reload=1,
                        responses_since_reload=0)
    assert core.halted is None
    for _ in range(3):
        core.on_page_loaded("discover", "https://axiom.trade/discover", "<html>", frames_since_reload=0,
                            responses_since_reload=0)
    assert core.halted == "empty_reloads 3"


def test_a_parser_that_refuses_the_shape_keeps_the_raw_row_and_counts(tmp_path):
    class P:
        @staticmethod
        def parse_wallet_event(t): return None
        @staticmethod
        def parse_trader_directory(h): return []
        @staticmethod
        def parse_kol_callouts(h): return []
        @staticmethod
        def parse_discovery(h): return []
    core, _ = _core(tmp_path, parsers=P)
    core.on_ws_frame("wss://s", "garbage", page="trackers")
    core.on_page_loaded("trackers", "https://axiom.trade/trackers", "<html>x</html>",
                        frames_since_reload=1, responses_since_reload=0)
    assert core.conn.execute("SELECT COUNT(*) FROM axiom_raw_messages").fetchone()[0] == 1
    assert core.conn.execute("SELECT COUNT(*) FROM axiom_wallet_events").fetchone()[0] == 0
    assert core.parse_refused == 1


def test_a_parsed_event_lands_with_the_raw_row_id(tmp_path):
    class P:
        @staticmethod
        def parse_wallet_event(t):
            return {"address": "A", "chain": "solana", "mint": "M", "side": "buy", "amount_usd": 5.0,
                    "tx_sig": "S", "axiom_ts": "x"}
    core, _ = _core(tmp_path, parsers=P)
    rid = core.on_ws_frame("wss://s", "{}", page="trackers")
    row = core.conn.execute("SELECT raw_id, tx_sig FROM axiom_wallet_events").fetchone()
    assert (row[0], row[1]) == (rid, "S")


def test_heartbeat_reports_frames_in_the_last_minute_and_the_halt(tmp_path):
    core, clk = _core(tmp_path)
    core.on_ws_frame("wss://s", "{}", page="trackers")
    clk.mono += 61_000_000_000
    core.on_ws_frame("wss://s", "{}", page="trackers")
    core.write_heartbeat(pages_open=2)
    hb = json.loads((tmp_path / "hb.json").read_text())
    assert hb["ws_frames_60s"] == 1 and hb["frames_total"] == 2 and hb["halted"] is None


def test_prune_keeps_the_retention_window(tmp_path):
    core, clk = _core(tmp_path)
    clk.iso = "2026-09-01T00:00:00+00:00"; core.on_ws_frame("wss://s", "{}", page="p")
    clk.iso = "2026-09-16T00:00:00+00:00"; core.on_ws_frame("wss://s", "{}", page="p")
    assert core.prune(7) == 1
    assert core.conn.execute("SELECT COUNT(*) FROM axiom_raw_messages").fetchone()[0] == 1


def test_directory_upsert_skips_truncated_addresses():
    c = _c()
    n = dba.upsert_directory(c, [
        {"x_handle": "a", "address": None, "truncated": True, "chain": "solana"},
        {"x_handle": "b", "address": "FULLADDR", "chain": "solana", "pnl_24h": 1.0}], T0)
    assert n == 1


def test_the_worker_never_touches_the_funded_path():
    src = (REPO / "sniper" / "feeds" / "axiom_browser.py").read_text()
    for banned in ("memecoin_real_runner", "real_entry_hook", "memecoin_real_leg", "reserve_"):
        assert banned not in src
    loop = (REPO / "scripts" / "memecoin_runner_loop.py").read_text()
    assert "axiom" not in loop.lower()


def test_default_profile_dir_is_outside_the_repo():
    p = AB.default_profile_dir(env={})
    assert REPO not in p.parents and p != REPO


def test_a_repeated_rest_body_is_stored_once_and_frames_commit_in_batches(tmp_path):
    core, clk = _core(tmp_path)
    a = core.on_rest_response("https://api.axiom.trade/wo/catalogue?v=1", 200, '{"big":1}', page="t")
    b = core.on_rest_response("https://api.axiom.trade/wo/catalogue?v=2", 200, '{"big":1}', page="t")
    assert a is not None and b is None and core.rest_total == 1
    # a second connection sees nothing until a commit is due (batched), then everything
    core.conn.commit(); core._last_commit = clk.mono
    core.on_ws_frame("wss://s", "{}", page="t")
    assert core.conn.in_transaction
    clk.mono += 1_000_000_001
    core.on_ws_frame("wss://s", "{}", page="t")
    assert not core.conn.in_transaction


def test_raw_rows_go_to_the_attached_store_when_one_is_attached(tmp_path):
    c = _c()
    dba.attach_raw_store(c, tmp_path / "raw.db")
    dba.insert_raw(c, received_mono_ns=1, received_at=T0, transport="ws", source="s", page="p", payload="x")
    c.commit()
    assert c.execute("SELECT COUNT(*) FROM axraw.axiom_raw_messages").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM main.axiom_raw_messages").fetchone()[0] == 0
    assert dba.prune_raw(c, now_iso="2026-10-01T00:00:00+00:00", retention_days=7) == 1


def test_ensure_tables_migrates_a_raw_table_that_predates_body_sha():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE axiom_raw_messages (id INTEGER PRIMARY KEY, received_mono_ns INTEGER, "
              "received_at TEXT, transport TEXT, source TEXT, page TEXT, payload TEXT, byte_len INTEGER)")
    dba.ensure_axiom_tables(c)
    assert "body_sha" in {r[1] for r in c.execute("PRAGMA table_info(axiom_raw_messages)")}


def test_a_rest_body_is_routed_to_its_parser_by_endpoint_name(tmp_path):
    class P:
        @staticmethod
        def parse_trader_directory(body):
            return [{"x_handle": "h", "display": "H", "address": "FULL", "chain": "solana", "pnl_24h": 1.0,
                     "trades": 2, "volume": 3.0, "followers": 4}]
        @staticmethod
        def parse_wallet_event(t): return None
    core, _ = _core(tmp_path, parsers=P)
    core.on_rest_response("https://api6.axiom.trade/smart-money-traders?x=1", 200, '{"rows":[]}', page="trackers")
    core.on_rest_response("https://api6.axiom.trade/unknown-thing", 200, '{}', page="trackers")
    assert [tuple(r) for r in core.conn.execute("SELECT x_handle, address FROM axiom_trader_directory")] == [("h", "FULL")]
    assert core.parse_refused == 0


def test_429s_are_counted_in_the_heartbeat_and_never_halt(tmp_path):
    core, _ = _core(tmp_path)
    core.on_rest_response("https://api.axiom.trade/x", 429, "", page="t")
    core.on_rest_response("https://api.axiom.trade/y", 500, "", page="t")
    core.write_heartbeat(pages_open=1)
    hb = json.loads((tmp_path / "hb.json").read_text())
    assert hb["http_429"] == 1 and hb["http_errors"] == 1 and core.halted is None


def test_a_rest_receipt_keeps_the_callback_entry_stamp_not_the_post_body_time(tmp_path):
    core, clk = _core(tmp_path)
    entry = clk.mono
    clk.mono += 700_000_000                                   # a slow body read
    core.on_rest_response("https://api.axiom.trade/z", 200, '{"a":1}', page="t", received_mono_ns=entry)
    assert core.conn.execute("SELECT received_mono_ns FROM axiom_raw_messages").fetchone()[0] == entry


def test_a_generic_evm_directory_row_lands_as_chain_evm():
    c = _c()
    dba.upsert_directory(c, [{"x_handle": "h", "address": "0xabc", "chain": None}], T0)
    assert c.execute("SELECT chain FROM axiom_trader_directory").fetchone()[0] == "evm"


def test_discovery_gets_the_source_url_and_drops_rows_without_a_chain(tmp_path):
    seen = {}
    class P:
        @staticmethod
        def parse_discovery(body, source_url=None):
            seen["url"] = source_url
            return [{"chain": "4663", "mint": "0xm"}, {"chain": None, "mint": "0xn"}]
        @staticmethod
        def parse_wallet_event(t): return None
    core, _ = _core(tmp_path, parsers=P)
    core.on_rest_response("https://rh-api5.axiom.trade/discover-trending?x=1", 200, "[]", page="discover")
    assert seen["url"] == "https://rh-api5.axiom.trade/discover-trending"
    assert core.conn.execute("SELECT COUNT(*) FROM axiom_discovery_tokens").fetchone()[0] == 1


def test_a_page_document_403_backs_off_reloads_but_an_api_403_halts(tmp_path):
    core, clk = _core(tmp_path)
    core.on_rest_response("https://axiom.trade/discover", 403, "", page="discover", resource_type="document")
    assert core.halted is None and core.page_403 == 1
    assert core.backoff_until_mono == clk.mono + 300_000_000_000
    core.on_rest_response("https://axiom.trade/discover", 403, "", page="discover", resource_type="document")
    assert core.backoff_until_mono == clk.mono + 600_000_000_000     # doubled
    core.write_heartbeat(pages_open=2)
    assert json.loads((tmp_path / "hb.json").read_text())["reloads_paused_s"] == 600
    core.on_rest_response("https://api6.axiom.trade/smart-money-traders", 403, "", page="trackers")
    assert core.halted.startswith("http_403")


def test_a_wallet_transaction_without_a_mint_is_skipped_not_fatal(tmp_path):
    class P:
        @staticmethod
        def parse_wallet_transactions(body, source_url=None):
            return [{"address": "A", "chain": "solana", "mint": None, "side": "buy"},
                    {"address": "A", "chain": "solana", "mint": "M", "side": "sell", "tx_sig": "S"}]
        @staticmethod
        def parse_wallet_event(t): return None
    core, _ = _core(tmp_path, parsers=P)
    core.on_rest_response("https://api8.axiom.trade/tracked-wallet-transactions-v4", 200, "[]", page="t")
    assert core.conn.execute("SELECT COUNT(*) FROM axiom_wallet_events").fetchone()[0] == 1
    assert core.parse_refused == 0


def test_socket_open_and_close_are_counted_and_listed(tmp_path):
    core, _ = _core(tmp_path)
    core.on_ws_open("wss://cluster9.axiom.trade/?t=1", page="trackers")
    core.on_ws_close("wss://cluster9.axiom.trade/", page="trackers", error="ECONNRESET")
    core.write_heartbeat(pages_open=1)
    hb = json.loads((tmp_path / "hb.json").read_text())
    assert hb["ws_open"] == 1 and hb["ws_closed"] == 1
    assert hb["ws_recent"][-1][1] == "error" and hb["ws_recent"][0][2] == "wss://cluster9.axiom.trade/"


def test_a_repeated_wallet_event_keeps_its_first_receipt_stamp():
    c = _c()
    ev = {"address": "A", "chain": "solana", "mint": "M", "side": "buy", "tx_sig": "S"}
    dba.insert_wallet_event(c, ev, received_mono_ns=1, received_at=T0)
    dba.insert_wallet_event(c, ev, received_mono_ns=2, received_at=T0)
    rows = c.execute("SELECT received_mono_ns FROM axiom_wallet_events").fetchall()
    assert [r[0] for r in rows] == [1]


@pytest.mark.parametrize('path', ['rest', 'ws', 'page_error'])
def test_parsed_callbacks_release_the_main_ledger_before_idle(tmp_path, path):
    class Parsers:
        @staticmethod
        def parse_trader_directory(body):
            return [{'x_handle': 'trader', 'address': 'ADDR', 'chain': 'solana'}]

        @staticmethod
        def parse_kol_callouts(body):
            raise ValueError('Invalid page shape after the directory write')

        @staticmethod
        def parse_wallet_event(body):
            return {'address': 'ADDR', 'chain': 'solana', 'mint': 'MINT',
                    'side': 'buy', 'tx_sig': 'TX'}

    db_path = tmp_path / 'ledger.db'
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    dba.ensure_axiom_tables(conn)
    dba.attach_raw_store(conn, tmp_path / 'raw.db')
    conn.execute('CREATE TABLE other_writer (value INTEGER)')
    conn.commit()
    core = AB.CaptureCore(conn, now_iso_fn=lambda: T0, mono_ns_fn=lambda: 1,
                          parsers=Parsers)
    other = sqlite3.connect(db_path, timeout=0)
    try:
        if path == 'rest':
            core.on_rest_response('https://api.axiom.trade/smart-money-traders',
                                  200, '{}', page='trackers')
        elif path == 'ws':
            core.on_ws_frame('wss://socket.axiom.trade/', '{}', page='trackers')
        else:
            core.on_page_loaded('trackers', 'https://axiom.trade/trackers', '<html>',
                                frames_since_reload=1, responses_since_reload=0)
        other.execute('INSERT INTO other_writer VALUES (1)')
        other.commit()
        assert not conn.in_transaction
        assert other.execute('SELECT value FROM other_writer').fetchone()[0] == 1
    finally:
        other.close()
        conn.close()


@pytest.mark.parametrize('host,chain', [('rh-api5', '4663'), ('bnb-api4', '56'), ('base-api2', '8453')])
def test_rest_wallet_rows_keep_the_source_chain_and_callback_clock(tmp_path, host, chain):
    from sniper.feeds import axiom_parse
    core, clk = _core(tmp_path, parsers=axiom_parse)
    row = [None] * 25
    row[0], row[1], row[2] = T0, '0x' + 'a' * 40, 'TX'
    row[7], row[9], row[16] = 12.5, 'buy', '0x' + 'b' * 40
    arrival = clk.mono
    clk.mono += 700_000_000
    core.on_rest_response(f'https://{host}.axiom.trade/tracked-wallet-transactions-v4',
                          200, json.dumps([row]), page='trackers',
                          received_mono_ns=arrival)
    saved = core.conn.execute('SELECT chain, received_mono_ns, amount_usd FROM axiom_wallet_events').fetchall()
    assert [tuple(r) for r in saved] == [(chain, arrival, 12.5)]


def test_repeated_rest_body_refreshes_directory_after_parser_repair(tmp_path):
    class P:
        @staticmethod
        def parse_trader_directory(body):
            return [{'x_handle':'trader','address':'ADDR','chain':'solana'}]
    core,clk=_core(tmp_path,parsers=P)
    url='https://api.axiom.trade/smart-money-traders'
    core.on_rest_response(url,200,'{}',page='trackers')
    core.conn.execute('DELETE FROM axiom_trader_directory');core.conn.commit()
    clk.iso='2026-09-16T10:01:00+00:00'
    assert core.on_rest_response(url,200,'{}',page='trackers') is None
    assert core.conn.execute('SELECT seen_at FROM axiom_trader_directory').fetchone()[0]==clk.iso
    assert core.conn.execute('SELECT COUNT(*) FROM axiom_raw_messages').fetchone()[0]==1


def test_saved_halt_stops_before_browser_open(tmp_path,monkeypatch):
    core,_=_core(tmp_path)
    core.halt('http_403 saved')
    again,_=_core(tmp_path)
    monkeypatch.setattr(AB,'open_browser',lambda *a,**k: pytest.fail('Browser opened after saved halt'))
    assert AB.run_capture(again,cdp_url='http://127.0.0.1:1',pages=['trackers'],page_interval_s=1,
                          retention_days=7,max_cycles=1)=='http_403 saved'


def test_apex_xhr_403_halts_instead_of_document_backoff(tmp_path):
    core,_=_core(tmp_path)
    core.on_rest_response('https://axiom.trade/api/private',403,'',page='trackers',resource_type='xhr')
    assert core.halted.startswith('http_403')


def test_parsed_receipts_record_live_and_history_provenance(tmp_path):
    class P:
        PARSER_VERSION='test-parser'
        @staticmethod
        def parse_wallet_event(body):
            return dict(address='A',chain='solana',mint='M',side='buy',tx_sig='S',observation_kind='live')
        @staticmethod
        def parse_wallet_transactions(body,source_url=None):
            return [P.parse_wallet_event(body)]
    core,clk=_core(tmp_path,parsers=P)
    core.on_rest_response('https://api.axiom.trade/tracked-wallet-transactions-v4',200,'[]',page='trackers')
    clk.mono+=1
    core.on_ws_frame('wss://cluster.axiom.trade','{}',page='trackers')
    rows=core.conn.execute('SELECT transport,observation_kind,clock_domain,capture_run_id FROM axiom_wallet_events ORDER BY id').fetchall()
    assert [(x[0],x[1]) for x in rows]==[('rest','history'),('ws','live')]
    assert rows[0][2] and rows[0][2]==rows[1][2] and rows[0][3]==rows[1][3]


def test_rest_receipts_keep_both_callback_clocks(tmp_path):
    core,clk=_core(tmp_path)
    clk.iso='2026-09-16T10:00:02+00:00'
    core.on_rest_response('https://api.axiom.trade/x',200,'{}',page='trackers',received_mono_ns=9,received_at=T0)
    assert tuple(core.conn.execute('SELECT received_mono_ns,received_at FROM axiom_raw_messages').fetchone())==(9,T0)


def test_ws_callouts_reach_research_table_without_wallet_event(tmp_path):
    class P:
        @staticmethod
        def parse_wallet_event(body): return None
        @staticmethod
        def parse_kol_callouts(body):
            return [dict(kol_handle='caller',mint='mint',chain='solana',text='call')]
    core,_=_core(tmp_path,parsers=P)
    body=json.dumps({'room':'gmgn-callouts','content':{'callout':{}}})
    core.on_ws_frame('wss://cluster.axiom.trade',body,page='trackers')
    core.on_ws_frame('wss://cluster.axiom.trade',body,page='trackers')
    assert core.conn.execute('SELECT COUNT(*) FROM axiom_kol_callouts').fetchone()[0]==1
    assert not core.conn.in_transaction


def test_capture_lock_rejects_a_second_owner(tmp_path):
    from scripts.axiom_session import capture_lock
    lock_path=tmp_path/'capture.lock'
    with capture_lock(lock_path):
        with pytest.raises(BlockingIOError):
            with capture_lock(lock_path):
                pytest.fail('second capture acquired the lock')
    with capture_lock(lock_path):
        pass


def test_ws_transport_alone_does_not_prove_live_event(tmp_path):
    class P:
        @staticmethod
        def parse_wallet_event(body):
            return dict(address='A',chain='solana',mint='M',side='buy',tx_sig='S')
    core,_=_core(tmp_path,parsers=P)
    core.on_ws_frame('wss://cluster.axiom.trade','{}',page='trackers')
    assert core.conn.execute('SELECT observation_kind FROM axiom_wallet_events').fetchone()[0]=='unknown'


def test_sensitive_wallet_bundle_is_not_recorded(tmp_path):
    core,_=_core(tmp_path)
    core.on_rest_response('https://api.axiom.trade/bundle-key-and-wallets-v2',200,
                          '{"privateKey":"DO_NOT_STORE"}',page='trackers')
    assert core.conn.execute('SELECT COUNT(*) FROM axiom_raw_messages').fetchone()[0]==0


def test_supported_ws_discovery_reaches_research(tmp_path):
    class P:
        @staticmethod
        def parse_wallet_event(body): return None
        @staticmethod
        def parse_discovery(body):
            return [dict(chain='solana',mint='M',mcap=10,mcap_unit='SOL',parser_version='axiom-discovery-v2')]
    core,_=_core(tmp_path,parsers=P)
    core.on_ws_frame('wss://cluster.axiom.trade',json.dumps({'room':'sol-discover:trending:5m','content':[]}),page='discover')
    assert core.conn.execute('SELECT mcap_unit FROM axiom_discovery_tokens').fetchone()[0]=='SOL'
    assert not core.conn.in_transaction


def test_one_closed_tab_reopens_after_one_heartbeat_interval(tmp_path, monkeypatch):
    from contextlib import nullcontext
    import playwright.sync_api
    elapsed = [0]
    class Page:
        url = 'https://axiom.trade'
        closed = False
        def is_closed(self): return self.closed
        def close(self): self.closed = True
        def on(self, *args): pass
        def add_init_script(self, *args): pass
        def goto(self, url, **kwargs): self.url = url
        def wait_for_timeout(self, *args): pass
        def content(self): return '<html></html>'
    class Context:
        pages = []
        opened_at = []
        def new_page(self):
            page = Page(); self.pages.append(page); self.opened_at.append(elapsed[0]); return page
    ctx = Context()
    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', lambda: nullcontext(object()))
    monkeypatch.setattr(AB, 'open_browser', lambda *args, **kwargs: (ctx, lambda: None))
    core, _ = _core(tmp_path)
    monkeypatch.setattr(core, 'on_page_loaded', lambda *args, **kwargs: None)
    def wait(seconds):
        elapsed[0] += seconds
        if len(ctx.pages) == 2:
            ctx.pages[1].close()
    AB.run_capture(core, cdp_url='test', pages=['trackers','discover'], page_interval_s=300,
                   retention_days=7, max_cycles=2, sleep_fn=wait)
    assert ctx.opened_at == [0, 0, 30]


def test_the_wait_loop_closes_a_write_left_open_by_a_silent_feed(tmp_path):
    """The heartbeat tick force-commits, so a silent feed cannot hold the funded ledger."""
    core, clk = _core(tmp_path)
    core.on_ws_frame("wss://s", "{}", page="t")          # batched: transaction still open
    assert core.conn.in_transaction
    core._commit_if_due(force=True)                       # what the wait loop now does every 30 s
    assert not core.conn.in_transaction
