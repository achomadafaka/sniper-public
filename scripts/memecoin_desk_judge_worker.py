#!/usr/bin/env python3
"""Desk JUDGE seat worker (build step 4). Off the fill path: its own process, its own desk DB handle.

The cohort selects its frozen Claude subscription or Codex subscription model. GPT calls use
a strict v26 schema and bypass Claude warm spares; unsupported output caps refuse before dispatch.
The configured deadline is unchanged. Decisions are recorded
through step 3 (sniper.services.memecoin_desk.record_decision) with a short-lived ledger writer.
Default: one bounded pass; --loop is explicit.

    python3 scripts/memecoin_desk_judge_worker.py --desk-db data/memecoin_desk.db \
        --ledger-db sniper_tracker.db --playbook docs/.../2026-10-21-desk-judge-playbook.md [--loop]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import signal
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402

from sniper.persistence import db_memecoin_desk as store              # noqa: E402
from sniper.services import memecoin_desk as desk                     # noqa: E402
from sniper.services import memecoin_desk_judge as J                  # noqa: E402

LATENCY_LOG = ROOT / "data" / "research" / "desk-judge-latency.jsonl"


def pinned_release_sha():
    """Code identity only: own retained, clean pin; funded manifests/configs are separate deploy gates."""
    from scripts.memecoin_runtime_verify import _git
    try:
        code, state = ROOT.resolve(), Path(state_root()).resolve()
        sha = code.name
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or code != state / ".runtime" / "memecoin" / "releases" / sha:
            return None
        marker = (code / ".git").read_text().strip()
        gitdir = state / ".git" / "worktrees" / sha
        if marker != "gitdir: " + str(gitdir) or not (gitdir / "locked").is_file():
            return None
        if (Path((gitdir / "gitdir").read_text().strip()).resolve() != code / ".git"
                or (gitdir / "HEAD").read_text().strip() != sha):
            return None
        for flag, expected in (("--show-toplevel", code), ("--absolute-git-dir", gitdir)):
            rc, actual, _ = _git(code, "rev-parse", flag)
            if rc or not actual or Path(actual).resolve() != expected:
                return None
        rc, head, _ = _git(code, "rev-parse", "HEAD")
        if rc or head != sha:
            return None
        rc, dirty, _ = _git(code, "status", "--porcelain", "--untracked-files=no")
        return sha if not rc and not dirty else None
    except (OSError, UnicodeError, ValueError):
        return None


def load_playbook(path):
    """(system prompt below the '---' line, prompt_version)."""
    text = Path(path).read_text()
    version = re.search(r"prompt_version: `([^`]+)`", text)
    if "\n---\n" not in text or version is None:
        raise ValueError("playbook: needs a prompt_version line and a '---' delimiter")
    return text.split("\n---\n", 1)[1], version.group(1)


AUTH_TEXT = re.compile(r"authenticat|oauth", re.I)
AUTH_PAGE_STATE = None          # None = <state root>/data/desk/desk_sdk_auth_page.json (tests point it elsewhere)


def classify(result_msg, assistant_error, exc):
    """Map one call's outcome to a JUDGE_CALL_ERRORS slug, or None for a usable answer. A dead login is
    auth_expired even when the SDK raises after its error result (2026-10-21: 'OAuth session expired and
    could not be refreshed' came with an Exception, and read as provider_error)."""
    if assistant_error in ("authentication_failed", "oauth_expired") or \
            (exc is not None and AUTH_TEXT.search(str(exc))) or \
            (result_msg is not None and result_msg.is_error and AUTH_TEXT.search(str(result_msg.result or ""))):
        return "auth_expired"
    if exc is not None:
        name = type(exc).__name__
        if isinstance(exc, asyncio.TimeoutError):
            return "timeout"
        if "CLINotFound" in name or "ProcessError" in name or "CLIConnection" in name:
            return "cli_failed"
        return "provider_error"
    if result_msg is None:
        return "provider_error"
    if getattr(result_msg, "api_error_status", None) == 429 or assistant_error == "rate_limit":
        return "rate_limited"
    if getattr(result_msg, "stop_reason", None) == "refusal":
        return "refusal"
    return None            # is_error without a known cause becomes model_result_error in step 3


from sniper.services.memecoin_desk_model_echo import echo_text  # noqa: E402


def answer_budget(environ=None):
    """v23 answer budget (MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS): the JUDGE call's hard output-token cap, hidden reasoning
    included (CLAUDE_CODE_MAX_OUTPUT_TOKENS on the CLI). Unset/empty = no cap (today). A value outside 64..32000 refuses
    startup. A cut answer is malformed, so it is a SKIP like any other malformed answer. Placeholder value until eCat's
    latency number; the cohort freezes it (policy.judge_max_output_tokens) and a mismatch refuses the call."""
    raw = (os.environ if environ is None else environ).get("MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS", "").strip()
    if not raw:
        return None
    if not re.fullmatch(r"[0-9]{1,6}", raw) or not 64 <= int(raw) <= 32000:
        raise ValueError("MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS must be an integer in 64..32000")
    return int(raw)


def _sdk_options(system, *, model, effort, thinking, persist_session, max_output_tokens=None, cli_path=None):
    """The one options build both transports use, so a warm spare and a cold call cannot drift apart."""
    from claude_agent_sdk import ClaudeAgentOptions
    extra = {} if thinking is None else {"thinking": thinking}      # e.g. {"type": "disabled"}: the RISK seat
    if cli_path:                    # the JUDGE seat only passes it (judge_cli_path); every other seat keeps the bundled CLI
        extra["cli_path"] = cli_path
    if max_output_tokens is not None:
        extra["env"] = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(int(max_output_tokens))}
    opts = ClaudeAgentOptions(model=model, tools=[], setting_sources=[], mcp_servers={},
                              strict_mcp_config=True, max_turns=1, effort=effort, system_prompt=system, **extra)
    if not persist_session:  # The CLI's persistence opt-out requires print mode.
        opts.extra_args = {"no-session-persistence": None, "print": None}
    return opts


