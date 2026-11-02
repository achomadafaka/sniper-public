"""Refused-mint forward capture: identity, isolation, censoring and completeness."""
import datetime as dt
import sqlite3

import pytest

from sniper.services import memecoin_refusal_capture as cap


def test_roundtrip_capture_uses_the_reserved_notional():
    from scripts.memecoin_refusal_capture_loop import poll_refusals
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.reserve_pending(conn, mint="FLAME", source_user_id="u", strategy="alpha_b",
                            notional=175, reserved_at="2026-09-11T10:15:29+00:00", attempt_id="A")
    conn.execute("INSERT INTO memecoin_entry_attempts "
                 "(attempt_id,mint,origin_event_id,terminal_reason,decision_started_at,network_id,terminal_status,pending_id,completed_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", ("A", "FLAME", "O", "round trip 13.04% over 10.0% cap (sell back $152.18 of $175.00)",
                                               "2026-09-11T10:15:29+00:00", "solana", "refused", pid, "2026-09-11T10:15:29+00:00"))
    rows, _ = poll_refusals(conn, since_iso="2026-09-11T10:15:00+00:00")
    assert rows[0]["quote_input_usd"] == 175
    capture = cap.start({}, mint="FLAME", origin_event_id="O", gate="roundtrip_cap",
                        refused_at=rows[0]["refused_at"], horizon_s=810, cadence_s=25,
                        max_concurrent=2, now=0, now_epoch=1750000000, quote_input_usd=rows[0]["quote_input_usd"])
    sizes = []
    row = cap.quote_once(capture, http=lambda *_a, **_k: (200, {}, None), rpc_url="rpc",
                         clip_usd=20, now_iso=rows[0]["refused_at"], now_mono=0,
                         quote_fn=lambda mint, usd, **_: sizes.append(usd) or {"outAmount": "100000000"},
                         decimals_fn=lambda *_a, **_k: 6)
    assert sizes == [175] and row["quote_input_usd"] == 175
    assert row["token_qty_atomic"] == "100000000"
    conn.close()


def test_roundtrip_capture_never_substitutes_twenty_dollars_for_unknown_size():
    refusals = []
    result = cap.start({}, mint="M", origin_event_id="O", gate="roundtrip_cap",
                       refused_at="2026-09-11T10:15:29+00:00", horizon_s=810, cadence_s=25,
                       max_concurrent=2, now=0, now_epoch=1750000000, on_refuse=lambda *args: refusals.append(args))
    assert result is None
    assert refusals[0][1] == "not_captured_size_unknown"


class TestGateIdentity:
    """Exact identity, never a prefix. The bare mcap-band strings must NOT be captured: they are a
    retired gate that merely shares a word with a live one, and they carry 349 refusals."""

    @pytest.mark.parametrize("reason,gate", [
        ("m5 volume $485 under floor $1790 (m5_turnover_under_floor, revival guard)",
         "m5_turnover_under_floor"),
        ("m5 volume $485 under floor $1790 (m5_turnover_under_big_floor)",
         "m5_turnover_under_big_floor"),
        ("m5 volume $485 under floor $1790 (m5_turnover_under_big_floor, revival guard)",
         "m5_turnover_under_big_floor"),
        ("m5 volume $9 over ceiling $3 (m5_turnover_over_ceiling)", "m5_turnover_over_ceiling"),
        ("h1 volume $28 under floor $176 (turnover_under_floor, revival guard)",
         "turnover_under_floor"),
        ("coin ran 4.2x above first-seen price (cap 3.0x)", "chase_first_seen"),
        ("coin already ran +49% past their entry (cap 30%)", "chase_past_entry"),
        # `chase_quote`, not `execution_premium` (controls review, 2026-09-08). This string comes
        # from the CHASE-on-quote gate, which measures the quote against the copied trader's price
        # and returns `skipped_chase`. The execution-premium gate measures against the CHART price
        # and is the `route quote is Nx the chart mid` row below. This case asserted the mislabel.
        ("exact quote is +43% above source price (cap 30%)", "chase_quote"),
        ("route quote is 2.4x the chart mid (cap 2.0x)", "route_over_mid"),
        ("losing re-entry: our last real position on this coin lost", "losing_reentry"),
        ("re-entry cooldown: a real position on this coin closed within", "reentry_cooldown"),
    ])
    def test_live_gates_classify(self, reason, gate):
        assert cap.classify_gate(reason) == gate

    @pytest.mark.parametrize("reason", [
        "under_floor", "over_ceiling",                       # RETIRED mcap band
        "h1 volume $4 under floor $176 (revival guard)",      # live but publishes no gate token
        "not_approved", "unroutable_chain", "disarmed", "no quote", "insufficient bankroll",
        "one position per coin: already held or reserved",
        "no planned-quantity sell route", "", None,
    ])
    def test_never_captured(self, reason):
        assert cap.classify_gate(reason) is None

    def test_source_buy_floor_is_captured_because_its_slot_is_shown(self):
        """REVERSED 2026-09-17 (operator). The refusal slot shows source_buy_floor refusals with an
        ENTER button. Without a capture the card had no price and no move (POKEMON 07:51Z), so a
        card the operator can act on showed "...". A card that is shown must be priced."""
        gate = cap.classify_gate("source buy $0 (0.000%) under mcap floor 0.500% ($500)")
        assert gate == "source_buy_floor"
        assert cap.should_capture(gate) is True

    # Real reason strings from the ledger, 2026-09-16..29 (Gamma root cause, 2026-09-30): 790 and 91
    # refusals of these two families had no matcher, so the capture loop dropped every one.
    @pytest.mark.parametrize("reason,gate", [
        ("source buy $100 under absolute floor $197", "source_buy_abs_floor"),
        ("source buy $5 under absolute floor $197", "source_buy_abs_floor"),
        ("h1 organic volume $796 under floor $3000 (under_abs_floor)", "under_abs_floor"),
        ("h1 organic volume $715 under floor $3000 (under_abs_floor, revival guard)",
         "under_abs_floor"),
        ("h1 organic volume $328 under floor $624 (under_abs_floor, 2.00% of mcap $31205)",
         "under_abs_floor"),
        ("h1 organic volume $1 under floor $250 (under_abs_floor, pro-rated to pair age 105s)",
         "under_abs_floor"),
        ("h1 organic volume $3 under floor $129 (under_abs_floor, 2.00% of mcap $34946, "
         "pro-rated to pair age 664s)", "under_abs_floor"),
        # neighbours that must keep their own family
        ("m5 organic volume $31 under floor $5000 (m5_volume_under_abs_floor, revival guard)",
         "m5_volume_under_abs_floor"),
        ("source buy $0 (0.000%) under mcap floor 0.500% ($500)", "source_buy_floor"),
        ("drawdown pause: today's realized PnL fell $100 below the day's high; automatic entries "
         "resume 11:04:53 UTC", "drawdown_pause"),
    ])
    def test_absolute_floors_are_captured(self, reason, gate):
        assert cap.classify_gate(reason) == gate
        assert cap.should_capture(gate) is True

    # Real reason strings from the ledger, 2026-09-16..29 (Alpha GO 2026-09-30): the knobs we want
    # to grade, each uncaptured until now.
    @pytest.mark.parametrize("reason,gate", [
        ("coin_too_young: 60s old, floor 120s", "coin_too_young"),
        ("coin_too_young: mint 24s old (pair age unknown), floor 120s", "coin_too_young"),
        ("daily loss limit reached (latched)", "daily_loss_limit"),
        ("entry older than 60s", "entry_stale"),
        ("post_peak_confirmed", "post_peak_confirmed"),
        ("sell_sim_min_out_repeated: fresh funded sell simulation still hit router min-out \u2014 "
         "fail-closed", "sell_sim_min_out_repeated"),
        ("m5 organic volume $0 under floor $1522 (m5_volume_unreadable)", "m5_volume_unreadable"),
    ])
    def test_graded_knobs_are_captured(self, reason, gate):
        assert cap.classify_gate(reason) == gate
        assert cap.should_capture(gate) is True

    @pytest.mark.parametrize("reason", [
        # safety verdicts stay uncaptured (a verdict, not a knob)
        "exit route pays HiMSSzzw at a Pump.fun hop; Jupiter cannot build that sell (unsellable trap, "
        "fries 123)",
        "live mint authority (infinite-mint rug risk)",
        # the same text embedded in a contract failure is not the gate
        "invalid_funded_contract: coin_too_young: 60s old, floor 120s",
        "invalid_funded_contract: post_peak_confirmed",
        # the m5 txns twin keeps its own family, never the volume one
    ])
    def test_verdicts_and_embedded_text_are_not_captured(self, reason):
        assert cap.classify_gate(reason) is None

    def test_the_m5_txns_twin_keeps_its_own_family(self):
        assert cap.classify_gate("m5 txns 0 under floor 3 (m5_txns_unreadable)") == "m5_txns_unreadable"

    def test_the_absolute_source_buy_ceiling_has_no_family_until_it_fires(self):
        assert cap.classify_gate("source buy $2500 over absolute ceiling $2000") is None

    def test_every_named_family_is_captured(self):
        """Guards the capture set from quietly shrinking."""
        named = {g for g, _ in cap.GATES}
        assert named - cap.CAPTURED_FAMILIES == set()


