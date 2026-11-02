import asyncio
import inspect
import importlib
import sys
import types


def _load_relay(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_AI", "test-token")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "123")
    monkeypatch.setenv("BOT_WORKING_DIR", "/tmp/sniper-bot-test")
    if "telegram" not in sys.modules:
        telegram_mod = types.ModuleType("telegram")
        telegram_mod.Update = type("Update", (), {})
        sys.modules["telegram"] = telegram_mod

        constants_mod = types.ModuleType("telegram.constants")
        constants_mod.ChatAction = type(
            "ChatAction",
            (),
            {"TYPING": "typing", "UPLOAD_PHOTO": "upload_photo"},
        )
        sys.modules["telegram.constants"] = constants_mod

        ext_mod = types.ModuleType("telegram.ext")
        ext_mod.ApplicationBuilder = type("ApplicationBuilder", (), {})
        ext_mod.CommandHandler = type("CommandHandler", (), {})
        ext_mod.MessageHandler = type("MessageHandler", (), {})
        ext_mod.ContextTypes = type("ContextTypes", (), {"DEFAULT_TYPE": object})
        ext_mod.filters = type("filters", (), {"TEXT": object(), "COMMAND": object(), "PHOTO": object(), "Document": type("Document", (), {"IMAGE": object()})})
        sys.modules["telegram.ext"] = ext_mod

    if "claude_agent_sdk._errors" not in sys.modules:
        errors_mod = types.ModuleType("claude_agent_sdk._errors")

        class ProcessError(Exception):
            def __init__(self, message, *, exit_code=None, stderr=None):
                super().__init__(message)
                self.exit_code = exit_code
                self.stderr = stderr

        errors_mod.ProcessError = ProcessError
        sys.modules["claude_agent_sdk._errors"] = errors_mod
    sys.modules.pop("bot.relay", None)
    import bot.relay as relay

    return importlib.reload(relay)


def test_query_claude_clears_poisoned_session_and_retries_fresh(monkeypatch):
    relay = _load_relay(monkeypatch)
    ProcessError = sys.modules["claude_agent_sdk._errors"].ProcessError
    relay._sessions[123] = "bad-session"
    calls = []

    async def fake_run_query(prompt, model, chat_id, bot, user_id):
        calls.append((model, relay._sessions.get(user_id)))
        if len(calls) == 1:
            raise ProcessError("Command failed with exit code 1", exit_code=1, stderr="boom")
        return "ok"

    monkeypatch.setattr(relay, "_run_query", fake_run_query)

    result = asyncio.run(relay.query_claude("hello", 123, 456, None))

    assert result == "ok"
    assert calls == [
        (relay.PRIMARY_MODEL, "bad-session"),
        (relay.PRIMARY_MODEL, None),
    ]


def test_query_claude_without_saved_session_does_not_retry_process_error(monkeypatch):
    relay = _load_relay(monkeypatch)
    ProcessError = sys.modules["claude_agent_sdk._errors"].ProcessError
    calls = []

    async def fake_run_query(prompt, model, chat_id, bot, user_id):
        calls.append((model, relay._sessions.get(user_id)))
        raise ProcessError("Command failed with exit code 1", exit_code=1, stderr="boom")

    monkeypatch.setattr(relay, "_run_query", fake_run_query)

    result = asyncio.run(relay.query_claude("hello", 123, 456, None))

    assert result.startswith("Error: Command failed with exit code 1")
    assert calls == [(relay.PRIMARY_MODEL, None)]


def test_hook_stop_spawns_background_jobs_without_blocking(monkeypatch):
    relay = _load_relay(monkeypatch)
    spawned = []

    def fake_spawn(cmd, *, log_name=None):
        spawned.append((cmd, log_name))

    monkeypatch.setattr(relay, "_spawn_detached", fake_spawn)

    # Call it the way the SDK does: async, three positional args.
    asyncio.run(relay._hook_stop({"hook_event_name": "Stop"}, None, {"signal": None}))

    # The interpreter is host-dependent (BOT_PYTHON, set in .env) — assert the resolved
    # value, not a hardcoded path, or this pins the empty .venv that some hosts have.
    py = relay.BOT_PYTHON
    assert spawned == [
        (
            [py, "scripts/checkpoint.py", "--quiet", "--reason", "session-stop"],
            "checkpoint.log",
        ),
        (
            [py, "scripts/world_analyst.py", "--quick", "--quiet", "--domains", "geo,politics,finance"],
            "world_analyst.log",
        ),
    ]


def test_hooks_match_sdk_callback_contract(monkeypatch):
    """The SDK invokes hooks as `await cb(input, tool_use_id, context)`.

    Regression: both hooks were sync single-arg functions, so every invocation died with
    "takes 1 positional argument but 3 were given" and neither hook ever ran. The old test
    called _hook_stop(None) directly, so it stayed green while production was broken.
    """
    relay = _load_relay(monkeypatch)

    for hook in (relay._hook_stop, relay._hook_pre_compact):
        assert inspect.iscoroutinefunction(hook), f"{hook.__name__} must be async"
        params = list(inspect.signature(hook).parameters.values())
        positional = [
            p for p in params
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        assert len(positional) == 3, (
            f"{hook.__name__} must accept (input, tool_use_id, context); got {len(positional)}"
        )


def test_sessions_survive_a_restart(tmp_path, monkeypatch):
    """Sessions were in-memory only, so every restart silently wiped the agent's memory
    of the conversation. They must round-trip through disk."""
    relay = _load_relay(monkeypatch)
    monkeypatch.setattr(relay, "_SESSIONS_FILE", tmp_path / "bot_sessions.json")

    relay._set_session(123, "sess-abc")
    relay._set_session(456, "sess-def")

    # Simulate a restart: wipe memory, reload from disk.
    relay._sessions.clear()
    relay._load_sessions()
    assert relay._sessions == {123: "sess-abc", 456: "sess-def"}

    # /reset must clear from disk too, or the next boot resurrects a dead session.
    assert relay._clear_session(123) == "sess-abc"
    relay._sessions.clear()
    relay._load_sessions()
    assert relay._sessions == {456: "sess-def"}


def test_load_sessions_tolerates_missing_file(tmp_path, monkeypatch):
    relay = _load_relay(monkeypatch)
    monkeypatch.setattr(relay, "_SESSIONS_FILE", tmp_path / "nope.json")
    relay._sessions.clear()
    relay._load_sessions()  # must not raise
    assert relay._sessions == {}


def test_hook_pre_compact_reads_trigger_from_dict(monkeypatch):
    """HookInput is a TypedDict — getattr() on it always silently fell back to 'auto'."""
    relay = _load_relay(monkeypatch)
    seen = []
    monkeypatch.setattr(relay, "_pre_compact_blocking", lambda trigger: seen.append(trigger))

    asyncio.run(relay._hook_pre_compact({"trigger": "manual"}, None, {"signal": None}))

    assert seen == ["manual"]


# --- New: model routing / per-user pin / /hard escalation ----------------------------


def test_resolve_model_defaults_to_primary_and_honors_pin(monkeypatch):
    relay = _load_relay(monkeypatch)
    assert relay._resolve_model(123) == relay.PRIMARY_MODEL
    relay._set_user_model(123, "claude-opus-4-8")
    assert relay._resolve_model(123) == "claude-opus-4-8"
    # Other users unaffected.
    assert relay._resolve_model(456) == relay.PRIMARY_MODEL
    # Clearing the pin returns to default.
    relay._set_user_model(123, None)
    assert relay._resolve_model(123) == relay.PRIMARY_MODEL


def test_user_models_survive_a_restart(tmp_path, monkeypatch):
    """A pinned model must round-trip through disk alongside the session id, or /model
    silently reverts on every relay restart."""
    relay = _load_relay(monkeypatch)
    monkeypatch.setattr(relay, "_SESSIONS_FILE", tmp_path / "bot_sessions.json")

    relay._set_session(123, "sess-abc")
    relay._set_user_model(123, "claude-opus-4-8")
    relay._set_user_model(456, "claude-haiku-4-5-20251001")  # model-only, no session

    relay._sessions.clear()
    relay._user_models.clear()
    relay._load_sessions()

    assert relay._sessions == {123: "sess-abc"}
    assert relay._user_models == {123: "claude-opus-4-8",
                                  456: "claude-haiku-4-5-20251001"}


def test_load_sessions_accepts_legacy_string_format(tmp_path, monkeypatch):
    """Old bot_sessions.json wrote `{"123": "sess-abc"}`. Existing installs must not
    lose their session on the first boot after this refactor."""
    import json
    relay = _load_relay(monkeypatch)
    f = tmp_path / "bot_sessions.json"
    f.write_text(json.dumps({"123": "legacy-sess"}))
    monkeypatch.setattr(relay, "_SESSIONS_FILE", f)
    relay._sessions.clear()
    relay._user_models.clear()

    relay._load_sessions()
    assert relay._sessions == {123: "legacy-sess"}
    assert relay._user_models == {}


def test_query_claude_uses_user_pin_over_primary(monkeypatch):
    """A pinned model must beat PRIMARY_MODEL; without a pin the default still wins."""
    relay = _load_relay(monkeypatch)
    seen = []

    async def fake_run_query(prompt, model, chat_id, bot, user_id):
        seen.append(model)
        return "ok"

    monkeypatch.setattr(relay, "_run_query", fake_run_query)
    relay._set_user_model(123, "claude-opus-4-8")

    asyncio.run(relay.query_claude("hi", 123, 456, None))
    asyncio.run(relay.query_claude("hi", 999, 456, None))  # no pin

    assert seen == ["claude-opus-4-8", relay.PRIMARY_MODEL]


def test_query_claude_model_override_wins_over_pin(monkeypatch):
    """/hard <msg> must run on the escalation model even if the user has pinned something else."""
    relay = _load_relay(monkeypatch)
    seen = []

    async def fake_run_query(prompt, model, chat_id, bot, user_id):
        seen.append(model)
        return "ok"

    monkeypatch.setattr(relay, "_run_query", fake_run_query)
    relay._set_user_model(123, "claude-sonnet-5")

    asyncio.run(relay.query_claude("hi", 123, 456, None, model_override="claude-opus-4-8"))
    assert seen == ["claude-opus-4-8"]
    # Pin persists — the override was one-shot.
    assert relay._resolve_model(123) == "claude-sonnet-5"


def test_query_claude_rate_limit_fallback_skipped_when_primary_is_fallback(monkeypatch):
    """If primary is already FALLBACK_MODEL, a rate limit must NOT recursively re-invoke it."""
    relay = _load_relay(monkeypatch)
    seen = []

    async def fake_run_query(prompt, model, chat_id, bot, user_id):
        seen.append(model)
        raise Exception("rate_limit 429 boom")

    monkeypatch.setattr(relay, "_run_query", fake_run_query)
    relay._set_user_model(123, relay.FALLBACK_MODEL)

    result = asyncio.run(relay.query_claude("hi", 123, 456, None))
    assert seen == [relay.FALLBACK_MODEL]                    # one call, no retry
    assert result.startswith("Error: rate_limit 429 boom")


# --- New: fast-path short-circuit ----------------------------------------------------


def test_fast_reply_hits_greetings_and_acks_case_insensitive(monkeypatch):
    relay = _load_relay(monkeypatch)
    for text, expected in [
        ("hi", "hi 👋"),
        ("Hello!", "hi 👋"),
        ("thanks.", "np"),
        ("ok", "👍"),
        ("PING", "pong"),
    ]:
        assert relay._maybe_fast_reply(text) == expected, text


def test_fast_reply_falls_through_for_real_messages(monkeypatch):
    """A real prompt that happens to start with a greeting word must NOT get the canned reply."""
    relay = _load_relay(monkeypatch)
    for text in [
        "hi can you check the runner",       # longer than 12 chars once stripped
        "thanks for fixing that bug",
        "",
        "   ",
        "please compact",                     # not in dictionary
    ]:
        assert relay._maybe_fast_reply(text) is None, text


# --- New: auto-compact on session bloat ---------------------------------------------


def test_auto_compact_clears_session_when_bloated(monkeypatch):
    relay = _load_relay(monkeypatch)
    relay._sessions[123] = "big-session"
    monkeypatch.setattr(relay, "SESSION_BLOAT_BYTES", 1_000)
    monkeypatch.setattr(relay, "_session_size_bytes", lambda sid: 5_000)

    fired = asyncio.run(relay._maybe_auto_compact(123, 456, None))
    assert fired is True
    assert 123 not in relay._sessions


def test_auto_compact_leaves_small_sessions_alone(monkeypatch):
    relay = _load_relay(monkeypatch)
    relay._sessions[123] = "small-session"
    monkeypatch.setattr(relay, "SESSION_BLOAT_BYTES", 1_000)
    monkeypatch.setattr(relay, "_session_size_bytes", lambda sid: 500)

    fired = asyncio.run(relay._maybe_auto_compact(123, 456, None))
    assert fired is False
    assert relay._sessions[123] == "small-session"


def test_auto_compact_noop_when_no_session(monkeypatch):
    """A fresh user without a saved session_id must not blow up the compact check."""
    relay = _load_relay(monkeypatch)
    relay._sessions.clear()
    monkeypatch.setattr(relay, "SESSION_BLOAT_BYTES", 1_000)

    def _boom(_sid):
        raise AssertionError("should not stat when no session is saved")

    monkeypatch.setattr(relay, "_session_size_bytes", _boom)
    fired = asyncio.run(relay._maybe_auto_compact(123, 456, None))
    assert fired is False


def test_auto_compact_survives_missing_session_file(monkeypatch):
    """size=None (file gone / stat failed) must be treated as 'small' — never a crash."""
    relay = _load_relay(monkeypatch)
    relay._sessions[123] = "ghost"
    monkeypatch.setattr(relay, "_session_size_bytes", lambda sid: None)

    fired = asyncio.run(relay._maybe_auto_compact(123, 456, None))
    assert fired is False
    assert relay._sessions[123] == "ghost"
