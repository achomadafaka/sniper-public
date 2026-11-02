"""Central model-tier registry so an Anthropic model bump is a one-line change here — not
another repo-wide grep sweep.

Anthropic does not expose a floating "latest" alias (models are version-pinned by design so
behavior upgrades are opt-in). We approximate that with named tiers whose defaults live here:
`OPUS`, `SONNET`, `HAIKU`, `FAST`. Each accepts an env override with the tier name — e.g.
`SNIPER_OPUS=claude-opus-4-9` flips every Opus caller without a code change.

Callers import the constants, never the string literal:

    from sniper.settings.models import OPUS, SONNET
    opts = ClaudeAgentOptions(model=OPUS, ...)

Domain-specific env vars (BOT_PRIMARY_MODEL, ANALYST_MODEL) still win where they exist — this
module is the default, not a policy that overrides operator configuration.
"""
from __future__ import annotations

import os

# Frontier / reasoning-heavy default. Every analyst / reviewer / scoring agent that historically
# hardcoded a 4-7 / 4-8 string should read from here instead.
OPUS = os.getenv("SNIPER_OPUS", "claude-opus-5")

# General-purpose default for chat + moderate reasoning; 3-5x faster + much cheaper than Opus.
# The Telegram relay default (bot/relay.py) also lands here.
SONNET = os.getenv("SNIPER_SONNET", "claude-sonnet-5")

# Fast + cheap tier for classifier-style workloads, trivial-message replies, and rate-limit
# fallback where PRIMARY = Sonnet.
HAIKU = os.getenv("SNIPER_HAIKU", "claude-haiku-4-5-20251001")

# Convenience alias — same as HAIKU today; kept as a distinct name so callers that want "the
# cheapest good-enough tier" don't accidentally bump to Haiku 5 when we upgrade the classifier
# workload separately from the fallback tier.
FAST = os.getenv("SNIPER_FAST", HAIKU)

# Default when a caller has no strong preference. Sonnet strikes the right balance.
DEFAULT = os.getenv("SNIPER_DEFAULT_MODEL", SONNET)


__all__ = ["OPUS", "SONNET", "HAIKU", "FAST", "DEFAULT"]
