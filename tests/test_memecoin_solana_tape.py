"""Solana vault-keyed tape + depth (v2 prereg 2026-07-24).

Fixtures encode LIVE-verified mainnet cases, not the author's assumptions (fixtures-encode-the-bug
lesson): the Jimothy LP-remove (same-sign deltas, RemoveLiquidityByRange2 logs, coin −1100074209 /
quote −121997149), 13/13 opposite-sign = Buy/Sell swaps, and the our-own-wallet-as-pool-candidate
resolution failure found on our own MELON entry fill.
"""
import sqlite3
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest

from sniper.analysis.exit_features import profile_capability
from sniper.services import memecoin_solana_amounts as sa
from sniper.services import memecoin_solana_tape as st
from sniper.services.memecoin_solana_blocktime import build_read_rpc_call


def test_shared_read_rpc_call_spaces_mixed_methods_and_retries_one_429():
    now = [0.0]
    sleeps = []
    calls = []
    outcomes = [urllib.error.HTTPError("u", 429, "slow", {}, None), "sig-ok", "tx-ok"]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    def raw(method, params):
        calls.append((method, now[0]))
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    call = build_read_rpc_call(min_call_s=1.0, max_429_retries=1, retry_backoff_s=3.0,
                               clock=lambda: now[0], sleep=sleep, raw_call=raw)
    assert call("getSignaturesForAddress", []) == "sig-ok"
    assert call("getTransaction", []) == "tx-ok"
    assert [m for m, _ts in calls] == [
        "getSignaturesForAddress", "getSignaturesForAddress", "getTransaction"]
    assert calls[1][1] - calls[0][1] >= 3.0
    assert calls[2][1] - calls[1][1] >= 1.0


def test_shared_read_rpc_call_second_429_raises():
    def raw(_method, _params):
        raise urllib.error.HTTPError("u", 429, "slow", {}, None)

    call = build_read_rpc_call(min_call_s=0.1, max_429_retries=1, retry_backoff_s=0.1,
                               clock=lambda: 0.0, sleep=lambda _s: None, raw_call=raw)
    with pytest.raises(urllib.error.HTTPError):
        call("getTransaction", [])


def test_shared_read_rpc_call_spaces_starts_without_serializing_inflight_requests():
    entered = 0
    entered_lock = threading.Lock()
    both_entered = threading.Event()
    release = threading.Event()

    def raw(_method, _params):
        nonlocal entered
        with entered_lock:
            entered += 1
            if entered == 2:
                both_entered.set()
        assert release.wait(1.0)
        return "ok"

    call = build_read_rpc_call(min_call_s=0.01, raw_call=raw)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(call, "getTransaction", [str(i)]) for i in range(2)]
        assert both_entered.wait(0.5)
        release.set()
        assert [future.result() for future in futures] == ["ok", "ok"]


@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan")])
def test_shared_read_rpc_call_rejects_non_finite_or_non_positive_cadence(bad):
    with pytest.raises(ValueError):
        build_read_rpc_call(min_call_s=bad, raw_call=lambda _m, _p: None)

COIN = "CoinMint111111111111111111111111111111111111"
POOL_OWNER = "PoolAuth111111111111111111111111111111111111"
COIN_VAULT = "CoinVault11111111111111111111111111111111111"
WSOL_VAULT = "WsolVault11111111111111111111111111111111111"
OUR = "3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH"


def _tx(entries_pre, entries_post, payer="Payer111111111111111111111111111111111111111"):
    """entries = [(mint, owner, address, amount)]; address order defines accountKeys."""
    addrs, seen = [payer], {payer}
    for (_m, _o, a, _amt) in entries_pre + entries_post:
        if a not in seen:
            addrs.append(a); seen.add(a)
    def bals(entries):
        return [{"mint": m, "owner": o, "accountIndex": addrs.index(a),
                 "uiTokenAmount": {"amount": str(amt)}} for (m, o, a, amt) in entries]
    return {"transaction": {"message": {"accountKeys": [{"pubkey": a} for a in addrs]}},
            "meta": {"preTokenBalances": bals(entries_pre), "postTokenBalances": bals(entries_post)}}


def test_resolve_pool_excludes_our_own_wallet():
    """Live failure 2026-07-24: on OUR OWN entry fill our wallet holds both coin and quote, so the
    mint-keyed resolver saw two candidates and failed closed on every one of our fills."""
    tx = _tx([(COIN, POOL_OWNER, COIN_VAULT, 1000), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 500),
              (COIN, OUR, "OurCoinAta1111111111111111111111111111111111", 0),
              (sa.WSOL, OUR, "OurWsolAta1111111111111111111111111111111111", 90)],
             [(COIN, POOL_OWNER, COIN_VAULT, 900), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 560),
              (COIN, OUR, "OurCoinAta1111111111111111111111111111111111", 100),
              (sa.WSOL, OUR, "OurWsolAta1111111111111111111111111111111111", 30)], payer=OUR)
    assert sa.resolve_pool(tx, COIN) is None                       # ambiguous without exclusion
    got = sa.resolve_pool(tx, COIN, exclude_owner=OUR)
    assert got == (POOL_OWNER, COIN_VAULT, sa.WSOL)
    accounts = sa.resolve_pool_accounts(tx, COIN, exclude_owner=OUR)
    assert accounts == (POOL_OWNER, COIN_VAULT, sa.WSOL, WSOL_VAULT)


def test_paper_pool_resolution_uses_token_facing_modeled_route_before_mint_scan():
    import sqlite3
    from sniper.services import memecoin_solana_tape as st
    from sniper.analysis.tick_features import ensure_feature_tables

    tape = _cursor_db()
    ensure_feature_tables(tape)
    ledger = sqlite3.connect(":memory:")
    ledger.execute("CREATE TABLE memecoin_fills(position_id INTEGER,kind TEXT,tx_sig TEXT,id INTEGER)")
    other_owner = "OtherPool11111111111111111111111111111111111"
    tx = _tx(
        [(COIN, POOL_OWNER, COIN_VAULT, 1000), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 500),
         (COIN, other_owner, "OtherCoinVault1111111111111111111111111111111", 2000),
         (sa.WSOL, other_owner, "OtherWsolVault1111111111111111111111111111111", 800)],
        [(COIN, POOL_OWNER, COIN_VAULT, 900), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 560),
         (COIN, other_owner, "OtherCoinVault1111111111111111111111111111111", 1900),
         (sa.WSOL, other_owner, "OtherWsolVault1111111111111111111111111111111", 860)],
    )
    requested = []

    def get_sigs(address, params):
        requested.append((address, params["limit"]))
        return [{"signature": "route-sig"}]

    position = {
        "id": -1, "mint": COIN,
        "route_binding": {"route_plan": [{"swapInfo": {
            "ammKey": POOL_OWNER, "inputMint": sa.WSOL, "outputMint": COIN,
        }}]},
    }
    vault, spent = st.resolve_position_pool(
        tape, ledger, position, lambda _sig: tx,
        get_sigs_fn=get_sigs,
        call_fn=lambda *_args: pytest.fail("generic mint scan used despite exact modeled route"),
        return_fetch_count=True, max_fetch=4,
    )

    assert (vault, spent) == (COIN_VAULT, 1)
    assert requested == [(POOL_OWNER, 4)]