class WarmSpare:
    """ONE pre-connected, never-used CLI client for one seat (Alpha/operator 2026-10-23, JUDGE latency P2).

    A cold `query()` spawns the CLI on every call (~1 s of wall - duration_ms). The spare is spawned while
    the seat is idle, used for exactly ONE sheet and then disconnected, so no context ever carries between
    calls; the next spare is spawned in the background after each use. Same options builder as the cold
    path: same model, effort, thinking, system prompt and persistence flags, so no new cohort.

    Edge cases: take() never waits -- no ready spare (spawning, failed, closed, or taken by a concurrent
    caller) returns None and the caller makes today's cold call. A spare older than `max_age_s` is
    discarded unused (a long-idle CLI may hold a stale login) and replaced. Idle refresh replaces it
    before expiry; failed preparation retries at most once per 30 s. close() disconnects whatever is held; a
    spawn that finishes after close() disconnects itself. All client I/O runs on the spare's own event
    loop thread, never on the caller's."""

    def __init__(self, options, *, max_age_s=600.0, client_cls=None, clock=time.monotonic):
        import threading
        self.options, self.max_age_s, self._clock = options, float(max_age_s), clock
        if not math.isfinite(self.max_age_s) or self.max_age_s <= 0:
            raise ValueError("warm spare max age must be finite and positive")
        self._client_cls = client_cls
        self._lock = threading.Lock()
        self._client = self._born = None
        self._connect_ms = None
        self._retry_at = 0.0
        self._spawn_failed = False
        self._taken, self._retiring = set(), set()
        self._spawning = self._closed = False
        self.stats = {"spawned": 0, "spawn_failed": 0, "used": 0, "stale": 0, "cold_no_spare": 0, "failed_in_use": 0}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="judge-warm-spare", daemon=True)
        self._thread.start()
        self._spawn()

    def _cls(self):
        if self._client_cls is None:
            from claude_agent_sdk import ClaudeSDKClient
            self._client_cls = ClaudeSDKClient
        return self._client_cls

    def _spawn(self, *, renew=False):
        retired = None
        with self._lock:
            if self._closed or self._spawning or self._clock() < self._retry_at:
                return
            if self._client is not None:
                if not renew or self._clock() - self._born < self.max_age_s - min(30.0, self.max_age_s / 2):
                    return
                retired, self._client = self._client, None
                self._born = None
            self._spawning = True

        async def go():
            client = None
            if retired is not None:
                await self._quiet_disconnect(retired)
            with self._lock:
                closed = self._closed
                if closed:
                    self._spawning = False
            if closed:
                self._stop_if_closed()
                return
            started = time.perf_counter()
            failed = False
            try:
                client = self._cls()(self.options)
                await asyncio.wait_for(client.connect(), timeout=10.0)
            except Exception:                      # noqa: BLE001 - a failed spawn means cold calls, never a crash
                failed = True
                elapsed = round((time.perf_counter() - started) * 1000, 1)
                print("[desk-judge] warm spare spawn failed; cold fallback, retry after 30 s", flush=True)
                if client is not None:
                    await self._quiet_disconnect(client)
                client = None
            else:
                elapsed = round((time.perf_counter() - started) * 1000, 1)
            with self._lock:
                self._spawn_failed, self._connect_ms = failed, elapsed
                if failed:
                    self.stats["spawn_failed"] += 1
                    self._retry_at = self._clock() + 30.0
                keep = client is not None and not self._closed
                if keep:
                    self._client, self._born = client, self._clock()
                    self._spawning = False
                    self.stats["spawned"] += 1
            if client is not None and not keep:
                await self._quiet_disconnect(client)
            if not keep:
                with self._lock:
                    self._spawning = False
            self._stop_if_closed()
        asyncio.run_coroutine_threadsafe(go(), self._loop)

    @staticmethod
    async def _quiet_disconnect(client):
        try:
            await client.disconnect()             # SDK cleanup owns its bounded terminate/kill escalation
        except Exception:                          # noqa: BLE001 - a dead spare has nothing left to close
            pass

    def _retire_locked(self, client):
        future = asyncio.run_coroutine_threadsafe(self._quiet_disconnect(client), self._loop)
        self._retiring.add(future)
        return future

    def _retired(self, future):
        with self._lock:
            self._retiring.discard(future)
        self._stop_if_closed()

    def _discard(self, client):
        with self._lock:
            future = self._retire_locked(client)
        future.add_done_callback(self._retired)     # a completed future invokes its callback immediately

    def _stop_if_closed(self):
        with self._lock:
            if self._closed and not self._spawning and not self._taken and not self._retiring:
                self._loop.call_soon_threadsafe(self._loop.stop)

    def refresh(self):
        """Idle-loop maintenance only; never waits for connection or disconnect."""
        self._spawn(renew=True)

    def take(self, *, timing=None):
        """A ready spare (exclusively the caller's) or None. Never blocks."""
        with self._lock:
            client, born = self._client, self._born
            stale = client is not None and self._clock() - born > self.max_age_s
            reason = ("closed" if self._closed else "stale" if stale else None if client is not None else
                      "spawning" if self._spawning else "spawn_failed" if self._spawn_failed else "empty")
            connect_ms = self._connect_ms if client is not None or reason == "spawn_failed" else None
            if stale:
                self.stats["stale"] += 1
                client = None            # leave the unused client for _spawn/close to retire under the lock
            else:
                self._client = self._born = None
                if client is not None:
                    self._taken.add(client)
            if reason is not None:
                self.stats["cold_no_spare"] += 1
            if timing is not None:
                timing.update(spare_miss_reason=reason, spare_connect_ms=connect_ms)
        self._spawn(renew=stale)
        return client

    def run(self, client, user, collect, timeout_s):
        """Send ONE sheet on `client`, feed every message to `collect`, then disconnect it and spawn the next
        spare. Raises what the call raised (TimeoutError on the deadline)."""
        async def go():
            await client.query(user)
            async for m in client.receive_response():
                collect(m)
        future = asyncio.run_coroutine_threadsafe(go(), self._loop)
        try:
            future.result(timeout=timeout_s)
            self.stats["used"] += 1
        except BaseException:
            future.cancel()
            self.stats["failed_in_use"] += 1
            raise
        finally:
            self._discard(client)
            with self._lock:
                self._taken.discard(client)
            self._spawn()
            self._stop_if_closed()

    def close(self, *, wait=True):
        """Drain at process shutdown; model-switch retirement already runs on its own thread."""
        import threading
        with self._lock:
            self._closed = True
            client, self._client = self._client, None
            # Publish retirement before an older cleanup callback can see a closed, empty owner.
            future = self._retire_locked(client) if client is not None else None
        if future is not None:
            future.add_done_callback(self._retired)
        self._stop_if_closed()                     # preparation, owned calls and cleanup drain off-path
        if wait and threading.current_thread() is not self._thread:
            self._thread.join(timeout=35.0)
            if self._thread.is_alive():
                print("[desk-judge] warm spare shutdown drain timed out; cleanup pending", flush=True)


