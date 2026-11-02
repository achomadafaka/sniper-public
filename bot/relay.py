#!/usr/bin/env python3
"""Sniper AI Bot — Telegram ↔ Claude Code relay.

Thin bridge that pipes Telegram messages into Claude Code via the Agent SDK
and streams responses back.

Capability contract: the agent on the other end of Telegram is a FULL Claude Code
session in this repo — not a reduced one. See _base_opts(): it loads all three
setting sources (user + project + local), so it gets ~/.claude/CLAUDE.md and the
project CLAUDE.md, the 18 project subagents, the project skills, the .mcp.json
servers (sniper + supabase), the enabled plugins, and the settings.local.json hooks
(secret_file_guard, brain_ambient RAG). It runs on the claude_code system-prompt
preset with a host briefing appended, with skills="all" and permissions bypassed.
Supabase shared memory: SUPABASE_URL / SUPABASE_ANON_KEY (see bot/supabase_helper.py).

Features:
- Session continuity — remembers conversation context across messages
- Typing indicator stays active while Claude thinks
- Progress messages stream as Claude works (tool calls + intermediate text)
- Sonnet 5 default (fast, cheap); Opus 4-8 one-shot via /hard or pinned via /model opus;
  Haiku fallback on rate limit. Dropped the [1m] window suffix — the 1M variant is slower
  and pricier per turn, and the auto-compact-on-bloat below plus /compact make it moot
  in a chat thread. `/model` swaps a user's default without restarting the bot.
- Auto-compact on session bloat — resumed transcripts >300KB get reset before the next
  turn re-primes them through the API. Keeps latency flat over a long chat.
- Fast-path for trivial greetings/acks — "hi", "thanks", "ok" never hit Claude.

Usage:
    TELEGRAM_BOT_TOKEN_AI=... ALLOWED_TELEGRAM_USER_IDS=123,456 python3 relay.py
"""

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

# relay.py is launched directly (cron watchdog / shell), not by a systemd unit with
# an EnvironmentFile, so it loads the repo .env itself. Existing env always wins.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except Exception:
    pass

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sniper-bot")

# httpx logs every request line at INFO, and python-telegram-bot polls getUpdates every ~10s
# with the bot token IN THE URL. That wrote the token to the systemd journal in plaintext,
# forever, on a ~10s cadence. Silence it — we lose nothing, the bot logs its own activity.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN_AI"]
ALLOWED_IDS = {
    int(uid.strip())
    for uid in os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").split(",")
    if uid.strip().isdigit()
}
WORKING_DIR = os.getenv("BOT_WORKING_DIR", str(Path.home() / "sniper"))
MAX_MESSAGE_LEN = 4096  # Telegram limit
SCREENSHOTS_DIR = Path(WORKING_DIR) / "tmp" / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# Interpreter used for the helper scripts the bot shells out to (brain, checkpoint,
# world analyst). Must be one with the repo deps installed — on this host that is the
# pyenv 3.13.0 build, not the (empty) .venv. Override via BOT_PYTHON in .env.
BOT_PYTHON = os.getenv("BOT_PYTHON", ".venv/bin/python3")

# Model tiers. Sonnet default = ~3-5x faster + much cheaper per turn than Opus, enough for the
# vast majority of Telegram asks. Opus is one command away (`/hard <msg>` for a single turn,
# `/model opus` to pin). Haiku is the rate-limit fallback. Dropped the [1m] context suffix —
# slower + pricier, and auto-compact-on-bloat plus /compact make it unneeded in a chat thread.
# Defaults route through sniper.settings.models — a fleet-wide bump is one line there, not a
# grep sweep. BOT_* env vars still win where set (operator can override without touching code).
import sys as _sys, pathlib as _pathlib   # a bare `python3 bot/relay.py` puts this file's
_R = str(_pathlib.Path(__file__).resolve().parents[1])  # own dir on sys.path,
if _R not in _sys.path:                    # never the repo root
    _sys.path.insert(0, _R)
from sniper.settings import models as _M
PRIMARY_MODEL = os.getenv("BOT_PRIMARY_MODEL", _M.SONNET)
ESCALATION_MODEL = os.getenv("BOT_ESCALATION_MODEL", _M.OPUS)
FALLBACK_MODEL = os.getenv("BOT_FALLBACK_MODEL", _M.HAIKU)

# Auto-compact when a resumed session's transcript on disk exceeds this many bytes.
# Every turn re-primes the whole transcript through the API — a 500KB session pays that
# cost on every message. Threshold picked to fire well before the CLI's own PreCompact.
SESSION_BLOAT_BYTES = int(os.getenv("BOT_SESSION_BLOAT_BYTES", "300000"))

# Model aliases accepted by /model and /hard/... — shorthand -> real model id from the registry.
MODEL_ALIASES = {
    "opus":    _M.OPUS,
    "sonnet":  _M.SONNET,
    "haiku":   _M.HAIKU,
    "fast":    _M.FAST,
    "hard":    _M.OPUS,
}

# All tool calls are shown live in Telegram (no batching)


# ---------------------------------------------------------------------------
# Session management — persist across messages for context continuity
# ---------------------------------------------------------------------------

_sessions: dict[int, str] = {}  # telegram_user_id -> claude session_id
_user_models: dict[int, str] = {}  # telegram_user_id -> pinned model id (overrides PRIMARY_MODEL)
_last_chat_id: int | None = None  # most recent active chat — used by hooks for Telegram push

# Sessions live on disk, not just in memory. They used to be in-memory only, so ANY restart
# (systemctl restart, the MemoryMax=1200M ceiling firing, a crash) silently wiped the agent's
# memory of the conversation and you'd start cold mid-thread. Claude session ids stay
# resumable across process restarts, so there is no reason to throw them away.
_SESSIONS_FILE = Path(WORKING_DIR) / "data" / "bot_sessions.json"