def test_vault_rows_swap_and_sync():
    tx = _tx([(COIN, POOL_OWNER, COIN_VAULT, 1000), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 500)],
             [(COIN, POOL_OWNER, COIN_VAULT, 900), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 560)])
    rows = sa.vault_rows(tx, COIN, COIN_VAULT, slot=77, signature="s1")
    kinds = {r["kind"]: r for r in rows}
    assert set(kinds) == {"sol_sync", "sol_swap"}
    assert kinds["sol_swap"]["amount0"] == "-100" and kinds["sol_swap"]["amount1"] == "60"
    assert kinds["sol_sync"]["amount0"] == "900" and kinds["sol_sync"]["amount1"] == "560"
    assert all(r["pool"] == COIN_VAULT and r["block_no"] == 77 for r in rows)


def test_vault_rows_lp_remove_is_sol_burn():
    """The live Jimothy LP pull: both deltas negative -> sol_burn (depth_pull signal)."""
    tx = _tx([(COIN, POOL_OWNER, COIN_VAULT, 2000074209), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 200000000)],
             [(COIN, POOL_OWNER, COIN_VAULT, 900000000), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 78002851)])
    kinds = {r["kind"]: r for r in sa.vault_rows(tx, COIN, COIN_VAULT, slot=5, signature="s2")}
    assert "sol_burn" in kinds
    assert int(kinds["sol_burn"]["amount0"]) < 0 and int(kinds["sol_burn"]["amount1"]) < 0


def test_vault_rows_one_sided_emits_sync_only():
    """Single-sided CLMM liquidity / fee ops must not fabricate directional flow."""
    tx = _tx([(COIN, POOL_OWNER, COIN_VAULT, 1000)], [(COIN, POOL_OWNER, COIN_VAULT, 1200)])
    rows = sa.vault_rows(tx, COIN, COIN_VAULT, slot=6, signature="s3")
    assert [r["kind"] for r in rows] == ["sol_sync"]
    assert rows[0]["amount1"] is None       # absent quote vault is unknown, never a real zero reserve


def test_profile_capability_sol_profile():
    cap = profile_capability("solana", "sol")
    assert not cap.forced_legacy and cap.depth_trusted
    assert cap.required == frozenset({"price", "flow", "vol", "depth"})
    assert profile_capability("solana", None).forced_legacy          # no tape -> still legacy
    assert profile_capability("solana", "mystery").forced_legacy


def _ticks():
    from sniper.persistence import db_ticks as t
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c)
    t.ensure_block_times(c)
    return c


def test_tape_pass_commits_only_the_contiguous_prefix_before_fetch_failure():
    """A provider failure preserves the frontier but need not discard an already-decoded prefix."""
    tc = _ticks()
    st.ensure_sol_cursor(tc)
    sigs = [{"signature": "new2", "slot": 11}, {"signature": "new1", "slot": 10}]
    ok_tx = _tx([(COIN, POOL_OWNER, COIN_VAULT, 100), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 50)],
                [(COIN, POOL_OWNER, COIN_VAULT, 90), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 56)])
    ok_tx["blockTime"] = 1_700_000_000
    n = st.tape_pass_for_vault(tc, COIN, COIN_VAULT, lambda a, p: sigs,
                               lambda s: ok_tx if s == "new1" else None,   # new2 fails
                               "2026-07-24T00:00:00+00:00", tx_budget=10)
    assert n == 1
    row = tc.execute("SELECT last_sig, frontier_before FROM sol_sig_cursor").fetchone()
    assert row["last_sig"] == "new1"                     # decoded contiguous prefix advances
    assert row["frontier_before"].startswith("decode:")  # known work remains engine-visible
    assert st.vault_tape_incomplete(tc, COIN_VAULT)
    assert tc.execute("SELECT COUNT(*) FROM pool_ticks").fetchone()[0] > 0

    n = st.tape_pass_for_vault(tc, COIN, COIN_VAULT, lambda a, p: sigs, lambda s: ok_tx,
                               "2026-07-24T00:00:00+00:00", tx_budget=10)
    assert n == 2
    assert tc.execute("SELECT last_sig FROM sol_sig_cursor WHERE vault=?",
                      (COIN_VAULT,)).fetchone()[0] == "new2"               # newest after oldest-first
    assert tc.execute("SELECT COUNT(*) FROM pool_ticks WHERE kind='sol_swap'").fetchone()[0] > 0
    assert tc.execute("SELECT block_ts FROM block_times WHERE chain_id='solana' AND block_no=10"
                      ).fetchone() is not None                             # slot->time came free


def test_sequential_fetch_stops_at_first_provider_failure():
    tc = _ticks()
    st.ensure_sol_cursor(tc)
    sigs = [{"signature": "new3", "slot": 12}, {"signature": "new2", "slot": 11},
            {"signature": "new1", "slot": 10}]
    ok_tx = _tx([(COIN, POOL_OWNER, COIN_VAULT, 100),
                 (sa.WSOL, POOL_OWNER, WSOL_VAULT, 50)],
                [(COIN, POOL_OWNER, COIN_VAULT, 90),
                 (sa.WSOL, POOL_OWNER, WSOL_VAULT, 56)])
    calls = []

    def fetch(signature):
        calls.append(signature)
        return ok_tx if signature == "new1" else None

    assert st.tape_pass_for_vault(
        tc, COIN, COIN_VAULT, lambda _a, _p: sigs, fetch,
        "2026-07-24T00:00:00+00:00", tx_budget=10, tx_fetch_workers=1) == 1
    assert calls == ["new1", "new2"]


