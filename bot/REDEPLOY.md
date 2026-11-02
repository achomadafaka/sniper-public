# SDK Claude Channel (`bot/relay.py`) — Redeploy Guide

The **Sniper AI Bot**: a Telegram ↔ Claude Code relay via the Claude Agent SDK. You DM the
bot, it runs Claude Code (all tools + MCP servers) in the repo and streams responses back —
session continuity, live tool-call streaming, Sonnet default, Opus escalation, and Haiku fallback.

This is checked into the repo, so it already travels with the codebase to any host. This file
is the "make it run" checklist for a fresh VPS (or locally).

## 0. Status on the current (local) host — set up 2026-07-14
Everything below is already done on `/home/bigpapi/Desktop/Sniper` **except the credentials**.
The unit is installed but intentionally left disabled: without `TELEGRAM_BOT_TOKEN_AI`,
`relay.py` raises `KeyError` at import and `Restart=on-failure` would crash-loop it.
To finish: fill the two creds in `.env` (§2), then `systemctl --user enable --now sniper-bot.service`.

## 1. Dependencies
```bash
pip install -r bot/requirements.txt         # python-telegram-bot>=21, claude-agent-sdk>=0.1
# Claude Code CLI must be installed + authenticated on the host (the SDK shells out to it).
```
⚠️ On this host the repo `.venv` is **EMPTY**. The interpreter with the deps (and the one every
cron here uses) is `/home/bigpapi/.pyenv/versions/3.13.0/bin/python3`. That is what the unit's
`ExecStart` and the `BOT_PYTHON` env var both point at — do not "fix" them back to `.venv`.

## 2. Required env (put in the repo `.env` — `relay.py` loads it itself via python-dotenv)
| var | purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN_AI` | **the bot token** for THIS relay bot (from @BotFather) |
| `ALLOWED_TELEGRAM_USER_IDS` | comma-sep whitelist of Telegram user ids allowed to talk to it (get yours from @userinfobot). **Unset ⇒ the bot rejects everyone** (fail-closed, deliberate) |
| `BOT_WORKING_DIR` | repo path the bot runs Claude in (default `~/sniper`; on this box `/home/bigpapi/Desktop/Sniper`) |
| `BOT_PYTHON` | interpreter for the scripts the bot shells out to (brain/checkpoint/world_analyst). Defaults to `.venv/bin/python3` — **must be overridden on this host**, see §1 |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` | shared-memory store (see `bot/supabase_helper.py`) |
| `BOT_PRIMARY_MODEL` / `BOT_ESCALATION_MODEL` / `BOT_FALLBACK_MODEL` | optional overrides (defaults come from `sniper.settings.models`: Sonnet / Opus / Haiku) |

The unit deliberately has **no `EnvironmentFile`** — systemd's parser is stricter than dotenv's
and chokes on some values in `.env`. Let `relay.py` read `.env` itself.

## 3. Launch (systemd --user)
`systemd/sniper-bot.service` is now pathed for this host (no edits needed).
```bash
cp systemd/sniper-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sniper-bot.service
systemctl --user status sniper-bot.service
journalctl --user -u sniper-bot -f          # follow logs
```
### Memory — the old "relay.py leaks" note was wrong
Measured 2026-07-14: **relay.py sits at ~62 MB and does not leak.** The memory is spent on the
**Claude CLI child the SDK spawns per query — ~353 MB for a single trivial query** — and
`MemoryMax` is enforced on the whole cgroup, so those children were eating the cap. A real
conversation peaked at 497 MB against the old 1200 MB ceiling.

That ceiling is now actively dangerous, because the agent has `Task` and `Workflow`: it can fan
out subagents, and **each subagent is another ~350 MB CLI process in the same cgroup**. Four
parallel agents would exceed 1200 MB and systemd would kill the *entire service* mid-reply.
Current settings (host has 30 GB; the real-money runner and desktop still need room):

| setting | value | why |
|---|---|---|
| `MemoryHigh` | 6G | soft — kernel reclaims under pressure before anything dies |
| `MemoryMax` | 8G | hard bound on a runaway fan-out |
| `OOMPolicy` | `continue` | an OOM-killed **child** must not tear down the bot; worst case is one failed query |
| `Restart` / `StartLimitIntervalSec` | `always` / `0` | never give up and leave the bot silently dead |

Sessions persist to `data/bot_sessions.json`, so a restart (OOM, crash, deploy) resumes the
conversation instead of starting cold. Do NOT go back to an in-memory-only session dict.

To keep it alive when you're logged out: `loginctl enable-linger bigpapi`.
Note every *other* `sniper-*.service` on this box is disabled — the rest of the stack moved to
cron watchdogs. A fail-closed cron watchdog also exists at `scripts/sniper_bot_watchdog.sh` if
you ever prefer that route; don't run both.

