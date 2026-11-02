import pytest
import threading
import time

_REAL_MONOTONIC = time.monotonic

# --- THE OPERATOR'S REAL .env MUST NOT REACH A TEST PROCESS (2026-09-09) -------------------------
#
# THE CARRIER. `.env` sets MEMECOIN_REAL_GLOBAL_FEED_ENTRIES=0. Around ten test files call
# `load_dotenv` / `load_env_files`, which put that into `os.environ` process-wide. Settings reads it
# AT IMPORT TIME into a module constant (`sniper/settings/memecoin.py`), and the runner then refuses
# every `global_feed` entry — so a test whose event is global_feed fails, in a file that never
# mentions the flag. Measured on one 101-file chunk: 18 failures, of which 16 were this value and
# the other 2 were MEMECOIN_REAL_MAX_TURNOVER_M5 and MEMECOIN_EVM_V4_MULTIHOP_MIN_DEPTH_CLIPS,
# also from `.env`.
#
# WHY IT LOOKS LIKE A RACE AND IS NOT. One `load_dotenv` poisons every module imported after it in
# the same process, so a single worker (`-n0`) is WORSE than parallel — under xdist each worker
# takes a subset and many never import a dotenv-loading file. The failing test identity varies
# because it is decided by which module imports settings first. There is no minimal (polluter,
# victim) PAIR: the reproducer is an ORDER.
#
# WHY THIS RUNS AT IMPORT AND NOT IN A FIXTURE. The constant is poisoned when settings is IMPORTED.
# An autouse fixture runs per-test, long after that, and would be too late.
#
# SURGICAL, NOT A BLANKET NO-OP. Only a load resolving to the repo's own `.env` / `.fomo.env` is
# skipped. A test that loads its own tmp file still works, which is what keeps `test_env_files.py`
# (it tests the loader) and every `monkeypatch`-based caller honest.
import os as _os
import pathlib as _pathlib

# LOWER CPU PRIORITY FOR EVERY TEST PROCESS (operator, 2026-10-07: more xdist workers, pytest.ini -n 10).
# Tests share the host with the funded runner; at nice 10 the runner's threads always win the CPU.
# The main process and each xdist worker load this file, so all of them drop priority (raising
# niceness needs no privilege). Subprocesses a test starts inherit it. Absolute, not +10: a worker
# inherits its parent's 10 and would otherwise stack to 19.
_os.nice(max(0, 10 - _os.nice(0)))

_REPO_ROOT = _pathlib.Path(__file__).resolve().parents[1]
# This tree AND, from a git worktree, the main checkout: research scripts hard-code the main checkout
# as their state root, and a load of its .env from a worktree run went straight past a guard that knew
# only this tree (2026-10-02; tests/_isolation_guards.py).
from _isolation_guards import env_roots as _env_roots  # noqa: E402
_ENV_ROOTS = _env_roots(_REPO_ROOT)
_OPERATOR_ENV_FILES = tuple((root / n) for root in sorted(_ENV_ROOTS) for n in (".env", ".fomo.env"))


def _operator_env_key_names():
    """The KEY NAMES in the operator's env files. Names only — a value is never read here."""
    names = set()
    for path in _OPERATOR_ENV_FILES:
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                names.add(line.split("=", 1)[0].strip())
    return names


def _is_operator_env(path):
    if not path:
        return False
    try:
        return _pathlib.Path(path).resolve() in {p.resolve() for p in _OPERATOR_ENV_FILES}
    except OSError:
        return False


def _neutralise_operator_env():
    """Scrub what already leaked, then stop the two loaders from bringing it back.

    BOTH HALVES ARE NEEDED. conftest is not guaranteed to beat every import path, so a value may
    already be in `os.environ` before this runs; and a later `load_dotenv` would re-add it.
    """
    leaked = _operator_env_key_names()
    for key in leaked:
        _os.environ.pop(key, None)

    try:
        import dotenv as _dotenv
    except ImportError:
        _dotenv = None
    if _dotenv is not None and not getattr(_dotenv.load_dotenv, "_sniper_neutralised", False):
        _real_load_dotenv = _dotenv.load_dotenv

        def _guarded_load_dotenv(dotenv_path=None, *a, **kw):
            # No path means "find the nearest .env", which in this tree IS the operator's.
            if dotenv_path is None or _is_operator_env(dotenv_path):
                return False
            return _real_load_dotenv(dotenv_path, *a, **kw)

        _guarded_load_dotenv._sniper_neutralised = True
        _dotenv.load_dotenv = _guarded_load_dotenv

    try:
        from sniper.util import envfiles as _envfiles
    except ImportError:
        _envfiles = None
    if _envfiles is not None and not getattr(_envfiles.load_env_files, "_sniper_neutralised", False):
        _real_load_env_files = _envfiles.load_env_files

        def _guarded_load_env_files(root, names=(".env", ".fomo.env"), env=None):
            # A caller passing its own `env` dict cannot pollute os.environ, so let it through —
            # that is how `test_env_files.py` exercises the real loader.
            if env is None and _pathlib.Path(str(root)).resolve() in _ENV_ROOTS:
                return []
            return _real_load_env_files(root, names=names, env=env)

        _guarded_load_env_files._sniper_neutralised = True
        _envfiles.load_env_files = _guarded_load_env_files