def test_tape_pass_holds_cursor_when_the_first_transaction_fetch_fails():
    tc = _ticks()
    st.ensure_sol_cursor(tc)
    sigs = [{"signature": "new2", "slot": 11}, {"signature": "new1", "slot": 10}]
    n = st.tape_pass_for_vault(
        tc, COIN, COIN_VAULT, lambda _a, _p: sigs, lambda _s: None,
        "2026-07-24T00:00:00+00:00", tx_budget=10,
    )
    row = tc.execute("SELECT last_sig,frontier_before FROM sol_sig_cursor").fetchone()
    assert n == -1
    assert row["last_sig"] == ""
    assert row["frontier_before"].startswith("decode:")


def test_tape_pass_can_overlap_bounded_transaction_reads():
    tc = _ticks()
    st.ensure_sol_cursor(tc)
    sigs = [{"signature": "new2", "slot": 11}, {"signature": "new1", "slot": 10}]
    entered = 0
    lock = threading.Lock()
    both_entered = threading.Event()
    release = threading.Event()
    ok_tx = _tx([(COIN, POOL_OWNER, COIN_VAULT, 100),
                 (sa.WSOL, POOL_OWNER, WSOL_VAULT, 50)],
                [(COIN, POOL_OWNER, COIN_VAULT, 90),
                 (sa.WSOL, POOL_OWNER, WSOL_VAULT, 56)])
    ok_tx["blockTime"] = 1_700_000_000

    def get_tx(_signature):
        nonlocal entered
        with lock:
            entered += 1
            if entered == 2:
                both_entered.set()
        assert release.wait(1.0)
        return ok_tx

    def release_when_concurrent():
        assert both_entered.wait(0.5)
        release.set()

    helper = threading.Thread(target=release_when_concurrent)
    helper.start()
    result = st.tape_pass_for_vault(
        tc, COIN, COIN_VAULT, lambda _address, _params: sigs, get_tx,
        "2026-07-24T00:00:00+00:00", tx_budget=10, tx_fetch_workers=2,
    )
    helper.join()
    assert result == 2


def test_successful_empty_walk_advances_durable_check_watermark():
    tc = _ticks()
    st.ensure_sol_cursor(tc)
    tc.execute(
        "INSERT INTO sol_sig_cursor(vault,last_sig,updated_at,frontier_before,floor_ts) "
        "VALUES(?,?,?,?,?)", (COIN_VAULT, "head", "2026-07-26T11:59:00+00:00", None, None))

    result = st.tape_pass_for_vault(
        tc, COIN, COIN_VAULT, lambda _address, _params: [], lambda _sig: None,
        "2026-07-26T12:00:00+00:00", tx_budget=10)

    row = tc.execute(
        "SELECT last_sig,updated_at,frontier_before FROM sol_sig_cursor WHERE vault=?",
        (COIN_VAULT,)).fetchone()
    assert result == 0
    assert tuple(row) == ("head", "2026-07-26T12:00:00+00:00", None)


def test_complete_active_scope_can_contract_abandoned_older_floor():
    tc = _ticks()
    st.ensure_sol_cursor(tc)
    tc.execute(
        "INSERT INTO sol_sig_cursor(vault,last_sig,updated_at,frontier_before,floor_ts) "
        "VALUES(?,?,?,?,?)",
        (COIN_VAULT, "old-cursor", "2026-07-26T11:00:00+00:00", "enum:old", 100.0),
    )
    seen = []

    result = st.tape_pass_for_vault(
        tc, COIN, COIN_VAULT,
        lambda _address, params: seen.append(dict(params)) or [], lambda _sig: None,
        "2026-07-26T12:00:00+00:00", tx_budget=10, floor_ts=2000.0,
        allow_floor_contraction=True,
    )

    row = tc.execute(
        "SELECT last_sig,frontier_before,floor_ts FROM sol_sig_cursor WHERE vault=?",
        (COIN_VAULT,),
    ).fetchone()
    assert result == 0
    assert tuple(row) == ("", None, 2000.0)
    assert seen == [{"limit": 1000}]


def test_small_rolling_floor_advance_does_not_restart_incomplete_capture():
    tc = _ticks()
    st.ensure_sol_cursor(tc)
    tc.execute(
        "INSERT INTO sol_sig_cursor(vault,last_sig,updated_at,frontier_before,floor_ts) "
        "VALUES(?,?,?,?,?)",
        (COIN_VAULT, "cursor", "2026-07-26T11:00:00+00:00", "enum:frontier", 100.0),
    )

    assert st.tape_pass_for_vault(
        tc, COIN, COIN_VAULT, lambda _address, _params: None, lambda _sig: None,
        "2026-07-26T12:00:00+00:00", tx_budget=10, floor_ts=500.0,
        allow_floor_contraction=True,
    ) == -1

    row = tc.execute(
        "SELECT last_sig,frontier_before,floor_ts FROM sol_sig_cursor WHERE vault=?",
        (COIN_VAULT,),
    ).fetchone()
    assert tuple(row) == ("cursor", "enum:frontier", 100.0)


def test_position_capture_uses_only_the_current_required_feature_window(monkeypatch):
    target = {"id": "v6", "mint": "Mint", "network_id": "solana",
              "opened_at": "2026-07-26T10:00:00+00:00"}
    monkeypatch.setattr(st, "_bootstrap_history_s", lambda: 300.0)
    monkeypatch.setattr(
        st, "resolve_position_pool",
        lambda _tc, _mc, _pos, _get_tx, **_kw: ("Vault", 0),
    )
    floors = []
    monkeypatch.setattr(
        st, "tape_pass_for_vault",
        lambda _tc, _mint, _vault, _get_sigs, _get_tx, _now, **kwargs:
            floors.append(kwargs["floor_ts"]) or 0,
    )

    st.solana_tape_pass(
        object(), object(), object(), object(), "2026-07-26T12:00:00+00:00",
        tx_budget=10, positions_override=[target], allow_floor_contraction=True,
    )

    assert floors == [1785066900.0]