def _load_sessions() -> None:
    """Restore telegram_user_id -> {session_id, model} from the last run.

    Backward compat: old format stored the session id as a bare string; new format is
    {"session": "...", "model": "..."}. Both are accepted; new writes are always dicts.
    """
    global _sessions, _user_models
    try:
        import json
        raw = json.loads(_SESSIONS_FILE.read_text())
    except FileNotFoundError:
        logger.info("No saved sessions (%s) — starting cold", _SESSIONS_FILE)
        _sessions = {}
        _user_models = {}
        return
    except Exception:
        logger.exception("Could not read %s; keeping in-memory state", _SESSIONS_FILE)
        return

    new_sessions: dict[int, str] = {}
    new_models: dict[int, str] = {}
    try:
        for k, v in (raw or {}).items():
            uid = int(k)
            if isinstance(v, str):
                new_sessions[uid] = v
            elif isinstance(v, dict):
                if v.get("session"):
                    new_sessions[uid] = str(v["session"])
                if v.get("model"):
                    new_models[uid] = str(v["model"])
    except Exception:
        logger.exception("Could not parse session records; keeping in-memory state")
        return

    _sessions = new_sessions
    _user_models = new_models
    logger.info("Restored %d session(s), %d model pref(s) from %s",
                len(_sessions), len(_user_models), _SESSIONS_FILE)


def _save_sessions() -> None:
    """Persist sessions + per-user model pins. Best-effort: never break a reply over this."""
    try:
        import json
        payload: dict[str, dict] = {}
        for uid in set(_sessions) | set(_user_models):
            entry: dict[str, str] = {}
            if uid in _sessions:
                entry["session"] = _sessions[uid]
            if uid in _user_models:
                entry["model"] = _user_models[uid]
            payload[str(uid)] = entry
        _SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SESSIONS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(_SESSIONS_FILE)  # atomic
    except Exception:
        logger.exception("Could not persist sessions")


def _set_session(user_id: int, session_id: str) -> None:
    if _sessions.get(user_id) != session_id:
        _sessions[user_id] = session_id
        _save_sessions()


def _clear_session(user_id: int) -> str | None:
    old = _sessions.pop(user_id, None)
    if old is not None:
        _save_sessions()
    return old


def _resolve_model(user_id: int) -> str:
    """Which model does this user's next turn run on? Per-user pin > PRIMARY_MODEL default."""
    return _user_models.get(user_id) or PRIMARY_MODEL


def _set_user_model(user_id: int, model: str | None) -> None:
    """Pin a per-user model. None clears the pin so future turns use PRIMARY_MODEL."""
    changed = False
    if model is None:
        if _user_models.pop(user_id, None) is not None:
            changed = True
    else:
        if _user_models.get(user_id) != model:
            _user_models[user_id] = model
            changed = True
    if changed:
        _save_sessions()


def _project_slug(working_dir: str) -> str:
    """Reproduce Claude Code's on-disk project-dir naming: absolute path, '/' -> '-'."""
    resolved = str(Path(working_dir).resolve())
    return "-" + resolved.lstrip("/").replace("/", "-")


def _session_size_bytes(session_id: str) -> int | None:
    """Size on disk of a Claude session's jsonl. None if unreadable / missing."""
    try:
        path = Path.home() / ".claude" / "projects" / _project_slug(WORKING_DIR) / f"{session_id}.jsonl"
        return path.stat().st_size
    except Exception:
        return None


async def _maybe_auto_compact(user_id: int, chat_id: int, bot) -> bool:
    """Reset a bloated session before its transcript blows up the next turn's latency.

    Every turn re-primes the entire resumed transcript through the API. A 500KB session
    pays that on every message. When we cross SESSION_BLOAT_BYTES we clear the session so
    the next turn starts fresh — CLAUDE.md + MEMORY.md auto-load, so the agent stays
    oriented without us having to run a full brain briefing here.
    """
    sid = _sessions.get(user_id)
    if not sid:
        return False
    size = _session_size_bytes(sid)
    if size is None or size < SESSION_BLOAT_BYTES:
        return False
    old = _clear_session(user_id)
    logger.info("Auto-compact: cleared %s (%dB >= %dB threshold)",
                (old or "?")[:16], size, SESSION_BLOAT_BYTES)
    if bot is not None:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(f"🧠 Auto-compacted: session was {size // 1024}KB "
                      f"(threshold {SESSION_BLOAT_BYTES // 1024}KB). Fresh session — "
                      f"CLAUDE.md and MEMORY.md re-load automatically."),
            )
        except Exception:
            pass
    return True


# --- Fast-path: greetings and acks that never need to hit Claude --------------------

_FAST_REPLIES = {
    "hi": "hi 👋", "hello": "hi 👋", "hey": "hey", "yo": "yo", "sup": "sup",
    "gm": "gm ☕", "gn": "gn 🌙", "gday": "gday",
    "thanks": "np", "thx": "np", "ty": "np", "cheers": "np", "tysm": "np",
    "ok": "👍", "okay": "👍", "k": "👍", "cool": "👍", "nice": "👍", "sweet": "👍",
    "lol": "😄", "haha": "😄", "lmao": "😄", "lmfao": "😄",
    "ping": "pong", "pong": "🏓",
}


def _maybe_fast_reply(text: str) -> str | None:
    """Return a canned reply for trivial greetings/acks, else None.

    Only fires for very short input where the semantics are unambiguous — so a real
    message like 'hi, can you check the runner' will fall through to Claude untouched.
    """
    if not text:
        return None
    stripped = text.strip().rstrip("!.?").lower()
    if not stripped or len(stripped) > 12:
        return None
    return _FAST_REPLIES.get(stripped)