def spare_for(slot, model, make):
    """The JUDGE warm spare for `model` (Alpha G2): a spare built for another model is closed and replaced, so an Opus
    cohort pays one cold call and then runs warm. slot = {"spare": WarmSpare | None}; None (spare off) stays None."""
    old = None
    if model == J.GPT_MODEL:
        old, slot["spare"] = slot["spare"], None
    elif slot["spare"] is not None and slot["spare"].options.model != model:
        old, slot["spare"] = slot["spare"], make(model)
    if old is not None:
        import threading

        def retire():                   # off the decision path: close() can wait up to 5 s for a disconnect (eCat)
            try:
                old.close()
            except Exception:
                logging.getLogger(__name__).exception("judge warm spare retire failed")
        threading.Thread(target=retire, name="judge-spare-retire", daemon=True).start()
    return slot["spare"]


def initial_spare_model(conn, version, budget, environ=None):
    """Warm the configured compatible U cohort; historical registrations do not establish activity."""
    env = os.environ if environ is None else environ
    cohort_id = (env.get("MEMECOIN_DESK_U_COHORT_ID") or "").strip()
    if cohort_id:
        try:
            cohort = store.get_cohort(conn, cohort_id)
            policy = cohort.get("policy") if isinstance(cohort, dict) else None
            if (isinstance(policy, dict) and cohort.get("producer") == "U" and
                    cohort.get("prompt_version") == version and policy.get("judge_max_output_tokens") == budget):
                return J.judge_model(cohort, environ=env)
        except Exception as exc:       # preparation cannot veto a ticket; the real call retains its validation
            print(json.dumps({"warm_seed_failed": type(exc).__name__}), flush=True)
    return J.MODEL



def judge_response_schema():
    """The v26 seven-key grammar; domain and conditional checks remain in validate_verdict."""
    props = {
        "k": {"type": "string"}, "v": {"type": "string", "enum": ["E", "W", "S"]},
        "e": {"anyOf": [{"type": "null"}, {"type": "array", "minItems": 5, "maxItems": 5,
                                         "items": {"type": ["number", "string", "null"]}}]},
        "r": {"anyOf": [{"type": "null"}, {"type": "array", "minItems": 3, "maxItems": 3,
                                         "items": {"type": ["number", "null"]}}]},
        "c": {"type": "integer", "minimum": 1, "maximum": 16},
        "enter_ttl_s": {"type": "integer"}, "next_check_s": {"type": "integer"},
    }
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def gpt_call_model(system, user, **kwargs):
    from sniper.services.memecoin_desk_gpt import call_model
    return call_model(system, user, **kwargs)


def dispatch_judge_call(system, user, *, cohort, budget, spares, make_spare):
    policy = (cohort or {}).get("policy") or {}
    if policy.get("judge_max_output_tokens") != budget:
        return {"text": None, "is_error": False, "call_error": "answer_budget_mismatch",
                "usage": None, "max_output_tokens": budget}
    try:
        model = J.judge_model(cohort)
    except ValueError:
        return {"text": None, "is_error": False, "call_error": "provider_error",
                "usage": None, "model_requested": None}
    if model == J.GPT_MODEL and budget is not None:
        return {"text": None, "is_error": False, "call_error": "output_budget_unsupported",
                "usage": None, "model_requested": model, "provider": "codex_cli", "no_model_call": True,
                "max_output_tokens": budget}
    spare = spare_for(spares, model, make_spare)
    if model == J.GPT_MODEL:
        from sniper.services.memecoin_desk_contract import judge_deadline_s
        result = gpt_call_model(system, user, deadline_s=judge_deadline_s(), model=model,
                                effort=policy["judge_effort"], schema_obj=judge_response_schema())
    else:
        result = sdk_call_model(system, user, persist_session=False, **({"spare": spare} if spare else {}),
                                **({"max_output_tokens": budget} if budget is not None else {}),
                                **({"model": model} if model != J.MODEL else {}),
                                **({"cli_path": judge_cli_path()} if judge_cli_path() else {}))
    result = {**result, "model_requested": model, "provider": J.judge_provider(model)}
    if budget is not None:
        result["max_output_tokens"] = budget
    return result

def judge_cli_path(environ=None):
    """MEMECOIN_DESK_JUDGE_CLI_PATH for the JUDGE seat (unset = the SDK's bundled CLI, 2.1.209 on 10-23, which cannot run
    Opus 5.5). Read only by the JUDGE call path: RISK, post-mortem and scout seats never see it (Lambda G1)."""
    return ((os.environ if environ is None else environ).get("MEMECOIN_DESK_JUDGE_CLI_PATH") or "").strip() or None