def test_compute_state_sol_profile_end_to_end():
    """Sol tape -> TRENDING, candidate-capable, and a REAL depth_pull_flag on sol_burn."""
    from sniper.services.memecoin_exit_market_state import compute_state
    swaps, times = [], {}
    for i in range(8):                        # 8 per-slot prints, fresh, complete slot->time
        swaps.append({"kind": "sol_swap", "block_no": 100 + i, "maker": None,
                      "tx_sig": f"swap-{i}",
                      "amount0": str(-1000 - 10 * i), "amount1": str(500 + 5 * i)})
        swaps.append({"kind": "sol_sync", "block_no": 100 + i, "maker": None,
                      "tx_sig": f"swap-{i}",
                      "amount0": "90000", "amount1": str(56000 + i)})
        times[100 + i] = f"2026-07-24T00:00:{30 + i:02d}+00:00"
    contract = {"chain_id": "solana", "mint": COIN, "pool": COIN_VAULT, "pool_kind": "sol",
                "quote_side": 1, "position_id": 9}
    now_ts = 1784900000.0
    import datetime as _dt
    now_ts = _dt.datetime.fromisoformat("2026-07-24T00:00:40+00:00").timestamp()
    s = compute_state(swaps, times, contract, now_ts=now_ts)
    assert s.state.value == "trending" and s.candidate_capable()
    assert s.depth_pull_flag is False and s.reserve_quote is not None
    assert s.feature_version == "v4" and s.engine_version == "exit-brain-v5"

    burn = {"kind": "sol_burn", "block_no": 108, "tx_sig": "burn-1", "maker": None,
            "amount0": "-90000", "amount1": "-56000"}
    burn_sync = {"kind": "sol_sync", "block_no": 108, "tx_sig": "burn-1", "maker": None,
                 "amount0": "10", "amount1": "10"}
    times[108] = "2026-07-24T00:00:38+00:00"
    s2 = compute_state(swaps + [burn, burn_sync], times, contract, now_ts=now_ts)
    assert s2.depth_pull_flag is True          # the rug signal, live on Solana
    # WO-3 (pull lane): the removal is sized — |quote burned| / (|burned| + post reserve).
    assert s2.liquidity_removed_fraction is not None and s2.liquidity_removed_fraction > 0.99


def test_tape_modules_never_touch_custody_var():
    import inspect
    body = inspect.getsource(st)
    assert "MEMECOIN_SOLANA_RPC_URL" not in body
    assert "solana_read_rpc" in body


# --- P1 regression, Codex review 2026-07-24 --------------------------------------------------
def test_enumeration_pages_back_to_the_cursor_and_never_skips_a_backlog():
    """A multi-page backlog must reach the cursor without skipping signatures."""
    from sniper.services.memecoin_solana_tape import _new_signatures
    universe = [{"signature": f"s{i:04d}", "slot": i} for i in range(250)]   # s0000 oldest
    def get_sigs(_vault, params):
        newest_first = universe[::-1]
        before = params.get("before")
        if before:
            idx = next(i for i, s in enumerate(newest_first) if s["signature"] == before)
            newest_first = newest_first[idx + 1:]
        until = params.get("until")
        if until:
            stop = next((i for i, s in enumerate(newest_first) if s["signature"] == until), None)
            if stop is not None:
                newest_first = newest_first[:stop]
        return newest_first[:params["limit"]]

    got, reached = _new_signatures("V", None, get_sigs)
    assert reached
    assert [s["signature"] for s in got] == [s["signature"] for s in universe]   # all, oldest-first

    # and with a cursor: everything strictly newer than it, nothing skipped
    got2, reached2 = _new_signatures("V", "s0009", get_sigs)
    assert reached2
    assert [s["signature"] for s in got2] == [f"s{i:04d}" for i in range(10, 250)]


def test_signature_enumeration_uses_maximum_rpc_pages_without_growing_work_bound():
    """Use Solana's 1,000-row page while preserving the existing 5,000-row pass bound."""
    from sniper.services import memecoin_solana_tape as st
    assert st._SIG_LIMIT == 1000
    assert st._MAX_PAGES == 5
    assert st._SIG_LIMIT * st._MAX_PAGES == 5000


def test_enumeration_fails_closed_when_a_page_fails():
    from sniper.services.memecoin_solana_tape import _new_signatures
    assert _new_signatures("V", None, lambda _v, _p: None) == (None, False)


from sniper.services.memecoin_solana_tape import _new_signatures  # noqa: E402


def _sig_universe(n):
    universe = [{"signature": f"s{i:04d}", "slot": i} for i in range(1, n + 1)]
    def get_sigs(_vault, params):
        nf = universe[::-1]
        if params.get("before"):
            i = next(k for k, x in enumerate(nf) if x["signature"] == params["before"])
            nf = nf[i + 1:]
        if params.get("until"):
            st = next((k for k, x in enumerate(nf) if x["signature"] == params["until"]), None)
            if st is not None:
                nf = nf[:st]
        return nf[:params["limit"]]
    return universe, get_sigs


def test_backlog_larger_than_one_page_bound_is_still_enumerated_whole():
    """Codex 2026-07-24: with 1,500 new signatures the 10-page bound returned s0501..s1500 and the
    caller then advanced the cursor, putting s0001..s0500 permanently BELOW it."""
    universe, get_sigs = _sig_universe(1500)
    got, reached = _new_signatures("V", None, get_sigs)
    assert reached
    assert [x["signature"] for x in got] == [x["signature"] for x in universe]


def test_hitting_the_enumeration_bound_reports_not_reached_instead_of_pretending():
    """The bound is a runaway guard. Tripping it means the walk did NOT reach the cursor, and the
    caller must be able to see that — an error log is operator-visible, not engine-visible."""
    from sniper.services import memecoin_solana_tape as st
    _u, get_sigs = _sig_universe(st._MAX_PAGES * st._SIG_LIMIT + 500)
    got, reached = _new_signatures("V", None, get_sigs)
    assert len(got) == st._MAX_PAGES * st._SIG_LIMIT
    assert reached is False


def test_empty_cursor_stops_at_explicit_evidence_floor_not_genesis():
    rows = [{"signature": f"s{i}", "slot": i, "blockTime": i * 10}
            for i in range(100, 0, -1)]
    calls = []

    def get_sigs(_vault, params):
        calls.append(dict(params))
        page = rows
        if params.get("before"):
            idx = next(i for i, row in enumerate(page)
                       if row["signature"] == params["before"])
            page = page[idx + 1:]
        return page[:params["limit"]]

    got, reached = _new_signatures("V", None, get_sigs, floor_ts=950)
    assert reached is True
    assert [r["signature"] for r in got] == [f"s{i}" for i in range(95, 101)]
    assert len(calls) == 1


def test_evidence_floor_never_skips_a_signature_with_unknown_block_time():
    rows = [
        {"signature": "new", "slot": 3, "blockTime": 1000},
        {"signature": "unknown", "slot": 2, "blockTime": None},
        {"signature": "old", "slot": 1, "blockTime": 800},
    ]

    got, reached = _new_signatures(
        "V", None, lambda _vault, params: rows[:params["limit"]], floor_ts=900)

    assert reached is True
    assert [row["signature"] for row in got] == ["old", "unknown", "new"]