SYSTEM_PROMPT = f"""You are the Sniper AI Bot — an autonomous engineering agent running LOCALLY on the
owner's machine, in the repo at {WORKING_DIR}. There is NO VPS. The old Amsterdam box
(188.166.91.20) is dead and gone; ignore any doc that tells you to ssh anywhere.

You are a full Claude Code session reached over Telegram — same tools, same skills, same
subagents, same MCP servers, same CLAUDE.md, same brain as an interactive session in this
repo. Permissions are bypassed, so nothing will stop you: that is exactly why the real-money
guardrails below are hard rules and not suggestions.

IDENTITY: You are an engineer, not a chatbot. Act immediately, verify your work, suggest next steps.

TELEGRAM OUTPUT: your replies are chat messages, not a terminal. Lead with the answer. No
walls of text, no restating the question. Long output gets chunked at 4096 chars, so be terse.

=== REAL MONEY IS LIVE ON THIS HOST — READ THIS FIRST ===
The memecoin copy runner trades REAL FUNDS (Solana + EVM/Base) right now. It is
release-pinned: it runs from .runtime/memecoin/current/, NOT from whatever branch is
checked out. A cron watchdog restarts it every minute.
- NEVER kill, restart, or "fix" the memecoin runner, its watchdog, or its config on your own.
- NEVER edit trading params, arming/allowlists, sizing, TP/SL, or wallet keys without EXPLICIT
  owner approval in this chat.
- NEVER deploy runtime. Deploys go only through scripts/deploy_memecoin_runtime.sh <full-sha>
  reachable from origin/main, and only when the owner asks.
- Editing a file in the working tree does NOT affect the live runner. Do not assume it does.
Health check (read-only, safe): cat data/memecoin_runner.heartbeat
  → ts age < 30s and auth_ok true = healthy.

GUARDRAILS:
- NEVER create or deploy strategies without explicit owner approval.
- NEVER modify scoring logic without explicit owner approval.
- Do NOT restart services or run analyzer batches / live-order paths unless asked.
- Run the focused tests for what you touched before committing. Commit scoped, verified units.
- Do NOT push to origin or open PRs without explicit owner approval.
- `git pull` before editing — other agents and local sessions share this ONE checkout.
- Honesty rules are absolute: verify before claiming, quote real output, never say "fixed"
  or "working" without having actually run it. Say "I didn't see the output" when you didn't.

RUNTIME MODEL — the trading stack is kept alive by CRON WATCHDOGS, not systemd. Every
sniper-*.service unit is disabled EXCEPT sniper-bot.service, which is the unit running YOU.
So: never use systemctl/journalctl to reason about the trading engines (they'll show you
nothing), and never `systemctl --user restart sniper-bot` — that kills your own session
mid-reply. Read live state from processes, cron and logs:
  ps aux | grep -E "[m]emecoin_runner_loop|[s]niper.web.server"
  crontab -l          # memecoin, sniper-web, crypto5m, hl_copy, hl_follow watchdogs
  tail -n 50 logs/<name>.log
Python for repo scripts: {BOT_PYTHON} (the repo .venv is EMPTY — never use it).

SHARED MEMORY (Supabase brain via bot/supabase_helper.py):
- Semantic recall:  {BOT_PYTHON} bot/supabase_helper.py semantic "<task in words>"
- Exact/key search: {BOT_PYTHON} bot/supabase_helper.py search "<key or address>"
- Read context:     {BOT_PYTHON} bot/supabase_helper.py read-full
- Write session log:{BOT_PYTHON} bot/supabase_helper.py write-log "<summary>"
447+ active findings live there. Query BEFORE starting work — prior findings often already
answer or REFUTE the idea. Reads default to status=active.

FIRST THING EVERY SESSION — get live state, assume nothing:
  cat data/memecoin_runner.heartbeat
  ps aux | grep -E "[m]emecoin_runner_loop|[s]niper.web.server"
  git status --short && git log --oneline -5

PROJECT: Sniper — Polymarket wallet intelligence + memecoin copy trading.
Source of truth: CLAUDE.md and AGENTS.md (read them; they override your assumptions).
Live handoff state: ~/.claude/projects/-home-bigpapi-Desktop-Sniper/memory/HANDOFF.md
DB: sniper_tracker.db (SQLite WAL). Config: .env."""


# ---------------------------------------------------------------------------
# Context lifecycle hooks — keep Supabase brain current automatically
# ---------------------------------------------------------------------------

def _tg_send(text: str) -> None:
    """Fire-and-forget Telegram message via raw HTTP (sync, safe to call from hooks)."""
    if not _last_chat_id:
        return
    try:
        import urllib.request, urllib.parse, json as _json
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        for i in range(0, len(text), MAX_MESSAGE_LEN):
            chunk = text[i:i + MAX_MESSAGE_LEN]
            data = _json.dumps({"chat_id": _last_chat_id, "text": chunk}).encode()
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # never raise from a hook helper