def sdk_call_model(system, user, *, deadline_s=None, model=J.MODEL, effort="low", thinking=None,
                   persist_session=True, spare=None, max_output_tokens=None, cli_path=None):
    """One Agent SDK call; returns the frozen result dict. Never raises. The post-mortem seat reuses it
    with its own model and deadline."""
    from claude_agent_sdk import query, ResultMessage, AssistantMessage
    if deadline_s is None:                                          # the JUDGE seat: the one knob validate_verdict also reads
        from sniper.services.memecoin_desk_contract import judge_deadline_s
        deadline_s = judge_deadline_s()
    opts = _sdk_options(system, model=model, effort=effort, thinking=thinking, persist_session=persist_session,
                        max_output_tokens=max_output_tokens, cli_path=cli_path)
    found = {"result": None, "assistant_error": None, "model_echo": None}

    def collect(m):
        if isinstance(m, AssistantMessage):
            found["model_echo"] = m.model
            found["assistant_error"] = m.error or found["assistant_error"]
        elif isinstance(m, ResultMessage):
            found["result"] = m

    async def run():
        async for m in query(prompt=user, options=opts):
            collect(m)

    exc = None
    transport = "cold"
    spare_timing = {"spare_miss_reason": "disabled" if spare is None else "options_mismatch", "spare_connect_ms": None}
    wall_started = time.perf_counter()
    # A spare built for other options (another system prompt or seat) is never used for this call.
    client = (spare.take(timing=spare_timing) if spare is not None and spare.options.system_prompt == system and spare.options.model == model
              else None)
    if client is not None:
        transport = "warm_spare"
        try:
            spare.run(client, user, collect, deadline_s)
        except BaseException as e:      # noqa: BLE001 - classified below like a cold failure
            if isinstance(e, KeyboardInterrupt):
                raise
            exc = e
            # A spare that died before answering falls back to ONE cold call inside what is left of the
            # deadline; a timeout or a received result does not (a second call would only be later).
            remaining = deadline_s - (time.perf_counter() - wall_started)
            if found["result"] is None and not isinstance(e, (TimeoutError, asyncio.TimeoutError)) and remaining > 1.0:
                transport, exc = "warm_spare_failed_cold", None
                found.update(result=None, assistant_error=None, model_echo=None)
                try:
                    asyncio.run(asyncio.wait_for(run(), timeout=remaining))
                except BaseException as e2:  # noqa: BLE001
                    if isinstance(e2, KeyboardInterrupt):
                        raise
                    exc = e2
    else:
        try:
            asyncio.run(asyncio.wait_for(run(), timeout=deadline_s))
        except BaseException as e:      # noqa: BLE001 - every failure becomes a typed SKIP cause
            if isinstance(e, KeyboardInterrupt):
                raise
            exc = e
    r = found["result"]
    text = r.result if r else None
    echo = found["model_echo"]
    out = {"text": text if isinstance(text, str) else None, "is_error": bool(r.is_error) if r else None,
           "session_id": r.session_id if r else None, "result_id": getattr(r, "uuid", None) if r else None,
           "usage": dict(r.usage or {}) if r else {}, "notional_usd": getattr(r, "total_cost_usd", None) if r else None,
           "model_echo": echo_text(echo),
           "call_error": classify(r, found["assistant_error"], exc),
           "billing_basis": "subscription_flat",       # this adapter only ever runs on the Claude subscription
           # WHERE THE SECONDS GO (Alpha 2026-10-23, JUDGE latency item 1): the CLI's own total and API
           # time beside our wall clock. CLI/API durations are not additive phase timers. Spare connect
           # time measures background preparation, never the cold query's unknown connect phase.
           "timing": {"wall_ms": round((time.perf_counter() - wall_started) * 1000, 1),
                      "duration_ms": getattr(r, "duration_ms", None) if r else None,
                      "duration_api_ms": getattr(r, "duration_api_ms", None) if r else None,
                      "num_turns": getattr(r, "num_turns", None) if r else None,
                      "transport": transport, **spare_timing}}
    # Callers only ever see str|None (Codex/Gamma 2026-10-22). A container text is a typed failure with the
    # receipt (usage, cost) kept; a container echo becomes a marker that echo_ok rejects (None would pass)
    # and its repr rides in that marker (echo_text), so the raw evidence stays in the one column.
    if text is not None and not isinstance(text, str):
        out["is_error"] = True
        out["text_type"] = type(text).__name__
        out["call_error"] = out["call_error"] or "model_result_error"   # a classified cause keeps priority
    if exc is not None:
        out["exception"] = type(exc).__name__
    auth_page(out["call_error"])
    return out


sdk_call_model.real_sdk_adapter = True

def auth_page(call_error, alert=None):
    """One page when the subscription login dies, one when a later call works again, across every desk
    seat that uses this adapter (JUDGE, crawler, scout label, post-mortem, RISK shadow): the state file is
    shared and flock-guarded, so no seat or restart re-pages. Never raises (Alpha, 2026-10-21)."""
    if call_error not in ("auth_expired", None):
        return None                               # other failures say nothing about the login
    try:
        import fcntl
        path = Path(AUTH_PAGE_STATE or Path(state_root()) / "data" / "desk" / "desk_sdk_auth_page.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path) + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                dead = bool(json.loads(path.read_text()).get("dead"))
            except (OSError, ValueError, AttributeError):
                dead = False
            now_dead = call_error == "auth_expired"
            if now_dead == dead:
                return None
            text = ("Claude subscription login looks dead (may be a provider outage that mentions authentication): "
                    "every desk Haiku seat fails until it works; check claude /login" if now_dead
                    else "Claude subscription login works again")
            if alert is None:
                from sniper.services.memecoin_alerts import send_alert
                alert = send_alert
            alert("DESK-SDK-AUTH", text)
            tmp = Path(str(path) + ".tmp")
            tmp.write_text(json.dumps({"dead": now_dead, "at": datetime.now(timezone.utc).isoformat()}))
            tmp.replace(path)
            return text
    except OSError as exc:
        print(json.dumps({"auth_page_state_unwritable": type(exc).__name__}), flush=True)
        return None


def log_latency(result, started, done, prompt_version, billing_basis):
    try:
        LATENCY_LOG.parent.mkdir(parents=True, exist_ok=True)
        u = result.get("usage") or {}
        with open(LATENCY_LOG, "a") as fh:
            fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "source": "desk_judge_worker",
                                 "model": result.get("model_requested") or J.MODEL, "provider": result.get("provider", "claude_sdk"),
                                 "prompt_version": prompt_version,
                                 "wall_ms": round((done - started) * 1000, 1), "call_error": result.get("call_error"),
                                 "out_tok": u.get("output_tokens"), "cache_read_tok": u.get("cache_read_input_tokens"),
                                 "notional_api_equivalent_usd": result.get("notional_usd"),
                                 "billing_basis": billing_basis}, sort_keys=True) + "\n")
    except OSError:
        pass


QUIET = ("pending", "unresolved")          # re-reported every pass until they move: not logged