_neutralise_operator_env()


# The real-money memecoin knobs this suite pins to canonical defaults, hoisted to module
# scope so a guard can ask WHICH names conftest sets on purpose instead of carrying its own
# copy of the list. Adding an entry here automatically excludes it from that guard, which is
# what keeps the guard non-enumerated (2026-09-09).
PINNED_MEMECOIN_ENV = {
    "MEMECOIN_REAL_ENABLED": "0",
    # The exit dust-route guard (default ON in production) is off for the suite: it holds a deep
    # loss with no own market mark (every EVM fixture but a 4663 tape). tests/test_memecoin_exit_dust_guard.py
    # turns it on for its own cases.
    "MEMECOIN_EXIT_DUST_GUARD": "0",
    # The live .env sets authenticated_funded. Under that mode build_evm_context calls
    # validate_lifi_config(conn=None), which raises "LI.FI activation record is required"
    # and fails four context-build tests plus test_memecoin_multi_venue — but ONLY in a
    # checkout that has a .env, so it passes in every git worktree and looked for a while
    # like another session's WIP. Deterministic, 6/6, and pre-existing.
    "MEMECOIN_LIFI_MODE": "disabled",
    "MEMECOIN_TRADE_SIZE_USD": "10",
    "MEMECOIN_MAX_CONCURRENT": "6",
    "MEMECOIN_MAX_TOTAL_EXPOSURE_USD": "60",
    "MEMECOIN_DAILY_LOSS_LIMIT_USD": "30",
    "MEMECOIN_REAL_SIZING": "fixed",
    "MEMECOIN_MIN_TRADE_USD": "30",
    "MEMECOIN_REAL_STOP_PCT": "50",
    # Timecap lift pinned OFF: the live .env arms it, and a test asserting a family's shipped
    # policy_id must not flip to the -v2 name because the HOST is lifted. Lift tests set it
    # themselves (2026-08-26; test_the_policy_shape_when_it_does_apply broke on env leakage).
    "MEMECOIN_TIMECAP_LIFT_ENABLED": "0",
    "MEMECOIN_REAL_MAX_HOLD_S": "43200",
    "MEMECOIN_REAL_TP_PCT": "0",
    "MEMECOIN_REAL_DERISK_PCT": "0",
    "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO": "0",
    "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD": "0",
    "MEMECOIN_ARMS": "control,momentum_a,alpha_b",
    "MEMECOIN_SOLANA_RPC_URL": "",
    # --- WEB AUTH: never inherit the operator's real credentials -------------------------
    # Same import-time .env leak as above, different blast radius. SNIPER_DASH_USER/PASS arm a
    # before_request BASIC-AUTH guard (sniper/web/server.py:398) that 401s EVERY route, and
    # SNIPER_API_KEY gates the agent endpoints. All three are read at MODULE SCOPE, so whichever
    # test first imports the web stack freezes them — and once armed, every web test gets
    # `401 Authentication required` (text/html), .get_json() returns None, and the failures
    # surface as the baffling "'NoneType' object is not subscriptable".
    #
    # This was 58 failures across test_replay_api (44), test_followable_leaderboard (6),
    # test_ui_algo_tabs, test_web_server_asset_paths, test_classification_diagnostics, ... —
    # all of which pass in isolation, so it read as flakiness for as long as it existed.
    # Force-pinned (not setdefault) so it beats both the shell and the import-time .env load.
    # A test that WANTS auth sets it itself.
    "SNIPER_DASH_USER": "",
    "SNIPER_DASH_PASS": "",
    "SNIPER_API_KEY": "",
    # --- TELEGRAM: the test suite must never message a real human ------------------------
    # TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID live in .env/.fomo.env and leak into os.environ by
    # the same import-time load. Any test that exercised the alert path then resolved the REAL
    # bot + chat and POSTED — the operator was getting ~10 live messages per test run, and after
    # the alerts were pointed at the Memeta group the whole group got spammed.
    # send_telegram() already fails CLOSED with no HTTP call when token/chat are empty, so
    # blanking them here is sufficient AND keeps the send path itself under test.
    # A test that wants to assert a send injects its own token/chat or a fake http seam.
    "TELEGRAM_BOT_TOKEN": "",
    "MEMECOIN_TG_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "MEMECOIN_TG_CHAT_ID": "",
    "MEMECOIN_TG_ALERTS": "0",
    "MEMECOIN_TG_MAX_MCAP_USD": "0",
    "MEMECOIN_TG_MIN_GAP_S": "0",
    # --- APPROVAL SPEED FLAG: tests must default to receipt-first behavior -----------------
    # Read at call time (cfg.memecoin_evm_approval_pipeline),
    # so the import-time .env leak flips _approve to the pipelined return shape mid-suite
    # (test_memecoin_evm_gas_buffer's `ok is True` fails, order-dependent). Force-pinned OFF;
    # a test that wants pipeline behavior monkeypatch.setenv's it itself.
    "MEMECOIN_EVM_APPROVAL_PIPELINE": "0",
    # --- 4663 RECEIPT HINT: no test binds the watcher's live hint socket ---------------------
    # Default ON in code (memecoin_receipt_hint). A test driving run_push on 4663 would otherwise start
    # a listener on the shared name the live 4663 watcher binds. Force-pinned OFF; the hint's own tests
    # set cfg.MEMECOIN_RH_FAST_RECEIPT_FETCH and point socket_path at a temp path.
    "MEMECOIN_RH_FAST_RECEIPT_FETCH": "0",
    # --- 4663 BRIDGE FRESH READ: no test reads the live tx_read host ----------------------------
    # Default ON in code. start_decide would build a reader on the .env tx_read URL. Force-pinned OFF;
    # its tests inject the reader (memecoin_rh_fast_decide.bridge_state_reader's post seam).
    "MEMECOIN_RH_FAST_BRIDGE_FRESH_READ": "0",
}