def test_older_position_expands_floor_by_restarting_without_a_hole():
    tc = _cursor_db()
    st.ensure_sol_cursor(tc)
    st._write_cursor(tc, "V", "s100", "t0", None, 950)
    rows = [{"signature": f"s{i}", "slot": i, "blockTime": i * 10}
            for i in range(100, 89, -1)]

    def get_sigs(_vault, params):
        return rows[:params["limit"]]

    fetched = []
    st.tape_pass_for_vault(
        tc, "MINT", "V", get_sigs,
        lambda sig: fetched.append(sig) or {
            "meta": {"preTokenBalances": [], "postTokenBalances": []},
            "blockTime": int(sig[1:]) * 10,
        },
        "t1", tx_budget=20, floor_ts=900)
    row = tc.execute(
        "SELECT last_sig, floor_ts, frontier_before FROM sol_sig_cursor WHERE vault='V'").fetchone()
    assert fetched == [f"s{i}" for i in range(90, 101)]
    assert tuple(row) == ("s100", 900.0, None)


# --- completeness must reach the ENGINE, not just the log (Codex 2026-07-24) -----------------

def _cursor_db():
    import sqlite3
    from sniper.persistence import db_ticks as _t
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    _t.ensure_tick_tables(c); _t.ensure_block_times(c)
    return c


def _huge_backlog_fns(n):
    """n new signatures — more than one enumeration walk can reach."""
    universe, get_sigs = _sig_universe(n)
    def get_tx(sig):
        slot = int(sig[1:])
        return {"meta": {"preTokenBalances": [], "postTokenBalances": []},
                "blockTime": 1_700_000_000 + slot}
    return universe, get_sigs, get_tx


def test_bound_hit_holds_the_cursor_and_marks_the_vault_incomplete():
    """The old code logged the loss and advanced anyway, putting the un-enumerated remainder
    permanently BELOW the cursor. It must instead hold and record a drain frontier."""
    from sniper.services import memecoin_solana_tape as st
    tc = _cursor_db()
    n = st._MAX_PAGES * st._SIG_LIMIT + 500
    _u, get_sigs, get_tx = _huge_backlog_fns(n)
    st.tape_pass_for_vault(tc, "MINT", "V", get_sigs, get_tx, "2026-01-01T00:00:00Z", tx_budget=40)
    row = tc.execute("SELECT last_sig, frontier_before FROM sol_sig_cursor WHERE vault='V'").fetchone()
    assert row is not None and row["frontier_before"], "no drain frontier recorded"
    assert not row["last_sig"], "cursor advanced over an un-enumerated hole"
    assert st.vault_tape_incomplete(tc, "V") is True


def test_failed_signatures_advance_cursor_without_clearing_excluded_head():
    """Finalized failed transactions are proven no-ops, not an undecodable continuity hole."""
    from sniper.services import memecoin_solana_tape as st
    tc = _cursor_db()
    rows = [{"signature": f"s{i}", "slot": i, "err": {"InstructionError": [0, 1]}}
            for i in range(1, 7)]

    def get_sigs(_vault, params):
        newest = rows[::-1]
        if params.get("before"):
            index = next(i for i, row in enumerate(newest)
                         if row["signature"] == params["before"])
            newest = newest[index + 1:]
        return newest[:params["limit"]]

    st.ensure_sol_cursor(tc)
    st._write_cursor(tc, "V", "s0", "t0", st._frontier("enum", "s5"))
    fetched = []
    assert st.tape_pass_for_vault(
        tc, "MINT", "V", get_sigs, lambda sig: fetched.append(sig), "t1", tx_budget=10,
    ) == 0
    cursor = tc.execute(
        "SELECT last_sig,frontier_before FROM sol_sig_cursor WHERE vault='V'"
    ).fetchone()
    assert fetched == []
    assert tuple(cursor) == ("s4", st._frontier("decode", "s5"))
    assert st.vault_tape_incomplete(tc, "V") is True


# --- P0-4 (Codex 2026-07-24 activation-blocker review) ----------------------------------------

def test_budget_truncation_leaves_the_vault_incomplete_not_silently_complete():
    """A fully-enumerated batch (reached=True, no page-bound hole) larger than the DECODE budget
    must still read incomplete. Old bug: the cursor advanced over only the decoded prefix and left
    `frontier_before` NULL, so 40/41 real, already-enumerated signatures read as a COMPLETE vault
    with one silently never decoded — exactly the repro Codex named (41 sigs / budget 40)."""
    from sniper.services import memecoin_solana_tape as st
    tc = _cursor_db()
    universe, get_sigs, get_tx = _huge_backlog_fns(41)
    n = st.tape_pass_for_vault(tc, "MINT", "V", get_sigs, get_tx, "2026-01-01T00:00:00Z", tx_budget=40)
    assert n == 40
    row = tc.execute("SELECT last_sig, frontier_before FROM sol_sig_cursor WHERE vault='V'").fetchone()
    assert row["frontier_before"], "no incompleteness recorded despite 1 undecoded signature"
    assert st.vault_tape_incomplete(tc, "V") is True
    # never permanently lost, only delayed: a later pass rediscovers and decodes the outstanding sig.
    for _ in range(3):
        st.tape_pass_for_vault(tc, "MINT", "V", get_sigs, get_tx, "2026-01-01T00:00:00Z", tx_budget=40)
        row = tc.execute("SELECT last_sig, frontier_before FROM sol_sig_cursor WHERE vault='V'").fetchone()
        if row["last_sig"] == universe[-1]["signature"] and not row["frontier_before"]:
            break
    assert row["last_sig"] == universe[-1]["signature"]
    assert row["frontier_before"] is None
    assert st.vault_tape_incomplete(tc, "V") is False


def test_zero_decode_budget_holds_existing_cursor_and_marks_incomplete():
    """A vault reached with no remaining transaction budget must not erase its prior cursor."""
    tc = _cursor_db()
    universe, get_sigs = _sig_universe(3)
    st.ensure_sol_cursor(tc)
    st._write_cursor(tc, "V", universe[0]["signature"], "t0", None)
    fetched = []
    n = st.tape_pass_for_vault(
        tc, "MINT", "V", get_sigs, lambda sig: fetched.append(sig), "t1", tx_budget=0)
    row = tc.execute(
        "SELECT last_sig, frontier_before FROM sol_sig_cursor WHERE vault='V'").fetchone()
    assert n == 0 and fetched == []
    assert row["last_sig"] == universe[0]["signature"]
    assert row["frontier_before"].startswith("decode:")
    assert st.vault_tape_incomplete(tc, "V")