def standing_pass(desk_conn, fires_dir, now_iso, cursor_path):
    """Watch v22: freeze each new fire request (standing_fires/<UTC day>.jsonl, appended by the universe worker) through
    desk.record_fire, which re-checks it, then expire waiting tickets. The courier queues the frozen fires in the same
    pass. A refused fire is logged by name, never retried; the cursor survives restarts (a fire is consumed once).
    At a UTC day change the previous day's file is drained from its cursor first (Codex: a pre-midnight fire is not
    dropped; an expired one is refused by record_fire, never queued)."""
    out = []
    day = now_iso[:10]
    try:
        cur = json.loads(Path(cursor_path).read_text())
    except (OSError, ValueError):
        cur = {}
    if cur.get("day") and cur["day"] != day:
        _drain(desk_conn, Path(fires_dir) / f"{cur['day']}.jsonl", cur.get("offset", 0), now_iso, out)
        cur = {}
    offset = _drain(desk_conn, Path(fires_dir) / f"{day}.jsonl", cur.get("offset", 0) if cur.get("day") == day else 0,
                    now_iso, out)
    Path(cursor_path).write_text(json.dumps({"day": day, "offset": offset}))
    for ticket_id in desk.expire_watches(desk_conn, now_iso=now_iso):
        out.append({"step": "expire_watch", "ticket_id": ticket_id, "state": "expired"})
    out += [dict(r, step="order_end") for r in desk.record_order_ends(desk_conn, now_iso=now_iso)]   # prereg section 4
    return out


def _drain(desk_conn, path, offset, now_iso, out):
    """Freeze every complete line of `path` after `offset`; returns the new offset."""
    if not path.exists():
        return offset
    with open(path) as fh:
        fh.seek(offset)
        for line in fh:
            if not line.endswith("\n"):
                break                                  # a partial last line is read next pass
            offset += len(line.encode())
            row = None
            try:
                row = json.loads(line)
                if "relook" in row:                    # watch-list-v1 change trigger (re-checked by the writer)
                    desk.record_relook(desk_conn, row["ticket_id"], row["relook"], now_iso=now_iso)
                    out.append({"step": "watch_relook", "ticket_id": row["ticket_id"], "state": row["relook"]["trigger"]})
                    continue
                if "cancel" in row:                    # standing-v2 band exit (re-checked by the writer)
                    desk.record_cancel(desk_conn, row["ticket_id"], row["cancel"], now_iso=now_iso)
                    out.append({"step": "standing_cancel", "ticket_id": row["ticket_id"], "state": row["cancel"]["reason"]})
                    continue
                desk.record_fire(desk_conn, row["ticket_id"], row["fire"], now_iso=now_iso)
                out.append({"step": "standing_fire", "ticket_id": row["ticket_id"], "state": "fired"})
            except (ValueError, KeyError, TypeError) as exc:
                out.append({"step": "standing_fire", "ticket_id": row.get("ticket_id") if isinstance(row, dict) else None,
                            "refused": str(exc)})
    return offset


def start_dump_cache(environ=None):
    """Codex's dump-group snapshot cache (MEMECOIN_DESK_DUMP_GROUP_BLOCK, default 1), started once. None when the knob is
    off or the module is not deployed yet (then the courier behaves exactly as before)."""
    env = os.environ if environ is None else environ
    if (env.get("MEMECOIN_DESK_DUMP_GROUP_BLOCK", "1") or "1").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    try:
        from sniper.services.memecoin_desk_dump_group_cache import SnapshotCache
    except ImportError:
        # the courier then records a typed dump_group_unknown (cache_absent) fact on every ticket it routes
        logging.getLogger(__name__).warning("desk dump-group cache module absent: every routed ticket logs dump_group_unknown")
        return None
    from sniper.util.state_paths import state_root
    return SnapshotCache(state_root() / "data" / "desk" / "dump_group" / "cache.json", enabled=True).start()


def courier_pass(desk_conn, ledger_writer, now_iso, dump_cache=None):
    """Hand recorded ENTERs to the intent queue (dispatch_outbox) and append terminal intent and close
    truth (reconcile_intents). Both are idempotent and never decide; they run after each JUDGE pass,
    under the worker's flock, off every fill path (Codex FAIL on 72cf3bb84: no production caller).
    The ledger is opened only when the desk DB holds work for them."""
    if not (store.list_undispatched_decisions(desk_conn, limit=1) or store.list_outbox(desk_conn, limit=1)
            or store.list_intents(desk_conn, limit=1) or store.list_filled_tickets(desk_conn, limit=1)):
        return []
    out = []
    with ledger_writer() as ledger_conn:
        for step in (desk.dispatch_outbox, desk.reconcile_intents):
            try:
                kw = {"dump_cache": dump_cache} if step is desk.dispatch_outbox else {}
                out += [dict(r, step=step.__name__) for r in step(desk_conn, ledger_conn, now_iso=now_iso, **kw)]
            except ValueError as exc:              # intent_conflict / chronology: named, next pass retries
                out.append({"step": step.__name__, "refused": str(exc), "ticket_id": getattr(exc, "ticket_id", None)})
    return out


def page_refusals(out, state_path, alert, memo=None):
    """One page when the courier starts refusing a step, one when the set changes, one on recovery
    (Gamma MED on a23a5d93e: an intent_conflict stops every later ENTER and was only a printed line).
    The paged set is kept on disk, so a worker restart never re-pages (trap b6c22073), and in `memo`,
    so a state file that cannot be written neither kills the worker nor re-pages every pass.
    ponytail: saved when the page is handed off, not on delivery; a lost page is not retried."""
    memo = {} if memo is None else memo
    now = sorted({"%s:%s:%s" % (r["step"], r["refused"], r.get("ticket_id")) for r in out if r.get("refused")})
    if "refused" in memo:
        before = memo["refused"]
    else:
        try:
            before = json.loads(Path(state_path).read_text()).get("refused", [])
        except (OSError, ValueError, AttributeError):
            before = []
    if now == before:
        return None
    text = ("courier blocked: %s; ENTERs after the refused row are not dispatched" % ", ".join(now) if now
            else "courier recovered (was %s)" % ", ".join(before))
    alert("DESK-COURIER", text)
    memo["refused"] = now
    try:
        tmp = Path(str(state_path) + ".tmp")
        tmp.write_text(json.dumps({"refused": now, "at": datetime.now(timezone.utc).isoformat()}))
        tmp.replace(state_path)
    except OSError as exc:                     # a restart may re-page this one; the worker keeps running
        print(json.dumps({"page_state_unwritable": type(exc).__name__}), flush=True)
    return text


def _paged_state(state_path, memo):
    if "paged" not in memo:
        try:
            memo["paged"] = set(json.loads(Path(state_path).read_text()).get("paged", []))
        except (OSError, ValueError, AttributeError, TypeError):
            memo["paged"] = set()
    return memo["paged"]