"""
pytest conftest for the Sniper test suite.

Scoring determinism
-------------------
analyze_users.py reads scoring thresholds from env vars at module load time
(e.g. SCORE_FLAG_THRESHOLD, MIN_PNL, MAX_PNL, EMERGING_SCORE_THRESHOLD, ...).
If any of these are set differently on different machines or CI environments,
the same fixture inputs can produce different scores / classifications, making
the replay harness non-deterministic.

This conftest pins all scoring-relevant env vars to their canonical default
values BEFORE any test module is imported.  Values are taken directly from the
defaults in analyze_users.py so the harness always runs against the baseline
scoring model, regardless of what is set in the shell environment.

To temporarily override a threshold in a test, use:
    import os
    monkeypatch.setenv("SCORE_FLAG_THRESHOLD", "60")
    import importlib, analyze_users
    importlib.reload(analyze_users)   # re-bake the constant
    ... test ...
    importlib.reload(analyze_users)   # restore after test

Operational deployments are free to set env vars as needed; this pinning only
applies inside the pytest process.
"""

import os

from sniper.settings.scoring_env_defaults import pin_scoring_env as _pin_scoring_env_defaults


def pin_scoring_env() -> None:
    """Pin scoring env vars to canonical defaults using setdefault semantics.

    Does not overwrite values the operator has explicitly set in the shell —
    that is intentional for normal development runs where custom thresholds
    may be in use.

    The replay harness force-sets these defaults before reloading analyze_users,
    so fixture results are deterministic regardless of the shell environment.
    """
    # setdefault semantics for general test runs.
    _pin_scoring_env_defaults(force=False)


def _block_production_supabase() -> None:
    """Null out Supabase credentials so tests cannot accidentally write to
    the production database.  Any test that genuinely needs Supabase access
    must explicitly set these env vars (e.g. via monkeypatch.setenv).

    This prevents subprocess-spawning tests (e.g. test_action_tool) from
    inheriting real credentials and polluting strategy_agent_observations.
    """
    os.environ["SUPABASE_URL"] = ""
    os.environ["SUPABASE_SERVICE_KEY"] = ""
    os.environ["SUPABASE_ANON_KEY"] = ""
    # Point the memecoin kill switch at a path that never exists: the default is the LIVE
    # operator file (data/memecoin_kill_switch), so an operator-tripped kill would flip every
    # disarmed-by-default test. Tests that exercise the switch pass their own tmp path.
    os.environ["MEMECOIN_KILL_SWITCH_FILE"] = os.path.join(
        os.path.dirname(__file__), "no-such-kill-switch")
    # Pin the real-money memecoin knobs to canonical defaults: the operator's live .env ARMS the
    # real leg, and sniper/feeds/hyperliquid_feed.py loads .env into os.environ at import
    # (setdefault semantics) — so any test importing it would flip armed-state defaults for the
    # whole worker. Force-pinning here wins over both the shell and that import-time load.
    for _k, _v in PINNED_MEMECOIN_ENV.items():
        os.environ[_k] = _v


def _disable_web_server_background_fetchers() -> None:
    """`web_server` import fires `start_background_fetcher` at module load
    (web_server.py:230), which spawns a daemon thread that calls the live
    Gamma API on a 6-hour loop. In test runs that loop floods the test DB
    with writes, contends with test setup, and (when the network is slow)
    hangs the suite at ~29%.

    Monkey-patch the fetcher to a no-op BEFORE web_server is imported by
    any test. We patch the underlying module attribute so the `from … import
    start_background_fetcher as _start_end_date_fetcher` line in web_server
    resolves to the no-op.
    """
    import sniper.services.fetch_end_dates as _fed_mod

    def _noop_fetcher(*_args, **_kwargs):
        return None

    _fed_mod.start_background_fetcher = _noop_fetcher  # type: ignore[assignment]