class TestRefusalPoll:
    @pytest.fixture
    def ledger(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE memecoin_entry_attempts (
            attempt_id TEXT PRIMARY KEY, mint TEXT, origin_event_id TEXT,
            terminal_reason TEXT, decision_started_at TEXT,
            network_id TEXT, terminal_status TEXT, completed_at TEXT)""")
        yield conn
        conn.close()

    def _insert(self, ledger, attempt_id, at, *, reason=None, completed_at=None,
                status="refused", network="solana"):
        ledger.execute(
            "INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?,?,?,?,?)",
            (attempt_id, "mint", "origin-" + attempt_id, reason, at,
             network, status, completed_at))

    def test_pages_keep_every_origin_at_a_shared_timestamp(self, ledger):
        from scripts.memecoin_refusal_capture_loop import poll_refusals
        for attempt in ("c", "a", "b"):
            self._insert(ledger, attempt, "2026-09-01T00:00:01+00:00",
                         reason="coin ran 4.2x above first-seen price (cap 3.0x)",
                         completed_at="2026-09-01T00:00:02+00:00")
        cursor, got = "2026-09-01T00:00:00+00:00", []
        for _ in range(4):
            page, cursor = poll_refusals(ledger, since_iso=cursor, limit=1)
            got.extend(page)
        assert sorted(row["origin_event_id"] for row in got) == ["origin-a", "origin-b", "origin-c"]
        assert {row["refused_at"] for row in got} == {"2026-09-01T00:00:01+00:00"}

    @pytest.mark.parametrize("completed_at", [
        "2026-09-01T00:00:02+00:00",
        "2026-09-01T00:00:03+00:00",
        "2026-09-01T00:00:04+00:00",
    ])
    def test_unfinished_attempt_is_seen_after_it_completes(self, ledger, completed_at):
        from scripts.memecoin_refusal_capture_loop import poll_refusals
        reason = "coin ran 4.2x above first-seen price (cap 3.0x)"
        self._insert(ledger, "slow", "2026-09-01T00:00:01+00:00", status=None)
        self._insert(ledger, "fast", "2026-09-01T00:00:02+00:00", reason=reason,
                     completed_at="2026-09-01T00:00:03+00:00")
        page, cursor = poll_refusals(ledger, since_iso="2026-09-01T00:00:00+00:00")
        assert [row["origin_event_id"] for row in page] == ["origin-fast"]
        ledger.execute(
            "UPDATE memecoin_entry_attempts SET terminal_status='refused', terminal_reason=?, "
            "completed_at=? WHERE attempt_id='slow'",
            (reason, completed_at))
        page, cursor = poll_refusals(ledger, since_iso=cursor)
        assert [row["origin_event_id"] for row in page] == ["origin-slow"]
        assert page[0]["refused_at"] == "2026-09-01T00:00:01+00:00"
        assert poll_refusals(ledger, since_iso=cursor) == ([], cursor)

    def test_filtered_pages_advance_and_source_buy_floor_is_captured(self, ledger):
        from scripts.memecoin_refusal_capture_loop import poll_refusals
        at = "2026-09-01T00:00:01+00:00"
        self._insert(ledger, "a", at,
                     reason="source buy $0 (0.000%) under mcap floor 0.500% ($500)")
        self._insert(ledger, "b", at, reason="under_floor")
        self._insert(ledger, "c", at,
                     reason="m5 volume $485 under floor $1790 (m5_turnover_under_big_floor)",
                     status=None)
        cursor = "2026-09-01T00:00:00+00:00"
        page, cursor = poll_refusals(ledger, since_iso=cursor, limit=1)
        assert [row["gate"] for row in page] == ["source_buy_floor"]
        page, cursor = poll_refusals(ledger, since_iso=cursor, limit=1)
        assert page == []
        page, cursor = poll_refusals(ledger, since_iso=cursor, limit=1)
        assert page == [{"mint": "mint", "origin_event_id": "origin-c",
                         "gate": "m5_turnover_under_big_floor", "refused_at": at,
                         "network_id": "solana", "attempt_id": "c"}]   # 2026-09-29: slot-end shadow

    def test_unfinished_pages_do_not_block_new_rows_or_pending_rows(self, ledger):
        from scripts.memecoin_refusal_capture_loop import poll_refusals
        at = "2026-09-01T00:00:01+00:00"
        reason = "coin ran 4.2x above first-seen price (cap 3.0x)"
        for attempt in ("a", "b", "c"):
            self._insert(ledger, attempt, at, status=None)
        self._insert(ledger, "d", at, reason=reason, completed_at=at)
        cursor, got = "2026-09-01T00:00:00+00:00", []
        for _ in range(4):
            page, cursor = poll_refusals(ledger, since_iso=cursor, limit=1)
            got.extend(page)
        assert [row["origin_event_id"] for row in got] == ["origin-d"]
        ledger.execute(
            "UPDATE memecoin_entry_attempts SET terminal_status='refused', terminal_reason=?, "
            "completed_at=? WHERE attempt_id='c'", (reason, at))
        got = []
        for _ in range(3):
            page, cursor = poll_refusals(ledger, since_iso=cursor, limit=1)
            got.extend(page)
        assert [row["origin_event_id"] for row in got] == ["origin-c"]

    def test_late_insert_with_same_timestamp_keeps_origin_and_scope(self, ledger):
        from scripts.memecoin_refusal_capture_loop import poll_refusals
        at = "2026-09-01T00:00:01+00:00"
        reason = "coin ran 4.2x above first-seen price (cap 3.0x)"
        self._insert(ledger, "z", at, reason=reason, completed_at=at)
        page, cursor = poll_refusals(ledger, since_iso="2026-09-01T00:00:00+00:00")
        assert [row["origin_event_id"] for row in page] == ["origin-z"]
        self._insert(ledger, "old", "2026-09-01T00:00:00+00:00", reason=reason)
        self._insert(ledger, "evm", at, reason=reason, network="4663")
        self._insert(ledger, "filled", at, reason=reason, status="filled")
        self._insert(ledger, "a", at, reason=reason, completed_at=at)
        page, cursor = poll_refusals(ledger, since_iso=cursor)
        assert [row["origin_event_id"] for row in page] == ["origin-a"]
        assert poll_refusals(ledger, since_iso=cursor) == ([], cursor)


class TestKeyDisjointness:
    def test_overlap_fails_startup(self):
        env = {"MEMECOIN_CAPTURE_RPC_POOL_URLS": "https://a/?k=1",
               "MEMECOIN_SOLANA_RPC_URL": "https://a/?k=1"}
        with pytest.raises(RuntimeError) as e:
            cap.assert_pool_disjoint(env)
        assert "https://a" not in str(e.value)          # the URL carries the key; never log it

    def test_unset_fails_startup(self):
        with pytest.raises(RuntimeError):
            cap.assert_pool_disjoint({})

    def test_it_catches_a_role_it_was_never_told_about(self):
        """The first version hardcoded eight variable names and five did not exist -- it passed
        while checking a partly imaginary list. The subject set is DERIVED now, so a key added
        tomorrow is covered without editing this module."""
        env = {"MEMECOIN_CAPTURE_RPC_POOL_URLS": "https://x/?k=1",
               "SOME_BRAND_NEW_RPC_URL": "https://x/?k=1"}
        with pytest.raises(RuntimeError):
            cap.assert_pool_disjoint(env)

    def test_the_real_research_key_name_is_covered(self):
        env = {"MEMECOIN_CAPTURE_RPC_POOL_URLS": "https://r/?k=9",
               "MEMECOIN_RESEARCH_HELIUS_RPC_URL": "https://r/?k=9"}
        with pytest.raises(RuntimeError):
            cap.assert_pool_disjoint(env)

    def test_unrelated_variables_are_not_subjects(self):
        env = {"MEMECOIN_CAPTURE_RPC_POOL_URLS": "https://cap/?k=1",
               "SOME_API_TOKEN": "https://cap/?k=1"}
        assert cap.assert_pool_disjoint(env) == ["https://cap/?k=1"]

    def test_disjoint_passes(self):
        env = {"MEMECOIN_CAPTURE_RPC_POOL_URLS": "https://cap/?k=1",
               "MEMECOIN_SOLANA_RPC_POOL_URLS": "https://f1/?k=2 https://f2/?k=3"}
        assert cap.assert_pool_disjoint(env) == ["https://cap/?k=1"]


class TestJupiterYield:
    def test_429_shuts_the_capture_and_releases(self):
        t = {"now": 0.0}
        y = cap.JupiterYield(hold_s=120.0, clock=lambda: t["now"])
        assert not y.blocked()
        assert y.observe(429) is True
        assert y.blocked()
        t["now"] = 119.0
        assert y.blocked()
        t["now"] = 121.0
        assert not y.blocked()

    def test_a_non_urllib_transport_still_trips_the_yield(self):
        """The seam's whole job is to not miss a 429. `getattr(exc, "code")` reads urllib only, so
        swapping the transport for requests or httpx would silence the yield with nothing failing
        loudly. Four attributes, via the pool's own extractor."""
        from sniper.execution.solana_rpc_pool import _exception_status

        class _RequestsStyle(Exception):
            status_code = 429

        class _WithResponse(Exception):
            class response:
                status_code = 429

        assert _exception_status(_RequestsStyle()) == 429
        assert _exception_status(_WithResponse()) == 429

        caps = {}
        c = _start(caps, "m")
        y = cap.JupiterYield(clock=lambda: 0.0)

        def raising_http(method, url, headers=None, body=None, timeout=10):
            raise _RequestsStyle("throttled")

        cap.quote_once(c, http=raising_http, rpc_url="u", clip_usd=20.0, now_iso="t",
                       now_mono=1.0, jup_yield=y,
                       quote_fn=lambda m, usd, http=None: http("GET", "https://lite-api.jup.ag/x"),
                       decimals_fn=lambda m, u, http=None: 6)
        assert y.blocked(), "a non-urllib 429 must shut the capture like a urllib one"

    def test_it_never_fails_over_to_the_host_funded_falls_back_to(self):
        """A 429 on Jupiter's primary must not send the capture to `api.jup.ag` -- that is the host
        the FUNDED runner falls back to, so failing over would move research load onto funded's
        escape hatch at the exact moment funded needs it. The property is invisible in the happy
        path, so it is pinned by host here rather than left to the code reading correctly."""
        from sniper.execution.solana_real_seams import JUP_BASE, JUP_FALLBACK_BASE

        caps = {}
        c = _start(caps, "m")
        y = cap.JupiterYield(clock=lambda: 0.0)
        y.observe(429)

        hosts = []

        def recording_http(method, url, headers=None, body=None, timeout=10):
            hosts.append(url)
            return 200, {}, None

        for _ in range(5):
            cap.quote_once(c, http=recording_http, rpc_url="u", clip_usd=20.0, now_iso="t",
                           now_mono=1.0, jup_yield=y,
                           quote_fn=lambda m, usd, http=None: http("GET", JUP_FALLBACK_BASE),
                           decimals_fn=lambda m, u, http=None: 6)
        assert hosts == [], "yielding must make NO provider call, on either host"
        assert not any(JUP_FALLBACK_BASE in h for h in hosts)
        assert not any(JUP_BASE in h for h in hosts)

    def test_other_statuses_do_not_shut_it(self):
        y = cap.JupiterYield(clock=lambda: 0.0)
        for s in (200, 400, 404, 500, None):
            assert y.observe(s) is False
        assert not y.blocked()


REFUSED_AT = "2026-08-26T00:00:00+00:00"
# Anchored to REFUSED_AT, so the staleness guard sees a fresh refusal rather than the wall clock.
FRESH_EPOCH = dt.datetime(2026, 8, 26, 0, 0, 1, tzinfo=dt.timezone.utc).timestamp()


def _start(caps, mint, *, now=0.0, max_concurrent=2, on_refuse=None):
    return cap.start(caps, mint=mint, origin_event_id="o-" + mint, gate="chase_first_seen",
                     refused_at=REFUSED_AT, horizon_s=810.0, cadence_s=5.0,
                     max_concurrent=max_concurrent, now=now, on_refuse=on_refuse,
                     now_epoch=FRESH_EPOCH)


class TestCapacityIsRefusalNotDelay:
    def test_overflow_refuses_and_records_rather_than_queueing(self):
        caps, refused = {}, []
        for m in ("a", "b"):
            assert _start(caps, m, on_refuse=lambda *a: refused.append(a)) is not None
        assert _start(caps, "c", on_refuse=lambda *a: refused.append(a)) is None
        assert len(caps) == 2                             # never queued
        assert [r[1] for r in refused] == ["not_captured_capacity"]

    def test_a_finished_capture_frees_the_slot(self):
        caps = {}
        _start(caps, "a"); _start(caps, "b")
        caps[cap.capture_id("solana", "a", "o-a")].done = True
        assert _start(caps, "c") is not None

    def test_expected_obs_is_fixed_before_any_observation(self):
        caps = {}
        c = _start(caps, "a")
        assert c.expected_obs == 162                       # 810 / 5, the reaper's denominator

    def test_a_refusal_past_its_own_horizon_is_never_started(self):
        """A worker restart polls a BACKLOG. Without this guard each stale refusal opens a capture
        that begins after its window closed, whose first observation already reaches the horizon,
        which the coverage test then stamps `complete` -- fabricated series in the one dataset
        built to answer a counterfactual."""
        caps, refused = {}, []
        got = cap.start(caps, mint="m", origin_event_id="o", gate="chase_first_seen",
                        refused_at="2026-08-26T00:00:00+00:00", horizon_s=810.0, cadence_s=5.0,
                        max_concurrent=4, now=0.0,
                        now_epoch=dt.datetime(2026, 8, 26, 6, 0,
                                              tzinfo=dt.timezone.utc).timestamp(),
                        on_refuse=lambda *a: refused.append(a))
        assert got is None and caps == {}
        assert [r[1] for r in refused] == ["not_captured_stale"]

    def test_a_fresh_refusal_still_starts(self):
        caps = {}
        assert cap.start(caps, mint="m", origin_event_id="o", gate="chase_first_seen",
                         refused_at="2026-08-26T00:00:00+00:00", horizon_s=810.0, cadence_s=5.0,
                         max_concurrent=4, now=0.0,
                         now_epoch=dt.datetime(2026, 8, 26, 0, 0, 3,
                                               tzinfo=dt.timezone.utc).timestamp()) is not None

    def test_duplicate_origin_does_not_double_capture(self):
        caps = {}
        assert _start(caps, "a") is not None
        assert _start(caps, "a") is None


class TestQuoteObservation:
    def _cap(self):
        caps = {}
        return _start(caps, "MINT")

    def test_price_is_usd_per_token_from_the_exact_route(self):
        c = self._cap()
        row = cap.quote_once(
            c, http=lambda *a, **k: (200, {}, None), rpc_url="u", clip_usd=20.0,
            now_iso="2026-08-26T00:00:10+00:00", now_mono=10.0,
            quote_fn=lambda m, usd, http=None: {"outAmount": "2000000", "quote_id": "q1"},
            decimals_fn=lambda m, u, http=None: 6)
        assert row["quality"] == "ok"
        assert row["quote_price"] == pytest.approx(10.0)   # $20 for 2.0 tokens
        assert row["age_s"] == pytest.approx(10.0)
        assert row["horizon_s"] == 810.0

    def test_the_first_observation_buys_and_the_rest_sell_that_bag(self):
        """An all-buy series never pays the exit spread and reads better than the position would.

        The entry fixes the bag; every later observation values THAT bag on the sell side, which is
        funded's own expected_usdc / tokens convention.
        """
        c = self._cap()
        seen = []
        common = dict(http=lambda *a, **k: (200, {}, None), rpc_url="u", clip_usd=20.0,
                      decimals_fn=lambda m, u, http=None: 6)

        def buy(m, usd, http=None):
            seen.append("buy")
            return {"outAmount": "2000000", "quote_id": "b"}          # $20 -> 2.0 tokens

        def sell(m, atomic, slip, http=None):
            seen.append(("sell", atomic))
            return {"outAmount": "19000000", "quote_id": "s"}          # 2.0 tokens -> $19

        first = cap.quote_once(c, now_iso="2026-08-26T00:00:01+00:00", now_mono=1.0,
                               quote_fn=buy, exit_fn=sell, **common)
        second = cap.quote_once(c, now_iso="2026-08-26T00:00:26+00:00", now_mono=26.0,
                                quote_fn=buy, exit_fn=sell, **common)

        assert first["quote_side"] == "buy" and first["quote_price"] == pytest.approx(10.0)
        assert second["quote_side"] == "sell"
        # $19 received for the 2.0-token bag = $9.50/token: the 5% round trip is PAID, not hidden.
        assert second["quote_price"] == pytest.approx(9.5)
        assert seen == ["buy", ("sell", 2000000)], seen

    def test_the_bag_is_fixed_by_the_entry_and_never_requoted(self):
        """A later sell must price the ORIGINAL bag. Re-deriving it from a fresh notional would
        silently track the coin's own move and cancel the very signal we are capturing."""
        c = self._cap()
        common = dict(http=lambda *a, **k: (200, {}, None), rpc_url="u", clip_usd=20.0,
                      decimals_fn=lambda m, u, http=None: 6)
        asked = []
        cap.quote_once(c, now_iso="2026-08-26T00:00:01+00:00", now_mono=1.0,
                       quote_fn=lambda m, usd, http=None: {"outAmount": "2000000"},
                       exit_fn=lambda m, a, s, http=None: {"outAmount": "1"}, **common)
        for i, t in enumerate((26.0, 51.0, 76.0)):
            cap.quote_once(c, now_iso="2026-08-26T00:0%d:00+00:00" % (i + 1), now_mono=t,
                           quote_fn=lambda m, usd, http=None: {"outAmount": "999"},
                           exit_fn=lambda m, a, s, http=None: (asked.append(a),
                                                               {"outAmount": "19000000"})[1],
                           **common)
        assert asked == [2000000, 2000000, 2000000], asked

    def test_decimals_cost_one_rpc_per_mint_not_per_observation(self):
        c = self._cap()
        calls = []

        def dec(m, u, http=None):
            calls.append(m)
            return 6

        for i in range(3):
            cap.quote_once(c, http=lambda *a, **k: (200, {}, None), rpc_url="u", clip_usd=20.0,
                           now_iso="t", now_mono=float(i),
                           quote_fn=lambda m, usd, http=None: {"outAmount": "1000000"},
                           decimals_fn=dec)
        assert len(calls) == 1

    def test_missing_route_records_a_row_rather_than_vanishing(self):
        c = self._cap()
        row = cap.quote_once(c, http=lambda *a, **k: (200, {}, None), rpc_url="u", clip_usd=20.0,
                             now_iso="t", now_mono=1.0,
                             quote_fn=lambda m, usd, http=None: None,
                             decimals_fn=lambda m, u, http=None: 6)
        assert row["quality"] == "no_route"
        assert row["quote_price"] is None

    def test_yielding_writes_a_gap_never_a_retry(self):
        c = self._cap()
        y = cap.JupiterYield(clock=lambda: 0.0)
        y.observe(429)
        called = []
        row = cap.quote_once(c, http=lambda *a, **k: (200, {}, None), rpc_url="u", clip_usd=20.0,
                             now_iso="t", now_mono=1.0, jup_yield=y,
                             quote_fn=lambda *a, **k: called.append(1),
                             decimals_fn=lambda *a, **k: 6)
        assert row is None and called == []


class TestPumpCadence:
    def _harness(self):
        caps = {}
        c = _start(caps, "a", now=0.0)
        rows = []
        kw = dict(http=lambda *a, **k: (200, {}, None), rpc_url="u", clip_usd=20.0,
                  record_fn=rows.append,
                  quote_fn=lambda m, usd, http=None: {"outAmount": "1000000"},
                  decimals_fn=lambda m, u, http=None: 6)
        return caps, c, rows, kw

    def test_observes_on_cadence_not_every_pass(self):
        caps, c, rows, kw = self._harness()
        for t in (0.0, 1.0, 2.0, 5.0, 6.0, 10.0):
            cap.pump(caps, now_mono=t, now_iso="t", **kw)
        assert len(rows) == 3                              # t=0, 5, 10

    def test_cadence_does_not_drift_with_pass_time(self):
        """MEASURED LIVE 2026-08-26: a nominal 5s cadence ran at 6.2s, because `next_due` anchored
        to `now` and absorbed each pass's HTTP time plus the loop's poll granularity. At 6.2s only
        131 observations fit an 810s window against a fixed `expected_obs` of 162, so the reaper
        would stamp `truncated_worker` on every healthy capture."""
        caps, c, rows, kw = self._harness()
        t = 0.0
        for _ in range(40):                                # passes land late and irregularly
            cap.pump(caps, now_mono=t, now_iso="t", **kw)
            t += 1.3
        assert c.next_due == pytest.approx(round(c.next_due / 5.0) * 5.0)   # still on the grid
        assert len(rows) == pytest.approx(len(rows), abs=0)
        assert 9 <= len(rows) <= 11                        # ~52s of passes / 5s, no accumulation

    def test_a_long_stall_snaps_forward_and_never_bursts(self):
        caps, c, rows, kw = self._harness()
        cap.pump(caps, now_mono=0.0, now_iso="t", **kw)
        cap.pump(caps, now_mono=300.0, now_iso="t", **kw)   # worker was stalled for 5 minutes
        assert len(rows) == 2                               # one observation, not 60 catch-ups
        assert c.next_due == pytest.approx(305.0)

    def test_age_is_measured_from_the_refusal_not_the_capture_start(self):
        caps = {}
        c = cap.start(caps, mint="m", origin_event_id="o", gate="chase_first_seen",
                      refused_at=REFUSED_AT, horizon_s=810.0, cadence_s=5.0,
                      max_concurrent=2, now=0.0, now_epoch=FRESH_EPOCH)
        row = cap.quote_once(c, http=lambda *a, **k: (200, {}, None), rpc_url="u", clip_usd=20.0,
                             now_iso="2026-08-26T00:00:07+00:00", now_mono=0.0,
                             quote_fn=lambda m, usd, http=None: {"outAmount": "1000000"},
                             decimals_fn=lambda m, u, http=None: 6)
        assert row["age_s"] == pytest.approx(7.0)          # the 7s enqueue delay is visible

    def test_stops_at_the_horizon_rail(self):
        caps, c, rows, kw = self._harness()
        cap.pump(caps, now_mono=810.0, now_iso="t", **kw)
        assert rows == [] and c.done is True


class TestReaperRunsOnItsOwnClock:
    """Every other outcome is written by the capture loop, so none of them can report the loop
    being dead. The reaper must fire on STALENESS, never on a tally the worker had to be alive
    to increment."""

    def _counts(self, n_obs, terminal=None, age_min=30, reach_s=None, gap_s=5.2, cadence_s=5.0):
        at = (dt.datetime(2026, 8, 26, 12, 0, tzinfo=dt.timezone.utc)
              - dt.timedelta(minutes=age_min)).isoformat()
        reach = 809.0 if reach_s is None else reach_s
        # Mirrors observation_counts: the series carries the cadence it was TAKEN at, because the
        # table holds captures at more than one cadence and one global threshold cannot judge both.
        return {"c1": ("MINT", at, 810.0, 162, n_obs, terminal, reach, cadence_s, gap_s)}

    def _reap(self, counts):
        got = []
        n = cap.reap(counts, now_iso="2026-08-26T12:00:00+00:00", horizon_s=810.0, cadence_s=5.0,
                     stamp_fn=lambda cid, outcome, **kw: got.append((cid, outcome)))
        return n, got

    def test_a_slow_series_is_judged_at_its_own_cadence_not_the_callers(self):
        """A 25s capture with 25s spacing is healthy. Judged against the caller's 5s default it
        would be stamped `gapped` on every row -- the same defect as a censor pinned to one nominal
        interval, which cost a full day elsewhere in this codebase on 2026-08-26."""
        n, got = self._reap(self._counts(32, reach_s=800.0, gap_s=26.0, cadence_s=25.0))
        assert got == [("c1", "complete")], got

    def test_a_real_hole_in_a_slow_series_is_still_gapped(self):
        """The looser threshold must not go blind: 25s cadence tolerates 25s spacing, not 400s."""
        n, got = self._reap(self._counts(20, reach_s=800.0, gap_s=400.0, cadence_s=25.0))
        assert got == [("c1", "gapped")], got

    def test_a_fast_series_keeps_the_tight_threshold(self):
        """Loosening for slow captures must not loosen fast ones: 26s is a hole at 5s cadence."""
        n, got = self._reap(self._counts(150, reach_s=808.0, gap_s=26.0, cadence_s=5.0))
        assert got == [("c1", "gapped")], got

    def test_a_worker_that_stopped_is_truncated(self):
        n, got = self._reap(self._counts(97, reach_s=480.0))     # died at 8 minutes
        assert n == 1 and got == [("c1", "truncated_worker")]

    def test_full_series_is_complete(self):
        n, got = self._reap(self._counts(162, reach_s=809.0))
        assert got == [("c1", "complete")]

    def test_scheduler_jitter_does_not_false_stamp_a_healthy_capture(self):
        """MEASURED 2026-08-26: a healthy capture produced 159 of 162 observations because a 1s
        poll granularity against a 5s cadence loses a slot now and then. Stamping that
        `truncated_worker` would destroy the only signal that finds a worker which really died."""
        n, got = self._reap(self._counts(159, reach_s=808.4))
        assert got == [("c1", "complete")]

    def test_a_capture_with_no_observations_at_all_is_truncated(self):
        n, got = self._reap(self._counts(0, reach_s=0.0, gap_s=None))
        assert got == [("c1", "truncated_worker")]

    def test_a_hole_in_the_middle_is_gapped_not_complete(self):
        """`complete` must mean OBSERVED THROUGHOUT, not "reached the end". This module's own 429
        yield produces the difference: a capture that yields 120s mid-series then runs to the
        horizon reaches the end with a 120s hole, and under provider pressure that is the NORMAL
        shape -- so a two-outcome reaper would stamp holed series complete exactly when they are
        least trustworthy. Found by Beta in review, 2026-08-26."""
        n, got = self._reap(self._counts(140, reach_s=809.0, gap_s=120.0))
        assert got == [("c1", "gapped")]

    def test_jitter_sized_gaps_are_still_complete(self):
        n, got = self._reap(self._counts(159, reach_s=808.4, gap_s=11.0))
        assert got == [("c1", "complete")]

    def test_a_short_window_cannot_be_satisfied_by_one_early_row(self):
        """`reached >= window - cadence` alone passes a single row when the window is no longer
        than two cadences. Not reachable at the shipped 810s horizon, but horizon_s is per-capture
        and MEMECOIN_REAL_MAX_HOLD_S has moved 600 -> 2592000 -> 600 inside one day."""
        at = (dt.datetime(2026, 8, 26, 12, 0, tzinfo=dt.timezone.utc)
              - dt.timedelta(minutes=30)).isoformat()
        got = []
        cap.reap({"c1": ("MINT", at, 8.0, 1, 1, None, 8.0, 5.0, 8.0)},
                 now_iso="2026-08-26T12:00:00+00:00", horizon_s=8.0, cadence_s=5.0,
                 stamp_fn=lambda cid, outcome, **kw: got.append((cid, outcome)))
        assert got == [("c1", "truncated_worker")]

    def test_the_gap_is_recorded_not_only_judged(self):
        got = []
        cap.reap(self._counts(140, reach_s=809.0, gap_s=120.0),
                 now_iso="2026-08-26T12:00:00+00:00", horizon_s=810.0, cadence_s=5.0,
                 stamp_fn=lambda cid, outcome, **kw: got.append(kw))
        assert got[0]["max_gap_s"] == 120.0 and got[0]["n_obs"] == 140

    def test_a_capture_still_inside_its_window_is_left_alone(self):
        n, got = self._reap(self._counts(20, age_min=5))
        assert n == 0 and got == []

    def test_an_already_terminal_capture_is_never_restamped(self):
        n, got = self._reap(self._counts(0, terminal="not_captured_capacity"))
        assert n == 0 and got == []


class TestStorage:
    @pytest.mark.parametrize("floor,want_floor", [
        ("180000000000000000000001", "180000000000000000000001"),
        (None, None),
        ("invalid", None),
        (18.5, None),
        (-1, None),
    ])
    def test_exact_quote_quantities_round_trip_as_text(self, tmp_path, floor, want_floor):
        from sniper.persistence import db_memecoin_captures as store
        conn = store.connect(tmp_path / "exact.db")
        capture = _start({}, "MINT")
        common = dict(http=lambda *a, **kw: (200, {}, None), rpc_url="u", clip_usd=20.0,
                      decimals_fn=lambda *a, **kw: 9)
        acquired = "1000000000000000000000001"
        entry = cap.quote_once(
            capture, now_iso="2026-08-26T00:00:01+00:00", now_mono=1,
            quote_fn=lambda *a, **kw: {"outAmount": acquired}, **common)
        sale = cap.quote_once(
            capture, now_iso="2026-08-26T00:00:06+00:00", now_mono=6,
            exit_fn=lambda *a, **kw: {"outAmount": "190000000000000000000001",
                                     "otherAmountThreshold": floor}, **common)
        missing = cap.quote_once(
            capture, now_iso="2026-08-26T00:00:11+00:00", now_mono=11,
            exit_fn=lambda *a, **kw: None, **common)
        for row in (entry, sale, missing):
            assert row["token_qty_atomic"] == acquired
            assert row["token_decimals"] == 9
            stored = dict(row)
            stored["token_qty_atomic"] = int(acquired)
            store.record_observation(conn, stored)
        got = conn.execute(
            "SELECT token_qty_atomic, token_decimals, quote_output_atomic, "
            "quote_min_output_atomic, typeof(token_qty_atomic), typeof(quote_output_atomic) "
            "FROM memecoin_refusal_captures ORDER BY id").fetchall()
        assert tuple(got[0]) == (acquired, 9, acquired, None, "text", "text")
        assert tuple(got[1]) == (acquired, 9, "190000000000000000000001", want_floor, "text", "text")
        assert tuple(got[2]) == (acquired, 9, None, None, "text", "null")
        assert [entry["quality"], sale["quality"], missing["quality"]] == ["ok", "ok", "no_route"]
        conn.close()

    def test_quantity_migration_is_additive_and_repeatable(self, tmp_path):
        from sniper.persistence import db_memecoin_captures as store
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE memecoin_refusal_captures (
            id INTEGER PRIMARY KEY, capture_id TEXT, origin_event_id TEXT, network_id TEXT,
            mint TEXT, gate TEXT, refused_at TEXT, observed_at TEXT, age_s REAL,
            horizon_s REAL, quote_price REAL, quote_input_usd REAL, quote_route_target TEXT,
            quote_id TEXT, provider TEXT, endpoint_index INTEGER, latency_ms REAL,
            http_status INTEGER, quality TEXT, expected_obs INTEGER, outcome TEXT)""")
        conn.execute("INSERT INTO memecoin_refusal_captures (capture_id, quote_price) VALUES ('old', 1.25)")
        conn.commit()
        conn.close()
        for _ in range(2):
            conn = store.connect(path)
            columns = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(memecoin_refusal_captures)")}
            assert columns.get("token_qty_atomic") == "TEXT"
            assert columns.get("token_decimals") == "INTEGER"
            assert columns.get("quote_output_atomic") == "TEXT"
            assert columns.get("quote_min_output_atomic") == "TEXT"
            row = conn.execute(
                "SELECT quote_price, token_qty_atomic, token_decimals, quote_output_atomic, "
                "quote_min_output_atomic FROM memecoin_refusal_captures WHERE capture_id='old'").fetchone()
            assert tuple(row) == (1.25, None, None, None, None)
            conn.close()

    def test_observations_are_append_only_and_countable(self, tmp_path):
        from sniper.persistence import db_memecoin_captures as store
        conn = store.connect(tmp_path / "c.db")
        for i in range(3):
            store.record_observation(conn, {
                "capture_id": "c1", "network_id": "solana", "mint": "M", "gate": "chase_first_seen",
                "refused_at": "2026-08-26T00:00:00+00:00", "observed_at": "2026-08-26T00:00:%02d"
                % (i * 5), "age_s": i * 5.0, "horizon_s": 810.0, "expected_obs": 162,
                "quote_price": 1.0 + i, "quality": "ok"})
        store.terminal_row(conn, "c2", "not_captured_capacity", mint="N",
                           refused_at="2026-08-26T00:00:00+00:00", expected_obs=162)
        counts = store.observation_counts(conn, before_iso="2026-08-27")
        assert counts["c1"][4] == 3 and counts["c1"][5] is None
        assert counts["c2"][4] == 0 and counts["c2"][5] == "not_captured_capacity"


# ---------------------------------------------------------------- EVM chains (2026-09-13)
# Robinhood (4663) and BSC (56) refusals had no follow-up price path, so EVM refusals could not be
# graded afterwards. The EVM lane reads DexScreener display marks (no wallet, no custody RPC) and
# says so on every row; it has its own capacity so Solana captures do not shrink.
EPOCH = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc).timestamp()
REFUSED = "2026-09-01T00:00:00+00:00"


def _evm_kw(**over):
    kw = dict(gate="chase_first_seen", refused_at=REFUSED, horizon_s=810, cadence_s=25,
              max_concurrent=2, now=0, now_epoch=EPOCH + 1)
    kw.update(over)
    return kw


def test_evm_refusals_are_polled_with_their_chain():
    from scripts.memecoin_refusal_capture_loop import poll_refusals
    ledger = sqlite3.connect(":memory:")
    ledger.execute("""CREATE TABLE memecoin_entry_attempts (
        attempt_id TEXT PRIMARY KEY, mint TEXT, origin_event_id TEXT, terminal_reason TEXT,
        decision_started_at TEXT, network_id TEXT, terminal_status TEXT, completed_at TEXT)""")
    reason = "coin ran 4.2x above first-seen price (cap 3.0x)"
    for i, net in enumerate(("solana", "4663", "56", "8453")):
        ledger.execute("INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?,?,?,?,?)",
                       (net, "mint-" + net, "origin-" + net, reason,
                        "2026-09-01T00:00:0%d+00:00" % (i + 1), net, "refused", None))
    rows, _ = poll_refusals(ledger, since_iso=REFUSED, networks=("solana", "4663", "56"))
    assert [(r["origin_event_id"], r["network_id"]) for r in rows] == [
        ("origin-solana", "solana"), ("origin-4663", "4663"), ("origin-56", "56")]
    rows, _ = poll_refusals(ledger, since_iso=REFUSED)
    assert [r["network_id"] for r in rows] == ["solana"]        # the default stays Solana-only


def test_evm_captures_have_their_own_capacity():
    caps, refused = {}, []
    on = lambda *a: refused.append(a)
    for i in range(2):
        assert cap.start(caps, mint="S%d" % i, origin_event_id="s%d" % i, network_id="solana",
                         on_refuse=on, **_evm_kw())
    # Solana is full. EVM starts against its own budget, and fills it.
    for i, net in enumerate(("4663", "56")):
        assert cap.start(caps, mint="0xe%d" % i, origin_event_id="e%d" % i, network_id=net,
                         on_refuse=on, **_evm_kw())
    assert cap.start(caps, mint="0xe9", origin_event_id="e9", network_id="4663",
                     on_refuse=on, **_evm_kw()) is None
    assert refused[-1][1] == "not_captured_capacity"
    # Two live EVM captures never count against Solana's budget.
    assert cap.start(caps, mint="S9", origin_event_id="s9", network_id="solana",
                     on_refuse=on, **_evm_kw(max_concurrent=3)) is not None


def test_a_low_priority_family_never_takes_the_last_free_slot():
    """Gamma LOW on 91b9a3c7a (2026-09-30): the new, mostly low-signal families would have displaced
    75 Solana captures of the graded families in 16.9 days. With 5 of 6 slots busy, a new-family
    refusal is refused and counted; an old-family refusal still gets the slot."""
    caps, refused = {}, []
    on = lambda *a: refused.append(a)
    kw = _evm_kw(max_concurrent=6)
    for i in range(5):
        assert cap.start(caps, mint="S%d" % i, origin_event_id="s%d" % i, network_id="solana",
                         on_refuse=on, **kw)
    for gate in sorted(cap.LOW_PRIORITY_FAMILIES):
        # A size, so a sized family (slippage_fail) reaches the reserve check, not the size one.
        assert cap.start(caps, mint="L" + gate, origin_event_id="l" + gate, network_id="solana",
                         on_refuse=on, **{**kw, "gate": gate, "quote_input_usd": 100.0}) is None
        assert refused[-1][1] == "not_captured_capacity_reserve" and refused[-1][3] == gate
    assert cap.start(caps, mint="R", origin_event_id="r", network_id="solana", on_refuse=on,
                     **{**kw, "gate": "chase_past_entry"}) is not None


def test_a_low_priority_family_starts_while_two_slots_are_free():
    caps, refused = {}, []
    kw = _evm_kw(max_concurrent=6, gate="coin_too_young")
    for i in range(4):
        assert cap.start(caps, mint="S%d" % i, origin_event_id="s%d" % i, network_id="solana",
                         **{**kw, "gate": "chase_first_seen"})
    assert cap.start(caps, mint="Y", origin_event_id="y", network_id="solana",
                     on_refuse=lambda *a: refused.append(a), **kw) is not None
    assert refused == []


def test_a_loop_full_of_floor_and_cooldown_refusals_still_captures_an_impact_cap_refusal():
    """Gamma INSTRUMENT GAP (2026-10-02): source_buy_floor, source_buy_abs_floor and reentry_cooldown
    filled all 6 Solana slots, so impact_cap went uncaptured after 10-01 18:11Z. Replay-shaped: a
    stream of those refusals, then one impact_cap, which must start."""
    caps, refused = {}, []
    on = lambda *a: refused.append(a)
    kw = _evm_kw(max_concurrent=6)
    families = ("source_buy_floor", "source_buy_abs_floor", "reentry_cooldown")
    for i in range(30):
        cap.start(caps, mint="F%d" % i, origin_event_id="f%d" % i, network_id="solana",
                  on_refuse=on, **{**kw, "gate": families[i % 3]})
    live = [c for c in caps.values() if not c.done]
    assert len(live) == 5 and {r[1] for r in refused} == {"not_captured_capacity_reserve"}
    assert cap.start(caps, mint="IMPACT", origin_event_id="impact", network_id="solana",
                     on_refuse=on, **{**kw, "gate": "impact_cap", "quote_input_usd": 50.0}) is not None


def test_every_added_family_is_low_priority_and_every_low_priority_family_is_named():
    named = {g for g, _ in cap.GATES}
    assert cap.LOW_PRIORITY_FAMILIES <= named
    assert {"roundtrip_cap", "chase_quote", "impact_cap", "chase_past_entry"}.isdisjoint(
        cap.LOW_PRIORITY_FAMILIES)


def test_evm_observations_are_display_marks_from_one_batched_read():
    caps = {}
    cap.start(caps, mint="0xaa", origin_event_id="a", network_id="4663", **_evm_kw(max_concurrent=4))
    cap.start(caps, mint="0xbb", origin_event_id="b", network_id="56", **_evm_kw(max_concurrent=4))
    cap.start(caps, mint="SOLMINT", origin_event_id="s", network_id="solana", **_evm_kw(max_concurrent=4))
    calls, rows = [], []

    def display_fn(keys):
        calls.append(sorted(keys))
        return 200, {("4663", "0xaa"): 0.0012, ("56", "0xbb"): None}

    cap.pump(caps, now_mono=0, now_iso="2026-09-01T00:00:03+00:00", now_epoch=EPOCH + 3,
             http=lambda *_a, **_k: (200, {}, None), rpc_url="rpc", clip_usd=20,
             record_fn=rows.append, quote_fn=lambda mint, usd, **_: {"outAmount": "1000000"},
             decimals_fn=lambda *_a, **_k: 6, display_fn=display_fn)
    assert calls == [[("4663", "0xaa"), ("56", "0xbb")]]          # ONE read for every due EVM capture
    by = {r["capture_id"]: r for r in rows}
    evm = by["4663:0xaa:a"]
    assert (evm["network_id"], evm["quote_price_basis"], evm["provider"]) == ("4663", "display", "dexscreener")
    assert (evm["quote_price"], evm["quality"], evm["quote_side"]) == (0.0012, "ok", None)
    assert (by["56:0xbb:b"]["quote_price"], by["56:0xbb:b"]["quality"]) == (None, "no_route")
    sol = by["solana:SOLMINT:s"]
    assert (sol["quote_price_basis"], sol["provider"]) == ("exact_route", "jupiter")


def test_a_display_429_yields_the_evm_lane_only():
    caps, rows = {}, []
    cap.start(caps, mint="0xaa", origin_event_id="a", network_id="4663", **_evm_kw())
    hold = cap.JupiterYield(hold_s=60, clock=lambda: 0.0, name="DexScreener")
    cap.pump(caps, now_mono=0, now_iso="2026-09-01T00:00:03+00:00", now_epoch=EPOCH + 3,
             http=None, rpc_url="rpc", clip_usd=20, record_fn=rows.append,
             display_fn=lambda keys: (429, {}), display_yield=hold)
    # A 429 answers nothing about the coin: unknown (provider_error), never no_route.
    assert hold.blocked() and rows[0]["http_status"] == 429 and rows[0]["quality"] == "provider_error"


def test_evm_rows_keep_their_chain_and_basis_in_storage(tmp_path):
    from sniper.persistence import db_memecoin_captures as store
    conn = store.connect(tmp_path / "c.db")
    store.record_observation(conn, {
        "capture_id": "4663:0xaa:o", "origin_event_id": "o", "network_id": "4663", "mint": "0xaa",
        "refused_at": REFUSED, "observed_at": "2026-09-01T00:00:03+00:00", "age_s": 3.0,
        "quote_price": 0.5, "quote_price_basis": "display", "quality": "ok"})
    store.terminal_row(conn, "4663:0xaa:o", "not_captured_capacity", mint="0xaa", refused_at=REFUSED)
    got = [tuple(r) for r in conn.execute(
        "SELECT network_id, quote_price_basis, outcome FROM memecoin_refusal_captures ORDER BY id")]
    assert got == [("4663", "display", None), ("4663", None, "not_captured_capacity")]
    # The exact-route reader never returns a display mark. A legacy row (no basis) is exact route.
    assert store.quote_prices_for_origin(conn, "o") == (None, None)
    for age, price, basis in ((2.0, 2.0, "exact_route"), (5.0, 3.0, None)):
        store.record_observation(conn, {
            "capture_id": "solana:M:s", "origin_event_id": "s", "network_id": "solana", "mint": "M",
            "refused_at": REFUSED, "observed_at": "2026-09-01T00:00:05+00:00", "age_s": age,
            "quote_price": price, "quote_price_basis": basis, "quality": "ok"})
    assert store.quote_prices_for_origin(conn, "s") == (2.0, 3.0)


def test_handoff_reads_the_capture_chain():
    from scripts.memecoin_refusal_capture_loop import handed_off
    ledger = sqlite3.connect(":memory:")
    ledger.execute("CREATE TABLE memecoin_positions (id INTEGER, mint TEXT, network_id TEXT, "
                   "execution_mode TEXT, opened_at TEXT)")
    ledger.execute("INSERT INTO memecoin_positions VALUES (7,'0xaa','4663','real','2026-09-01T00:05:00+00:00')")
    robin = cap.start({}, mint="0xaa", origin_event_id="o", network_id="4663", **_evm_kw())
    bsc = cap.start({}, mint="0xaa", origin_event_id="o", network_id="56", **_evm_kw())
    assert handed_off(ledger, robin) == 7
    assert handed_off(ledger, bsc) is None


def test_an_impact_cap_refusal_is_captured():
    """kaRAT (dc82c96a, 2026-09-13): refused at 90% impact 40 s before its curve migrated."""
    from sniper.services import memecoin_refusal_capture as cap
    assert cap.classify_gate("price impact 90.22% over 12.0% cap") == "impact_cap"
    assert cap.should_capture("impact_cap")
    assert cap.classify_gate("price impact read failed") is None


def test_the_display_lane_prices_an_arc_token_from_its_dexscreener_pair():
    """5042 refusals had no capture rows: the EVM lane covered 4663 and 56 only. DexScreener lists
    Arc as chainId 'arc' (verified 2026-09-17 on 8 refused mints)."""
    mint = "0x05363533aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    pair = {"chainId": "arc", "pairAddress": "0xpair", "priceUsd": "0.000002651",
            "baseToken": {"address": mint}, "liquidity": {"usd": 5000}}
    fn = cap.dexscreener_display_fn(http=lambda method, url: (200, {"pairs": [pair]}, None))
    status, prices = fn([("5042", mint)])
    assert (status, prices) == (200, {("5042", mint): 0.000002651})


def test_the_evm_capture_lane_defaults_to_robinhood_bsc_and_arc():
    import os, subprocess, sys
    env = {k: v for k, v in os.environ.items() if k != "MEMECOIN_CAPTURE_EVM_NETWORKS"}
    out = subprocess.run(
        [sys.executable, "-c", "import sniper.settings.memecoin as c; print(','.join(c.MEMECOIN_CAPTURE_EVM_NETWORKS))"],
        capture_output=True, text=True, env=env, check=True).stdout.strip()
    assert out == "4663,56,5042"


# --- DexScreener 30-pair truncation (2026-09-17) ---------------------------------------------------
# /latest/dex/tokens returns at most 30 PAIRS per response, even for one token (WETH on Base, wSOL:
# 30 each). 8 Arc mints in one batch returned 30 pairs and 3 of them came back unpriced; each priced
# alone. A token absent from a capped response is UNKNOWN, never no_route.

def _pair(mint, chain="arc", price="0.5", liq=1000):
    return {"chainId": chain, "pairAddress": "0x" + mint[-4:] + str(liq), "priceUsd": price,
            "baseToken": {"address": mint}, "liquidity": {"usd": liq}}


def _ds(responses):
    calls = []

    def http(method, url):
        calls.append(url.split("/tokens/", 1)[1].split("?", 1)[0].split(","))
        return responses[len(calls) - 1]
    return http, calls


A, B, C = ("0x" + c * 40 for c in "abc")


def test_a_capped_batch_re_asks_only_the_unpriced_tokens_in_one_more_read():
    heavy = [_pair(A, liq=i + 1) for i in range(30)]
    http, calls = _ds([(200, {"pairs": heavy}, None), (200, {"pairs": [_pair(B, price="2.5")]}, None)])
    status, prices = cap.dexscreener_display_fn(http=http)([("5042", A), ("5042", B), ("5042", C)])
    assert len(calls) == 2 and sorted(calls[1]) == sorted([B, C])
    assert status == 200
    assert prices == {("5042", A): 0.5, ("5042", B): 2.5, ("5042", C): None}


def test_a_token_still_absent_from_a_capped_re_ask_is_unknown_not_no_route():
    heavy = [_pair(A, liq=i + 1) for i in range(30)]
    heavy_b = [_pair(B, liq=i + 1) for i in range(30)]
    http, calls = _ds([(200, {"pairs": heavy}, None), (200, {"pairs": heavy_b}, None)])
    _status, prices = cap.dexscreener_display_fn(http=http)([("5042", A), ("5042", B), ("5042", C)])
    assert len(calls) == 2                                  # never a third read in one pass
    assert prices == {("5042", A): 0.5, ("5042", B): 0.5}   # C unknown: absent, not None


def test_an_uncapped_batch_is_read_once_and_a_missing_token_is_no_route():
    http, calls = _ds([(200, {"pairs": [_pair(A)]}, None)])
    _status, prices = cap.dexscreener_display_fn(http=http)([("5042", A), ("5042", B)])
    assert len(calls) == 1 and prices == {("5042", A): 0.5, ("5042", B): None}


def test_a_429_on_the_first_read_is_not_re_asked_and_prices_nothing():
    http, calls = _ds([(429, {}, None)])
    status, prices = cap.dexscreener_display_fn(http=http)([("5042", A), ("5042", B)])
    assert (len(calls), status, prices) == (1, 429, {})


def test_a_429_on_the_re_ask_keeps_the_first_prices_and_stops_the_lane():
    heavy = [_pair(A, liq=i + 1) for i in range(30)]
    http, calls = _ds([(200, {"pairs": heavy}, None), (429, {}, None)])
    caps, rows = {}, []
    for mint, origin in ((A, "a"), (B, "b")):
        cap.start(caps, mint=mint, origin_event_id=origin, network_id="5042", **_evm_kw(max_concurrent=4))
    hold = cap.JupiterYield(hold_s=120, clock=lambda: 0.0, name="DexScreener")
    cap.pump(caps, now_mono=0, now_iso="2026-09-01T00:00:03+00:00", now_epoch=EPOCH + 3,
             http=None, rpc_url="rpc", clip_usd=20, record_fn=rows.append,
             display_fn=cap.dexscreener_display_fn(http=http), display_yield=hold)
    by = {r["mint"]: r for r in rows}
    assert hold.blocked()
    assert (by[A]["quality"], by[A]["quote_price"]) == ("ok", 0.5)
    assert (by[B]["quality"], by[B]["quote_price"]) == ("provider_error", None)


def test_a_failed_re_ask_keeps_the_first_reads_prices():
    heavy = [_pair(A, liq=i + 1) for i in range(30)]
    calls = []

    def http(method, url):
        calls.append(url)
        if len(calls) == 2:
            raise TimeoutError("re-ask timed out")
        return 200, {"pairs": heavy}, None
    status, prices = cap.dexscreener_display_fn(http=http)([("5042", A), ("5042", B)])
    assert (len(calls), status, prices) == (2, 200, {("5042", A): 0.5})