def _save_paged(state_path, memo):
    try:
        tmp = Path(str(state_path) + ".tmp")
        tmp.write_text(json.dumps({"paged": sorted(memo["paged"]), "at": datetime.now(timezone.utc).isoformat()}))
        tmp.replace(state_path)
    except OSError as exc:                     # a restart may re-page; the worker keeps running
        print(json.dumps({"hold_page_state_unwritable": type(exc).__name__}), flush=True)


def _gate_text(h):
    return ("cohort %s, unknown %s, flat_model %s, known net %s, bounded net %s, bound %s/leg"
            % (h["cohort_id"][-8:], h["unknown_by_kind"], h["flat_model"], h["known_net_usd"],
               h["bounded_net_usd"], h["bound_usd"]))


def note_holds(out, state_path, alert, memo):
    """Cost-gate holds (operator yes 2026-10-22): ONE log line per held ticket (its first held pass) and ONE page
    on the first hold per cohort. Paged keys live on disk (a restart never re-pages, trap b6c22073) and in `memo`.
    Returns the lines logged."""
    holds = [r for r in out if r.get("reason") == "cohort_accounting_unknown" and r.get("hold")]
    if not holds:
        return []
    paged, logged, lines, fresh = _paged_state(state_path, memo), memo.setdefault("logged", set()), [], False
    for row in holds:
        h = row["hold"]
        if row["ticket_id"] not in logged:
            logged.add(row["ticket_id"])
            line = {"desk_hold": row["ticket_id"], **h}
            print(json.dumps(line, sort_keys=True), flush=True)
            lines.append(line)
        key = "first:" + h["cohort_id"]
        if key not in paged:
            paged.add(key)
            fresh = True
            alert("DESK-COST-GATE", "ENTER held: " + _gate_text(h))
    if fresh:
        _save_paged(state_path, memo)
    return lines