def _disable_live_sync_calls() -> None:
    """`POST /api/followed-wallets` calls `sync_watchlist_wallet(wallet,
    force=True)`, which in turn calls `fetch_user_trades` against the
    live Polymarket Data API. The HTTP call has no test-friendly
    timeout and hangs any integration test that exercises that
    endpoint when the API is slow or unreachable.

    Stub both the public sync entrypoint AND the underlying
    fetch_user_trades helper to be no-ops. Tests that genuinely
    exercise the sync logic must mock these explicitly themselves
    (only test_scoring.py imports them directly — flagged for
    retirement in any case).
    """
    from sniper.services import sync as _sync_mod

    def _noop_sync(*_args, **_kwargs):
        return None

    def _noop_fetch_user_trades(*_args, **_kwargs):
        return []

    _sync_mod.sync_watchlist_wallet = _noop_sync  # type: ignore[assignment]
    _sync_mod.fetch_user_trades = _noop_fetch_user_trades  # type: ignore[assignment]

    # `GET /api/recent-suspicious-trades` calls helpers that hit the
    # live Polymarket API even when the test's
    # SUSPICIOUS_STATUS_MAX_WALLET_FETCHES_PER_REQUEST env var is set
    # to 0 — the constant was captured at `sniper.settings.web` import
    # time. Stub the underlying helpers so the endpoint always uses
    # cache (which test code pre-populates).
    def _noop_position_maps(*_args, **_kwargs):
        return ({}, {})

    _sync_mod.fetch_wallet_position_maps = _noop_position_maps  # type: ignore[assignment]

    import sniper.markets.patterns as _patterns_mod
    import sniper.markets.polymarket_api as _pm_mod
    _patterns_mod.fetch_wallet_order_pattern_map = (
        lambda *_a, **_kw: {}
    )  # type: ignore[assignment]
    _pm_mod.fetch_user_trades = lambda *_a, **_kw: []  # type: ignore[assignment]

    # Disable the web_server background executor that fans out to live
    # API syncs (positions, trades, watchlist). Tests that pre-populate
    # the DB and then GET an endpoint should see their inserted rows,
    # not whatever the background sync replaces them with. Stub at the
    # dispatch boundary (`_enqueue_background_sync`) so the underlying
    # `sync.sync_followed_positions_wallet` etc. remain callable for
    # tests that mock the network layer and assert on the sync logic
    # itself (e.g. test_sync_cost_basis).
    # No live upstream Polymarket price socket in a test worker (2026-09-29). The web server starts
    # it in register_ws_stream at its import; switch that off before the import below. It sleeps on
    # and reads the process-wide clock, which the tests that patch time share with it.
    import sniper.routes.ws_stream as _ws_stream_mod
    _ws_stream_mod._POLY_WS_AUTOSTART = False
    try:
        from sniper.web import server as _web_server_mod
        _web_server_mod._enqueue_background_sync = (
            lambda *_a, **_kw: False
        )  # type: ignore[assignment]
    except Exception:
        # web_server may not be importable yet in some test environments;
        # the patch is best-effort. Tests that actually need it will
        # re-trigger the import after this hook runs.
        pass


# Pin at import time (before test collection imports analyze_users).
def _pin_exit_state_worker_off() -> None:
    """The exit-state materializer defaults ON in production (2026-07-23). Force it OFF inside
    pytest so no test can open real ticks/ledger connections or issue block-time RPC just because
    it imported the worker. A test that wants it on sets the env var itself."""
    os.environ["MEMECOIN_EXIT_STATE_ENABLED"] = "0"
    # The desk curve tape is retired by default (operator 2026-10-27); the curve-tape tests exercise recording,
    # so it is pinned ON here. Tests of the retirement set it to 0 or delete it themselves.
    os.environ["MEMECOIN_DESK_CURVE_CAPTURE"] = "1"