def _spawn_detached(cmd: list[str], *, log_name: str | None = None) -> None:
    """Launch a child process that survives hook/session teardown."""
    import subprocess

    logs_dir = Path(WORKING_DIR) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    stdout_target = subprocess.DEVNULL
    log_fh = None
    if log_name:
        log_fh = open(logs_dir / log_name, "a")
        stdout_target = log_fh

    try:
        subprocess.Popen(
            cmd,
            cwd=WORKING_DIR,
            stdout=stdout_target,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        if log_fh is not None:
            log_fh.close()


# The SDK's HookCallback contract is:
#     async (input: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput
# These used to be sync, single-arg functions, so the CLI killed every invocation with
# "_hook_stop() takes 1 positional argument but 3 were given" — meaning neither hook has
# ever actually run. HookInput is a TypedDict, so read it with .get(), not getattr().

def _pre_compact_blocking(trigger: str) -> None:
    """The blocking half of the PreCompact hook — runs off the event loop."""
    import subprocess

    # Checkpoint to Supabase
    try:
        subprocess.run(
            [BOT_PYTHON, "scripts/checkpoint.py",
             "--quiet", "--reason", f"pre-compact-{trigger}"],
            cwd=WORKING_DIR, timeout=20, check=False,
        )
    except Exception:
        logger.exception("PreCompact checkpoint failed")

    # Run brain.py --full and push to Telegram
    try:
        _tg_send("🧠 Context nearly full — auto-compacting. Here's your briefing:")
        result = subprocess.run(
            [BOT_PYTHON, "scripts/brain_briefing.py", "--full"],
            capture_output=True, text=True, timeout=30, cwd=WORKING_DIR,
        )
        brain_output = result.stdout or result.stderr or "(no output)"
        _tg_send(brain_output)
        _tg_send("✅ Compaction done. Context summarised — continuing seamlessly.")
    except Exception:
        logger.exception("PreCompact brain briefing failed")


async def _hook_pre_compact(input_data, tool_use_id, context) -> dict:
    """PreCompact hook: fires when the context window is nearly full.

    Checkpoints state to Supabase, then pushes a brain.py --full briefing to Telegram.
    """
    trigger = (input_data or {}).get("trigger", "auto") if isinstance(input_data, dict) else "auto"
    try:
        # subprocess.run here would stall the event loop (and the typing indicator)
        # for up to 50s. Push it to a thread.
        await asyncio.to_thread(_pre_compact_blocking, trigger)
    except Exception:
        logger.exception("PreCompact hook failed")
    return {}


async def _hook_stop(input_data, tool_use_id, context) -> dict:
    """Stop hook: checkpoint to Supabase + refresh world context, both detached.

    Kept truly non-blocking: if this does synchronous work, the SDK reports a timeout
    after the user-visible task has already finished.
    """
    try:
        _spawn_detached(
            [BOT_PYTHON, "scripts/checkpoint.py",
             "--quiet", "--reason", "session-stop"],
            log_name="checkpoint.log",
        )
    except Exception:
        logger.exception("Stop hook: checkpoint spawn failed")
    try:
        _spawn_detached(
            [BOT_PYTHON, "scripts/world_analyst.py",
             "--quick", "--quiet", "--domains", "geo,politics,finance"],
            log_name="world_analyst.log",
        )
    except Exception:
        logger.exception("Stop hook: world analyst spawn failed")
    return {}


def _build_hooks() -> dict:
    """Build the hooks dict for ClaudeAgentOptions."""
    try:
        from claude_agent_sdk.types import HookMatcher
        return {
            "PreCompact": [HookMatcher(hooks=[_hook_pre_compact], timeout=30)],
            "Stop":       [HookMatcher(hooks=[_hook_stop],        timeout=30)],
        }
    except Exception:
        return {}


def _base_opts(model: str) -> "ClaudeAgentOptions":
    """Build options that are always set, whether fresh or resumed.

    Goal: the Telegram agent has the SAME capabilities and the SAME context as an
    interactive Claude Code session opened in this repo. Every field below exists to
    close a gap that made it *weaker* than the CLI.
    """
    from claude_agent_sdk import ClaudeAgentOptions
    return ClaudeAgentOptions(
        cwd=WORKING_DIR,
        model=model,
        permission_mode="bypassPermissions",

        # Load ALL filesystem settings — this is the CLI's own default (the SDK treats
        # None as "all"). It used to pass ["project"], which silently dropped:
        #   user  -> ~/.claude/CLAUDE.md (the owner's global instructions),
        #            ~/.claude/settings.json (enabledPlugins, effortLevel=xhigh)
        #   local -> .claude/settings.local.json (30 permission rules,
        #            enableAllProjectMcpServers, and the secret_file_guard +
        #            brain_ambient RAG hooks)
        # project keeps: ./CLAUDE.md, .claude/agents/ (18), .claude/skills/ (5), .mcp.json
        setting_sources=["user", "project", "local"],

        # Claude Code's REAL system prompt, with our host briefing appended.
        # A bare string here REPLACES the preset — which is what this used to do, so the
        # bot was running with no Claude Code system prompt at all. `append` keeps the
        # preset (and its dynamic sections: working dir, auto-memory, git status).
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": SYSTEM_PROMPT,
        },

        # Skills were never enabled. "all" also wires up the Skill tool for us.
        skills="all",

        # NOTE: do NOT set betas=["context-1m-2025-08-07"] here. This host authenticates
        # with a Claude subscription, not an API key, and the CLI answers with
        # "Custom betas are only available for API key users. Ignoring provided betas."
        # The 1M window comes from the model ID suffix instead — see PRIMARY_MODEL.

        max_turns=200,
        hooks=_build_hooks(),
    )


# ---------------------------------------------------------------------------
# Claude Code Agent SDK integration
# ---------------------------------------------------------------------------

def _describe_tool_call(name: str, inp: dict) -> str:
    """Turn a tool call into a short, readable Telegram message."""
    n = name.lower()
    if n == "bash":
        cmd = str(inp.get("command", ""))[:120]
        return f"💻 `{cmd}`"
    elif n == "read":
        path = str(inp.get("file_path", ""))
        short = path.split("/")[-1] if "/" in path else path
        return f"📖 Reading {short}"
    elif n == "write":
        path = str(inp.get("file_path", ""))
        short = path.split("/")[-1] if "/" in path else path
        return f"✏️ Writing {short}"
    elif n == "edit":
        path = str(inp.get("file_path", ""))
        short = path.split("/")[-1] if "/" in path else path
        return f"🔧 Editing {short}"
    elif n == "glob":
        return f"🔍 Finding files: {inp.get('pattern', '')}"
    elif n == "grep":
        return f"🔍 Searching: {str(inp.get('pattern', ''))[:60]}"
    elif n == "webfetch":
        return f"🌐 Fetching: {str(inp.get('url', ''))[:80]}"
    elif n == "websearch":
        return f"🌐 Searching web: {str(inp.get('query', ''))[:80]}"
    elif n == "agent":
        desc = str(inp.get("description", inp.get("prompt", "")))[:80]
        return f"🤖 Spawning subagent: {desc}"
    else:
        return f"⚙️ {name}"


# ---------------------------------------------------------------------------
# Signal parser — lets Claude self-trigger commands by emitting ⚡/cmd in text
# ---------------------------------------------------------------------------

_SIGNAL_PREFIX = "⚡/"