def test_incomplete_vault_is_not_candidate_capable():
    """The whole point: incompleteness has to arrive at compute_state as DATA, or the engine keeps
    acting on a tape it knows is missing trades."""
    from sniper.services.memecoin_exit_market_state import compute_state
    contract = {"chain_id": "4663", "mint": "C", "pool": "0xp", "pool_kind": "v2",
                "position_id": 1, "quote_side": 0, "entry_price": 1.0, "tape_incomplete": True}
    st = compute_state([], {}, contract, cfg=None, now_ts=1_700_000_000)
    assert st.state.name == "DATA_INSUFFICIENT"
    assert not st.candidate_capable()


def test_positions_override_captures_only_explicit_targets(monkeypatch):
    from sniper.services import memecoin_solana_tape as st

    real = [{"id": "real", "mint": "RealMint", "network_id": "solana",
             "opened_at": "2026-07-26T12:00:00+00:00"}]
    target = {"id": "v6", "mint": "V6Mint", "network_id": "solana",
              "opened_at": "2026-07-26T12:00:00+00:00"}
    monkeypatch.setattr("sniper.services.memecoin_fastlane.open_real_positions", lambda _c: real)
    seen = []
    monkeypatch.setattr(
        st, "resolve_position_pool",
        lambda tc, mc, pos, get_tx, **kw: (seen.append(pos["id"]) or None, 0),
    )

    spent = st.solana_tape_pass(
        object(), object(), object(), object(), "2026-07-26T12:00:00+00:00",
        tx_budget=10, positions_override=[target],
    )

    assert spent == 0
    assert seen == ["v6"]


def test_normal_pass_reserves_budget_for_funded_vault_during_paper_discovery(monkeypatch):
    from sniper.services import memecoin_solana_tape as st

    opened_at = "2026-07-26T12:00:00+00:00"
    real = [{"id": "real", "mint": "RealMint", "network_id": "solana",
             "opened_at": opened_at}]
    mconn = sqlite3.connect(":memory:")
    mconn.row_factory = sqlite3.Row
    mconn.execute(
        "CREATE TABLE memecoin_positions(id TEXT,mint TEXT,network_id TEXT,opened_at TEXT,"
        "is_open INTEGER,execution_mode TEXT)"
    )
    mconn.executemany(
        "INSERT INTO memecoin_positions VALUES(?,?,?,?,1,'paper')",
        (("paper-a-old", "PaperA", "solana", opened_at),
         ("paper-a-new", "PaperA", "solana", "2026-07-26T12:01:00+00:00"),
         ("paper-b-old", "PaperB", "solana", opened_at),
         ("paper-b-new", "PaperB", "solana", "2026-07-26T12:01:00+00:00")),
    )
    monkeypatch.setattr("sniper.services.memecoin_fastlane.open_real_positions", lambda _c: real)
    resolved = []

    def resolve(_tc, _mc, pos, _get_tx, **kwargs):
        resolved.append((pos["id"], kwargs["max_fetch"]))
        if pos["id"] == "real":
            return "FundedVault", 0
        return f"Vault-{pos['mint']}", 1 if kwargs["max_fetch"] else 0

    monkeypatch.setattr(st, "resolve_position_pool", resolve)
    captures = []

    def tape(_tc, _mint, vault, _get_sigs, _get_tx, _now, **kwargs):
        captures.append((vault, kwargs["tx_budget"]))
        return 1

    monkeypatch.setattr(st, "tape_pass_for_vault", tape)

    spent = st.solana_tape_pass(
        mconn, object(), object(), object(), opened_at,
        tx_budget=4, include_paper=True,
    )

    assert [row[0] for row in resolved] == [
        "real", "paper-a-old", "paper-b-old", "paper-a-new", "paper-b-new",
    ]
    assert captures[0] == ("FundedVault", 2)
    assert spent == 4


def test_positions_override_shares_decode_budget_across_every_v6_vault(monkeypatch):
    targets = [
        {"id": f"v6-{index}", "mint": f"Mint{index}", "network_id": "solana",
         "opened_at": "2026-07-26T12:00:00+00:00"}
        for index in range(3)
    ]
    monkeypatch.setattr(
        st, "resolve_position_pool",
        lambda _tc, _mc, pos, _get_tx, **_kw: (f"Vault-{pos['id']}", 0),
    )
    budgets = []

    def tape(_tc, _mint, vault, _get_sigs, _get_tx, _now, **kwargs):
        budgets.append((vault, kwargs["tx_budget"]))
        return kwargs["tx_budget"]

    monkeypatch.setattr(st, "tape_pass_for_vault", tape)

    spent = st.solana_tape_pass(
        object(), object(), object(), object(), "2026-07-26T12:00:00+00:00",
        tx_budget=9, positions_override=targets,
    )

    assert spent == 9
    assert budgets == [("Vault-v6-0", 3), ("Vault-v6-1", 3), ("Vault-v6-2", 3)]


def test_normal_pass_yields_between_positions_when_wall_clock_slice_expires(monkeypatch):
    opened_at = "2026-07-26T12:00:00+00:00"
    positions = [
        {"id": "real-1", "mint": "Mint1", "network_id": "solana", "opened_at": opened_at},
        {"id": "real-2", "mint": "Mint2", "network_id": "solana", "opened_at": opened_at},
    ]
    monkeypatch.setattr(
        "sniper.services.memecoin_fastlane.open_real_positions", lambda _conn: positions)
    resolved = []
    clock_values = iter((0.0, 6.0))
    monkeypatch.setattr(
        st, "resolve_position_pool",
        lambda _tc, _mc, pos, _get_tx, **_kw: (resolved.append(pos["id"]) or None, 0),
    )

    st.solana_tape_pass(
        object(), object(), object(), object(), opened_at, tx_budget=40,
        deadline_monotonic=5.0, clock=lambda: next(clock_values),
    )

    assert resolved == ["real-1"]