def _isolate_state_root_from_operator_env() -> None:
    """Point SNIPER_STATE_ROOT at a throwaway dir with NO .env, exactly as the deploy gate does
    (deploy_memecoin_runtime.sh VALIDATION_STATE). engine_authoritative_chains() and the kill
    flag now read the on-disk state-root `.env` LIVE (Codex 2026-07-25) — so a suite run from the
    repo root while the operator's .env is ARMED would resolve solana/4663 as engine-authoritative
    and fail every band-policy test. That made repo-root runs disagree with the deploy's release
    run: the exact two-context drift this project keeps getting bitten by. A clean state root makes
    both agree — fail-OFF everywhere. Tests needing a specific state root set it themselves.
    A root that is already set (a deliberate integration run, or an xdist worker inheriting the
    controller's) is kept.

    DB_PATH IS SET EITHER WAY (2026-09-27). An xdist worker inherits the controller's root AND its
    throwaway DB_PATH, then `_neutralise_operator_env` pops DB_PATH because the operator's `.env`
    names it. Returning early on the inherited root left DB_PATH unset, so every worker's
    `sniper.web.server` import ran init_db on the relative default: the funded ledger (1,578 write
    locks in two hours, tests/test_conftest_ledger_isolation.py)."""
    root = os.environ.get("SNIPER_STATE_ROOT")
    if not root:
        import tempfile
        root = tempfile.mkdtemp(prefix="sniper-test-stateroot-")
        os.environ["SNIPER_STATE_ROOT"] = root
        # /tmp IS RAM ON THIS HOST (2026-09-28). Nothing removed these: 2,371 roots held 4.8 GB,
        # pushed RAM to 84% and swap to 100%, and the dashboard read WARN/DELAYED. Only the process
        # that CREATED the root removes it; xdist workers inherit it and never register this.
        import atexit
        import shutil
        atexit.register(shutil.rmtree, root, True)
    # DB_PATH defaults to a throwaway ABSOLUTE file under the isolated state root — exactly what
    # the deploy gate does (deploy_memecoin_runtime.sh: DB_PATH="$VALIDATION_STATE/test.db").
    # It used to default to the REAL ledger "for tests that read it via DB_PATH", but nothing
    # holds that path read-only: the Telegram delivery-audit worker WRITES via DB_PATH
    # (memecoin_telegram._persist_delivery_audit), and every repo-root suite run stamped its
    # fixture sends into the production memecoin_telegram_delivery_audit table — 567 rows by
    # 2026-08-06, which then corrupted an operator alert-volume analysis. A test that genuinely
    # needs the real ledger must opt in by setting DB_PATH itself.
    os.environ.setdefault("DB_PATH", os.path.join(root, "test.db"))


def _pin_telegram_dispatch_off() -> None:
    """No test may open a Telegram dispatcher — the worker WRITES to the funded ledger.

    `_persist_delivery_audit` resolves DB_PATH, which `_isolate_state_root_from_operator_env`
    below deliberately points at the real sniper_tracker.db so tests can READ it. The
    delivery-audit worker turned that read affordance into a write: on 2026-08-06 the funded
    ledger held 567 test-written audit rows against 145 real ones — enough to make a phone-noise
    measurement come out 4x wrong. The blanked tokens above stop the SEND but not the audit row,
    because send_telegram fails closed and the dispatcher's `finally` audits anyway.

    Defense in depth, not the primary guard: the token/chat blanking in
    _block_production_supabase() already makes dispatch_enabled() False on its own. This pin holds
    even if a test sets a token. A test that wants the dispatcher sets
    MEMECOIN_TG_DISPATCH_ENABLED=1 itself. Same class as the clock-offset isolation below."""
    os.environ["MEMECOIN_TG_DISPATCH_ENABLED"] = "0"


pin_scoring_env()
_block_production_supabase()
_pin_exit_state_worker_off()
_pin_telegram_dispatch_off()
_isolate_state_root_from_operator_env()

# Isolate the clock-offset file: run_once tests call record_real_utc with stub Date headers,
# which would otherwise pollute the real data/clock_offset.json with a bogus offset (caught in
# a deploy verification 2026-07-08). xdist workers need separate files too, or one worker's
# fake Date header can move another worker's corrected clock mid-test.
_clock_prefix = os.path.join(os.sep, "tmp", "sniper_test_clock_offset")
_clock_cur = os.environ.get("SNIPER_CLOCK_OFFSET_FILE")
if _clock_cur is None or _clock_cur.startswith(_clock_prefix):
    _worker = os.environ.get("PYTEST_XDIST_WORKER") or "main"
    os.environ["SNIPER_CLOCK_OFFSET_FILE"] = f"{_clock_prefix}_{_worker}_{os.getpid()}.json"
_disable_web_server_background_fetchers()
_disable_live_sync_calls()


# --- TESTS NEVER RESOLVE A REAL HOSTNAME (2026-10-01) ---------------------------------------------
#
# THE FAILURE. The deploy of 84a3bdd2a was refused at 99%: xdist workers died one after another
# ("node down: Not properly terminated"). This host's resolver takes ~20 s to answer NXDOMAIN, so a
# test that resolves a fake RPC hostname (`https://rpc.example/...`) waits 20 s for every lookup;
# a few lookups pass --timeout=60, and pytest-timeout's thread method ends the worker with os._exit.
#
# THE GUARD (tests/_hermetic/hermetic_dns.py). A non-local name is refused AT ONCE with
# socket.gaierror: the error such a lookup ends in anyway, without the wait. Local names and IP
# literals resolve as before. SNIPER_TESTS_ALLOW_DNS=1 lets a deliberate live run resolve.
# Installed at import, like the guards above. tests/_hermetic also goes first on PYTHONPATH, so a
# child Python process a test starts runs its sitecustomize.py and inherits the refusal (Gamma LOW:
# a child re-imports socket unpatched and waited 10 s on a lookup). A child given its own env
# without PYTHONPATH, or a non-Python child (curl), is not covered.
_HERMETIC_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_hermetic")