def note_aged(gates, state_path, alert, memo, *, now_epoch, page_after_s):
    """ONE page per cohort and oldest unknown once that unknown is older than `page_after_s`, from the cohorts'
    own unknown rows, whether or not any ENTER is held (Codex P2 on 3f19c5853). Returns the keys paged."""
    paged, fresh = _paged_state(state_path, memo), []
    for g in gates:
        oldest = g.get("oldest_unknown_at")
        if not oldest:
            continue
        try:
            age = now_epoch - datetime.fromisoformat(oldest).timestamp()
        except ValueError:
            continue
        key = "aged:%s:%s" % (g["cohort_id"], oldest)
        if age >= page_after_s and key not in paged:
            paged.add(key)
            fresh.append(key)
            alert("DESK-COST-GATE", "unknown cost unpriced > %d min: %s" % (page_after_s // 60, _gate_text(g)))
    if fresh:
        _save_paged(state_path, memo)
    return fresh


from sniper.services.memecoin_desk_contract import CLOCK_MAX_AGE_S   # noqa: E402  (hash-bound in D_SHEET_DEFINITION)


def make_ps_inputs_fn(desk_conn, desk_dir, fill_sigs_of):
    """New-cohort local facts/probe from one refreshed claim; no provider or fill-path work."""
    from decimal import Decimal, ROUND_FLOOR
    from sniper.services import memecoin_desk_contract as K
    from sniper.services import memecoin_desk_ps_facts as F
    from sniper.services import memecoin_desk_ps_evidence as E
    from sniper.services import memecoin_desk_ps_tape as PT

    def inputs(ticket, candidate, meta, ts):
        cohort = store.get_cohort(desk_conn, ticket["cohort_id"])
        policy, original = cohort["policy"], ticket["candidate"]
        K.validate_ps_inputs(policy, cohort["prompt_version"])
        hard = policy["hard_filter"]
        source = store.get_intake_source_receipt(desk_conn, ticket["opportunity_id"], cohort_id=ticket["cohort_id"],
            hard_policy=hard, candidate=original, as_of=ts)
        if source is None:
            raise ValueError("ps_source_receipt_unknown")
        filtered = next((s for s in ticket["stages"] if s["stage"] == "hard_filter"), None)
        evidence = ((filtered or {}).get("inputs") or {}).get("evidence")
        try:
            safety = K.safety_from_evidence(original, evidence, policy=hard, as_of=ts)
            known = K._utc(evidence["known_at"])
        except (ValueError, TypeError, KeyError):
            raise ValueError("ps_safety_proof_unknown") from None
        try:
            state = candidate["snapshot"]["fields"]["pool_state"]["value"]
            spend = int((Decimal(str(policy["clip_usd"])) * Decimal(10**9) / Decimal(str(state["sol_usd"])))
                        .to_integral_value(rounding=ROUND_FLOOR))
            model = E.round_trip(base=int(state["base_reserve_atomic"]), real_quote=int(state["quote_reserve_atomic"]),
                virtual_quote=int(state["virtual_quote_atomic"]),
                fees=tuple(state[k] for k in ("lp_fee_bps", "protocol_fee_bps", "creator_fee_bps")) + (0,), spend=spend)
            loss, limit = model["loss_pct"], hard["round_trip_loss_pct_max"]
            if not K._number(loss, minimum=0) or not K._number(limit, minimum=0):
                raise ValueError
        except (ValueError, TypeError, KeyError, ArithmeticError):
            raise ValueError("ps_probe_unavailable") from None
        if loss > limit:
            raise ValueError("ps_probe_kill")
        try:
            sigs = tuple(fill_sigs_of(candidate["mint"]))
            if len(sigs) > F.MAX_ROWS:
                raise ValueError
            ours = PT.ps_actors_of_signatures(desk_dir, sigs)
        except Exception:
            raise ValueError("ps_ours_unknown") from None
        facts = F.facts_for(candidate, desk_dir=desk_dir, as_of=ts, receipt_cut=meta["receipt_cut"],
            cut_slot=meta["cut_slot"], source_metadata=source.get("source_metadata"), ours=frozenset(ours),
            ours_sigs=frozenset(sigs), bounds=policy["rail_bounds"], version=policy["ps_inputs"])
        safety["sell_probe"] = "ok"
        flow = facts["flow_60s"]
        sells = flow["sell_n"] > 0 if flow is not None else None
        if policy["ps_inputs"] == "pumpswap-facts-v2" and sells is False and flow.get("partial") is not False:
            sells = None                             # zero seen in a partial window is not proof of no sells
        safety["sells_exist_60s"] = sells
        proof = {key: original[key] for key in ("network_id", "mint", "pool")}
        proof.update(emission_snapshot_hash=original["snapshot"]["hash"], known_at=known,
                     probe_receipt_cut=meta["receipt_cut"], probe_cut_slot=meta["cut_slot"])
        out = {"ps_facts": facts, "safety": safety, "safety_proof": proof}
        if policy["ps_inputs"] == F.VERSION3:
            out["d_blocks"] = d_blocks(facts, candidate, ours=frozenset(ours),
                                       pass_persist_at=K._utc(filtered["ts"]) if filtered else None, decision_at=ts)
        return out

    def d_blocks(facts, candidate, *, ours, pass_persist_at, decision_at):
        """The worker's D blocks: reads the buyer ledger and one clock sample, then build_d_blocks."""
        from sniper.persistence.db_core import get_readonly_connection
        from sniper.persistence.db_memecoin import desk_buyer_ledger
        if pass_persist_at is None:
            raise ValueError("ps_pass_persist_unknown")
        try:
            conn = get_readonly_connection()
            try:
                ledger = desk_buyer_ledger(conn)
            finally:
                conn.close()
        except Exception as exc:          # buyers_for maps an Exception to unknown:ledger_unreadable
            ledger = exc
        return build_d_blocks(candidate, facts, desk_dir=desk_dir, ours=ours, pass_persist_at=pass_persist_at,
                              decision_at=decision_at, clock=clock_sample(), ledger=ledger)

    def clock_sample():
        return read_clock_sample(Path(state_root()) / "data" / "clock_offset.json", now=time.time())
    return inputs


from sniper.services.memecoin_desk_intake import clock_sample as read_clock_sample   # noqa: E402,F401
from sniper.services.memecoin_desk_d_blocks import build_d_blocks, context_blocks, history_blocks  # noqa: E402,F401


def main(argv=None, *, env_root=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    # cron has no environment: the knobs come from the state root's .env (Gamma FAIL b), loaded here and
    # not at import so a test import never pulls the real .env. Existing variables win. An API key or
    # base URL never reaches the SDK, even when an env file carries one: the seat runs on the subscription.
    load_env_files(str(env_root or state_root()))
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        os.environ.pop(key, None)
    from sniper.services.memecoin_desk_contract import judge_deadline_s, judge_defer_max_s
    try:
        print("[desk-judge] call deadline %.1f s (MEMECOIN_DESK_JUDGE_DEADLINE_S)" % judge_deadline_s(), flush=True)
        print("[desk-judge] defer ceiling %.0f s for new cohorts (MEMECOIN_DESK_JUDGE_DEFER_MAX_S)" % judge_defer_max_s(),
              flush=True)
    except ValueError as exc:                                # a bad knob stops the worker at start, never a silent default
        raise SystemExit(str(exc))
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--desk-db", required=True)
    p.add_argument("--ledger-db", required=True)
    p.add_argument("--playbook", required=True)
    p.add_argument("--worker", default="judge-%d" % os.getpid())
    p.add_argument("--calls-per-hour", type=int,
                   default=int(os.getenv("MEMECOIN_DESK_SEAT_CALLS_PER_HOUR", "0") or 0))
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval-s", type=float, default=0.5)
    a = p.parse_args(argv)
    if os.getenv("MEMECOIN_DESK_JUDGE_ENABLED", "0").strip() != "1":
        print("[desk-judge] MEMECOIN_DESK_JUDGE_ENABLED is not 1; exiting")
        return 0
    if not Path(a.ledger_db).is_file():
        p.error("--ledger-db must exist")
    if a.calls_per_hour <= 0:
        p.error("--calls-per-hour (MEMECOIN_DESK_SEAT_CALLS_PER_HOUR) must be set > 0")
    system, version = load_playbook(a.playbook)
    from sniper.persistence.db_core import get_connection

    @contextmanager
    def ledger_writer():
        conn = get_connection(a.ledger_db)
        try:
            yield conn
        finally:
            conn.close()

    budget = answer_budget()                         # v23: refuses startup on a malformed value
    # P2 warm spare (operator yes 2026-10-23). MEMECOIN_DESK_JUDGE_WARM_SPARE=0 = today's cold call per sheet.
    # Only for the real SDK adapter: a stubbed sdk_call_model (tests, a replay) never spawns a CLI process.
    spares = {"spare": None}
    spare_on = (os.getenv("MEMECOIN_DESK_JUDGE_WARM_SPARE", "1").strip() != "0"
                and getattr(sdk_call_model, "real_sdk_adapter", False))

    def make_spare(model):
        return WarmSpare(_sdk_options(system, model=model, effort="low", thinking=None, persist_session=False,
                                      max_output_tokens=budget, cli_path=judge_cli_path()),
                         max_age_s=float(os.getenv("MEMECOIN_DESK_JUDGE_WARM_SPARE_MAX_AGE_S", "600")))
    def call_model(sys_prompt, user, cohort=None):
        t0 = time.monotonic()
        res = dispatch_judge_call(sys_prompt, user, cohort=cohort, budget=budget,
                                  spares=spares, make_spare=make_spare)
        basis = ((cohort or {}).get("policy") or {}).get("model_billing_basis", "unknown")
        log_latency(res, t0, time.monotonic(), version, basis)
        return res

    page_memo, hold_memo = {}, {}
    import sniper.settings.memecoin as cfg_m
    from sniper.persistence import db_memecoin as ledger_m

    def page(kind, text):
        from sniper.services.memecoin_alerts import send_alert
        send_alert(kind, text)

    import fcntl
    lock = open(Path(a.desk_db).with_name("desk_judge_worker.lock"), "w")
    try:                                              # one worker: a second start exits, never races recover()
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-judge] another worker holds the lock; exiting")
        return 0
    desk_conn = store.connect(a.desk_db)
    release_sha = pinned_release_sha()              # startup only; never a Git call per sheet or model response
    if spare_on:
        seed_model = initial_spare_model(desk_conn, version, budget)
        if seed_model != J.GPT_MODEL:
            spares["spare"] = make_spare(seed_model)
            import atexit
            atexit.register(lambda: spares["spare"] and spares["spare"].close())
    now_fn = lambda: datetime.now(timezone.utc)
    shortlist = J.ranker_gate(Path(state_root()) / "data" / "desk")   # cohort U: runners shortlist only
    asof_fn = None
    if os.getenv("MEMECOIN_DESK_JUDGE_ASOF", "1").strip() != "0":     # default ON; 0 = emission facts (rollback)
        from sniper.services import memecoin_desk_asof as asof_mod
        desk_dir = Path(state_root()) / "data" / "desk"
        asof_fn = lambda candidate, ts, intake_mint=None, integrity=False: asof_mod.refresh(candidate, desk_dir=desk_dir, now=ts,
                                                                         intake_mint=intake_mint, integrity=integrity)
    # a trigger cohort (select_version desk-judge-select-v2) scores and band-checks its ticket inline from this snapshot at the
    # claim: the same tape read as asof_fn plus the u-runners-v1 features of the same window. Independent of the ASOF rollback knob
    # (a trigger cohort without a snapshot refuses, it never falls back to emission facts).
    from sniper.services import memecoin_desk_asof as rank_asof_mod
    rank_desk_dir = Path(state_root()) / "data" / "desk"
    rank_asof_fn = lambda candidate, ts: rank_asof_mod.refresh(candidate, desk_dir=rank_desk_dir, now=ts, rank_inputs=True)
    from sniper.services import memecoin_desk_soft_facts as soft_mod
    soft_dir = Path(state_root()) / "data" / "desk"
    soft_fn = lambda ticket: soft_mod.for_ticket(ticket, desk_dir=soft_dir, now=time.time(),
                                                 cohort_fn=lambda t: store.get_cohort(desk_conn, t["cohort_id"]))
    structure_fn = None
    def fill_sigs_of(mint):                   # OUR fills, read-only and off the funded filling path
        from sniper.persistence.db_core import get_readonly_connection
        from sniper.persistence.db_memecoin import funded_fill_signatures
        conn = get_readonly_connection(str(a.ledger_db))
        try:
            return funded_fill_signatures(conn, mint)
        finally:
            conn.close()
    ps_inputs_fn = make_ps_inputs_fn(desk_conn, soft_dir, fill_sigs_of)
    if os.getenv("MEMECOIN_DESK_JUDGE_STRUCTURE", "1").strip() != "0":   # hf4 kill knob: 0 = no chart block (rollback)
        from sniper.services import memecoin_desk_structure_tape as structure_mod
        structure_fn = structure_mod.judge_structure_fn(soft_dir, lambda ticket: store.get_cohort(desk_conn, ticket["cohort_id"]),
                                                        fill_sigs_of,
                                                        close_of=lambda pid: store.close_of_position(desk_conn, pid))
    context_fn = None
    if os.getenv("MEMECOIN_DESK_JUDGE_CONTEXT", "1").strip() != "0":     # kill knob: 0 = no ctx block (rollback)
        from sniper.services import memecoin_desk_context_facts as context_mod
        context_fn = context_mod.judge_context_fn(soft_dir, lambda ticket: store.get_cohort(desk_conn, ticket["cohort_id"]))
    dump_cache = start_dump_cache()          # once, in the background; lookups at the courier have zero I/O
    stopping = {"flag": False}
    # SIGTERM drain (operator 2026-10-23: a deploy cut a JUDGE call and the cost gate held the cohort): claim nothing
    # new, let the in-flight call finish inside its own deadline and be recorded, then exit. The watchdog waits for it.
    signal.signal(signal.SIGTERM, lambda *_: stopping.update(flag=True))
    try:
        J.recover(desk_conn, ledger_writer, now_fn=now_fn)       # under the flock: every bare claim is a dead worker's
        for call_id in J.close_orphan_claims(desk_conn):             # a lost claim an older release left open
            print(json.dumps({"closed_orphan_claim": call_id}), flush=True)
        while True:
            try:
                out = J.run_once(desk_conn, ledger_writer, call_model=call_model, system_prompt=system,
                                 worker=a.worker, now_fn=now_fn, calls_per_hour=a.calls_per_hour,
                                 shortlist_fn=shortlist, asof_fn=asof_fn, soft_fn=soft_fn, structure_fn=structure_fn,
                                 context_fn=context_fn, expected_prompt_version=version,
                                 rank_asof_fn=rank_asof_fn, stop_fn=lambda: stopping["flag"], ps_inputs_fn=ps_inputs_fn,
                                 release_sha=release_sha)
            except ValueError as exc:                  # a refused proof: marker stands, next pass continues
                print(json.dumps({"refused": str(exc)}), flush=True)
                out = []
            for row in out:
                print(json.dumps({"ticket_id": row["ticket_id"], "action": row.get("action"),
                                  "reason": row.get("reason")}), flush=True)
            standing = standing_pass(desk_conn, Path(a.desk_db).parent / "desk" / "standing_fires", now_fn().isoformat(),
                                     Path(a.desk_db).with_name("desk_standing_fires.cursor"))
            for row in standing:
                print(json.dumps(row), flush=True)
            couriered = courier_pass(desk_conn, ledger_writer, now_fn().isoformat(), dump_cache=dump_cache)
            for row in couriered:
                if row.get("state") not in QUIET:
                    print(json.dumps({k: row.get(k) for k in ("step", "ticket_id", "state", "reason", "refused")}),
                          flush=True)
            page_refusals(couriered, Path(a.desk_db).with_name("desk_courier_page.json"), page, page_memo)
            hold_state = Path(a.desk_db).with_name("desk_hold_page.json")
            note_holds(couriered, hold_state, page, hold_memo)
            if time.monotonic() >= hold_memo.get("aged_next", 0.0):   # once a minute, held ENTERs or not
                hold_memo["aged_next"] = time.monotonic() + 60.0
                try:
                    with ledger_writer() as lc:
                        gates = ledger_m.desk_cohort_gates(lc)
                    note_aged(gates, hold_state, page, hold_memo, now_epoch=now_fn().timestamp(),
                              page_after_s=float(cfg_m.MEMECOIN_DESK_UNKNOWN_COST_PAGE_S))
                except Exception as exc:              # a page check never stops the worker; named
                    print(json.dumps({"aged_page_failed": type(exc).__name__}), flush=True)
            if not a.loop or stopping["flag"]:
                return 0
            if spares["spare"] is not None:
                spares["spare"].refresh()
            time.sleep(a.interval_s)
    except KeyboardInterrupt:
        return 0
    finally:
        desk_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