## 3a. Capability parity — the agent is a FULL Claude Code session
Verified 2026-07-14 by booting `_base_opts()` and reading the SDK's `init` message:
**77 tools (44 MCP) · 29 subagents · 76 skills/slash-commands · Skill + Task + Workflow.**

`_base_opts()` in `relay.py` is the single source of truth. Four fields carry the parity —
do not "tidy" them away:

| field | why |
|---|---|
| `setting_sources=["user","project","local"]` | It used to be `["project"]` only, which silently dropped `~/.claude/CLAUDE.md`, the enabled plugins, and **all** of `.claude/settings.local.json` (30 permission rules, `enableAllProjectMcpServers`, the `secret_file_guard` + `brain_ambient` RAG hooks). The SDK treats `None` as "all sources" — `["project"]` was *narrower than default*. |
| `system_prompt={"type":"preset","preset":"claude_code","append":…}` | A bare **string** here **replaces** Claude Code's system prompt. That's what it used to be, so the bot ran with *no* Claude Code system prompt at all. `append` keeps the preset **and** its dynamic sections (working dir, auto-memory, git status). |
| `skills="all"` | Skills were never enabled. Also wires up the `Skill` tool automatically. |
| model tiers from `sniper.settings.models` | Sonnet is the default, `/hard` or `/model opus` escalates, and Haiku is the fallback. The relay intentionally does not request the `[1m]` variant; session compaction controls transcript growth. |

Self-scheduling / proactive-messaging ("clawdbot-like") comes free with the preset:
`CronCreate` / `CronList` / `ScheduleWakeup` / `PushNotification` / `Monitor` / `SendMessage`.

Notes on the MCP surface (these apply to an interactive session too, not just the bot):
- The `init` message is an **early snapshot** — MCP servers are still connecting when it fires,
  so the tool count it reports varies run to run (seen: 30/0, 51/18, 77/44). This is a
  cold-start race, not a capability loss; the servers register as they come up. Verified
  end-to-end that the agent really does call `mcp__sniper__get_runtime_health` and get live
  data back. Don't panic at a low number in `init`.
- `claude.ai` Gmail / Drive / Calendar — **needs-auth**. They authenticate interactively, so a
  headless bot won't have them. Nothing to fix.
- **Removed 2026-07-14** (were dead config):
  - `plugin:github` — its `.mcp.json` wants `GITHUB_PERSONAL_ACCESS_TOKEN`, which is set
    nowhere, so the server failed on every startup. It's also redundant: `gh` is authenticated
    (scopes `repo`, `workflow`) and the harness tells agents to use `gh` for GitHub anyway.
    Disabled in `~/.claude/settings.json`. To restore: flip it back to `true` **and** export a
    PAT — flipping it alone just reinstates the failure.
  - `openclaw-gateway.service` — `ExecStart` pointed at
    `…/node_modules/openclaw/dist/index.js`, which no longer exists (only `clawhub` remains in
    `.npm-global`). Unit deleted from `~/.config/systemd/user/`. It was already disabled +
    inactive, so nothing stopped running. Reinstall OpenClaw if you ever want the gateway back.

## 3b. ⚠️ What this bot actually is
`relay.py` runs Claude Code with `permission_mode="bypassPermissions"` — unrestricted shell — in
a repo on a host that is **trading real money** (the memecoin runner). Anyone who can DM the bot
from a whitelisted Telegram ID has that shell. `ALLOWED_TELEGRAM_USER_IDS` is the *only* thing
standing between a stranger and the wallet keys. Keep it tight; keep the token secret.
The `SYSTEM_PROMPT` was rewritten 2026-07-14 to tell the agent the truth about this host
(local, cron-driven, real money live) — it previously described the dead VPS and systemd units.

## 4. Getting a bot token
The old tokens died with the VPS. Either recover the old bot's token (@BotFather → your bot →
*API Token*) or create a fresh bot (@BotFather → `/newbot`, ~2 min). Paste it into `.env` as
`TELEGRAM_BOT_TOKEN_AI`. The token is a credential — set it yourself; never commit it.

## 5. One bot can also push the memecoin front-run alerts
The relay **polls** (`getUpdates`); the memecoin alerter only **sends** (`sendMessage`). Those
don't conflict, so the SAME bot token can do both. To reuse this bot for alerts, also set
`TELEGRAM_BOT_TOKEN` = the same token and `TELEGRAM_CHAT_ID` = your user id (the memecoin
`MEMECOIN_TG_*` knobs fall back to those), then `MEMECOIN_TG_ALERTS=1`. Or run a separate
dedicated alert bot — your call. See `docs/superpowers/specs/2026-07-08-memecoin-thesis-capture-and-alert-latency-design.md` §2.