def test_paper_discovery_deadline_does_not_drop_resolved_funded_vault(monkeypatch):
    opened_at = "2026-07-26T12:00:00+00:00"
    real = [{"id": "real", "mint": "RealMint", "network_id": "solana",
             "opened_at": opened_at}]
    mconn = sqlite3.connect(":memory:")
    mconn.row_factory = sqlite3.Row
    mconn.execute(
        "CREATE TABLE memecoin_positions(id TEXT,mint TEXT,network_id TEXT,opened_at TEXT,"
        "is_open INTEGER,execution_mode TEXT)"
    )
    mconn.execute(
        "INSERT INTO memecoin_positions VALUES(?,?,?,?,1,'paper')",
        ("paper", "PaperMint", "solana", opened_at),
    )
    monkeypatch.setattr(
        "sniper.services.memecoin_fastlane.open_real_positions", lambda _conn: real)
    now = [0.0]

    def resolve(_tc, _mc, pos, _get_tx, **_kwargs):
        if pos["id"] == "paper":
            now[0] = 6.0
            return "PaperVault", 0
        return "FundedVault", 0

    monkeypatch.setattr(st, "resolve_position_pool", resolve)
    captured = []
    monkeypatch.setattr(
        st, "tape_pass_for_vault",
        lambda _tc, _mint, vault, *_args, **_kwargs: captured.append(vault) or 0,
    )

    st.solana_tape_pass(
        mconn, object(), object(), object(), opened_at,
        tx_budget=4, include_paper=True,
        deadline_monotonic=5.0, clock=lambda: now[0],
    )

    assert captured == ["FundedVault"]


def test_positions_override_checks_complete_vaults_before_spending_remainder_on_backlog(
        monkeypatch):
    targets = [
        {"id": f"v6-{index}", "mint": f"Mint{index}", "network_id": "solana",
         "opened_at": "2026-07-26T12:00:00+00:00"}
        for index in range(3)
    ]
    tc = sqlite3.connect(":memory:")
    tc.execute("CREATE TABLE sol_sig_cursor(vault TEXT,frontier_before TEXT)")
    tc.executemany("INSERT INTO sol_sig_cursor VALUES(?,?)", (
        ("Vault-v6-0", "decode:backlog"),
        ("Vault-v6-1", None),
        ("Vault-v6-2", None),
    ))
    monkeypatch.setattr(
        st, "resolve_position_pool",
        lambda _tc, _mc, pos, _get_tx, **_kw: (f"Vault-{pos['id']}", 0),
    )
    budgets = []

    def tape(_tc, _mint, vault, _get_sigs, _get_tx, _now, **kwargs):
        budgets.append((vault, kwargs["tx_budget"]))
        return kwargs["tx_budget"] if vault == "Vault-v6-0" else 0

    monkeypatch.setattr(st, "tape_pass_for_vault", tape)

    assert st.solana_tape_pass(
        object(), tc, object(), object(), "2026-07-26T12:00:00+00:00",
        tx_budget=9, positions_override=targets,
    ) == 9
    assert budgets == [("Vault-v6-1", 3), ("Vault-v6-2", 4), ("Vault-v6-0", 9)]


def test_exact_bound_remainder_is_decoded_before_vault_becomes_complete():
    """Clearing the old hole is not completion: an exactly-full discarded head must decode too."""
    from sniper.services import memecoin_solana_tape as st
    tc = _cursor_db()
    n = st._MAX_PAGES * st._SIG_LIMIT + 300
    universe, get_sigs = _sig_universe(n)
    decoded = []

    def get_tx(sig):
        decoded.append(sig)
        slot = int(sig[1:])
        return {"meta": {"preTokenBalances": [], "postTokenBalances": []},
                "blockTime": 1_700_000_000 + slot}

    row = None
    for _ in range(40):
        st.tape_pass_for_vault(tc, "MINT", "V", get_sigs, get_tx, "2026-01-01T00:00:00Z",
                               tx_budget=200)
        row = tc.execute(
            "SELECT last_sig, frontier_before FROM sol_sig_cursor WHERE vault='V'"
        ).fetchone()
        if row["last_sig"] == universe[-1]["signature"] and not row["frontier_before"]:
            break
    assert row is not None
    assert row["last_sig"] == universe[-1]["signature"]
    assert row["frontier_before"] is None
    assert decoded == [s["signature"] for s in universe]
    assert st.vault_tape_incomplete(tc, "V") is False


def test_completeness_holds_continuously_across_budget_truncation():
    """P0-B (Codex round 2): 41 sigs / budget 40. At NO pass with an un-decoded signature
    remaining may the vault read complete. Pass 2 used to clear the frontier on an empty
    bounded drain while s0041 was still un-decoded."""
    tc = _ticks()
    st.ensure_sol_cursor(tc)
    all_sigs = [{"signature": f"s{i:04d}", "slot": i} for i in range(41, 0, -1)]  # newest-first

    def get_sigs(_a, params):
        until, before = params.get("until"), params.get("before")
        out = []
        for s in all_sigs:
            n = int(s["signature"][1:])
            if until and n <= int(until[1:]):
                continue
            if before and n >= int(before[1:]):
                continue
            out.append(s)
        return out[:params.get("limit", 100)]

    ok_tx = _tx([(COIN, POOL_OWNER, COIN_VAULT, 100), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 50)],
                [(COIN, POOL_OWNER, COIN_VAULT, 90), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 56)])
    ok_tx["blockTime"] = 1_700_000_000

    def decoded():
        return tc.execute("SELECT COUNT(DISTINCT tx_sig) FROM pool_ticks").fetchone()[0]

    for pass_no in range(1, 5):
        st.tape_pass_for_vault(tc, COIN, COIN_VAULT, get_sigs, lambda s: ok_tx,
                               f"2026-07-24T00:0{pass_no}:00+00:00", tx_budget=40)
        remaining = 41 - decoded()
        if remaining > 0:
            assert st.vault_tape_incomplete(tc, COIN_VAULT), \
                f"pass {pass_no}: {remaining} un-decoded sigs but vault read COMPLETE"
        else:
            break
    assert decoded() == 41
    assert not st.vault_tape_incomplete(tc, COIN_VAULT)


def test_completeness_holds_continuously_across_multi_pass_decode_backlog():
    """A decode frontier is not an RPC `before` frontier. With 100 signatures / budget 40,
    applying the exclusive `before` bound to the decode marker used to hide s0099+s0100 on pass 3:
    the cursor sat at s0098 while the vault briefly read complete. Every unfetched signature keeps
    the vault incomplete, regardless of how many non-empty bounded segments precede the tail."""
    tc = _cursor_db()
    universe, get_sigs, get_tx = _huge_backlog_fns(100)
    fetched = []

    def recording_tx(sig):
        fetched.append(sig)
        return get_tx(sig)

    expected = {s["signature"] for s in universe}
    for pass_no in range(1, 8):
        st.tape_pass_for_vault(
            tc, "MINT", "V", get_sigs, recording_tx,
            f"2026-07-24T00:0{pass_no}:00+00:00", tx_budget=40,
        )
        missing = expected - set(fetched)
        if missing:
            assert st.vault_tape_incomplete(tc, "V"), (
                f"pass {pass_no}: {len(missing)} signatures unfetched but vault read complete"
            )
        else:
            break

    assert set(fetched) == expected
    assert not st.vault_tape_incomplete(tc, "V")