async def _handle_signals(text: str, chat_id: int, bot, user_id: int) -> None:
    """Parse Claude's response text for ⚡/command signals and execute them.

    Claude can autonomously trigger commands by including lines like:
        ⚡/brain
        ⚡/brain full
        ⚡/compact
        ⚡/reset

    This fires after each streamed text block — multiple signals per response OK.
    """
    import re
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(_SIGNAL_PREFIX):
            continue
        cmd_str = stripped[len(_SIGNAL_PREFIX):].strip().lower()
        parts = cmd_str.split()
        cmd = parts[0] if parts else ""
        args = parts[1:] if len(parts) > 1 else []
        logger.info("Signal detected: ⚡/%s %s", cmd, args)

        try:
            if cmd == "brain":
                full = "full" in args
                await bot.send_message(chat_id=chat_id, text="🧠 Auto-running brain briefing...")
                await run_brain(chat_id, bot, full=full)

            elif cmd == "compact":
                await bot.send_message(chat_id=chat_id, text="🧠 Auto-compacting context...")
                _clear_session(user_id)
                import subprocess
                result = await asyncio.to_thread(
                    subprocess.run,
                    [BOT_PYTHON, "scripts/brain_briefing.py", "--full"],
                    capture_output=True, text=True, timeout=30, cwd=WORKING_DIR,
                )
                brain_output = result.stdout or "(no output)"
                await bot.send_message(chat_id=chat_id, text=f"```\n{brain_output[:MAX_MESSAGE_LEN - 10]}\n```", parse_mode="Markdown")

            elif cmd == "reset":
                _clear_session(user_id)
                await bot.send_message(chat_id=chat_id, text="🔄 Auto-reset: session cleared.")

            else:
                logger.warning("Unknown signal: ⚡/%s", cmd)
        except Exception as e:
            logger.warning("Signal ⚡/%s failed: %s", cmd, e)


async def _run_query(prompt: str, model: str, chat_id: int, bot, user_id: int) -> str:
    """Run a Claude Code query with typing indicator and progress streaming."""
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage, SystemMessage

    # Resume existing session if available, otherwise start fresh
    session_id = _sessions.get(user_id)
    if session_id:
        opts = _base_opts(model)
        opts.resume = session_id
        logger.info("Resuming session %s for user %s", session_id[:16], user_id)
    else:
        opts = _base_opts(model)
        logger.info("Starting fresh session for user %s", user_id)

    result_text = ""
    message_count = 0
    tool_calls = 0
    last_progress_at = 0
    text_chunks_sent = 0
    last_activity = asyncio.get_running_loop().time()

    # Idle timeout: if no agent messages for this many seconds, kill the query.
    # This catches rate-limit hangs and API stalls without limiting turns.
    IDLE_TIMEOUT = int(os.getenv("BOT_IDLE_TIMEOUT_SECONDS", "300"))  # 5 min default

    # Keep sending "typing..." action every 4 seconds + watchdog
    async def keep_typing():
        try:
            while True:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                # Watchdog: check idle time
                idle_secs = asyncio.get_running_loop().time() - last_activity
                if idle_secs > IDLE_TIMEOUT:
                    logger.warning("Idle watchdog triggered: %ds without agent messages", int(idle_secs))
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"⏰ Query stalled ({int(idle_secs)}s idle, {message_count} msgs, {tool_calls} tools). "
                                 f"Likely hit rate limit or API hang. Cancelling — send your request again.",
                        )
                    except Exception:
                        pass
                    raise asyncio.CancelledError()
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise

    typing_task = asyncio.create_task(keep_typing())

    try:
        async for message in query(prompt=prompt, options=opts):
            last_activity = asyncio.get_running_loop().time()
            message_count += 1
            msg_type = type(message).__name__
            logger.info("Agent msg #%d: %s", message_count, msg_type)

            # Capture session ID for continuity
            if isinstance(message, SystemMessage) and message.subtype == "init":
                new_session_id = message.data.get("session_id")
                if new_session_id:
                    _set_session(user_id, new_session_id)
                    logger.info("Session ID captured: %s", new_session_id[:16])

            elif isinstance(message, ResultMessage):
                result_text = message.result or ""
                # ResultMessage always carries session_id — more reliable than SystemMessage
                if hasattr(message, "session_id") and message.session_id:
                    _set_session(user_id, message.session_id)
                cost = getattr(message, "total_cost_usd", None)
                turns = getattr(message, "num_turns", None)
                logger.info(
                    "Result (stop=%s, len=%d, cost=$%.3f, turns=%s)",
                    message.stop_reason, len(result_text), cost or 0, turns,
                )

            else:
                # Extract content blocks from any message type
                content = getattr(message, "content", None)
                if isinstance(content, list):
                    for block in content:
                        block_cls = type(block).__name__

                        # Stream text as it arrives
                        if "Text" in block_cls:
                            text = getattr(block, "text", "")
                            if text and len(text) > 20:
                                text_chunks_sent += 1
                                for i in range(0, len(text), MAX_MESSAGE_LEN):
                                    chunk = text[i:i + MAX_MESSAGE_LEN]
                                    try:
                                        await bot.send_message(
                                            chat_id=chat_id, text=chunk,
                                            parse_mode="Markdown",
                                        )
                                    except Exception:
                                        try:
                                            await bot.send_message(chat_id=chat_id, text=chunk)
                                        except Exception:
                                            pass
                                # Signal parser — Claude self-triggers commands via ⚡/cmd
                                await _handle_signals(text, chat_id, bot, user_id)

                        # Show tool calls live
                        elif "ToolUse" in block_cls:
                            tool_calls += 1
                            tool_name = getattr(block, "name", "unknown")
                            tool_input = getattr(block, "input", {}) or {}
                            action = _describe_tool_call(tool_name, tool_input)
                            try:
                                await bot.send_message(chat_id=chat_id, text=action)
                            except Exception:
                                pass
    finally:
        if typing_task:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

    # Check for turn limit stops — inform user
    if hasattr(message, "subtype"):
        if message.subtype == "error_max_turns":
            return f"⚠️ Hit {200}-turn limit ({tool_calls} tool calls). Send another message to continue."

    # If we already streamed text blocks to Telegram, don't re-send the final result
    # (it's the same content — sending it again causes doubling)
    if text_chunks_sent > 0:
        return ""
    elif result_text:
        return result_text
    else:
        return f"(completed {message_count} messages, {tool_calls} tool calls, but no text output — try a more specific request)"