def _refuse_non_local_dns():
    if _os.environ.get("SNIPER_TESTS_ALLOW_DNS") == "1":
        return
    import sys
    if _HERMETIC_DIR not in sys.path:
        sys.path.append(_HERMETIC_DIR)
    import hermetic_dns
    hermetic_dns.install()
    paths = [p for p in _os.environ.get("PYTHONPATH", "").split(_os.pathsep) if p]
    if _HERMETIC_DIR not in paths:
        _os.environ["PYTHONPATH"] = _os.pathsep.join([_HERMETIC_DIR] + paths)


_refuse_non_local_dns()


# NAMED TO SORT FIRST, ON PURPOSE. pytest sets up one conftest's autouse fixtures in ALPHABETICAL order
# and tears them down in reverse. This one must be set up before anything pulls in `monkeypatch`, so
# that every monkeypatch undo runs first and this restore runs LAST: torn down before a monkeypatch,
# it deleted a name the test had added with raising=False and monkeypatch's own undo then failed on
# it (2026-10-02). A fixture, not a pytest hook: a hook would also revert module-scoped fixtures' state
# after their first test.
@pytest.fixture(autouse=True)
def _a_isolation_restore():
    """Every test ends with `sniper.settings.memecoin` and os.environ as it found them
    (tests/_isolation_guards.py). Knob-overlay writes, in-test reloads, in-place edits, direct env
    writes and env-file loads outlived their tests and failed 275 others when the 251-file memecoin
    set ran in reversed file order (2026-10-02)."""
    from _isolation_guards import environ_restored, settings_restored
    with settings_restored(), environ_restored():
        yield


@pytest.fixture(autouse=True)
def _reset_dexscreener_breaker():
    """The DexScreener breaker keeps its cooldown in MODULE globals (same shape as the
    GeckoTerminal one it copies). Without this, a test that trips it leaves every later test in
    the same xdist worker served by an open breaker -- which returns {} with no HTTP call, so the
    victim fails only in a suite run and passes alone. Cost us that exact misdiagnosis twice on
    2026-08-24."""
    from sniper.execution import solana_quote as _q
    _q._ds_cooldown_until[0] = 0.0
    _q._ds_funded_cooldown_until[0] = 0.0
    yield
    _q._ds_cooldown_until[0] = 0.0
    _q._ds_funded_cooldown_until[0] = 0.0


@pytest.fixture(autouse=True)
def _no_inherited_busy_momentum_pool():
    """memecoin_real_runner._momentum_read_pool() is ONE process-global ShadowReads(workers=2) per
    xdist worker, and the gate's shadow reads wait on it for at most 1 s. A test that leaves both of
    its threads busy (a blocking fake, a slow real read) starves a later test in the same worker:
    test_chase_shadow_returns_while_tape_read_is_blocked and
    test_the_gate_shadow_records_the_tape_move_beside_the_display_move refused deploy fc24af7f3
    (2026-10-15, Gamma task 2765ab12) and passed alone. A pool that still has work queued or running
    when a test starts is dropped, so that test lazily builds its own; an idle pool is reused, so
    threads do not grow per test. The dropped pool's threads are daemons that finish and park."""
    import sys
    runner = sys.modules.get("sniper.services.memecoin_real_runner")
    if runner is not None:
        with runner._MOMENTUM_POOL_LOCK:
            pool = runner._MOMENTUM_POOL
            if pool is not None and getattr(pool.queue, "unfinished_tasks", 0):
                runner._MOMENTUM_POOL = None
    yield


@pytest.fixture(autouse=True)
def _reset_broadcast_generation_fault():
    """`exec_lock._GENERATION_FAULT` is a one-way, process-wide latch BY DESIGN: a real broadcast
    stamp failure stands the 4663 fast lane down until the process restarts. Tests that send fake
    raws ("0xraw") through eth_send_raw or a fan-out provider set it, and under xdist it leaked into
    every later lane test on the same worker: 22 failures, all `generation_unknown`, that pass when
    the file runs alone (Alpha 2026-10-01). Clear on both sides: no test inherits a latch, and no
    test exports one. Tests of the latch itself set and read it inside their own body."""
    from sniper.execution import exec_lock as _el
    _el._GENERATION_FAULT.clear()
    _el._GENERATION_FAULT_REASON[0] = None
    yield
    _el._GENERATION_FAULT.clear()
    _el._GENERATION_FAULT_REASON[0] = None