def test_new_head_arriving_during_decode_drain_stays_inside_incomplete_region():
    tc = _cursor_db()
    universe, _initial_get_sigs, get_tx = _huge_backlog_fns(100)
    fetched = []

    def get_sigs(_vault, params):
        until, before = params.get("until"), params.get("before")
        out = []
        for s in reversed(universe):                 # newest first, including a later appended head
            n = int(s["signature"][1:])
            if until and n <= int(until[1:]):
                continue
            if before and n >= int(before[1:]):
                continue
            out.append(s)
        return out[:params.get("limit", 100)]

    def recording_tx(sig):
        fetched.append(sig)
        return get_tx(sig)

    st.tape_pass_for_vault(tc, "MINT", "V", get_sigs, recording_tx,
                           "2026-07-24T00:01:00+00:00", tx_budget=40)
    universe.append({"signature": "s0101", "slot": 101})
    expected = {s["signature"] for s in universe}

    for pass_no in range(2, 8):
        st.tape_pass_for_vault(tc, "MINT", "V", get_sigs, recording_tx,
                               f"2026-07-24T00:0{pass_no}:00+00:00", tx_budget=40)
        missing = expected - set(fetched)
        assert not missing or st.vault_tape_incomplete(tc, "V")
        if not missing:
            break

    assert set(fetched) == expected
    assert not st.vault_tape_incomplete(tc, "V")


# --- the pool's owning program, recorded 2026-09-02 ----------------------------------------------
#
# `fastlane_sol_state` marks exist only when a pool's program emits an account update decoded as
# `sol_sync`; `_record_state` runs on nothing else. So state coverage is a property of the owning
# program — and it was the one field the resolver already computed and threw away into `_owner`.
# Without it, "which programs never answer" is not a question the data can be asked: on 2026-09-02,
# 18 of 24 uncovered funded mints had never emitted a state mark and not one program was nameable.


def test_the_vault_table_carries_the_owning_program():
    import sqlite3
    from sniper.services import memecoin_solana_tape as st
    tc = sqlite3.connect(":memory:")
    st.ensure_sol_cursor(tc)
    cols = {r[1] for r in tc.execute("PRAGMA table_info(sol_pool_vaults)")}
    assert "program" in cols, (
        "without the owning program, a state-coverage gap cannot be attributed to anything")


def test_the_program_column_is_added_to_an_existing_table():
    """Idempotent migration: a database created before this column gains it, and gains it once."""
    import sqlite3
    from sniper.services import memecoin_solana_tape as st
    tc = sqlite3.connect(":memory:")
    tc.execute("CREATE TABLE sol_pool_vaults ("
               "pool TEXT PRIMARY KEY, mint TEXT NOT NULL, coin_vault TEXT NOT NULL, "
               "quote_vault TEXT NOT NULL, quote_mint TEXT NOT NULL, resolved_at TEXT NOT NULL)")
    tc.execute("INSERT INTO sol_pool_vaults VALUES('P','M','C','Q','QM','2026-09-01T00:00:00+00:00')")
    st.ensure_sol_cursor(tc)
    st.ensure_sol_cursor(tc)                       # twice: the guard must be idempotent
    cols = {r[1] for r in tc.execute("PRAGMA table_info(sol_pool_vaults)")}
    assert "program" in cols
    # the pre-existing row survives, and NULL means "predates the column", never "no program"
    assert tc.execute("SELECT program FROM sol_pool_vaults WHERE pool='P'").fetchone()[0] is None


def test_the_tape_emits_for_a_pool_quoted_in_an_exotic_token(monkeypatch, tmp_path):
    """A pool quoted in something other than WSOL/USDC must still produce ticks.

    QQQ position 63509 (2026-09-22) sat on pool 2Qrpq...9mxk, quoted in Xs8S1uUs... — QQQx, a
    tokenized Nasdaq ETF at $748.52 with $2.3M liquidity, so perfectly priceable. `vault_rows`
    matches the quote leg by MINT against a default of (WSOL, USDC), so it found no quote leg:
    no `sol_swap` row at all, and a `sol_sync` whose amount1 is None, which `_record_state`
    refuses. pool_ticks held ZERO rows for that pool for its whole life.

    The resolver accepts exotic quotes deliberately (the 2026-08-14 PEECOIN/GLDx dynamic pass) and
    bound this pool fine, so the two halves of the lane disagreed. The binding knows the real quote
    mint; the emitter now uses it.
    """
    from sniper.services.memecoin_solana_amounts import vault_rows

    COIN, QUOTE = "CoinMint", "Xs8S1uUs1zvS2p7iwtsG3b6fkhpvmwz4GYU3gWAmWHZ"
    COIN_VAULT, QUOTE_VAULT = "CoinVault", "QuoteVault"
    tx = {"meta": {
        "preTokenBalances": [
            {"accountIndex": 0, "mint": COIN, "owner": "pool",
             "uiTokenAmount": {"amount": "1000", "decimals": 6}},
            {"accountIndex": 1, "mint": QUOTE, "owner": "pool",
             "uiTokenAmount": {"amount": "5000", "decimals": 6}}],
        "postTokenBalances": [
            {"accountIndex": 0, "mint": COIN, "owner": "pool",
             "uiTokenAmount": {"amount": "1200", "decimals": 6}},
            {"accountIndex": 1, "mint": QUOTE, "owner": "pool",
             "uiTokenAmount": {"amount": "4000", "decimals": 6}}]},
        "transaction": {"message": {"accountKeys": [COIN_VAULT, QUOTE_VAULT]}}}

    default = vault_rows(tx, COIN, COIN_VAULT, slot=7, signature="sig")
    bound = vault_rows(tx, COIN, COIN_VAULT, slot=7, signature="sig", quote_mints=(QUOTE,))

    assert not [r for r in default if r["kind"] == "sol_swap"], (
        "the static quote pair should not have matched this pool — if it does, the fixture is "
        "wrong and this test proves nothing")
    swaps = [r for r in bound if r["kind"] == "sol_swap"]
    assert swaps, "the bound quote mint must produce a swap row"
    assert swaps[0]["amount1"] is not None
    syncs = [r for r in bound if r["kind"] == "sol_sync"]
    assert syncs and syncs[0]["amount1"] is not None, "a state row needs both reserves"