async def query_claude(prompt: str, user_id: int, chat_id: int, bot,
                       model_override: str | None = None) -> str:
    """Send a prompt to Claude Code.

    Model selection: `model_override` (from /hard, /fast) > per-user pin (/model) > PRIMARY_MODEL.
    Auto-compacts bloated sessions first. On rate limit, falls back to FALLBACK_MODEL (Haiku)
    unless the primary already IS the fallback.
    """
    from claude_agent_sdk._errors import ProcessError

    primary = model_override or _resolve_model(user_id)

    # Reset a huge resumed transcript before it costs us this whole turn's latency.
    await _maybe_auto_compact(user_id, chat_id, bot)

    had_session = user_id in _sessions

    try:
        return await _run_query(prompt, primary, chat_id, bot, user_id)
    except Exception as exc:
        err_str = str(exc)

        # A poisoned resumed session can make the bundled Claude CLI exit during
        # initialization with only a generic ProcessError. If we had a saved
        # session, clear it and retry once fresh before surfacing the error.
        if had_session and (
            isinstance(exc, ProcessError)
            or "session" in err_str.lower()
            or "resume" in err_str.lower()
        ):
            logger.warning("Saved session failed, starting fresh: %s", err_str[:100])
            _clear_session(user_id)
            try:
                return await _run_query(prompt, primary, chat_id, bot, user_id)
            except Exception as retry_exc:
                exc = retry_exc
                err_str = str(exc)
                logger.exception("Fresh session retry also failed")
        if ("rate" in err_str.lower() or "429" in err_str) and primary != FALLBACK_MODEL:
            logger.warning("%s rate limited, falling back to %s", primary, FALLBACK_MODEL)
            _clear_session(user_id)  # Fresh session with fallback model
            try:
                return await _run_query(prompt, FALLBACK_MODEL, chat_id, bot, user_id)
            except Exception as fallback_exc:
                logger.exception("Fallback model also failed")
                return (f"Both models failed.\n{primary}: {err_str[:200]}\n"
                        f"{FALLBACK_MODEL}: {str(fallback_exc)[:200]}")
        logger.exception("Claude query failed")
        return f"Error: {err_str[:500]}"


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — report user ID for lockdown setup."""
    user = update.effective_user
    uid = user.id
    await update.message.reply_text(
        f"Sniper AI Bot\n\n"
        f"Your Telegram user ID: `{uid}`\n"
        f"Your username: @{user.username or 'none'}\n\n"
        f"{'✅ You are authorized.' if uid in ALLOWED_IDS or not ALLOWED_IDS else '🔒 Not authorized. Add your ID to ALLOWED_TELEGRAM_USER_IDS.'}",
        parse_mode="Markdown",
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset conversation — clear session for fresh start."""
    if not _is_authorized(update):
        return
    uid = update.effective_user.id
    old = _clear_session(uid)
    await update.message.reply_text(
        f"🔄 Session reset. {'Cleared session ' + old[:16] + '...' if old else 'No active session.'}\nNext message starts fresh.",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick system status without burning Claude tokens.

    Reports what actually runs on this host: the real-money memecoin runner (via its
    heartbeat) and the web dashboard. The sniper-*.service systemd units are all
    disabled here — everything is kept alive by cron watchdogs.
    """
    if not _is_authorized(update):
        return
    import json
    import subprocess
    import time

    lines = []

    # Real-money memecoin runner — heartbeat is the authoritative health signal.
    try:
        hb_raw = (Path(WORKING_DIR) / "data" / "memecoin_runner.heartbeat").read_text()
        hb = json.loads(hb_raw)
        age = time.time() - float(hb.get("ts", 0))
        healthy = age < 30 and bool(hb.get("auth_ok"))
        lines.append(
            f"{'✅' if healthy else '🔴'} memecoin runner (REAL MONEY)\n"
            f"   heartbeat {age:.0f}s ago | auth_ok={hb.get('auth_ok')} "
            f"| events={hb.get('events')} opened={hb.get('opened')}"
        )
    except Exception as e:
        lines.append(f"🔴 memecoin runner: no readable heartbeat ({e})")

    # Live processes.
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["pgrep", "-af", "memecoin_runner_loop|sniper.web.server"],
            capture_output=True, text=True, timeout=10,
        )
        procs = [ln for ln in result.stdout.splitlines() if ln.strip()]
        lines.append(
            f"\n🔧 processes ({len(procs)}):\n"
            + ("\n".join(f"   {p[:110]}" for p in procs) if procs else "   none running")
        )
    except Exception as e:
        lines.append(f"\n🔧 processes: check failed ({e})")

    output = "\n".join(lines)[:MAX_MESSAGE_LEN - 100]
    await update.message.reply_text(output)


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/model — show current; /model opus|sonnet|haiku|<raw-id> — pin; /model default — clear.

    A pinned model is persisted per-user across restarts. Useful for holding Opus for a
    stretch of hard work without prefixing every message with /hard.
    """
    if not _is_authorized(update):
        return
    uid = update.effective_user.id
    if not context.args:
        current = _resolve_model(uid)
        pinned = _user_models.get(uid)
        await update.message.reply_text(
            f"Current: `{current}`\n"
            f"Pinned:  `{pinned or '(none)'}`\n"
            f"Default: `{PRIMARY_MODEL}`\n"
            f"Fallback (rate limit): `{FALLBACK_MODEL}`\n"
            f"Escalation (/hard):    `{ESCALATION_MODEL}`\n\n"
            f"Usage: /model opus | sonnet | haiku | default | <raw-model-id>",
            parse_mode="Markdown",
        )
        return
    arg = context.args[0].strip().lower()
    if arg in ("default", "reset", "clear", "-", "none"):
        _set_user_model(uid, None)
        await update.message.reply_text(f"Model pin cleared → using default `{PRIMARY_MODEL}`.",
                                        parse_mode="Markdown")
        return
    resolved = MODEL_ALIASES.get(arg, context.args[0].strip())  # keep original case for raw id
    _set_user_model(uid, resolved)
    await update.message.reply_text(f"Pinned model: `{resolved}`", parse_mode="Markdown")


async def _run_prompt_with_model(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                 model: str) -> None:
    """Shared body for /hard and /fast: run one turn on an explicit model."""
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    global _last_chat_id
    _last_chat_id = chat_id

    # Strip the "/hard " or "/fast " prefix; everything after is the prompt.
    text = update.message.text or ""
    parts = text.split(" ", 1)
    prompt = parts[1] if len(parts) > 1 else ""
    if not prompt.strip():
        await update.message.reply_text(f"Usage: `{parts[0]} <message>` → one turn on `{model}`.",
                                        parse_mode="Markdown")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    response = await query_claude(prompt, uid, chat_id, context.bot, model_override=model)
    if response:
        for i in range(0, len(response), MAX_MESSAGE_LEN):
            chunk = response[i:i + MAX_MESSAGE_LEN]
            try:
                await update.message.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(chunk)


async def cmd_hard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hard <msg> — run one turn on the escalation model (Opus) without touching your default."""
    if not _is_authorized(update):
        return
    await _run_prompt_with_model(update, context, ESCALATION_MODEL)


async def cmd_fast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fast <msg> — run one turn on the fast/cheap model (Haiku) without touching your default."""
    if not _is_authorized(update):
        return
    await _run_prompt_with_model(update, context, FALLBACK_MODEL)


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current memory usage."""
    if not _is_authorized(update):
        return
    import subprocess
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["bash", "-c", "free -h && echo '---' && ps aux --sort=-%mem | head -8"],
            capture_output=True, text=True, timeout=10,
        )
        await update.message.reply_text(f"```\n{result.stdout[:MAX_MESSAGE_LEN - 50]}\n```", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def run_brain(chat_id: int, bot, full: bool = False) -> None:
    """Run brain.py and push output to Telegram. Shared by /brain command and signal parser."""
    import subprocess
    flag = "--full" if full else "--quick"
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [BOT_PYTHON, "scripts/brain_briefing.py", flag],
            capture_output=True, text=True, timeout=30, cwd=WORKING_DIR,
        )
        brain_output = result.stdout or result.stderr or "(no output)"
    except Exception as e:
        brain_output = f"brain.py failed: {e}"
    for i in range(0, len(brain_output), MAX_MESSAGE_LEN - 10):
        chunk = brain_output[i:i + MAX_MESSAGE_LEN - 10]
        try:
            await bot.send_message(chat_id=chat_id, text=f"```\n{chunk}\n```", parse_mode="Markdown")
        except Exception:
            await bot.send_message(chat_id=chat_id, text=chunk)


async def cmd_brain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/brain — run brain.py briefing on demand. /brain full for full Supabase memories."""
    if not _is_authorized(update):
        return
    chat_id = update.effective_chat.id
    full = bool(context.args and context.args[0].lower() == "full")
    await update.message.reply_text("🧠 Loading brain briefing...")
    await run_brain(chat_id, context.bot, full=full)


async def cmd_regime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/regime — show current market regime status for all monitored strategies."""
    if not _is_authorized(update):
        return
    import subprocess
    await update.message.reply_text("Checking regime status...")
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [BOT_PYTHON, "scripts/regime_monitor.py", "--status"],
            capture_output=True, text=True, timeout=30, cwd=WORKING_DIR,
        )
        output = result.stdout or result.stderr or "(no output)"
    except Exception as e:
        output = f"regime_monitor.py failed: {e}"
    for i in range(0, len(output), MAX_MESSAGE_LEN - 10):
        chunk = output[i:i + MAX_MESSAGE_LEN - 10]
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"<pre>{chunk}</pre>",
                parse_mode="HTML",
            )
        except Exception:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=chunk)


async def cmd_world(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/world — run the world analyst agent (full cycle, all domains).
    /world quick — geo + finance only (~3 min faster).
    Runs the Claude-powered world analyst, updates brain, sends conclusions here.
    """
    if not _is_authorized(update):
        return
    import subprocess
    quick = bool(context.args and context.args[0].lower() in ("quick", "fast", "q"))
    domains = "geo,politics,finance" if quick else "geo,politics,finance,sports,crypto"
    mode = "quick" if quick else "full"
    await update.message.reply_text(
        f"🌍 Launching World Analyst ({mode} mode — all domains: {domains})...\n"
        "Results will be sent here when complete (3-10 min)."
    )
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [BOT_PYTHON, "scripts/world_analyst.py",
             "--domains", domains,
             *(["--quick"] if quick else [])],
            capture_output=True, text=True, timeout=600, cwd=WORKING_DIR,
        )
        if proc.returncode == 0:
            await update.message.reply_text("✅ World analyst complete — brain updated.")
        else:
            err = (proc.stderr or proc.stdout or "no output")[:500]
            await update.message.reply_text(f"⚠️ World analyst exited non-zero:\n{err}")
    except asyncio.TimeoutError:
        await update.message.reply_text("⏰ World analyst timed out (>10 min). Check logs/world_analyst.log")
    except Exception as e:
        await update.message.reply_text(f"❌ World analyst error: {e}")


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/compact — run brain.py --full, clear session, reinject briefing as fresh context.

    Simulates what Claude Code's /compact + brain.py does:
    - Dumps full system state to Telegram so you can see it
    - Clears the current Claude session (preventing stale/heavy context)
    - Seeds a fresh session with the brain briefing so Claude is instantly oriented
    """
    if not _is_authorized(update):
        return
    import subprocess

    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    await update.message.reply_text("🧠 Compacting context — running brain briefing...")

    # Step 1: Run brain.py --full
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [BOT_PYTHON, "scripts/brain_briefing.py", "--full"],
            capture_output=True, text=True, timeout=30, cwd=WORKING_DIR,
        )
        brain_output = result.stdout or result.stderr or "(no output)"
    except Exception as e:
        brain_output = f"brain.py failed: {e}"

    # Step 2: Send briefing to Telegram (chunked)
    await update.message.reply_text("📋 Brain briefing:")
    for i in range(0, len(brain_output), MAX_MESSAGE_LEN - 10):
        chunk = brain_output[i:i + MAX_MESSAGE_LEN - 10]
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"```\n{chunk}\n```", parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=chunk)

    # Step 3: Clear old session
    old = _clear_session(uid)
    logger.info("Compact: cleared session %s for user %s", (old or "none")[:16], uid)

    # Step 4: Seed fresh session with brain context so Claude is immediately oriented
    await update.message.reply_text("🔄 Starting fresh session with brain context loaded...")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    seed_prompt = (
        "Context compaction just occurred. Here is the full system briefing — "
        "treat this as your restored context and confirm you're oriented:\n\n"
        + brain_output[:6000]
    )
    response = await query_claude(seed_prompt, uid, chat_id, context.bot)
    if response:
        for i in range(0, len(response), MAX_MESSAGE_LEN):
            chunk = response[i:i + MAX_MESSAGE_LEN]
            try:
                await update.message.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(chunk)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle regular text messages — relay to Claude Code."""
    if not _is_authorized(update):
        return

    user_text = update.message.text
    if not user_text:
        return

    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # Track for hook use (pre-compact auto-push to Telegram)
    global _last_chat_id
    _last_chat_id = chat_id

    # Fast-path: trivial greetings/acks never touch Claude. Saves ~5-30s and a full
    # session-resume for messages where a canned reply is genuinely all that's warranted.
    fast = _maybe_fast_reply(user_text)
    if fast is not None:
        logger.info("Fast-reply to %s: %r -> %r", uid, user_text[:40], fast)
        await update.message.reply_text(fast)
        return

    logger.info("Message from %s: %s", uid, user_text[:80])

    # Send initial typing indicator
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    response = await query_claude(user_text, uid, chat_id, context.bot)

    # Send final response if not empty (might have been streamed already)
    if response:
        for i in range(0, len(response), MAX_MESSAGE_LEN):
            chunk = response[i:i + MAX_MESSAGE_LEN]
            try:
                await update.message.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(chunk)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages and image documents — save to disk, inject path into Claude."""
    if not _is_authorized(update):
        return

    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    global _last_chat_id
    _last_chat_id = chat_id

    # Determine source: direct photo or image sent as document
    is_document = bool(update.message.document)
    caption = update.message.caption or ""

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)

    try:
        if is_document:
            file_obj = await context.bot.get_file(update.message.document.file_id)
            short_id = update.message.document.file_id[:8]
            # Preserve original extension if available
            orig_name = update.message.document.file_name or ""
            ext = Path(orig_name).suffix.lower() or ".jpg"
        else:
            # photo[] is sorted smallest → largest; take the last (highest res)
            photo = update.message.photo[-1]
            file_obj = await context.bot.get_file(photo.file_id)
            short_id = photo.file_id[:8]
            ext = ".jpg"

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}_{short_id}{ext}"
        save_path = SCREENSHOTS_DIR / filename

        await file_obj.download_to_drive(str(save_path))
        logger.info("Photo saved: %s (from user %s)", save_path, uid)

    except Exception as e:
        logger.error("Failed to download photo from user %s: %s", uid, e)
        await update.message.reply_text(f"❌ Failed to save image: {e}")
        return

    await update.message.reply_text(f"📸 Image saved — analysing...")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Build prompt: tell Claude where the image is, include any caption
    prompt_parts = [
        f"[Image saved at: {save_path}]",
    ]
    if caption:
        prompt_parts.append(f"[Caption: {caption}]")
    prompt_parts.append(
        "Please read and analyse this image using your Read tool. "
        "Describe what you see, flag any important information (e.g. news headlines, "
        "biased sources, chart patterns, trade ideas, screenshots of data), and give "
        "your honest assessment of its relevance to our Sniper system."
    )
    prompt = "\n".join(prompt_parts)

    response = await query_claude(prompt, uid, chat_id, context.bot)

    if response:
        for i in range(0, len(response), MAX_MESSAGE_LEN):
            chunk = response[i:i + MAX_MESSAGE_LEN]
            try:
                await update.message.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(chunk)


def _is_authorized(update: Update) -> bool:
    """Check if the user is authorized. Rejects all users if ALLOWED_IDS is not configured."""
    if not ALLOWED_IDS:
        logger.error("ALLOWED_TELEGRAM_USER_IDS is not set — rejecting all requests for safety")
        return False
    uid = update.effective_user.id
    if uid not in ALLOWED_IDS:
        logger.warning("Unauthorized access attempt from user %s", uid)
        asyncio.create_task(
            update.message.reply_text("🔒 Not authorized.")
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Starting Sniper AI Bot (primary=%s, escalation=%s, fallback=%s, bloat_threshold=%dKB)",
                PRIMARY_MODEL, ESCALATION_MODEL, FALLBACK_MODEL, SESSION_BLOAT_BYTES // 1024)
    logger.info("Working directory: %s", WORKING_DIR)
    logger.info("Authorized user IDs: %s", ALLOWED_IDS or "ANY (not locked down!)")

    # Pick the conversation back up where it left off across restarts.
    _load_sessions()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("brain", cmd_brain))
    app.add_handler(CommandHandler("compact", cmd_compact))
    app.add_handler(CommandHandler("world", cmd_world))
    app.add_handler(CommandHandler("regime", cmd_regime))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("hard", cmd_hard))
    app.add_handler(CommandHandler("fast", cmd_fast))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_photo))

    # Graceful shutdown
    def shutdown_handler(sig, frame):
        logger.info("Received %s, shutting down...", signal.Signals(sig).name)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    logger.info("Bot is polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