@pytest.fixture(autouse=True)
def _reset_v4_key_scan_memory():
    """`evm_v4_quote._scan_miss` / `_scan_429` / `_scan_depth` / `_scan_exhausted` remember a clean miss, a 429, a walk and
    a walk that ran out per pool / URL across calls in production. Under pytest one test's miss or 429 would park the next
    test's scan."""
    from sniper.execution import evm_v4_quote as _v4
    _v4._scan_miss.clear()
    _v4._scan_429.clear()
    _v4._scan_depth.clear()
    _v4._scan_exhausted.clear()
    yield
    _v4._scan_miss.clear()
    _v4._scan_429.clear()
    _v4._scan_depth.clear()
    _v4._scan_exhausted.clear()


@pytest.fixture(autouse=True)
def _reset_multihop_bridge_memo(monkeypatch):
    """`evm_v4_multihop._BRIDGE_MEMO` keeps bridge-leg provider answers across calls in production
    (2026-10-17), and its refresher thread re-reads them every 60 s. Under pytest one test's answer
    would serve the next test's lookup, and a refresher started by one test would call a finished
    test's fakes in the middle of a later one. Cleared on both sides; the thread never starts."""
    from sniper.execution import evm_v4_multihop as _mh
    _mh.reset_bridge_memo()
    monkeypatch.setattr(_mh, "_ensure_bridge_refresher", lambda: None)
    yield
    _mh.reset_bridge_memo()


@pytest.fixture(autouse=True)
def _reset_multihop_last_good_bridge():
    """`evm_v4_multihop._BRIDGE_LAST_GOOD` remembers a bridge pool across calls in production. Under
    pytest one test's fixture pool would be served to the next test's empty discovery."""
    from sniper.execution import evm_v4_multihop as _mh
    _mh._BRIDGE_LAST_GOOD.clear()
    _mh._POOL_LIQUIDITY.clear()
    yield
    _mh._BRIDGE_LAST_GOOD.clear()
    _mh._POOL_LIQUIDITY.clear()


@pytest.fixture(autouse=True)
def _reset_nominal_peak_ticks():
    """`memecoin_real_runner._PEAK_TICKS` remembers each position's recent display ticks for the
    two-tick peak confirmation (2026-10-17). In-memory test ledgers reuse position ids, so a tick
    from one test would confirm another's. Clear on both sides."""
    import sys as _sys

    def clear():
        _rr = _sys.modules.get("sniper.services.memecoin_real_runner")   # never imported for this
        if _rr is not None and hasattr(_rr, "_PEAK_TICKS"):
            _rr._PEAK_TICKS.clear()
    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def _reset_slot_lane_decision_memory():
    """`memecoin_slot_skips._LAST` remembers the slot lane's last decision per source signature, and
    `slot_lane_verdict` reads it. Tests share signatures (test_memecoin_slot_lane.CURVE), so a volume
    refusal noted by one test made a later test in the same xdist worker read `decided` instead of
    `failed` (deploy gate 2026-10-21). Clear on both sides."""
    import sys as _sys

    def clear():
        _sk = _sys.modules.get("sniper.services.memecoin_slot_skips")   # never imported for this
        if _sk is not None:
            with _sk._LAST_LOCK:
                _sk._LAST.clear()
            _sk._RING.clear()
    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def _reset_funded_429_stamp():
    """`solana_real_seams._funded_last_429_at` remembers the last funded-role Jupiter 429 for 10 s;
    the exotic warmer skips its refresh inside that. Under xdist a failover test's fake 429 would
    quiet a later warmer test in the same worker. Clear on both sides."""
    from sniper.execution import solana_real_seams as _seams
    _seams._funded_last_429_at = None
    yield
    _seams._funded_last_429_at = None


@pytest.fixture(autouse=True)
def _no_leaked_hot_rebuild_request():
    """`request_rebuild()` sets a MODULE-GLOBAL event, and the funded-open path in both swap
    executors calls it. In production that global is right: one process, one hot lane, and any
    funded open should end the epoch. Under pytest a whole suite shares one interpreter, so a
    request set by one test survives into the next test that runs a lane -- which consumes it,
    ends its epoch before the lane emits anything, and fails an assertion that has nothing to do
    with rebuilds. Measured: one leaked request turns
    test_run_hot_exit_lane_drives_evm_lane_to_v5 into `assert ([])`. It only shows up in a large
    run because xdist has to put both tests in the same worker, so it reads as a flaky test
    rather than a leak. Clear on both sides: no test inherits a request, and no test exports one."""
    from sniper.services import memecoin_hot_supervisor as _hot
    _hot.rebuild_requested()
    yield
    _hot.rebuild_requested()


def _drain_display_supply_reads():
    """Wait out and forget the runner's display-supply reads, if the runner module was ever loaded
    (never imports it: most tests do not touch it)."""
    import sys
    rr = sys.modules.get("sniper.services.memecoin_real_runner")
    if rr is None:
        return
    pool = getattr(rr, "_DISPLAY_SUPPLY_POOL", None)
    if pool is not None:
        pool.shutdown(wait=True)
        rr._DISPLAY_SUPPLY_POOL = None
    rr._DISPLAY_SUPPLY_READS.clear()
    rr._DISPLAY_SUPPLY_RETRY_AT.clear()


@pytest.fixture(autouse=True)
def _no_leaked_display_supply_read(monkeypatch):     # declared: torn down before its undo (Gamma NIT)
    """`memecoin_real_runner.pin_display_total_supply` reads on a MODULE-GLOBAL one-thread pool and
    never waits for it, by design. Under pytest a read started by one test (the blind-4663 trail
    sweeps) ran inside the NEXT test on the same xdist worker and hit that test's monkeypatched
    `evm_wallet._http`: test_memecoin_rh_fast_entry.py failed ~1 combined run in 5 with
    ('evm_wallet._http', False) in its network guard (2026-10-07). It requests `monkeypatch`, so it is
    torn down before monkeypatch's undo and the read finishes under the test that started it. tests/test_display_supply_read_isolation.py is the deterministic reproduction."""
    _drain_display_supply_reads()
    yield
    _drain_display_supply_reads()


# THREADS ALIVE TODAY THAT NO TEST CAN STOP YET (2026-10-08): process singletons with no stop hook.
# Each is debt, not permission: give it a stop and delete its name here. A NEW name fails the guard.
_KNOWN_SINGLETON_THREADS = frozenset({
    "memecoin-submit-audit",          # sniper/execution/submission_wiring.py
    "memecoin-paper-fanout",          # sniper/services/memecoin_paper_fanout.py
    "memecoin-telegram-dispatch",     # sniper/services/memecoin_telegram.py
    "memecoin-tpu-monitor",           # sniper/execution/solana_tpu_sidecar.py
})


def _guarded_threads(before):
    """New, live threads a test must not leave behind: non-daemon ones and any `memecoin-*`,
    `rh-fast-*` or `chain1-*` one (a chain1_protect status watch polls protect.flashbots.net every
    12 s for ~6.4 min; tests/test_memecoin_eth_parity.py leaked them and refused deploy 2eba6d095). Module-level ThreadPoolExecutor workers (shared pools, idle between tests) and
    pytest-timeout's own timers are out; so are the known singletons above."""
    out = []
    for t in threading.enumerate():
        if t in before or not t.is_alive() or t.name.startswith("pytest_timeout"):
            continue
        target = getattr(t, "_target", None)
        if getattr(target, "__module__", None) == "concurrent.futures.thread":
            continue
        if t.name in _KNOWN_SINGLETON_THREADS:
            continue
        if not t.daemon or t.name.startswith(("memecoin-", "rh-fast-", "chain1-")):
            out.append(t)
    return out


@pytest.fixture(autouse=True)
def _no_leaked_threads():
    """A thread a test starts must stop in that test (house rule b18c16b4). Deploy e2f5ae905 was
    refused on tests/test_memecoin_rh_fast_entry.py: start_lane's four loops, started by an earlier
    test in the same xdist worker, called urlopen inside a later test's network guard. Submission
    health warmers are stopped through their own registry (submission_backends.close_all); anything
    else still alive 2 s after the test fails it, by name."""
    before = set(threading.enumerate())
    yield
    leaked = _guarded_threads(before)
    if not leaked:
        return
    if any(t.name == "memecoin-submission-health" for t in leaked):
        from sniper.execution import submission_backends
        submission_backends.close_all()
    deadline = _REAL_MONOTONIC() + 2.0       # a test may have faked time.monotonic for its thread
    for t in _guarded_threads(before):
        t.join(max(0.0, deadline - _REAL_MONOTONIC()))
    left = _guarded_threads(before)
    if left:
        pytest.fail("test left thread(s) running: %s" % ", ".join(sorted(
            "%s (%s)" % (t.name, getattr(getattr(t, "_target", None), "__module__", "?")) for t in left)))


@pytest.fixture(autouse=True)
def _offline_paper_exit_quote(monkeypatch):
    """Paper bracket exits are priced from an EXECUTABLE route quote in production. Unstubbed,
    every exit-rule test would make a live HTTP call and take the noquote path. Price from the
    pool mid the caller already has, which is what those rule assertions were written against.
    Tests that assert the executable/noquote behaviour itself override this explicitly."""
    from sniper.execution.solana_quote import modeled_fill
    from sniper.services import memecoin_exits

    def _mid_quote(mint, network_id, qty, mid=None, liq=None):
        if not mid or mid <= 0:
            return None, "quote_unavailable"
        return modeled_fill(mid, mid * qty, liq, "sell"), "dexscreener"

    monkeypatch.setattr(memecoin_exits, "_exit_quote_fn", _mid_quote, raising=False)


@pytest.fixture(autouse=True)
def _isolated_research_pacer(monkeypatch, tmp_path_factory):
    """The research key's shared pacer state lives per user (~/.cache/sniper/research_pacer): no test
    may read or write the live one that the runtime's research readers share."""
    monkeypatch.setenv("MEMECOIN_RESEARCH_PACER_DIR", str(tmp_path_factory.mktemp("research_pacer")))
