"""Config knobs for the memecoin copy-mirror (loaded from env)."""
import functools
from sniper.settings.known_group import KNOWN_GROUP_LAUNCH_V1
import hashlib
import json
import logging
import math
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sniper.util.state_paths import state_path, state_root

MEMECOIN_FUNDED_ONLY = os.getenv("MEMECOIN_FUNDED_ONLY", "0").strip().lower() in {
    "1", "true", "yes"}


LIFI_4663_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "execution" / "lifi_4663_manifest.json"
)
_LIFI_MODES = {"disabled", "public_probe", "authenticated_funded"}
_LIFI_AUTHORITY_KEYS = {
    "abi", "code_hash", "selector", "spender", "target", "tool", "type", "types",
}


def _valid_hex(value, size):
    return isinstance(value, str) and len(value) == size + 2 and value.startswith("0x") \
        and all(char in "0123456789abcdef" for char in value[2:])


def load_lifi_manifest(path=LIFI_4663_MANIFEST_PATH):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("LI.FI manifest is absent or invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("LI.FI manifest must be an object")
    top = value.get("top_level")
    steps = value.get("steps")
    reject = value.get("reject")
    if value.get("version") != 1 or value.get("chain_id") != "4663":
        raise RuntimeError("LI.FI manifest has the wrong version or chain")
    if value.get("allow_exchanges") != ["nordstern"]:
        raise RuntimeError("LI.FI manifest has invalid exchange authority")
    if value.get("fee") != {"numerator": 25, "denominator": 10_000, "integrator_fee": 0}:
        raise RuntimeError("LI.FI manifest has invalid fee authority")
    if not isinstance(top, dict) or not _LIFI_AUTHORITY_KEYS <= set(top):
        raise RuntimeError("LI.FI manifest has incomplete top-level authority")
    if top.get("type") != "lifi" or top.get("tool") != "nordstern":
        raise RuntimeError("LI.FI manifest has invalid top-level authority")
    if not _valid_hex(top.get("target"), 40) or not _valid_hex(top.get("spender"), 40) \
            or not _valid_hex(top.get("selector"), 8) or not _valid_hex(top.get("code_hash"), 64) \
            or not _valid_hex(top.get("facet"), 40) \
            or not _valid_hex(top.get("facet_code_hash"), 64):
        raise RuntimeError("LI.FI manifest has invalid top-level code authority")
    if not isinstance(steps, list) or len(steps) != 2:
        raise RuntimeError("LI.FI manifest must contain two steps")
    if [(step.get("tool"), step.get("type")) for step in steps if isinstance(step, dict)] != [
            ("feeCollection", "protocol"), ("nordstern", "swap")]:
        raise RuntimeError("LI.FI manifest has invalid step authority")
    for step in steps:
        if not _LIFI_AUTHORITY_KEYS <= set(step) \
                or not _valid_hex(step.get("approve_to"), 40) \
                or not _valid_hex(step.get("target"), 40) \
                or not _valid_hex(step.get("spender"), 40) \
                or not _valid_hex(step.get("selector"), 8) \
                or not _valid_hex(step.get("code_hash"), 64):
            raise RuntimeError("LI.FI manifest has incomplete step authority")
    swap = steps[1]
    if swap.get("abi") != "IceCreamSwap()" or swap.get("types") != [] \
            or not _valid_hex(swap.get("executor"), 40) \
            or not _valid_hex(swap.get("executor_code_hash"), 64):
        raise RuntimeError("LI.FI manifest has incomplete Nordstern authority")
    if not isinstance(value.get("abis"), dict) \
            or set(value["abis"]) != {"allowance", "approve", "facet_address"}:
        raise RuntimeError("LI.FI manifest has incomplete ABI authority")
    if not isinstance(reject, dict) or not reject.get("addresses") or not reject.get("selectors"):
        raise RuntimeError("LI.FI manifest has incomplete reject authority")
    if any(not _valid_hex(address, 40) for address in reject["addresses"]) \
            or any(not _valid_hex(selector, 8) for selector in reject["selectors"]):
        raise RuntimeError("LI.FI manifest has invalid reject authority")
    if set(reject["addresses"]) != {
            "0x000000000022d473030f116ddee9f6b43ac78ba3",
            "0x8eabb4e117fb70b346592e013855f6d825f50af1",
    }:
        raise RuntimeError("LI.FI manifest has incomplete permit reject authority")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return value, hashlib.sha256(canonical.encode()).hexdigest()


def memecoin_lifi_mode():
    mode = os.getenv("MEMECOIN_LIFI_MODE", "disabled").strip().lower()
    if mode not in _LIFI_MODES:
        raise RuntimeError("MEMECOIN_LIFI_MODE is invalid")
    return mode


def validate_lifi_config(*, conn=None, custody_wallet=None):
    _manifest, manifest_hash = load_lifi_manifest()
    mode = memecoin_lifi_mode()
    if mode != "authenticated_funded":
        return None
    errors = []
    api_url = os.getenv("MEMECOIN_LIFI_API_URL", "https://li.quest/v1/quote").strip()
    api_key = os.getenv("MEMECOIN_LIFI_API_KEY", "").strip()
    proof_rpc = os.getenv("MEMECOIN_LIFI_PROOF_RPC_URL_4663", "").strip()
    release_sha = os.getenv("MEMECOIN_LIFI_ACTIVE_RELEASE_SHA", "").strip()
    active_hash = os.getenv("MEMECOIN_LIFI_ACTIVE_MANIFEST_HASH", "").strip()
    deploy_path = os.getenv("SNIPER_MEMECOIN_DEPLOY_MANIFEST", "").strip()
    try:
        deploy = json.loads(Path(deploy_path).read_text())
    except (OSError, TypeError, json.JSONDecodeError):
        deploy = None
    if not api_url.startswith("https://"):
        errors.append("MEMECOIN_LIFI_API_URL must use HTTPS")
    if not api_key:
        errors.append("MEMECOIN_LIFI_API_KEY is required")
    if not proof_rpc.startswith("https://"):
        errors.append("MEMECOIN_LIFI_PROOF_RPC_URL_4663 must use HTTPS")
    if not _valid_hex("0x" + release_sha, 40):
        errors.append("MEMECOIN_LIFI_ACTIVE_RELEASE_SHA must be a full SHA")
    if active_hash != manifest_hash:
        errors.append("MEMECOIN_LIFI_ACTIVE_MANIFEST_HASH does not match the release")
    if not isinstance(deploy, dict) or deploy.get("sha") != release_sha \
            or deploy.get("lifi_manifest_sha256") != manifest_hash:
        errors.append("LI.FI activation does not match the deploy manifest")
    activation = None
    if conn is None or not custody_wallet:
        errors.append("LI.FI activation record is required")
    else:
        from sniper.persistence import db_memecoin as m
        activation = m.active_lifi_release(
            conn, release_sha=release_sha, manifest_hash=manifest_hash,
            chain_id="4663", custody_wallet=custody_wallet,
        )
        if activation is None:
            errors.append("LI.FI activation record does not match the release")
    if errors:
        raise RuntimeError("invalid LI.FI configuration: " + "; ".join(errors))
    return activation

MEMECOIN_COPY_SIZE_USD = float(os.getenv("MEMECOIN_COPY_SIZE_USD", "100"))
# Single source of truth for the /feed poll cadence. 1000ms is the documented WAF floor; the
# runner clamps to it. This module previously defaulted to 500 while the runner defaulted to
# 1500 — two answers for one knob, and the 500 was never reachable. fomo's own web app refetches
# the same REST /feed every 15s (verified 2026-07-28 in their bundle), so 1000ms already polls
# The global feed uses this poll. The armed follow lane uses Fomo's WebSocket first.
MEMECOIN_POLL_FLOOR_MS = 1000
MEMECOIN_POLL_MS = max(MEMECOIN_POLL_FLOOR_MS, int(os.getenv("MEMECOIN_POLL_MS", "1000")))

# Funded Solana cluster admission. It stays off until the pinned worker passes activation checks.
MEMECOIN_REAL_CLUSTER_GATE_ENABLED = os.getenv(
    "MEMECOIN_REAL_CLUSTER_GATE_ENABLED", "0"
).strip().lower() in {"1", "true", "yes"}
MEMECOIN_CLUSTER_GATE_SOCKET = os.getenv(
    "MEMECOIN_CLUSTER_GATE_SOCKET",
    str(state_path("data", "memecoin_cluster_gate.sock")),
)

# Copy-logic thresholds (2026-07-05): entry chase guard + exit brackets.
MEMECOIN_CHASE_MAX_PCT = float(os.getenv("MEMECOIN_CHASE_MAX_PCT", "15"))
MEMECOIN_STOP_LOSS_PCT = float(os.getenv("MEMECOIN_STOP_LOSS_PCT", "60"))
MEMECOIN_TAKE_PROFIT_PCT = float(os.getenv("MEMECOIN_TAKE_PROFIT_PCT", "500"))

# Conviction-scaled sizing (2026-07-06): bet ~ sqrt(their_size/pivot), clamped.
# ⚠ REFUTED basis: the +0.38 corr(their_size, realized_pnl) that motivated this was a
# duplicate-event/spam artifact (see Supabase finding/memecoin-copy-duplicate-event-bug;
# N collapsed 187->6, CI includes 0). Do NOT enable on that evidence. Kept OFF; needs a
# fresh, dedup-clean forward study before it means anything.
MEMECOIN_CONVICTION_SIZING = os.getenv("MEMECOIN_CONVICTION_SIZING", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_SIZE_PIVOT_USD = float(os.getenv("MEMECOIN_SIZE_PIVOT_USD", "20000"))  # their_size where mult=1.0
MEMECOIN_SIZE_MIN_MULT = float(os.getenv("MEMECOIN_SIZE_MIN_MULT", "0.5"))
MEMECOIN_SIZE_MAX_MULT = float(os.getenv("MEMECOIN_SIZE_MAX_MULT", "3.0"))

# --- fomo account analyzer (2026-07-06) ---
# fomo's /v2/users/{id}/swaps has NO working pagination — it returns the newest
# <=100 swaps and ignores page/offset/cursor. SWAP_LIMIT is explicit; the cap is
# fomo's. SLEEP_S spaces out per-trader calls in the --all batch (WAF safety).
MEMECOIN_ANALYZER_SWAP_LIMIT = int(os.getenv("MEMECOIN_ANALYZER_SWAP_LIMIT", "100"))
# 2s base spacing — the analyzer shares fomo's WAF budget with the live runner,
# so give it room; the CLI also backs off + retries on 429.
MEMECOIN_ANALYZER_SLEEP_S = float(os.getenv("MEMECOIN_ANALYZER_SLEEP_S", "2.0"))

# --- honest exit-fill slippage model (2026-07-06 review fix) ---
# Paper exits previously filled at DexScreener mid with ZERO price impact while
# entries paid Jupiter slippage -> inflated PnL. Model exit (and buy-fallback)
# impact as a constant-product-ish fraction k*notional/liquidity, capped. This is
# a conservative FLOOR (real impact usually higher); unknown liquidity -> cap.
MEMECOIN_SLIPPAGE_K = float(os.getenv("MEMECOIN_SLIPPAGE_K", "1.0"))
MEMECOIN_SLIPPAGE_CAP = float(os.getenv("MEMECOIN_SLIPPAGE_CAP", "0.5"))

# --- wallet-discovery funnel (2026-07-06) ---
# Capture non-curated large buys from the feed firehose as copy candidates. The
# curated list goes quiet; the feed streams hundreds of large-buy traders/hour we
# otherwise discard. MIN_USD keeps the aggregate to genuine large buys (the edge is
# large-trades-only). ENABLED is a kill switch for the capture pass.
MEMECOIN_DISCOVERY_ENABLED = os.getenv("MEMECOIN_DISCOVERY_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_DISCOVERY_MIN_USD = float(os.getenv("MEMECOIN_DISCOVERY_MIN_USD", "1000"))
MEMECOIN_FOLLOW_ACTIVITY_MIN_USD = float(os.getenv(
    "MEMECOIN_FOLLOW_ACTIVITY_MIN_USD", str(MEMECOIN_DISCOVERY_MIN_USD)))

# --- per-follow activity coverage poll + signal-source A/B (2026-07-06) ---
# The global /feed under-samples active traders (measured: Binkieee 11/18 large buys
# caught). This pass polls each curated follow's own /swaps to catch large buys the feed
# MISSED, copies them tagged signal_source='follow_activity' (vs 'global_feed'), so paper
# PnL can be compared by source over time. These signals are LATE (poll-cadence, post-herd)
# by design — the experiment measures whether the real-time feed's speed actually pays.
MEMECOIN_FOLLOW_ACTIVITY_ENABLED = os.getenv("MEMECOIN_FOLLOW_ACTIVITY_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_FOLLOW_ACTIVITY_MAX_AGE_S = float(os.getenv("MEMECOIN_FOLLOW_ACTIVITY_MAX_AGE_S", "900"))   # only copy trades <15m old
MEMECOIN_FOLLOW_ACTIVITY_DEDUP_S = float(os.getenv("MEMECOIN_FOLLOW_ACTIVITY_DEDUP_S", "180"))       # skip if a feed event for (uid,mint) is within 3m
MEMECOIN_FOLLOW_ACTIVITY_PERIOD_S = float(os.getenv("MEMECOIN_FOLLOW_ACTIVITY_PERIOD_S", "300"))     # runner cadence
# --- fast lane (2026-07-15 latency cliff): <60s follow catches were +$79/copy 78% WR (honest
# alpha_b, n=9, whale-inflated — pre-registered read at >=30 closes) vs −$14 to −$16/copy at
# 60-600s. A second worker polls ONLY the effective armed set continuously (~31 wallets × 2s
# ≈ 62s/sweep + IDLE_S) so armed catches land in the +EV band; the 300s lane keeps covering
# the watch-only roster and EXCLUDES armed wallets while the fast lane runs (WAF budget —
# swap-id dedup makes lane overlap harmless either way). 0/off reverts to single-lane exactly.
MEMECOIN_FOLLOW_FAST_ENABLED = os.getenv("MEMECOIN_FOLLOW_FAST_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
# SLOW LANE KILLED by default (2026-07-28). Measured over 7d, split by lane:
#   armed / fast lane      p50 lag  29.0s  — 75% inside the +EV <60s band
#   watch-only / 300s lane p50 lag 266.6s  — only 15% inside it, i.e. 85% sits in the
#                                            -$14..-$16/copy bucket the 07-15 cliff identified
# The 300s cadence puts this lane in the losing band BY CONSTRUCTION — it cannot be tuned into
# the +EV band without becoming the fast lane. Its two jobs are already covered elsewhere: the
# global /feed carries both large_buy and large_sell for every wallet (so sell-mirror coverage
# survives), and armed wallets are on the fast lane. Set to 1 to restore the old behaviour.
MEMECOIN_FOLLOW_SLOW_ENABLED = os.getenv("MEMECOIN_FOLLOW_SLOW_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# Operator safety gate on the delayed-catch REAL leg (separate from the fast-lane SPEED toggle
# above). Off -> follow-activity opens NO real positions on either lane; paper copy, DELAYED-CATCH
# alerts, and sell-mirror coverage are untouched. The fast-lane latency-cliff edge (<60s catches
# +$79/copy 78% WR) is whale-inflated and pre-registered for judgement at >=30 closed armed follow
# copies (lag <120s) vs the global feed's +$3.88/copy — real money stays OFF until that read clears.
# FLIP to 1 only after the pre-registered read confirms the edge. Global-feed real entries are
# unaffected (this gate is scoped to signal_source='follow_activity').
MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED = os.getenv("MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_FOLLOW_FAST_IDLE_S = float(os.getenv("MEMECOIN_FOLLOW_FAST_IDLE_S", "5"))
# On-chain lane (stage 2, 2026-08-21): the standalone research watcher writes armed wallets'
# on-chain buys to onchain_wallet_detections ~2s after chain confirmation — before, and
# sometimes instead of, the fomo feed (two frankdegods buys never surfaced on fomo at all).
# This lane feeds fresh detections through the SAME real_hook gate chain as the feed lanes.
# Default OFF: the .env must arm it explicitly.
MEMECOIN_ONCHAIN_LANE_ENABLED = os.getenv(
    "MEMECOIN_ONCHAIN_LANE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}

# Axiom observation capture (2026-09-16, spec docs/superpowers/specs/2026-09-16-axiom-observation-
# lanes-design.md). A headless Chromium on an operator-logged-in profile records what the app
# receives. Observation only: nothing here reaches the funded entry hook.
AXIOM_CAPTURE_ENABLED = os.getenv("AXIOM_CAPTURE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# The operator's speed control: how often the directory / KOL / discovery pages reload. Push data
# over the app's own WebSocket costs no requests. The operator asked for full speed; the default
# starts here and the knob is theirs.
AXIOM_PAGE_INTERVAL_S = float(os.getenv("AXIOM_PAGE_INTERVAL_S", "600") or 600)
AXIOM_RAW_RETENTION_DAYS = float(os.getenv("AXIOM_RAW_RETENTION_DAYS", "7") or 7)
# Attach to the operator's own Chrome (started with --remote-debugging-port=9222) instead of a
# separate profile. Their login and Cloudflare clearance are reused; nothing is launched.
AXIOM_CDP_URL = (os.getenv("AXIOM_CDP_URL", "") or "").strip()
# A detection older than this never fires — it is a missed pop, not an entry.
MEMECOIN_ONCHAIN_LANE_MAX_AGE_S = float(os.getenv("MEMECOIN_ONCHAIN_LANE_MAX_AGE_S", "45"))
# J7 ON SOLANA, LIVE OR SHADOW (2026-09-28, operator: "fill only from the fast lanes"; Alpha GO).
# "shadow": a J7 BUY on Solana from a wallet the on-chain lane covers runs every entry gate and
# records its decision (terminal 'shadow', delivery_lane 'j7_shadow') but never reserves, quotes or
# signs; the on-chain and slot lanes fill it. J7 SELLS (mirror exits), EVM and uncovered wallets
# stay live. MEMECOIN_J7_SOLANA_SHADOW_UNCOVERED=1 shadows uncovered wallets too. Activation is a
# separate operator decision; the default is "live", today's behaviour.
MEMECOIN_J7_SOLANA_MODE = os.getenv("MEMECOIN_J7_SOLANA_MODE", "live").strip().lower()
MEMECOIN_J7_SOLANA_SHADOW_UNCOVERED = os.getenv(
    "MEMECOIN_J7_SOLANA_SHADOW_UNCOVERED", "0").strip().lower() in {"1", "true", "yes", "on"}
# SHADOW ONLY WHILE THE WATCHER IS UP (2026-09-28, Alpha). In shadow a covered J7 buy is filled by
# the on-chain lanes alone, so a dead or reconnecting Solana watcher dropped every covered buy with
# nothing said. J7 goes back to live whenever data/onchain_watch.heartbeat is older than this, is
# unreadable, or reports a failed or missing subscription. The watcher beats at least every 15 s
# (its receive timeout), so 45 s is three missed beats. 0 = no health gate (the pre-09-28 rule).
MEMECOIN_J7_SHADOW_WATCHER_MAX_AGE_S = float(
    os.getenv("MEMECOIN_J7_SHADOW_WATCHER_MAX_AGE_S", "45"))
# J7 SHADOW ON EVM BUYS, PER CHAIN (2026-09-29, Alpha GO; spec
# 2026-09-29-memecoin-j7-shadow-evm-buys-design.md). Comma-separated chain ids or names ("4663").
# A J7 BUY on a listed chain, by a wallet ALL of whose verified addresses there are in the transfer
# filter the EVM watcher's socket acked, records its decision and stops before the leg, as on Solana.
# Sells stay live (J7 is the only EVM sell signal). Empty = today. Parsed at call time by the runner
# (_j7_shadow_evm_chains); an unknown entry is warned once and ignored, and "solana" is ignored here.
MEMECOIN_J7_SHADOW_EVM_CHAINS = os.getenv("MEMECOIN_J7_SHADOW_EVM_CHAINS", "").strip()
# J7 AS DATA ONLY ON EVM, PER CHAIN (2026-09-30, operator; spec
# 2026-09-30-memecoin-j7-data-only-evm-design.md). On a listed chain J7 never touches a funded
# decision: every J7 follow buy shadows (no coverage or health fall-through to live) and a J7 sell
# never reaches a mirror exit. The D2 J7 cross-check always runs there (J7 as a liveness signal
# only; memecoin_onchain_evm_j7_xcheck_chains). Empty = today. Listing a chain is an operator call
# after j7_watcher_parity.py --gate passes for it.
MEMECOIN_J7_DATA_ONLY_EVM_CHAINS = os.getenv("MEMECOIN_J7_DATA_ONLY_EVM_CHAINS", "").strip()
# The EVM push beat is written on every DELIVERED head (4663 ~10/s, 56 ~2/s, 8453 every 2 s), so an
# older beat means no head for this long. 15 s = MEMECOIN_EVM_WS_STALE_S, where the watcher itself
# reconnects; the gap it leaves is recovered by the watcher's gap backfill.
MEMECOIN_J7_SHADOW_EVM_MAX_AGE_S = float(os.getenv("MEMECOIN_J7_SHADOW_EVM_MAX_AGE_S", "15"))
# E1 ON EVM: OUR OWN BAND MCAP (2026-09-29, Alpha GO; spec 2026-09-29-memecoin-e1-evm-own-mcap-design.md).
# Comma-separated chain ids. On a listed chain DexScreener's market_cap never sets the band: the
# source fill price x the coin's on-chain totalSupply does (basis `evm_source`), the supply read
# started beside the market-data GET. A coin nothing prices is refused mcap_unresolved, as EVM always
# has. Empty = today. Parsed at call time by the runner (_evm_chain_knob).
MEMECOIN_EVM_OWN_MCAP_CHAINS = os.getenv("MEMECOIN_EVM_OWN_MCAP_CHAINS", "").strip()
# How long the own stage may wait for the supply read AFTER the GET has returned. Past it the coin is
# unresolved (refused), never priced from anything else. Measured 2026-09-29: a funded 56 read is
# p50 28 ms / p90 65 ms and the GET p90 ~205 ms, so on 56 the read is normally done before the GET.
MEMECOIN_EVM_OWN_MCAP_WAIT_MS = float(os.getenv("MEMECOIN_EVM_OWN_MCAP_WAIT_MS", "150"))
MEMECOIN_ONCHAIN_LANE_POLL_S = float(os.getenv("MEMECOIN_ONCHAIN_LANE_POLL_S", "1"))
# The EVM wallet watcher re-verifies the pinned Relay contract code this often (2026-09-28); inside
# the window a detection makes no code reads. 0 = verify on every detection, as before.
MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S = float(os.getenv("MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S", "600"))
# The EVM wallet watcher re-reads a NULL tx or receipt (2026-09-28): the WS provider can deliver a
# Transfer before the read node has indexed its block (56: defibit null on 2 of 15 of publicnode's
# newest-block txs), and a null used to drop the buy in silence. Bounded by time AND attempts; the
# retry only runs on a null. 0 = one read, as before.
MEMECOIN_ONCHAIN_EVM_NULL_READ_RETRY_S = float(os.getenv("MEMECOIN_ONCHAIN_EVM_NULL_READ_RETRY_S", "2.0"))
MEMECOIN_ONCHAIN_EVM_NULL_READ_MAX_RETRIES = int(os.getenv("MEMECOIN_ONCHAIN_EVM_NULL_READ_MAX_RETRIES", "4"))
# J7 CROSS-CHECK (2026-09-29, spec 2026-09-29-memecoin-evm-watch-dead-subscription-design.md): a J7 event
# by an acked address whose tx the watcher's socket did not deliver within the grace forces one
# reconnect, at most one per min gap. Chains: memecoin_onchain_evm_j7_xcheck_chains().
MEMECOIN_ONCHAIN_EVM_J7_XCHECK_GRACE_S = float(os.getenv("MEMECOIN_ONCHAIN_EVM_J7_XCHECK_GRACE_S", "15"))
MEMECOIN_ONCHAIN_EVM_J7_XCHECK_MIN_GAP_S = float(os.getenv("MEMECOIN_ONCHAIN_EVM_J7_XCHECK_MIN_GAP_S", "300"))
# The watcher reads a just-delivered tx and receipt from MEMECOIN_ONCHAIN_EVM_TX_READ_URL_<chain> first
# (the WS provider's HTTP endpoint; empty = the read host only, as before) (2026-09-28). A 403 or 429
# there cools it for this long, so a rate-limiting provider is not hammered; any miss falls back to the
# read host. The timeout bounds one read there. MEMECOIN_ONCHAIN_EVM_TX_READ_WS_URL_<chain> (default empty)
# wins when set: the same reads over the provider's websocket, for a host whose HTTP refuses receipts
# (BSC publicnode, 2026-10-01). The sell shadow never reads over it. Same timeout; the cooldown below
# applies to a failed connect, this shorter one to a failed or slow read on an open connection.
MEMECOIN_ONCHAIN_EVM_TX_READ_WS_READ_COOLDOWN_S = float(os.getenv("MEMECOIN_ONCHAIN_EVM_TX_READ_WS_READ_COOLDOWN_S", "10"))
MEMECOIN_ONCHAIN_EVM_TX_READ_COOLDOWN_S = float(os.getenv("MEMECOIN_ONCHAIN_EVM_TX_READ_COOLDOWN_S", "60"))
MEMECOIN_ONCHAIN_EVM_TX_READ_TIMEOUT_S = float(os.getenv("MEMECOIN_ONCHAIN_EVM_TX_READ_TIMEOUT_S", "1.5"))
# The push watcher decides a transaction ONCE for every watched owner in it, so a later Transfer log
# of a tx decided within this many seconds is skipped (2026-09-28): one 4663 tx delivered as 11 logs
# cost 22 reads in 0.85 s, the burst that earns a publicnode 403. A null read is not a decision and is
# never skipped. 0 = read every log, as before.
MEMECOIN_ONCHAIN_EVM_TX_DEDUPE_S = float(os.getenv("MEMECOIN_ONCHAIN_EVM_TX_DEDUPE_S", "60"))
# GAP BACKFILL (2026-09-28, operator YES). A restart or reconnect leaves the push watcher blind to
# the blocks in between. On a named chain it re-reads that gap once with eth_getLogs {Transfer, to =
# a watched address} on the chain's Alchemy pool member (the only endpoint that serves a
# recipient-only query), newest first, clipped to the lane's age window and to this many calls.
# Empty (default) = off.
MEMECOIN_ONCHAIN_EVM_BACKFILL_CHAINS = frozenset(
    c.strip() for c in os.getenv("MEMECOIN_ONCHAIN_EVM_BACKFILL_CHAINS", "").split(",") if c.strip())
MEMECOIN_ONCHAIN_EVM_BACKFILL_MAX_CALLS = int(os.getenv("MEMECOIN_ONCHAIN_EVM_BACKFILL_MAX_CALLS", "45"))
# Cross-process wake (2026-09-27, operator: remove the detection->execution delay): the confirmed
# watcher sends a datagram after each committed detection and the lane sweeps at once instead of at
# its next poll (baseline p50 0.427 s, p90 0.860 s). The poll above stays as recovery. 0 = the
# rollback: pure polling, no socket bound (sniper/services/memecoin_onchain_wake.py).
MEMECOIN_ONCHAIN_LANE_WAKE = os.getenv(
    "MEMECOIN_ONCHAIN_LANE_WAKE", "1").strip().lower() not in {"0", "false", "no", "off"}
MEMECOIN_FOLLOW_FAST_FETCH_WORKERS = max(1, int(os.getenv(
    "MEMECOIN_FOLLOW_FAST_FETCH_WORKERS", "4")))
# Ban-guard cooldown: a 403 on a FRESH token (the 2026-08-03 app-ban signature) pauses ALL fomo
# calls this long. Hammering a ban is how a temporary block becomes a permanent one.
MEMECOIN_FOMO_BAN_COOLDOWN_S = float(os.getenv("MEMECOIN_FOMO_BAN_COOLDOWN_S", "900"))
# 2026-09-09: where the funded runner's copy signal comes from. "fomo" = the fomo /feed HTTP poll
# (three accounts banned by 2026-09-09). "j7" = J7Tracker's wallet-tracker Socket.IO relay of the
# tracked fomo roster (see sniper/feeds/j7_feed.py). With "j7" the runner never calls fomo.
MEMECOIN_SIGNAL_SOURCE = (os.getenv("MEMECOIN_SIGNAL_SOURCE", "fomo") or "fomo").strip().lower()

# --- copy-hardening: parallel exit-strategy arms (2026-07-07) ---
# Constants are LAUNCH GUESSES, recalibrated from Arm A's time-to-peak instrumentation.
MEMECOIN_A_ROLLOVER_SWEEPS = int(os.getenv("MEMECOIN_A_ROLLOVER_SWEEPS", "2"))
MEMECOIN_A_TRAIL_PCT = float(os.getenv("MEMECOIN_A_TRAIL_PCT", "15"))
MEMECOIN_A_STOP_PCT = float(os.getenv("MEMECOIN_A_STOP_PCT", "30"))
MEMECOIN_A_MAX_HOLD_S = float(os.getenv("MEMECOIN_A_MAX_HOLD_S", "900"))       # 15 min
MEMECOIN_B_STOP_PCT = float(os.getenv("MEMECOIN_B_STOP_PCT", "50"))
MEMECOIN_B_MAX_HOLD_S = float(os.getenv("MEMECOIN_B_MAX_HOLD_S", "43200"))     # 12 h
# Uniform paper max-hold backstop (2026-07-21): arm-agnostic upper bound on ANY paper position's
# age — control has no timecap by design, so a coin pinned between SL and TP rode open for 13
# days. Applied identically to every arm so cross-arm comparisons stay unbiased. Days; 0 = off.
MEMECOIN_PAPER_MAX_HOLD_D = float(os.getenv("MEMECOIN_PAPER_MAX_HOLD_D", "14"))
# Paper exit engine (spec 2026-10-08-memecoin-paper-exit-onchain-ticks-design.md), frozen into each
# study-arm position at ENTRY: dexscreener_sweep_v0 (today: the 15 s sweep on DexScreener/GT mids) or
# onchain_ticks_v1 (memecoin_paper_ticks: the pool's own on-chain state). An open position keeps the
# engine it opened under; switching back affects new entries only.
MEMECOIN_PAPER_EXIT_ENGINE = os.getenv("MEMECOIN_PAPER_EXIT_ENGINE", "dexscreener_sweep_v0").strip()
# A v1 tick older than this is no tick: price rails HOLD (time rails still fire). 5 s, the funded
# swap-mark freshness.
MEMECOIN_PAPER_TICK_MAX_AGE_S = float(os.getenv("MEMECOIN_PAPER_TICK_MAX_AGE_S", "5"))
# 56 (operator 2026-10-08): each open 56 v1 position's exact bag quote is read at most once per this
# many seconds on the dRPC 56 research key (drpc_paced). Its tick is fresh for this plus the max age.
MEMECOIN_PAPER_TICK_56_PERIOD_S = float(os.getenv("MEMECOIN_PAPER_TICK_56_PERIOD_S", "5"))
# Paper price path recorder (2026-09-27): memecoin_paper_path rows from the paper bracket's own
# price map. Measurement only. 0 = the rollback: nothing is written.
MEMECOIN_PAPER_PATH_ENABLED = os.getenv("MEMECOIN_PAPER_PATH_ENABLED", "1").strip().lower() in {"1", "true", "yes"}

# ---- V6 live-paper engine (2026-07-26 design; default OFF, funded-runner-independent) ----------
# Inert config for the isolated V6 live-paper book. This enable flag must NEVER be wired into a
# funded selector; it only gates the separate V6 paper worker. Parsed and validated together so a
# bad value fails closed at import (matching this module's fail-closed posture) instead of opening
# positions on a nonsensical cap/interval. Networks are a non-empty ordered subset of the two
# funded chains V6 paper runs on.
from sniper.settings.memecoin_v6_paper import (  # noqa: E402
    MEMECOIN_V6_EVENT_CATCHUP_MAX, MEMECOIN_V6_EVENT_COALESCE_MS,
    MEMECOIN_V6_EVM_FALLBACK_POLL_MS, MEMECOIN_V6_HEARTBEAT_S,
    MEMECOIN_V6_PAPER_ADMISSION_NETWORKS, MEMECOIN_V6_PAPER_DB,
    MEMECOIN_V6_PAPER_ENABLED, MEMECOIN_V6_PAPER_MAX_OPEN,
    MEMECOIN_V6_PAPER_NETWORKS, MEMECOIN_V6_PAPER_NOTIONAL_USD,
    MEMECOIN_V6_PAPER_QUOTE_MAX_AGE_S, MEMECOIN_V6_PAPER_SWEEP_S,
    MEMECOIN_V6_QUOTE_REFRESH_S, MEMECOIN_V6_SOLANA_BINDING_REFRESH_S,
    load_settings as _load_v6_paper_settings,
)
# trail_d (2026-07-15): mirror-sell + a trailing stop that ARMS only after +activate%, then exits
# `trail`% off the peak — meant to keep more of the fat tail than alpha_b's fixed rails and control's
# fixed TP. PAPER-ONLY A/B; never a real arm until it beats control/alpha_b on the eval gate.
# LAUNCH GUESSES, to be recalibrated from the arm's own peak/give-back instrumentation.
MEMECOIN_TRAIL_D_ACTIVATE_PCT = float(os.getenv("MEMECOIN_TRAIL_D_ACTIVATE_PCT", "30"))
MEMECOIN_TRAIL_D_TRAIL_PCT = float(os.getenv("MEMECOIN_TRAIL_D_TRAIL_PCT", "25"))
MEMECOIN_TRAIL_D_STOP_PCT = float(os.getenv("MEMECOIN_TRAIL_D_STOP_PCT", "50"))
MEMECOIN_TRAIL_D_MAX_HOLD_S = float(os.getenv("MEMECOIN_TRAIL_D_MAX_HOLD_S", "43200"))   # 12 h
# peak_lock_e (2026-07-16): forward PAPER challenger. It reuses the already-running activation,
# rollover, catastrophe-stop, and timecap registrations; only the exit trigger changes from
# trail_d's drawdown to "activated, then no new high for N sweeps while still profitable."
MEMECOIN_PEAK_E_ACTIVATE_PCT = float(os.getenv(
    "MEMECOIN_PEAK_E_ACTIVATE_PCT", str(MEMECOIN_TRAIL_D_ACTIVATE_PCT)))
MEMECOIN_PEAK_E_STALL_SWEEPS = int(os.getenv(
    "MEMECOIN_PEAK_E_STALL_SWEEPS", str(MEMECOIN_A_ROLLOVER_SWEEPS)))
MEMECOIN_PEAK_E_STOP_PCT = float(os.getenv(
    "MEMECOIN_PEAK_E_STOP_PCT", str(MEMECOIN_TRAIL_D_STOP_PCT)))
MEMECOIN_PEAK_E_MAX_HOLD_S = float(os.getenv(
    "MEMECOIN_PEAK_E_MAX_HOLD_S", str(MEMECOIN_TRAIL_D_MAX_HOLD_S)))
# combo_f (2026-07-23): partial de-risk + remainder trail — the P1/P2 combination from the
# stop/derisk redesign study (counterfactual +$85-92 on the n=29 funded cohort; see
# scripts/research/memecoin_partial_derisk_counterfactual.py). Sell FRACTION at the first
# +TRIGGER% crossing, trail the remainder TRAIL% off the running peak, catastrophe stop always
# live. Max-hold 0 = off (the arm-agnostic MEMECOIN_PAPER_MAX_HOLD_D backstop still applies),
# matching the study's remainder-keeps-actual-fate convention for comparability with control.
MEMECOIN_COMBO_F_TRIGGER_PCT = float(os.getenv("MEMECOIN_COMBO_F_TRIGGER_PCT", "20"))
MEMECOIN_COMBO_F_FRACTION = float(os.getenv("MEMECOIN_COMBO_F_FRACTION", "0.5"))
MEMECOIN_COMBO_F_TRAIL_PCT = float(os.getenv("MEMECOIN_COMBO_F_TRAIL_PCT", "30"))
MEMECOIN_COMBO_F_STOP_PCT = float(os.getenv("MEMECOIN_COMBO_F_STOP_PCT", "60"))
MEMECOIN_COMBO_F_MAX_HOLD_S = float(os.getenv("MEMECOIN_COMBO_F_MAX_HOLD_S", "0"))
# `funded_clone` — the Phase 0 BASELINE. It resolves its rails through the same function the
# funded contract builder uses (memecoin_risk.funded_policy_for_origin), so it carries whatever
# policy a funded entry from the same origin would be frozen under: band default, per-wallet
# override, copy arm, mirror-on-sell and all. Every other arm differs from funded on every rail,
# so a study using one of them as control measures the configuration difference and calls it a
# translation gap. Not in the default list — the operator enables it via MEMECOIN_ARMS.
#
# Available arms: control, momentum_a, alpha_b, scalp_c/scalp_c2, trail_d, peak_lock_e, and
# `real_shadow` — a PAPER
# arm mirroring the LIVE real-leg rails (MEMECOIN_REAL_* knobs: stop / TP / de-risk / timecap +
# mirror-on-sell) across the FULL curated roster, so the real config is evaluated on 10-20x the data
# the tiny real book yields. It reuses the exact real exit decision so it can't drift. Enable it
# operationally via MEMECOIN_ARMS in .env (kept out of the code default so a bare deploy stays lean).
DEFAULT_MEMECOIN_ARMS = [
    "control", "momentum_a", "alpha_b", "scalp_c", "scalp_c2", "trail_d", "peak_lock_e",
    "combo_f",
]
MEMECOIN_ARMS = [a.strip() for a in os.getenv(
    "MEMECOIN_ARMS", ",".join(DEFAULT_MEMECOIN_ARMS)).split(",") if a.strip()]
# Below this many independent clusters (min of matched events, coins, wallets) the Strategy Lab
# reports statistics but refuses ranking language. 30 is the existing project floor for arm
# verdicts (finding/memecoin-deep-review-2026-07-08), not a number invented for this surface.
MEMECOIN_PAPER_MIN_EVIDENCE_CLUSTERS = int(
    os.getenv("MEMECOIN_PAPER_MIN_EVIDENCE_CLUSTERS", "30"))

# PER-ARM CHAIN SCOPE (2026-09-22). The paper arms copy every event on every chain: the entry path
# reads `ev.network_id` only to price the fill, and no chain gate exists anywhere before it.
# MEMECOIN_PAPER_ARM_CHAINS_<ARM>="solana,4663" restricts one arm to those network ids. An arm with
# no knob runs on every chain, so an unset environment is byte-identical to the old behaviour.
#
# The gate sits BEFORE the executable quote, for the reason the "NO ARMS, NO WORK" note gives in
# `copy_event_to_arms`: the fill price comes from the Jupiter lite-api, so an event this lane will
# not trade must not buy a quote first.
MEMECOIN_PAPER_ARM_CHAINS = {
    k[len("MEMECOIN_PAPER_ARM_CHAINS_"):].strip().lower(): frozenset(
        x.strip() for x in v.split(",") if x.strip())
    for k, v in os.environ.items()
    if k.startswith("MEMECOIN_PAPER_ARM_CHAINS_") and k[len("MEMECOIN_PAPER_ARM_CHAINS_"):].strip()
}


def paper_arm_allows_chain(arm, network_id):
    """True when `arm` may copy an event on `network_id`. An arm with no knob allows every chain."""
    allowed = MEMECOIN_PAPER_ARM_CHAINS.get(str(arm or "").strip().lower())
    if not allowed:
        return True
    return str(network_id or "") in allowed


# ORIGIN SAMPLER (2026-09-22). Takes 1 event in N for the paper arms. 1 is every event, so the
# default changes nothing. The key is the ORIGIN (`origin_event_id`, which the entry trace reads
# from `ev.fomo_event_id`), so every arm sees the SAME subset and the arms stay comparable on a
# common event set — a per-arm roll would decorrelate them and make the A/B meaningless.
MEMECOIN_PAPER_ARM_SAMPLE_DIVISOR = max(1, int(
    os.getenv("MEMECOIN_PAPER_ARM_SAMPLE_DIVISOR", "1") or 1))


def paper_arm_samples_event(origin_event_id):
    """True when the paper arms take this origin. Deterministic, so a replay makes one decision.

    `zlib.crc32`, never `hash()`: PYTHONHASHSEED randomises `hash()` per process, so the sampled
    subset would differ between the runner and any tool that re-reads the same events.

    An origin with no id is always TAKEN. A missing key cannot be sampled stably, and dropping a
    signal is the destructive answer to that.
    """
    if MEMECOIN_PAPER_ARM_SAMPLE_DIVISOR <= 1:
        return True
    key = str(origin_event_id or "").strip()
    if not key:
        return True
    import zlib
    return zlib.crc32(key.encode()) % MEMECOIN_PAPER_ARM_SAMPLE_DIVISOR == 0

# PAPER ARM OPEN-COIN CAP (2026-09-23). The bound the measurement arms never had.
#
# On 2026-09-05 the operator added `real_shadow` as a measurement arm and removed it within the
# hour, recorded beside MEMECOIN_ARMS in .env: "its paper positions are marked through the same
# quoter/Jupiter lanes as funded reads; operator prefers no contention with money-deciding calls."
#
# THE COST IS PER OPEN COIN. `bracket_pass` builds ONE batched price map keyed by `coin_identity`
# over every open position, so a second position on a coin already open is free, and
# `copy_event_to_arms` buys one entry quote per EVENT however many arms run. Measured over
# 2026-09-16..2026-09-23T12:49Z with two arms alive: distinct open coins p50 26, p90 36, p99 40,
# peak 46 (open position rows p50 57, p90 80, peak 102). 60 therefore sits above the measured peak
# and cannot bind in normal operation; it exists to clip a pathological accumulation, which the
# hold tail makes real (paper hold p50 955 s but p99 136,522 s).
#
# The blind sub-cap counts non-Solana coins only, because Solana is served by the batched
# DexScreener call while a blind-chain coin enters the GeckoTerminal pass: one HTTP per ~30 coins
# plus a per-mint rotation capped at 8. 12 keeps the paper-only blind set inside one batch call and
# under two rotation passes; blind chains were 20% of the measured paper book.
#
# 0 disables either cap. An unset environment is byte-identical to the behaviour before them.
MEMECOIN_PAPER_ARM_MAX_OPEN_COINS = int(
    os.getenv("MEMECOIN_PAPER_ARM_MAX_OPEN_COINS", "60") or 0)
MEMECOIN_PAPER_ARM_MAX_OPEN_BLIND_COINS = int(
    os.getenv("MEMECOIN_PAPER_ARM_MAX_OPEN_BLIND_COINS", "12") or 0)

# --- selection gate thresholds (2026-07-07) — operator-read green/red, NOT a score ---
# lifetime green requires positive fomo totalPnL that isn't mostly unsold bags.
MEMECOIN_GATE_UNREALIZED_MAX_SHARE = float(os.getenv("MEMECOIN_GATE_UNREALIZED_MAX_SHARE", "0.5"))
# fresh = traded recently AND sustained activity (not a one-day fluke)
MEMECOIN_GATE_FRESH_MAX_AGE_H = float(os.getenv("MEMECOIN_GATE_FRESH_MAX_AGE_H", "48"))
MEMECOIN_GATE_FRESH_MIN_ACTIVE_DAYS = int(os.getenv("MEMECOIN_GATE_FRESH_MIN_ACTIVE_DAYS", "3"))
# minimum forward snapshots before a snapshot-derived window can be called green/red
MEMECOIN_GATE_SNAP_MIN_DAYS_7D = int(os.getenv("MEMECOIN_GATE_SNAP_MIN_DAYS_7D", "7"))
MEMECOIN_GATE_SNAP_MIN_DAYS_30D = int(os.getenv("MEMECOIN_GATE_SNAP_MIN_DAYS_30D", "30"))

# --- post-buy-pump instrumentation (2026-07-07) — the actual front-run edge signal ---
# forward-capture each large-buy sighting's price path -> per-trader "do their buys pump" read.
MEMECOIN_PUMP_ENABLED = os.getenv("MEMECOIN_PUMP_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
# 6h/24h horizons added 2026-07-11: the 1h ceiling made every TP/exit study horizon-limited
# (peaks beyond 1h invisible). Outcomes now mature in ~24.5h; hit-rates are per-horizon so
# pre-2026-07-11 rows (1h ceiling) are NOT comparable to newer rows at the long horizons.
MEMECOIN_PUMP_HORIZONS_S = [int(x) for x in os.getenv(
    "MEMECOIN_PUMP_HORIZONS_S", "60,300,900,3600,21600,86400").split(",") if x.strip()]
# full-feed retention (2026-07-11): append-only memecoin_firehose of every event, both sides,
# no size floor — episode reconstruction, unbiased lead-lag, cluster-entry research.
MEMECOIN_FIREHOSE_ENABLED = os.getenv("MEMECOIN_FIREHOSE_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_PUMP_HIT_PCT = float(os.getenv("MEMECOIN_PUMP_HIT_PCT", "20"))        # "pumped" threshold
MEMECOIN_PUMP_CAPTURE_PERIOD_S = float(os.getenv("MEMECOIN_PUMP_CAPTURE_PERIOD_S", "30"))
MEMECOIN_PUMP_ABANDON_S = float(os.getenv("MEMECOIN_PUMP_ABANDON_S", "1800"))  # mature overdue final mark
MEMECOIN_PUMP_MIN_SIGNALS = int(os.getenv("MEMECOIN_PUMP_MIN_SIGNALS", "5"))   # min matured to show/rank
MEMECOIN_CAPTURE_V2_ENABLED = os.getenv("MEMECOIN_CAPTURE_V2_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_CAPTURE_V2_MAX_MINTS = int(os.getenv("MEMECOIN_CAPTURE_V2_MAX_MINTS", "120"))
# --- tape-priced momentum_a on 4663 (2026-07-24) ---
# How stale the newest tape block may be and still price a position. 90s ~ 3 momentum sweeps;
# beyond that the tape is not "faster than DexScreener" in any useful sense.
MEMECOIN_TAPE_PRICE_MAX_AGE_S = float(os.getenv("MEMECOIN_TAPE_PRICE_MAX_AGE_S", "90"))

# --- exit-state materializer (2026-07-23; Phase 2 standalone process) ---
# DEFAULT ON as of 2026-07-23 (operator green-light). Log-only: the worker only ever writes the
# ticks DB and holds a READ-ONLY ledger connection. It is idle whenever there are no open real
# positions, and its block-time backfill shares the ingest RPC with the funded EVM leg
# (memecoin_evm_leg.py) — MEMECOIN_EXIT_BLOCKTIME_MAX_PER_TICK is the spend bound that keeps that
# contention capped. Tests pin this OFF in conftest so the default can never leak into a test run.
# Default is OFF: enabling it turns on block-time RPC spend and is an operator STOP condition
# (memecoin_exit_state_worker docstring). The default must fail SAFE — a deploy whose state-root
# .env omits the line must NOT silently start spending RPC (WO-11 smoke gate caught a "1" default
# that inverted this; live behaviour is unchanged because .env sets it explicitly to 0).
MEMECOIN_EXIT_STATE_ENABLED = os.getenv("MEMECOIN_EXIT_STATE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_EXIT_STATE_INTERVAL_S = float(os.getenv("MEMECOIN_EXIT_STATE_INTERVAL_S", "30"))
MEMECOIN_EXIT_BLOCKTIME_MAX_PER_TICK = int(os.getenv("MEMECOIN_EXIT_BLOCKTIME_MAX_PER_TICK", "64"))
# --- WO-7 execution tiering — ALL default OFF; each upgrade gates on measured fill-delta (20+/variant) ---
MEMECOIN_EXIT_PRIORITY_FEE_ENABLED = os.getenv(
    "MEMECOIN_EXIT_PRIORITY_FEE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_EXIT_PRIORITY_FEE_MICROLAMPORTS = int(
    os.getenv("MEMECOIN_EXIT_PRIORITY_FEE_MICROLAMPORTS", "0"))
MEMECOIN_EXIT_PRIORITY_FEE_CAP_MICROLAMPORTS = int(   # [bound] hard per-exit fee ceiling
    os.getenv("MEMECOIN_EXIT_PRIORITY_FEE_CAP_MICROLAMPORTS", "2000000"))
MEMECOIN_EXIT_DEPTH_AWARE_SLIPPAGE_ENABLED = os.getenv(
    "MEMECOIN_EXIT_DEPTH_AWARE_SLIPPAGE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_EXIT_DEPTH_MAX_RUNG_PCT = float(   # [bound] the depth-aware first rung can never exceed this
    os.getenv("MEMECOIN_EXIT_DEPTH_MAX_RUNG_PCT", "0.25"))
# WO-1 push lane. OFF until the deployment smoke and operator switch; pull remains authoritative.
MEMECOIN_HOT_EXIT_ENABLED = os.getenv(
    "MEMECOIN_HOT_EXIT_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_HOT_EXIT_MAX_DECISION_GAP_S = float(os.getenv(
    "MEMECOIN_HOT_EXIT_MAX_DECISION_GAP_S", "60"))
MEMECOIN_HOT_EXIT_PREWARM_PERIOD_S = float(os.getenv(
    "MEMECOIN_HOT_EXIT_PREWARM_PERIOD_S", "3"))
# WO-9 — per-chain EVM hot-lane block-poll period (4663/8453 are ~2s-block chains). The RPC runs off
# the event loop; the pull lane still bounds the worst-case decision gap. Default OFF via the flag.
MEMECOIN_HOT_EVM_POLL_S = float(os.getenv("MEMECOIN_HOT_EVM_POLL_S", "2"))
# WO-12A — off-loop prewarm scheduler: dedicated bounded executor (spare capacity, never the
# decode/sale threads) + a hard per-preview deadline. Small by design; prewarm may only consume
# spare capacity and must never delay a CUT, exact confirm, decode, WS processing, or the pull sweep.
MEMECOIN_HOT_PREWARM_WORKERS = int(os.getenv("MEMECOIN_HOT_PREWARM_WORKERS", "2"))
MEMECOIN_HOT_PREWARM_DEADLINE_S = float(os.getenv("MEMECOIN_HOT_PREWARM_DEADLINE_S", "3"))
# WO-2 — max transactions decoded to corroborate one vault-delta sale. In a structure-CUT flow
# window, more than this many sigs -> fail closed to the pull lane (never a fabricated sale).
MEMECOIN_HOT_DECODE_MAX_SIGS = int(os.getenv("MEMECOIN_HOT_DECODE_MAX_SIGS", "10"))
# Bound read-RPC enumeration before the independent pull lane takes over. Four 1000-row pages is
# far beyond a 60 s held-pool window in normal operation without letting one busy vault pin the WS
# consumer for minutes during provider trouble.
MEMECOIN_HOT_DECODE_MAX_PAGES = int(os.getenv("MEMECOIN_HOT_DECODE_MAX_PAGES", "4"))


def _lp_severity_bounds():
    """Validated per-chain replay registration. Empty/invalid/inverted entries stay degenerate."""
    raw = os.getenv("MEMECOIN_LP_SEVERITY_BOUNDS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out = {}
    for chain, value in parsed.items():
        if not isinstance(value, dict):
            continue
        try:
            minor = float(value["minor"])
            catastrophic = float(value["catastrophic"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0.0 < minor < catastrophic <= 1.0:
            out[str(chain).strip().lower()] = {
                "minor": minor, "catastrophic": catastrophic}
    return out


# WO-3 — registered by WO-7 replay independently per chain (>=10 decoded removals). Absent or
# malformed -> degenerate: every decoded burn is catastrophic -> CUT.
MEMECOIN_LP_SEVERITY_BOUNDS = _lp_severity_bounds()


def _opt_float(name):
    """Unset -> None. Present-invalid is preserved for startup validation to reject loudly."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return raw
    return value if math.isfinite(value) and value > 0 else raw


def _opt_int(name):
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return raw
    return value if value > 0 else raw


# WO-5B registration slots (fit by WO-7 replay, per the finite candidate grid). None -> conservative:
# the route feed never declares the route gone, and the executable-no-progress window never elapses,
# so age alone can never DERISK. No hand-picked constants.
MEMECOIN_ROUTE_GONE_STREAK = _opt_int("MEMECOIN_ROUTE_GONE_STREAK")
MEMECOIN_NO_PROGRESS_WINDOW_S = _opt_float("MEMECOIN_NO_PROGRESS_WINDOW_S")
# WO-11 (rev 2) — the REGISTERED finite structure-evidence window (seconds) that gates hot-ring
# time-eviction. Pivots (prior_low/high) consume the whole retained path, so bounding it changes
# structure-CUT behavior; eviction stays DISABLED (ring is count-backstopped, fail-closed to the pull
# lane on overflow) until an operator deliberately registers this window. Default UNSET -> None ->
# eviction disabled. Present-invalid is preserved and rejected at armed startup; a typo must not look
# like an intentional degenerate registration.
MEMECOIN_HOT_STRUCTURE_WINDOW_S = _opt_float("MEMECOIN_HOT_STRUCTURE_WINDOW_S")

# WO-10 — chains where the Uniswap-V3 depth decoder has been live-verified against captured logs.
# A V3 pool on an UNverified chain is flow/vol/structure-capable but never depth-capable (no v3_burn
# can CUT). A chain joins ONLY after its decoder is proven against real logs — never on faith.
#
# WO-10 CLOSED 2026-07-25: 4663 (Robinhood) is evidence-verified — real Uniswap-V3 Swap/Mint/Burn/
# Collect events were captured from the live chain (tests/fixtures/memecoin/evm/4663/, canonical pool
# 0x5d25149b…), and tests/test_memecoin_wo10_real_evm_evidence.py independently reproduces the
# canonical keccak topics, ABI field positions + signed tick, tx/block/log identity, in-range vs
# out-of-range burn severity, collect-never-removes-depth, and hot==pull decode parity. 8453 stays
# UNverified (no captured evidence). The env var can ADD chains but cannot remove an evidence-verified
# one — a proven decoder does not become unproven by config; disabling depth is an arming (WO-8) lever.
_V3_DEPTH_EVIDENCE_VERIFIED = frozenset({"4663"})
MEMECOIN_V3_DEPTH_VERIFIED_CHAINS = _V3_DEPTH_EVIDENCE_VERIFIED | frozenset(
    c.strip().lower() for c in os.getenv("MEMECOIN_V3_DEPTH_VERIFIED_CHAINS", "").split(",")
    if c.strip())
# V5-only (2026-07-25 retirement): absence means v5. An explicit non-v5 value is a stale config and
# fails validation at startup rather than silently selecting a retired engine.
MEMECOIN_EXIT_ENGINE_VERSION = os.getenv("MEMECOIN_EXIT_ENGINE_VERSION", "v5").strip().lower()
# --- versioned exit-engine funded authority (2026-07-24/25 preregs; flag-gated OFF) ---
# Chains where the selected quant engine is authoritative over funded
# exits, selected at RESERVATION time and frozen into the position's immutable funded contract
# (the "engine-envelope-v1" policy variant). Default "" = off everywhere: every existing/new
# reservation keeps the normal per-band policy, byte-identical, until an operator lists a chain
# here. A chain added later never touches an already-open position — only a NEW reservation reads
# this. Comma-separated normalized chain ids, e.g. "solana" or "4663".
MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS = {c.strip() for c in os.getenv(
    "MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS", "").split(",") if c.strip()}


def engine_authoritative_chains(*, env=None):
    """Read the operator kill flag from the mutable state root on every decision boundary.

    The pinned release's process environment is an import-time snapshot, so it cannot represent a
    live `.env` edit. The state-root file is authoritative; missing/unreadable/malformed state fails
    OFF. Reading never mutates ``os.environ`` and therefore cannot alter unrelated runtime config.
    """
    try:
        env_path = state_root(env) / ".env"
        value = None
        with env_path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, candidate = line.split("=", 1)
                if key.strip() == "MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS":
                    candidate = candidate.strip()
                    if candidate and candidate[0] in ("'", '"'):
                        if len(candidate) < 2 or candidate[-1] != candidate[0]:
                            return set()
                        candidate = candidate[1:-1]
                    elif candidate and candidate[-1] in ("'", '"'):
                        return set()
                    value = candidate
                    break
        if value is None:
            return set()
        return {chain.strip() for chain in value.split(",") if chain.strip()}
    except (OSError, ValueError, UnicodeError):
        return set()
# Canary bound (prereg §5: "enabling v3 on funded exits" requires an operator-approved N=5
# canary). The selected engine may DRIVE at most this many concurrently-open positions at once (oldest
# position id first); any engine-authoritative position beyond the bound runs the envelope's
# rails only (catastrophe / unquotable / timecap) — same as a non-capable engine state.
MEMECOIN_ENGINE_MAX_POSITIONS = int(os.getenv("MEMECOIN_ENGINE_MAX_POSITIONS", "5"))
# State-freshness gate (P0-1, 2026-07-24 activation-blocker review). The latest materialized
# MarketState for a position must be no older than this many seconds — AND match the position's
# mint/chain identity and the CURRENT feature-computation engine/feature version — before the v3
# selected engine may act on it. A stale or wrong-identity snapshot (a reused position_id across a DB
# reset/backup restore, or a row computed under older feature code) must fall back to the
# envelope's own rails, never sell against it (the "January-snapshot-sells-July-position" hazard).
MEMECOIN_ENGINE_STATE_MAX_AGE_S = float(os.getenv("MEMECOIN_ENGINE_STATE_MAX_AGE_S", "120"))
# Solana pull-tape spend bound (v2 prereg 2026-07-24): total getTransaction calls per worker pass
# across all open Solana positions, on the READ-ONLY endpoint. 0 disables the tape entirely.
MEMECOIN_SOL_TAPE_ENABLED = os.getenv(
    "MEMECOIN_SOL_TAPE_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
MEMECOIN_SOL_TAPE_MAX_TX_PER_TICK = int(os.getenv("MEMECOIN_SOL_TAPE_MAX_TX_PER_TICK", "40"))
# Widen the Solana pull tape beyond open REAL positions to open PAPER positions (same coins the
# funded rails see; the winner-regime evidence V6 needs). Observation-only; budget still bounds.
MEMECOIN_SOL_TAPE_INCLUDE_PAPER = os.getenv(
    "MEMECOIN_SOL_TAPE_INCLUDE_PAPER", "1").strip().lower() in ("1", "on", "true", "yes")
# Unactionable-CUT remap cooldown (2026-07-25): after the executable gate refuses an engine CUT,
# suppress re-mapping (not re-deciding/logging) for this long — kills the no-op propose/refuse
# loop on unquotable bags. 0 disables. [bound] 300s = well past transient RPC blips.
MEMECOIN_ENGINE_CUT_COOLDOWN_S = float(os.getenv("MEMECOIN_ENGINE_CUT_COOLDOWN_S", "300"))
# P0-A (2026-07-24): a derisk PENDING claim (derisk_pending_at) older than this with no sig and
# no fill = the executor died between claim and submit -> recovery releases it (rail re-arms).
# 600s [bound]: far past any executor ladder, far under the position's life.
MEMECOIN_DERISK_PENDING_STALE_S = float(os.getenv("MEMECOIN_DERISK_PENDING_STALE_S", "600"))
# HOW OLD A SUBMITTED DE-RISK MUST BE BEFORE THE RECOVERY PASS MAY SETTLE IT (2026-09-25, pos 64934).
# The recovery exists for executors that DIED or hit an unknown confirm. Without an age bound it
# also took LIVE ones: a manual de-risk runs on the main loop, persists its signature before
# broadcast, then waits up to 47 x 1 s in its confirm loop plus up to 3 s of USDC re-reads before it
# books -- and the fast lane's sweep, ~1 s later, confirmed the landed signature and cleared the
# claim the executor's booking needed. 64934's intent went 'confirmed' 0.72 s after submission and
# the fill was never booked. 120 s is more than twice the executor's longest in-flight window.
# 0 restores the old unbounded behaviour.
MEMECOIN_DERISK_RECOVERY_MIN_AGE_S = float(os.getenv("MEMECOIN_DERISK_RECOVERY_MIN_AGE_S", "120"))
# A manual close behind an unknown-confirm partial (73882, 2026-10-15: refused already_closing, then
# held 120 s by the age above) settles that partial itself: one confirm read under the executor's
# lock, no age gate; confirmed books it unpriced, failed clears it, anything else waits ~3 s and
# refuses. Never clears on not_found. 0 = the old refusal.
MEMECOIN_MANUAL_CLOSE_SETTLES_PARTIAL = os.getenv(
    "MEMECOIN_MANUAL_CLOSE_SETTLES_PARTIAL", "1").strip().lower() in {"1", "true", "yes", "on"}
# The same settle for the rails: real_exit_sweep skips a position behind an unknown partial, so no
# rail ran, the stop included, until the recovery above. The sweep offers it to a background thread
# (at most once per 2 s; no RPC on the exit tick); the next tick sweeps it. 0 = the old wait.
MEMECOIN_RAIL_CLOSE_SETTLES_PARTIAL = os.getenv(
    "MEMECOIN_RAIL_CLOSE_SETTLES_PARTIAL", "1").strip().lower() in {"1", "true", "yes", "on"}
# [inherited] same unsigned-claim lease as de-risk. Signed close claims are never lease-released.
MEMECOIN_CLOSE_PENDING_STALE_S = float(os.getenv("MEMECOIN_CLOSE_PENDING_STALE_S", "600"))
# Blind-chain probe capture (2026-07-16): DexScreener-blind chains (4663) get their forward tape
# from bounded GeckoTerminal batches in a background thread, never on the poll loop.
# 8 probes / 60s ≈ 0.13 rps; a 40-coin book fully rotates in ~5 min (anchor staleness ≤1h holds).
MEMECOIN_CAPTURE_BLIND_ENABLED = os.getenv(
    "MEMECOIN_CAPTURE_BLIND_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_CAPTURE_BLIND_PERIOD_S = float(os.getenv("MEMECOIN_CAPTURE_BLIND_PERIOD_S", "60"))
# GeckoTerminal's existing simple-token endpoint accepts 30 addresses in one call. Use the whole
# request before increasing request frequency: 8/97 coverage left eligible 4663 horizons ungraded.
MEMECOIN_CAPTURE_BLIND_MAX_MINTS = int(os.getenv("MEMECOIN_CAPTURE_BLIND_MAX_MINTS", "30"))
# GeckoTerminal blind-chain pricing (2026-07-16). GT indexes the Robinhood chain (4663) that
# DexScreener does not and is the sole live blind-chain price source. OFF means no blind-chain price.
# TTL is the shared
# price-cache window (the blind worker's batch warms the per-mint exit-sweep lookups); MIN_INTERVAL
# rate-guards the per-mint path so it cannot burst the free endpoint (≈20 req/min ceiling). A 429
# parks every GT caller for the bounded COOLDOWN window; cached prices remain readable meanwhile.
MEMECOIN_GT_ENABLED = os.getenv("MEMECOIN_GT_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
# TTL raised 15 -> 45 (2026-07-17): at 15s every ~15s bracket sweep was a guaranteed cache miss ->
# GT HTTP every sweep -> chronic 429 cooldowns that starved the V4 pool-discovery lookups AND left
# blind-chain marks 60s+ blind during each cooldown. 45s trades <=45s mark staleness for a ~3x HTTP
# cut and steady availability. GT prices only feed marks/triggers/mcap — executable entry/exit
# prices always come from the venue quoter, never GT.
MEMECOIN_GT_TTL_S = float(os.getenv("MEMECOIN_GT_TTL_S", "45"))
MEMECOIN_GT_MIN_INTERVAL_S = float(os.getenv("MEMECOIN_GT_MIN_INTERVAL_S", "3"))
MEMECOIN_GT_429_COOLDOWN_S = float(os.getenv("MEMECOIN_GT_429_COOLDOWN_S", "60"))
# Pool-liquidity fetch for Robinhood 4663 (the only on-chain liquidity source — GT's POOL endpoint,
# separate from the price call). DEFAULT OFF: verified 2026-07-18 that GT's FREE tier is already
# saturated by the runner's continuous pricing (shared per-IP budget), so every pool call 429s and
# would only add load that risks the funded pricing path. The mechanism (gt_pool_stats_for) is built
# and tested; flip this to "on" once a GT Pro/API key lifts the rate limit, and 4663 liquidity + the
# net-of-slippage exit MTM populate with no code change.
MEMECOIN_GT_POOL_LIQUIDITY = os.getenv("MEMECOIN_GT_POOL_LIQUIDITY", "off").strip().lower() in ("1", "on", "true", "yes")
# position-mark write dedupe (2026-07-17): memecoin_position_marks grew ~20k rows/2h on 4663 — one
# row per coin per ~15s sweep even when the TTL-cached price hadn't changed. A mark younger than
# this per (chain, mint) skips the INSERT. 0 disables. Exit decisions read the live price_map, not
# marks; marks are telemetry/replay tape, so 30s granularity is safe.
MEMECOIN_MARK_WRITE_MIN_INTERVAL_S = float(os.getenv("MEMECOIN_MARK_WRITE_MIN_INTERVAL_S", "30"))
# open-position tape floor (2026-07-22, Phase 2 Unit B): the 30s universe dedupe above is a
# RECORDING limit, and the Solana tape sits pinned against it (24h histogram: 64.8% of gaps in
# 30-35s, 0.1% below 30s). That cost real forensic ability — position 12110's executable peak
# (+36.06%) exceeded EVERY mark mid in its hold window (+32.59%), so 4663 rail decisions could not
# be reconstructed from the tape. Coins with an OPEN REAL position (<=MEMECOIN_MAX_CONCURRENT, so
# ~0.3% of the row cost the 30s floor was protecting against) record at this interval instead.
# 1s (2026-07-27, was 5): the recording floor must not become the new binding constraint once the
# fast lane ticks at 1s. Scoped to OPEN REAL positions only (<=7 rows), so the general 30s floor
# still protects the wide tape from row growth.
MEMECOIN_MARK_WRITE_MIN_INTERVAL_OPEN_S = float(
    os.getenv("MEMECOIN_MARK_WRITE_MIN_INTERVAL_OPEN_S", "1"))
# POST-CLOSE MARKS (2026-09-23 19:19Z, operator: "any token we enter, keep the data we read on it and
# keep reading it for at least 1 min 30 s after we close"). Every tape stopped at the close sweep, so
# the question "did it come back after the stop" had no instrument. A funded position closed within
# this window stays in the swap-mark work set and keeps the fine mark floor; nothing that decides
# reads a closed row. 0 disables; any positive value is floored at 90 s, the operator's minimum.
def _post_close_mark_s(raw):
    try:
        v = float(raw or 0)
    except (TypeError, ValueError):
        return 120.0
    return 0.0 if v <= 0 else max(90.0, v)


MEMECOIN_POST_CLOSE_MARK_S = _post_close_mark_s(os.getenv("MEMECOIN_POST_CLOSE_MARK_S", "120"))
# open-position quote fast lane (2026-07-22, Phase 2 Unit A/C). Default OFF: turning it on costs
# RPC (~1 batched JSON-RPC request per open position per tick against MEMECOIN_EVM_RPC_URL_<chain>)
# and the provider's limits are the operator's call. ENABLED alone is SHADOW — it only writes
# source='fastlane_rpc' marks. DECIDES is the separate flip that lets rails consume those quotes;
# that changes WHEN rails evaluate on real money, so it is announced before it is turned on.
MEMECOIN_FASTLANE_ENABLED = os.getenv(
    "MEMECOIN_FASTLANE_ENABLED", "0").strip().lower() in ("1", "on", "true", "yes")
# Freshness bound on the liquidity a stuck-bag write-off may rest on. The stuck-bag detector reads
# liquidity from the mark tape the funded runner already writes for each open real position, and a
# DEAD verdict recommends CUTTING the bag -- so a mark older than this reads as UNKNOWN, never as
# "untradeable". Same 600s contract radar_quote_map applies to its own cache.
MEMECOIN_STUCK_MARK_MAX_AGE_S = float(os.getenv("MEMECOIN_STUCK_MARK_MAX_AGE_S", "600"))
# This read-only lane changes only the nominal mark source. Exact confirmation stays authoritative.
MEMECOIN_SOLANA_SWAP_MARK_ENABLED = os.getenv(
    "MEMECOIN_SOLANA_SWAP_MARK_ENABLED", "1"
).strip().lower() in ("1", "on", "true", "yes")
# UNIT C (2026-07-27): when on, the funded rails DECIDE on the 1s fast lane instead of the ~31s
# bracket pass, and the slow pass stops deciding so the two lanes cannot race on stop_hits.
# COUPLED TO MEMECOIN_REAL_STOP_CONFIRM_SWEEPS -- that debounce counts SWEEPS, not seconds, so
# flipping this without raising the count silently cuts stop confirmation from ~62s (2 x 31s) to
# ~2s and lets a single bad tick market-sell into the wick. The up-spike quarantine does not help:
# it only filters ticks that read too HIGH. See the note on that setting.
# Exact-quote DEMAND gate (2026-07-27). The rails evaluate at the fast tick on the cheap mid and
# only buy an EXACT route quote when it matters: within NEAR_PCT points of a rail, or once per
# HEARTBEAT_S so unsellable detection stays alive for a position parked mid-band. PER_MINUTE is a
# hard backstop so a mis-scoped predicate can never take the provider down again (it did: ops
# streak 165 on 2026-07-27, 120 quotes/min against a ~60/min budget).
# ALWAYS (2026-09-02): quote every open funded position exactly on EVERY fast-lane tick, and skip
# the distance test entirely. Every branch of the demand gate reasons from the cheap mid, and a mid
# can be wrong rather than coarse: a funded 4663 position's DexScreener mid froze at one value for
# 21 marks over 24s while the executable price rose 51% past its take-profit. The gate declined
# correctly on that input and the take-profit never fired. That is self-sealing -- a frozen mid can
# never look near a rail -- so no staleness heuristic built on the same mid can catch it.
# Affordable now in a way it was not on 2026-07-27: MAX_CONCURRENT 3 at a 1s tick is 180 quotes/min
# against a 4663 endpoint measured at 4.5 req/s (270/min, p50 118ms, 201/201). Size
# MEMECOIN_EXACT_QUOTE_PER_MINUTE so its 75% rail bucket covers MAX_CONCURRENT x 60 with headroom.
# WARNING: this makes the STOP evaluate on the true sell-side executable price every tick, and on a
# nano coin the round-trip spread alone put a fresh position at -31.6% against a -35% stop. Review
# MEMECOIN_REAL_STOP_CONFIRM_SWEEPS before turning this on.
MEMECOIN_EXACT_QUOTE_ALWAYS = os.getenv(
    "MEMECOIN_EXACT_QUOTE_ALWAYS", "0").strip().lower() in ("1", "on", "true", "yes")
# Route series writer thread (2026-10-16, Alpha; Gamma PASS conditions). On: a dedicated daemon thread
# drains the route-quote buffer on its own autocommit connection every 2 s and writes the restart
# marker at start. Off (any value but 1/on/true/yes): no thread, no marker, the runner loop's
# maintenance pass flushes on the funded connection as before. Read once at runner start.
MEMECOIN_ROUTE_SERIES_WRITER_THREAD = os.getenv(
    "MEMECOIN_ROUTE_SERIES_WRITER_THREAD", "1").strip().lower() in ("1", "on", "true", "yes")
MEMECOIN_EXACT_QUOTE_NEAR_PCT = float(os.getenv("MEMECOIN_EXACT_QUOTE_NEAR_PCT", "10"))
MEMECOIN_EXACT_QUOTE_HEARTBEAT_S = float(os.getenv("MEMECOIN_EXACT_QUOTE_HEARTBEAT_S", "60"))
MEMECOIN_EXACT_QUOTE_PER_MINUTE = float(os.getenv("MEMECOIN_EXACT_QUOTE_PER_MINUTE", "30"))
# A 429 is the provider's own rate answer, and it outranks our chosen rate. Hold the exact-quote
# budget shut this long after one, so the provider's window drains instead of being re-spent into a
# closed door. 15s is well inside the 180s rails-blind staleness bound, so a real outage still
# pages; it only stops one throttle episode from reading as a deepening outage.
MEMECOIN_EXACT_QUOTE_RATE_LIMIT_COOLDOWN_S = float(
    os.getenv("MEMECOIN_EXACT_QUOTE_RATE_LIMIT_COOLDOWN_S", "15"))
# The Solana exact-exit preview (balance read + Jupiter quote) answers within this, or returns an
# INFRA timeout (the rail holds, the ops streak moves, unsellable misses never do) and frees the
# funded exit lock (2026-09-27: one preview balance read took 12.5 s under the lock on 66015).
# Healthy one-position focus passes: p50 0.30 / p99 0.65 / max 1.93 s. 0 = no deadline.
MEMECOIN_SOLANA_PREVIEW_DEADLINE_S = float(os.getenv("MEMECOIN_SOLANA_PREVIEW_DEADLINE_S", "3"))
MEMECOIN_FASTLANE_DECIDES = os.getenv(
    "MEMECOIN_FASTLANE_DECIDES", "0").strip().lower() in ("1", "on", "true", "yes")
# 1s (2026-07-27, was 5): the funded tape drives the operator ticker AND the trail ratchet, and at
# 5s the measured open-position mark gaps were 14.6s median / 79s max, so >=10% moves landed
# entirely between samples (1.4% of intervals over 6h; max 77%). The cost is ONE batched
# DexScreener call per tick for <=MEMECOIN_MAX_CONCURRENT mints -> 60 req/min against a ~300/min
# budget. The EXACT per-position preview lane is NOT 1s-safe (2 round trips x 7 positions = 14/s)
# and stays on its own chains; see memecoin_fastlane.in_scope vs mid_scope.
MEMECOIN_FASTLANE_TICK_S = float(os.getenv("MEMECOIN_FASTLANE_TICK_S", "1"))
# LARGEST OPEN BOOK THAT MAY SLEEP TO A DEADLINE (2026-09-24). The fast lane slept a full tick
# AFTER its work, so its period was work + tick and every stop inherited ~0.6 s of it. Sleeping to
# `pass start + tick` removes that, but it raises the exit lane's quote rate ~1.6x on ONE Jupiter
# key (1 RPS / 60 per minute), and the pressure is concentrated exactly where the book is big:
# measured over 14 d, exit quotes taken with one position open sat in a minute that lost a quote to
# rate limiting 0.8% of the time, with two open 25.8%, with three open 75.2%. So the deadline is
# scoped to a book at or below this number. 0 = off everywhere, which is today's behaviour and the
# safe default for a rolled-back release; 1 = the single-position case only. Raise it when the key
# budget rises (a 10 RPS Developer key), never because the lane "feels" slow.
MEMECOIN_FASTLANE_DEADLINE_SLEEP_MAX_BOOK = int(
    os.getenv("MEMECOIN_FASTLANE_DEADLINE_SLEEP_MAX_BOOK", "0"))
# Rail wake (hot TP lane 2026-08-22; every price rail 2026-09-27, memecoin_hot_tp.py): a confirmed
# Solana mark that says a stop, trail, take-profit or de-risk level was probably crossed, or that
# the price moved BIG_MOVE_PCT since the last exact quote, wakes the fastlane worker for a FOCUS
# pass on that position instead of waiting for the tick. Timing change only: the exact preview,
# rails, claim and executor are untouched. ENABLED=0 is the rollback (tick only); SHADOW=1 logs
# the wakes without waking. Wakes coalesce per mint to MIN_INTERVAL_S and are capped at
# MAX_PER_MINUTE because each costs one exact quote and one provider 429 closes the exact-quote
# budget for MEMECOIN_EXACT_QUOTE_RATE_LIMIT_COOLDOWN_S.
MEMECOIN_HOT_TP_ENABLED = os.getenv("MEMECOIN_HOT_TP_ENABLED", "1").strip().lower() in ("1", "on", "true", "yes")
MEMECOIN_HOT_TP_MIN_INTERVAL_S = float(os.getenv("MEMECOIN_HOT_TP_MIN_INTERVAL_S", "0.25"))
MEMECOIN_RAIL_WAKE_BIG_MOVE_PCT = float(os.getenv("MEMECOIN_RAIL_WAKE_BIG_MOVE_PCT", "5"))
MEMECOIN_RAIL_WAKE_MAX_PER_MINUTE = float(os.getenv("MEMECOIN_RAIL_WAKE_MAX_PER_MINUTE", "60"))
MEMECOIN_RAIL_WAKE_SHADOW = os.getenv("MEMECOIN_RAIL_WAKE_SHADOW", "0").strip().lower() in ("1", "on", "true", "yes")
# tick-stream ingest v0 (2026-07-23): the quant data plane's raw swap tape — observation ONLY,
# separate DB file (data/memecoin_ticks.db), never read by any entry/exit path. 4663 ingests the
# WHOLE chain tape (~100 events/min, probed); 8453 only watched pools; Solana arrival events per
# open real position via wss logsSubscribe. Spec:
# docs/superpowers/specs/2026-07-23-memecoin-tick-stream-design.md
MEMECOIN_TICKSTREAM_ENABLED = os.getenv(
    "MEMECOIN_TICKSTREAM_ENABLED", "0").strip().lower() in ("1", "on", "true", "yes")
MEMECOIN_TICKSTREAM_TICK_S = float(os.getenv("MEMECOIN_TICKSTREAM_TICK_S", "3"))
# The 4663 scan reads the full chain from the custody endpoint. A slower scan reduces HTTP 429s.
MEMECOIN_TICKSTREAM_4663_TICK_S = float(os.getenv(
    "MEMECOIN_TICKSTREAM_4663_TICK_S", "10"))
MEMECOIN_TICKSTREAM_RETENTION_D = float(os.getenv("MEMECOIN_TICKSTREAM_RETENTION_D", "7"))
# Catch-up bound (2026-07-23 review P1): observed_at is stamped with INGEST time (eth_getLogs
# carries no block time), so replaying a deep historical gap stamps hours-old swaps as "now" and
# poisons the feature windows. A gap deeper than this many blocks is skipped forward to the tip
# (honest as a gap) rather than replayed stamped-as-current.
MEMECOIN_TICKSTREAM_MAX_CATCHUP_BLOCKS = int(
    os.getenv("MEMECOIN_TICKSTREAM_MAX_CATCHUP_BLOCKS", "900"))
# Liquidity bridge (2026-07-23 review P1): the tick feature job writing on-chain pool liquidity
# into memecoin_token_liquidity feeds PAPER exit slippage — a decision-path write, not pure
# observation, and it can bridge an unrelated/non-canonical pool's reserve onto a coin. OFF by
# default so the tick plane stays observation-only until the bridge is validated (canonical pool,
# fresh data) behind a pre-registered read.
MEMECOIN_TICK_LIQUIDITY_BRIDGE = os.getenv(
    "MEMECOIN_TICK_LIQUIDITY_BRIDGE", "0").strip().lower() in ("1", "on", "true", "yes")
# candidate cohorts (2026-07-16): every concrete-chain coin the feed shows us gets a low-priority
# capture cohort, so lifecycle/consensus datasets stop being survivorship-only (corpus study #1
# lever). Priority 50 < signal 100 < position 400 — candidates only consume leftover sweep budget.
MEMECOIN_CAPTURE_CANDIDATES_ENABLED = os.getenv(
    "MEMECOIN_CAPTURE_CANDIDATES_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_CAPTURE_CANDIDATE_TTL_S = float(os.getenv("MEMECOIN_CAPTURE_CANDIDATE_TTL_S", "86400"))
MEMECOIN_STRATEGY_V0_ENABLED = os.getenv(
    "MEMECOIN_STRATEGY_V0_ENABLED", "1").strip().lower() in {"1", "true", "yes"}

# --- thesis capture (2026-07-08) — traders' posted reasoning, DISPLAY/CONTEXT only ---
# Theses flow through the same /feed we poll (type='thesis_created'); we capture curated ones.
# NOT a copy signal (theses skew underwater-cope). stance = objective PnL bucket, no NLP.
MEMECOIN_THESIS_ENABLED = os.getenv("MEMECOIN_THESIS_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_THESIS_UNDERWATER_PCT = float(os.getenv("MEMECOIN_THESIS_UNDERWATER_PCT", "-10"))  # <= -> underwater
MEMECOIN_THESIS_WINNING_PCT = float(os.getenv("MEMECOIN_THESIS_WINNING_PCT", "20"))         # >= -> winning

# --- telegram front-run latency instrument (2026-07-08) ---
# Fire a Telegram the instant we detect a curated large buy (global feed only), to test whether
# we reach the operator's phone BEFORE fomo's own push. External send -> default OFF (opt-in).
# The bot token is a credential set by the operator in env; never hardcode it.
MEMECOIN_TG_ALERTS = os.getenv("MEMECOIN_TG_ALERTS", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_TG_BOT_TOKEN = os.getenv("MEMECOIN_TG_BOT_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
MEMECOIN_TG_CHAT_ID = os.getenv("MEMECOIN_TG_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "")
MEMECOIN_TG_MIN_USD = float(os.getenv("MEMECOIN_TG_MIN_USD", "0"))        # alert only on buys >= this
# Share of market cap the buy must also clear, in PERCENT. 0 = off, which is byte-identical to the
# flat floor above. ALERT-ONLY: the 0.1% arm showed no per-coin separation in paper (mean delta
# -0.19 pts, 90% CI [-11.35,+15.04], median AGAINST the rule, n=198 coins), so it must not reach
# paper admission — which is cohort-frozen — or funded admission, which cannot compute it yet.
MEMECOIN_TG_MIN_MCAP_PCT = float(os.getenv("MEMECOIN_TG_MIN_MCAP_PCT", "0"))


def tg_min_buy_usd(market_cap, *, floor=None, pct=None):
    """The minimum source buy that earns an alert: max(floor, pct% of market cap).

    An unknown or non-positive market cap falls back to the flat floor. It must not disable the
    floor, and it must not invent a share of zero — a missing mcap is missing, not small.
    """
    floor = MEMECOIN_TG_MIN_USD if floor is None else float(floor)
    pct = MEMECOIN_TG_MIN_MCAP_PCT if pct is None else float(pct)
    try:
        mc = float(market_cap or 0.0)
    except (TypeError, ValueError):
        mc = 0.0
    if pct <= 0 or mc <= 0:
        return floor
    return max(floor, mc * pct / 100.0)


# Alert mcap ceiling: no coin alert (front-run, consensus, or follow-activity) fires for a coin
# with a KNOWN market cap above this. 0 = off. ALERT-ONLY — entry admission has its own ceiling
# (MEMECOIN_REAL_MAX_MCAP_USD) with fail-closed resolution; this one is advisory and fail-open.
MEMECOIN_TG_MAX_MCAP_USD = float(os.getenv("MEMECOIN_TG_MAX_MCAP_USD", "0"))


def tg_mcap_ok(market_cap, *, ceiling=None):
    """True when the coin may alert under the mcap ceiling. An unknown or non-positive market cap
    passes: a fresh coin has no mcap yet and is small by construction — the ceiling exists to drop
    KNOWN-big coins, not fresh ones (the entry gate does its own fail-closed resolution)."""
    ceiling = MEMECOIN_TG_MAX_MCAP_USD if ceiling is None else float(ceiling)
    try:
        mc = float(market_cap or 0.0)
    except (TypeError, ValueError):
        mc = 0.0
    return ceiling <= 0 or mc <= 0 or mc <= ceiling
MEMECOIN_TG_MAX_PER_POLL = int(os.getenv("MEMECOIN_TG_MAX_PER_POLL", "5"))  # burst guard (Telegram 429)
MEMECOIN_TG_TIMEOUT_S = float(os.getenv("MEMECOIN_TG_TIMEOUT_S", "8"))    # 3s was too tight — a real 3.16s send timed out + was lost (biases the send-latency sample); 8s covers slow tails, a genuine hang still bounded (rare, paper)
# Front-run lag above this is not a front-run — label it 'stale' rather than dressing a minutes-old
# backfill signal as a sub-second detection (22% of live alerts had negative/garbage lag, 6 were
# 60-890s old; 2026-07-15 alert-reality audit).
MEMECOIN_TG_STALE_LAG_S = float(os.getenv("MEMECOIN_TG_STALE_LAG_S", "60"))
# The entry card's chain line anchors on the BSC header milliTimestamp when the detection row carries
# it (7eb5c92c, 2026-10-17). Display only. 0 restores the whole-second anchor everywhere.
MEMECOIN_CARD_BLOCK_MS_ENABLED = os.getenv("MEMECOIN_CARD_BLOCK_MS_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
# Above this lag the front-run alert is NOT SENT at all (2026-07-16, operator: "no delayed catch
# bullshit") — a minutes-old signal is unactionable; labeling it stale still spams the channel.
# Unknown/negative lag still sends (fail-open; the builder labels it). 0 = never suppress.
MEMECOIN_TG_MAX_LAG_S = float(os.getenv("MEMECOIN_TG_MAX_LAG_S", "120"))
# 120 (was 60) since 2026-07-17: must match MEMECOIN_REAL_FOLLOW_MAX_AGE_S — a catch the real
# leg can ENTER must never be invisible to the operator.
# CONSENSUS alerts (2026-07-17): non-curated buys alert ONLY as the 2nd+ distinct buyer on a
# coin under CONSENSUS_MAX_MCAP within 60m — the 49%-hit20 consensus signal (solo non-curated
# is 13.8-20% noise and stays silent). Curated buys alert as before.
MEMECOIN_TG_CONSENSUS_ALERTS = os.getenv("MEMECOIN_TG_CONSENSUS_ALERTS", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_TG_CONSENSUS_MAX_MCAP = float(os.getenv("MEMECOIN_TG_CONSENSUS_MAX_MCAP", "2000000"))
# Periodic DB-only operator health digest. Repository default stays off; funded hosts opt in.
# Collection and Telegram delivery both run outside the entry loop.
MEMECOIN_TG_DIGEST_ENABLED = os.getenv(
    "MEMECOIN_TG_DIGEST_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
try:
    MEMECOIN_TG_DIGEST_PERIOD_S = float(os.getenv("MEMECOIN_TG_DIGEST_PERIOD_S", "3600"))
except (TypeError, ValueError):
    # Optional observability must never make funded settings unimportable.
    MEMECOIN_TG_DIGEST_PERIOD_S = 0.0
if not math.isfinite(MEMECOIN_TG_DIGEST_PERIOD_S):
    MEMECOIN_TG_DIGEST_PERIOD_S = 0.0
# The follow-activity DELAYED-CATCH alert (backfill path, 120-900s old by construction). OFF by
# default 2026-07-16 for the same reason; the real leg on that path is separately gated.
MEMECOIN_TG_DELAYED_CATCH_ENABLED = os.getenv("MEMECOIN_TG_DELAYED_CATCH_ENABLED", "0").strip().lower() in {"1", "true", "yes"}

# --- runner liveness (watchdog) ---
# Heartbeat is rewritten every poll (~1.5s). If it is older than this the loop is wedged (or the
# process is up but not cycling) -> the watchdog restarts it. ~80 missed polls before we act.
MEMECOIN_HEARTBEAT_MAX_AGE_S = float(os.getenv("MEMECOIN_HEARTBEAT_MAX_AGE_S", "120"))
# exit sweep runs ~15s; if the last SUCCESSFUL sweep is older than this, SL/TP is dead even if
# the poll loop looks alive -> watchdog restarts (real stop-losses must not fail silently).
MEMECOIN_SWEEP_MAX_AGE_S = float(os.getenv("MEMECOIN_SWEEP_MAX_AGE_S", "90"))
# AUTH-DEAD re-page interval. The page fired ONCE on the crossing and then went silent: on
# 2026-09-02 the feed was dead 121 min / 1443 crash-loops and the only live indicator after that
# single page was `cycles=0`, byte-identical to a quiet market. Re-page while dead, carrying the
# elapsed outage. 0 disables the re-page (back to one-shot).
MEMECOIN_AUTH_REPAGE_S = float(os.getenv("MEMECOIN_AUTH_REPAGE_S", "900"))

# --- real-money risk envelope (S1) — bankroll is the wallet balance, not a knob ---
MEMECOIN_REAL_ENABLED = os.getenv("MEMECOIN_REAL_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_TRADE_SIZE_USD = float(os.getenv("MEMECOIN_TRADE_SIZE_USD", "10"))
# sizing mode: 'fixed' = every entry is exactly MEMECOIN_TRADE_SIZE_USD (the original invariant).
# 'bankroll' = entry size scales with equity: max(MIN_TRADE_USD, equity / MAX_CONCURRENT).
# 'fraction' = max(MIN_TRADE_USD, equity * MEMECOIN_BANKROLL_FRACTION) (operator-ordered
# 2026-07-10: 13% clips). equity = the VENUE's free USDC + the VENUE's deployed cost basis
# (open + reserved on that venue) — a Base position never inflates a Solana clip. Profits grow
# equity -> the clip grows with it.
# 2026-07-15 (operator): 'bankroll' — fixed $10 clips were laughable next to the real bankroll.
# clip = max(MIN_TRADE_USD, VENUE_equity / MAX_CONCURRENT), so each chain's capital is spread across
# up to MAX_CONCURRENT concurrent positions and is FULLY UTILIZED at full book, not left idle.
MEMECOIN_REAL_SIZING = os.getenv("MEMECOIN_REAL_SIZING", "bankroll").strip().lower()
# PER-CHAIN SIZING-MODE OVERRIDE (2026-09-16, Arc build). The live default is 'bankroll'
# (equity / slots); the operator wants Arc at a flat 50% of equity regardless, which is the
# 'fraction' formula, not a bigger bankroll divisor. MEMECOIN_BANKROLL_FRACTION_<CHAIN> alone
# cannot do that -- 'bankroll' mode never reads the fraction constant at all -- so the MODE
# itself needs the same per-chain override, falling back to the global for every other chain.
MEMECOIN_REAL_SIZING_BY_CHAIN = {
    k[len("MEMECOIN_REAL_SIZING_"):]: v.strip().lower()
    for k, v in os.environ.items() if k.startswith("MEMECOIN_REAL_SIZING_")
}


def real_sizing_for(chain):
    return MEMECOIN_REAL_SIZING_BY_CHAIN.get(str(chain or "").strip(), MEMECOIN_REAL_SIZING)


MEMECOIN_BANKROLL_FRACTION = float(os.getenv("MEMECOIN_BANKROLL_FRACTION", "0.13"))
# PER-CHAIN OVERRIDE (2026-09-16, Arc build). Same shape as MEMECOIN_MAX_CONCURRENT_BY_CHAIN:
# MEMECOIN_BANKROLL_FRACTION_<CHAIN> scopes one chain, unset chains keep the global fraction.
MEMECOIN_BANKROLL_FRACTION_BY_CHAIN = {
    k[len("MEMECOIN_BANKROLL_FRACTION_"):]: float(v)
    for k, v in os.environ.items() if k.startswith("MEMECOIN_BANKROLL_FRACTION_")
}


def bankroll_fraction_for(chain):
    return MEMECOIN_BANKROLL_FRACTION_BY_CHAIN.get(
        str(chain or "").strip(), MEMECOIN_BANKROLL_FRACTION)


MEMECOIN_MIN_TRADE_USD = float(os.getenv("MEMECOIN_MIN_TRADE_USD", "30"))   # bankroll-mode floor
# PER-ENTRY CEILING ON EVM CLIPS (2026-09-04, operator: "cap EVM entries at 150").
#
# Bankroll sizing is `equity / slots` with a FLOOR and no ceiling, so a growing book grows the
# clip without bound. This caps what any single EVM entry may risk, independent of equity, slots
# or the risk scale. It is a CEILING, not a target: a smaller computed clip is left alone, and the
# existing floor, spendable-balance clamp and route-capacity reduction all still apply after it.
# 0 disables the cap and restores pure bankroll sizing.
# Operator 2026-10-04: caps apply to every funded chain and are never target sizes.
# Explicit activation keeps rollback possible; missing wallet evidence uses the lower ceiling.
MEMECOIN_REAL_WALLET_CAPS_ENABLED = os.getenv("MEMECOIN_REAL_WALLET_CAPS_ENABLED", "0").lower() in {"1", "true", "yes"}
MEMECOIN_REAL_MAX_ENTRY_USD = 175.0
MEMECOIN_REAL_UNPROVEN_MAX_ENTRY_USD = 50.0
# Explicit operator budget overrides by stable wallet ID; never a sizing target or arm action.
MEMECOIN_REAL_FULL_CAP_WALLET_IDS = frozenset(
    uid.strip() for uid in os.getenv("MEMECOIN_REAL_FULL_CAP_WALLET_IDS", "").split(",")
    if uid.strip())

MEMECOIN_EVM_MAX_CLIP_USD = float(os.getenv("MEMECOIN_EVM_MAX_CLIP_USD", "0"))
# PER-CHAIN OVERRIDE (2026-09-16, Arc build). Same shape as above; 0 (the default fallback)
# disables the cap on any chain without its own entry, same as today.
MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN = {
    k[len("MEMECOIN_EVM_MAX_CLIP_USD_"):]: float(v)
    for k, v in os.environ.items() if k.startswith("MEMECOIN_EVM_MAX_CLIP_USD_")
}


# PER-TIER CLIP CAPS (2026-09-22, operator, Robinhood ladder): "100% equity up to $50; above $50
# one $50 clip at a time keeping the remainder; two $50 clips when possible, growing to two $100;
# three $100; then split equity in three up to $170 clips". Bankroll mode already gives
# clip = equity / active slots with slots 1 -> 2 -> 3 at MEMECOIN_TWO/THREE_SLOT_EQUITY_USD; the
# missing piece is a CEILING PER ACTIVE SLOT COUNT so the clip stops at $50 (1 slot), $100 (2)
# and $170 (3) while the rest stays in the wallet. MEMECOIN_EVM_TIER_CLIP_USD_<chain>="50,100,170".
# A chain without the knob keeps its flat MEMECOIN_EVM_MAX_CLIP_USD(_<chain>) ceiling.
MEMECOIN_EVM_TIER_CLIP_USD_BY_CHAIN = {
    k[len("MEMECOIN_EVM_TIER_CLIP_USD_"):]: tuple(
        float(x) for x in v.split(",") if x.strip())
    for k, v in os.environ.items() if k.startswith("MEMECOIN_EVM_TIER_CLIP_USD_")
}


def evm_max_clip_usd_for(chain, slots=None):
    """The per-entry ceiling for `chain`; with `slots` (the ACTIVE slot count from
    max_concurrent_for) a chain with per-tier caps returns the cap for that tier."""
    key = str(chain or "").strip()
    tiers = MEMECOIN_EVM_TIER_CLIP_USD_BY_CHAIN.get(key)
    if tiers and slots is not None:
        idx = min(max(1, int(slots)), len(tiers)) - 1
        return tiers[idx]
    return MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN.get(key, MEMECOIN_EVM_MAX_CLIP_USD)
# ── FUNDED ETHEREUM (chain 1) PARAMETERS (operator 2026-09-30, applied by Alpha to the proposed
# values: "clip $20, round-trip cap 10%, gas ceiling 5 gwei, canary stop -25% of clip from the 5th
# close"). CODE DEFAULTS for chain 1 only, each with a suffix-scoped env override, so no .env line
# is needed. None of them arms anything: the pinned coverage manifest still refuses every chain-1
# entry until each capability is evidenced (eth-mainnet spec §5, §6).
def _chain_float(name, chain, default, *, minimum=0.0):
    """A per-chain float knob `<name>_<chain>`. Malformed, non-finite or below `minimum`: `default`."""
    raw = os.getenv("%s_%s" % (name, str(chain or "").strip()))
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= minimum else default


# A FIXED clip per entry, replacing the chain's sizing mode. 0 = off (bankroll or fraction sizing).
# Fixed means fixed: a balance under the clip refuses the entry, it does not shrink the clip. The
# regime risk scale still scales it down, and a manual entry still names its own size.
_EVM_FIXED_CLIP_USD_DEFAULTS = {"1": 20.0}


# AN ENTRY'S MIN-OUT TOLERANCE, PER EVM CHAIN (2026-10-08, operator "fix this"; design 19ff3de41).
# Chain 1 signs through Flashbots Protect, which never includes a transaction that would revert: OG
# 4973 cleared block 26147894's fees 4.5 s before its slot, but three other buys in that block (~18x
# our size) moved the V4 pool past a 3% minimum, and it expired unmined, 0.547% short. An unmined
# send never reaches the min-out retry. 8 is 4663's working value. The SIGNED minimum still answers to
# the copy cap (copy_minimum_refusal), so on chain 1 a quote is admitted up to ~24% over the source
# (1.35 x 0.92) instead of ~31%. Every other chain: MEMECOIN_ENTRY_SLIPPAGE_PCT, as before. Never the
# min-out RETRY knob (a reused knob carries its live value).
_EVM_ENTRY_SLIPPAGE_PCT_DEFAULTS = {"1": 8.0}


def evm_entry_slippage_bps_for(chain):
    """The entry quote's min-out tolerance on `chain`, in bps: MEMECOIN_EVM_ENTRY_SLIPPAGE_PCT_<chain>,
    else the chain's default above, else MEMECOIN_ENTRY_SLIPPAGE_PCT. A value no rung can sign
    (malformed, non-finite, negative, >= 100) keeps the default. Call-time."""
    key = str(chain or "").strip()
    default = _EVM_ENTRY_SLIPPAGE_PCT_DEFAULTS.get(key, MEMECOIN_ENTRY_SLIPPAGE_PCT)
    pct = _chain_float("MEMECOIN_EVM_ENTRY_SLIPPAGE_PCT", key, default)
    return int(round((pct if pct < 100 else default) * 100))


def evm_fixed_clip_usd_for(chain):
    key = str(chain or "").strip()
    return _chain_float("MEMECOIN_EVM_FIXED_CLIP_USD", key, _EVM_FIXED_CLIP_USD_DEFAULTS.get(key, 0.0))


# The most a round trip's GAS may cost, in % of the clip. The global 3% would refuse every $20
# entry on chain 1, where gas is a fixed dollar cost. Read by the chain-1 gas guard (step 5).
_EVM_ROUNDTRIP_COST_CAP_PCT_DEFAULTS = {"1": 10.0}


def evm_roundtrip_cost_cap_pct_for(chain):
    key = str(chain or "").strip()
    default = _EVM_ROUNDTRIP_COST_CAP_PCT_DEFAULTS.get(key, MEMECOIN_MAX_ROUNDTRIP_COST_PCT)
    return _chain_float("MEMECOIN_MAX_ROUNDTRIP_COST_PCT", key, default, minimum=1e-9)


# ENTRIES are refused above this gas price, in gwei. Exits are never refused by it. 0 = off.
_EVM_MAX_GAS_GWEI_DEFAULTS = {"1": 5.0}


def evm_max_gas_gwei_for(chain):
    key = str(chain or "").strip()
    return _chain_float("MEMECOIN_EVM_MAX_GAS_GWEI", key, _EVM_MAX_GAS_GWEI_DEFAULTS.get(key, 0.0))


def evm_gas_guard_chains():
    """Chains whose entries pass the gas guard (cost cap and gwei ceiling): those with a default."""
    return frozenset(_EVM_ROUNDTRIP_COST_CAP_PCT_DEFAULTS) | frozenset(_EVM_MAX_GAS_GWEI_DEFAULTS)


# Route classes whose round trip the flat knob under-prices: their own knob is REQUIRED (fork
# receipts 2026-09-30 / 2026-10-07: V4_HOOKED p90 638,162; V4_MULTIHOP 444,609-488,436 hookless;
# V4_MULTIHOP_HOOKED 1,051,328, all against a flat 622,791).
_ROUNDTRIP_GAS_REQUIRED_CLASSES = frozenset({"V4_HOOKED", "V4_MULTIHOP", "V4_MULTIHOP_HOOKED"})


def evm_roundtrip_gas_units_for(chain, route_class=None):
    """Gas UNITS of one round trip on `chain` (unwrap, entry swap, approval, exit swap), or None
    while unmeasured. MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_<chain>, set from the fork rehearsal's
    measurement. NOT INVENTED: absent is not zero, and the gas guard refuses while it is None.

    With `route_class`, MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_<chain>_<class> first. V2V3 and V4 fall
    back to the flat knob; the classes in _ROUNDTRIP_GAS_REQUIRED_CLASSES never do."""
    key = str(chain or "").strip()
    if route_class:
        value = _chain_float("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS", "%s_%s" % (key, route_class), 0.0)
        if value >= 1:
            return int(value)
        if route_class in _ROUNDTRIP_GAS_REQUIRED_CLASSES:
            return None
    value = _chain_float("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS", key, 0.0)
    return int(value) if value >= 1 else None


def evm_tip_gwei_for(chain):
    """Gwei added on top of the buffered node gas price when signing on `chain`. 0 = none."""
    return _chain_float("MEMECOIN_EVM_TIP_GWEI", str(chain or "").strip(), 0.0)


def evm_entry_tip_floor_wei_for(chain):
    """The lowest tip an ENTRY swap signs on `chain`, in wei: MEMECOIN_EVM_ENTRY_TIP_FLOOR_GWEI_<chain>
    (operator GO 2026-10-09, chain 1: copier buys tipping 0.5-5.8 gwei were ordered ahead of ours).
    0 = off; malformed, non-finite or negative: 0. Type-2 chains only; capped by the signer's gas
    reserve over the gas limit. Exits, approvals and unwraps never read it. Call-time."""
    gwei = _chain_float("MEMECOIN_EVM_ENTRY_TIP_FLOOR_GWEI", str(chain or "").strip(), 0.0)
    return int(round(gwei * 1e9))


def evm_exit_tip_floor_wei_for(chain):
    """Sell/held-token approval tip floor, in wei; Ethereum defaults to 1 gwei. Call-time.
    0 disables it; malformed, non-finite or negative values disable it. Type-2 only, subject
    to the signer's gas reserve cap. Entry approvals and unwraps retain their own fees."""
    key = str(chain or "").strip()
    if key == "1" and os.getenv("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1") is None:
        return 1_000_000_000
    wei = _chain_float("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI", key, 0.0) * 1e9
    return int(round(wei)) if math.isfinite(wei) else 0


def evm_tip_cap_gwei_for(chain):
    """The highest tip the chain-1 fee headroom may sign (sniper/execution/chain1_protect.py), so a
    fee spike cannot pay a large tip. Default 2 gwei."""
    return _chain_float("MEMECOIN_EVM_TIP_CAP_GWEI", str(chain or "").strip(), 2.0)


def memecoin_chain1_protect_replace():
    """Call-time: a chain-1 full exit that Flashbots Protect simulates as a revert on 2 consecutive
    status polls is replaced at the same nonce with a fresh quote at the next wider rung and bumped
    fees, signed by the exit lane (operator via Alpha, 2026-10-08, after BUNKER 72163). Off by
    default; off means today's wait for the receipt and the deadline."""
    return os.getenv("MEMECOIN_CHAIN1_PROTECT_REPLACE", "0").strip().lower() in ("1", "on", "true", "yes")


# Replacements per held chain-1 close. Each one widens to the next exit-ladder rung, so the ladder
# length bounds it too.
MEMECOIN_CHAIN1_PROTECT_REPLACE_MAX = int(os.getenv("MEMECOIN_CHAIN1_PROTECT_REPLACE_MAX", "2"))


def memecoin_chain1_fee_headroom():
    """Call-time: chain-1 signing takes its tip from recent median tips (p75, floored at
    MEMECOIN_EVM_TIP_GWEI_1, capped at MEMECOIN_EVM_TIP_CAP_GWEI_1) and its maxFee toward 2 x the
    next base fee + tip, never below today's and never past the per-tx native gas reserve
    (2026-10-08, after two Flashbots Protect MaxFeePerGasTooLow expiries). Off by default."""
    return os.getenv("MEMECOIN_CHAIN1_FEE_HEADROOM", "0").strip().lower() in ("1", "on", "true", "yes")


# CANARY STOP: from the Nth automatic real close on the chain, entries stop while the cumulative
# GROSS return on the deployed clip (sum of realized PnL / sum of entry notional) is below this
# percentage. (N, pct) or None. N = 0 turns it off; a non-negative pct keeps the default.
_EVM_CANARY_STOP_DEFAULTS = {"1": (5, -25.0)}


def evm_canary_stop_for(chain):
    key = str(chain or "").strip()
    default = _EVM_CANARY_STOP_DEFAULTS.get(key)
    from_close = int(_chain_float("MEMECOIN_EVM_CANARY_STOP_FROM_CLOSE", key,
                                  default[0] if default else 0))
    if from_close <= 0:
        return None
    pct = _chain_float("MEMECOIN_EVM_CANARY_STOP_RETURN_PCT", key,
                       default[1] if default else 0.0, minimum=-100.0)
    if pct >= 0:
        pct = default[1] if default else 0.0
    return (from_close, pct) if pct < 0 else None


# THE SAME CEILING FOR SOLANA (2026-09-08, operator: "cap solana entries at 175").
# Solana had no per-entry ceiling at all: `real_trade_size` has taken a `max_clip_usd` since
# 2026-09-04, but only the EVM gate and the EVM half of the dashboard ever passed one, so a
# growing Solana book grew the clip without bound. Same semantics as the EVM knob — a CEILING,
# applied only downward, after the risk scale; the floor, the spendable-balance clamp and the
# route-capacity reduction all still apply. 0 disables it.
#
# Not retroactive and not currently binding: the largest Solana entry ever taken is $173.77
# (median $107.94 over the last 40), so this pins the ceiling where bankroll sizing already tops
# out rather than cutting anything the book is doing today.
MEMECOIN_SOLANA_MAX_CLIP_USD = float(os.getenv("MEMECOIN_SOLANA_MAX_CLIP_USD", "0"))
# ABOVE NANO (operator order 2026-10-08 ~14:00Z: "Raise the Solana clip size to 200 on entries above
# 50k mcap"). A Solana entry whose classification resolved the coin at $50K or more (band micro,
# small, mid, or out_of_scope with a resolved mcap) takes this as its Solana max clip, its global
# hard cap and the per-wallet cap of full-cap and proven wallets. A ceiling, never a target; it never
# lowers a larger ceiling or enables a disabled one; unproven and trial caps stay. 0 = off (today).
# Read at import: a change needs a restart. memecoin_risk.solana_clip_ceiling_usd applies it.
MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO = float(os.getenv("MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO", "200"))
# THE THRESHOLD (operator 2026-10-08 ~15:00Z, "make the entries larger, whatever works"): the
# resolved entry mcap at or above which a Solana entry takes the raised ceiling. 50000 = above nano
# (the rule above). 0 = EVERY Solana entry, nano and unknown mcap included, so the slot lane's single
# prefetched USDC leg sizes at the raised ceiling and its reservations match it. A negative or
# non-finite value is treated as 50000 (a typo never widens). Read at import.
MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD = float(os.getenv("MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD", "50000"))
# ONLY THE BEST WALLETS (operator 2026-10-08 ~14:20Z): the wallets the raised Solana ceiling goes to,
# in the full-cap list's format (bare id = every chain, id@chain). Unset or empty = every wallet (per
# the threshold above); set = only these, still subject to the threshold; all-malformed = nobody.
# Kept RAW: memecoin_wallet_caps parses it and re-reads it from .env every 30 s. Its own list:
# sizing never reads the cross-wallet re-entry list.
MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS = os.getenv("MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS", "").strip()
# NATIVE FLOAT DRIFT PANEL (operator 2026-10-08 ~16:35Z): the runner's display-only sampler thread
# (memecoin_native_float). Default on; 0 = off (the panel then shows stale). Read at import.
MEMECOIN_NATIVE_FLOAT_SAMPLER = os.getenv("MEMECOIN_NATIVE_FLOAT_SAMPLER", "1").strip().lower() in {"1", "true", "yes"}
# EQUITY CHANGE RECORDER (operator 2026-10-08 ~17:40Z): total equity once a minute on the sampler thread
# above (memecoin_equity_change). DEFAULT OFF since the operator removed the card the same evening ("Get
# rid of the equity panel, I don't want it"); the module and its route stay, unused. 1 = on.
MEMECOIN_EQUITY_RECORDER = os.getenv("MEMECOIN_EQUITY_RECORDER", "0").strip().lower() in {"1", "true", "yes"}
# PER-CHAIN cap (2026-07-15): concurrency, exposure, and the gas exit reserve are scoped to ONE
# venue's book — an EVM position must not eat a Solana slot. Doubles as the bankroll-sizing divisor.
MEMECOIN_MAX_CONCURRENT = int(os.getenv("MEMECOIN_MAX_CONCURRENT", "7"))
# PER-CHAIN CONCURRENCY (2026-08-30, operator). MEMECOIN_MAX_CONCURRENT is GLOBAL and does three
# jobs at once: the portfolio slot cap, the per-chain slot cap, and the bankroll SIZING DIVISOR in
# `real_trade_size`. That coupling is why it cannot express what the operator asked for --
#     solana: 1 concurrent, clip = FULL solana equity  (stated twice, MUST NOT CHANGE)
#     4663:   2 concurrent, clip = 4663 equity / 2
# -- because raising the global to let 4663 reach 2 also divides the solana clip. Measured on the
# live balances: divisor 1 gives solana $35.42, divisor 2 gives $17.71, divisor 3 gives $11.80.
# No single global value produces $35.42 and $32.50 together.
#
# Format: MEMECOIN_MAX_CONCURRENT_<CHAIN>=n  e.g. MEMECOIN_MAX_CONCURRENT_4663=2
# An unconfigured chain falls back to the global value and behaves EXACTLY as before.
# HOW OLD A POSITION'S EXECUTABLE MARK MAY BE AND STILL COUNT TOWARD SIZING EQUITY (2026-09-06).
#
# Sizing equity used to be `cash + COST BASIS of open positions`, so a bag that had gone to nothing
# kept its full purchase price in the equity that decides the NEXT clip. Live case: LARP (position
# 50693) cost $75.22 and its last executable mark was $5.08; with $195.90 of 4663 cash over three
# slots that produced a $90.37 clip where honest equity gives $65.30 — every later 4663 bet 38% too
# large, funded by an asset we could not sell.
#
# MEASURED, so this is not a guessed number. Over 228 funded positions closed in the seven days to
# 2026-09-06, the age of `last_executable_at` at close was p50 0s, p90 2s, p99 61s, max 927s, and
# the mark sweep itself runs at p50 4s / p90 32s / max 117s. A healthy position's executable mark is
# seconds old, essentially always. 300s is five times the p99.
#
# THE DIRECTION IS ONE-WAY AND DELIBERATE. Past this bound a position is valued at ZERO, never at
# cost. The failure being fixed is a dead bag inflating the next bet, so missing or stale data may
# only SHRINK a clip. A paused position (`tracking_paused_at` set) is zero regardless of age.
#
# THE SAME BOUND ALSO PROTECTS A POSITION THAT IS MERELY NEW. A fill has no mark yet, and its cost
# IS the most recent executable evidence we hold — we just paid it. So an UNMARKED position inside
# this bound is valued at COST, and only outside it at zero, where the absence of a mark is evidence
# rather than latency. MEASURED on funded positions since 2026-08-30: time from open to a first
# executable mark is p50 90s and p90 701s, and 23 of 247 consecutive entry pairs opened within 120s
# of each other — so about one entry in ten is sized while its predecessor is plausibly still
# unmarked, and zeroing those understated equity by a whole clip. The one-way rule is intact: the
# dead-bag case is by definition old and can never reach the cost fallback.
MEMECOIN_SIZING_MARK_MAX_AGE_S = float(os.getenv("MEMECOIN_SIZING_MARK_MAX_AGE_S", "300"))
MEMECOIN_MAX_CONCURRENT_BY_CHAIN = {
    k[len("MEMECOIN_MAX_CONCURRENT_"):]: int(v)
    for k, v in os.environ.items()
    if k.startswith("MEMECOIN_MAX_CONCURRENT_") and k != "MEMECOIN_MAX_CONCURRENT_TOTAL"
    and str(v).strip().lstrip("-").isdigit()
}
# THE GLOBAL CAP NEEDS ITS OWN KNOB, or it silently disappears. Repurposing MEMECOIN_MAX_CONCURRENT
# as "the default per-chain limit" would leave nothing enforcing the portfolio total, so per-chain
# limits would become a way to exceed it -- solana 1 + 4663 2 = 3 open with a "1" configured.
# Unset, it falls back to the global value, which is exactly today's behaviour.
MEMECOIN_MAX_CONCURRENT_TOTAL = int(
    os.getenv("MEMECOIN_MAX_CONCURRENT_TOTAL", str(MEMECOIN_MAX_CONCURRENT)))


def _slot_free_after_s(raw):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


# LONG HOLDS FREE THEIR SLOT (operator 2026-10-07: "long hold positions ... act like dust"). A funded
# position open at least this many seconds stops counting toward the automatic slot caps (global and
# per chain; db_memecoin.automatic_funded_count). The exposure cap, gas reserve and coin dedup are
# unchanged. 0 = off (malformed or negative too). Funded closes since 09-15: hold p95 1,067 s.
MEMECOIN_SLOT_FREE_AFTER_S = _slot_free_after_s(os.getenv("MEMECOIN_SLOT_FREE_AFTER_S", "0"))
MEMECOIN_EQUITY_SLOT_TIERS_ENABLED = os.getenv(
    "MEMECOIN_EQUITY_SLOT_TIERS_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_EQUITY_SLOT_TIERS_ENABLED_BY_CHAIN = {
    k[len("MEMECOIN_EQUITY_SLOT_TIERS_ENABLED_"):]: v.strip().lower() in {"1", "true", "yes"}
    for k, v in os.environ.items() if k.startswith("MEMECOIN_EQUITY_SLOT_TIERS_ENABLED_")
}
MEMECOIN_TWO_SLOT_EQUITY_USD = float(os.getenv(
    "MEMECOIN_TWO_SLOT_EQUITY_USD", "100"))
MEMECOIN_THREE_SLOT_EQUITY_USD = float(os.getenv(
    "MEMECOIN_THREE_SLOT_EQUITY_USD", "300"))


def max_concurrent_for(network_id, *, equity=None):
    """Return the slot limit for one chain and its current equity.

    The configured chain value is a hard maximum. An absent equity keeps the old static behavior.
    """
    key = str(network_id or "").strip()
    limit = max(1, int(MEMECOIN_MAX_CONCURRENT_BY_CHAIN.get(
        key, MEMECOIN_MAX_CONCURRENT)))
    tiers_enabled = MEMECOIN_EQUITY_SLOT_TIERS_ENABLED_BY_CHAIN.get(
        key, MEMECOIN_EQUITY_SLOT_TIERS_ENABLED)
    if equity is None or not tiers_enabled:
        return limit
    value = float(equity)
    if not math.isfinite(value) or value < 0:
        raise ValueError("equity must be finite and nonnegative")
    # PER-CHAIN THRESHOLDS (operator 2026-10-08: chain 1 at $75 clips, "unlock more slots as the
    # equity grows"). A slot opens only when the equity pays the chain's minimum clip in every slot;
    # the global $100 would split a $110 chain-1 book into two $55 clips under the gas cost cap.
    # Call-time; malformed keeps the global. Three is never below two, so 3 cannot open before 2.
    two = _chain_float("MEMECOIN_TWO_SLOT_EQUITY_USD", key, MEMECOIN_TWO_SLOT_EQUITY_USD)
    three = max(two, _chain_float("MEMECOIN_THREE_SLOT_EQUITY_USD", key,
                                  MEMECOIN_THREE_SLOT_EQUITY_USD))
    slots = 3 if value >= three else 2 if value >= two else 1
    return min(limit, slots)
# 0 = exposure cap DISABLED (operator 2026-07-15). Bankroll sizing self-bounds: MAX_CONCURRENT slots
# x (venue_equity / MAX_CONCURRENT) == venue equity, and you cannot spend USDC you don't hold.
MEMECOIN_MAX_TOTAL_EXPOSURE_USD = float(os.getenv("MEMECOIN_MAX_TOTAL_EXPOSURE_USD", "0"))
# 0 = daily-loss breaker DISABLED (operator-ordered 2026-07-10; no floor but the wallet itself).
MEMECOIN_DAILY_LOSS_LIMIT_USD = float(os.getenv("MEMECOIN_DAILY_LOSS_LIMIT_USD", "30"))
# Drawdown pause (2026-09-15, operator): automatic entries stop for _S seconds once today's real
# realized PnL falls _USD below the day's high. 0 = off. See db_memecoin.drawdown_pause_until.
MEMECOIN_DRAWDOWN_PAUSE_USD = float(os.getenv("MEMECOIN_DRAWDOWN_PAUSE_USD", "0"))
MEMECOIN_DRAWDOWN_PAUSE_S = float(os.getenv("MEMECOIN_DRAWDOWN_PAUSE_S", "3600"))
# Operator fresh start inside a day: the daily loss limit and the drawdown pause count only closes at
# or after this ISO time, and only on its own UTC day (then it is ignored). Empty = count the whole day.
MEMECOIN_RISK_DAY_START_AT = (os.getenv("MEMECOIN_RISK_DAY_START_AT", "") or "").strip()
# provisional floor: replaced by the shadow-phase measurement (smallest clip where round-trip
# cost <= MAX_ROUNDTRIP_COST_PCT of notional). Doubles as the launch-viability gate.
MEMECOIN_MIN_TRADE_FLOOR_USD = float(os.getenv("MEMECOIN_MIN_TRADE_FLOOR_USD", "5"))


def min_trade_floor_usd_for(chain):
    """The minimum funded clip on `chain`: MEMECOIN_MIN_TRADE_FLOOR_USD_<chain> when set and valid,
    else the global floor (operator 2026-10-08: chain 1 never buys under $50; mainnet gas makes a
    smaller clip -EV). Call-time; malformed, negative or non-finite keeps the global."""
    return _chain_float("MEMECOIN_MIN_TRADE_FLOOR_USD", str(chain or "").strip(),
                        MEMECOIN_MIN_TRADE_FLOOR_USD)
MEMECOIN_MAX_ROUNDTRIP_COST_PCT = float(os.getenv("MEMECOIN_MAX_ROUNDTRIP_COST_PCT", "3"))
# SOL gas reserve (provisional placeholders; shadow measures true fees — over-reserve until then)
MEMECOIN_GAS_ENTRY_SOL = float(os.getenv("MEMECOIN_GAS_ENTRY_SOL", "0.002"))
MEMECOIN_GAS_EXIT_SOL = float(os.getenv("MEMECOIN_GAS_EXIT_SOL", "0.002"))
MEMECOIN_GAS_RETRY_FACTOR = float(os.getenv("MEMECOIN_GAS_RETRY_FACTOR", "2"))

# RENT SWEEP (operator, 2026-08-26: "get our gas money back, as part of this system").
# Every funded entry creates SPL token accounts and each holds Solana's rent-exempt minimum
# (~0.00203928 SOL). Selling empties the account but does not close it, so the deposit stays locked
# forever. Measured 2026-08-26: 52 empty accounts holding 0.106043 SOL, twice the wallet balance at
# the time and twice what the operator had just bought with trading bankroll to unblock entries. At
# ~25 closes/day the lock rate is ~0.05 SOL/day, which is the drain that emptied the gas wallet.
# The sweep runs INSIDE the runner because every real execution on this wallet serializes behind
# one cross-process lock; a cron with --execute would be a second signer outside it.
MEMECOIN_RENT_SWEEP_ENABLED = os.getenv(
    "MEMECOIN_RENT_SWEEP_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# Seconds between passes. This is a chore, not a rail: infrequent is fine and keeps it far away
# from the entry path.
MEMECOIN_RENT_SWEEP_PERIOD_S = float(os.getenv("MEMECOIN_RENT_SWEEP_PERIOD_S", "1800"))
# Do not spend a transaction fee to reclaim one account's rent; wait until it is worth a signature.
MEMECOIN_RENT_SWEEP_MIN_ACCOUNTS = int(os.getenv("MEMECOIN_RENT_SWEEP_MIN_ACCOUNTS", "8"))
# One transaction per pass, so the sweep can never own the wallet lock while an entry wants it.
MEMECOIN_RENT_SWEEP_MAX_ACCOUNTS = int(os.getenv("MEMECOIN_RENT_SWEEP_MAX_ACCOUNTS", "12"))

# AUTOMATIC GAS TOP-UP (operator, 2026-08-26). On 2026-08-25 the wallet fell below the entry gas
# requirement and every funded entry was refused from 18:35 until the operator topped up by hand the
# next day — a silent outage that read as a quiet tape. Nothing could buy its own gas.
# The SAFETY PROPERTY IS THE CAP. A fixed dollar amount per top-up (no price arithmetic, no
# target-seeking) plus a durable daily cap read from memecoin_gas_topups, so a restart cannot hand a
# misbehaving loop a fresh budget. Off by default; .env turns it on.
MEMECOIN_GAS_TOPUP_ENABLED = os.getenv(
    "MEMECOIN_GAS_TOPUP_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# Trigger. Set ABOVE the entry requirement, not at it: firing at the gate means trading has already
# stopped. ~2x the single-position requirement (0.008918 with the rent term) gives warning.
MEMECOIN_GAS_TOPUP_LOW_SOL = float(os.getenv("MEMECOIN_GAS_TOPUP_LOW_SOL", "0.02"))
# USDC spent per top-up. Small and fixed — the worst a single misfire can do.
MEMECOIN_GAS_TOPUP_USD = float(os.getenv("MEMECOIN_GAS_TOPUP_USD", "3"))
# Hard ceiling per UTC day across all top-ups. Zero disables the ceiling.
MEMECOIN_GAS_TOPUP_DAILY_CAP_USD = float(os.getenv("MEMECOIN_GAS_TOPUP_DAILY_CAP_USD", "10"))
# How often the runner checks. Gas starvation stops trading, so this is checked far more often than
# the rent sweep; the check itself is two balance reads and costs nothing when gas is fine.
MEMECOIN_GAS_TOPUP_PERIOD_S = float(os.getenv("MEMECOIN_GAS_TOPUP_PERIOD_S", "300"))

# POST-TP FOLLOW-UP COLLECTOR (measurement only, 2026-08-26). After a funded take-profit we stop
# observing the coin, so the executable tail reads as "nothing above +50%" when it is really
# "nothing observed above +50%". This prices the tail with real exit quotes for the exact quantity
# sold, at fixed offsets after the close. It gates whether a laddered take-profit is worth building.
# NOT the same thing as MEMECOIN_TP_CONTINUATION_CAPTURE_ENABLED, which samples every second for
# sixty into the research database for the confirmation-window question. Two collectors, two
# questions, two flags — they shared one flag for a single deploy and one env edit started both.
# It is EVIDENCE ONLY: no decision, gate, policy or inventory may read it.
# ~4 quotes per TP close, one at a time; funded TPs ran 12 on 2026-08-25, so ~48/day.
MEMECOIN_POST_TP_FOLLOWUP_ENABLED = os.getenv(
    "MEMECOIN_POST_TP_FOLLOWUP_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# How often the collector runs. Fine to be slow: the horizons are minutes apart and a late sample is
# recorded by its ACTUAL elapsed time, never relabelled to its target.
MEMECOIN_POST_TP_FOLLOWUP_PERIOD_S = float(
    os.getenv("MEMECOIN_POST_TP_FOLLOWUP_PERIOD_S", "30"))
# Samples quoted per pass. One keeps a post-downtime backlog draining gently instead of bursting
# against the provider the live marks share.
MEMECOIN_POST_TP_FOLLOWUP_PER_PASS = int(
    os.getenv("MEMECOIN_POST_TP_FOLLOWUP_PER_PASS", "1"))

# REVIVAL WATCH (operator, 2026-08-26). An m5-floor refusal gets ONE delayed re-ask if the coin's
# tape wakes up inside the window. The floor is mcap-relative, so clearing it in 30s means several
# percent of the whole market cap trading in half a minute — this fires RARELY by construction and
# that is the intent, not a defect. The watch holds no reservation, no slot and no capital; on
# clearance the original event goes back through the normal entry hook and the full gate stack runs.
MEMECOIN_REVIVAL_WATCH_ENABLED = os.getenv(
    "MEMECOIN_REVIVAL_WATCH_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_REVIVAL_WATCH_WINDOW_S = float(os.getenv("MEMECOIN_REVIVAL_WATCH_WINDOW_S", "30"))
# DexScreener refreshes m5 on a ~30s cadence, so the window buys about ONE fresh read; 10s is about
# catching that refresh soon after it lands rather than about resolution.
MEMECOIN_REVIVAL_WATCH_POLL_S = float(os.getenv("MEMECOIN_REVIVAL_WATCH_POLL_S", "10"))
# OTHER HOLDINGS (2026-09-13, operator). Snapshot period, and the proceeds under which a leftover
# is shown but not offered for sale.
MEMECOIN_HOLDINGS_ENABLED = os.getenv("MEMECOIN_HOLDINGS_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_HOLDINGS_PERIOD_S = float(os.getenv("MEMECOIN_HOLDINGS_PERIOD_S", "300"))
MEMECOIN_HOLDINGS_MIN_SELL_USD = float(os.getenv("MEMECOIN_HOLDINGS_MIN_SELL_USD", "0.5"))
# A signed EVM holding sell that never mines (Gamma HIGH on PR #407): with no receipt, its nonce
# unspent and the node holding nothing of ours (pending == confirmed nonce) for this many minutes,
# the operator is paged with the chain, nonce and hash for a cancel. Never rebroadcast, never
# auto-failed: the signed bytes could still mine.
MEMECOIN_EVM_HOLDING_STUCK_PAGE_MIN = float(os.getenv("MEMECOIN_EVM_HOLDING_STUCK_PAGE_MIN", "10"))
# The same sell still pending in a mempool (pending > confirmed nonce, ours unspent) this many minutes:
# likely underpriced, so it can hold the fence forever too (Gamma LOW on 5ae0aaf55). One page to
# replace it with a higher fee or cancel it at that nonce; never rebroadcast, never failed.
MEMECOIN_EVM_HOLDING_MEMPOOL_PAGE_MIN = float(os.getenv("MEMECOIN_EVM_HOLDING_MEMPOOL_PAGE_MIN", "30"))
# The same sell's nonce seen spent by ANOTHER transaction (confirmed nonce past ours, no receipt of
# our hash) settles as failed only after it has held this long: a reorg or a lagging receipt index
# must not read as another transaction.
MEMECOIN_EVM_HOLDING_NONCE_SPENT_CONFIRM_S = float(os.getenv(
    "MEMECOIN_EVM_HOLDING_NONCE_SPENT_CONFIRM_S", "60"))
# OPERATOR WITHDRAWALS (2026-09-13). The ONLY address the dashboard's Withdraw button can pay. The
# web chooses an amount, never a destination. Empty = the button is off.
MEMECOIN_WITHDRAW_SOLANA_ADDRESS = os.getenv("MEMECOIN_WITHDRAW_SOLANA_ADDRESS", "").strip()


def revival_watch_window_s(chain):
    """`MEMECOIN_REVIVAL_WATCH_WINDOW_S_<chain>`, else the global window (2026-09-12).

    DexScreener's 5-minute volume runs 1-2 minutes behind on 4663 (WMR, ITEMS), so a 30 s watch
    expired before the provider could show the volume it waits for.
    """
    raw = os.getenv("MEMECOIN_REVIVAL_WATCH_WINDOW_S_%s" % chain)
    try:
        return float(raw) if raw is not None and str(raw).strip() else float(MEMECOIN_REVIVAL_WATCH_WINDOW_S)
    except (TypeError, ValueError):
        return float(MEMECOIN_REVIVAL_WATCH_WINDOW_S)


def revival_watch_poll_s(chain):
    """`MEMECOIN_REVIVAL_WATCH_POLL_S_<chain>`, else the global poll interval. A long window polls
    slower so a watch does not spend the DexScreener budget one request per second."""
    raw = os.getenv("MEMECOIN_REVIVAL_WATCH_POLL_S_%s" % chain)
    try:
        return float(raw) if raw is not None and str(raw).strip() else float(MEMECOIN_REVIVAL_WATCH_POLL_S)
    except (TypeError, ValueError):
        return float(MEMECOIN_REVIVAL_WATCH_POLL_S)
MEMECOIN_REVIVAL_WATCH_MAX = int(os.getenv("MEMECOIN_REVIVAL_WATCH_MAX", "4"))
# Scope. 100000 is the operator's lean (most useful on small coins). 250000 would mean "every coin
# the floor actually gates", since the floor leg is silent above MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD.
# 0 disables the scope check entirely.
MEMECOIN_REVIVAL_WATCH_MAX_MCAP_USD = float(
    os.getenv("MEMECOIN_REVIVAL_WATCH_MAX_MCAP_USD", "100000"))

# RAILS-DEAF DETECTOR (2026-08-26). `rails_blind_streak` counts sweeps that resolved nothing, so it
# cannot see a sweep that never ran — and that is the worse case. Measured 08-24: 87.1 min holding a
# funded position with an active stop and ZERO marks, worst window 926s, zero alerts. This checks
# TIME outside the sweep instead of counting failures inside it.
# Deliberately NOT MEMECOIN_STUCK_MARK_MAX_AGE_S (600s): that knob answers a different question, and
# overloading one threshold across two questions is how the last one ended up inert.
MEMECOIN_RAILS_BLIND_NOTICE_S = float(os.getenv("MEMECOIN_RAILS_BLIND_NOTICE_S", "60"))
MEMECOIN_RAILS_BLIND_ALERT_S = float(os.getenv("MEMECOIN_RAILS_BLIND_ALERT_S", "120"))
# Re-page cadence while the condition persists, per position. At a 120s maintenance pass an
# un-deduped alert fires every pass for the whole outage, which is how an alert gets muted.
MEMECOIN_RAILS_BLIND_REALERT_S = float(os.getenv("MEMECOIN_RAILS_BLIND_REALERT_S", "300"))

# VOLUME-DEATH SHADOW (2026-08-26). Measures what a silence-based exit WOULD have done. Arms
# nothing. Gamma measured 15x separation (TP winners median 4.4-7.3s interior silence vs timecap
# losers 64.1s) but a fixed 20s cutoff also cuts 9 of 35 winners, so the cost is real and must be
# seen against live outcomes before anyone arms it.
MEMECOIN_VOLDEATH_SHADOW_ENABLED = os.getenv(
    "MEMECOIN_VOLDEATH_SHADOW_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# The operating point under test. Every threshold is logged in ONE row so the cutoff is chosen from
# one live dataset rather than five reruns of the same window.
MEMECOIN_VOLDEATH_CUTOFF_S = float(os.getenv("MEMECOIN_VOLDEATH_CUTOFF_S", "20"))
MEMECOIN_VOLDEATH_THRESHOLDS_S = tuple(
    int(x) for x in os.getenv("MEMECOIN_VOLDEATH_THRESHOLDS_S", "10,15,20,30,60").split(",") if x)
# How recently swap evidence must have landed for ANY coin before this coin's silence counts as
# market quiet rather than our own outage.
MEMECOIN_VOLDEATH_HEALTH_WINDOW_S = float(
    os.getenv("MEMECOIN_VOLDEATH_HEALTH_WINDOW_S", "60"))

# SELL-SHARE ENTRY GATE (operator, 2026-08-26): "we wont enter if we are reading above 80% volume in
# just sells... 80 might even be too conservative the truth might be something around 65%."
# READS COUNTS, NOT VOLUME, and every refusal says so. The volume split needs the pool's own flow,
# and the pool was resolved before entry on 0 of 99 funded positions — a volume gate would fail open
# on every entry. DexScreener's txns.m5 count split is free in the payload the entry path already
# fetches, same 5-minute window the threshold lives in.
# 0 disables. Fail-open on every unknown (chase-guard convention).
MEMECOIN_REAL_MAX_SELL_SHARE_PCT = float(
    os.getenv("MEMECOIN_REAL_MAX_SELL_SHARE_PCT", "80"))
# A 2-transaction window with 2 sells is "100% sells" on nothing. Below this the share is logged and
# ignored — the sample cannot support a refusal.
MEMECOIN_REAL_SELL_SHARE_MIN_TXNS = int(
    os.getenv("MEMECOIN_REAL_SELL_SHARE_MIN_TXNS", "8"))

# SIGNAL-TIME POOL RESOLVER (2026-08-26). Mint -> pool resolution has been a consequence of HOLDING
# a coin (0 of 99 funded entries had a pool before opening), which leaves three observation lanes
# failing open: pre-entry flow reaches 0.25% of attempts, the volume-death shadow is blind on 31% of
# holds, and the sell-share gate's volume evidence almost never fires. One input, three consumers.
# Entry takes ~1.8s and resolution ~1-3s, so the FIRST signal on a new mint still loses the race;
# the win is every later signal, the revival watch's 30s window, and open-position tape coverage.
MEMECOIN_SIGNAL_POOL_RESOLVER_ENABLED = os.getenv(
    "MEMECOIN_SIGNAL_POOL_RESOLVER_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# A signal burst must not become an RPC burst. Uses the RESEARCH key, never the funded pool and
# never the capture rotator.
MEMECOIN_SIGNAL_POOL_RESOLVER_MAX_PER_MIN = int(
    os.getenv("MEMECOIN_SIGNAL_POOL_RESOLVER_MAX_PER_MIN", "10"))
# A hot mint re-signalled every poll is resolved once per TTL, not once per signal.
MEMECOIN_SIGNAL_POOL_RESOLVER_TTL_S = float(
    os.getenv("MEMECOIN_SIGNAL_POOL_RESOLVER_TTL_S", "900"))
MEMECOIN_SIGNAL_POOL_RESOLVER_MAX_PENDING = int(
    os.getenv("MEMECOIN_SIGNAL_POOL_RESOLVER_MAX_PENDING", "64"))
# entry price-impact hard cap: abort (don't chase) a quote whose impact exceeds this % of notional.
MEMECOIN_ENTRY_SLIPPAGE_PCT = float(os.getenv("MEMECOIN_ENTRY_SLIPPAGE_PCT", "3"))
# SPLIT KNOBS (2026-07-11): one constant was doing two unrelated jobs —
# (a) IMPACT_CAP: pre-flight abort when the QUOTE itself says our order moves the pool more
#     than this (self-inflicted cost, known before send). The economic guard.
# (b) TX_SLIPPAGE: Jupiter slippageBps = how much worse than the quote the FILL may be before
#     the tx reverts on-chain (movement/MEV between quote and land; exceeded -> revert ~= gas
#     only). Too tight reverts fills at exactly the pump moments; too wide is sandwich budget
#     (bounded: tolerance x clip, e.g. 8% x $50 = $4 worst case).
# Defaults fall back to the legacy fused knob so unconfigured envs behave as before.
MEMECOIN_ENTRY_IMPACT_CAP_PCT = float(os.getenv("MEMECOIN_ENTRY_IMPACT_CAP_PCT",
                                                str(MEMECOIN_ENTRY_SLIPPAGE_PCT)))
MEMECOIN_ENTRY_TX_SLIPPAGE_PCT = float(os.getenv("MEMECOIN_ENTRY_TX_SLIPPAGE_PCT",
                                                 str(MEMECOIN_ENTRY_SLIPPAGE_PCT)))
# Raydium venue-fallback quotes (2026-08-14): when Jupiter has NO route (coin too new to be
# indexed), quote + build against Raydium's official trade API instead. Jupiter always answers
# first, so indexed coins are byte-identical. 0 disables the fallback entirely.
MEMECOIN_SOLANA_VENUE_FALLBACK = os.getenv(
    "MEMECOIN_SOLANA_VENUE_FALLBACK", "1").strip().lower() in {"1", "true", "yes"}
# A RAYDIUM ENTRY QUOTE FAR ABOVE JUPITER'S IS A PHANTOM (2026-09-27, Alpha GO). LUCKY ee561e40:
# Raydium's API quoted 1.32x Jupiter, won the thin-route contest, set the min-out, and the chain's
# route produced Jupiter's number to 0.015% -> router 0x26. ATqKVV 4b839bbb: 1.445x, refused at
# simulation; 10 s later the venues agreed within 0.5%. Honest pairs agree within ~1%. On an ENTRY
# (USDC or SOL in), a Raydium win above this multiple of Jupiter's out returns Jupiter's quote
# instead. 0 = off (today's rule). Exits never pass through it.
MEMECOIN_SOLANA_VENUE_FALLBACK_MAX_RATIO = float(
    os.getenv("MEMECOIN_SOLANA_VENUE_FALLBACK_MAX_RATIO", "1.15") or 0)
# ...unless Jupiter's own priceImpactPct (a fraction) says its route is dust: then the bigger
# Raydium answer is the real pool, the PEECOIN rescue (2026-08-14, an 83% Jupiter round trip).
MEMECOIN_SOLANA_VENUE_FALLBACK_DUST_IMPACT = float(
    os.getenv("MEMECOIN_SOLANA_VENUE_FALLBACK_DUST_IMPACT", "0.50") or 0.50)
# VENUES THE SOLANA ENTRY QUOTE NEVER ROUTES THROUGH (2026-09-23, operator). Jupiter dex labels,
# comma-separated. Denali (program DNL1tgEj…) carries a per-pool volume cap: both funded buys
# Jupiter ever routed through it (Ethena 91679b13, hyENA 69601c34, 2026-09-23, 0 successes)
# reverted on chain with VolumeCapExceeded, paid the fee, and filled only on the retry lane at a
# worse price. Entry only; exits keep every route. Unknown labels are ignored by Jupiter, so a typo
# excludes nothing. Set to an empty value to disable.
MEMECOIN_SOLANA_EXCLUDE_DEXES = os.getenv("MEMECOIN_SOLANA_EXCLUDE_DEXES", "Denali")
# Token-2022 fees at or below this limit can pass only when the fee cannot change.
MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS = int(os.getenv(
    "MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS", "300"))
# A LIVE TRANSFER-FEE AUTHORITY CANNOT REACH US (2026-09-06, operator approved). The gate refused
# any token-2022 mint whose `transferFeeConfigAuthority` was set, on the reasoning that the holder
# could raise the fee to 100% and strand the position. Token-2022 does not let them do it in time:
# a fee change is written as `newerTransferFee` stamped with a FUTURE epoch and the older value
# applies until that boundary. A Solana epoch is about 2 days. Our holds are minutes to hours, so
# the earliest a hike can bite is long after we are out.
#
# Measured the same day, 30 Solana mints this gate refused: 19 carry no transfer-fee extension at
# all (they were refused for freeze, mint authority or permanent delegate, not for this), and of
# the 11 that do, NONE has ever raised its fee and none sits at or above 10%. Ten are at 3% and one
# at 1%. TOA and BUTT -- four refusals across three armed wallets inside three minutes -- share one
# authority, 5KXDF6QnqhBj72hDtJNkkpFaQVUfbFXNybMsp3DiK6tD, which is a launchpad taking a creator
# fee rather than two independent ruggers.
#
# WHAT THIS DOES NOT RELAX. The CURRENT fee must still be inside
# MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS, so a mint that is already confiscatory is still
# refused. Freeze authority, mint authority and permanent delegate are untouched -- a permanent
# delegate needs no epoch and can take the balance now, which is the rug this one is not.
# Set to 0 to restore the old refusal.
MEMECOIN_SOLANA_ALLOW_LIVE_TRANSFER_FEE_AUTHORITY = os.getenv(
    "MEMECOIN_SOLANA_ALLOW_LIVE_TRANSFER_FEE_AUTHORITY", "1").strip().lower() in {"1", "true", "yes"}

# mcap-TIERED impact cap (2026-07-15): a flat IMPACT_CAP refused the small-cap pumps that ARE the
# edge — a thin sub-$1M pool structurally quotes higher impact for the same clip, and a coin that can
# 2-3x absorbs more entry impact than a $20M coin that barely moves (SOLdiers $677.7k aborted at
# 9.37% vs the 5% flat cap). Tiers are "<mcap_usd_ceiling>:<cap_pct>" pairs; the FIRST ascending
# ceiling the mcap is UNDER wins. mcap at/above the top ceiling — or unknown — keeps the flat base
# cap (never loosen on absent data). Data (n=18 real quotes): accepted fills all <5%; aborts one at
# 7.1%, one at 9.4%, then a wall at 18-25% (dead-pool liquidity, -EV round-trip). 12% clears the
# borderline small-caps and keeps the wall out. Solana-only — the EVM leg has no impact gate.
# nano tier added 2026-07-17: sub-$50K pools are so thin the 12% cap auto-blocked the class
# the micro ladder exists for; 15% on a $50 clip bounds the entry cost at ~$7.50 worst case.
MEMECOIN_ENTRY_IMPACT_CAP_TIERS = os.getenv(
    "MEMECOIN_ENTRY_IMPACT_CAP_TIERS", "50000:15,1000000:12,5000000:8,30000000:6")


def _parse_impact_cap_tiers(raw):
    tiers = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ceil_s, cap_s = part.split(":")
            tiers.append((float(ceil_s), float(cap_s)))
        except (ValueError, TypeError):
            continue   # skip a malformed pair rather than break the money path at import
    tiers.sort(key=lambda t: t[0])
    return tiers


_IMPACT_CAP_TIERS = _parse_impact_cap_tiers(MEMECOIN_ENTRY_IMPACT_CAP_TIERS)


def entry_impact_cap_for_mcap(mcap):
    """Price-impact abort cap (%) for a signal-time market cap. Unknown mcap (None / non-numeric /
    non-positive) or a cap at/above the top tier -> the flat base MEMECOIN_ENTRY_IMPACT_CAP_PCT
    (conservative: NEVER loosen on absent data — a gate that fires on missing data is the safe
    failure). Smaller caps get a looser cap because thin pools quote higher impact for the same
    clip and small-cap pumps absorb it. A tier never TIGHTENS below the operator's base floor."""
    base = MEMECOIN_ENTRY_IMPACT_CAP_PCT
    try:
        mc = float(mcap)
    except (TypeError, ValueError):
        return base
    if not mc > 0:   # None already raised; catches 0, negatives, NaN
        return base
    for ceil_usd, cap_pct in _IMPACT_CAP_TIERS:
        if mc < ceil_usd:
            return max(cap_pct, base)
    return base


# ROUND-TRIP cap (2026-08-12): the impact cap above bounds what we PAY to get in. Nothing bounded
# what the pool RETURNS on the way out, and the exit ladder below WIDENS tolerance until the sell
# lands, so a one-sided pool was uncapped in the only direction that empties the bag. KALMAN
# (position 36192) bought inside its 15% nano cap and sold the whole $36.95 bag back for $17.02 in
# 86 s -- its pool held 3.886 SOL and our own sell raised its token reserve 22.1% in one
# transaction. Its mint also carries an immutable 3% Token-2022 transfer fee, charged on both legs.
#
# The cap is a FACTOR of the resolved one-way cap, not an absolute number, for two reasons: the two
# gates can never contradict each other, and a round trip through one pool costs about the one-way
# twice. So this refuses ASYMMETRY -- a sell side far worse than the buy side the entry cap already
# approved -- and leaves size to the tier ladder. 2.0 against the nano tier gives a 30% refusal
# line; 25 real Solana funded closes since 2026-08-05 have a median round trip of -0.8%, so the
# line sits far outside normal trade. Set the absolute knob only from a measured distribution.
MEMECOIN_ENTRY_ROUNDTRIP_CAP_FACTOR = float(
    os.getenv("MEMECOIN_ENTRY_ROUNDTRIP_CAP_FACTOR", "2"))
MEMECOIN_ENTRY_ROUNDTRIP_CAP_PCT = os.getenv("MEMECOIN_ENTRY_ROUNDTRIP_CAP_PCT")
# The round-trip budget as a FRACTION OF THE POSITION'S OWN TAKE-PROFIT, mirroring the EVM rule
# (MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC). Same default, 0.4, so at today's TP of 25 the
# budget is exactly 10.0 and the flat MEMECOIN_ENTRY_ROUNDTRIP_CAP_PCT=10 it replaces is reproduced
# to the float (0.4*25.0 == 10.0 exactly). Operator decision 2026-09-08: the two funded chains
# capped one concept under two rules and agreed only by arithmetic accident, so a TP retune moved
# the EVM budget and left Solana's stale. Set to 0 to return to the absolute knob.
MEMECOIN_ENTRY_ROUNDTRIP_CAP_TP_FRAC = float(
    os.getenv("MEMECOIN_ENTRY_ROUNDTRIP_CAP_TP_FRAC", "0.4") or 0.0)

# OVERSIZE RETRY (2026-08-23). A Solana packet is capped at 1232 bytes and a larger signed swap is
# refused before broadcast. Three refusals on 2026-08-23 were 1346, 1248 and 1259 bytes: two of the
# three missed by 16 and 27 bytes. Across 8 oversized payloads since 2026-08-10 the overshoot was
# +3, +11, +16, +21, +114, +121, +124 and +239 bytes.
#
# `maxAccounts` caps the route's account-key count, which is what drives the packet size. Measured
# on three live routes it saved 31, 136 and 206 bytes for 0.0%, 0.02% and 2.0% fewer tokens out.
# There is a hard cliff below it: every mint tested returned NO ROUTE at 40 and at 32, so 48 is not
# a dial, it is the one setting that works.
#
# The retry ALSO excludes the first hop's venue (2026-09-14). maxAccounts counts accounts, not
# bytes: HumidiFi's accounts are not in Jupiter's lookup tables, so a USDC -> SOL hop through it
# costs 200-330 bytes more at the same count, and the capped re-quote kept or even chose it
# (1,473 -> 1,442 bytes, refused). See `_oversize_retry_exclusions` in solana_swap_executor.
#
# The retry runs ONLY after a first build came back oversized, so the normal 97% of entries send no
# key and keep today's routes and today's pricing. The blended cost of the 2% worst case over a 2.7%
# retry rate is about 0.02%.
#
# 0 disables the retry. The re-quote re-runs EVERY price gate (impact, sell route, round trip, chase,
# post-peak) because the retry re-enters execute_entry rather than splicing a new quote past them.
MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS = int(
    os.getenv("MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS", "48"))

# SOLANA PRE-BALANCE READS START WITH THE QUOTE (2026-09-27, Alpha GO "L2"). The executor's USDC and
# token reads (the fill-delta baseline) used to start after every price gate: p50 112.7 / p90 138.7
# ms on top of the quote. They now start at the top of execute_entry, inside the wallet lock, and
# are joined where they used to run. 0 = the old read-after-the-gates path (runner restart).
MEMECOIN_ENTRY_OVERLAP_PRE_READS = os.getenv(
    "MEMECOIN_ENTRY_OVERLAP_PRE_READS", "1").strip().lower() in {"1", "true", "yes", "on"}
# THE J7 NATIVE QUOTE (2026-09-28, design section 12 B1). off | shadow | live, default shadow:
# a general-path Solana entry on a pump.fun curve or canonical PumpSwap pool reads the pool on its
# own thread beside the Jupiter quote and logs both side by side ("native quote shadow"). It is
# never used. "live" runs as shadow until Alpha's GO on the shadow read (n >= 20 qualifying
# decisions, native within 0.5% of Jupiter on the same reserves slot, 0 model misses).
MEMECOIN_ENTRY_NATIVE_QUOTE = os.getenv("MEMECOIN_ENTRY_NATIVE_QUOTE", "shadow").strip().lower()

# SOLANA GATE READS RIDE THE MARKET-DATA GET (2026-09-27, Alpha GO "B"). The gate's bankroll read
# (p50 44 / p90 87 ms) and the token-safety mint read (22 / 43 ms) ran after the DexScreener GET
# (p50 233 / p90 493 ms), which neither depends on; they now start with it and the gate joins
# them. 0 = the old order (runner restart).
MEMECOIN_ENTRY_EARLY_GATE_READS = os.getenv(
    "MEMECOIN_ENTRY_EARLY_GATE_READS", "1").strip().lower() in {"1", "true", "yes", "on"}
# THE J7 SOLANA EARLY ENTRY QUOTE (2026-09-28, Alpha GO option 4; memecoin_early_quote.py). The
# Jupiter entry quote starts at decision start, beside the GET and the reservation stage, for the
# clip the risk envelope would approve. The executor uses it only for that exact clip and while it
# is younger than MEMECOIN_ENTRY_EARLY_QUOTE_MAX_AGE_S; anything else discards it and quotes as
# before. Needs MEMECOIN_ENTRY_EARLY_GATE_READS (the clip is sized from those balances).
# 0 = today's path (runner restart).
MEMECOIN_ENTRY_EARLY_QUOTE = os.getenv(
    "MEMECOIN_ENTRY_EARLY_QUOTE", "1").strip().lower() in {"1", "true", "yes", "on"}
MEMECOIN_ENTRY_EARLY_QUOTE_MAX_AGE_S = float(
    os.getenv("MEMECOIN_ENTRY_EARLY_QUOTE_MAX_AGE_S", "1.5"))
# ON-CHAIN LANE EARLY QUOTE, C1 (spec 2026-10-01-memecoin-onchain-lane-latency-design.md, operator
# order 2026-10-01). An on-chain lane event carries no market cap (OnchainEv.their_market_cap is
# None), so the pre-screen above skipped the early quote on every on-chain decision and its quote
# waited for the market-data GET. 1 = an UNKNOWN mcap passes the pre-screen for signal_source
# 'onchain_watch' only (a J7 event with an unknown mcap still skips); every other early-quote rule
# and every gate after the GET is unchanged. 0 (default) = today's path. Runner restart.
MEMECOIN_ENTRY_EARLY_QUOTE_ONCHAIN = os.getenv(
    "MEMECOIN_ENTRY_EARLY_QUOTE_ONCHAIN", "0").strip().lower() in {"1", "true", "yes", "on"}
# RAYDIUM BESIDE JUPITER ON ENTRY QUOTES, C2 (same spec). The thin-route second opinion
# (_quote_with_fallback) asked Raydium only after Jupiter answered: 28 of 61 on-chain lane quotes took
# it at p50 442-511 ms against 107 ms for the rest. 1 = on the ENTRY quote seam only, the Raydium
# quote starts together with Jupiter's on a 2-worker pool (both busy = today's serial path); the
# decision rule is unchanged and a non-thin route drops the Raydium answer unread. 0 (default) =
# today's path. Runner restart.
MEMECOIN_ENTRY_RAYDIUM_PARALLEL = os.getenv(
    "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", "0").strip().lower() in {"1", "true", "yes", "on"}
# THE ENTRY NEVER WAITS FOR A LATE RAYDIUM SECOND OPINION (2026-10-02, operator yes via Alpha). With
# the parallel Raydium quote above, a thin Jupiter quote used to block on Raydium: quote p50 460 vs 107
# ms, and Raydium changed the quote in 6 of 77 thin cases. 1 (default) = Raydium's answer counts only
# if it is already done when Jupiter lands; otherwise Jupiter decides and the late answer is recorded
# as a shadow (data/raydium_late_shadow.jsonl). 0 = today's blocking comparison. Runner restart.
MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING = os.getenv(
    "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", "1").strip().lower() in {"1", "true", "yes", "on"}
# SOLANA ROUND-TRIP REFUSAL RE-QUOTE (2026-09-28, operator "start building"). A round-trip refusal
# the one-bank sequential simulation did not confirm (transaction_limits, unsupported_route, ...)
# re-runs the entry ONCE from the quote step on a fresh quote, every gate included, holding the
# reservation; the refusal stands only if the fresh quote also fails. SHORK (attempt 7fa44410):
# Jupiter's buy quote gave 27.9% fewer tokens than a quote 3.0 s later and than every on-chain
# trade in its slot. 7 of 82 round-trip refusals since 09-14 re-quoted >10% better. Costs about
# one quote + one reverse quote, only on would-be refusals. 0 = refuse on the first reading.
MEMECOIN_ENTRY_ROUNDTRIP_REQUOTE = os.getenv(
    "MEMECOIN_ENTRY_ROUNDTRIP_REQUOTE", "1").strip().lower() in {"1", "true", "yes", "on"}
# SOLANA ROUND-TRIP RESIZE (2026-09-28, operator "go on the round-trip rebuild"). A round trip over
# the cap mixes a FIXED cost (route fees, transfer tax: what a tiny buy also pays) with OUR SIZE's
# impact. Before refusing, a probe buy+sell of MEMECOIN_ENTRY_ROUNDTRIP_PROBE_USD prices the fixed
# part. Fixed cost >= TARGET_FRACTION x cap: refuse (a trap at any size). Otherwise the clip shrinks,
# assuming size cost scales with the clip, to land at TARGET_FRACTION x cap, and the entry re-runs
# ONCE at that clip with every gate. Below MIN_FRACTION of the clip (or MEMECOIN_MIN_TRADE_USD) it
# refuses. SHORK 7fa44410: probe 7.6%, clip 30.6%. 0 = refuse as before.
MEMECOIN_ENTRY_ROUNDTRIP_RESIZE = os.getenv(
    "MEMECOIN_ENTRY_ROUNDTRIP_RESIZE", "1").strip().lower() in {"1", "true", "yes", "on"}
MEMECOIN_ENTRY_ROUNDTRIP_PROBE_USD = float(os.getenv("MEMECOIN_ENTRY_ROUNDTRIP_PROBE_USD", "1.0"))
MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_TARGET_FRACTION = float(
    os.getenv("MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_TARGET_FRACTION", "0.8"))
MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_MIN_FRACTION = float(
    os.getenv("MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_MIN_FRACTION", "0.25"))
# EVM ROUND-TRIP RESIZE (operator 2026-10-14: "make the blocker smart, not a higher bar"; 73849).
# The EVM ladder's round-trip refusal gets the Solana rule above: a probe of PROBE_USD prices the
# fixed cost, the clip shrinks to land at TARGET_FRACTION x the SAME budget, one re-quote at that
# clip decides, and every later gate still runs. 0 = refuse as before.
# FAST-LANE DEXSCREENER ON A KEPT-ALIVE CONNECTION (operator 2026-10-14; 73851). The 56 and 4663 lanes'
# market-stats reads share one pooled client pinged every MEMECOIN_DEX_WARM_INTERVAL_S (a cold urllib
# GET measured 41-237 ms, a warm one 14-20 ms). 0 = today's per-call urllib worker.
MEMECOIN_DEX_WARM_HTTP = os.getenv("MEMECOIN_DEX_WARM_HTTP", "1").strip().lower() in {"1", "true", "yes", "on"}
MEMECOIN_DEX_WARM_INTERVAL_S = float(os.getenv("MEMECOIN_DEX_WARM_INTERVAL_S", "20"))
MEMECOIN_EVM_ENTRY_ROUNDTRIP_RESIZE = os.getenv(
    "MEMECOIN_EVM_ENTRY_ROUNDTRIP_RESIZE", "1").strip().lower() in {"1", "true", "yes", "on"}
# SOLANA EXIT READS OVERLAP (2026-09-28, Alpha GO "Step 1"; solana_exit_executor.execute_exit).
# Each exit rung read the token balance three times and USDC once, serially, before broadcast.
# Now: one snapshot gives the rung's human and raw balance, the USDC pre-read rides beside the
# quote, and the signing re-read rides beside the /swap build, still judged by verify_exit_intent
# before anything is recorded or sent. 0 = the serial order (runner restart).
MEMECOIN_EXIT_OVERLAP_READS = os.getenv(
    "MEMECOIN_EXIT_OVERLAP_READS", "1").strip().lower() in {"1", "true", "yes", "on"}
# THE EVM GATE READS RIDE THE GET TOO (2026-09-27, Alpha GO "A"). On 4663 `_gate_balances` (WETH +
# ETH/USD || native) ran after every gate inside a 319 ms p50 gap before the reservation; it depends
# on the chain and wallet, never the coin. 0 = the old order (runner restart).
MEMECOIN_ENTRY_EARLY_GATE_READS_EVM = os.getenv(
    "MEMECOIN_ENTRY_EARLY_GATE_READS_EVM", "1").strip().lower() in {"1", "true", "yes", "on"}

# NATIVE PUMP.FUN EXIT TAIL CAP (2026-09-22, ONLY ref e4952cc0). The native sell_v2 composite
# carries 26 static accounts before Jupiter's payout -> USDC tail is added. With Jupiter's best
# HIMS tail it assembled at 1,392 bytes against the 1,232-byte packet and the entry was refused;
# the same tail capped at this many accounts assembled at 1,029. The builder tries Jupiter's best
# tail first, then this cap, then onlyDirectRoutes, and stops at the first that fits. 0 skips the
# capped shape.
MEMECOIN_PUMP_NATIVE_EXIT_TAIL_MAX_ACCOUNTS = int(
    os.getenv("MEMECOIN_PUMP_NATIVE_EXIT_TAIL_MAX_ACCOUNTS", "20"))

# FOMO THROTTLE WAIT CAP (2026-08-23). The inter-call gap is GLOBAL: the main poll loop and the
# armed follow-fast lane (one call per armed wallet) share one guard. At an escalated `gap_s` each
# queued caller ratchets `_last_call` forward by another gap, so a lane with a dozen wallets pushes
# the main thread's wait minutes out. The main thread writes the heartbeat, so it went stale at
# 120 s (MEMECOIN_HEARTBEAT_MAX_AGE_S), the watchdog killed the runner as wedged, and the funded
# lane lost about a minute of coverage. Caught live 2026-08-23 15:33-15:36 by SIGUSR2.
#
# A LEGITIMATE single-call wait never exceeds `gap_s`, whose ceiling is 30 s. Anything above that is
# queueing behind other callers. 45 s therefore permits a full wait at maximum escalation plus
# slack, and refuses only a real backlog, while staying far under the 120 s staleness bound.
# STEADY PACING, SO THE GUARD IS NOT ONLY "FLAT OUT" OR "PUNISHED" (2026-09-06).
# The guard's gap starts at 0 and decays back to 0, so the follow lanes fire as fast as they can
# until fomo answers 429, back off to a 16 s gap, recover, and hammer again. Measured that day:
# 64 throttle episodes, 16,918 s elevated, 82 engagements. The fast lane polls all 64 armed wallets
# with a 1 s sleep across 4 workers, and at an escalated gap a 64-wallet sweep takes SEVENTEEN
# MINUTES -- which is the armed-wallet REST lane's 21 s mean lag and its 191 s worst case.
#
# A small permanent floor keeps every call under the limit instead of discovering it by being
# refused, and it makes the lane FASTER on average: a steady 0.25 s gap sweeps 64 wallets in 16 s,
# where the punitive gap sweeps them in 17 minutes. 0 preserves the previous behaviour exactly.
MEMECOIN_FOMO_BASE_GAP_S = float(os.getenv("MEMECOIN_FOMO_BASE_GAP_S", "0"))

# THE GLOBAL FEED IS A TELESCOPE, NOT A TRADING SIGNAL (2026-09-06, operator approved).
# It is the ONLY way we see wallets we do not already follow — `memecoin_live_wallet_ranking`
# restricts to signal_source='global_feed' precisely because follow_activity rows exist only for
# wallets we already follow — and 24 armed wallets were found through it. So it stays.
#
# But it must not run at entry cadence. Measured over 14 days it produced 16 real fills for -$79.9
# and 6,383 paper positions for -$8,772.6, while the armed lane produced 394 real fills for +$942.8.
# And it spends 14x the fomo request budget of the armed lane (7,609 events per 10 min against 558),
# so every armed-wallet poll queues behind it on the shared rate guard — which is what made the
# armed REST lane's lag 21 s mean and 191 s worst.
#
# Discovery adds 1-40 wallets a day. That does not need a 5 s poll. This gates ONLY the feed fetch,
# NOT the loop: the loop period also drives the ~15 s stop-loss bracket sweep, and slowing that to
# slow the feed is the exact coupling that let a feed backoff hold a stop-loss on 2026-09-02.
# 0 keeps the previous behaviour (fetch every cycle).
MEMECOIN_GLOBAL_FEED_PERIOD_S = float(os.getenv("MEMECOIN_GLOBAL_FEED_PERIOD_S", "0"))

# Real entries from the global feed. Default ON preserves the previous behaviour; the evidence for
# turning it off is the -$79.9 over 16 fills above, against +$942.8 from the armed lane.
MEMECOIN_REAL_GLOBAL_FEED_ENTRIES = os.getenv(
    "MEMECOIN_REAL_GLOBAL_FEED_ENTRIES", "1").strip().lower() in {"1", "true", "yes"}

# 0 disables the cap and restores the old unbounded sleep.
MEMECOIN_FOMO_MAX_THROTTLE_WAIT_S = float(
    os.getenv("MEMECOIN_FOMO_MAX_THROTTLE_WAIT_S", "45"))
# HOW LONG THE MAIN LOOP'S FEED POLL MAY STAND INSIDE THE BACKOFF (2026-09-06).
#
# THE BACKOFF IS NOT SHORTENED BY THIS. `before_call` refuses a wait longer than its cap WITHOUT
# ratcheting `_last_call`, so a skipped poll costs no one else their turn; the guard's `gap_s` and
# the 45 s cap above are untouched for every caller that genuinely must wait.
#
# WHY THE LOOP IS DIFFERENT. `_fetch_s` INCLUDES the guard's sleep -- it is measured around
# `fomo_get`, and `fomo_request` sleeps before the call -- so `fomo_fetch=28.6s` on 2026-09-06 was
# our own backoff after a 429, doing exactly what it was built to do. The wrong fix is to shorten
# it: `finding-memecoin-fomo-account-ban-2026-08-03` is what continuing to call a host that has
# just refused you buys, a valid token and a 403 and no feed at all.
#
# The right fix is to stop STANDING STILL inside it, because THE EXIT SWEEP SHARES THIS LOOP. A
# blocked cycle stops every rail on every open position: on 2026-09-02 the loop went 143 s without a
# cycle, and position 48565 -- confirmed approved stop at -41.5% since 18:51:19 -- did not sell
# until 18:55:27 at -81.3%, so $27.80 of a $58.08 loss happened after we had decided to sell. A feed
# backoff must never be able to hold a stop-loss.
#
# So the loop refuses the poll and carries on to the sweep. It skips feed polls for as long as the
# guard says, which is the backoff honoured to the second.
MEMECOIN_FOMO_POLL_MAX_WAIT_S = float(
    os.getenv("MEMECOIN_FOMO_POLL_MAX_WAIT_S", "3"))

# MANUAL ENTRY CLIP (2026-08-23). A manual buy is the operator naming a coin, so it must not take
# the bankroll clip (equity / MEMECOIN_MAX_CONCURRENT = the WHOLE balance). It uses this fixed
# size, or whatever `size_usd` the caller passes. The value still goes through the full risk gate
# -- bankroll, exposure, concurrency and the min-trade floor all validate against it -- so an
# override larger than the venue holds is refused, never clamped (reservation == spend).
MEMECOIN_MANUAL_TRADE_SIZE_USD = float(
    os.getenv("MEMECOIN_MANUAL_TRADE_SIZE_USD", "20"))

# OPERATOR OVERRIDE CAP (2026-09-22, operator order). A manual entry with override=True bypasses
# every selection AND most safety gates (kept: kill switch, mechanical route/quote existence) --
# see memecoin_manual_intents.py and real_entry_hook's `override` handling. This is the ONE limit
# an override cannot cross: enforced SERVER-SIDE, always, regardless of what the request names or
# what the automatic sizing would have picked. A knob, not a literal, so the operator can find and
# change it without a code edit -- but the default is exactly the number the operator named.
MEMECOIN_MANUAL_OVERRIDE_MAX_USD = float(
    os.getenv("MEMECOIN_MANUAL_OVERRIDE_MAX_USD", "175"))

# DESK LANE (operator 2026-10-21, rulings a10fde26 / 61859b83; spec
# 2026-10-21-desk-execution-plumbing-design.md §1.5). The model is the trader on this lane; these
# are the operator's limits on it. Values live in .env. Every default fails CLOSED: off, no chain,
# no clip, no slots, no loss budget, no rail bounds.
# Desk INTAKE phase 1 (collect only; scripts/memecoin_desk_intake.py). Kill knob + per-feed polite pass caps (one GET
# per list per pass) and the refusal backoff ladder (seconds; a 403/429/challenge is never retried through).
MEMECOIN_DESK_INTAKE_ENABLED = os.getenv("MEMECOIN_DESK_INTAKE_ENABLED", "1").strip() == "1"
MEMECOIN_DESK_INTAKE_PUMPFUN_PASS_S = float(os.getenv("MEMECOIN_DESK_INTAKE_PUMPFUN_PASS_S", "60"))
MEMECOIN_DESK_INTAKE_DEXSCREENER_PASS_S = float(os.getenv("MEMECOIN_DESK_INTAKE_DEXSCREENER_PASS_S", "60"))
# Retention: day files older than KEEP_DAYS are pruned (polls archived, first_sightings kept forever). OFF by default
# with a reason (Gamma 2026-10-27): no day may be pruned before Gamma posts that feed's results doc.
MEMECOIN_DESK_INTAKE_PRUNE_ENABLED = os.getenv("MEMECOIN_DESK_INTAKE_PRUNE_ENABLED", "0").strip() == "1"
MEMECOIN_DESK_INTAKE_KEEP_DAYS = int(os.getenv("MEMECOIN_DESK_INTAKE_KEEP_DAYS", "30"))
MEMECOIN_DESK_INTAKE_BACKOFF_S = tuple(float(x) for x in os.getenv("MEMECOIN_DESK_INTAKE_BACKOFF_S", "600,1800,3600").split(","))
MEMECOIN_DESK_ENABLED = os.getenv("MEMECOIN_DESK_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on"}
# Large-buyer pilot (operator 2026-10-31 via Alpha): a deterministic desk cohort, no LLM. Default off.
MEMECOIN_LARGE_BUYER_ENABLED = os.getenv("MEMECOIN_LARGE_BUYER_ENABLED", "0").strip() == "1"
MEMECOIN_LARGE_BUYER_MIN_OTHERS = int(os.getenv("MEMECOIN_LARGE_BUYER_MIN_OTHERS", "1"))   # CROWD: one prior qualifying wallet in 60 s; frozen in its own cohort
MEMECOIN_LARGE_BUYER_MAX_FILLS = int(os.getenv("MEMECOIN_LARGE_BUYER_MAX_FILLS", "30"))
MEMECOIN_DESK_CHAINS = frozenset(
    c.strip().lower() for c in os.getenv("MEMECOIN_DESK_CHAINS", "").split(",") if c.strip())
MEMECOIN_DESK_CLIP_USD = float(os.getenv("MEMECOIN_DESK_CLIP_USD", "0"))
MEMECOIN_DESK_MAX_CONCURRENT = int(os.getenv("MEMECOIN_DESK_MAX_CONCURRENT", "0"))
MEMECOIN_DESK_DAILY_LOSS_USD = float(os.getenv("MEMECOIN_DESK_DAILY_LOSS_USD", "0"))
# Desk cohort cost gate (operator yes 2026-10-22): an unknown cost is valued at a conservative upper bound in the
# cumulative-kill check, never 0. LEGACY=1 restores the old rule (any unknown refuses every entry). The SOL price
# turns the lamport caps into USD; it is a pessimistic constant, not a quote (no provider call on the gate).
MEMECOIN_DESK_COST_GATE_LEGACY = os.getenv("MEMECOIN_DESK_COST_GATE_LEGACY", "0").strip() == "1"
MEMECOIN_DESK_BOUND_SOL_USD = float(os.getenv("MEMECOIN_DESK_BOUND_SOL_USD", "300"))
MEMECOIN_DESK_UNKNOWN_COST_PAGE_S = float(os.getenv("MEMECOIN_DESK_UNKNOWN_COST_PAGE_S", "3600"))

# A desk seat's rails must each fall inside an operator range; a value outside is REJECTED, never
# clamped. Format: "rail:lo-hi,rail:lo-hi" over tp_pct / sl_pct / trail_pct / derisk_pct, e.g.
#   MEMECOIN_DESK_RAIL_BOUNDS=tp_pct:15-150,sl_pct:10-40,trail_pct:8-30
# A rail with no range may not be set by the seat at all. sl_pct is REQUIRED on every desk entry
# (custom rails switch an unnamed stop OFF), so a value without an sl_pct range, or any malformed
# value, parses to {} and every desk entry is refused.
DESK_RAIL_FIELDS = ("tp_pct", "sl_pct", "trail_pct", "derisk_pct")
DESK_REQUIRED_RAILS = ("sl_pct",)


def parse_desk_rail_bounds(raw):
    out = {}
    try:
        for part in str(raw or "").split(","):
            if not part.strip():
                continue
            name, _, span = part.strip().partition(":")
            lo, sep, hi = span.partition("-")
            if name not in DESK_RAIL_FIELDS or not sep or name in out:
                return {}
            lo, hi = float(lo), float(hi)
            if not (math.isfinite(lo) and math.isfinite(hi) and 0 < lo <= hi):
                return {}
            out[name] = (lo, hi)
    except ValueError:
        return {}
    return out if all(r in out for r in DESK_REQUIRED_RAILS) else {}


MEMECOIN_DESK_RAIL_BOUNDS = parse_desk_rail_bounds(os.getenv("MEMECOIN_DESK_RAIL_BOUNDS", ""))
# DESK VERDICT PRICE CAP (operator 2026-10-22, S6: "make sure it just won't happen"). A desk entry never
# fills above the price its verdict saw: ref = the U snapshot's curve price, cap = ref x (1 + tolerance +
# the modelled curve fee + the modelled impact of the clip on the snapshot's real quote reserves). The
# executor refuses a quote above it and signs that minimum, so the tx fails instead of filling worse.
MEMECOIN_DESK_VERDICT_TOLERANCE_PCT = float(os.getenv("MEMECOIN_DESK_VERDICT_TOLERANCE_PCT", "3"))
MEMECOIN_DESK_VERDICT_FEE_PCT = float(os.getenv("MEMECOIN_DESK_VERDICT_FEE_PCT", "1.25"))   # pump 95+30 bps
# A desk quote closer than this to the verdict-price cap is not sent (2026-10-23, T/ACC: 0.19%
# headroom on a 0.5%/s climb landed past the floor and paid the fee). 0 = old behaviour.
MEMECOIN_DESK_CAP_MIN_HEADROOM_PCT = float(os.getenv("MEMECOIN_DESK_CAP_MIN_HEADROOM_PCT", "0.5"))
# FILL DRIFT (operator 2026-10-23, pos 74173 filled +6.7% over the sheet after a ~5 s call): the move from the price the JUDGE
# saw to the executor's own quote, net of the modelled fee and impact, may not exceed K x the coin's own 5 s true-range noise.
# K and the noise basis are COHORT-frozen in policy.standing_orders (drift_k, noise_basis); absent = no gate, old basis.
# This is only the global kill: 0 stops new verdicts from freezing a drift limit (already-frozen tickets keep theirs).
MEMECOIN_DESK_DRIFT_ENABLED = int(os.getenv("MEMECOIN_DESK_DRIFT_ENABLED", "1"))
# LIMIT ARM (operator 2026-10-23 ~20:2xZ, choice B): per ticket a frozen coin flip (sha256(ticket_id) low bit) picks LIMIT or LOG. LIMIT: an
# entry quote above the sheet as-of price (net of modelled fee+impact) is not bought; it becomes a 60 s "buy at or below the sheet price"
# standing order. Needs the cohort key standing_orders.limit_arm. 0 = no new ticket gets the key (every ticket LOG).
MEMECOIN_DESK_LIMIT_ARM_ENABLED = int(os.getenv("MEMECOIN_DESK_LIMIT_ARM_ENABLED", "1"))
# PumpSwap cap inputs (verdict_price_pumpswap): the oldest pool reserves and SOL/USD it may price from.
# Provisional until the graduated stratum's F0 freezes them (Gamma).
MEMECOIN_DESK_PS_RESERVE_MAX_AGE_S = float(os.getenv("MEMECOIN_DESK_PS_RESERVE_MAX_AGE_S", "60"))
MEMECOIN_DESK_PS_SOL_MAX_AGE_S = float(os.getenv("MEMECOIN_DESK_PS_SOL_MAX_AGE_S", "120"))
# RISK-live engine side (2026-10-22): the funded sweep applies the watcher's live TIGHTEN/EXIT rows
# (memecoin_desk_risk_apply). Ships ON (operator standing rule); 0/false/off/no = the kill.
MEMECOIN_DESK_RISK_APPLY = os.getenv("MEMECOIN_DESK_RISK_APPLY", "1").strip().lower() not in (
    "0", "false", "off", "no")
# RISK-live LOOSEN_TRAIL (operator order 2026-10-23 via Alpha, v8): the model may WIDEN the trailing stop of an open desk
# position, ONLY the trail, up to MEMECOIN_DESK_RISK_TRAIL_MAX_PCT (never above the cohort's trail rail bound), at most
# one applied loosen per position per MEMECOIN_DESK_RISK_LOOSEN_GAP_S, never after an attention auto-tighten, never
# within the gap of a RISK tighten, never when a rail would fire. Ships ON; 0/false/off/no = stop NEW loosens (an already
# applied loosen stays until a later tighten or the close: there is no auto-restore).
MEMECOIN_DESK_RISK_TRAIL_LOOSEN = os.getenv("MEMECOIN_DESK_RISK_TRAIL_LOOSEN", "1").strip().lower() not in (
    "0", "false", "off", "no")
MEMECOIN_DESK_RISK_TRAIL_MAX_PCT = float(os.getenv("MEMECOIN_DESK_RISK_TRAIL_MAX_PCT", "25"))
MEMECOIN_DESK_RISK_LOOSEN_GAP_S = float(os.getenv("MEMECOIN_DESK_RISK_LOOSEN_GAP_S", "60"))
# Desk engine attention, runner side (memecoin_desk_attention; pre-registration 2026-10-22). Thresholds are
# UNSET until Gamma's dev-day addendum: unset = dry (rows record values, nothing fires or acts).
def _desk_attention_num(name):
    try:
        v = float(os.getenv(name, ""))
    except ValueError:
        return None
    return v if math.isfinite(v) and v > 0 else None


MEMECOIN_DESK_ATTENTION_N_S = _desk_attention_num("MEMECOIN_DESK_ATTENTION_N_S")
MEMECOIN_DESK_ATTENTION_N_T_S = _desk_attention_num("MEMECOIN_DESK_ATTENTION_N_T_S")
MEMECOIN_DESK_ATTENTION_Q_PCT = _desk_attention_num("MEMECOIN_DESK_ATTENTION_Q_PCT")
MEMECOIN_DESK_ATTENTION_Q2_PCT = _desk_attention_num("MEMECOIN_DESK_ATTENTION_Q2_PCT")
MEMECOIN_DESK_ATTENTION_S_PCT = _desk_attention_num("MEMECOIN_DESK_ATTENTION_S_PCT")
MEMECOIN_DESK_ATTENTION_THRESHOLDS_VERSION = os.getenv("MEMECOIN_DESK_ATTENTION_THRESHOLDS_VERSION", "").strip()
MEMECOIN_DESK_ATTENTION_AUTO_TIGHTEN = os.getenv("MEMECOIN_DESK_ATTENTION_AUTO_TIGHTEN", "1").strip().lower() not in (
    "0", "false", "off", "no")

# Desk producer U, the Pump.fun gRPC universe worker (spec 2026-10-21-desk-universe-producer-design.md).
# Its own process and connection; "1" = run (the worker exits at once otherwise).
MEMECOIN_DESK_UNIVERSE_ENABLED = os.getenv("MEMECOIN_DESK_UNIVERSE_ENABLED", "0").strip() == "1"
MEMECOIN_DESK_UNIVERSE_GRPC_HOST = os.getenv("MEMECOIN_DESK_UNIVERSE_GRPC_HOST", "grpc.fra.shyft.to")
MEMECOIN_DESK_UNIVERSE_RING_MB = float(os.getenv("MEMECOIN_DESK_UNIVERSE_RING_MB", "64"))
MEMECOIN_DESK_UNIVERSE_COOLDOWN_S = float(os.getenv("MEMECOIN_DESK_UNIVERSE_COOLDOWN_S", "300"))
MEMECOIN_DESK_UNIVERSE_SOL_MAX_AGE_S = float(os.getenv("MEMECOIN_DESK_UNIVERSE_SOL_MAX_AGE_S", "120"))
MEMECOIN_DESK_UNIVERSE_BAND_LO_USD = float(os.getenv("MEMECOIN_DESK_UNIVERSE_BAND_LO_USD", "30000"))
MEMECOIN_DESK_UNIVERSE_BAND_HI_USD = float(os.getenv("MEMECOIN_DESK_UNIVERSE_BAND_HI_USD", "100000"))
# Validate in the producer, so malformed optional capture config cannot break funded settings imports.
MEMECOIN_DESK_PS_KEEP_WINDOW_S = os.getenv("MEMECOIN_DESK_PS_KEEP_WINDOW_S", "660")

# LOSING RE-ENTRY BLOCK (2026-08-23, operator: "do not re-enter a coin we lost on last time").
# KIRK is the case: -$15.27 at 15:54, re-entered 43 min later, -$24.24 the same way. The existing
# MEMECOIN_REAL_REENTRY_COOLDOWN_S is 0 and is the wrong shape regardless -- it expires and it
# blocks winners too. This is loss-conditional and does not expire while the last close is a loss.
# Evidence: -12.5% return on 15 such entries vs -4.8% on 294 others, bootstrap -7.6pp with
# 95% CI [-27.3, +13.3] and P(worse)=0.77. Directional, not proof; it costs 4.9% of entries.
MEMECOIN_REAL_BLOCK_LOSING_REENTRY = os.getenv(
    "MEMECOIN_REAL_BLOCK_LOSING_REENTRY", "1").strip().lower() in {"1", "true", "yes"}


def entry_roundtrip_cap_pct(impact_cap_pct, tp_pct=None):
    """The immediate round-trip cost (%) above which an entry aborts.

    THE BUDGET IS RELATIVE TO THE POSITION'S OWN TAKE-PROFIT when the caller knows it, because that
    is the comparison that makes a number a refusal: the same 24% round trip is fatal against a 25%
    TP and merely expensive against a 200% one. This is the EVM rule
    (`evm_v4_multihop.round_trip_within_budget`) applied to the Solana entry, by operator decision
    on 2026-09-08 — the two funded chains capped one concept under two rules and agreed only
    because 0.4 x 25 happens to equal the flat 10, so a TP retune moved one budget and left the
    other stale. Of the 21 live Solana round-trip refusals, 7 break even between TP 25.1% and 27.8%,
    so a 2-point retune separates the rules on a third of them.

    Fail-closed in the same three ways as the EVM rule. A non-finite or non-positive TP falls back
    to the absolute knob rather than passing everything, a fraction of 0 disables the TP-relative
    path rather than the gate, and the absolute knob still wins when no TP is supplied — so the
    LI.FI bridge callers, which have no position TP, are unchanged.
    """
    try:
        frac = float(MEMECOIN_ENTRY_ROUNDTRIP_CAP_TP_FRAC)
    except (TypeError, ValueError):
        frac = 0.0
    if math.isfinite(frac) and frac > 0 and tp_pct is not None:
        try:
            tp = float(tp_pct)
        except (TypeError, ValueError):
            tp = 0.0
        if math.isfinite(tp) and tp > 0:
            return tp * frac
    # NaN is the trap here, not None. `float("nan")` succeeds, and every `cost > nan` is False, so
    # a NaN cap disables the gate in silence. Both reads demand a finite positive number.
    if MEMECOIN_ENTRY_ROUNDTRIP_CAP_PCT is not None:
        try:
            absolute = float(MEMECOIN_ENTRY_ROUNDTRIP_CAP_PCT)
            if math.isfinite(absolute) and absolute > 0:
                return absolute
        except (TypeError, ValueError):
            pass   # a malformed knob must not disable the gate
    try:
        one_way = float(impact_cap_pct)
    except (TypeError, ValueError):
        one_way = MEMECOIN_ENTRY_IMPACT_CAP_PCT
    if not (math.isfinite(one_way) and one_way > 0):
        one_way = MEMECOIN_ENTRY_IMPACT_CAP_PCT
    return one_way * MEMECOIN_ENTRY_ROUNDTRIP_CAP_FACTOR


# exit slippage ladder (%): retry a real sell at widening slippage, then declare a stuck bag.
MEMECOIN_EXIT_SLIPPAGE_LADDER = [float(x) for x in os.getenv(
    "MEMECOIN_EXIT_SLIPPAGE_LADDER", "3,10,25").split(",") if x.strip()]
# Seconds after a desk buy's confirmed fill during which the exit PREVIEW may size the bag from
# that fill when the owner token index still answers an empty list (2026-10-23: rails blind
# 2-10 s after every desk buy). 0 = off. The sell itself always reads the custody balance.
MEMECOIN_EXIT_PREVIEW_FILL_QTY_S = float(os.getenv("MEMECOIN_EXIT_PREVIEW_FILL_QTY_S", "30"))
# Manual (operator-button) exit ladder. An operator who pressed Close wants OUT, not the best-price
# walk — on a thin pool the 3% first rung usually misses, so the automatic ladder walks 3->10->25
# with a quote+build+sign+send+confirm on each rung, and the button feels stuck for seconds
# (2026-08-14). A wider first rung lands on attempt one. Still fail-closed: it is a ladder, not a
# blank cheque, and executable_exit_allowed re-checks a fresh preview before signing.
MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL = [float(x) for x in os.getenv(
    "MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL", "15,30").split(",") if x.strip()]
# real-leg entry staleness gate (s): drop a buy older than this (the pump is over) — tighter than
# the paper follow_activity 900s. The real copy leg passes this to run_once; paper leaves it off.
MEMECOIN_REAL_ENTRY_MAX_AGE_S = float(os.getenv("MEMECOIN_REAL_ENTRY_MAX_AGE_S", "120"))
# Follow-activity polls every 300s, so ITS signals are 120-900s old by construction — the 120s live
# gate would drop 213/286 of them. It gets its own, wider staleness budget; the chase guard below is
# what makes that safe (a coin that already ran is skipped regardless of age).
MEMECOIN_REAL_FOLLOW_MAX_AGE_S = float(os.getenv("MEMECOIN_REAL_FOLLOW_MAX_AGE_S", "900"))
# Chase guard on the REAL path (there was none). Skip a buy whose price has already run this far past
# the trader's entry — we would be buying his exit liquidity, not copying him. Also the only thing
# catching a broken quote (DexScreener has returned a 493,178% gap). 0 = off.
MEMECOIN_REAL_CHASE_MAX_PCT = float(os.getenv("MEMECOIN_REAL_CHASE_MAX_PCT", "40"))
# Same-slot exposed send (the receipt head is at or before the source's own slot, so the transaction can land in S beside other buys that move the pool past the source's post-trade reserves):
# the OUTPUT haircut for those sends only, in market-movement-only mode. Case WALT e0ee3456 (2026-10-29): CHASE 30 signed a min 1.08% above the available. Rollback = set it to the chase value.
MEMECOIN_REAL_SAME_SLOT_CHASE_MAX_PCT = os.getenv("MEMECOIN_REAL_SAME_SLOT_CHASE_MAX_PCT", "40")      # RAW string on purpose: parsed only where a same-slot send uses it, so a bad value can never fail the import for every other path
MEMECOIN_REAL_FRESH_FOLLOW_MAX_LAG_S = float(os.getenv(
    "MEMECOIN_REAL_FRESH_FOLLOW_MAX_LAG_S", "60"))
MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT = float(os.getenv(
    "MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT", "40"))
# The exact route includes our price impact. Nano entries need a separate limit.
MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT = float(os.getenv(
    "MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT", "40"))
# Robinhood nanocaps use the strict cap when the display price is absent or differs by more than 10%.
MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT = float(os.getenv(
    "MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT", "10"))
MEMECOIN_REAL_4663_NANO_DISPLAY_DRIFT_MAX_PCT = float(os.getenv(
    "MEMECOIN_REAL_4663_NANO_DISPLAY_DRIFT_MAX_PCT", "10"))
# ROBINHOOD CHASE CAP ABOVE NANO (2026-09-12, operator "Do both"). A hard ceiling on how far above the
# trader's price a 4663 entry on a coin of $50k+ may quote, applied even to wallets with a
# chase_past_entry waiver (the waiver set the cap to 0 = none). Funded 4663 closes since 08-25 above
# nano by fill vs trader: 20-30% n=11 +$239.26, 30-40% n=9 -$95.50, 40-50% n=4 -$31.22,
# >=50% n=16 -$192.92. Nano keeps its own caps (>=30% there: n=17 +$57.18). 0 = off.
MEMECOIN_REAL_4663_OVER_NANO_QUOTE_CHASE_MAX_PCT = float(os.getenv(
    "MEMECOIN_REAL_4663_OVER_NANO_QUOTE_CHASE_MAX_PCT", "0"))


def _nano_chain_pct(suffix, chain, fallback):
    """A per-chain nano cap: `MEMECOIN_REAL_<chain>_NANO_<suffix>`, else the 4663 value.

    The 4663 value is the default for every other EVM chain on purpose. It is the measured one, and
    a chain with no funded history of its own has no evidence to justify a looser number.
    """
    raw = os.getenv("MEMECOIN_REAL_%s_NANO_%s" % (chain, suffix))
    if raw is not None and str(raw).strip():
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return float(fallback)


# Operator fast-lane-only copies (2026-10-06): a confirmed native chain failure is the sole
# automatic ordinary fallback. Manual buys and exits keep their authority. Default preserves
# installations without a native lane; the funded runtime opts in explicitly.
MEMECOIN_SOLANA_FAST_LANE_ONLY = os.getenv('MEMECOIN_SOLANA_FAST_LANE_ONLY', '0') == '1'
# Fresh, complete five-minute organic tape only; unknown passes. Entry reads memory only.
MEMECOIN_SOLANA_VOLUME_GATE_ENABLED = os.getenv('MEMECOIN_SOLANA_VOLUME_GATE_ENABLED', '0') == '1'
MEMECOIN_SOLANA_VOLUME_MIN_M5_MCAP_RATIO = float(
    os.getenv('MEMECOIN_SOLANA_VOLUME_MIN_M5_MCAP_RATIO', '0.02'))
MEMECOIN_SOLANA_VOLUME_MAX_AGE_S = float(os.getenv('MEMECOIN_SOLANA_VOLUME_MAX_AGE_S', '5'))
# Operator-approved 2026-10-18: enabled explicitly at the pinned policy cutover.
MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW = os.getenv('MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW', '0') == '1'
MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON = os.getenv('MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON', '{}')
# Exact J7 hints may use a confirmed wallet-paid receipt through the ordinary safety path.
MEMECOIN_J7_SOLANA_VERIFIED_BACKUP = os.getenv('MEMECOIN_J7_SOLANA_VERIFIED_BACKUP', '0') == '1'
# With fast-lane-only on, the slot lane's MECHANICAL failures that admit ONE ordinary on-chain
# attempt (operator order 2026-10-06, via Alpha): a landed failed send, a routed buy it handed off
# for having no live venue, an exotic leg that expired or was released, a build refused before
# signing. Its policy, sizing and proof refusals are never overridden. memecoin_onchain_lane
# names each class (slot_lane_verdict). Rollback: set this to landed_fail alone.
SOLANA_FALLBACK_CLASS_NAMES = frozenset({'landed_fail', 'routed_handoff', 'exotic_released',
                                         'build_refused', 'graduation_released',
                                         # the runner's own restart blackout (2026-10-07, EELDREW)
                                         'lane_down',
                                         # a multi_own_buy frame the lane rejected unsent (2026-10-14)
                                         'slot_shape_unsupported'})


def parse_solana_fallback_classes(raw):
    """Comma list -> the known class names it names. An unknown name is ignored, never trusted."""
    return frozenset(c.strip() for c in str(raw or '').split(',')) & SOLANA_FALLBACK_CLASS_NAMES


MEMECOIN_SOLANA_FALLBACK_CLASSES = parse_solana_fallback_classes(os.getenv(
    'MEMECOIN_SOLANA_FALLBACK_CLASSES', ','.join(sorted(SOLANA_FALLBACK_CLASS_NAMES))))
MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT = float(os.getenv('MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', '0'))
# Operator 2026-10-19: source-post to current-pre marginal movement only; unknown proceeds.
# Explicit cutover switch preserves rollback without changing independent slippage/impact gates.
MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY = os.getenv('MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY', '0') == '1'
if not math.isfinite(MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT) or MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT < 0:
    raise ValueError('MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT must be finite and nonnegative')
# Operator 2026-10-09: Robinhood's 30% maximum also bounds wallet overrides at signing.
MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT = float(os.getenv('MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT','30'))
if (not math.isfinite(MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT)
        or MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT < 0):
    raise ValueError('MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT must be finite and nonnegative')
# PER-WALLET COPY-PREMIUM CAP (2026-10-08, operator: "raise 397397's chase cap to 50%"). Entries are
# "source_user_id=pct" (every chain) or "chain:source_user_id=pct" (that chain; it wins), PERCENT, not a
# ratio (MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET is a ratio: 1.5). One value moves BOTH terms of the
# wallet's ceiling: the hard cap over the source's average becomes it, and the post-trade band cap is
# lifted to it (never lowered), so the ceiling is min(post x (1+W), average x (1+W)). Robinhood's
# independent maximum bounds W; an absent wallet keeps the bounded global cap. Global 0 stays off. A
# malformed, non-positive or non-finite entry is dropped loudly; an over-ceiling entry clamps.
# Read at import: a change needs a restart. Gamma grades the first 20 trades (663c9882).
MEMECOIN_REAL_MAX_COPY_PREMIUM_CEILING_PCT = 75.0
# PER-WALLET SOLANA CAP LIFT (operator 2026-10-09 via Alpha; spec
# docs/superpowers/specs/2026-10-09-memecoin-wallet-cap-lift-design.md). A "solana:<uid>"
# entry whose uid is on MEMECOIN_REAL_COPY_PREMIUM_LIFT_WALLETS may go to the LIFTED ceiling; every
# other entry keeps the 75 sanity ceiling. At 150 the slot lane's haircut floor is <= 0, so the signed
# min-out is the clip at 2.5 x the source's average fill (pump_curve_buy.capped_min_out).
MEMECOIN_REAL_MAX_COPY_PREMIUM_LIFTED_CEILING_PCT = 150.0
MEMECOIN_REAL_COPY_PREMIUM_LIFT_WALLETS = frozenset(
    w.strip().lower() for w in os.getenv("MEMECOIN_REAL_COPY_PREMIUM_LIFT_WALLETS", "").split(",")
    if w.strip())


def parse_copy_premium_by_wallet(raw, *, ceiling=MEMECOIN_REAL_MAX_COPY_PREMIUM_CEILING_PCT,
                                 lifted=None, lifted_ceiling=MEMECOIN_REAL_MAX_COPY_PREMIUM_LIFTED_CEILING_PCT,
                                 log=None):
    """{"uid" | "chain:uid": pct} from the knob; every refused or clamped entry is named via `log`.
    An entry over its ceiling CLAMPS to it (Alpha 2026-10-09: a wrong edit order must never cut a
    wallet's cap to the global); a malformed, non-finite or non-positive one is dropped."""
    lifted = MEMECOIN_REAL_COPY_PREMIUM_LIFT_WALLETS if lifted is None else lifted
    out = {}
    for part in str(raw or "").split(","):
        if not part.strip():
            continue
        key, sep, value = part.partition("=")
        key = key.strip().lower()
        try:
            pct = float(value) if sep and key else float("nan")
        except ValueError:
            pct = float("nan")
        if not (math.isfinite(pct) and pct > 0):
            if log is not None:
                log("MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET entry %r refused (needs key=pct, "
                    "pct > 0); that wallet keeps the global cap" % part.strip())
            continue
        chain, _sep, uid = key.rpartition(":")
        top = lifted_ceiling if chain == "solana" and uid in lifted else ceiling
        if pct > top:
            if log is not None:
                log("MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET entry %r clamped to %.0f (its ceiling: "
                    "%s)" % (part.strip(), top, "the listed Solana lift" if top == lifted_ceiling
                             else "not a solana entry of a MEMECOIN_REAL_COPY_PREMIUM_LIFT_WALLETS uid"))
            pct = top
        out[key] = pct
    return out


MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET = parse_copy_premium_by_wallet(
    os.getenv("MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET", ""),
    log=lambda text: logging.getLogger(__name__).error(text))


def copy_premium_cap(source_user_id, chain):
    """(pct, source) of the hard copy-premium cap for one wallet on one chain: the wallet's
    MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET entry ("wallet"), else the global ("global"); a
    global 0 is ("global_off") whatever the wallet says. Robinhood's independent positive
    maximum bounds both; a bound global reports "chain". Zero disables that chain bound."""
    hard = MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT
    if hard <= 0:
        return hard, "global_off"
    table = MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET
    uid = str(source_user_id or "").strip().lower()
    network, source = str(chain or "").strip().lower(), "global"
    if uid and table:
        for key in ("%s:%s" % (network, uid), uid):
            if key in table:
                hard, source = table[key], "wallet"
                break
    if (network == "4663" and MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT > 0
            and hard > MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT):
        hard = MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT
        if source == "global":
            source = "chain"
    return hard, source


def copy_cap_override_fields(source_user_id, chain, *, lane, price, average, post=None,
                             band_pct=None, band_used_pct=None):
    """The trace substage `copy_cap_override` for a wallet WITH an override entry, else None (other
    wallets' traces stay byte for byte). Gamma's stop rule 663c9882 grades the entries admitted only
    because of the override: `override_applied` is True when `price` is over the ceiling the global
    caps give and within the wallet's. `band_pct` is the post-trade band cap BEFORE the override (None
    when this lane has no band term); a missing price or reference leaves the verdict None.
    `band_used_pct` is the band the caller ENFORCES; without it the lift is recomputed from `band_pct`,
    which misreads a band clamped after the lift (4663 above nano: 50 recorded, 30 enforced; Gamma
    LOW on 6c0e57ae0)."""
    pct, source = copy_premium_cap(source_user_id, chain)
    if source != "wallet":
        return None

    def ceiling(hard, band):
        terms = []
        if average:
            terms.append(float(average) * (1 + hard / 100.0))
        if band is not None and band > 0 and post:
            terms.append(float(post) * (1 + band / 100.0))
        return min(terms) if terms else None

    hard0, _source = copy_premium_cap(None, chain)
    band1 = (band_used_pct if band_used_pct is not None
             else None if band_pct is None else wallet_band_cap_pct(source_user_id, chain, band_pct))
    if MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
        return {"lane": lane, "chain": str(chain), "override_applied": None,
                "premium_pct": None, "reference_basis": "intervening_market",
                "cap_without_override": hard0, "cap_used": pct,
                "band_without_override": band_pct, "band_used": band1}
    without, with_ = ceiling(hard0, band_pct), ceiling(pct, band1)
    try:
        premium = (float(price) / float(average) - 1.0) * 100.0 if price and average else None
    except (TypeError, ValueError, ZeroDivisionError):
        premium = None
    applied = (None if not price or without is None or with_ is None
               else bool(without < float(price) <= with_))
    return {"lane": lane, "chain": str(chain), "override_applied": applied,
            "premium_pct": premium, "reference_basis": "source_average",
            "post_premium_pct": ((float(price) / float(post) - 1.0) * 100.0) if price and post else None,
            "cap_without_override": hard0, "cap_used": pct,
            "band_without_override": band_pct, "band_used": band1}


def wallet_band_cap_pct(source_user_id, chain, base_pct):
    """The post-trade band cap with the wallet's copy-premium override applied: max(base, W) when the
    wallet has an entry and the band is on (base > 0), else `base_pct` unchanged."""
    pct, source = copy_premium_cap(source_user_id, chain)
    try:
        base = float(base_pct)
    except (TypeError, ValueError):
        return base_pct
    return max(base, pct) if source == "wallet" and base > 0 else base_pct
# N1 residual 1, direct routed path (Alpha, 2026-10-07): a routed buy's whole average (the owner's
# USDC fall / coins gained) may exceed its own venue event's average by at most this ratio. Above
# it the USDC fall carries more than the route (an unrelated outflow in the same slot), so the
# average is unknown and the hard cap refuses. Routed fees and the USDC->SOL hop stay under it:
# measured on 124 routed buys 09-26..10-07 (pump_amm, CPMM, LaunchLab), whole-leg p99 1.021 (max
# 1.023), split routes max 1.057, with up to ~2% SOL/USD reference error. 1.06 refuses none of them.
MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO = float(os.getenv('MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO', '1.06'))
if not math.isfinite(MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO) or MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO < 1:
    raise ValueError('MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO must be finite and at least 1')


def memecoin_real_nano_quote_chase_max_pct(chain):
    return _nano_chain_pct("QUOTE_CHASE_MAX_PCT", str(chain),
                           MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT)


def memecoin_real_nano_display_drift_max_pct(chain):
    return _nano_chain_pct("DISPLAY_DRIFT_MAX_PCT", str(chain),
                           MEMECOIN_REAL_4663_NANO_DISPLAY_DRIFT_MAX_PCT)
# Execution-premium cap (2026-08-23): the exact route quote over the SAME-INSTANT chart mid, as a
# ratio. The two caps above reference the TRADER'S FILL, which mixes the coin's move with our own
# slippage; on a nano coin the second term dominates and they fire on coins that never ran. This
# one measures only what our route costs, so it needs no per-band constant. Realized return by
# quote/display over 221 filled closes: <1.15x +1.07%, 1.15-1.5x -2.35%, 1.5-2.0x -5.38%,
# >=2.0x -52.14% (n=18, 22% wins). See execution_premium_decision. 0 = off (fail-open).
MEMECOIN_REAL_MAX_EXEC_PREMIUM = float(os.getenv("MEMECOIN_REAL_MAX_EXEC_PREMIUM", "0"))
# E11 on-chain (MEMECOIN_DEXSCREENER_OFF=E11:<chain>): the premium cap on the source's POST-TRADE
# price instead of the display price. 1.2 = the live 1.5 / the measured median post-trade/display
# 1.25 (spec 2026-10-08-memecoin-evm-entry-gates-onchain-design.md, decision (b)); the 2026-10-15
# read refits it on filled closes. 0 = no post-trade gate (the missing-display posture applies).
MEMECOIN_REAL_MAX_EXEC_PREMIUM_POST_TRADE = float(
    os.getenv("MEMECOIN_REAL_MAX_EXEC_PREMIUM_POST_TRADE", "1.2"))
# the ONE exit policy that goes live (the A/B winner is a paper research construct; real runs one).
# alpha_b = mirror-sell + wide catastrophe stop + hard 12h timecap (feed-independent exits).
MEMECOIN_REAL_POLICY = os.getenv("MEMECOIN_REAL_POLICY", "alpha_b")
# SLOT LANE (2026-09-26, operator: fill at the source slot S or S+1). A processed-logs pump.fun
# lane that builds its own guarded transaction; see
# docs/superpowers/specs/2026-09-26-solana-slot-lane-design.md. 0 = off (default).
MEMECOIN_SOLANA_SLOT_LANE_ENABLED = os.getenv("MEMECOIN_SOLANA_SLOT_LANE_ENABLED", "0")
# "live" sends funded transactions; anything else (default "shadow") builds and times only.
MEMECOIN_SOLANA_SLOT_LANE_MODE = os.getenv("MEMECOIN_SOLANA_SLOT_LANE_MODE", "shadow")
MEMECOIN_SOL_EXEC_BUFFER_CAP_USD = float(os.getenv("MEMECOIN_SOL_EXEC_BUFFER_CAP_USD", "0"))
# The cap is spendable buy capital; fees/rent are additional. Refill above the cap by this
# small cushion, then wait until spendable capital falls below cap before refilling again.
MEMECOIN_SOL_EXEC_BUFFER_HEADROOM_USD = float(os.getenv("MEMECOIN_SOL_EXEC_BUFFER_HEADROOM_USD", "5"))
MEMECOIN_SOL_EXEC_BUFFER_TOPUP_USD = float(os.getenv("MEMECOIN_SOL_EXEC_BUFFER_TOPUP_USD", "0"))
# 0 removes the daily refill budget; per-refill size, buffer target and USDC reserve still apply.
MEMECOIN_SOL_EXEC_BUFFER_DAILY_CAP_USD = float(os.getenv("MEMECOIN_SOL_EXEC_BUFFER_DAILY_CAP_USD", "0"))
MEMECOIN_SOL_EXEC_BUFFER_MIN_USDC_USD = float(os.getenv("MEMECOIN_SOL_EXEC_BUFFER_MIN_USDC_USD", "0"))
MEMECOIN_SOL_EXEC_BUFFER_PERIOD_S = float(os.getenv("MEMECOIN_SOL_EXEC_BUFFER_PERIOD_S", "300"))
# BufferWatch (2026-10-21): page once when native SOL stays under one clip with no refill for this long,
# and once on recovery. 0 (default) = the slot lane's current clip, the amount prefer_sol's sol_funds()
# needs; > 0 overrides it with a fixed USD amount.
MEMECOIN_SOL_BUFFER_STALL_BELOW_USD = float(os.getenv("MEMECOIN_SOL_BUFFER_STALL_BELOW_USD", "0"))
MEMECOIN_SOL_BUFFER_STALL_PAGE_S = float(os.getenv("MEMECOIN_SOL_BUFFER_STALL_PAGE_S", "1800"))
# Size the lane's prefetched leg for the market's regime scale (2026-09-27, Alpha), so a regime-
# scaled buy builds its scaled clip instead of falling back at S+7 (Dougherty 65991). 0 = the
# rollback: a full-clip leg, and every scaled buy refuses slot_lane_scaled_clip as before.
MEMECOIN_SLOT_LANE_SCALED_LEG = os.getenv(
    "MEMECOIN_SLOT_LANE_SCALED_LEG", "1").strip().lower() in {"1", "true", "yes", "on"}
# Fund a coin classified at another scale than the prefetched leg in native SOL instead of refusing
# it slot_lane_scaled_clip (spec 2026-10-02 scaled-clip-sol, operator yes). Pump curve and SOL-quoted
# pump AMM only. A matching warm cash leg also covers the scaled clip without an on-demand quote.
# 0 = today's refusal before any reservation.
MEMECOIN_SLOT_LANE_SCALED_SOL = os.getenv(
    "MEMECOIN_SLOT_LANE_SCALED_SOL", "0").strip().lower() in {"1", "true", "yes", "on"}
# Learn each PumpSwap pool when pump.fun migrates into it, from one logsSubscribe on its migration
# account over the keyless read socket (2026-09-27, Alpha), so the lane's first buy on a pool no
# longer refuses pool_unknown and falls back at S+7 (lemon 65992, Dougherty 65991). 0 = no
# listener: pools are learned on a miss, as before.
MEMECOIN_SLOT_LANE_MIGRATION_PREWARM = os.getenv(
    "MEMECOIN_SLOT_LANE_MIGRATION_PREWARM", "1").strip().lower() in {"1", "true", "yes", "on"}
# A SECOND migration socket on the research Helius key (2026-10-07, operator order via Alpha): the
# keyless public socket stalls 5-14 times an hour. Never the funded key or the signal socket's key
# (memecoin_slot_signal.research_migration_ws refuses both). ~2,800 frames/day, ~31 MB, ~620 credits
# at Helius's 20 credits/MB. "0" keeps the public socket alone.
MEMECOIN_SLOT_LANE_MIGRATION_RESEARCH_WS = os.getenv(
    "MEMECOIN_SLOT_LANE_MIGRATION_RESEARCH_WS", "1").strip().lower() in {"1", "true", "yes", "on"}
# ROUTED BUYS on the slot lane's socket (operator 2026-09-27: "widen the slot lane to read routed
# buys"): programSubscribe on the owners' Token and Token-2022 accounts pairs a USDC-paid routed buy
# with its logs frame. "shadow" (default) logs each pair and writes nothing; "live" hands it to the
# on-chain lane as a detection row and wakes that lane; "off" subscribes to nothing. Anything else
# is "off". Going live needs Alpha's read of shadow and the operator's yes.
MEMECOIN_SLOT_LANE_ROUTED = os.getenv("MEMECOIN_SLOT_LANE_ROUTED", "shadow").strip().lower()
# ROUTED PUMP BUYS ON THE LANE (2026-09-27, Alpha GO). A routed buy whose own logs frame carries
# exactly one PumpSwap/curve buy event into its coin (router proVF4 names its own account there, so
# the lane's owner decoder misses it) is built by the lane's existing pump builders. When the lane is
# live and takes it, the routed hand-off does not write, so one source buy has one funded path.
# Default on; 0 = the routed path exactly as before. Read once when the lane starts.
MEMECOIN_SLOT_LANE_ROUTED_PUMP = os.getenv(
    "MEMECOIN_SLOT_LANE_ROUTED_PUMP", "1").strip().lower() in {"1", "true", "yes"}
# ROUTED BUYS ON PROCESSED PROOF (operator 2026-10-14: "whatever would get us the fastest execution").
# The Shyft processed transaction carries the signer, the owner's USDC payment and the acquired coin,
# the same facts the confirmed receipt proves; on, the lane signs a routed buy at processed instead of
# waiting ~0.3 s for confirmation (10 of the last 20 non-fast Solana fills were routed). The residual
# risk is a processed source transaction that never confirms (a dropped fork), the same risk every
# native processed buy already carries. Default off; Shyft profile only. Read at import.
MEMECOIN_SLOT_LANE_ROUTED_PROCESSED_PROOF = os.getenv(
    "MEMECOIN_SLOT_LANE_ROUTED_PROCESSED_PROOF", "0").strip().lower() in {"1", "true", "yes"}
# RABBITSTREAM AS A SLOT-LANE SIGNAL (operator 2026-10-15: "anything that we think can be better or
# faster live"). On the EXISTING RabbitStream prepare stream (no new connection): DFlow routes decode
# from the pinned on-chain IDL (sniper/execution/dflow_swap.py), and every owner-signed pre-execution
# frame is stamped beside the processed feeds on one monotonic clock (FeedArrivals, the receipt
# journal as decode_state rabbit_pre_execution). Default off: 0 is today's prepare path exactly.
# Read at import.
MEMECOIN_SLOT_LANE_RABBIT_SIGNAL = os.getenv(
    "MEMECOIN_SLOT_LANE_RABBIT_SIGNAL", "0").strip().lower() in {"1", "true", "yes"}
# RABBIT-LED FUNDED BUY (operator 2026-10-15 "yes", stage 2; its OWN knob, Alpha 2026-10-15, so the
# stage-1 line above cannot turn funded buys on). With RABBIT_SIGNAL also on, a tracked owner's
# pump-curve buy seen on that stream fires a funded copy BEFORE it executes, on a pre-execution
# owner-signed proof (memecoin_rabbit_signal), with the source_not_landed exit behind it. Without
# RABBIT_SIGNAL it does nothing. Default off: 0 never sends from a rabbit frame. HOT: a cockpit
# overlay (memecoin_entry_knobs) lands on this attribute and every signal reads it.
MEMECOIN_SLOT_LANE_RABBIT_BUY = os.getenv(
    "MEMECOIN_SLOT_LANE_RABBIT_BUY", "0").strip().lower() in {"1", "true", "yes"}
# DFLOW -> PUMP_AMM PRE-EXECUTION FIRE (Alpha GO 2026-10-17, scope extended under e7c39fd9 and the
# operator's 2026-10-17 "turn preconf on"; memecoin_rabbit_dflow_amm). With RABBIT_SIGNAL and
# RABBIT_BUY on, an owner's USDC-paid DFlow route whose one coin leg is a canonical PumpSwap buy fires
# the same pre-execution owner-signed buy as the curve path, priced from one read of the pool taken
# BEFORE the source's slot. Default ON (operator 2026-10-15: improvements go live with a kill knob);
# 0/false/off/no = off (the curve path only, exactly as before). Read per frame.
MEMECOIN_SLOT_LANE_RABBIT_DFLOW_AMM = os.getenv(
    "MEMECOIN_SLOT_LANE_RABBIT_DFLOW_AMM", "1").strip().lower() not in ("0", "false", "off", "no")
# The OKX proVF4 swap_tob -> PumpSwap handler (memecoin_rabbit_okx_amm, Alpha GO 2026-10-17): ON at deploy;
# 0/false/off/no switches only that route off.
MEMECOIN_SLOT_LANE_RABBIT_OKX_AMM = os.getenv(
    "MEMECOIN_SLOT_LANE_RABBIT_OKX_AMM", "1").strip().lower() not in ("0", "false", "off", "no")
# The Jupiter route_v2 -> PumpSwap handler (memecoin_rabbit_jupiter_amm, Alpha GO 2026-10-17): ON at deploy.
MEMECOIN_SLOT_LANE_RABBIT_JUPITER_AMM = os.getenv(
    "MEMECOIN_SLOT_LANE_RABBIT_JUPITER_AMM", "1").strip().lower() not in ("0", "false", "off", "no")
# The rabbit/preconf frame line's read fields (Beta 2026-10-17, task 5a7e1967): read_ms, read_minus_frame,
# state_source, read_basis and held on the one WARNING line per frame (the curve read's old logger.info
# line folded in). 0/false/off/no: the line exactly as before. Read off the loop, at each line.
MEMECOIN_SLOT_LANE_RABBIT_FRAME_READ_FIELDS = os.getenv(
    "MEMECOIN_SLOT_LANE_RABBIT_FRAME_READ_FIELDS", "1").strip().lower() not in ("0", "false", "off", "no")
# The rabbit pool tape (memecoin_rabbit_tape, Beta, Alpha GO 2026-10-17): one processed Yellowstone stream
# on the funded Shyft token holds curve/pool/vault/mint/FeeConfig state, and a pre-execution fire prices
# from it with no RPC read. ON at deploy (the standing speed order); 0/false/off/no: every read goes to RPC
# exactly as before and the stream is not started. Read per signal on the fill path, at start for the stream.
MEMECOIN_SLOT_LANE_RABBIT_TAPE = os.getenv(
    "MEMECOIN_SLOT_LANE_RABBIT_TAPE", "1").strip().lower() not in ("0", "false", "off", "no")
# The add-on REFUSAL (memecoin_addon_guard; operator 2026-10-17 'Fix it, it happened again' / 'At
# least for him'): a scoped wallet's buy smaller than his prior net into the coin inside the window,
# while we hold nothing there, refuses addon_after_missed_entry. REFUSE=0: off. WALLET_IDS: comma list of
# source_user_ids, '*' = every wallet; absent = no wallet (off). MAX_SHARE_PCT: refuse when the buy is
# under this % of his prior net (100 = any smaller add-on).
MEMECOIN_REAL_ADDON_REFUSE = os.getenv("MEMECOIN_REAL_ADDON_REFUSE", "1").strip().lower() not in ("0", "false", "off", "no")
MEMECOIN_REAL_ADDON_WINDOW_S = float(os.getenv("MEMECOIN_REAL_ADDON_WINDOW_S", "600") or 0)
MEMECOIN_REAL_ADDON_MAX_SHARE_PCT = float(os.getenv("MEMECOIN_REAL_ADDON_MAX_SHARE_PCT", "100") or 0)
MEMECOIN_REAL_ADDON_REFUSE_WALLET_IDS = frozenset(
    w.strip() for w in os.getenv("MEMECOIN_REAL_ADDON_REFUSE_WALLET_IDS", "").split(",") if w.strip())
# The add-on label (memecoin_addon_label, Alpha 2026-10-17): every funded attempt records, at finish,
# whether the copied trader had bought the coin in the 10 minutes before (first buy price/size/time,
# prior net, share, our price over his first buy, our attempt on that first buy). A label, never a gate;
# 0/false/off/no: nothing read or written.
MEMECOIN_ADDON_LABEL = os.getenv("MEMECOIN_ADDON_LABEL", "1").strip().lower() not in ("0", "false", "off", "no")
# E1 exotic round trip (Alpha 2026-10-17, after Gigabite 73927): an exotic-quote PumpSwap slot-lane buy
# prices its immediate round trip from the warm leg plus an exact reverse minimum fetched off-path
# using a retained admitted pool event. Surplus return is ignored; insufficient return or missing/freshness
# evidence refuses exotic_round_trip_unmeasured. The frozen position's entry_roundtrip_cap_pct applies.
# First-use seeds have no event and refuse until the worker notes a buy and the warmer refreshes it.
# 0/false/off/no restores the prior path without a reverse fetch or this gate.
MEMECOIN_SLOT_LANE_EXOTIC_ROUND_TRIP = os.getenv(
    "MEMECOIN_SLOT_LANE_EXOTIC_ROUND_TRIP", "1").strip().lower() not in ("0", "false", "off", "no")
# SHARED by every rabbit handler (DFlow AMM, curve). The oldest pool read a pre-execution fire may price from, in slots before the frame's slot (Gamma LOW-1,
# 2026-10-17): a lagging node's read at S-k misses the net sells of S-k..S-1, which would put the modelled
# post price ABOVE the real one, a looser cap. Beyond it, or with no context slot: read_stale, refused.
try:
    MEMECOIN_SLOT_LANE_RABBIT_READ_MAX_LAG_SLOTS = int(os.getenv("MEMECOIN_SLOT_LANE_RABBIT_READ_MAX_LAG_SLOTS", "2"))
except ValueError:
    MEMECOIN_SLOT_LANE_RABBIT_READ_MAX_LAG_SLOTS = 2
if MEMECOIN_SLOT_LANE_RABBIT_READ_MAX_LAG_SLOTS < 0:
    MEMECOIN_SLOT_LANE_RABBIT_READ_MAX_LAG_SLOTS = 2
# The rabbit-led fire's breaker (operator 2026-10-15, memecoin_rabbit_signal.RabbitBackstop):
# N source_failed + source_not_landed verdicts within WINDOW_MIN minutes pause rabbit fires for
# PAUSE_MIN minutes, paged once. In memory; N=0 = no breaker. Read at each verdict.
MEMECOIN_SLOT_LANE_RABBIT_BREAKER_N = int(os.getenv("MEMECOIN_SLOT_LANE_RABBIT_BREAKER_N", "3"))
MEMECOIN_SLOT_LANE_RABBIT_BREAKER_WINDOW_MIN = float(os.getenv("MEMECOIN_SLOT_LANE_RABBIT_BREAKER_WINDOW_MIN", "15"))
MEMECOIN_SLOT_LANE_RABBIT_BREAKER_PAUSE_MIN = float(os.getenv("MEMECOIN_SLOT_LANE_RABBIT_BREAKER_PAUSE_MIN", "60"))
# HELIUS PRECONFIRMATIONS AS THE THIRD SLOT-LANE FEED (operator 2026-10-17 "turn preconf on", ON:
# memecoin_preconf_signal). A tracked owner's frame from wss://beta.helius-rpc.com (preconfSubscribe,
# accountInclude = the slot-lane owners, includeSource helius) goes through the rabbit owner-frame
# path: its own ed25519 owner-signature check, one shared buyer, the pre-execution proof and the
# source_not_landed exit. Read at start for the feed; HOT for its fires (a cockpit overlay lands
# here and every signal reads it). 0 = no preconf socket at all.
MEMECOIN_SLOT_LANE_PRECONF = os.getenv(
    "MEMECOIN_SLOT_LANE_PRECONF", "1").strip().lower() in {"1", "true", "yes"}
# Hard daily credit cap of the preconf subscription (Helius bills max(300, 10 x messages) per slot
# that delivers a message). At the cap the socket closes until 00:00Z, paged once. 0 = no cap.
MEMECOIN_SLOT_LANE_PRECONF_DAILY_CREDITS = int(os.getenv("MEMECOIN_SLOT_LANE_PRECONF_DAILY_CREDITS", "50000"))
# PRECONF SOURCES (Alpha GO 2026-10-17): one preconfSubscribe socket per includeSource value, because a
# frame carries no source field. Known values helius, bam, in order; unknown ones are dropped (a
# warning); none left = helius only, the subscription before this knob.
def _preconf_sources(raw):
    out = []
    for part in str(raw or "").split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in ("helius", "bam"):
            logging.getLogger(__name__).warning(
                "MEMECOIN_SLOT_LANE_PRECONF_SOURCES entry %r ignored (want helius, bam)", name)
        elif name not in out:
            out.append(name)
    return tuple(out) or ("helius",)


MEMECOIN_SLOT_LANE_PRECONF_SOURCES = _preconf_sources(os.getenv("MEMECOIN_SLOT_LANE_PRECONF_SOURCES", "helius,bam"))
# The preconf fire's own breaker; unset = rabbit's values (memecoin_rabbit_signal.RabbitBackstop).
MEMECOIN_SLOT_LANE_PRECONF_BREAKER_N = (int(os.environ["MEMECOIN_SLOT_LANE_PRECONF_BREAKER_N"])
                                        if os.getenv("MEMECOIN_SLOT_LANE_PRECONF_BREAKER_N") else None)
MEMECOIN_SLOT_LANE_PRECONF_BREAKER_WINDOW_MIN = (
    float(os.environ["MEMECOIN_SLOT_LANE_PRECONF_BREAKER_WINDOW_MIN"])
    if os.getenv("MEMECOIN_SLOT_LANE_PRECONF_BREAKER_WINDOW_MIN") else None)
MEMECOIN_SLOT_LANE_PRECONF_BREAKER_PAUSE_MIN = (
    float(os.environ["MEMECOIN_SLOT_LANE_PRECONF_BREAKER_PAUSE_MIN"])
    if os.getenv("MEMECOIN_SLOT_LANE_PRECONF_BREAKER_PAUSE_MIN") else None)
# CROWDED PRIORITY FEE (operator YES 2026-10-15 via Alpha; memecoin_crowded_fee): a slot-lane pump
# buy whose source frame arrived while our feed head was still before the source's slot (rule
# "early": the race inside slot S is still winnable) bids CROWDED_PRIORITY_LAMPORTS of priority
# fee instead of the bid cache's (1.52M lamports fallback on 40 of 44 recorded bids), with a 25%
# hash holdout as the control. Priority + tip never exceed MAX_SEND; the day's treated sends never
# exceed a positive DAILY (counted when sent, failures included), paged once, then normal bids until
# UTC midnight. DAILY=0 (default) is unlimited; priority=0 is off. Only the compute-unit price changes.
# The priority knob is HOT through the cockpit overlay (memecoin_entry_knobs); the others are read per send too.
def _lamports(name, default, maximum):
    raw = os.getenv(name, str(default)).strip()
    value = int(raw)
    if not 0 <= value <= maximum:
        raise ValueError("%s=%s outside 0..%d" % (name, raw, maximum))
    return value
MEMECOIN_SLOT_LANE_CROWDED_RULE = os.getenv("MEMECOIN_SLOT_LANE_CROWDED_RULE", "early").strip().lower()
if MEMECOIN_SLOT_LANE_CROWDED_RULE not in ("early", "off"):
    raise ValueError("MEMECOIN_SLOT_LANE_CROWDED_RULE must be early or off")
MEMECOIN_SLOT_LANE_CROWDED_PRIORITY_LAMPORTS = _lamports("MEMECOIN_SLOT_LANE_CROWDED_PRIORITY_LAMPORTS", 0, 20_000_000)
MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS = _lamports("MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS", 12_000_000, 20_000_000)
MEMECOIN_SLOT_LANE_CROWDED_DAILY_LAMPORTS = _lamports("MEMECOIN_SLOT_LANE_CROWDED_DAILY_LAMPORTS", 0, 2_000_000_000)
# The venues the crowded rule may treat (Alpha GO 2026-10-16; MIDWAY 7de9dad9, a routed DAMM v2 buy that
# landed S+1 at index 52 on the 1.52M static bid: venue_not_in_scope). Default every slot-lane venue;
# "pump_curve,pump_amm" is the scope before this; empty = none. An exotic-quote build of a listed venue
# is in. A name outside the six is an import error. Read per send.
_CROWDED_VENUES = ("pump_curve", "pump_amm", "meteora_damm_v2", "raydium_cpmm", "raydium_launchlab", "meteora_dbc")
_raw_crowded_venues = os.getenv("MEMECOIN_SLOT_LANE_CROWDED_VENUES")
MEMECOIN_SLOT_LANE_CROWDED_VENUES = (_CROWDED_VENUES if _raw_crowded_venues is None else tuple(
    v.strip().lower() for v in _raw_crowded_venues.split(",") if v.strip()))
if set(MEMECOIN_SLOT_LANE_CROWDED_VENUES) - set(_CROWDED_VENUES):
    raise ValueError("MEMECOIN_SLOT_LANE_CROWDED_VENUES names %s; allowed: %s" % (
        ",".join(sorted(set(MEMECOIN_SLOT_LANE_CROWDED_VENUES) - set(_CROWDED_VENUES))), ",".join(_CROWDED_VENUES)))
# LEARN A MISSED POOL NOW (2026-09-27, Alpha GO). A PumpSwap buy the lane refuses `pool_unknown`
# is handed to a learner thread that reads that pool at once and gives the lane the same buy ONCE
# more, inside a 250 ms budget from the miss; past it, or when the read fails, the buy is handed off
# as before. 5 of the 7 PumpSwap fills that missed the lane in its first 25.7 h were pool misses.
# Default on; 0 = the old miss-then-hand-off. Read once when the lane starts.
MEMECOIN_SLOT_LANE_MISS_LEARN = os.getenv(
    "MEMECOIN_SLOT_LANE_MISS_LEARN", "1").strip().lower() in {"1", "true", "yes"}
# ROUTED PUMP POOL MISS -> LEARN, B1 (spec 2026-10-01-memecoin-slot-lane-routed-venues-design.md,
# Alpha GO). A ROUTED pump buy whose PumpSwap pool the cache does not know was handed to the
# on-chain lane at once (pool_unknown, 3 of 10 routed pump refusals 09-27..10-01). 1 = the same
# learner reads that pool and, inside its 250 ms budget, converts the routed buy AGAIN with every
# check (coin's pool, SOL quote, migrated); a buy that then qualifies goes to the lane, anything
# else writes its routed row as before. COST: a miss that expires or cannot be read writes that row up
# to the 250 ms budget later than today's immediate hand-off; the learner line logs learn_ms with
# routed=1 for each one. 0 (default) = today's immediate hand-off. Lane restart.
MEMECOIN_SLOT_LANE_ROUTED_POOL_LEARN = os.getenv(
    "MEMECOIN_SLOT_LANE_ROUTED_POOL_LEARN", "0").strip().lower() in {"1", "true", "yes", "on"}
# RAYDIUM CPMM ON THE SLOT LANE (2026-09-27, lever 1; docs/superpowers/specs/2026-09-27-slot-lane-any-
# venue-design.md section 6). A routed buy whose own logs frame carries the CPMM swap for its coin,
# in a pool quoted in SOL or USDC, is built natively on the lane. "shadow" (default, Alpha 22:2xZ:
# read-only) builds and times it and never reserves or sends; "off" decodes nothing. There is no
# "live" (2026-09-28, operator "go live with everything"): SOL- and USDC-quoted pools reserve, sign
# and send on the lane (one source buy, at most one funded attempt: a live buy the lane refuses before
# reserving writes its routed row for the on-chain lane). Exotic quotes stay shadow. The first live
# sends are capped by MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR.
MEMECOIN_SLOT_LANE_CPMM = os.getenv("MEMECOIN_SLOT_LANE_CPMM", "shadow").strip().lower()
# RAYDIUM LAUNCHLAB ON THE SLOT LANE (2026-09-27, Alpha GO, phase (a)). A routed buy whose own logs
# frame carries LaunchLab's TradeEvent for its coin, in a pool quoted in SOL, USDC or (with the ZEC
# leg on) ZEC, is built natively and SIMULATED once off the lane thread (never sent). Same modes
# as MEMECOIN_SLOT_LANE_CPMM: "shadow" (default), "off", or "live" for SOL- and USDC-quoted pools.
MEMECOIN_SLOT_LANE_LAUNCHLAB = os.getenv("MEMECOIN_SLOT_LANE_LAUNCHLAB", "shadow").strip().lower()
# METEORA DAMM V2 ON THE SLOT LANE, B2 (spec 2026-10-01-memecoin-slot-lane-routed-venues-design.md, Alpha
# GO). A routed buy whose frame ran cp-amm, on a SOL-quoted pool the lane has learned for that coin
# (from an earlier sighting's confirmed transaction), is built natively with the lane's USDC -> SOL leg;
# its minimum out is the price floor at the source's own average x (1 + chase cap). "off" (default),
# "shadow" (build and simulate once, never send) or "live" (capped by
# MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR, live only after shadow sims, Gamma CLEAN and a canary).
MEMECOIN_SLOT_LANE_DAMM_V2 = os.getenv("MEMECOIN_SLOT_LANE_DAMM_V2", "off").strip().lower()
# DAMM V2 FIRST SIGHT (Alpha GO 2026-10-07): a routed cp-amm frame names no pool, so a coin the cache
# never learned was handed off (S+3/S+4). With this on, the MissLearner finds the pool BY MINT inside
# its budget (two memcmp getProgramAccounts in parallel + one getMultipleAccounts on its own
# ONCHAIN_WATCH_HELIUS_RPC_URL client; measured n=36 p50 136 / p90 179 / max 221 ms) and the lane
# retries the buy once. Only SOL pools (USDC when non-SOL DAMM is listed) at or above the depth
# floor; anything else is handed off as before. Default off.
MEMECOIN_SLOT_LANE_DAMM_V2_MISS_LEARN = os.getenv(
    "MEMECOIN_SLOT_LANE_DAMM_V2_MISS_LEARN", "0").strip().lower() in {"1", "true", "yes", "on"}
# Measured 2026-10-07 on 22 first-sight coins: every source pool the pick matched was >= $2.7k deep;
# every SOL pool beside a MET/JUP-quoted source pool was <= $406.
MEMECOIN_SLOT_LANE_DAMM_V2_MISS_MIN_DEPTH_USD = float(os.getenv(
    "MEMECOIN_SLOT_LANE_DAMM_V2_MISS_MIN_DEPTH_USD", "2500"))
# METEORA DBC ON THE SLOT LANE (operator order 2026-10-07 via Alpha: live, no shadow phase). A routed
# buy whose frame ran DBC is learned by mint on the MissLearner's DBC thread (getProgramAccounts on the
# base mint + one getMultipleAccounts; n=13 p50 112 / p90 153 ms) and built as swap2 ExactIn at the
# source-average price floor. Hooked coins, migrated/complete/completing curves and rate-limiter pools
# are refused. "off" (default) | "live"; anything else is off. USDC and exotic quotes also need
# "meteora_dbc" on MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES. Spec 2026-10-07-slot-lane-meteora-dbc-venue-parked.md.
MEMECOIN_SLOT_LANE_DBC = os.getenv("MEMECOIN_SLOT_LANE_DBC", "off").strip().lower()
# THE FIRST LIVE SENDS OF A NEW SLOT-LANE VENUE ARE CAPPED (2026-09-28, Alpha): at most this many lane
# sends an hour PER VENUE (CPMM, LaunchLab, DAMM v2 each count their own; per venue since 2026-10-01,
# Alpha, so one venue's canary cap cannot starve another), counted in-process over a sliding hour. A
# buy over its venue's cap is refused before any reservation and reaches the on-chain lane through
# its routed row. 0 = none. Default 6 until Alpha lifts it.
MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR = int(
    os.getenv("MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR", "6") or 0)


def _parse_venue_caps(raw):
    """"venue=n,venue=n" -> {venue: n}. A value that is not a whole number >= 0 reads as 0 (no live
    sends on that venue): a typo in a cap must close it, never fall back to the looser default. A
    part with no '=' or no venue ("meteora_damm_v2:2") is kept under its own text at 0, so the slot
    lane's start warning names it instead of it vanishing."""
    out = {}
    for part in (raw or "").split(","):
        key, sep, value = part.partition("=")
        if not sep or not key.strip():
            if part.strip():
                out[part.strip().lower()] = 0
            continue
        try:
            cap = int(value.strip())
        except ValueError:
            cap = 0
        out[key.strip().lower()] = max(0, cap)
    return out


# Per-venue override of the cap above, e.g. "meteora_damm_v2=2" for the DAMM v2 canary (Gamma's
# pre-registration 7353d67e7: 2 an hour per venue). A venue not named uses the cap above.
MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE = _parse_venue_caps(
    os.getenv("MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE", ""))
# LAUNCHLAB PHASE (b), SHADOW ONLY (2026-09-27, Alpha GO). "1" builds a LaunchLab buy whose quote asset
# the lane keeps no leg for (xStocks, PreStocks, launchpad tokens: 22 of 31 census quote mints) with
# ONE on-demand Jupiter USDC -> quote leg per buy, on a bounded worker (<= 2 in flight, never the
# lane thread), simulated once, never sent. Its Jupiter client is the PAPER key (2 calls per such
# buy, about 7 buys/day), never the funded seam while LaunchLab is shadow-only; default off.
# CPMM EXOTIC QUOTES, SHADOW ONLY (2026-09-28, Alpha GO): the same on-demand leg worker and paper
# key for a CPMM buy quoted in an asset the lane keeps no leg for (65 of 83 census CPMM swaps; the
# first live case was ANTHROPIC). About 80 such buys/day, 2 paper-key calls each: ~160 calls/day,
# plus the exotic pools' first reads on the custody RPC. Default off.
MEMECOIN_SLOT_LANE_CPMM_ONDEMAND_LEG = os.getenv(
    "MEMECOIN_SLOT_LANE_CPMM_ONDEMAND_LEG", "0").strip().lower() in {"1", "true", "yes", "on"}
# QUOTE-RESIDUE SWEEP (2026-09-28, design section 8.1; memecoin_quote_residue). Enqueues the existing
# holding sell for a quote asset an ExactIn leg left behind, only when the wallet holds no more of it
# than the recorded residue (x1.02) and it is worth >= $5 (>= $0.50 after 7 days); never SOL/USDC,
# never a balance the operator holds on purpose. Inert until an exotic-quote venue goes live.
MEMECOIN_QUOTE_RESIDUE_SWEEP = os.getenv(
    "MEMECOIN_QUOTE_RESIDUE_SWEEP", "0").strip().lower() in {"1", "true", "yes", "on"}
# QUOTE-RESIDUE CREDITS (operator order 2026-10-07 ~15:30Z via Alpha). The holdings worker reads each
# real Solana exit's receipt once and records the quote asset it left (a native pump exit's tail is
# sized to the curve leg's minimum), then credits a holding sell's proceeds pro rata into the
# realized_pnl of the positions whose residue it sold (memecoin_quote_residue_credits, append-only).
# Default off. SINCE bounds both passes (native exits went live 2026-09-09); older history is the
# backfill script's (scripts/memecoin_quote_residue_backfill.py, operator --confirm).
MEMECOIN_QUOTE_RESIDUE_CREDITS = os.getenv(
    "MEMECOIN_QUOTE_RESIDUE_CREDITS", "0").strip().lower() in {"1", "true", "yes", "on"}
MEMECOIN_QUOTE_RESIDUE_CREDITS_SINCE = os.getenv(
    "MEMECOIN_QUOTE_RESIDUE_CREDITS_SINCE", "2026-09-09T00:00:00+00:00").strip()
# SENDER FOR EVERY SLOT-LANE VENUE (Alpha 2026-10-07; operator: Sender on every send). The A/B
# (MEMECOIN_SLOT_LANE_SENDER_AB, sha256-attempt-odd-v1) tips pump_curve/pump_amm on arm B only.
# Three modes, each needing the A/B's Sender provider; the tip cap and guard are unchanged; exits and
# every other lane stay untipped:
#   off (default)  today: the A/B on pump venues only, other venues untipped.
#   ab             the same sha256-attempt-odd hash on EVERY venue, trace rule
#                  sha256-attempt-odd-all-venues-v1 (a new cohort, split from the old by name).
#   all            arm B on every venue, trace rule all-venues-b-v1.
# Any other value reads off, with one warning naming it.
def slot_lane_sender_all_mode(raw):
    value = str(raw or "").strip().lower()
    if value in ("", "0", "off"):
        return "off"
    if value in ("ab", "all"):
        return value
    logging.getLogger("memecoin").warning(
        "MEMECOIN_SLOT_LANE_SENDER_ALL=%r is not off|ab|all: reading off (today's A/B on pump only)", raw)
    return "off"


MEMECOIN_SLOT_LANE_SENDER_ALL = slot_lane_sender_all_mode(os.getenv("MEMECOIN_SLOT_LANE_SENDER_ALL", ""))

# NOZOMI A/B (operator YES 2026-10-09 via Alpha; Gamma's pre-registration f7760ad8b): the source_user_ids
# whose slot-lane sends split on nozomi-sha256-attempt-odd-v1 (arm B tips Nozomi instead of Sender).
# Empty = no arm anywhere; '*' includes every nonempty source UID admitted by funded authority,
# including future armed wallets. The provider itself needs MEMECOIN_SLOT_LANE_NOZOMI_AB=1 and
# MEMECOIN_NOZOMI_API_KEY (solana_sender_ab.nozomi_provider).
MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS = frozenset(
    w.strip() for w in os.getenv("MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS", "").split(",") if w.strip())

# WARM-AFTER-USE EXOTIC LEGS (Alpha GO 2026-10-07): after a live exotic leg, keep that (venue, quote
# mint, clip) leg warm for 60 s on the prefetch thread (after the SOL legs; at most 2; own 429
# backoff), so a repeat buy skips the ~180 ms p50 leg. Measured since 10-02: 14 of 43 live legs had a
# same-quote leg in the 60 s before (all the same wallet). Default off.
MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS = os.getenv(
    "MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS", "0").strip().lower() in {"1", "true", "yes", "on"}
# How long a warm exotic leg stays warm after its last live use (Alpha (i), 2026-10-15). The leg is
# bound to its quote token, not to one coin's pool, so a repeat quote token on ANY coin is served:
# TTWofwAg repeats came 0.2..16.1 min apart. Two legs at most, re-quoted every 1.5 s while warm
# (~2.7 rps on the funded Jupiter key, accepted). 60 = the old behaviour. Read at lane start.
# Malformed, non-finite or not positive: 60, the old and lighter window, never an import error.
try:
    MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S = float(os.getenv("MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S", "1800"))
except ValueError:
    MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S = 60.0
if not (math.isfinite(MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S) and MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S > 0):
    MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S = 60.0
# The most warm exotic legs at once (Alpha GO 2026-10-16, RIBBIT 01cc75c7: one pool seeds a leg per
# inventory clip, smallest first, so a cap of 2 evicted the 50 USD trial clip in the same pass). The
# cap is max(2, min(this, inventory clips)), the HTTP budget 2 starts per leg per 1.5 s (80 funded
# Jupiter calls/min per leg while warm), and a same-binding re-seed renews the window. 2 or less, or
# malformed: the old warmer exactly (cap 2, 4 starts, no renewal). Read at lane start.
try:
    MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX = int(os.getenv("MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX", "4"))
except ValueError:
    MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX = 2
MEMECOIN_SLOT_LANE_LAUNCHLAB_ONDEMAND_LEG = os.getenv(
    "MEMECOIN_SLOT_LANE_LAUNCHLAB_ONDEMAND_LEG", "0").strip().lower() in {"1", "true", "yes", "on"}
# EXOTIC QUOTES LIVE (operator GO 2026-10-02 12:0xZ; spec
# docs/superpowers/specs/2026-10-02-memecoin-slot-lane-exotic-quotes-live-design.md). A comma list of
# raydium_cpmm, raydium_launchlab: a venue named here (and live in its own mode) sends a buy quoted in
# anything but SOL/USDC through the on-demand leg worker on the FUNDED Jupiter seam, handed back to
# the lane thread for the ordinary live path, capped by "<venue>:exotic" (default 2 an hour). Empty
# (default): exotic quotes stay shadow on the paper key.
# ONE-SHOT RE-QUOTE OF AN EXOTIC LEG OVER THE PACKET (Alpha GO 2026-10-07; 3fe2e25ea2aa refused at
# 1,293 bytes, a lost fill). off (default) | oversize: a build refused 'over the packet' releases its
# reservation and goes back to the on-demand leg worker for a tighter leg, maxAccounts 20 then
# onlyDirectRoutes, each at most once; the worker hands it back like any requeued leg, so the lane
# thread and the wallet lock never wait on it. Anything else reads off.
MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE = os.getenv("MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE", "off").strip().lower()
MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES = frozenset(
    part.strip().lower() for part in os.getenv("MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", "").split(",")
    if part.strip())
# GRADUATION DIRECT (operator order via Alpha 2026-10-07; memecoin_slot_graduation). When a copied
# curve buy cannot land because the curve completed (the slot-lane send reverts BondingCurveComplete,
# or the source's own TradeEvent leaves no real tokens), watch the canonical PumpSwap pool PDA and buy
# there with the ordinary live path and the same caps against the SOURCE's curve price, instead of
# waiting for Jupiter to index the new pool. off (default) | shadow (watch and log, never hold or
# send) | live. Anything else reads off. The deadline bounds the pool watch (5 graduations 09-28..10-07:
# the pool appeared 1-9 slots after our revert); the poll is the read cadence while waiting.
MEMECOIN_SLOT_LANE_GRADUATION_DIRECT = os.getenv("MEMECOIN_SLOT_LANE_GRADUATION_DIRECT", "off").strip().lower()
MEMECOIN_SLOT_LANE_GRADUATION_DEADLINE_S = float(os.getenv("MEMECOIN_SLOT_LANE_GRADUATION_DEADLINE_S", "6"))
MEMECOIN_SLOT_LANE_GRADUATION_POLL_S = float(os.getenv("MEMECOIN_SLOT_LANE_GRADUATION_POLL_S", "0.4"))
# QUOTE-TOKEN CURVES (operator "fix" via Alpha 2026-10-07, HI EtVFMDoB). live: graduation direct also
# watches a curve quoted in another token (its pool is coin/<quote>), offered before the on-demand leg
# when the source's own buy completed the curve, or on a 'curve is complete' build refusal; the
# hand-back takes a fresh leg into the quote. off (default) | live; a wSOL curve is the same either way.
# Inert unless MEMECOIN_SLOT_LANE_GRADUATION_DIRECT is live and pump_curve is exotic-live.
MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES = os.getenv(
    "MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES", "off").strip().lower()
# CPMM PHASE 2b, ZEC ONLY (2026-09-27, Alpha GO). 35 of 83 census CPMM swaps were quoted in Zcash
# (A7bdiY..., 8 decimals). "1" keeps a USDC -> ZEC Jupiter leg warm on the lane's prefetch thread so
# a ZEC-quoted CPMM pool builds without a synchronous Jupiter call. It SPENDS funded Jupiter calls
# (a /quote and a /swap-instructions per refresh, counted under role slot_leg), so it is default off.
# Stays OFF until CPMM goes live (Alpha 2026-09-27): no shadow path may call the funded seam, and the
# paper key cannot carry ~1.1 calls/s; raydium_cpmm_buy.zec_leg_on() ignores it while CPMM is shadow.
MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG = os.getenv(
    "MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG", "0").strip().lower() in {"1", "true", "yes", "on"}
# Funded mirror guard: a source partial sell or one other currently armed wallet with a fresh,
# explicit post-trade holding keeps us in the token; all independent exit rails remain active.
# The window is bounded because the global feed misses sells, so an old positive holding is not
# reliable current truth. $1 removes floating-point dust without guessing at position size.
# --- tp-only funded policy (2026-08-11) -------------------------------------------------------
# A FIXED take-profit with NO mirror-on-sell, for a roster of one wallet whose own exits we do not
# want to copy. Default OFF; enabling it makes `resolve_funded_policy` return this instead of the
# band policy for every non-engine chain.
#
# THE PARAMETERS ARE A RETROSPECTIVE CANDIDATE, NOT A MEASUREMENT. An earlier version of this
# comment said "measured, not chosen"; that was false and review caught it. +50 was the argmax of
# a 21-cell search (7 take-profit levels x 3 caps) on a single wallet.
#
# Reproduce with scripts/tp_only_evidence_artifact.py. At the frozen as_of, deduplicated to one
# path per MINT, randomuser123/solana has n=8 coins — an inline run that reported n=12 was
# counting positions, not coins. On the corrected unit the argmax moves to +150 (+16.4%/coin),
# but that cell fires on 1 of 8, its second time-half is NEGATIVE (-3.9) and it dies without its
# best coin (-2.1%). +50 gives +12.5%/coin, fires on 4 of 8, 90% CI [-10.5, +33.0].
#
# So the LEVEL is not determined by this data. What survives is the shape: the same coins fire at
# every target from +15 to +50, so a lower take-profit wins no extra trades and takes less from an
# identical set. +50 is kept as the least-fragile cell, not as a fitted optimum.
#
# Weak evidence, no forward test, default OFF, and scoped to a named wallet-chain allowlist.
# SCOPE (corrected 2026-08-11 by review): an earlier revision made this a global switch inside
# `resolve_funded_policy`, so it applied to EVERY wallet on EVERY non-engine chain — a policy fitted
# to one wallet's 12 coins would have been inherited silently by any wallet armed later. It is now
# an explicit allowlist of `handle:chain` pairs, evaluated where source_user_id is in scope.
# Empty = off. A wallet not named here is untouched.
# --- follow-30-50 funded policy (2026-08-11, operator-directed) ---------------------------------
# Flat +30% take-profit, -50% stop, and a stop confirmation window measured in SECONDS.
#
# The seconds matter. `MEMECOIN_REAL_STOP_CONFIRM_SWEEPS` counts SWEEPS, and the funded fast lane
# ticks every MEMECOIN_FASTLANE_TICK_S (1s), so the shipped "5 sweep" debounce is a FIVE SECOND
# window, not the ~75s its authors intended when the bracket pass ran at ~31s. A five-second dip
# below the stop sells the position. That is the shake-out this policy exists to prevent, so the
# confirmation is specified in seconds and converted against the live tick rate.
#
# 0 = keep the legacy sweep-count behaviour. Any positive value overrides it.
MEMECOIN_FOLLOW3050_WALLETS = tuple(
    x.strip() for x in os.getenv("MEMECOIN_FOLLOW3050_WALLETS", "").split(",") if x.strip())


def _source_mirror_tp100_wallets(raw):
    """Exact source UID + canonical chain pairs; malformed funded authority fails startup."""
    from sniper.services.memecoin_chains import canonical_chain_id
    if not str(raw or "").strip():
        return frozenset()
    pairs = set()
    for part in str(raw).split(","):
        uid, separator, chain = part.strip().rpartition(":")
        network = canonical_chain_id(chain)
        if (not separator or not uid or any(c.isspace() for c in uid)
                or network is None or network == "evm-unknown"):
            raise ValueError("MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS requires source_uid:chain pairs")
        pairs.add((uid, network))
    return frozenset(pairs)


# Explicit operator profile: TP100 or exact proportional source sells, with indefinite holds.
MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS = _source_mirror_tp100_wallets(
    os.getenv("MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", ""))
MEMECOIN_FOLLOW3050_TP_PCT = float(os.getenv("MEMECOIN_FOLLOW3050_TP_PCT", "30"))
MEMECOIN_FOLLOW3050_STOP_PCT = float(os.getenv("MEMECOIN_FOLLOW3050_STOP_PCT", "50"))
MEMECOIN_FOLLOW3050_CONFIRM_S = float(os.getenv("MEMECOIN_FOLLOW3050_CONFIRM_S", "75"))
# follow-30-50-v1 for EVERY funded wallet, not just MEMECOIN_FOLLOW3050_WALLETS. Default OFF.
# The allowlist is keyed by handle and read from memecoin_wallets; with the roster gate off an
# entry can come from a wallet with no row there, so the allowlist path falls through to the BAND
# DEFAULT (mirror_on_sell=True, trail, de-risk) for the majority of new positions. This makes the
# resolver return 30/70-no-mirror for all of them instead. Engine-authoritative chains keep their
# envelope — the check sits after that branch.
MEMECOIN_FOLLOW3050_ALL_WALLETS = os.getenv(
    "MEMECOIN_FOLLOW3050_ALL_WALLETS", "0").strip().lower() in {"1", "true", "yes"}
# These bands use their band policy before the global follow policy.
MEMECOIN_REAL_BAND_POLICY_BANDS = tuple(
    x.strip().lower() for x in os.getenv("MEMECOIN_REAL_BAND_POLICY_BANDS", "").split(",")
    if x.strip()
)


# Elapsed-time stop confirmation. 0 = off (legacy sweep count only). When positive, the runner
# holds a nominated stop until the breach has PERSISTED this long by wall clock, released by any
# quoted non-stop sweep exactly like the counter it supplements.
#
# CEILING, not decoration: a confirmation window that never resolves is far worse than one that
# fires early, because this policy family has no mirror and no trail — the stop and the -80%
# catastrophe floor are the only downside exits. The window is clamped so a clock fault, a stalled
# lane or a bad config cannot defer a stop indefinitely.
# INSTANT LOSS EXIT DEPTH (2026-09-03, operator: "if we read a price lets say -50% we should sell
# instantly i believe"). At or below this loss, a nominated stop skips confirmation entirely and
# sells on the FIRST read. A bad tick is what the debounce exists for; a read this deep is not a
# bad tick, and the wait is what costs money — FATCOIN went 22pp past its -35% stop in 4s and MEOW
# 13.5pp in 9s, together -$83.89, 62% of that day's gross losses.
#
# Default ACTIVE, unlike the confirmation knobs which default inert. For a loss rail the safe
# direction is selling, so a lost or empty .env must not silently restore the slower behaviour.
# 0 disables it and restores pure count-based debounce.
MEMECOIN_REAL_STOP_INSTANT_PCT = float(os.getenv("MEMECOIN_REAL_STOP_INSTANT_PCT", "50"))
MEMECOIN_REAL_STOP_CONFIRM_SECONDS = float(os.getenv("MEMECOIN_REAL_STOP_CONFIRM_SECONDS", "0"))
MEMECOIN_REAL_STOP_CONFIRM_MAX_S = float(os.getenv("MEMECOIN_REAL_STOP_CONFIRM_MAX_S", "180"))

# INSTANT-BYPASS GRACE WINDOW (2026-09-04, operator). DEBTCOIN (pos 50664) b_stopped on the FIRST
# exact quote within seconds of fill -- entry_liquidity $37.7K, token age 380s -- then
# round-tripped: sold at -59.7% on the depth bypass, a paper mirror on the same mint priced it
# back to 0.0003967 -- ABOVE our own 0.0003865 entry -- three minutes later, then geckoterminal
# caught it at -76.5% ninety seconds after that. A freshly-filled position on a thin/young pool
# can wick past the depth on its FIRST read and revert before a second one -- that is exactly the
# bad-tick case the debounce exists for (see MEMECOIN_REAL_STOP_INSTANT_PCT above), not the
# FATCOIN/MEOW slow-bleed case the depth was built for one day earlier. Below this age since the
# CONFIRMED fill, a nominated b_stop loses the instant bypass and falls back to the normal
# sweep-count/elapsed debounce -- it can still sell inside the window, just not off a single read.
#
# X (pos 50341, cited in an earlier version of this note as a second launch-chaos case) is NOT
# one: entry_token_age_s 609,811s (7.06 days), liquidity $19.1K -- a genuine -90.9% death spiral,
# not a wick. Caught by Gamma review before deploy: a standard funded 4663 contract freezes
# stop_pct=35 < catastrophe_pct=80, and b_stop's own check returns before catastrophe is ever
# considered (alpha_b_real_exit_reason) -- so ANY move past -80% still reports reason="b_stop",
# and gating on that label alone would have deferred X's own instant sell. The gate is MAGNITUDE
# against the frozen catastrophe floor (_effective_catastrophe_pct), not the label: a b_stop past
# that floor keeps its instant bypass at any age, same as a bare "catastrophe" reason. 0 disables
# the window and restores age-blind instant selling.
MEMECOIN_REAL_STOP_INSTANT_GRACE_S = float(os.getenv("MEMECOIN_REAL_STOP_INSTANT_GRACE_S", "20"))
# HARD NO-LOSS-SELL WINDOW (2026-09-04, operator). Distinct from the grace above: that one only
# removes the SINGLE-READ bypass and still lets a stop fire on two confirming reads. This one
# refuses a LOSS exit outright for the first N seconds after the fill.
#
# Measured over the whole funded history, forcing every loss exit inside the window to wait:
#   window   affected   total      median    improved
#      15s          5   +$23       +$1.82       3/5
#      20s          9   +$87       +$3.60       6/9
#      30s         12   +$88       +$2.71       7/12
#      45s         19   +$109      +$3.56      13/19
#      60s         25   +$124      +$1.18      14/25
# Positive at every window, worst single case -$29 (DEBTCOIN) and it does not grow with the
# window. Fast stops sell at a local low: for holds under 60s the price was above our exit 67-75%
# of the time within 15-60s.
#
# CATASTROPHE IS INCLUDED, deliberately. Loss exits inside 20s across the entire funded history:
# 18, of which catastrophe = 0. Inside 60s: 66, of which catastrophe = 1. The carve-out protected
# against something that has happened once.
#
# TAKE-PROFIT IS NOT BLOCKED. At a 60s window 121 exits are affected and 80 of them are TPs, worth
# about +$21 in aggregate against a worst case of -$1,706 (GPRO). Blocking those is enormous tail
# risk for nothing. 0 disables the window.
MEMECOIN_REAL_NO_LOSS_SELL_S = float(os.getenv("MEMECOIN_REAL_NO_LOSS_SELL_S", "0"))


def stop_confirm_window_s(seconds=None):
    """The elapsed-time stop window, clamped. 0 disables it."""
    secs = MEMECOIN_REAL_STOP_CONFIRM_SECONDS if seconds is None else float(seconds)
    if not (secs > 0) or secs != secs:
        return 0.0
    return min(float(secs), float(MEMECOIN_REAL_STOP_CONFIRM_MAX_S))


# TP CONFIRMATION WINDOW (2026-08-29, operator). The inverse of the stop's: on the first mark at
# or above the take-profit, hold while the breach stays CONTINUOUS, and sell at the CURRENT mark
# once the window elapses — the point is selling into continuation rather than at the touch.
#
# DEFAULT 0, WHICH IS TODAY'S INSTANT BEHAVIOUR EXACTLY. Rollback is the env, not a code change.
#
# THE CLAMP IS TIGHTER THAN THE STOP'S 180s AND FOR THE OPPOSITE REASON. A deferred stop risks
# money already lost; a deferred take-profit risks giving back a gain that is already on the
# table, and the operator has accepted that a revert inside the window loses the exit. At 30s the
# leak is bounded to something the trail and timecap can still own. The study's candidate windows
# are 3, 5 and 10 seconds, so this leaves room to test without leaving room to defer indefinitely.
# SOURCE-IMPACT CHASE ALLOWANCE (2026-08-29, operator). The chase cap measures our quote against
# the trader's FILL, which on a thin book already contains his own price impact — s4if's $999 into
# a $72k coin was refused at +31% against a cap of 30. The gate charged us his footprint.
#
# The runner estimates the allowance per event. It uses one exact quote at the source size.
# This ceiling bounds the model error on a low-liquidity pool.
#
# WHAT THE CLAMP GUARDS, from the read register (104f11f7). With the live caps — 15 standard, 30
# fresh-follow — this clamp puts the effective ceiling at 1.40x and 1.55x, so the >= 2.0x cliff
# (median -63.4% over n=12 of 443 funded closes) is unreachable. RAISING THIS KNOB WALKS TOWARD IT.
#
# THE BENCHMARK IS THE REGION ONLY THE EXTENSION REACHES, not the coarse band: 1.30x to 1.55x runs
# mean -10.8% / median -15.9% historically (n=42). That is what impact-adjusted admissions are
# graded against, and BEATING IT IS NOT MAKING MONEY — a -5% admission beats the benchmark. The
# registered kill reads against this number at n >= 40.
# THE STUDY BACKLOG BOUNDARY (2026-08-29). Study-arm positions opened before this ISO timestamp
# were never evaluated by any exit sweep — the arms were wired into the open path and not the exit
# path — so they close under `sweep_closure` with a NULL realized PnL rather than under a rail that
# did not decide them. EMPTY IS THE DEFAULT and means no boundary: every position takes its rails.
# Set it once, to the deploy stamp of the sweep fix, and never again.
MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE = os.getenv("MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE", "").strip()
MEMECOIN_CHASE_SOURCE_IMPACT_MAX_PCT = float(
    os.getenv("MEMECOIN_CHASE_SOURCE_IMPACT_MAX_PCT", "25"))
# THE CHASE REFERENCE (2026-09-28, operator: "measure what we can actually control"). `avg_fill`
# (default, today) compares our quote with the source's AVERAGE fill, which charges the source's own
# price impact to us. `post_trade` compares with the price right AFTER the source's buy, taken from
# the same receipt the on-chain watcher already decoded (the slot lane's reference). It applies to
# on-chain-lane Solana events that carry a post-trade price; anything else keeps the average fill.
# Spec docs/superpowers/specs/2026-09-28-memecoin-chase-post-trade-reference-design.md.
MEMECOIN_REAL_CHASE_REFERENCE = os.getenv("MEMECOIN_REAL_CHASE_REFERENCE", "avg_fill").strip().lower()
# THE RISK BOUND on that reference (Lambda's review F1, 2026-09-28): the post-trade reference may sit
# at most this many times above the source's average fill. Without it, a source that buys a third of
# a thin pool (ratio 1.5) would let us pay 1.95x its average at cap 30, and the plausibility filter
# alone allows 50x. 2.0 admits 3310 (ratio ~1.6) and caps the premium over the average at 2.6x.
MEMECOIN_CHASE_POST_TRADE_MAX_RATIO = float(os.getenv("MEMECOIN_CHASE_POST_TRADE_MAX_RATIO", "2.0"))


def memecoin_evm_post_trade_chains(env=None):
    """EVM chains whose chase gates may use the source's post-trade price (spec
    2026-09-28-memecoin-evm-chase-post-trade-reference-design.md), as network ids. Read at call time;
    default empty = every EVM gate keeps the average fill. Applies only while
    MEMECOIN_REAL_CHASE_REFERENCE=post_trade."""
    env = os.environ if env is None else env
    raw = str(env.get("MEMECOIN_REAL_CHASE_REFERENCE_EVM_CHAINS", "") or "")
    from sniper.services.memecoin_chains import canonical_chain_id
    out = set()
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        chain = canonical_chain_id(part)
        if chain is None or chain == "solana":
            logging.getLogger("memecoin").warning(
                "MEMECOIN_REAL_CHASE_REFERENCE_EVM_CHAINS names %r, which is no EVM chain: ignored", part)
            continue
        out.add(chain)
    return frozenset(out)


# DEXSCREENER OUT, PHASE A (2026-09-28, spec 2026-09-28-memecoin-dexscreener-out-phase-a-design.md).
# Consumer ids from docs/audits/2026-09-28-dexscreener-removal-inventory.md. E3 is the pre-quote
# display chase (kept ON by decision; the id exists so the switch is one edit), X1 the funded exit
# sweep's own read, X2 the fast lane's batch. E5 is deferred with the market-data GET itself.
# PER CHAIN (Alpha 2026-09-29): `X1:4663` switches one consumer off on one chain, a bare `X1` on
# every chain. Chain names are canonicalized (`bsc` = 56, `base` = 8453).
# E9 (EVM chain resolution) and E10 (V4 pool discovery depth, also the multihop's legs) joined on
# 2026-09-29 (spec 2026-09-29-memecoin-dexscreener-out-e9-e10-design.md). E9 is bare only: the chain
# is exactly what it cannot know, so an `E9:<chain>` entry parses and never matches a call.
DEXSCREENER_CONSUMER_IDS = frozenset({"E3", "E6", "E7", "E9", "E10", "E11", "X1", "X2"})
_DEXSCREENER_WARNED = set()


def _dexscreener_warn(entry, why):
    if entry in _DEXSCREENER_WARNED:
        return
    _DEXSCREENER_WARNED.add(entry)
    logging.getLogger("memecoin").warning(
        "MEMECOIN_DEXSCREENER_OFF entry %r ignored: %s (consumers: %s)",
        entry, why, ",".join(sorted(DEXSCREENER_CONSUMER_IDS)))


def dexscreener_off_entries(env=None):
    """{(consumer, chain or None)} named in MEMECOIN_DEXSCREENER_OFF; None = every chain."""
    env = os.environ if env is None else env
    raw = str(env.get("MEMECOIN_DEXSCREENER_OFF", "") or "")
    from sniper.services.memecoin_chains import canonical_chain_id
    out = set()
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        consumer, _sep, chain = part.partition(":")
        consumer, chain = consumer.strip().upper(), chain.strip()
        if consumer not in DEXSCREENER_CONSUMER_IDS:
            _dexscreener_warn(part, "not a phase-A consumer")
            continue
        if not chain:
            out.add((consumer, None))
            continue
        canon = canonical_chain_id(chain)
        if canon is None:
            _dexscreener_warn(part, "unknown chain")
            continue
        out.add((consumer, canon))
    return out


def dexscreener_off(consumer, chain=None, env=None):
    """True when MEMECOIN_DEXSCREENER_OFF switches this consumer off on this chain (a bare entry covers
    every chain; without a chain only a bare entry answers). Read at call time; empty = today."""
    entries = dexscreener_off_entries(env)
    consumer = str(consumer or "").upper()
    if (consumer, None) in entries:
        return True
    if chain is None or not entries:
        return False
    from sniper.services.memecoin_chains import canonical_chain_id
    canon = canonical_chain_id(str(chain))
    return canon is not None and (consumer, canon) in entries


_NULL_READ_STEP_WARNED = set()


def memecoin_onchain_evm_null_read_step_s(chain, env=None):
    """The EVM push watcher's null-read retry step for `chain`, in seconds, or None for today's
    MEMECOIN_ONCHAIN_EVM_NULL_READ_RETRY_S / (MAX_RETRIES + 1) (spec
    2026-09-29-memecoin-evm-null-read-step-design.md, Alpha GO: `56:0.1`). A chain map read at call
    time; chain names are canonicalized, the last entry for a chain wins, and an entry that is not
    `<chain>:<finite seconds > 0>` is ignored and named once in a warning."""
    env = os.environ if env is None else env
    raw = str(env.get("MEMECOIN_ONCHAIN_EVM_NULL_READ_STEP_S", "") or "")
    from sniper.services.memecoin_chains import canonical_chain_id
    want, out = canonical_chain_id(str(chain)), None
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        name, _sep, value = part.partition(":")
        canon = canonical_chain_id(name.strip())
        try:
            step = float(value)
        except ValueError:
            step = None
        if canon is None or step is None or not math.isfinite(step) or step <= 0:
            if part not in _NULL_READ_STEP_WARNED:
                _NULL_READ_STEP_WARNED.add(part)
                logging.getLogger("memecoin").warning(
                    "MEMECOIN_ONCHAIN_EVM_NULL_READ_STEP_S entry %r ignored: want <chain>:<seconds > 0>",
                    part)
            continue
        if canon == want:
            out = step
    return out


def memecoin_onchain_evm_preconf_chains(env=None):
    """Chains whose EVM watcher also detects armed buys from preconfirmations (spec 2026-10-09-memecoin-
    base-flashblocks-detection-design.md, operator via Alpha: live). Base only: its endpoint and its
    zero-block-hash preconf shape are pinned in code. Read at call time; default empty = today's
    watcher; any other or malformed entry = empty."""
    env = os.environ if env is None else env
    parts = [p.strip() for p in str(env.get("MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS", "") or "").split(",")
             if p.strip()]
    return frozenset(parts) if parts and all(p == "8453" for p in parts) else frozenset()


def memecoin_onchain_evm_j7_xcheck_chains(env=None):
    """Chains whose EVM push watcher checks its topic-only subscriptions against J7 (spec
    2026-09-29-memecoin-evm-watch-dead-subscription-design.md, Alpha GO). Read at call time; default
    empty = today's watcher, no check."""
    env = os.environ if env is None else env
    raw = str(env.get("MEMECOIN_ONCHAIN_EVM_J7_XCHECK_CHAINS", "") or "")
    # Every J7 data-only chain runs the check too: there J7 is the watcher's liveness reference
    # and nothing else (operator 2026-09-30), so a data-only chain without it would miss a dead
    # filter that J7 could have named.
    raw += "," + str(env.get("MEMECOIN_J7_DATA_ONLY_EVM_CHAINS", "") or "")
    from sniper.services.memecoin_chains import canonical_chain_id
    return frozenset(str(canonical_chain_id(p.strip()) or p.strip().lower())
                     for p in raw.split(",") if p.strip()) - {"solana"}


def memecoin_onchain_evm_sell_shadow_chains(env=None):
    """Chains whose EVM watcher subscribes Transfers FROM watched wallets and records proved sales in
    the SHADOW table (spec 2026-09-29-memecoin-evm-onchain-sell-path-design.md, Alpha GO). Read at
    call time; default empty = today's watcher. Nothing acts on the rows."""
    env = os.environ if env is None else env
    raw = str(env.get("MEMECOIN_ONCHAIN_EVM_SELL_SHADOW_CHAINS", "") or "")
    from sniper.services.memecoin_chains import canonical_chain_id
    return frozenset(canonical_chain_id(p.strip()) or p.strip().lower()
                     for p in raw.split(",") if p.strip())


def memecoin_onchain_v2_swap_chains(env=None):
    """Chains whose volume tape subscribes the Uniswap-V2 and Solidly/Aerodrome Swap topics (spec
    2026-09-28-memecoin-8453-tape-v2-swaps-design.md), as network ids. Read at call time; default
    empty = today's tape. The reader counts a `v2` pool as covered only on these chains."""
    env = os.environ if env is None else env
    raw = str(env.get("MEMECOIN_ONCHAIN_V2_SWAP_CHAINS", "") or "")
    from sniper.services.memecoin_chains import canonical_chain_id
    out = set()
    for part in (p.strip() for p in raw.split(",")):
        chain = canonical_chain_id(part) if part else None
        if part and (chain is None or chain == "solana"):
            logging.getLogger("memecoin").warning(
                "MEMECOIN_ONCHAIN_V2_SWAP_CHAINS names %r, which is no EVM chain: ignored", part)
        elif chain:
            out.add(chain)
    return frozenset(out)


def chase_post_trade_max_ratio():
    """MEMECOIN_CHASE_POST_TRADE_MAX_RATIO as the clamp actually applies it: a value below 1, not
    finite or unreadable falls back to 2.0. Below 1 used to DISABLE the clamp (Beta F6, 2026-09-28)."""
    try:
        value = float(MEMECOIN_CHASE_POST_TRADE_MAX_RATIO)
    except (TypeError, ValueError):
        return 2.0
    return value if math.isfinite(value) and value >= 1.0 else 2.0
# The near-zero-impact reference size for the RATIO impact estimator (2026-09-02). Two quotes on
# the same pool in the same instant give impact = price_at_dust / price_at_size - 1, which needs no
# provider impact field and no external mid. $5 is small against the measured 4663 gate population
# (p1 liquidity $1,071 -> 0.47% self-impact, p50 $501,164 -> 0.001%) and large enough to be a real
# quote rather than dust the fee-tier ladder rounds away. Provisional and ungraded: the value that
# should be here is the one the refusal-path telemetry will show, and it is recorded per attempt so
# the estimator stays reconstructable when it changes.
MEMECOIN_CHASE_IMPACT_DUST_USD = float(
    os.getenv("MEMECOIN_CHASE_IMPACT_DUST_USD", "5"))
MEMECOIN_REAL_TP_CONFIRM_SECONDS = float(os.getenv("MEMECOIN_REAL_TP_CONFIRM_SECONDS", "0"))
MEMECOIN_REAL_TP_CONFIRM_MAX_S = float(os.getenv("MEMECOIN_REAL_TP_CONFIRM_MAX_S", "30"))


def tp_confirm_window_s(seconds=None):
    """The elapsed-time take-profit window, clamped. 0 disables it and restores instant TP."""
    secs = MEMECOIN_REAL_TP_CONFIRM_SECONDS if seconds is None else float(seconds)
    if not (secs > 0) or secs != secs:
        return 0.0
    return min(float(secs), float(MEMECOIN_REAL_TP_CONFIRM_MAX_S))


# REMOVED: stop_confirm_sweeps_for(). It converted a seconds target into a FROZEN hit count using
# a NOMINAL tick rate, which is not a duration. The runner counts detections with no clock, the
# fastlane sleeps AFTER variable quote work so 1s is a floor rather than a rate, and a slow-lane
# fallback at ~31s turned a "75 second" window into 38 MINUTES. Elapsed time is measured by
# stop_breach_confirmed() against a stored breach timestamp instead.


MEMECOIN_REAL_TP_ONLY_WALLETS = tuple(
    x.strip() for x in os.getenv("MEMECOIN_REAL_TP_ONLY_WALLETS", "").split(",") if x.strip())
MEMECOIN_REAL_TP_ONLY_TP_PCT = float(os.getenv("MEMECOIN_REAL_TP_ONLY_TP_PCT", "50"))
MEMECOIN_REAL_TP_ONLY_STOP_PCT = float(os.getenv("MEMECOIN_REAL_TP_ONLY_STOP_PCT", "60"))
MEMECOIN_REAL_TP_ONLY_MAX_HOLD_S = float(os.getenv(
    "MEMECOIN_REAL_TP_ONLY_MAX_HOLD_S", "21600"))          # 6h

MEMECOIN_REAL_MIRROR_HOLDER_WINDOW_S = float(os.getenv(
    "MEMECOIN_REAL_MIRROR_HOLDER_WINDOW_S", "3600"))
MEMECOIN_REAL_MIRROR_MIN_REMAINING_USD = float(os.getenv(
    "MEMECOIN_REAL_MIRROR_MIN_REMAINING_USD", "1"))
# SIZE-AWARE COPY-SELL (2026-09-15, operator: "sell size is important ... recognize profit taking
# from derisking or actually exiting"). On these chains the copied wallet's SELL closes our position
# when it has sold at least MIN_FRACTION of its holding in the coin since our entry (cumulative,
# tokens rebuilt from memecoin_events). A smaller sell is a profit-taking trim and we hold on rails.
# An operator OVERLAY on the frozen contract's mirror_on_sell=False: it replaces the old whole-exit
# test ("source remaining < $1", unreadable under J7) on the listed chains only. The per-position
# operator mirror_disabled flag still wins. Evidence (funded, source sold while we held, sold at
# their price -3%): Solana +$231 over 28 (better 71%); 30-70% sold +$134, >=95% +$212, <30% -$9;
# Robinhood -$35 over 22, so it is not listed. Empty = off everywhere.
MEMECOIN_REAL_MIRROR_SIZE_CHAINS = frozenset(
    part.strip().lower() for part in os.getenv("MEMECOIN_REAL_MIRROR_SIZE_CHAINS", "").split(",")
    if part.strip())
MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION = float(os.getenv(
    "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", "1.0"))
# At 1.0, only an on-chain sale plus a known zero owner token holding permits a mirror exit.
# Partial sells, unknown balances, USD-only feed events and V4 first-sell overrides hold on rails.
# Trader buys older than this before our entry do not count toward the holding: a stale campaign
# that exited through a sell we never saw would otherwise shrink every later sell's share.
MEMECOIN_REAL_MIRROR_SIZE_LOOKBACK_S = float(os.getenv(
    "MEMECOIN_REAL_MIRROR_SIZE_LOOKBACK_S", "86400"))
# SOURCE-SELL MIRROR (2026-09-27, operator order: "Build the mirror exits for safety", and "sell
# without waiting for any confirmation"). A registry owner's own sell, decoded from the slot lane's
# processed socket (or the confirmed watcher's backup row), exits the funded Solana position copied
# from that owner when the cumulative share it sold reaches MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION.
# Full-only uses the existing background resolver for an owner-wide zero-balance proof;
# account-only drops and unknown holdings cannot fire. Default ON by operator
# order; SHADOW=1 records the decision and never exits.
MEMECOIN_SOURCE_SELL_MIRROR_ENABLED = os.getenv(
    "MEMECOIN_SOURCE_SELL_MIRROR_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
MEMECOIN_SOURCE_SELL_MIRROR_SHADOW = os.getenv(
    "MEMECOIN_SOURCE_SELL_MIRROR_SHADOW", "0").strip().lower() in {"1", "true", "yes", "on"}
# The mirror's ACCOUNT path (2026-09-27): accountSubscribe at processed on each held position's
# source-owner token account, on the keyless read RPC. Sees routed sells (DFlow, OKX, Jupiter into
# any venue) exactly. 0 = logs and watcher paths only.
MEMECOIN_SOURCE_SELL_ACCOUNT_WATCH_ENABLED = os.getenv(
    "MEMECOIN_SOURCE_SELL_ACCOUNT_WATCH_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
# SLOT-LANE COMPUTE BUDGET (2026-09-27, Alpha). The lane's curve and PumpSwap buys carry their own
# compute-unit limit and price. Leaders order by fee per REQUESTED unit, so a tight limit with a
# higher price buys the same priority for less. Defaults are the values hard-coded until now;
# validate_real_exit_policy bounds both, so a bad value fails the deploy verify, not a buy.
MEMECOIN_SLOT_LANE_CU_LIMIT = int(os.getenv("MEMECOIN_SLOT_LANE_CU_LIMIT", "400000"))
# The entry-bid normal bid on a cold or stale pool (every first buy of a new coin, 2026-10-15), TOTAL
# lamports at any CU limit; still under MAX_TOTAL_LAMPORTS minus the tip and under a crowded override.
# Malformed or <= 0: 1,520,000, the constant it replaces (solana_entry_bids.FALLBACK_PRIORITY_LAMPORTS).
try:
    MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS = int(os.getenv("MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS", "1520000"))
except ValueError:
    MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS = 1_520_000
if MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS <= 0:
    MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS = 1_520_000
MEMECOIN_SLOT_LANE_CU_PRICE_MICROLAMPORTS = int(
    os.getenv("MEMECOIN_SLOT_LANE_CU_PRICE_MICROLAMPORTS", "200000"))
# SLOT-LANE CU LIMIT PER BUILD CLASS (2026-10-02, operator "fix everything" via Alpha). A transaction
# that runs out of compute units fails and the trade is lost. So a class (venue, funding, leg dex)
# gets a limit of its own only when at least SLOT_LANE_CU_MIN_SENDS successful sends of THAT class
# were measured (Gamma: different dexes cost different CU, and a dex seen 1-4 times has no measured
# tail). The limit is the recorded max x SLOT_LANE_CU_MARGIN, rounded up to the next 1k, and applies
# only to a build whose coin token program was in that class's sample. Every other build keeps
# MEMECOIN_SLOT_LANE_CU_LIMIT: unmeasured native and exotic funding, CPMM, LaunchLab, DAMM, a future venue, an
# unmeasured or multi-step leg, an unsampled token program, and a class demoted after a send ran out
# of compute units (pump_curve_buy.demote_cu_class).
# "leg" = the build spends the prefetched USDC -> SOL leg; the dex is its Jupiter routePlan label
# (DEFAULT_LEG_QUOTE_PARAMS allows BisonFi, ZeroFi, Raydium and SolFi V2), read from each receipt's
# leg program: BiSoNHVps... BisonFi, ZERor4xhb... ZeroFi, SV2EYYJyRz2... SolFi V2. No sampled send
# used Raydium. Evidence: our own successful slot-lane receipts 2026-09-25..10-02 (Gamma,
# ~/.cache/gamma/cuvenue; composition ~/.cache/lambda/cu_composition_20261002.json). Per class:
# (successful sends, max computeUnitsConsumed, coin token programs sampled).
SLOT_LANE_CU_MIN_SENDS = 10
SLOT_LANE_CU_MARGIN = 1.15
_T22_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
_SPL_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# Native PumpAMM (2026-10-12): 21 fetched own successful Token2022 receipts, 26-account
# buy_exact_quote_in, wSOL create/transfer/sync + coin idempotent ATA, optional 150-CU tip.
# Both ATAs created in the max (132132); coin reuse measured once (105860). One unsigned
# Europe Shyft simulation proves idempotent reuse costs less: wSOL 5938 < 15014, T22
# 5956 < 18717. Evidence: tests/fixtures/memecoin/slot_lane_native_cu_evidence.json.
# shortcut: this Token2022 composition uses the measured tail plus 15%; demote on exhaustion,
# and require new receipt evidence for a new token program or instruction layout.
SLOT_LANE_CU_CLASSES = {
    ("pump_amm", "native", "26_accounts"): (21, 132_132, frozenset({_T22_PROGRAM})),
    ("pump_curve", "leg", "BisonFi"): (20, 179_887, frozenset({_T22_PROGRAM})),
    ("pump_curve", "leg", "ZeroFi"): (4, 148_455, frozenset({_T22_PROGRAM})),        # n < 10: default
    ("pump_curve", "leg", "SolFi V2"): (1, 211_983, frozenset({_T22_PROGRAM})),      # n < 10: default
    ("pump_amm", "leg", "BisonFi"): (21, 186_857, frozenset({_T22_PROGRAM, _SPL_PROGRAM})),
    ("pump_amm", "leg", "ZeroFi"): (10, 158_250, frozenset({_T22_PROGRAM, _SPL_PROGRAM})),
}
# Operator 2026-10-23: a warm pool bids its measured p95 (floored at PRIORITY_FLOOR_MICRO_LAMPORTS), no longer at
# least the cold fallback. 1 restores the 2026-10-16 floor (a warm bid never under the fallback).
MEMECOIN_SOLANA_ENTRY_BID_WARM_FLOOR_AT_FALLBACK = os.getenv(
    "MEMECOIN_SOLANA_ENTRY_BID_WARM_FLOOR_AT_FALLBACK", "0").strip().lower() in {"1", "true", "yes"}


def slot_lane_cu_class_limit(max_cu):
    """The recorded max x SLOT_LANE_CU_MARGIN, rounded up to the next 1,000 units."""
    return int(math.ceil(float(max_cu) * SLOT_LANE_CU_MARGIN / 1000.0 - 1e-9)) * 1000


def slot_lane_cu_override_name(venue, funding, dex):
    """MEMECOIN_SLOT_LANE_CU_LIMIT_<VENUE>_<FUNDING>_<DEX>, e.g. ..._PUMP_AMM_LEG_BISONFI."""
    import re
    return "MEMECOIN_SLOT_LANE_CU_LIMIT_" + re.sub(
        r"[^A-Z0-9]+", "_", ("%s_%s_%s" % (venue, funding, dex)).upper()).strip("_")


MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS = {
    cls: int(os.getenv(slot_lane_cu_override_name(*cls), str(slot_lane_cu_class_limit(mx))))
    for cls, (n, mx, _programs) in SLOT_LANE_CU_CLASSES.items()
    if n >= SLOT_LANE_CU_MIN_SENDS}


def slot_lane_cu_class(venue, funding, *, leg_dex=None, token_program=None):
    """Measured (venue, funding, leg dex or native layout), or None (the lane default)."""
    cls = (venue, funding, leg_dex)
    if cls not in MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS:
        return None
    return cls if token_program in SLOT_LANE_CU_CLASSES[cls][2] else None


# EXOTIC-LEG CU RAISE (operator via Alpha 2026-10-07, option (b): live, default on). An exotic-quote
# build has no measured class, so it signed the lane default; 3wGiVeiz (QUBTAD8C) ran out at the then
# 260,000: quote ATA 22.2k + one Raydium CLMM hop 58.5k + coin ATA 18.6k + pump BuyExactQuoteInV2
# still running at 160.3k. A build that spends an on-demand leg now estimates its composition from the
# leg's own routePlan (in memory): ATA creates + each hop's budget + the venue's budget, and signs
# max(default, estimate x MARGIN) rounded up to 1k, capped at the chain's 1.4M. It never lowers.
# Budgets: our own slot receipts 2026-09-28..10-07 (164 sends, per top-level instruction from the
# logs), max x 1.3 rounded up to 10k. A hop's budget is the whole Jupiter route instruction for one
# hop, Jupiter's ~6-7k included: BisonFi n>=40 max 62.6k, ZeroFi n>=30 34.8k, Raydium CLMM n=7 66.4k,
# Manifest n=4 28.3k, Whirlpool n=3 49.3k. Meteora DLMM (cost grows with bins crossed), SolFi V2,
# Tessera, GoonFi and any other label have one sample or none: 150k. Venues: pump_amm max 105.3k;
# raydium_cpmm 33.5k; raydium_launchlab 55.9k; pump_curve here is the quote-token curve buy, whose
# tail is unmeasured (3wGiVeiz ran past 160k): 250k; DAMM v2 and DBC have no live receipt: 150k.
# ATA creates: two (quote and coin) at <= 28.3k each.
# Hadron (2026-10-29, 3GyqLyoz CHLOE): one hop ran 300.7k, double the blind 150k, and starved the
# pump_amm buy at 52.3k; 300.7k x 1.3 rounded up. Label per Jupiter program-id-to-label.
SLOT_LANE_EXOTIC_CU_HOP = {"BisonFi": 90_000, "ZeroFi": 50_000, "Raydium CLMM": 90_000,
                           "Manifest": 40_000, "Whirlpool": 70_000, "Hadron": 400_000}
SLOT_LANE_EXOTIC_CU_VENUE = {"pump_amm": 140_000, "raydium_cpmm": 50_000, "raydium_launchlab": 80_000,
                             "pump_curve": 250_000}
SLOT_LANE_EXOTIC_CU_UNMEASURED = 150_000
SLOT_LANE_EXOTIC_CU_ATAS = 60_000
SLOT_LANE_EXOTIC_CU_MARGIN = 1.15
SLOT_LANE_EXOTIC_CU_CAP = 1_400_000


_EXOTIC_CU_BLIND = {}
_EXOTIC_CU_BLIND_MAX = 32


def _count_blind_hops(labels):
    """Unmeasured hop labels, counted in memory for the heartbeat (2026-10-29, CHLOE's Hadron hop
    signed the blind budget unseen). No I/O on the build path. A racing increment can lose a count,
    so the counts are lower bounds."""
    try:
        for label in labels:
            key = label if label in _EXOTIC_CU_BLIND or len(_EXOTIC_CU_BLIND) < _EXOTIC_CU_BLIND_MAX else "(other)"
            _EXOTIC_CU_BLIND[key] = _EXOTIC_CU_BLIND.get(key, 0) + 1
    except Exception:   # diagnostics only: a build never fails on them
        pass


def slot_lane_exotic_cu_blind_counts():
    """{label: count} snapshot for the heartbeat; None when a concurrent write broke the copy."""
    try:
        return dict(_EXOTIC_CU_BLIND)
    except Exception:
        return None


def slot_lane_exotic_cu(leg, venue, base_limit):
    """(limit, leg_hops, cu_estimate) for a build spending an on-demand leg. No routePlan: the base
    limit and (None, None). The limit is never below `base_limit`. Pure, no I/O."""
    plan = ((leg or {}).get("quote") or {}).get("routePlan") if isinstance(leg, dict) else None
    if not isinstance(plan, list) or not plan:
        return int(base_limit), None, None
    labels = [str(((step or {}).get("swapInfo") or {}).get("label")) if isinstance(step, dict) else ""
              for step in plan]
    _count_blind_hops([label for label in labels if label not in SLOT_LANE_EXOTIC_CU_HOP])
    estimate = (SLOT_LANE_EXOTIC_CU_ATAS
                + SLOT_LANE_EXOTIC_CU_VENUE.get(venue, SLOT_LANE_EXOTIC_CU_UNMEASURED)
                + sum(SLOT_LANE_EXOTIC_CU_HOP.get(label, SLOT_LANE_EXOTIC_CU_UNMEASURED) for label in labels))
    raised = int(math.ceil(estimate * SLOT_LANE_EXOTIC_CU_MARGIN / 1000.0 - 1e-9)) * 1000
    return min(SLOT_LANE_EXOTIC_CU_CAP, max(int(base_limit), raised)), len(labels), estimate


def slot_lane_cu_limit(venue, funding, *, leg_dex=None, token_program=None):
    """The compute-unit limit for one slot-lane build. Pure lookup, no I/O."""
    cls = slot_lane_cu_class(venue, funding, leg_dex=leg_dex, token_program=token_program)
    return MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS[cls] if cls else MEMECOIN_SLOT_LANE_CU_LIMIT
# ORDINARY ENTRY CU PRICE (2026-09-27, Alpha). The Jupiter and Raydium-fallback entry builds.
# Unset (None) signs what they always did: Jupiter prioritizationFeeLamports "auto" and Raydium
# 25,000. Set, both carry this micro-lamport-per-CU price; Jupiter keeps dynamicComputeUnitLimit,
# as the exit path does. 0 is refused, not "no fee": Jupiter would then build with no priority.
MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS = _opt_int("MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS")
# Observation-only protected-hold evidence. Bounded near the measured armed fast-lane sweep;
# stale/unknown evidence can never suspend a funded stop (v1 only records would_protect).
MEMECOIN_HOLDER_EVIDENCE_MAX_AGE_S = float(os.getenv(
    "MEMECOIN_HOLDER_EVIDENCE_MAX_AGE_S", "120"))
# REAL-leg exit rails (decoupled from the paper alpha_b study knobs so operator changes to the
# live book never pollute the pre-registered A/B). 0 = that rail DISABLED. Defaults mirror the
# paper alpha_b rails; mirror-on-sell is feed-driven and always on regardless of these.
MEMECOIN_REAL_STOP_PCT = float(os.getenv("MEMECOIN_REAL_STOP_PCT", str(MEMECOIN_B_STOP_PCT)))


# IN-BAND m5 TURNOVER FLOOR (2026-09-08, operator). `MEMECOIN_REAL_MIN_TURNOVER_M5` is only
# consulted when the caller marks the volume block active, which since 0ef67c91 means mcap ABOVE
# MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD -- and `band_ok` inside volume_alive_decision is the
# opposite test, so it could never reject. This is the floor for coins BELOW the boundary, which is
# the whole funded nano/micro book.
#
# 2% measured on 621 closed funded positions: refuses 68 worth -$262.26 and keeps 556 worth
# +$826.19. Neighbours hold (1.5% -> +$202, 3.0% -> +$154), so it sits on a plateau. Applied
# globally by operator choice: on 4663 the sub-2% band is -$308 over 30, on Solana +$45 over 38, so
# going global forgoes that $45 and still nets +$262. 0 disables.
MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5 = float(
    os.getenv("MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5", "0"))
# IN-BAND m5 TURNOVER CEILING (2026-09-08). The upper edge of the band the evidence actually
# describes. `MEMECOIN_REAL_MAX_TURNOVER_M5` cannot fire at any market cap for the same reason the
# in-band floor could not, and it stays inactive by operator decision (priced separately: its whole
# case was 2026-08-27 at -$264.82 against +$169.48 on every other day combined).
#
# This is a DIFFERENT cut with different evidence. Codex's win/loss review found, and an independent
# 370-close cut replicated, that turnover 10-50% WITH a $1k-10k source buy returns +21.72% (n=44,
# win 75%) while neither leg alone clears +6%. Above 0.8x turnover my cut measures -7.88% (n=73).
# 0 = off, so the code lands inert until a value is set.
MEMECOIN_REAL_INBAND_MAX_TURNOVER_M5 = float(
    os.getenv("MEMECOIN_REAL_INBAND_MAX_TURNOVER_M5", "0"))
# MICRO-BAND m5 DOLLAR FLOOR (2026-09-18, operator). A DOLLAR bar, not a ratio, and it is a
# different cut from every turnover leg above.
#
# The operator's target is a take-profit inside two minutes. Measured on 218 funded copy closes
# that carry the gate's own volume reading (the `volume_gate` substage, which began on 2026-09-08),
# organic m5 volume = the provider's five-minute volume minus the copied buy:
#
#     micro band ($50k-250k mcap)        n    fast TP (<=120s)    pnl
#       organic exactly $0               6           33%       +$102.36
#       organic $1 - $5,000             24            4%       -$153.41
#       organic $5,000 - $100,000       75           36%       +$424.50
#       organic >= $100,000             23           70%       +$457.41
#
# ZERO IS NOT THIN, and that is why this leg has a lower edge. An organic value of exactly $0 means
# the only five-minute volume was the buy we are copying — a coin whose market has not started yet,
# not one whose market has stopped. Those six closes are the second-best cell in the table. A naive
# "under $5k" bar would refuse them along with the thin ones and give back $102.
#
# Both halves of the sample agree on the $1-$5,000 cell: 2026-09-08..12 is 6 closes, 17% fast,
# -$47.69; 2026-09-13..18 is 18 closes, 0% fast, -$105.72. The ratio form of the same rule does NOT
# survive that split (the early half turns +$53), so this leg is expressed in dollars.
#
# SCOPE IS THE MICRO BAND AND THE TWO FUNDED CHAINS WITH EVIDENCE. Nano behaves oppositely (its
# sub-$5k cell is positive in both halves), and chains 56 and 5042 carry one close each in the cell.
# 0 disables. Sample is 24 refused closes over 10 days; review when the cell doubles.
MEMECOIN_REAL_MICRO_MIN_VOLUME_M5_USD = float(
    os.getenv("MEMECOIN_REAL_MICRO_MIN_VOLUME_M5_USD", "0"))
MEMECOIN_REAL_MICRO_MIN_VOLUME_M5_CHAINS = os.getenv(
    "MEMECOIN_REAL_MICRO_MIN_VOLUME_M5_CHAINS", "solana,4663")


def micro_min_volume_m5_usd_for(chain_id, coin_band, env=None):
    """The micro-band m5 DOLLAR floor for one chain. 0 = no floor.

    Scoped by the canonical `coin_band`, so the band edges are not re-derived here. A chain outside
    the configured list reads 0, and so does any band other than micro.
    """
    if str(coin_band or "").strip().lower() != "micro":
        return 0.0
    src = env if env is not None else os.environ
    raw = src.get("MEMECOIN_REAL_MICRO_MIN_VOLUME_M5_CHAINS")
    if raw is None or str(raw).strip() == "":
        raw = MEMECOIN_REAL_MICRO_MIN_VOLUME_M5_CHAINS
    chains = {part.strip() for part in str(raw).split(",") if part.strip()}
    if str(chain_id or "") not in chains:
        return 0.0
    value = src.get("MEMECOIN_REAL_MICRO_MIN_VOLUME_M5_USD") if env is not None else None
    if value is None or str(value).strip() == "":
        value = MEMECOIN_REAL_MICRO_MIN_VOLUME_M5_USD
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0
def real_stop_pct_for_chain(network_id):
    """Per-chain catastrophe stop %. Override via MEMECOIN_REAL_STOP_PCT_<chain>. Robinhood (4663)
    defaults to 0 = DISABLED (operator decision 2026-07-12): its WETH-quoted launch tokens routinely
    dip -30/-50% and recover, so a fixed stop sells normal volatility; exits ride TP / 7-day timecap
    / unquotable / mirror-on-sell instead. Other chains use the global MEMECOIN_REAL_STOP_PCT."""
    ov = os.getenv(f"MEMECOIN_REAL_STOP_PCT_{network_id}")
    if ov not in (None, ""):
        try:
            return float(ov)
        except ValueError:
            pass
    if str(network_id) == "4663":
        return 0.0
    return MEMECOIN_REAL_STOP_PCT
# Positive is mandatory while armed; mirror-on-sell is not a terminal guarantee.
MEMECOIN_REAL_MAX_HOLD_S = float(os.getenv("MEMECOIN_REAL_MAX_HOLD_S", str(MEMECOIN_B_MAX_HOLD_S)))
MEMECOIN_REAL_TP_PCT = float(os.getenv("MEMECOIN_REAL_TP_PCT", "0"))   # FULL take-profit % gain; 0 = off
# CATASTROPHE FLOOR (2026-07-18): absolute max-loss backstop that fires on EVERY funded position
# regardless of a chain default of 0 (Robinhood 4663) OR a per-position override that disabled/widened
# the stop. Root cause it fixes: 0/4 live funded positions had ANY stop (meow -52%, VIM -73% bleeding
# untouched). A tighter operator/chain stop still fires first; this only catches death spirals, so it
# does NOT fight the 4663 "let -30/-50% dips recover" thesis. 0 disables (NOT recommended while armed).
MEMECOIN_REAL_CATASTROPHE_PCT = float(os.getenv("MEMECOIN_REAL_CATASTROPHE_PCT", "80"))

# LP-CATASTROPHIC EXIT, copy lane, Solana (operator 2026-10-21: "yes, if it doesnt slow it down
# for sure"; Alpha's design call: knob ON, fraction 0.5). The signal is pool liquidity
# L = sqrt(coin_reserve * quote_reserve) from COMPLETE same-slot vault pairs on the position's bound
# pool: swaps never lower L in a constant-product pool, an LP withdraw does. Fires when
# 1 - L_now / L_max_since_bind >= the fraction. A malformed fraction disables the rail (never a
# guessed threshold): a wrong number here sells healthy positions.
MEMECOIN_REAL_LP_CATASTROPHIC_EXIT = os.getenv(
    "MEMECOIN_REAL_LP_CATASTROPHIC_EXIT", "1").strip().lower() in {"1", "true", "yes", "on"}


def _lp_catastrophic_frac(raw):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0.0 < value < 1.0 else None


MEMECOIN_REAL_LP_CATASTROPHIC_FRAC = _lp_catastrophic_frac(
    os.getenv("MEMECOIN_REAL_LP_CATASTROPHIC_FRAC", "0.5"))
# Proven wallets use the same 15% round-trip loss cap by default.
# An operator can set a different value for this group.
MEMECOIN_EVM_MAX_TAX_PCT_PROVEN = float(os.getenv("MEMECOIN_EVM_MAX_TAX_PCT_PROVEN", "15"))
# de-risk rail: at +this % gain, sell back just the ENTRY notional (one-shot) and let the rest
# ride on house money. 0 = off. Distinct from TP (which closes the whole position).
MEMECOIN_REAL_DERISK_PCT = float(os.getenv("MEMECOIN_REAL_DERISK_PCT", "0"))
MEMECOIN_REAL_LEGACY_TP_PCT = float(os.getenv("MEMECOIN_REAL_LEGACY_TP_PCT", "60"))
# ROLLOVER / PROFIT-LOCK ENGINE (2026-07-18): a default trailing stop that replaces the set-in-stone
# TP. The trail ARMS once the position's peak crosses +TRAIL_ARM_PCT (so a normal early -30% dip
# that never climbed can't trip it — the 4663 "let dips recover" thesis), then exits when the
# EXECUTABLE mid gives back TRAIL_PCT from the high-water peak. Rides winners with no cap; catches the
# modest-top-then-crash coins (last night's bleed) that used to ride to the -stop. Loss profile
# (do NOT overstate — 2026-07-18): the trail arms at +TRAIL_ARM_PCT, INDEPENDENTLY of the +DERISK
# threshold. A coin that tops between +ARM and +DERISK trails WITHOUT ever de-risking, so on those the
# full stake is at risk — the trail LIMITS the loss (exit at the give-back), it does not protect
# principal. Principal is only recovered on coins that reach +DERISK. Operator override trail_pct still
# wins per-position. 0 = trail off.
MEMECOIN_REAL_TRAIL_PCT = float(os.getenv("MEMECOIN_REAL_TRAIL_PCT", "0"))
# 0 strips the trail from every FUNDED copy-arm contract (paper arms unaffected; stall stays).
MEMECOIN_REAL_COPY_ARM_TRAIL_ENABLED = os.getenv(
    "MEMECOIN_REAL_COPY_ARM_TRAIL_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
# 0 = operator manual mode: funded contracts carry NO tp/derisk/trail/stop/stall — only
# mirror-on-sell, the catastrophe floor, and the timecap remain automatic.
MEMECOIN_REAL_AUTO_EXITS_ENABLED = os.getenv(
    "MEMECOIN_REAL_AUTO_EXITS_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_REAL_TRAIL_ARM_PCT = float(os.getenv("MEMECOIN_REAL_TRAIL_ARM_PCT", "15"))

# Deterministic funded-entry classification. These facts are computed from already-fetched market
# data and local corrected-UTC observations; they add no entry-path network call. Regime state is
# monotonic in risk: unknown/narrow/thin can reduce a clip, never veto or enlarge it.
MEMECOIN_FUNDED_CLASSIFICATION_VERSION = int(os.getenv(
    "MEMECOIN_FUNDED_CLASSIFICATION_VERSION", "1"))
MEMECOIN_FUNDED_REGIME_RISK_SCALE = float(os.getenv(
    "MEMECOIN_FUNDED_REGIME_RISK_SCALE", "0.75"))
MEMECOIN_FUNDED_TAPE_WINDOW_S = float(os.getenv("MEMECOIN_FUNDED_TAPE_WINDOW_S", "900"))
# --- chart truth at the source buy (2026-08-28) --------------------------------------------------
# Yield to the funded lane before anything else. GeckoTerminal issues no API key and limits by IP,
# and the funded mark lane shares that IP — the Jupiter trap. A WINDOWED count of funded GT marks in
# the last 5 minutes, never a cumulative process counter (a counter resets on restart, so one read
# reports hours-since-restart rather than a rate).
MEMECOIN_CHART_TRUTH_YIELD_MARKS = int(os.getenv("MEMECOIN_CHART_TRUTH_YIELD_MARKS", "50"))
# 2500ms holds the job under 24 requests/min against a ~30/min budget.
MEMECOIN_CHART_TRUTH_MIN_GAP_MS = float(os.getenv("MEMECOIN_CHART_TRUTH_MIN_GAP_MS", "2500"))
# Cost scales with distinct POOLS, not events: median 8/hour, p90 18, max 42 measured over 400
# hours since 2026-08-01. START BELOW THE OBSERVED CHOKE, NOT AT THE PUBLISHED ONE (Alpha,
# 2026-08-28): a live 429 arrived after FIVE requests spaced 2,500 ms apart, so 30/min is the
# documented ceiling and not the real one. 20 opens under it; raise only on demonstrated tolerance
# across real hours, and the deferral queue absorbs the difference either way.
MEMECOIN_CHART_TRUTH_MAX_POOLS = int(os.getenv("MEMECOIN_CHART_TRUTH_MAX_POOLS", "20"))

MEMECOIN_FUNDED_TAPE_MIN_OBSERVATIONS = int(os.getenv(
    "MEMECOIN_FUNDED_TAPE_MIN_OBSERVATIONS", "3"))
MEMECOIN_FUNDED_TAPE_MIN_SPAN_S = float(os.getenv(
    "MEMECOIN_FUNDED_TAPE_MIN_SPAN_S", "30"))
MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT = float(os.getenv(
    "MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT", "20"))
MEMECOIN_FUNDED_POST_PEAK_GIVEBACK_PCT = float(os.getenv(
    "MEMECOIN_FUNDED_POST_PEAK_GIVEBACK_PCT", "30"))
MEMECOIN_FUNDED_REGIME_BUCKET_S = float(os.getenv(
    "MEMECOIN_FUNDED_REGIME_BUCKET_S", "900"))
MEMECOIN_FUNDED_REGIME_BASELINE_BUCKETS = int(os.getenv(
    "MEMECOIN_FUNDED_REGIME_BASELINE_BUCKETS", "23"))
MEMECOIN_FUNDED_REGIME_MIN_NONEMPTY_BUCKETS = int(os.getenv(
    "MEMECOIN_FUNDED_REGIME_MIN_NONEMPTY_BUCKETS", "8"))
MEMECOIN_FUNDED_REGIME_MIN_BASELINE_BUYS = int(os.getenv(
    "MEMECOIN_FUNDED_REGIME_MIN_BASELINE_BUYS", "30"))
MEMECOIN_FUNDED_REGIME_MAX_STALENESS_S = float(os.getenv(
    "MEMECOIN_FUNDED_REGIME_MAX_STALENESS_S", "180"))
# Corrected-UTC reservation anchor for the immutable funded contract. The coordinated deploy writes
# this only after the old runner is stopped and zero reservations are rechecked, so every row at or
# after the instant must carry a canonical contract matching its entry attempt and filled position.
MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT = os.getenv(
    "MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT", ""
).strip()
# ROLLOUT COHORT (2026-07-18): selects the COMPLETE default TP/de-risk/trail policy. Positions
# before this aware ISO instant retain LEGACY_TP with de-risk/trail off; positions at/after it use
# the configured rollover rails. Explicit position overrides remain authoritative field-by-field.
MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT = os.getenv(
    "MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT", ""
).strip()
# FIRST-SEEN chase guard (2026-07-16): refuse a real copy when our price is more than this ratio
# above the price at which OUR OWN feed first showed the coin. The per-event chase guard
# (MEMECOIN_REAL_CHASE_MAX_PCT) is blind to copying a LATE buyer — the armed-wallet trigger
# structurally fires on the 3rd+ buyer, and every fresh-coin real Solana loss in the 14d matched
# window entered at +101%..+986% above first-seen (SOLdiers x3 -$66, SCAM -$25, febu -$24).
# 2.0 = allow up to +100% above first sighting. 0 = off. Fail-OPEN on missing data.
MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO = float(os.getenv("MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", "2"))
# The anchor must be RECENT or the guard stops measuring a chase (2026-08-06). `first_seen` was the
# earliest buy our feed ever recorded for the coin, unbounded, so it drifted stale as history grew:
# across the 104 signals it refused in one live window the anchoring event was a MEDIAN 26.7 HOURS
# old (p90 13 days, max 23 days). "Ran 2x since someone bought it yesterday" is appreciation, not
# chasing, and by 2026-08-06 this single gate was refusing 64% of all eligible funded signals while
# the funded book took 2 entries in 7 days.
# 6h keeps every case the guard was actually built on — the documented fresh-coin Solana losses
# entered 25-262 MINUTES after first-seen — and discards anchors far outside that range.
# 0 = unbounded (previous behaviour).
MEMECOIN_REAL_FIRST_SEEN_WINDOW_S = float(os.getenv("MEMECOIN_REAL_FIRST_SEEN_WINDOW_S", "21600"))
# A market snapshot priced from a pair with less liquidity than this is not a price (2026-09-05:
# ESKER was refused "ran 3.4x above first-seen" on a $12.18 pair while the real $22.7k pool sat at
# 1.5x; FORESKIN 08-31 the same on a $216 pair, then +341%). Below the floor the entry gates see
# price/mcap as UNKNOWN and fail open to the source fill price. 0 disables.
MEMECOIN_REAL_SNAPSHOT_MIN_PAIR_LIQUIDITY_USD = float(
    os.getenv("MEMECOIN_REAL_SNAPSHOT_MIN_PAIR_LIQUIDITY_USD", "500"))
# Chains whose OPERATOR mcap targets (sl/tp_target_mcap, exit-target bands) are read on the FDV basis
# (price x total supply), the number GMGN and similar sites show. The provider marketCap is
# circulating on some coins: Hogdog 64977 read 35.3K against a 44.1K FDV, so every target the
# operator set from GMGN sat 20% off. Solana only by default: 4663 has no provider fdv, and the
# EVM chains' totalSupply-vs-GMGN semantics are unverified. Entry gates never read this.
MEMECOIN_REAL_MCAP_FDV_CHAINS = frozenset(
    c.strip() for c in os.getenv("MEMECOIN_REAL_MCAP_FDV_CHAINS", "solana").split(",") if c.strip())
# Time budget for the gas top-up pass's agreeing native reads. The pass runs on the main loop;
# 2026-09-05 two of three pool members timed out and three hedged reads (12s ceiling each, twice
# per pass) held the loop 60-70s until the watchdog restarted the runner. Past the budget the
# balance is unreadable for this pass. 0 disables.
MEMECOIN_EVM_GAS_TOPUP_READ_BUDGET_S = float(os.getenv("MEMECOIN_EVM_GAS_TOPUP_READ_BUDGET_S", "8"))
# RE-ENTRY cooldown (2026-07-16): one real campaign per coin per window. The 07-09 re-entry study
# found NO re-entry edge (one-position-per-coin), yet the real book re-entered SOLdiers 3x at ever
# higher mcaps. Applies only to REAL closed positions; 0 = off.
MEMECOIN_REAL_REENTRY_COOLDOWN_S = float(os.getenv("MEMECOIN_REAL_REENTRY_COOLDOWN_S", "86400"))
# Operator 2026-10-23: desk entries are exempt from the re-entry cooldown above (not from the losing re-entry block).
MEMECOIN_DESK_REENTRY_COOLDOWN_EXEMPT = os.getenv(
    "MEMECOIN_DESK_REENTRY_COOLDOWN_EXEMPT", "1").strip().lower() in {"1", "true", "yes"}
# Operator 2026-10-23 (second order): desk entries are exempt from the losing re-entry block too.
MEMECOIN_DESK_LOSING_REENTRY_EXEMPT = os.getenv(
    "MEMECOIN_DESK_LOSING_REENTRY_EXEMPT", "1").strip().lower() in {"1", "true", "yes"}
# Operator 2026-10-23: a desk mint whose latest position closed with realized_pnl > 0 re-emits when the tape
# prints above that exit price and skips the 300 s universe / 900 s ingest cooldowns (all gates unchanged).
# Cap and window are FROZEN per cohort (policy["winner_reentry"] = {"version", "per_hour", "window_s"}); a
# cohort without it never re-enters. This knob is the kill switch (worker emission + store bypass); _WINDOW_S
# is only the worker's arm-list prefilter, the store enforces the cohort's window.
# Operator 2026-10-23 (watch v22): JUDGE may WAIT on a price trigger / WATCH a coin in cohorts that froze
# policy["standing_orders"]; code fires the entry with no model call. Off: old grammar, no fires, watches expire.
MEMECOIN_DESK_STANDING_ORDERS = os.getenv(
    "MEMECOIN_DESK_STANDING_ORDERS", "1").strip().lower() in {"1", "true", "yes"}
# Operator 2026-10-23: the JUDGE queue serves the newest hard-filter PASS first (a stale ticket ages out as
# signal_aged). 0 = the original oldest-first order.
MEMECOIN_DESK_JUDGE_FRESHEST_FIRST = os.getenv(
    "MEMECOIN_DESK_JUDGE_FRESHEST_FIRST", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_DESK_WINNER_REENTRY = os.getenv(
    "MEMECOIN_DESK_WINNER_REENTRY", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_DESK_WINNER_REENTRY_WINDOW_S = float(os.getenv("MEMECOIN_DESK_WINNER_REENTRY_WINDOW_S", "1800"))
# v23 re-entry as a WATCH (operator 2026-10-23): 1 = the universe worker emits trigger "watch" claims on the first post-close
# trade (only a winner-reentry-v3 cohort accepts them; v1/v2 cohorts refuse them as reentry_trigger). Default 0 because it
# must flip WITH the v23 cohort cutover: on under a v22 cohort it would switch re-entry off there. 0 = the v22 triggers.
# v23 entry gate slot (operator GO 2026-10-23): 1 = every watch-grammar ENTER passes memecoin_desk.entry_gate (pass or convert
# to a WATCH; passes until Gamma's EV table lands) and the vet stage records it. 0 = no gate call, no record (kill knob).
MEMECOIN_DESK_ENTRY_GATE = os.getenv(
    "MEMECOIN_DESK_ENTRY_GATE", "1").strip().lower() in {"1", "true", "yes"}
# P2 (Alpha GO 2026-10-26): a px_hold WAIT on a coin with < 12 volatility bars becomes ONE re-judged WATCH instead of SKIP
# cond_unavailable (v23-facts cohorts only). 0 = SKIP as before (kill knob).
# chase-limit-v1 (Alpha 10-26 option 3): a skipped_chase ENTER in arm B becomes a 2 h standing limit at the verdict price.
# 0 = no new chase orders and every waiting one expires standing_off (kill knob).
# watch-list-v1 (operator 10-26): 1 = an arm-W WATCH coin is re-looked; 0 = every WATCH is FINAL (kill knob).
MEMECOIN_DESK_WATCH_LIST = os.getenv(
    "MEMECOIN_DESK_WATCH_LIST", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_DESK_CHASE_LIMIT = os.getenv(
    "MEMECOIN_DESK_CHASE_LIMIT", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_DESK_PX_HOLD_WATCH = os.getenv(
    "MEMECOIN_DESK_PX_HOLD_WATCH", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_DESK_REENTRY_WATCH = os.getenv(
    "MEMECOIN_DESK_REENTRY_WATCH", "0").strip().lower() in {"1", "true", "yes"}
# CROSS-WALLET RE-ENTRY (operator order 2026-10-08, brain 30822fa2; no study, operator judgment).
# A wallet on MEMECOIN_REAL_FULL_CAP_WALLET_IDS for the entry's chain is refused by the cooldown
# only for its OWN close inside the window, and by the losing block only when the coin's last close
# is its own loss; other wallets' closes and manual closes do not block it. Off = every close blocks
# every wallet, as before. Default ON by operator order; read at import, so a change needs a restart.
# Spec docs/superpowers/specs/2026-10-08-memecoin-cross-wallet-reentry-design.md.
MEMECOIN_REAL_CROSS_WALLET_REENTRY = os.getenv(
    "MEMECOIN_REAL_CROSS_WALLET_REENTRY", "1").strip().lower() in {"1", "true", "yes"}
# ITS OWN WALLET LIST (operator order 2026-10-08 ~13:10Z): the per-chain funded top 5 by percent
# return plus Degen on every chain. Same format as MEMECOIN_REAL_FULL_CAP_WALLET_IDS (bare id = every
# chain, id@chain = that chain). Set and non-empty, it replaces the full-cap list for the lift; unset
# or empty, the full-cap list decides. Kept RAW: memecoin_wallet_caps parses it (malformed entries
# dropped loudly, never widening) and re-reads it from .env every 30 s, so a change is live without
# a restart while the wallet-cap refresh runs. Clip sizing never reads it.
MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS = os.getenv(
    "MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS", "").strip()
# ONE POSITION PER COIN (policy, from the same 07-09 study). 0 lets several funded positions share
# a mint. The INSERT-level dedup race guard in reserve_entry is NOT covered by this and stays on
# either way — two concurrent reservations double-spending into one coin is a correctness fault,
# not a preference. `real_mirror_on_sell` refuses to close a (uid, mint) holding more than one open
# position, so with this off duplicates are unclosable BY MIRROR; follow-30-50-v1 runs mirror off,
# so that path is inert for the current armed roster.
MEMECOIN_REAL_ONE_POSITION_PER_COIN = os.getenv(
    "MEMECOIN_REAL_ONE_POSITION_PER_COIN", "1").strip().lower() in {"1", "true", "yes"}
# TOKEN SAFETY (honeypot / sell-simulation / Token-2022 transfer-fee check). Default ON.
# 0 SKIPS THE CALL ENTIRELY — the latency win is the point, and a result nobody reads is worse than
# no result. With it off a funded entry can buy a token it cannot sell back, which is an
# UNRECOVERABLE loss of the clip rather than a drawdown. No exit rail substitutes: the stop, the
# take-profit and the catastrophe floor all presuppose a sellable bag. Every bypassed entry logs a
# WARNING, which is then the only trace that the check did not run.
MEMECOIN_REAL_TOKEN_SAFETY_ENABLED = os.getenv(
    "MEMECOIN_REAL_TOKEN_SAFETY_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
# Operator-authorized, receipt-verified liquidity removals. Chain-local, immutable, and checked
# before reservation; a risk-cap override does not authorize another purchase of a known rug.
# Evidence: docs/audits/2026-10-04-bsc-stuck-69487-69489.md. No provider read on the fill path.
MEMECOIN_REAL_BLOCKED_TOKENS = frozenset({
    ("56", "0x31fbb624efa7d6f6a3f7b1eb0dfc5cccbb211d10"),
    ("56", "0x4030a670b27fcd711f304f3405908a785c3b848b"),
})
# ROSTER GATE — per-chain funded authority (`real_approved_user_ids`). Default ON. Off means ANY
# wallet the feed reports may open a funded position, not only the armed roster. Money stays
# bounded by MAX_CONCURRENT, MAX_TOTAL_EXPOSURE_USD and the daily loss limit, so this widens WHICH
# signals fill the book rather than the amount at risk.
# It DOES change the rails: follow-30-50-v1 is keyed by handle in MEMECOIN_FOLLOW3050_WALLETS, so a
# wallet absent from that list freezes the BAND DEFAULT contract (funded-nano-v2 and friends, which
# run mirror_on_sell=True with a trail and a de-risk) instead of the 30/70 no-mirror policy.
# Only the ENTRY authority check is switched. Auto-demote and the alert gate still read the roster.
MEMECOIN_REAL_ROSTER_GATE_ENABLED = os.getenv(
    "MEMECOIN_REAL_ROSTER_GATE_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
# V2 ENTRY fallback (2026-07-16, theveeman 0xf056…): on DexScreener-blind chains with the direct
# venue, entry quotes AND the pre-entry sell-route check fall back to the V2 Router02 path (same
# verified router + immutable connectors the real EXIT already uses) when the V3 quote is empty —
# fresh 4663 micros are born on V2-style pools (VIM precedent). Off -> V3-only (old behavior).
MEMECOIN_EVM_V2_ENTRY_ENABLED = os.getenv("MEMECOIN_EVM_V2_ENTRY_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
# V4/Universal-Router fallback (2026-07-17, SPACEHOOD case): fresh 4663 micros launch on V4
# singleton pools against NATIVE ETH. Third rung after V3+V2; quotes via the official V4Quoter
# (addresses verified on-chain against a live trade's PoolManager). Buys unwrap WETH->native
# (pre-transaction) and need NO approvals; sells permit via Permit2 inside the UR tx and wrap
# proceeds back to WETH. Fail-closed at every step.
# Stuck-bag re-entry backoff (2026-09-02). 83 stuck attempts across 5 positions; one ran 65 failing
# ladders in 31 minutes at 8-11s each. Exponential in the consecutive-failure count, capped by the
# ceiling so an unsellable bag that becomes sellable is still reachable. `manual` always pierces.
MEMECOIN_STUCK_BACKOFF_BASE_S = float(os.getenv("MEMECOIN_STUCK_BACKOFF_BASE_S", "30"))
# Ceiling 120s, not 300s: the asymmetry decides it. A retry costs one failed ladder (~10s of RPC,
# and slots sit empty 88% of the time), while missing a liquidity window costs the position, and
# on these coins a window can be seconds. 30/60/120/120... still cuts 65 attempts to ~15 — the
# whole 80% win — while retrying every two minutes instead of every five.
MEMECOIN_STUCK_BACKOFF_CEILING_S = float(os.getenv("MEMECOIN_STUCK_BACKOFF_CEILING_S", "120"))


# --- knobs that were INERT until 2026-09-02 ------------------------------------------------------
# Each of these was read as `getattr(cfg, "NAME", <fallback>)` at its call sites while this module
# never defined the name. So the fallback was not a default that .env could override — it was the
# ONLY reachable value, and setting the variable in .env changed nothing and raised no error. Two
# of them sit on funded surfaces (MEMECOIN_ROUTE_DEGRADATION_PCT, MEMECOIN_COPY_ARM_MIN_CLOSES),
# which is what makes a silent no-op dangerous rather than untidy.
#
# Every default below is EXACTLY the fallback its call sites already used, verified against each
# site, so defining them is a behavioural no-op — none of these appears in .env. Changing a value
# here is a change of live behaviour, not a config tidy-up.
MEMECOIN_CLOSE_CLAIM_HELD_ALERT_S = float(
    os.getenv("MEMECOIN_CLOSE_CLAIM_HELD_ALERT_S", "600"))
MEMECOIN_COPY_ARM_MIN_CLOSES = int(os.getenv("MEMECOIN_COPY_ARM_MIN_CLOSES", "3"))
# Exit market-state windows. The module constants in memecoin_exit_market_state remain the source
# of the numbers; these mirror them so the env can reach them.
MEMECOIN_EXIT_ARRIVAL_WINDOW_S = float(os.getenv("MEMECOIN_EXIT_ARRIVAL_WINDOW_S", "60"))
MEMECOIN_EXIT_DUST_ATOMIC = int(os.getenv("MEMECOIN_EXIT_DUST_ATOMIC", "0"))
MEMECOIN_EXIT_MIN_STRUCTURE_POINTS = int(os.getenv("MEMECOIN_EXIT_MIN_STRUCTURE_POINTS", "5"))
MEMECOIN_EXIT_PIVOT_CONFIRM = int(os.getenv("MEMECOIN_EXIT_PIVOT_CONFIRM", "2"))
# Freshness cap for tape-derived exit state. 180s was never chosen for 4663's ~10 blocks/s; it is
# the value the code fell back to. Retuning it is now possible and is an operator decision.
MEMECOIN_EXIT_STALE_S = float(os.getenv("MEMECOIN_EXIT_STALE_S", "180"))
MEMECOIN_EXIT_VOL_WINDOW_S = float(os.getenv("MEMECOIN_EXIT_VOL_WINDOW_S", "300"))
# None DISABLES each of these checks, which is what the inert fallback did. Keep that: an unset
# value must not silently start gating funded exits.
MEMECOIN_ROUTE_DEGRADATION_PCT = _opt_float("MEMECOIN_ROUTE_DEGRADATION_PCT")
MEMECOIN_STRUCTURE_BREAK_WINDOW_S = _opt_float("MEMECOIN_STRUCTURE_BREAK_WINDOW_S")
MEMECOIN_TICKSTREAM_SKIP_DISARMED_CHAINS = os.getenv(
    "MEMECOIN_TICKSTREAM_SKIP_DISARMED_CHAINS", "1")


def _evm_multihop_bridges(env=None):
    """{chain: (bridge_token, ...)} for the V4 multi-hop rung.

    Structured config, so it cannot come from one scalar read. Per-chain, matching the
    MEMECOIN_EVM_RPC_POOL_URLS_<chain> pattern already used in this module:
        MEMECOIN_EVM_MULTIHOP_BRIDGES_4663=0xaaa...,0xbbb...
    EMPTY IS THE WORKING DEFAULT AND IT IS NOT DARK. This docstring said the opposite until
    2026-09-03 — that an empty mapping makes the rung decline — and the code has never behaved that
    way. `_candidate_bridges` (evm_v4_multihop.py:167) reads:

        if configured:
            yield from configured
            return                 # <- per-coin discovery is SKIPPED
        ...discover the coin's own pool counter-currencies...

    So an empty mapping runs DISCOVERY, per coin, which is what produces `uniswap_v4_multihop` --
    13 funded fills and +$143.76 between 2026-08-31 and 2026-09-02, the most profitable route on
    the 4663 book against uniswap_v4-plain's -$51.31.

    CONFIGURING A BRIDGE HERE TURNS DISCOVERY OFF for that chain and pins the rung to your list.
    That is a NARROWING, not an enablement. Set it only to force a specific bridge deliberately,
    and expect coverage to fall on coins whose pools do not route through it.
    """
    src = env if env is not None else os.environ
    out = {}
    prefix = "MEMECOIN_EVM_MULTIHOP_BRIDGES_"
    for key, raw in src.items():
        if not key.startswith(prefix):
            continue
        chain = key[len(prefix):]
        toks = tuple(t.strip().lower() for t in str(raw).split(",") if t.strip())
        if chain and toks:
            out[chain] = toks
    return out


MEMECOIN_EVM_MULTIHOP_BRIDGES = _evm_multihop_bridges()


# BRIDGE-LEG PROVIDER MEMO (Alpha GO 2026-10-17; evm_v4_multihop._BRIDGE_MEMO). A remembered bridge
# lookup is served while younger than MAX_AGE_S and refreshed every REFRESH_S off the entry path.
# MAX_AGE_S 0 = off (every lookup live, as before); REFRESH_S 0 = no refresh (answers age out).
# Malformed, negative or non-finite = 0.
def _nonneg_seconds(name, default):
    try:
        value = float(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value >= 0 else 0.0


MEMECOIN_EVM_MULTIHOP_BRIDGE_MEMO_MAX_AGE_S = _nonneg_seconds(
    "MEMECOIN_EVM_MULTIHOP_BRIDGE_MEMO_MAX_AGE_S", "180")
MEMECOIN_EVM_MULTIHOP_BRIDGE_REFRESH_S = _nonneg_seconds("MEMECOIN_EVM_MULTIHOP_BRIDGE_REFRESH_S", "60")
# GeckoTerminal and DexScreener read together in _pools_with_depth (same calls, same results); 0 =
# one after the other, as before 2026-10-17.
MEMECOIN_EVM_MULTIHOP_PROVIDERS_CONCURRENT = os.getenv(
    "MEMECOIN_EVM_MULTIHOP_PROVIDERS_CONCURRENT", "1").strip().lower() not in ("0", "false", "off", "no")


# EVM chain-RPC pool (2026-08-13). The funded 4663 lane ran on ONE keyless public endpoint and
# 208 of 210 logged 429s came from evm_wallet._rpc — a rate limit that surfaced as `no_route`, an
# entry refused as though the coin had no pool. chainid.network lists TWO official endpoints for
# Robinhood Chain; both answered eth_chainId=0x1237 with a current block on 2026-08-13.
# auto_free doctrine: chain-owner official endpoints only, identity-checked, health-gated,
# quarantined on failure. No key, no subscription, no operator endpoint.
# Format: MEMECOIN_EVM_RPC_POOL_URLS_<chain>=url1,url2
def _evm_rpc_pool(env=None):
    src = env if env is not None else os.environ
    out = {}
    # 2026-08-30: arrowrpc REPLACED, it is a corpse. It answers HTTP 530 (Cloudflare 1033, origin
    # offline) on every probe, so the 429 rotation and the 0.4s latency hedge had been failing over
    # to nothing for an unknown period while the primary sat in its bimodal 180ms/1.0-1.4s mode.
    # publicnode is chain-owner official, keyless, and measured faster than the primary on every
    # funded method: chainId 74ms vs 157ms, QuoterV2-shaped eth_call 66ms, getBalance 59ms,
    # gasPrice 61ms, getTransactionCount 143ms, address-filtered getLogs 58ms, 16.5-17.4 req/s
    # sustained with zero errors.
    #
    # IT IS NOT A DROP-IN. publicnode REFUSES an unfiltered eth_getLogs (-32701, "Please specify an
    # address in your request") in 58ms while the primary serves the same range in 448ms, and our
    # 4663 tickstream scans the whole chain by design. `evm_wallet._pool_eligible` pins that shape
    # to the primary; without it a fast refusal wins the hedge and thins the tape silently. Do not
    # add an endpoint here without checking it against every request SHAPE we send, not just
    # eth_chainId. Its WSS is deliberately NOT adopted — untested, and publicnode has form for
    # accepting a subscription and then delivering nothing (Solana, 2026-08-07).
    #
    # 16.5 req/s is a measured FLOOR, not a ceiling, and free tier carries no SLA. The primary
    # stays the correctness anchor and the configured first choice.
    #
    # 8453 (2026-09-06). Both defaults are Base's own chain-owner endpoints, the two the free
    # endpoint registry already carries, and both answered eth_chainId=0x2105 with a current block
    # on 2026-09-06 at a 141 ms median over 5 samples. The identity the pool checks comes from the
    # CHAIN KEY of this mapping, so 8453 is proved as 0x2105 by the same code path as 4663.
    # `MEMECOIN_EVM_RPC_URL_8453` is itself one of the two, so the de-duplication below collapses
    # it and the pool is two members, not three.
    for chain, fallbacks in (("4663", ("https://robinhood-rpc.publicnode.com",)),
                             ("8453", ("https://mainnet.base.org",
                                       "https://mainnet-preconf.base.org")),
                             # 56 (2026-09-06). Chain-owner dataseed plus publicnode, both proved
                             # `eth_chainId` 0x38. publicnode is ~80ms against the dataseed's
                             # 234-370ms but returns 403 WITHOUT the runner's user agent -- the same
                             # trap as Base, and `_http` already sends one. `bsc.drpc.org` rate
                             # limits on the first request and `binance.llamarpc.com` does not
                             # resolve; neither may appear here.
                             ("56", ("https://bsc-dataseed.bnbchain.org",
                                     "https://bsc-rpc.publicnode.com")),
                             ("5042", ()),
                             # 1 (BUNKER, 2026-10-07): Infura 429s need an independent read backup.
                             # MEV Blocker passed balance, V4 state, logs and state-override reads;
                             # it does not share the other funded chains' publicnode budget.
                             # Signed chain-1 bytes remain Flashbots-only. An explicit empty
                             # MEMECOIN_EVM_RPC_POOL_URLS_1 restores custody-only reads.
                             ("1", ("https://rpc.mevblocker.io",)),
                             ("57073", ("https://rpc-gel.inkonchain.com",
                                        "https://rpc-qnd.inkonchain.com"))):
        primary = (src.get(f"MEMECOIN_EVM_RPC_URL_{chain}") or "").strip()
        raw = src.get(f"MEMECOIN_EVM_RPC_POOL_URLS_{chain}")
        extra = ([u.strip() for u in raw.split(",") if u.strip()] if raw is not None
                 else list(fallbacks))
        urls = [u for u in [primary, *extra] if u]
        seen, ordered = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u); ordered.append(u)
        if len(ordered) > 1:
            out[chain] = tuple(ordered)
    return out


MEMECOIN_EVM_RPC_POOL_URLS = _evm_rpc_pool()


# MINIMUM COIN AGE FOR A FUNDED ENTRY (2026-09-04, operator). 0 disables it, which is the default.
# Per chain: MEMECOIN_REAL_MIN_COIN_AGE_S_<chain> overrides the global for that chain.
#
# MEASURED over 700 funded closes with a known pair age in the 30 days to 2026-09-04:
#     under 10 min   n=112   -$429.00   median +$0.43   win 52%
#     10 min and up  n=588   +$265.58   median +$0.63   win 52%
# The split is NOT uniform across chains and the honest reading matters:
#     4663   under 10m  n= 10  -$218.55  median -$21.29  win 30%   <- consistent, tiny sample
#     4663   10m and up n= 72  +$448.41  median  +$2.52  win 56%
#     solana under 10m  n=102  -$210.45  median  +$2.93  win 54%   <- POSITIVE median
# On Solana the young bucket wins more often than it loses and its total is negative only because
# of the tail: the five worst trades are -$350.52 of the whole -$429.00. That is a left-tail
# problem an exit rail should own, not proof the entries are bad. So this ships as a knob, per
# chain, and the evidence supports 4663 far more than Solana.
#
# An UNKNOWN age ADMITS. Age is missing on about 9% of funded entries and those carry a positive
# median (+$1.02, 57% win), so refusing on a missing reading would cost more than it saves. This is
# a profitability filter, not a safety gate; safety gates fail closed, this one does not.
# How long a reserved EVM entry must sit unmined before the never-admitted proof may free it.
# 300s: long past any honest broadcast, far short of the 3h20m deadlock of 2026-09-04.
# May the multi-hop rung carry a pool with no provider-reported depth to the round-trip quote?
# The providers lag fresh 4663 coins by minutes and a copy signal arrives in seconds, so the old
# fail-closed answer was really "has DexScreener listed this yet", not "is this pool deep enough".
# The quote of all four legs is the stricter test and it still runs. 0 restores the old behaviour.
# Does a sell simulation that reverts AFTER a successful token pull still refuse the entry?
# `token_pull_ok=true` means the ERC-20 transferFrom worked under the same overrides, which is
# the exact question a honeypot gate asks. A revert past that point is the POOL we quoted, not
# the coin. 0 restores the old always-refuse behaviour.
# RELAY v3 (B, 2026-10-07). GET /requests/v2 retires 2026-11-24 and is already rate-limited;
# v3 carries no order preimage, so a v3 verification returns relay-origin-bound-v1, the orderId
# bound on chain from the origin deposit to the destination calldata. "v2" (default) is today.
# EARLY EVM SIZING FROM THE CACHED BALANCE (HUSH, Alpha GO 2026-10-07; code default off, the .env
# switch is the rollback). The early quote's sizing used to wait for the early balance read (56: p50
# 286 / p90 335 ms), so the early quote launched in 5 of 34 entries. On: the sizing uses the chain's
# cached pre-gate balance when it is from the CURRENT spend generation and at most CACHE_S old, and
# the same worker starts token safety beside the early quote with that clip. take() still binds mint
# + clip + age; the leg reuses only a passed early safety bound to mint + usd + its deadline.
MEMECOIN_EVM_EARLY_CACHED_SIZING = os.getenv(
    "MEMECOIN_EVM_EARLY_CACHED_SIZING", "0").strip().lower() in ("1", "on", "true", "yes")
MEMECOIN_EVM_EARLY_SIZING_CACHE_S = float(os.getenv("MEMECOIN_EVM_EARLY_SIZING_CACHE_S", "300"))
# EARLY CLIP SNAP (Alpha GO 2026-10-17, 9177657c family). The early quote is sized from the cached
# balance and the reservation from the fresh one, so the two clips drift by cents and take() threw
# the quote away as clip_mismatch (4663, 7 days: 6 of 8 mismatches were 1-4 cent drifts; each re-ran
# the ladder serially). A reservation whose fresh clip is at most this percent ABOVE the early clip
# reserves the early clip itself, so the contract, pending notional and spend all equal it and
# take() matches. DOWN ONLY: a clip above the fresh one would break the contract rule approved <=
# base x risk_scale (memecoin_funded_policy.build_funded_contract). 0 = off; malformed or outside
# [0, 1] = off.
def _early_clip_snap_pct(raw):
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else 0.0


MEMECOIN_EVM_EARLY_CLIP_SNAP_PCT = _early_clip_snap_pct(
    os.getenv("MEMECOIN_EVM_EARLY_CLIP_SNAP_PCT", "0.1"))
MEMECOIN_RELAY_REQUESTS_VERSION = ("v3" if os.getenv("MEMECOIN_RELAY_REQUESTS_VERSION", "v2").strip().lower()
                                   == "v3" else "v2")
# RELAY ORIGIN POLL (spec 2026-10-07-relay-origin-overlap-design.md Part 1, operator YES via Alpha).
# The watcher's origin verify re-asks Relay every POLL_S for the three indexing misses inside the
# same ~8 s budget (0 restores 0.1 x 2^n); REQUESTS_MAX_PER_S paces each process's Relay /requests
# calls (4 watchers x 2/s under Relay's 10/s per key; 0 = off); EARLY_GIVEUP stops the wait on a
# settled answer whose depositor can never pass (it only refuses what the 8 s path refuses).
MEMECOIN_RELAY_ORIGIN_POLL_S = float(os.getenv("MEMECOIN_RELAY_ORIGIN_POLL_S", "0.30"))
MEMECOIN_RELAY_REQUESTS_MAX_PER_S = float(os.getenv("MEMECOIN_RELAY_REQUESTS_MAX_PER_S", "2"))
MEMECOIN_RELAY_ORIGIN_EARLY_GIVEUP = os.getenv(
    "MEMECOIN_RELAY_ORIGIN_EARLY_GIVEUP", "1").strip().lower() in ("1", "on", "true", "yes")
# Relay's destination outTx `block` label absent: match on hash, chain and success, with the block
# from our own receipt, and an absent outTx timestamp from our own block header (operator YES via
# Alpha 2026-10-07; spec 2026-10-07-relay-origin-overlap-design.md edge map). Code default off.
MEMECOIN_RELAY_ACCEPT_OUTTX_BLOCK_ABSENT = os.getenv(
    "MEMECOIN_RELAY_ACCEPT_OUTTX_BLOCK_ABSENT", "0").strip().lower() in ("1", "on", "true", "yes")
# The RUNNER's acceptance of relay-origin-bound-v1 as wallet initiation. Default off: turning it
# on widens funded authority (the lost order-commitment checks) and is the operator's call.
MEMECOIN_RELAY_BOUND_PROOF_ACCEPT = os.getenv(
    "MEMECOIN_RELAY_BOUND_PROOF_ACCEPT", "0").strip().lower() in ("1", "on", "true", "yes")
MEMECOIN_EVM_SAFETY_PULL_OK_ADMITS = os.getenv(
    "MEMECOIN_EVM_SAFETY_PULL_OK_ADMITS", "1").strip().lower() in ("1", "on", "true", "yes")
# A REAL BUYER'S ROUND TRIP (spec 2026-10-07 EVM entry safety, section 1). When the synthetic
# sell reverts after the token moved into the sale's destination, admit only if one
# eth_simulateV1 -- buy through the entry quote, approve, sell the received amount through the exit
# quote -- succeeds and returns at least 1 - MEMECOIN_EVM_MAX_TAX_PCT of what it paid. Replaces the
# custom-error allowlist (a V4 hook that rejects every seller returns the same WrappedError as one
# that rejects only a state-override seller). 0 restores the allowlist. Failure path only.
MEMECOIN_EVM_SAFETY_ROUND_TRIP = os.getenv(
    "MEMECOIN_EVM_SAFETY_ROUND_TRIP", "1").strip().lower() in ("1", "on", "true", "yes")
# V2 BUY TAX SHADOW (spec 2026-10-07 EVM entry safety, section 2). "shadow": a PASS on a V2 sell
# route starts a thread that, 60 s later, measures the buy tax on the chain's RESEARCH host only
# (memecoin_v2_buy_tax_shadow) and logs `evm.buy_tax_shadow`. Never gates; "off" skips it.
_V2_BUY_TAX_GATE = os.getenv("MEMECOIN_EVM_V2_BUY_TAX_GATE", "shadow").strip().lower()
MEMECOIN_EVM_V2_BUY_TAX_GATE = _V2_BUY_TAX_GATE if _V2_BUY_TAX_GATE in ("off", "shadow") else "off"

MEMECOIN_EVM_MULTIHOP_ALLOW_UNMEASURED = os.getenv(
    "MEMECOIN_EVM_MULTIHOP_ALLOW_UNMEASURED", "1").strip().lower() in ("1", "on", "true", "yes")

MEMECOIN_EVM_DROPPED_ENTRY_MIN_AGE_S = float(
    os.getenv("MEMECOIN_EVM_DROPPED_ENTRY_MIN_AGE_S", "300") or 300)
# THE FAST LANES' SWAP DEADLINE (2026-10-09, Alpha). The BSC lane also sends privately to 48 Club
# Puissant, which custody cannot see; past this deadline a held copy reverts instead of buying, so it
# must stay well inside the dead proof above (tests/test_memecoin_lane_deadline_guard.py). Not an
# env knob on purpose: a larger value would reopen that hole.
LANE_SWAP_DEADLINE_S = 60

MEMECOIN_REAL_MIN_COIN_AGE_S = float(os.getenv("MEMECOIN_REAL_MIN_COIN_AGE_S", "0") or 0)


def inband_max_turnover_m5_for(chain_id, coin_band, env=None):
    """The IN-BAND m5 turnover CEILING for one chain and coin band. 0 = no ceiling.

    Key shape follows the existing per-chain-per-band convention (`_nano_chain_pct`):
    `MEMECOIN_REAL_<CHAIN>_<BAND>_INBAND_MAX_TURNOVER_M5`, else the global
    `MEMECOIN_REAL_INBAND_MAX_TURNOVER_M5`. The chain segment is upper-cased, which is identity for
    the numeric chains the convention grew up on and removes the ambiguity for named ones.

    WIDENED FOR SOLANA NANO ONLY, operator decision 2026-09-08. Measured on closed funded positions,
    turnover = volume_m5 / effective_mcap, split by mcap band because pooling hid it:

        solana nano (<$50k)                   solana micro ($50k-250k)
          0.10-0.50  n=64  +$413.09  13.8%     0.10-0.50  n=76  +$153.49   4.2%
          0.50-1.00  n=33  +$203.87  13.0%     0.50-1.00  n=40   +$46.31   2.4%
          1.00-2.00  n=30    -$8.61  -0.7%     1.00-2.00  n=45    +$2.71   0.1%

    On nano the 0.50-1.00 band returns what the band we deliberately target returns (13.0% vs
    13.8%) at a HIGHER win rate (72.7% vs 57.8%), and the losses start at 1.00. It survives the
    two-best-day removal (+$27.93 over n=25) that the chain-pooled version failed (-$20.93); 31 of
    its 33 trades sit in a nine-day August window, so it is thin outside that.

    `assumed_nano` (mcap unknown) deliberately does NOT match `nano` and keeps the global ceiling:
    the evidence is about coins we could MEASURE as nano, and widening on an assumption is the
    wrong direction. A malformed value falls back to the global rather than to 0, because 0 here
    means NO CEILING — a config slip must not remove the gate.
    """
    src = env if env is not None else os.environ
    band = str(coin_band or "").strip().upper()
    chain = str(chain_id or "").strip().upper()

    def _num(raw):
        if raw is None or str(raw).strip() == "":
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value >= 0 else None

    if band and chain:
        specific = _num(src.get(
            "MEMECOIN_REAL_%s_%s_INBAND_MAX_TURNOVER_M5" % (chain, band)))
        if specific is not None:
            return specific
    # THE GLOBAL COMES FROM THE MODULE CONSTANT, not from os.environ, unless the caller injected an
    # explicit mapping. The constant is already initialised from os.getenv at import, so `.env` is
    # honoured either way — but reading the live environment here made the answer depend on whether
    # some OTHER test had called load_dotenv, and it silently beat `monkeypatch.setattr(cfg, ...)`,
    # which is how every caller in this repo overrides a threshold. Six unrelated gate tests failed
    # that way the moment a per-band key existed in `.env`.
    if env is not None:
        injected = _num(src.get("MEMECOIN_REAL_INBAND_MAX_TURNOVER_M5"))
        if injected is not None:
            return injected
    fallback = _num(MEMECOIN_REAL_INBAND_MAX_TURNOVER_M5)
    return fallback if fallback is not None else 0.0


def inband_min_turnover_m5_for_chain(chain_id, env=None):
    """The IN-BAND m5 turnover floor for one chain. 0 = no floor.

    PER CHAIN BECAUSE THE BOOK IS PER CHAIN. Measured 2026-09-08 on 635 closed funded positions
    carrying `volume_m5` and `effective_mcap`, turnover = volume_m5 / effective_mcap:

        chain 4663                          chain solana
          <0.02        n= 30  -$307.68        <0.01        n= 25   +$86.11   <- profitable
          0.02-0.10    n= 36  +$366.15        0.01-0.10    n= 73   -$93.85
          0.10-0.50    n= 51  +$433.10        0.10-0.50    n=168  +$608.86

    On 4663 the 0.02-0.10 band is profitable, so a 0.10 floor throws away $366 while a 0.02 floor
    refuses the $307 of losses below it. On Solana the same 0.02 floor would admit a band that
    loses, and its lowest bucket is profitable, so a low floor there is the harmful direction. One
    global number cannot express both.

    Same shape as `min_coin_age_s_for_chain`: a per-chain key wins, else the global key, else the
    module default. A malformed value yields 0.0 rather than a floor nobody chose.
    """
    src = env if env is not None else os.environ
    raw = src.get("MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5_%s" % str(chain_id))
    if raw is None or str(raw).strip() == "":
        # Same rule as `inband_max_turnover_m5_for`: the global falls back to the MODULE CONSTANT
        # for an ambient read, so `monkeypatch.setattr(cfg, ...)` works and the answer does not
        # depend on whether another test loaded `.env` into os.environ.
        raw = (src.get("MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5") if env is not None else None)
        if raw is None or str(raw).strip() == "":
            raw = MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def min_coin_age_s_for_chain(chain_id, env=None):
    """Seconds a coin must already have existed before a funded entry is allowed. 0 = no floor."""
    src = env if env is not None else os.environ
    raw = src.get("MEMECOIN_REAL_MIN_COIN_AGE_S_%s" % str(chain_id))
    if raw is None or str(raw).strip() == "":
        raw = src.get("MEMECOIN_REAL_MIN_COIN_AGE_S", MEMECOIN_REAL_MIN_COIN_AGE_S)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def _wallet_seconds_map(raw):
    """`chain:uid=seconds,...` -> {"chain:uid": seconds}; the uid may itself contain ':' (evm:,
    pump:). Malformed, negative or non-finite entries are dropped, never guessed."""
    out = {}
    for part in (raw or "").split(","):
        key, sep, value = part.partition("=")
        key = key.strip().lower()
        if not sep or ":" not in key:
            continue
        try:
            seconds = float(value)
        except ValueError:
            continue
        if math.isfinite(seconds) and seconds >= 0:
            out[key] = seconds
    return out


# PER-WALLET COIN-AGE FLOOR (2026-10-07, china_sniper_1: a BSC launch sniper buys coins seconds
# old, and the 120 s floor refuses every one). `chain:uid=seconds`, read at call time; it overrides
# the chain floor for that wallet only. Empty = every wallet keeps its chain's floor.
def min_coin_age_s_for(chain_id, source_user_id=None, env=None):
    """The coin-age floor for this wallet on this chain: its override, else the chain's."""
    src = env if env is not None else os.environ
    if source_user_id:
        per_wallet = _wallet_seconds_map(src.get("MEMECOIN_REAL_MIN_COIN_AGE_S_BY_WALLET", ""))
        hit = per_wallet.get(("%s:%s" % (chain_id, source_user_id)).lower())
        if hit is not None:
            return hit
    else:
        # DESK / OPERATOR ENTRIES carry no source wallet (2026-10-26, Gamma's desk ENTER age check: crashes sit under
        # 60 s, the 60-120 s verdicts did best). Set = this floor for every entry without a source; unset = the chain's.
        raw = src.get("MEMECOIN_DESK_MIN_COIN_AGE_S")
        if raw is not None and str(raw).strip() != "":
            try:
                value = float(raw)
            except ValueError:
                value = None
            if value is not None and math.isfinite(value) and value >= 0:
                return value
    # The chain floor's own call shape when no env is given (its parity mirrors stub it without
    # `env`, tests/ink/test_admission_parity.py).
    return min_coin_age_s_for_chain(chain_id) if env is None else min_coin_age_s_for_chain(chain_id, env=env)


# PER-WALLET ABSOLUTE SOURCE-BUY FLOOR (2026-10-07, china_sniper_1 buys 0.1-0.2 BNB, $77-154, all
# under the $200 MEMECOIN_REAL_MIN_SOURCE_BUY_USD). `chain:uid=usd`, read at call time; replaces the
# absolute floor for that wallet only (tolerance, ratio and ceiling unchanged). Empty = today.
def source_buy_floor_override(chain_id, source_user_id, env=None):
    """This wallet's absolute source-buy floor on this chain, or None (no override)."""
    if not source_user_id:
        return None
    src = env if env is not None else os.environ
    per_wallet = _wallet_seconds_map(src.get("MEMECOIN_REAL_MIN_SOURCE_BUY_USD_BY_WALLET", ""))
    return per_wallet.get(("%s:%s" % (chain_id, source_user_id)).lower())


def min_source_buy_usd_for(chain_id, source_user_id=None, env=None):
    hit = source_buy_floor_override(chain_id, source_user_id, env=env)
    if hit is not None:
        return hit
    try:
        value = float(MEMECOIN_REAL_MIN_SOURCE_BUY_USD or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, value) if math.isfinite(value) else 0.0


# ON-CHAIN SELL MIRROR, PER WALLET (2026-10-07, operator: china_sniper_1 "mirror sells (whatever
# happens first)"). `chain:uid,...`: the EVM watcher's decoded sales (onchain_evm_sell_shadow) of
# these wallets exit our open position in that coin when they sold >= MIRROR_SIZE_MIN_FRACTION of
# their holding. Read at call time. Empty (default) = off, the shadow stays shadow.
# `chain:*` (2026-10-17, Alpha GO): every wallet armed on that chain when the mirror reads it
# (memecoin_onchain_lane.mirror_wallets), added to the listed uids; the same mirror rules per sale.
def onchain_sell_mirror_scope(chain_id, env=None):
    """{"uids", "all_armed", "malformed"} for `chain_id`. An entry with no chain or no wallet is
    malformed: ignored, and counted over the whole value."""
    src = env if env is not None else os.environ
    uids, all_armed, malformed = set(), False, 0
    for part in (src.get("MEMECOIN_REAL_ONCHAIN_SELL_MIRROR_WALLETS", "") or "").split(","):
        if not part.strip():
            continue
        chain, sep, uid = part.strip().partition(":")
        chain, uid = chain.strip(), uid.strip()
        if not sep or not chain or not uid:
            malformed += 1
            continue
        if chain != str(chain_id):
            continue
        if uid == "*":
            all_armed = True
        else:
            uids.add(uid.lower() if uid.lower().startswith("evm:") else uid)
    return {"uids": frozenset(uids), "all_armed": all_armed, "malformed": malformed}


def onchain_sell_mirror_wallets(chain_id, env=None):
    """The listed uids only (a `chain:*` entry is resolved against the armed roster by the lane)."""
    return onchain_sell_mirror_scope(chain_id, env=env)["uids"]


def _evm_archive_rpc_pool(env=None):
    """Optional archive-only read endpoints. No public fallback claims archive support."""
    src = env if env is not None else os.environ
    out = {}
    # 56: the operator's dedicated BSC endpoint for the volume-death poll feed (2026-10-01).
    # 1: an archive for the old bridge-pool keys Infura cannot serve (BUNKER 2026-10-07); empty
    # until the operator names one.
    for chain in ("4663", "5042", "56", "1"):
        raw = src.get(f"MEMECOIN_EVM_ARCHIVE_RPC_URLS_{chain}", "")
        urls = tuple(dict.fromkeys(u.strip() for u in raw.split(",") if u.strip()))
        if urls:
            out[chain] = urls
    return out


MEMECOIN_EVM_ARCHIVE_RPC_POOL_URLS = _evm_archive_rpc_pool()


def evm_archive_rpc_urls_for_chain(chain_id):
    """Configured archive endpoints for historical reads on one EVM chain."""
    return MEMECOIN_EVM_ARCHIVE_RPC_POOL_URLS.get(str(chain_id), ())


# SIGNAL-KEYED V4 PoolKey WARMING (2026-09-02). A stored key answers in ~10ms; a live resolve
# costs ~840ms and fails under load. Of 475 funded 4663 attempts that reserved capital in 48h, 64
# filled and 153 died on "no quote". Off by default: it spends read calls, and the operator turns
# it on deliberately. MAX_PER_MIN bounds a signal burst — 4663 sees ~830 signals a day.
# Relay on the BUY side. Off: relay is an aggregator carrying a bridge spread, and over 14 days
# relay-routed 4663 entries ran -25.1% median against +2.0% for direct-pool entries, losing $214.73
# across 11 fills while 45 direct fills made $64.82. Relay stays available for EXITS unconditionally
# — when every direct rung misses on a sell it is the only way a position closes.
MEMECOIN_EVM_RELAY_ENTRY_ENABLED = os.getenv(
    "MEMECOIN_EVM_RELAY_ENTRY_ENABLED", "0").strip().lower() in {"1", "true", "yes"}

# KYBERSWAP AGGREGATOR RUNG (Beta 02813ece, 2026-09-22; defined here 2026-09-22 22:10Z). The rung
# read both names through getattr fallbacks, so `.env` could not reach them; the values below are
# exactly those fallbacks (off, chain 4663 only). Turning it on is a deliberate `.env` change.
MEMECOIN_EVM_KYBER_AGG_ENABLED = os.getenv(
    "MEMECOIN_EVM_KYBER_AGG_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_EVM_KYBER_AGG_CHAINS = tuple(
    c.strip() for c in os.getenv("MEMECOIN_EVM_KYBER_AGG_CHAINS", "4663").split(",") if c.strip())

MEMECOIN_EVM_KEY_PREWARM_ENABLED = os.getenv(
    "MEMECOIN_EVM_KEY_PREWARM_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_EVM_KEY_PREWARM_MAX_PER_MIN = int(
    os.getenv("MEMECOIN_EVM_KEY_PREWARM_MAX_PER_MIN", "6"))
MEMECOIN_EVM_KEY_PREWARM_MAX_PENDING = int(
    os.getenv("MEMECOIN_EVM_KEY_PREWARM_MAX_PENDING", "64"))
MEMECOIN_EVM_KEY_PREWARM_TTL_S = float(os.getenv("MEMECOIN_EVM_KEY_PREWARM_TTL_S", "900"))

MEMECOIN_EVM_V4_ENABLED = os.getenv("MEMECOIN_EVM_V4_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
# MICRO LADDER (2026-07-16): dedicated exit policy for micro-cap real positions (assumed-micro
# or resolved mcap < MICRO_MCAP_USD). Evidence: micro winners peak in 15-60min, ~half of hit20
# winners round-trip to entry, yet 30% of the 250-500K band runs past +100% — so: recover the
# entry stake at +DERISK_PCT (house money), NO full TP (keep the tail), exit the remainder at
# TRAIL_PCT giveback from peak (executable-confirmed), position-scoped stop -SL_PCT (covers 4663
# micros without touching the chain-wide no-stop decision). Timecap stays global (scalp_c lesson).
# MAX_CONCURRENT bounds how many of these riskiest entries can be open at once (0 = off).
# Pre-registered read: >=30 closes, PnL + drop-top-1 vs the tp-ab arms.
MEMECOIN_REAL_MICRO_POLICY_ENABLED = os.getenv("MEMECOIN_REAL_MICRO_POLICY_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_REAL_MICRO_MCAP_USD = float(os.getenv("MEMECOIN_REAL_MICRO_MCAP_USD", "250000"))
MEMECOIN_REAL_MICRO_DERISK_PCT = float(os.getenv("MEMECOIN_REAL_MICRO_DERISK_PCT", "50"))
MEMECOIN_REAL_MICRO_TRAIL_PCT = float(os.getenv("MEMECOIN_REAL_MICRO_TRAIL_PCT", "50"))
MEMECOIN_REAL_MICRO_SL_PCT = float(os.getenv("MEMECOIN_REAL_MICRO_SL_PCT", "60"))
MEMECOIN_REAL_MICRO_MAX_CONCURRENT = int(os.getenv("MEMECOIN_REAL_MICRO_MAX_CONCURRENT", "4"))
# v2 partial de-risk (2026-07-23, operator-directed at n=29): nano/micro's first profit rail is a
# FIXED-FRACTION partial at +PARTIAL_DERISK_PCT% (bank FRACTION of the bag, ride the rest under a
# PARTIAL_TRAIL_PCT% giveback trail). Replaces the all-or-nothing recover-entry rail that sat above
# 5 of 10 stopped positions' executable peaks (11984 +48.3% -> rode to -66%). Study:
# scripts/research/memecoin_partial_derisk_counterfactual.py (+$85-92 on the 29-close cohort).
MEMECOIN_REAL_PARTIAL_DERISK_PCT = float(os.getenv("MEMECOIN_REAL_PARTIAL_DERISK_PCT", "20"))
MEMECOIN_REAL_PARTIAL_DERISK_FRACTION = float(os.getenv("MEMECOIN_REAL_PARTIAL_DERISK_FRACTION", "0.5"))
MEMECOIN_REAL_PARTIAL_TRAIL_PCT = float(os.getenv("MEMECOIN_REAL_PARTIAL_TRAIL_PCT", "30"))
# NANO band (2026-07-17, operator: "sub 50k, 50-150k … no one-fit-all"): coins under
# NANO_MCAP_USD — and assumed-micro entries, whose triple-unresolvable mcap means minutes-old,
# i.e. the smallest class — get earlier derisk + tighter trail than the micro band. The thinnest
# pools round-trip fastest. LAUNCH GUESSES; recalibrate per band at >=30 closes each.
MEMECOIN_REAL_NANO_MCAP_USD = float(os.getenv("MEMECOIN_REAL_NANO_MCAP_USD", "50000"))
MEMECOIN_REAL_NANO_DERISK_PCT = float(os.getenv("MEMECOIN_REAL_NANO_DERISK_PCT", "40"))
MEMECOIN_REAL_NANO_TRAIL_PCT = float(os.getenv("MEMECOIN_REAL_NANO_TRAIL_PCT", "40"))
MEMECOIN_REAL_NANO_SL_PCT = float(os.getenv("MEMECOIN_REAL_NANO_SL_PCT", "60"))
# MID band 1-2M keeps its scalp exit policy. The consensus value remains a diagnostic field.
MEMECOIN_REAL_MID_LO_MCAP_USD = float(os.getenv("MEMECOIN_REAL_MID_LO_MCAP_USD", "1000000"))
MEMECOIN_REAL_MID_HI_MCAP_USD = float(os.getenv("MEMECOIN_REAL_MID_HI_MCAP_USD", "2000000"))
MEMECOIN_REAL_MID_DERISK_PCT = float(os.getenv("MEMECOIN_REAL_MID_DERISK_PCT", "30"))
MEMECOIN_REAL_MID_TRAIL_PCT = float(os.getenv("MEMECOIN_REAL_MID_TRAIL_PCT", "35"))
MEMECOIN_REAL_MID_SL_PCT = float(os.getenv("MEMECOIN_REAL_MID_SL_PCT", "60"))
# consensus gate: entries at/above this mcap require >= MIN_BUYERS distinct prior buyers within
# 60min (prior_buyers_60m). 0 = gate off. Fail-OPEN when the count is unreadable (data absence
# must never block — the honeypot/mcap lesson).
MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP = float(os.getenv("MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP", "0"))
MEMECOIN_REAL_CONSENSUS_MIN_BUYERS = int(os.getenv("MEMECOIN_REAL_CONSENSUS_MIN_BUYERS", "1"))
# Fast-lane per-wallet spacing, decoupled from the analyzer's 2s WAF spacing: 1.0s over ~30
# armed wallets ≈ 33s sweep (was ~63s) — halves the median catch age toward the <60s +EV band.
MEMECOIN_FOLLOW_FAST_SLEEP_S = float(os.getenv("MEMECOIN_FOLLOW_FAST_SLEEP_S", "1.0"))
# ASSUME-MICRO (2026-07-16): when a Solana buy's mcap is unresolvable by ALL THREE sources
# (event field, same-mint feed history, live DexScreener), the coin is a minutes-old micro by
# construction — big caps always have a DS pair or feed history. Fail-closed here refused ~16
# armed buys/day, all in the sub-$1M pocket (58% hit20) — the 07-11 bug class one resolver
# layer deeper. The entry is bounded by token safety, the micro-tier impact cap, clip size,
# and stop/mirror. Solana-only; EVM keeps the conservative skip.
#
# The name is stale. This flag only lifts the market-cap resolution gate. The classifier assigns
# `assumed_nano`, which follows the nano admission rule.
MEMECOIN_REAL_MCAP_ASSUME_MICRO = os.getenv("MEMECOIN_REAL_MCAP_ASSUME_MICRO", "1").strip().lower() in {"1", "true", "yes"}


def solana_band_mcap_basis(raw):
    """`dexscreener` (default) or `onchain`; anything else is the default."""
    value = str(raw or "").strip().lower()
    return value if value in ("dexscreener", "onchain") else "dexscreener"


# SOLANA BAND MCAP, STAGE 3 (2026-09-27, operator via Alpha, option B). When the event mcap and the
# same-mint history are both empty, the band's mcap came from DexScreener, whose price lagged the
# source's fill by ~14% at the median (6 of 52 such bands would change). `onchain` prices that stage
# at the source's own fill x the mint's on-chain supply, from the token-safety mint read moved ahead
# of the band (never a second read); DexScreener stays as the labelled fallback. Default off: with
# `dexscreener`, gate order, reads and bands are exactly as before. The operator flips it.
MEMECOIN_SOLANA_BAND_MCAP_BASIS = solana_band_mcap_basis(
    os.getenv("MEMECOIN_SOLANA_BAND_MCAP_BASIS"))
# E1 ON SOLANA (2026-09-28, operator "no dex anywhere" via Alpha; spec
# 2026-09-28-memecoin-e1-solana-own-mcap-design.md). 1 = today: stage 3 may still take DexScreener's
# market_cap when the on-chain basis cannot price a coin (3 of 551 Solana classifications since
# 2026-09-27 09:00Z). 0 = never on Solana; the route stage (our own entry quote x mint supply) prices
# it instead.
# Only an explicit off value turns it off (Gamma L3): a typo keeps today's behaviour.
MEMECOIN_SOLANA_DEX_MCAP_FALLBACK = os.getenv(
    "MEMECOIN_SOLANA_DEX_MCAP_FALLBACK", "1").strip().lower() not in {"0", "false", "no", "off"}


def solana_mcap_unresolved(raw):
    """`assume` (default, today: band assumed_nano, the ceiling does not apply) or `refuse`
    ("mcap_unresolved"). Anything else is `refuse`: a malformed value must never skip the ceiling."""
    value = str(raw if raw is not None else "assume").strip().lower()
    return value if value in ("assume", "refuse") else "refuse"


# What a Solana coin that NO own stage priced does. With the DexScreener fallback off, `assume` would
# let a coin DexScreener used to price skip the $250k ceiling, so activation sets both together:
# MEMECOIN_SOLANA_DEX_MCAP_FALLBACK=0 and MEMECOIN_SOLANA_MCAP_UNRESOLVED=refuse.
MEMECOIN_SOLANA_MCAP_UNRESOLVED = solana_mcap_unresolved(os.getenv("MEMECOIN_SOLANA_MCAP_UNRESOLVED"))


def evm_key_proof_chains(raw):
    """Explicit EVM key-proof targets, including Arc; duplicates and unknown chains dropped."""
    out = []
    for part in str(raw or "").split(","):
        chain = part.strip()
        if chain in ("4663", "56", "8453", "5042") and chain not in out:
            out.append(chain)
    return tuple(out)


# evm-key-v1 ON AN ARMED J7 CHAIN (Alpha, 2026-10-01; default empty = Ink only, as before). A wallet
# armed on a chain where J7 never relayed one of its buys has no claim there, so no proof and no
# watching (figaro on 56, with direct proofs of the same key address on 4663 and 8453). A chain listed
# here also accepts evm-key-v1: a direct proof elsewhere plus a key-controlled account on the chain.
# It never replaces the wallet's own row on the chain. Rollback: empty it.
MEMECOIN_EVM_KEY_PROOF_CHAINS = evm_key_proof_chains(os.getenv("MEMECOIN_EVM_KEY_PROOF_CHAINS"))


def mcap_history_priced(raw):
    """"1"/"true"/"yes"/"on" turns it on; anything else, including unset, is off."""
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


# THE SOLANA ADMISSION CEILING TESTS A PRICED MCAP (523c9968, 2026-10-01, Alpha ruling, narrowed;
# activation is the operator's yes). The funded gate's stage-2 history is today's rule: three readings
# agreeing within a third, else the median mcap with the price ignored -- which passed fmpumpguy's
# A71Uf3jw at $131,269 under a $250,000 ceiling while the source paid ~4x that reading's price. On a
# Solana decision where history resolved, the gate always records a PRICED shadow beside it (a
# proven pump mint at source price x 1e9, else the readings' implied supply at the source price).
# "1": the ceiling ALONE also refuses on that shadow. Band, floor, entry_mcap on every chain and the
# exit targets keep today's value whatever this says (exit changes go to paper first).
# docs/audits/2026-10-01-solana-mcap-gate-two-lanes.md.
MEMECOIN_MCAP_HISTORY_PRICED = mcap_history_priced(os.getenv("MEMECOIN_MCAP_HISTORY_PRICED"))


def solana_mcap_knob_problem(fallback=None, unresolved=None):
    """The warning for the one unsafe pair (fallback off, unresolved=assume), else None."""
    fallback = MEMECOIN_SOLANA_DEX_MCAP_FALLBACK if fallback is None else fallback
    unresolved = MEMECOIN_SOLANA_MCAP_UNRESOLVED if unresolved is None else unresolved
    if not fallback and unresolved == "assume":
        return ("MEMECOIN_SOLANA_DEX_MCAP_FALLBACK=0 with MEMECOIN_SOLANA_MCAP_UNRESOLVED=assume "
                "WIDENS admission: a Solana coin no own stage prices skips the mcap ceiling; "
                "set MEMECOIN_SOLANA_MCAP_UNRESOLVED=refuse")
    return None


if solana_mcap_knob_problem():
    logging.getLogger("memecoin").warning(solana_mcap_knob_problem())
# real-entry mcap ceiling (2026-07-11): skip REAL copies of buys on coins above this market cap.
# Exploratory but overwhelming: 0 of 1,120 matured signal paths on >$30M coins hit +20% within
# the capture window (vs 35-42% for sub-$1M) — the front-run-the-herd premise needs a coin small
# enough to move. Paper arms unaffected (A/B integrity). 0 = off. Manual /real/buy bypasses.
MEMECOIN_REAL_MAX_MCAP_USD = float(os.getenv("MEMECOIN_REAL_MAX_MCAP_USD", "30000000"))
# Entry mcap FLOOR (2026-08-25, operator). 0 = off. A coin under this is refused `under_floor`.
# This is a COST decision, not a performance one. Realized PnL cannot support a floor -- sub-15k
# reads -16.1% cutting at 08-19 and +1.7% cutting at 08-21, nine trades apart. Entry COST is what
# is stable: median route/display +36.7% under 30k against +9.8% over 250k.
# Cost of the floor, recorded so it is not forgotten: sub-30k fills were 41% winners and supplied
# 6 of the 22 fills that ever returned +50% or more.
MEMECOIN_REAL_MIN_MCAP_USD = float(os.getenv("MEMECOIN_REAL_MIN_MCAP_USD", "0"))

# TPU sidecar RPC (2026-08-25). Empty = inherit the last funded pool URL, which is the previous
# behaviour and what caused this: the sidecar took rpc_urls[-1], the same key custody uses, and the
# two collided on that key's REQUEST RATE. The sidecar died on HTTP 429 from getEpochSchedule and
# crash-looped (restart_count 9, reason socket_unavailable), so every funded entry silently lost the
# fast submission path and fell back to RPC.
# Note this is HTTP 429 (rate limit), NOT the -32429 "max usage reached" that means credits are
# gone. The 2026-08-21 note in .env already recorded the shape: "custody+TPU were on it with no
# rotation".
# The sidecar is KEYLESS by design and only reads the leader schedule, which is infrequent and
# latency-tolerant -- the actual send is QUIC, not RPC. Default: keyless public read endpoint.
# Approved isolated TPU exception: separate same-provider/region HTTP+WS read credential and
# capacity, mainnet-genesis/no-send verified; never reuse custody/watch keys or their quota.
MEMECOIN_TPU_RPC_URL = os.getenv("MEMECOIN_TPU_RPC_URL", "").strip()

# FUNDED HISTORY WINDOW (2026-08-25, operator: "live funded path should read nothing outside from
# last week"). Days. 0 = unbounded, the previous behaviour.
# The system before 2026-08-19 is not the system running now -- the chase gate was dead until then
# and most current filters landed 08-23/24 -- so a funded gate that reads a 6-week history is
# deciding today on evidence from a configuration that no longer exists.
# BEHAVIOUR CHANGE THIS CAUSES, stated because it is not obvious: losing_real_coin_ids is
# PERMANENT per coin by design (memecoin_risk.py, "does not expire... which is the operator's
# intent"). Bounding it converts that permanent block into an expiring one. A coin that beat us
# eight days ago becomes enterable again.
MEMECOIN_REAL_HISTORY_DAYS = float(os.getenv("MEMECOIN_REAL_HISTORY_DAYS", "0"))

# LOSING RE-ENTRY BLOCK WINDOW (2026-08-25, operator: "just 24hrs"). Hours. 0 = fall back to
# MEMECOIN_REAL_HISTORY_DAYS; if that is also 0 the block is unbounded, its original form.
# The block was PERMANENT per coin by design (see losing_real_coin_ids). The operator has scoped it
# to a day: a coin that beat us yesterday is enterable again today.
# This is deliberately its OWN knob rather than sharing the 7-day history window, because the two
# answer different questions -- the history window is about which era of evidence a gate may read,
# this is about how long one coin stays on the naughty list.
MEMECOIN_REAL_LOSING_REENTRY_HOURS = float(os.getenv("MEMECOIN_REAL_LOSING_REENTRY_HOURS", "0"))

# QUIET WINDOW, UTC (operator, 2026-09-11). Real roster closes over 14 days by UTC block: 00-06
# n=33 -$309 WR 0.30; 06-12 +$40; 12-18 +$883; 18-24 -$127. "start-end" hours, end exclusive,
# wrap-around allowed ("22-4"). Empty = off. Automatic ENTRIES stand down inside the window;
# exits and manual entries are untouched. It reads the corrected clock, not the host clock.
MEMECOIN_REAL_QUIET_HOURS_UTC = (os.getenv("MEMECOIN_REAL_QUIET_HOURS_UTC", "") or "").strip()
# How long an undecided source alert waits for its entry decision before it is sent on its own
# (2026-09-11). Decisions reach the notifier at p50 0.3 s, p90 8.5 s; within the hold only the
# result message goes out. Delay budget for the alert, not a trading input.
MEMECOIN_TG_SOURCE_HOLD_S = float(os.getenv("MEMECOIN_TG_SOURCE_HOLD_S", "4") or 0)
# A submitted entry that is not yet confirmed pages as "confirmation unknown" only after this many
# seconds (2026-09-11: every buy paged it 1-9 s before BOUGHT). Genuinely stuck ones still page.
MEMECOIN_TG_UNRESOLVED_GRACE_S = float(os.getenv("MEMECOIN_TG_UNRESOLVED_GRACE_S", "30") or 0)
# REFUSAL NOTICES COLLAPSE TO ONE PER COIN PER WINDOW (2026-09-22). Gamma measured 1,927 of 3,027
# Telegram messages in seven days as entry REFUSED notices over 630 coins, one coin 38 times -- 64%
# of the channel, and none of it priced (docs/audits/2026-09-22-research-lanes-and-alerts-review.md).
# A collapsed refusal is still recorded as a transition, so Activity keeps every one. 0 disables.
MEMECOIN_TG_REFUSAL_COLLAPSE_S = float(os.getenv("MEMECOIN_TG_REFUSAL_COLLAPSE_S", "3600") or 0)
# The "EVALUATING" source card. 50 were sent in the same seven days and no outcome was attached to
# any of them; the result message that follows carries the same coin. Off by default. CAUTION: the
# card is the only live writer of `memecoin_alert_latency` (through `_source_intent`), so with it off
# that instrument stops filling. Set to 1 to restore the card.
MEMECOIN_TG_SOURCE_CARDS = os.getenv("MEMECOIN_TG_SOURCE_CARDS", "0").strip().lower() in {"1", "true", "yes"}
# Bounded wait for the other sell venues when the first answer values the bag below 1/20 of its
# entry cost (the dust-pool signature, SUIT 50977). Just above Relay's measured p99 of 3.4 s.
MEMECOIN_EVM_SELL_DUST_WAIT_S = float(os.getenv("MEMECOIN_EVM_SELL_DUST_WAIT_S", "3.5") or 0)
# Anchor-checked EVM exit (spec 2026-10-01-memecoin-evm-anchor-checked-exit-design.md): a sell
# quote below MIN_PCT of the position's sticky anchor is held, at most HOLD_S and >= 2 attempts,
# then sold. Both are policy values; the chain switch (memecoin_evm_exit_anchor_chains) is off.
MEMECOIN_EVM_EXIT_ANCHOR_MIN_PCT = float(os.getenv("MEMECOIN_EVM_EXIT_ANCHOR_MIN_PCT", "50") or 0)
MEMECOIN_EVM_EXIT_ANCHOR_HOLD_S = float(os.getenv("MEMECOIN_EVM_EXIT_ANCHOR_HOLD_S", "6") or 0)
# Hard ceiling on one EVM venue-ladder run (buy or sell). Measured rung p99 (4663, buy side):
# v3 6.7 s, v4 4.9 s, relay 3.4 s; only v4_multihop's tail is longer (41 s). A hung rung froze the
# main loop >85 s three times on 2026-09-11 and got the runner killed mid-entry.
MEMECOIN_EVM_LADDER_WAIT_S = float(os.getenv("MEMECOIN_EVM_LADDER_WAIT_S", "8") or 8)
# How long a measured BUY ladder keeps waiting for slower rungs after its first measured hit
# (2026-09-23). A launchpad/flap curve hit ends the wait at once. 4663 since 09-20 (n=34): the
# wait after the first hit was p50 3.0 s (v4 first), p90 5.0 s (curve first); v4_multihop answers
# at p50 2.0 s, relay at 3.1 s. 0 disables the grace (first measured hit decides).
MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S = float(
    os.getenv("MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S", "2.0") or 0)
_LADDER_GRACE_WARNED = set()


def evm_ladder_grace_after_hit_s_for(chain):
    """`MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_<chain>`, else the global grace (2026-09-28).

    One global grace cannot serve every chain: on 8453 the replay found 1.0 s worse on 0 of 15
    ladders while saving 0.5 s on each, and on 4663 the same 1.0 s is worse on 10 of 306
    (docs/superpowers/specs/2026-09-27-robinhood-speed-and-shared-subsecond-prices.md, section 4).
    Unset or blank means the global. 0 means no grace on that chain (the first measured hit
    decides), the same meaning as the global. A malformed, negative or non-finite value means the
    global, never 0: a typo must not end the wait on a chain. It is logged once per value. The
    value is read on every ladder run, an environment read with no I/O.
    """
    raw = os.getenv("MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_%s" % str(chain).strip())
    fallback = float(MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S or 0.0)
    if raw is None or not str(raw).strip():
        return fallback
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or not math.isfinite(value) or value < 0:
        if (str(chain), raw) not in _LADDER_GRACE_WARNED:        # a duplicate line at worst
            _LADDER_GRACE_WARNED.add((str(chain), raw))
            logging.getLogger(__name__).warning(
                "MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_%s=%r is not a grace in seconds; using the "
                "global %.2f s", chain, raw, fallback)
        return fallback
    return value
# HOW MUCH OF THE LADDER DEADLINE THE OPTIONAL RELAY REVERSE NEEDS BEFORE IT IS STARTED AT ALL
# (2026-09-23). `timeout=` on `urlopen` bounds each SOCKET OPERATION, not the total elapsed time,
# so a connect-plus-headers phase and a body read can each finish inside the timeout and still
# cross the ladder deadline together -- losing a forward quote that had already answered. Rather
# than abandon an in-flight request (a thread-per-timeout abandons exactly the entries already
# under time pressure), the reverse is simply not started when what is left of the deadline cannot
# hold it, and the hit is admitted unmeasured, which is the existing degrade path.
# 3.2 s is the measured p90 of the WHOLE relay rung over 30 days (n=179: p50 1,836 ms, p90
# 3,134 ms, p99 4,351 ms, max 4,531 ms), so it CONTAINS the forward leg and overstates the reverse
# alone -- deliberately, since overstating only skips an optional measurement. At the 8 s ladder
# this binds past the observed p99; `ladder_deadline` was recorded on the relay rung 0 times in
# that window. 0 disables the floor and restores the previous behaviour.
MEMECOIN_EVM_RELAY_REVERSE_MIN_BUDGET_S = float(
    os.getenv("MEMECOIN_EVM_RELAY_REVERSE_MIN_BUDGET_S", "3.2") or 0)
# HOW OLD A QUOTE MAY BE WHEN THE PREFLIGHT SIMULATES IT (2026-09-22). `row` 0xb30fcc1d passed its
# preflight at 21:40:21.35 and the chain reverted the transaction two seconds later, 115,763 gas
# for nothing. Half of that staleness was the lagging custody node (fixed by routing reads to the
# freshest member); the other half is ours -- the ladder takes 1.8-3.0 s and the minimum is
# computed at the top of it. Past this age the winning rung is re-asked before simulating.
# 750 ms is well under the observed ladder time and well over a single quote's latency, so a fresh
# quote costs one round trip and a stale one costs a reverted transaction.
MEMECOIN_EVM_PREFLIGHT_MAX_QUOTE_AGE_MS = float(
    os.getenv("MEMECOIN_EVM_PREFLIGHT_MAX_QUOTE_AGE_MS", "750") or 750)
# Chains whose V2 path search also routes through the coin's own pair token (read from its pair
# list, cached 10 min). BSC flap.sh coins pair with tokenized stocks and other tokens, not WBNB.
MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS = frozenset(
    x.strip() for x in os.getenv("MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS", "56").split(",") if x.strip())

# MEASUREMENT-ONLY PROBE of the PancakeSwap V3 -> V2 mixed hop (2026-09-22). OFF by default, and
# it can never admit an entry: two legs are two transactions, so a fill on the first with a failure
# on the second is an open position in a coin the ladder refused. It runs only where the ladder
# already found NO route, so it adds no latency to a signal that can still become an entry, and it
# answers one question: how many refused chain-56 signals this path would have addressed, at what
# round-trip cost. Atomic execution through the SmartRouter is a separate change.
MEMECOIN_EVM_MIXED_HOP_PROBE_ENABLED = os.getenv(
    "MEMECOIN_EVM_MIXED_HOP_PROBE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# ATOMIC EXECUTION of the mixed hop, separate from the measurement probe above and also OFF.
# The probe only counts what the path WOULD have quoted; this one signs. Both legs go in one
# SmartRouter multicall and the exit is simulated at the whole would-be balance BEFORE the entry
# is built, because a coin reachable only through a V3 bridge cannot be sold by the single-venue
# paths — there is no fallback, so an unproven exit must mean no entry.
MEMECOIN_EVM_MIXED_HOP_EXECUTION_ENABLED = os.getenv(
    "MEMECOIN_EVM_MIXED_HOP_EXECUTION_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# V3 BRIDGE-TOKEN RUNG (2026-10-02, Alpha; PI 9493dd86). Chains whose BUYS also quote
# quote -> X -> coin through the coin's own pair tokens X on V3 (the PCS V3 router and spender the
# v3 rung already uses), measured on the same pools reversed. Empty = off. A position the rung
# opened exits on its stored bridge whatever this says.
MEMECOIN_EVM_V3_BRIDGE_CHAINS = frozenset(
    x.strip() for x in os.getenv("MEMECOIN_EVM_V3_BRIDGE_CHAINS", "").split(",") if x.strip())
# The bridge tokens X the rung may route through, per chain, lowercase (Gamma MED, 2026-10-02):
# X is a second counterparty, so only tokens whose X/WBNB V3 pool was proven on chain enter, as
# constants with their evidence. An unlisted X is a named miss (`bridge_x_not_allowlisted:<X>`).
#
# 56 evidence: one read-only probe on bsc-dataseed, 2026-10-02 ~17:0xZ (Alpha-approved). symbol(),
# decimals(), PCS V3 factory 0x0bfbcf9f..1865 getPool, and QuoterV2 WBNB->X impact at $0.08 and $77
# (BNB ~$773) on the deepest tier. Listed: majors plus PI's ZEC. Every listed X moved 0.00% at $77.
#   USDC  0x8ac76a51  dec 18  tier 100   pool 0xf2688fb5..12c4
#   USD1  0x8d0d000e  dec 18  tier 500   pool 0x4a321860..72a9
#   BTCB  0x7130d2a1  dec 18  tier 100   pool 0x62edaf2a..013b
#   ETH   0x2170ed08  dec 18  tier 100   pool 0x62fcb3c1..3a1e
#   Cake  0x0e09fabb  dec 18  tier 500   pool 0xafb2da14..6c56
#   ZEC   0x1ba42e51  dec 18  tier 2500  pool 0xbd0dbc58..fd7c   (PI 9493dd86's bridge)
# Left OFF (Alpha, 2026-10-02): the tokenized-stock quote tokens BNCB 0x4902c5eb, QQQB 0x205812cd and
# FXIon 0x9b8e987e. They are deep (<= 0.07% at $77) and recur in 5-6 funded routes each, but stock-token
# issuers are the likeliest to pause (market hours, compliance), the exact exit-stranding case; their
# misses show as bridge_x_not_allowlisted. Also OFF: FDUSD (deepest V3 WBNB pool, tier 500, moved 7.77% at $77: thin for a clip); BABAB
# 0x4ef9d306 (its only V3 WBNB pool, tier 2500, gives no quote); DOGE 0xba2ae424 (one route only).
# USDT is the configured connector, which the standard scan already prices.
MEMECOIN_EVM_V3_BRIDGE_TOKENS = {
    "56": frozenset({
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",   # USDC
        "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d",   # USD1
        "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c",   # BTCB
        "0x2170ed0880ac9a755fd29b2688956bd959f933f8",   # ETH
        "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82",   # Cake
        "0x1ba42e5193dfa8b03d15dd1b86a3113bbbef8eeb",   # ZEC
    }),
}


def memecoin_evm_v3_bridge_tokens(chain_id):
    """The vetted bridge tokens for one chain (lowercase), empty when none."""
    return frozenset(MEMECOIN_EVM_V3_BRIDGE_TOKENS.get(str(chain_id)) or ())
MEMECOIN_EVM_MIXED_HOP_PROBE_CHAINS = frozenset(
    x.strip() for x in os.getenv("MEMECOIN_EVM_MIXED_HOP_PROBE_CHAINS", "56").split(",")
    if x.strip())

# Pre-entry flow read window, seconds. Must be >= the pool-prewarm cron cadence or a pre-warmed
# pool ages out between refreshes and every read returns no_swaps. Widening it is how the lane's
# RPC cost comes down: a 30-minute window holds coverage at a 10-minute refresh instead of a
# 2-minute one, which is 5x fewer calls for the same answer, and a longer sample gives a steadier
# turnover figure rather than a worse one.
MEMECOIN_PRE_ENTRY_FLOW_WINDOW_S = float(os.getenv("MEMECOIN_PRE_ENTRY_FLOW_WINDOW_S", "1800"))

# Out-of-regime mcap rejection, USD. 0 = off. A reading above this is treated as UNRESOLVABLE, not
# as a ceiling breach, so it fails closed regardless of the strategy ceiling — including when the
# ceiling is off entirely.
# NOT PURELY A CORRUPTION FILTER, and the distinction matters. Sorted above $1B the population is
# mostly REAL: $1,532,930,574,596 is cbBTC (Coinbase Wrapped BTC — that is approximately Bitcoin's
# market cap and a CORRECT reading), ~$292B is a canonical Base asset, ~$95B is WBNB. Genuine
# corruption starts around 1e12 and above ($9.3e51 on one 0x address). So this bound excludes
# ASSETS FAR OUTSIDE THE MEMECOIN REGIME plus real corruption, and both are things we do not want
# to copy-trade.
# DO NOT TIGHTEN IT THINKING YOU ARE SHARPENING A DATA CHECK. Counts: 52 attempts above $1B, 38
# above $10B, 10 above $100B. At $1B you would be making a strategy decision against 52 real
# attempts while believing you were filtering bad data. 0 of 301 fills carrying a decision mcap sit
# above $10B, so at this level it cannot refuse anything we have ever actually bought.
MEMECOIN_REAL_ABSURD_MCAP_USD = float(os.getenv("MEMECOIN_REAL_ABSURD_MCAP_USD", "0"))

# Historical in-band ceiling. The funded above-boundary call cannot enforce this setting.
# Traces report the configured ratio and an effective ratio of zero. Retained for legacy callers.
MEMECOIN_REAL_MAX_TURNOVER_M5 = float(os.getenv("MEMECOIN_REAL_MAX_TURNOVER_M5", "0"))

# This boundary selects the funded m5 policy. Zero disables the above-band m5 branch.
# Configured h1 floors apply independently of this boundary.
MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD = float(
    os.getenv("MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", "0"))

# These source identities use an earlier m5 boundary. The setting uses stable source
# IDs. A handle change must not change funded admission. An empty list keeps the global boundary.
MEMECOIN_REAL_STRICT_VOLUME_SOURCE_IDS = frozenset(
    x.strip() for x in os.getenv("MEMECOIN_REAL_STRICT_VOLUME_SOURCE_IDS", "").split(",")
    if x.strip())
MEMECOIN_REAL_STRICT_VOLUME_MIN_MCAP_USD = float(
    os.getenv("MEMECOIN_REAL_STRICT_VOLUME_MIN_MCAP_USD", "0"))

# Reduced m5 turnover floor ABOVE the band bound (2026-08-26, operator). 0 = off (the pre-existing
# silence). The fitted 10% cannot extrapolate to big coins, but "no moving-NOW check at all" let
# three dead giants in on stale h1 in one day. This is the floor's own ratio for that regime.
MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND = float(
    os.getenv("MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND", "0"))

# 1 = a floor-scoped entry with UNREADABLE m5 volume is refused (fail-closed, same posture as the
# mcap gate). 0 = the m5 leg fails open on missing data (chase-guard convention, the old behaviour).
MEMECOIN_REAL_M5_REQUIRED = int(os.getenv("MEMECOIN_REAL_M5_REQUIRED", "0"))

# ARMED volume-death exit (2026-08-26, operator). 0 = shadow only (the old state). When armed, a
# cohort position (opened_at >= the cutoff) whose own tape has been silent EXIT_S seconds on a
# HEALTHY stream exits as vol_death. Blind tape freezes the rail, never fires it. Replaces the
# 13-min timecap as the working staleness exit for the cohort — the timecap stays as a long
# catastrophe backstop via MEMECOIN_REAL_MAX_HOLD_S (frozen per position at entry).
# House boolean parse: a bare int() here was the ONE flag in this file that would crash the
# runner at import on "true" (Beta pre-deploy review, 2026-08-26).
MEMECOIN_VOLDEATH_ARMED = os.getenv("MEMECOIN_VOLDEATH_ARMED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_VOLDEATH_EXIT_S = float(os.getenv("MEMECOIN_VOLDEATH_EXIT_S", "30"))
MEMECOIN_VOLDEATH_ARM_MIN_OPENED_AT = os.getenv("MEMECOIN_VOLDEATH_ARM_MIN_OPENED_AT", "")
MEMECOIN_ROBINHOOD_VOLUME_WS_URL = os.getenv(
    "MEMECOIN_ROBINHOOD_VOLUME_WS_URL", "wss://robinhood-rpc.publicnode.com")
# SECOND WITNESS (2026-08-28, operator, position 44813). Swap silence alone cannot tell a dead coin
# from a dropped websocket: 44813 exited vol_death while the pool's own reserves moved 45% inside
# the window the swap clock called silent. ON by default; this exists so a rollback is an env edit
# rather than a code change on the funded exit path.
# TP CONTINUATION CAPTURE (2026-08-29). One-second sampling for 60s after a funded take-profit,
# into the research database, for the confirmation-window question.
#
# ITS OWN FLAG, deliberately. `MEMECOIN_POST_TP_FOLLOWUP_ENABLED` already exists and drives a
# DIFFERENT collector — four fixed horizons into the funded ledger for the laddered-TP question.
# Sharing one flag would make a single env edit start two collectors answering two questions at
# twice the research quota, and I did exactly that for one deploy before it was caught.
MEMECOIN_TP_CONTINUATION_CAPTURE_ENABLED = os.getenv(
    "MEMECOIN_TP_CONTINUATION_CAPTURE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_VOLDEATH_STATE_WITNESS = os.getenv(
    "MEMECOIN_VOLDEATH_STATE_WITNESS", "1").strip().lower() in {"1", "true", "yes"}
# EXECUTABLE-MOVE VETO (2026-09-03, operator, position 49013). Percent range of the exact
# whole-bag quote across the silence window, above which volume-death refuses to fire. Both venue
# witnesses answer `still` about a pool nobody trades when the binding is wrong, and 3 of the 7
# funded 4663 vol_death closes sold a coin whose executable price moved 16-43% inside the window
# the rail called silent. Veto only: it can freeze this rail, never fire it. 0 disables the veto.
# The default separates the one true-dead EVM case measured (1.7%, ETH/USD drift on a static pool)
# from the three live ones; it is fitted to n=7 and should move as fires accumulate.
MEMECOIN_VOLDEATH_MOVE_VETO_PCT = float(
    os.getenv("MEMECOIN_VOLDEATH_MOVE_VETO_PCT", "5.0"))
# Page once when a funded position's vol_death rail stays BLIND (on-chain proof pending or
# unavailable, or its entry route unknown) this many seconds; 0 = off. Alert only
# (memecoin_alerts.note_voldeath_coverage, 2026-09-26).
MEMECOIN_VOLDEATH_BLIND_PAGE_S = max(0.0, float(
    os.getenv("MEMECOIN_VOLDEATH_BLIND_PAGE_S", "120")))
# Exact quotes can veto a silence exit when the executable price moves.
# Flat quotes cannot replace missing pool-state evidence.
MEMECOIN_VOLDEATH_EXACT_WITNESS = os.getenv(
    "MEMECOIN_VOLDEATH_EXACT_WITNESS", "1").strip().lower() in {"1", "true", "yes"}

# The MINT-WIDE witness before a Solana vol_death fire (2026-09-28): after the bound pool's vault
# proves quiet, one bounded read of the mint's newest signatures vetoes with `mint_moving` when
# another pool traded inside the window. DEFAULT OFF (Alpha, 2026-09-28): activated by an explicit
# .env line on the operator's yes at a flat book, like this week's other money-path knobs.
MEMECOIN_VOLDEATH_MINT_WITNESS = os.getenv(
    "MEMECOIN_VOLDEATH_MINT_WITNESS", "0").strip().lower() in {"1", "true", "yes"}
# A veto episode longer than this past exit_s gives up and fires (Lambda F1): a mint that trades
# every <= 27 s on another pool would otherwise hold the rail until the 4 h max_hold.
MEMECOIN_VOLDEATH_MINT_VETO_MAX_S = float(os.getenv("MEMECOIN_VOLDEATH_MINT_VETO_MAX_S", "120"))

# ENTRY-RADAR RATINGS (2026-09-22, operator: archive the radar). The radar crons stopped on
# 2026-08-21, but the funded runner kept writing one rating per terminal entry attempt through
# `_record_radar_best_effort`: 51,960 rows, the newest written the day this flag was added. The
# reader was the Launch radar sub-tab, and that tab is removed. The write is terminal telemetry, so
# turning it off cannot change a funded result. The table and the module stay for the history.
MEMECOIN_ENTRY_RADAR_RATINGS_ENABLED = os.getenv(
    "MEMECOIN_ENTRY_RADAR_RATINGS_ENABLED", "0").strip().lower() in {"1", "true", "yes"}

# DROPPED-CLOSE RELEASE AGE (2026-09-03, PRC 49252). How old a `not_found` close must be before the
# recovery may treat "no node holds it" as final and release the claim. The re-offer path already
# calls anything younger than 20s "too_young … still inside the window where it may simply
# confirm"; 60s is three of those windows and ~30 block times on a 2s-block chain, so a sell that
# landed just before the read has had ample time to surface a receipt on every configured endpoint.
# Raising it only delays a release; lowering it risks releasing a claim on a sell that lands.
MEMECOIN_EVM_DROPPED_CLOSE_MIN_AGE_S = float(
    os.getenv("MEMECOIN_EVM_DROPPED_CLOSE_MIN_AGE_S", "60"))

# PIPELINED-UNWRAP RECEIPT WINDOW (2026-09-03, operator, attempt 7204b30c). 4663 submits the WETH
# unwrap ahead of its nonce+1 swap. Nonce order decides execution order, NOT txpool admission: the
# node prices the swap's `gas * price + value` against committed state, so a swap broadcast before
# its unwrap mines is rejected for insufficient funds. Seconds to wait for the unwrap's receipt
# before the swap is allowed to reference its ETH. This wait replaces a rejected entry rather than
# slowing a working one, so it is cheap against a feed lag measured in tens of seconds.
MEMECOIN_EVM_UNWRAP_CONFIRM_S = float(
    os.getenv("MEMECOIN_EVM_UNWRAP_CONFIRM_S", "3.0"))

# Signal-volume exclusion (2026-08-26, operator): the floor legs judge volume MINUS the copied
# wallet's own buy — the signal cannot be the volume that admits it. Ceiling always reads raw.
# TIMECAP LIFT (2026-08-26, operator). Off = every family keeps its shipped policy_id and its
# 780s cap. On = the families in TIMECAP_LIFT_POLICY_IDS are written under NEW policy ids with
# max_hold_s=14400, and new contracts freeze their own volume-death threshold. Never hand-write
# this while the runner is live: activation goes through
# `scripts/deploy_memecoin_runtime.sh <sha> --activate-timecap-lift`, which stops the runner first.
MEMECOIN_TIMECAP_LIFT_ENABLED = (
    os.getenv("MEMECOIN_TIMECAP_LIFT_ENABLED", "0").strip().lower() in {"1", "true", "yes"})

MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED = (
    os.getenv("MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", "0").strip().lower() in {"1", "true", "yes"})

# Mcap ceiling for the m5 turnover CEILING leg alone, USD. Unset/0 = the ceiling stays on the shared
# band bound above, which is the behaviour before this knob existed.
# WHY THE CEILING NEEDS ITS OWN BOUND (operator, 2026-08-25): the two legs answer different
# questions and therefore stop being true at different sizes. The FLOOR asks "is anything trading
# here at all", which stays meaningful at every mcap. The CEILING asks "is the whole float churning",
# which is a rug signature on a $10k coin and an ordinary busy session on a $224k one. Lowering the
# shared bound to scope the ceiling would silence the floor at the same time, and the operator wants
# the floor to keep working at every coin size.
# TRIGGER: happyseb ($224.1k mcap, m5 $543,894 = 2.43x) was refused as a float dump. The measured
# split behind the ceiling is n=4 under $150k at 0% win (avg -51.8%) against n=2 at/above $150k at
# 50% win (avg +15.9%) -- every breach that actually cost money was a micro-cap. Both cells are tiny;
# this bound is the operator's call on where the mechanism stops applying, not a fitted threshold.
MEMECOIN_REAL_TURNOVER_CEILING_MAX_MCAP_USD = float(
    os.getenv("MEMECOIN_REAL_TURNOVER_CEILING_MAX_MCAP_USD", "0"))


def losing_reentry_since_iso(now_iso=None):
    """ISO cutoff for the losing-re-entry block, or None when unbounded.

    Hours knob first, then the day-scale history window, then unbounded. Built in Python for the
    same reason as real_history_since_iso: SQLite datetime('now', ...) renders a space separator
    and ' ' sorts below 'T', so a string comparison silently reaches further back than asked."""
    hours = MEMECOIN_REAL_LOSING_REENTRY_HOURS
    if not hours or hours <= 0:
        return real_history_since_iso(now_iso)
    import datetime as _dt
    if now_iso:
        base = _dt.datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    else:
        base = _dt.datetime.now(_dt.timezone.utc)
    return (base - _dt.timedelta(hours=float(hours))).isoformat()


def real_history_since_iso(now_iso=None):
    """ISO cutoff for every funded historical read, or None when the window is off.

    Built in Python from an ISO stamp, never with SQLite datetime('now','-N days'): that renders a
    SPACE separator while our stamps use 'T', and ' ' < 'T', so a string comparison silently reaches
    further back than asked (finding-sqlite-datetime-now-space-separator-2026-08-23: a '24h' window
    really covered ~40h and overcounted by 69%)."""
    days = MEMECOIN_REAL_HISTORY_DAYS
    if not days or days <= 0:
        return None
    import datetime as _dt
    if now_iso:
        base = _dt.datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    else:
        base = _dt.datetime.now(_dt.timezone.utc)
    return (base - _dt.timedelta(days=float(days))).isoformat()
# REVIVAL GUARD (2026-08-17, operator): refuse a real entry when the coin's LAST-HOUR traded
# volume is under this floor — a priced coin with no recent tape is a dead coin someone is trying
# to revive, and our buy would be a large share of its only liquidity event. Fail-OPEN when the
# volume field is absent (a fresh pool DexScreener has not indexed yet has no volume data; the
# staleness/chase/band gates still apply — same convention as the chase guard). 0 = off.
MEMECOIN_REAL_MIN_VOLUME_H1_USD = float(os.getenv("MEMECOIN_REAL_MIN_VOLUME_H1_USD", "0"))
# Operator 2026-10-03: remove the Robinhood hourly dollar-volume veto (SPORE 530c00dd).
# Unset/blank inherits the global floor; explicit 0 disables this leg on 4663 only.
MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663 = (
    float(os.environ["MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663"])
    if os.getenv("MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663", "").strip() else None)
# Chains where NO volume gate refuses a funded entry (operator 2026-09-29: "remove the volume
# filters for bsc and base or fix them"). Canonical chain ids, comma-separated. The would-be refusal
# is still recorded (trace substage volume_gate_off_chain). Empty = every chain gated as before.
MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS = frozenset(
    c.strip() for c in os.getenv("MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS", "").split(",") if c.strip())
# DEAD-COIN GATE (operator via Alpha 2026-10-07, SWCH 71539: a 104-day-old coin with $0.49 of hourly
# volume). Refuse when the coin is at least MIN_AGE_S old AND its organic hourly volume (the source's
# own buy excluded, as the volume gate does) is under MAX_H1_USD, on the chains in DEAD_COIN_CHAINS
# only (default empty = record only). Unknown age or volume never refuses. Every chain records the
# would-be decision as trace substage dead_coin_gate. Small n (Alpha's read: age>=7d and h1<$100
# n=4, -8.2%); activation is the operator's.
MEMECOIN_REAL_DEAD_COIN_MIN_AGE_S = float(os.getenv("MEMECOIN_REAL_DEAD_COIN_MIN_AGE_S", "604800"))
MEMECOIN_REAL_DEAD_COIN_MAX_H1_USD = float(os.getenv("MEMECOIN_REAL_DEAD_COIN_MAX_H1_USD", "1000"))
MEMECOIN_REAL_DEAD_COIN_CHAINS = frozenset(
    c.strip() for c in os.getenv("MEMECOIN_REAL_DEAD_COIN_CHAINS", "").split(",") if c.strip())
# THE 4663 HOURLY DOLLAR FLOOR SCALES WITH THE COIN (2026-09-28, operator via Alpha). A known
# effective market cap caps the flat floor at this fraction of it: floor = min(flat, r x mcap). It
# only loosens small coins; an unknown market cap keeps the flat floor. 0 (default) = flat, as before.
MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_4663 = float(
    os.getenv("MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_4663", "0"))
# The short-horizon Solana nano/micro policy may omit both hourly volume legs.
# Keep the old gate by default; deployment must explicitly opt out.
MEMECOIN_REAL_SOLANA_SMALL_H1_FLOOR_ENABLED = os.getenv(
    "MEMECOIN_REAL_SOLANA_SMALL_H1_FLOOR_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
# SOURCE-BUY CONVICTION FLOOR. Require the copied wallet's buy to reach this fraction of the
# resolved entry market cap. This is the source trade's size, never our clip. The hard fallback
# applies only when every existing entry-mcap resolver fails. Both values use zero to disable the
# relevant branch.
MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO = float(
    os.getenv("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", "0.005"))
MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD = float(
    os.getenv("MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", "100"))
# PER-BAND SOURCE-BUY FLOOR (2026-09-12, operator "improve the per band logic"). Comma list of
# "band=ratio" or "chain:band=ratio"; the chain-specific entry wins. Bands are the funded coin
# bands (nano < $50k, micro < $250k, small < $1M, mid < $2M, by resolved entry mcap). A band not
# listed uses MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO. Empty = one ratio for every band, as before.
# Funded basis: nano made money at every ratio traded (0.8-5%, both chains, n=94); Solana micro
# lost at 0.8-2% (n=34 since 09-04, -$198) and made money at >=2% (n=12, +$307).
def _parse_source_buy_band_ratios(raw):
    out = {}
    for part in (raw or "").split(","):
        key, sep, value = part.partition("=")
        if not sep or not key.strip():
            continue
        try:
            ratio = float(value)
        except ValueError:
            continue
        if math.isfinite(ratio) and ratio >= 0:
            out[key.strip().lower()] = ratio
    return out


# BOUNDARY TOLERANCE ON THE SOURCE-BUY FLOOR (2026-09-24, operator). 0xdedrater bought $1,486.29
# of 和平熊猫 at 0.797% against a 0.800% bar of $1,493 and was refused BY $7. A hard floor with no
# tolerance at the boundary measures rounding, not conviction, and the coins either side of it are
# the same trade.
#
# UNITS, and this is the trap the name has to survive: this is a percent OF THE FLOOR, not
# percentage points of the mcap ratio. At 2.0 a $1,493 bar admits $1,463; it does NOT turn the
# 0.800% ratio into 0.784%.
#
# ONE-WAY LOOSER BY CONSTRUCTION. A tolerance can only admit what the old rule refused, never
# refuse what it admitted -- provided it cannot go negative, which is why it is clamped at 0 rather
# than trusted. 0 reproduces the old rule exactly and is the rollback.
# DEFAULT 0 = today's rule, byte for byte. The value the operator approved is written into `.env`
# and applied by a deploy restart, so the code carries no behaviour change on its own.
MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT_MAX = 50.0


def _source_buy_floor_tolerance_pct(raw):
    """Clamped at BOTH ends. Below 0 is 0 and above 50 is 50, and the ceiling is the point.

    Negative would silently TIGHTEN a funded entry gate -- the one direction this knob must never
    move. A fat-fingered 200 would take 100% off the floor and switch the rule off entirely while
    still reading as "a tolerance", so the ceiling refuses it loudly instead of obeying it.
    """
    try:
        value = float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    if value > MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT_MAX:
        logging.getLogger("memecoin").warning(
            "MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT=%s exceeds the %.0f%% ceiling and is "
            "being read as %.0f%%; this knob is a percent OF THE FLOOR, not percentage points of "
            "the mcap ratio", raw, MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT_MAX,
            MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT_MAX)
        return MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT_MAX
    return max(0.0, value)


MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT = _source_buy_floor_tolerance_pct(
    os.getenv("MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT", "0"))
# ONE SOURCE-SIZE BASIS ACROSS LANES (2026-09-28, Alpha GO; census
# docs/audits/2026-09-28-fast-lane-coverage-census.md). J7 reports what the trader PAID; the
# on-chain and slot lanes decoded the pool leg NET of pump fees (gross/net p50 1.0125 on 281 Solana
# detections), so Roveryaps' $199 buys read $196.55 on the fast lanes and failed a $197 floor J7
# passed. "gross" (default) judges what the trader paid into the swap, pool fees included; "net"
# restores the old pool-leg size. The source PRICE is not affected.
MEMECOIN_SOURCE_SIZE_BASIS = os.getenv("MEMECOIN_SOURCE_SIZE_BASIS", "gross").strip().lower()
MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND = _parse_source_buy_band_ratios(
    os.getenv("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", ""))
# Cap the ratio-derived source floor at >= $250k mcap; zero/absent keeps the existing rule.
MEMECOIN_REAL_SOURCE_BUY_FLOOR_CAP_USD_BY_CHAIN = _parse_source_buy_band_ratios(
    os.getenv("MEMECOIN_REAL_SOURCE_BUY_FLOOR_CAP_USD_BY_CHAIN", ""))
# Solana-only wallet ratio overrides. UID=0 disables only the mcap-ratio leg;
# absolute source-size limits, unknown-mcap fallback and token mcap ceiling still apply.
MEMECOIN_SOLANA_MIN_SOURCE_BUY_MCAP_RATIO_BY_WALLET = _parse_source_buy_band_ratios(
    os.getenv("MEMECOIN_SOLANA_MIN_SOURCE_BUY_MCAP_RATIO_BY_WALLET", ""))
# CUMULATIVE SOURCE BUY (2026-09-13, operator: "spaced buys kill us ... keep track of how much
# they've invested into a signal"). The mcap-ratio source-buy floor judges the trader's NET buys into
# the coin over this many seconds (the current buy included, sells subtracted), not the one buy.
# APU: $200 then $100 four seconds apart, each refused alone. Since 09-06, 157 of 577 ratio-floor
# refusals would pass on 30-minute net buys (4663 42/197, solana 105/329, 56 10/51); they have no
# funded outcome because they were refused. The absolute $ band keeps the single buy (its
# conjunction evidence measured the single buy). 0 = off.
MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S = float(os.getenv(
    "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", "0"))
# PER-WALLET MCAP CEILING (2026-09-12, operator "exempt frankdegods"). "chain:source_user_id=usd",
# comma-separated. It replaces MEMECOIN_REAL_MAX_MCAP_USD for that wallet on that chain only; 0 = no
# ceiling. The key is the wallet id, not the handle, because the handle is display text. First entry:
# frankdegods on Solana. All 9 funded Solana closes were above $250k and all 9 won (+$171.77, $103
# of it from 2Z); it has 0 closes below $250k, so the global ceiling refused every one of its entries.
MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET = _parse_source_buy_band_ratios(
    os.getenv("MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", ""))
# BIG-CAP CEILING LIFT (2026-10-06, operator GO via Alpha; Gamma's held-out study in
# ~/.cache/gamma/bigcap/REPORT.md). A would-be over_ceiling refusal on a listed chain is admitted up
# to MEMECOIN_REAL_BIGCAP_LIFT_MAX_MCAP_USD only when the source buy is >= ..._MIN_SOURCE_USD AND our
# OWN 5-minute volume (the on-chain tape, memecoin_volume_writer.gate_evidence) is >= ..._MIN_V5_MCAP_RATIO
# x mcap. Unknown own volume never lifts; no market-data provider is asked; a per-wallet mcap ceiling
# (MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET) is never raised. UNVALIDATED forward experiment (Codex N8,
# 2026-10-07): the study's 32 usable paper rows (26 coins, mean +6.86%) are pooled across chains
# (Solana 27 rows/22 coins +6.18%, 4663 one, 8453 three, 56 one), no chain meets the n>=30 bar, the
# holdout was inspected during research, and the historical volume input was DexScreener, not our
# own tape. The lift lands on 4663 and 8453 because the own tape knows the volume before entry there
# (81% and 61% of attempts since 09-29); it has no qualified cohort on either. Gamma grades it
# forward per chain from the attempt's bigcap_lift substage. Rollback: set the chain list empty.
MEMECOIN_REAL_BIGCAP_LIFT_CHAINS = os.getenv("MEMECOIN_REAL_BIGCAP_LIFT_CHAINS", "4663,8453")
def _bigcap_threshold(env, name, default):
    """A big-cap lift threshold, finite and positive, or ValueError at startup (Codex N9, 2026-10-06:
    a NaN ratio read as a zero volume requirement and lifted the ceiling). To turn the lift off,
    empty MEMECOIN_REAL_BIGCAP_LIFT_CHAINS; zero is not a disable."""
    try:
        value = float(env.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be a number" % name) from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("%s must be finite and positive, got %r" % (name, env.get(name, default)))
    return value


MEMECOIN_REAL_BIGCAP_LIFT_MAX_MCAP_USD = _bigcap_threshold(
    os.environ, "MEMECOIN_REAL_BIGCAP_LIFT_MAX_MCAP_USD", "500000")
MEMECOIN_REAL_BIGCAP_LIFT_MIN_SOURCE_USD = _bigcap_threshold(
    os.environ, "MEMECOIN_REAL_BIGCAP_LIFT_MIN_SOURCE_USD", "2500")
MEMECOIN_REAL_BIGCAP_LIFT_MIN_V5_MCAP_RATIO = _bigcap_threshold(
    os.environ, "MEMECOIN_REAL_BIGCAP_LIFT_MIN_V5_MCAP_RATIO", "0.10")
# PER-WALLET MCAP FLOOR (2026-09-18, operator). Same "chain:source_user_id=value" shape as the
# ceiling above, and the same precedence: it REPLACES MEMECOIN_REAL_MIN_MCAP_USD for that wallet on
# that chain. First entry: smol_intern on 4663, whose funded record splits by band -- 8 closes under
# $250k for -$57 and 4 closes above for +$41. The operator's aim is to point a filter at ONE wallet
# rather than move a global bar, and its refused small caps stay observed so the split can be
# re-read from its own refusals.
MEMECOIN_REAL_MIN_MCAP_USD_BY_WALLET = _parse_source_buy_band_ratios(
    os.getenv("MEMECOIN_REAL_MIN_MCAP_USD_BY_WALLET", ""))
# PER-WALLET EXECUTION PREMIUM CAP (2026-09-17, operator "a" for DegenCapitalLLC). "chain:source_user_id=ratio",
# comma-separated. It replaces MEMECOIN_REAL_MAX_EXEC_PREMIUM for that wallet on that chain; applied on
# Solana entries only. It never enables a disabled gate. Evidence: DegenCapitalLLC's 7 Solana premium
# refusals since 09-13 replayed with exact quotes gave 4 take-profits, 0 stops (~+$102 at $175), and
# its funded Solana fills paid 50%+ over the trader 3 times, 3 wins. Every other wallet: the same
# refusals avoided ~$433. Review after 2 weeks of this wallet's funded fills.
MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET = _parse_source_buy_band_ratios(
    os.getenv("MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", ""))


def solana_wallet_chase_cap_pct(source_user_id, base_pct):
    """The chase cap for one Solana source: MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET's ratio as
    (ratio - 1) x 100 when it is HIGHER than `base_pct`, else `base_pct`. A LIFT only: it never
    lowers a cap and never enables a cap that is off (base <= 0). One rule for the J7 exact-quote
    check and the slot lane's signed price floor (2026-09-28, operator: "widen the slot lane cap
    for top wallets"; DegenCapitalLLC BcoDT failed min-out at 2.62x and was +85% at 120 s)."""
    base = float(base_pct)
    ratio = (MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET or {}).get(
        ("solana:%s" % source_user_id).lower()) if source_user_id else None
    if base > 0 and ratio and float(ratio) > 1:
        base = max(base, (float(ratio) - 1.0) * 100.0)
    base = wallet_band_cap_pct(source_user_id, "solana", base)
    hard, _source = copy_premium_cap(source_user_id, "solana")
    if hard > 0:
        return min(base, hard) if base > 0 else hard
    return base


# PONS CREATOR-TAX TEST (2026-09-12, operator "b"). A Pons curve coin whose round trip is over the
# normal cost budget (0.4 x TP = 10%) but within MEMECOIN_PONS_TEST_MAX_ROUND_TRIP_PCT is bought at
# MEMECOIN_PONS_TEST_CLIP_USD, only for the listed source_user_ids, until MEMECOIN_PONS_TEST_MAX_TRADES
# such funded positions exist. Empty wallet list = off. Basis: every Pons coin refused on cost since
# 09-05 carried a 5% creator tax (-13.4% round trip), all 6 came from CryoPalmar, and there is no
# funded trade at that tax. The candle replay was 3 take-profits / 3 stops, net negative. This buys
# the funded evidence at a bounded cost (about 20 x $20 worst case).
MEMECOIN_PONS_TEST_WALLETS = frozenset(
    x.strip().lower() for x in os.getenv("MEMECOIN_PONS_TEST_WALLETS", "").split(",") if x.strip())
MEMECOIN_PONS_TEST_MAX_ROUND_TRIP_PCT = float(os.getenv("MEMECOIN_PONS_TEST_MAX_ROUND_TRIP_PCT", "14"))
MEMECOIN_PONS_TEST_CLIP_USD = float(os.getenv("MEMECOIN_PONS_TEST_CLIP_USD", "50"))
MEMECOIN_PONS_TEST_MAX_TRADES = int(os.getenv("MEMECOIN_PONS_TEST_MAX_TRADES", "20"))
# Alive-token bar RELATIVE to mcap (volume_h1 >= mcap * this). 0 = off (flat floor only).
# When set and mcap is known, the relative bar replaces the flat floor, which stays as the
# mcap-unknown fallback. See volume_alive_decision() for the measured basis (2026-08-19).
# Both h1 settings apply across market-cap bands. Missing h1 data still passes.
# ABSOLUTE SOURCE-BUY BAND (2026-09-08, operator). Both 0 = OFF, so this lands inert.
# The ratio above is a fraction of market cap and runs BACKWARDS across coin sizes — it passes
# 97.6% at nano and refuses 84% at mid, i.e. it refuses least where coins are smallest. These are
# absolute dollars on the source's own reported buy, and they ADD to the ratio rather than
# replacing it. The $1k-$10k band is half of a replicated conjunction with 10-50% turnover
# (n=44, +21.72%, 75% win); the ceiling is NOT evidence that a large buy is independently bad —
# source size proxies for coin size there. Unknown size passes.
MEMECOIN_REAL_MIN_SOURCE_BUY_USD = float(os.getenv("MEMECOIN_REAL_MIN_SOURCE_BUY_USD", "0"))
MEMECOIN_REAL_MAX_SOURCE_BUY_USD = float(os.getenv("MEMECOIN_REAL_MAX_SOURCE_BUY_USD", "0"))
MEMECOIN_REAL_MIN_TURNOVER_H1 = float(os.getenv("MEMECOIN_REAL_MIN_TURNOVER_H1", "0"))
# SOLANA MICRO HOURLY TURNOVER FLOOR (2026-09-28, operator "yes both"; spec
# 2026-09-28-memecoin-solana-micro-h1-turnover-floor-design.md). ORGANIC h1 (the source's buy
# subtracted) over effective_mcap, on Solana micro coins only, whatever the small-coin h1 switch says.
# 0 = off. The activation value 0.143 is on THE GATE'S figure and refuses exactly the 27 micro fills
# Gamma's move-possible evidence graded (-$139.31); her raw-h1 cut 0.1645 does not carry over (on the
# gate's figure it refuses 4 more fills that netted +$135.25).
MEMECOIN_REAL_SOLANA_MICRO_MIN_TURNOVER_H1 = float(
    os.getenv("MEMECOIN_REAL_SOLANA_MICRO_MIN_TURNOVER_H1", "0"))
# The 5-minute "moving NOW" leg: volume_m5 >= mcap * this. Ranked ABOVE the h1 leg by the
# operator (Momota: crash volume passed every h1 bar on a coin flat at entry). 0 = off.
MEMECOIN_REAL_MIN_TURNOVER_M5 = float(os.getenv("MEMECOIN_REAL_MIN_TURNOVER_M5", "0"))
# M5 TRADE-COUNT floor (2026-08-30, operator). Replaces the mcap-RATIO floor above as the
# "is anyone other than the trader we are copying active" check. 0 = OFF; 2 implements the
# approved rule `txns_m5_buys + txns_m5_sells > 1`.
#
# WHY A COUNT AND NOT THE OPERATOR'S LITERAL ASK. He asked for m5 volume above zero after
# subtracting the signal's own size. Measured: `volume_m5 > 0` refuses 0 of 5,913 snapshots, so
# it is a no-op — deleting the gate, not replacing it. And the subtraction is not computable:
# in 34.7% of snapshots (2,053 of 5,913) `volume_m5 < their_size_usd`, so their trade provably
# is NOT inside that aggregate yet. Feed lag plus the provider's own window lag would drive a
# third of coins negative for reasons unrelated to liquidity. A COUNT carries the same intent
# and does not care whether their fill has propagated.
#
# THE EVIDENCE IS NOT A PROVEN IMPROVEMENT AND MUST NOT BE READ AS ONE. Solana, n=154 mints with
# a gradeable paper outcome since 08-16: the banded ratio separates admitted from refused by
# 1.79 pts refusing ~46%; this rule separates by 2.46 pts refusing 10.4%. Permutation p = 0.456
# over 20,000 resamples — indistinguishable from noise. The defensible claim is "it blocks a
# quarter as many signals and nothing in the data says the extra 36% it admits are worse".
#
# IT ALSO REACHES A POPULATION THE RATIO CANNOT. The ratio legs live inside `if mc and mc > 0`,
# and 47.8% of attempts reaching the market-data stage have no resolvable mcap, so the old gate
# was structurally inert on nearly half the flow. A trade count needs no market cap.
MEMECOIN_REAL_MIN_TXNS_M5 = int(os.getenv("MEMECOIN_REAL_MIN_TXNS_M5", "0"))
# Absent txn counts: 0 = ADMIT (default), 1 = refuse. Coverage is 70.3% of snapshots since
# 08-16 — slightly BETTER than volume_m5 at 67.7%, but ~30% still lack it. Admitting matches the
# chase-guard convention this module already documents ("a missing field fails OPEN for its own
# leg only") and matches the de facto behaviour of the gate it replaces, which is skipped
# entirely wherever mcap is unresolvable. Fail-closed here would refuse ~30% of flow on missing
# DATA, which is the opposite of what this change is for.
MEMECOIN_REAL_TXNS_M5_REQUIRED = int(os.getenv("MEMECOIN_REAL_TXNS_M5_REQUIRED", "0"))
# COIN-AGE CEILING (2026-08-17, operator "Sorajiro no movement"): refuse a real entry when the
# coin's deepest pair is older than this. Measured over the prior 3 days of real closes: <1h coins
# n=34 broke even (+$0.86, 59% wins, all TP winners); EVERY older bucket lost — combined -$234.32
# over n=28 at 32% wins. An old range-bound coin has no expansion phase for the copy premise to
# ride. Fail-OPEN on unknown age (an unindexed pool is fresh by construction). 0 = off.
MEMECOIN_REAL_MAX_PAIR_AGE_S = float(os.getenv("MEMECOIN_REAL_MAX_PAIR_AGE_S", "0"))
# Which coin bands can open a funded position. The default admits nano through mid.
# `out_of_scope` remains unavailable. Set `small,mid` to restore the former floor.
# Which entry-refusal alert kinds stay OFF the phone (2026-08-06, operator: "too spammy").
# POLICY DECLINES only — the system working as designed. Measured 7-day volume before this:
# RISK-REFUSAL 80, MCAP-SKIP 53, STALE-SKIP 10, CONSENSUS-SKIP 7 = ~21/day, the bulk of the noise.
# DELIBERATELY NOT SILENCEABLE by default: TOKEN-SAFETY-REFUSAL, ENTRY-ROUTE-REFUSAL,
# SLIPPAGE-REFUSAL, ENTRY-REFUSAL, NETWORK-DROP, ENTRY-DISARMED — those say something is BROKEN
# (no sell route, unverifiable sell sim, disarmed runner), not that policy declined. Display only:
# every refusal is still persisted to memecoin_entry_attempts.terminal_reason regardless, so this
# changes what reaches the phone and nothing about what the funded runner decides or records.
# Set to "" to restore the old fully-verbose behaviour without a redeploy.
MEMECOIN_ALERT_SILENT_REFUSALS = frozenset(
    k.strip() for k in os.getenv(
        "MEMECOIN_ALERT_SILENT_REFUSALS",
        "RISK-REFUSAL,MCAP-SKIP,CONSENSUS-SKIP,STALE-SKIP").split(",")
    if k.strip())
MEMECOIN_REAL_RESERVABLE_BANDS = tuple(
    b.strip() for b in os.getenv(
        "MEMECOIN_REAL_RESERVABLE_BANDS", "nano,micro,small,mid").split(",")
    if b.strip())
# Per-wallet copy-arm exit policy (2026-08-04, operator-directed). When on, a funded reservation
# takes the exit rails of the paper arm the SOURCE WALLET's own copies performed best under
# (sniper/services/memecoin_copy_arm.py) instead of the coin-band default. Selection is the same
# number the Armed board displays, is frozen into the immutable contract at reservation, and does
# NOT gate admission — a wallet with no arm evidence still enters, on the fallback rails.
# DEFAULT OFF: shipping this inert keeps the funded leg byte-identical until an operator opts in.
#
# ⚠️ ACTIVATION ORDER. Read ONCE at import, like every setting in this module, so the value the
# funded runner uses is the one present when its process STARTED. Setting the env after a deploy
# leaves the running runner disabled while the config claims otherwise — the same class of trap as
# MEMECOIN_POLL_MS (2026-08-03), where a `.env` entry read at the wrong moment was silently
# ignored. Set it BEFORE `deploy_memecoin_runtime.sh`, then verify against the live process:
#     tr '\0' '\n' < /proc/$(pgrep -f memecoin_runner_loop | head -1)/environ | grep COPY_ARM
MEMECOIN_COPY_ARM_POLICY = os.getenv(
    "MEMECOIN_COPY_ARM_POLICY", "0").strip().lower() in {"1", "true", "yes", "on"}
# Adaptive exit TEMPLATES (2026-07-14) — the system's per-coin exit strategy, written as a
# per-position override (source='scalp'/'ride'). DEFAULT OFF: when off, entry + exit are
# byte-identical to today (the mcap ceiling refuses big-caps, positions get the default rails).
# When 'on': (1) a coin above MEMECOIN_SCALP_MCAP_USD is NO LONGER refused by the ceiling — it opens
# and gets the SCALP template (grab a small quick TP, tight stop) instead of moonshot rails; (2) the
# exit sweep lazily assigns the template to any real position lacking an override. Operator manual
# overrides always outrank a template. Params are launch guesses — SP#3 recalibrates by paired A/B.
MEMECOIN_AUTO_TEMPLATES = os.getenv("MEMECOIN_AUTO_TEMPLATES", "off").strip().lower()
MEMECOIN_SCALP_MCAP_USD = float(os.getenv("MEMECOIN_SCALP_MCAP_USD", "30000000"))   # >= this -> scalp
MEMECOIN_SCALP_TP_PCT = float(os.getenv("MEMECOIN_SCALP_TP_PCT", "25"))
MEMECOIN_SCALP_STOP_PCT = float(os.getenv("MEMECOIN_SCALP_STOP_PCT", "25"))
# paper zombie sweep (2026-07-11): retire open PAPER positions with no successful quote for this
# long (unindexed/dead coin -> no bracket can ever fire; they pile up as no-data rows on the
# board). Mirror-on-sell can still close them before the sweep. 0 = off.
MEMECOIN_PAPER_UNQUOTABLE_S = float(os.getenv("MEMECOIN_PAPER_UNQUOTABLE_S", "21600"))   # 6h
# stop debounce (2026-07-10): the −stop read must hold for this many CONSECUTIVE ~15s sweeps
# before the real stop executes. 1 = fire immediately (legacy). Forensics on the 4 paper
# stop-outs: 2 were single bad DexScreener ticks (other traders were buying 3–4× the "stop
# price" within seconds) — in real money that sells a healthy coin into an air pocket. The TP
# is deliberately NOT debounced (selling into a spike is favorable).
# Consecutive breaching sweeps required before a stop market-sells. Counted in SWEEPS, so its
# wall-clock meaning is set by the cadence of whichever lane decides:
#   old: 2 hits x ~31s bracket pass  = ~62s of confirmation
#   now: 5 hits x 1s fast lane       = ~5s of confirmation, from 5 INDEPENDENT reads
# Both axes improve: 12x faster to fire AND stronger evidence than 2 reads. Raised together with
# MEMECOIN_FASTLANE_DECIDES on 2026-07-27 -- leaving it at 2 would have cut confirmation to ~2s,
# which a single bad tick clears (DexScreener once returned $17.099 for a $0.003 coin). The
# up-spike quarantine is NOT a substitute: it filters ticks that read too high, and a stop fires
# on ticks that read too LOW.
MEMECOIN_REAL_STOP_CONFIRM_SWEEPS = int(os.getenv("MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", "5"))
# Consecutive sweeps the whole-bag executable quote must FAIL, while a display price still exists,
# before the position is treated as unsellable rather than as a transient quote outage. Measured
# 2026-07-24: a live 4663 pool returned $2.82 on a $300 chart-implied bag while its chart looked
# healthy, and catastrophe only fires while quotable — so nothing caught it. 4 sweeps ~= 1 min at the
# 15s cadence: long enough that an RPC blip cannot trip it, short enough to matter. 0 disables.
MEMECOIN_REAL_UNSELLABLE_SWEEPS = int(os.getenv("MEMECOIN_REAL_UNSELLABLE_SWEEPS", "4"))
# zombie guard (2026-07-10): a real position with NO successful price quote for this many seconds
# gets an on-chain exit attempt — without a quote the SL can never fire and a rugged coin would
# hold a concurrency slot forever. NOT a timecap: a quoted position is never touched. 0 = off.
# A failed attempt marks stuck_bag (one loud alert) and is NOT auto-retried — manual close retries.
MEMECOIN_REAL_UNQUOTABLE_EXIT_S = float(os.getenv("MEMECOIN_REAL_UNQUOTABLE_EXIT_S", "86400"))
# auto-demote guardrail (pre-registered 2026-07-10): after a real close, a wallet at cumulative
# real realized <= -LOSS_USD OR with CONSEC straight real losing closes is auto-revoked from the
# real allowlist (real_approved=0) + alerted. Re-approval is manual. 0 = that rule off.
MEMECOIN_REAL_DEMOTE_LOSS_USD = float(os.getenv("MEMECOIN_REAL_DEMOTE_LOSS_USD", "100"))
MEMECOIN_REAL_DEMOTE_CONSEC = int(os.getenv("MEMECOIN_REAL_DEMOTE_CONSEC", "4"))
# TRIAL ARM (spec 2026-10-01-memecoin-trial-arm-design.md; operator 2026-10-01: $20 clips on solana
# and 4663). A chain is a trial chain only while MEMECOIN_REAL_TRIAL_CLIP_USD_<CHAIN> (SOLANA, 4663)
# is a positive number: unset means no trial there, never the full clip. Read at call time by the
# start script only: the clip is copied into the trial row, so a later edit never re-sizes a trial.
# Open trial positions plus unresolved trial reservations, across every trial (proposed 2).
MEMECOIN_REAL_TRIAL_MAX_CONCURRENT = int(os.getenv("MEMECOIN_REAL_TRIAL_MAX_CONCURRENT", "2"))


def trial_clip_usd_for(chain):
    """The trial clip on `chain` in USD, or None: unset, malformed or not positive is no trial."""
    raw = os.getenv("MEMECOIN_REAL_TRIAL_CLIP_USD_%s" % str(chain or "").strip().upper())
    try:
        clip = float(raw)
    except (TypeError, ValueError):
        return None
    return clip if math.isfinite(clip) and clip > 0 else None


def memecoin_real_trial_auto():
    """Call-time: which trial verdicts act on their own. 'off' (default): every verdict only pages
    and stops the trial's entries. 'demote': DEMOTE, INSUFFICIENT and SHADOW also end the trial and
    disarm the chain. 'both': PROMOTE also ends the trial and keeps the chain armed at the full
    clip. Anything else is 'off'."""
    raw = os.getenv("MEMECOIN_REAL_TRIAL_AUTO", "off").strip().lower()
    return raw if raw in ("off", "demote", "both") else "off"
# Promote gate (2026-07-12, pre-registered): a wallet may be REAL-armed only on OUR copy evidence —
# >= N closed alpha_b paper copies AND >= $X cum copy PnL (their own PnL is the wrong criterion:
# PrimeT own -20% -> copy +$529; techquant own +1970% -> copy -$78). Operator force overrides but
# the audit row is marked. Mirrors auto-demote on the positive side.
MEMECOIN_REAL_PROMOTE_MIN_CLOSES = int(os.getenv("MEMECOIN_REAL_PROMOTE_MIN_CLOSES", "5"))
MEMECOIN_REAL_PROMOTE_MIN_PNL_USD = float(os.getenv("MEMECOIN_REAL_PROMOTE_MIN_PNL_USD", "25"))
# (Per-chain arming + its chain-veto were removed 2026-07-15 — arming is global: approved = armed on
# every chain. The aggregate promote gate above is the only copy-PnL bar.)
# EVM networks admitted to observation/routing. Funded context construction separately requires an
# explicit `direct` venue and verified addresses; Base remains observable but disabled for funded
# execution. Ethereum L1 (1) is structurally losing at small clips, Monad (143) has no confirmed
# routing, and BSC (56) remains held out pending a verified direct route and smoke test.
# Robinhood Chain (4663, live 2026-07-01) is now ENABLED (2026-07-12). Both prior hold-out reasons
# are resolved: (1) fomo emits 4663 events once x-supported-chains includes 4663 (capture live), and
# (2) the WETH-denominated variant EXISTS — CHAIN_QUOTE['4663']=WETH, usd_to_quote_atomic /
# quote_balance_usd convert USD<->WETH via live ETH/USD (fail-closed), tested in
# test_memecoin_evm_quote_token.py. Direct V3 and bounded V2 routes exist on 4663; RPC
# https://rpc.mainnet.chain.robinhood.com (gas = ETH). SAFE to enable unfunded: 0 WETH bankroll ->
# 0 sizing -> no entry (fail-closed) until the wallet is funded with WETH (+ETH gas). Fund
# 0x898BE9c27f8B391C47A3d9F2dA17781aa8aA3A1d, then a live smoke ($ round-trip) gates trusting it.
# A base58 (Solana) mint always routes to the Solana leg regardless of this list; this gates EVM.
# Chains DexScreener is blind to. Only GeckoTerminal may fill their indexed-price gaps; everywhere
# else a DexScreener miss remains missing rather than consulting a secondary source.
MEMECOIN_DEX_BLIND_CHAINS = {c.strip() for c in os.getenv(
    "MEMECOIN_DEX_BLIND_CHAINS", "4663").split(",") if c.strip()}
# WHICH CHAINS RUN THE ON-CHAIN RUNG LADDER (v2 / v4 / v4_multihop) — ITS OWN SETTING (2026-09-04).
#
# This used to borrow `MEMECOIN_DEX_BLIND_CHAINS`, which answers a completely different question:
# "which chains need a price source other than DexScreener". One name carried two meanings, and on
# 2026-09-04 clearing the pricing list to retire GeckoTerminal silently DISABLED THE V4 RUNG on
# 4663 -- where every pool is V4. The quote ladder fell through to v3 alone and 26 entries refused
# `no_route_all_venues` in 84 minutes while the pools they needed sat quotable the whole time.
# `evm.quote_miss` recorded it exactly: "no route on v3 (v2/v4 off)" with v2 and v4 both SKIPPED.
#
# Defaults to the pricing list PLUS the chains whose rungs we run regardless, so behaviour is
# unchanged for anyone who never sets it, but it can now be set independently -- and a chain
# removed from the PRICING list keeps its rungs.
#
# 8453 IS IN THE RUNG LIST AND NOT IN THE PRICING LIST (2026-09-06). Base is not DexScreener-blind
# -- DexScreener indexes it -- so adding it to `MEMECOIN_DEX_BLIND_CHAINS` would change which
# PRICE SOURCE Base consults, which is not what the Base parity build asks for. What Base needs is
# the v2/v4/v4_multihop RUNGS, which is this list. Keeping the two apart is the whole reason the
# split above exists.
_RUNG_LADDER_ALWAYS = ("8453", "56", "1")
MEMECOIN_EVM_RUNG_LADDER_CHAINS = {c.strip() for c in os.getenv(
    "MEMECOIN_EVM_RUNG_LADDER_CHAINS",
    ",".join(sorted(set(MEMECOIN_DEX_BLIND_CHAINS) | set(_RUNG_LADDER_ALWAYS)))).split(",")
    if c.strip()}
# V4-ONLY RUNG, INDEPENDENT OF THE LADDER ABOVE (2026-09-16, Arc build). Membership here tries
# the V4 rung without opening V2 and without needing MEMECOIN_EVM_RUNG_LADDER_CHAINS or
# MEMECOIN_DEX_BLIND_CHAINS membership -- that ladder gates v2 AND v4 as one pair, rooted in a
# pricing-source and a V2-ENTRY flag that have nothing to do with whether a chain's real pools
# happen to be V4. Arc has real, on-chain-verified V3 core; its non-V3 coins are V4-only, DexScreener
# already covers it, and it has no V2 pools at all -- none of the three conditions the combined
# ladder was built for apply, so it gets its own switch. A chain already in the ladder above tries
# V4 exactly as before, whether or not it is also listed here.
MEMECOIN_EVM_V4_RUNG_CHAINS = {c.strip() for c in os.getenv(
    "MEMECOIN_EVM_V4_RUNG_CHAINS", "").split(",") if c.strip()}

# Route an EVM buy that arrived with NO networkId to 4663. Default OFF, and separate from the
# FOMO_TO_DEX entry on purpose: the map entry restores market data (gates start REFUSING), while
# this changes which chain a buy routes to (money flow, opposite risk direction). It also
# REROUTES tokens listed on both base and robinhood via the liquidity tie-break, not only ones
# that previously resolved to None. No traffic since 2026-07-15. See _resolvable_evm_networks.
# DEXSCREENER PAIR ACTIVITY FALLBACK (2026-09-17, operator order B; finding
# solana-selected-dexscreener-pair-can-be-dust). `select_dex_pair` ranks pairs by quote-side USD. When
# that top pair holds under MEMECOIN_DEX_PAIR_DUST_USD on the quote side, or reports no liquidity, the
# fallback picks the listed pair with the most h1 transactions, then h1 volume, then quote-side
# liquidity. OFF by default: stats, trace and marks record both picks (`pair_selection`) and today's
# pick stays selected. The operator turns it ON after the shadow counts.
MEMECOIN_DEX_PAIR_ACTIVITY_FALLBACK = os.getenv(
    "MEMECOIN_DEX_PAIR_ACTIVITY_FALLBACK", "0").strip().lower() in {"1", "true", "yes"}
try:
    MEMECOIN_DEX_PAIR_DUST_USD = float(os.getenv("MEMECOIN_DEX_PAIR_DUST_USD", "100"))
except ValueError:
    MEMECOIN_DEX_PAIR_DUST_USD = 100.0
if not math.isfinite(MEMECOIN_DEX_PAIR_DUST_USD) or MEMECOIN_DEX_PAIR_DUST_USD < 0:
    MEMECOIN_DEX_PAIR_DUST_USD = 100.0

MEMECOIN_RESOLVE_UNKNOWN_EVM_4663 = os.getenv(
    "MEMECOIN_RESOLVE_UNKNOWN_EVM_4663", "0").strip().lower() in {"1", "true", "yes"}
# The same knob for Base, DEFAULT ON because that is what Base does today: `_resolvable_evm_networks`
# gates 4663 alone, so an EVM buy with no networkId has always been allowed to resolve to 8453.
# Taking the 4663 default here would REMOVE existing Base flow, which is the opposite of parity.
MEMECOIN_RESOLVE_UNKNOWN_EVM_8453 = os.getenv(
    "MEMECOIN_RESOLVE_UNKNOWN_EVM_8453", "1").strip().lower() in {"1", "true", "yes"}

MEMECOIN_EVM_ALLOWLIST = {c.strip() for c in os.getenv(
    "MEMECOIN_EVM_ALLOWLIST", "8453,4663").split(",") if c.strip()}
MEMECOIN_INK_PRECONFIRMED_ADMISSION_ENABLED = os.getenv(
    "MEMECOIN_INK_PRECONFIRMED_ADMISSION_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_INK_SUBBLOCKS_ENABLED = os.getenv(
    "MEMECOIN_INK_SUBBLOCKS_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# CHAINS TO OBSERVE (V4 pool-key discovery only), SEPARATE FROM THE FUNDED ALLOWLIST (2026-09-16,
# Arc build). A chain here gets its Initialize stream watched and stored -- no wallet arming, no
# execution authority, nothing this set can do reaches a signing or broadcast path. It exists so
# pool-key coverage can start accumulating for a chain BEFORE the (separate, later) decision to
# add it to MEMECOIN_EVM_ALLOWLIST. Default empty: observing a new chain is an explicit opt-in.
MEMECOIN_EVM_OBSERVE_CHAINS = {c.strip() for c in os.getenv(
    "MEMECOIN_EVM_OBSERVE_CHAINS", "").split(",") if c.strip()}
# Entry conditioner (2026-07-13, feature-store positive-selection gate). DEFAULT OFF = inert; the
# real entry path is byte-identical until an operator flips this. 'shadow' logs the would-be verdict
# on every real attempt (measure before gating money); 'on' blocks entries whose band-implied
# p(hit20) < MEMECOIN_ENTRY_MIN_HIT_PROB. Band rates are frozen constants in
# sniper/services/memecoin_entry_conditioner.py (refresh via memecoin_feature_scan.py --bands).
MEMECOIN_ENTRY_CONDITIONER = os.getenv("MEMECOIN_ENTRY_CONDITIONER", "off").strip().lower()
MEMECOIN_ENTRY_MIN_HIT_PROB = float(os.getenv("MEMECOIN_ENTRY_MIN_HIT_PROB", "0.10"))
# Solana custody + RPC for the real leg. The keypair is a funded wallet OUTSIDE the repo (0600, never
# in git/DB/logs); RPC is a Helius (or equivalent) endpoint. Both must be present for the real leg to
# construct — absent -> the real leg is a no-op and the paper runner is byte-identical. Never hardcode
# the RPC (it embeds an API key): set MEMECOIN_SOLANA_RPC_URL in env.
MEMECOIN_SOLANA_KEYPAIR = os.getenv(
    "MEMECOIN_SOLANA_KEYPAIR", str(state_path("data", "solana_keypair.json")))
MEMECOIN_SOLANA_INFRA_PROVIDER = os.getenv("MEMECOIN_SOLANA_INFRA_PROVIDER", "legacy")
MEMECOIN_SOLANA_RPC_URL = os.getenv("MEMECOIN_SOLANA_RPC_URL", "")
MEMECOIN_SOLANA_RPC_POOL_URLS = tuple(
    url.strip() for url in os.getenv("MEMECOIN_SOLANA_RPC_POOL_URLS", "").split(",")
    if url.strip())


def solana_funded_rpc_urls(env=None):
    """Explicit authenticated Shyft URLs, or the legacy pool and final fallback.

    Shyft accepts one or more distinct endpoints; it never appends a public fallback.

    Validation errors deliberately omit values because these URLs normally contain API keys.

    The bound was EXACTLY three until 2026-08-25. It is a shape check that catches a half-written
    pool, not a safety property, and a fixed count blocks the one thing an operator does when a
    provider caps: add another key. Minimum three is kept so a single endpoint can never masquerade
    as a pool; every other rule (distinct, HTTPS, fallback required) is unchanged.

    ORDER IS PRIORITY. Index 0 is primary and the last entry is the last-resort fallback, so put
    keyed endpoints before public ones. Keep at least one NON-provider endpoint in the list: four
    keys from the same vendor all die together in a vendor-wide outage, and the public endpoint is
    the only thing that survives it.
    """
    from urllib.parse import urlsplit
    from sniper.services.memecoin_solana_provider import infra_provider, provider_urls

    if env is None:
        env = {"MEMECOIN_SOLANA_INFRA_PROVIDER": MEMECOIN_SOLANA_INFRA_PROVIDER,
               "MEMECOIN_SOLANA_RPC_POOL_URLS": ",".join(MEMECOIN_SOLANA_RPC_POOL_URLS),
               "MEMECOIN_SOLANA_RPC_URL": MEMECOIN_SOLANA_RPC_URL}
    if infra_provider(env) == "shyft":
        return provider_urls("funded", env)
    pool = tuple(
        url.strip() for url in env.get("MEMECOIN_SOLANA_RPC_POOL_URLS", "").split(",")
        if url.strip())
    fallback = env.get("MEMECOIN_SOLANA_RPC_URL", "").strip()
    if not pool:
        return (fallback,) if fallback else ()
    if len(pool) < 3:
        raise ValueError("Solana funded RPC pool requires at least three URLs")
    urls = pool + ((fallback,) if fallback else ())
    if not fallback:
        raise ValueError("Solana funded RPC pool requires the legacy fallback URL")
    if len(set(urls)) != len(urls):
        raise ValueError("Solana funded RPC pool URLs must be distinct")
    if any(urlsplit(url).scheme.lower() != "https" or not urlsplit(url).netloc for url in urls):
        raise ValueError("Solana funded RPC pool requires HTTPS URLs")
    return urls
# READ-ONLY Solana endpoint for observation lanes only: arrival capture, getBlockTime (slot->time),
# getTransaction (vault-delta amounts). DELIBERATELY a separate var from MEMECOIN_SOLANA_RPC_URL:
# the keypair already exists on this host, so the real leg constructs the moment that one is set —
# wiring data through it would silently ARM Solana trading. This one never touches custody, signing
# or execution. Defaults to the free public endpoint (rate-limited, fine for bounded backfill).
MEMECOIN_SOLANA_READ_RPC_URL = os.getenv(
    "MEMECOIN_SOLANA_READ_RPC_URL", "" if MEMECOIN_SOLANA_INFRA_PROVIDER == "shyft"
    else "https://api.mainnet-beta.solana.com")
# Freeze and validate once at import; the getter stays a constant-time hot read.
if MEMECOIN_SOLANA_INFRA_PROVIDER == "shyft":
    from sniper.services.memecoin_solana_provider import provider_urls as _read_urls
    MEMECOIN_SOLANA_READ_RPC_URL = _read_urls("read", {
        "MEMECOIN_SOLANA_INFRA_PROVIDER": "shyft",
        "MEMECOIN_SOLANA_READ_RPC_URL": MEMECOIN_SOLANA_READ_RPC_URL})[0]


def solana_read_rpc():
    """Endpoint for read-only Solana observation. NEVER use for signing, custody or execution —
    those must go through MEMECOIN_SOLANA_RPC_URL, which is what arms the real leg."""
    return (MEMECOIN_SOLANA_READ_RPC_URL or "").strip()
# how often the real leg reconciles ledger vs on-chain token balances (drift -> alert).
MEMECOIN_RECONCILE_PERIOD_S = float(os.getenv("MEMECOIN_RECONCILE_PERIOD_S", "120"))
# a reservation still 'reserved' this long after creation is stale -> the recovery pass resolves it
# against chain. MUST exceed the worst-case confirm window (EVM receipts can take minutes) so it never
# races an in-flight confirm and frees a slot that's about to fill.
MEMECOIN_RESERVATION_STALE_S = float(os.getenv("MEMECOIN_RESERVATION_STALE_S", "600"))

# How young a reservation may be and still be scanned for the EXACT blockhash-expiry release.
# 0 = off; recovery then starts only at MEMECOIN_RESERVATION_STALE_S, which is the old behaviour.
# A Solana blockhash lives about 150 slots, so ~90s after broadcast the chain height has passed the
# reservation's recorded submission_last_valid_height and those exact signed bytes can never land.
# Waiting the full 600s to act on that proof blocked reservation 855 (happyseb) for 10 minutes on a
# transaction that had been dead for 1302 blocks -- it held the mint and an exposure slot the whole
# time. Only rows carrying BOTH a tx_sig and a persisted height binding are admitted this early, and
# they may take that one release and nothing else. The binding is written BEFORE network I/O, so it
# is a PRECONDITION for the proof, never evidence that anything was broadcast. Safety comes from the
# release condition instead: the chain must already be past the binding, so the blockhash is expired
# when we release and a late send can never land. A transaction that DID land answers confirmed or
# pending, never not_found. The never-broadcast release has no such guarantee, because a row with no
# sig has nothing bounding when its executor might still send, so it waits the full stale clock.
MEMECOIN_RESERVATION_EXPIRY_PROOF_S = float(
    os.getenv("MEMECOIN_RESERVATION_EXPIRY_PROOF_S", "0"))
# Fresh-pool re-quote retry (2026-08-14). A buy on a pool no aggregator has indexed yet aborts
# no-quote/no-route; the same coin is routable within minutes. Retry the funded hook on the stored
# event every _INTERVAL_S until it fills, a non-transient gate declines it, or the WINDOW closes /
# ATTEMPTS run out. 0 window disables the lane. The reservation is released on every miss, so this
# holds no slot across the wait, and reserve_pending's UNIQUE(mint) blocks any double-open.
MEMECOIN_ENTRY_RETRY_WINDOW_S = float(os.getenv("MEMECOIN_ENTRY_RETRY_WINDOW_S", "120"))
# A SEPARATE BUDGET FOR A BLIND MISS (2026-09-08). 0 = off, i.e. one window for both.
# The window above is calibrated for INDEX LAG — a 4663 pool's Initialize surfaces 38-61s after
# the block — and a provider outage is a different phenomenon with a different duration. HODL
# (0x110cfd84, 19:06:02Z) retried six times over 53s while v4 discovery and the v2 read alternated
# failures, the window expired, and discovery then found its pool in 670ms once the endpoints came
# back. Bounded in practice by MEMECOIN_REAL_ENTRY_MAX_AGE_S / FOLLOW_MAX_AGE_S, which every retry
# re-runs: a longer clock cannot admit an entry the age gate would refuse.
MEMECOIN_ENTRY_RETRY_BLIND_WINDOW_S = float(
    os.getenv("MEMECOIN_ENTRY_RETRY_BLIND_WINDOW_S", "0"))
MEMECOIN_ENTRY_RETRY_INTERVAL_S = float(os.getenv("MEMECOIN_ENTRY_RETRY_INTERVAL_S", "8"))
# A marginal round-trip-cost miss (route_over_budget_marginal) re-asks after this many seconds for its
# first three retries (73849, 2026-10-14); 0 = the ordinary interval.
MEMECOIN_ENTRY_RETRY_MARGINAL_S = float(os.getenv("MEMECOIN_ENTRY_RETRY_MARGINAL_S", "1.0"))
MEMECOIN_ENTRY_RETRY_MAX_ATTEMPTS = int(os.getenv("MEMECOIN_ENTRY_RETRY_MAX_ATTEMPTS", "12"))
# DIP RETRY (operator order 2026-09-30: "only for the most botted accounts, not for everyone").
# A Solana buy the chase cap refused, from a wallet named here, waits up to DIP_RETRY_S from its
# SIGNAL while a watch thread quotes our clip every DIP_RETRY_POLL_S; when the premium over the
# gate's own reference is back within the gate's cap, the whole decision runs again (every gate,
# the chase cap on a fresh funded quote). At most DIP_RETRY_MAX_HITS decisions per signal.
# WALLETS: comma list of source_user_id or handle (case-insensitive). Empty = nobody. 0 s = off.
# Evidence: Gamma 8c3bbaba2, DegenCapitalLLC +22.4% [+7.0, +32.2] n=8, every dip within 15 s;
# pooled over all wallets no window paid, so this is per wallet by design.
MEMECOIN_REAL_DIP_RETRY_S = float(os.getenv("MEMECOIN_REAL_DIP_RETRY_S", "0"))
# DIP RETRY MAX PREMIUM (operator 2026-10-09: "lower the dip retry to 10% above the entry"). PERCENT
# over the dip gate's own recorded reference. A cap only: the watch hits at min(gate cap, this) (EVM:
# restated on the quote basis through the refused quote's k), and the decision a hit runs refuses a
# fresh quote, and a signed minimum, above reference x (1 + min). Unset = today; <= 0, non-finite or
# malformed is refused loudly and treated as unset.
def parse_dip_retry_max_premium(raw, *, log=None):
    if raw is None or not str(raw).strip():
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float("nan")
    if not (math.isfinite(value) and value > 0):
        if log is not None:
            log("MEMECOIN_REAL_DIP_RETRY_MAX_PREMIUM_PCT=%r refused (needs a finite percent > 0); the dip "
                "retry keeps each gate's own cap" % raw)
        return None
    return value


MEMECOIN_REAL_DIP_RETRY_MAX_PREMIUM_PCT = parse_dip_retry_max_premium(
    os.getenv("MEMECOIN_REAL_DIP_RETRY_MAX_PREMIUM_PCT"),
    log=lambda text: logging.getLogger(__name__).error(text))
MEMECOIN_REAL_DIP_RETRY_WALLETS = tuple(
    w.strip().lower() for w in os.getenv("MEMECOIN_REAL_DIP_RETRY_WALLETS", "").split(",")
    if w.strip())

# DEGEN EXIT TRIAL, POLICY V4 (operator YES 2026-10-01; spec
# docs/superpowers/specs/2026-10-01-memecoin-degen-exit-trial-v4-design.md; read pre-registered in
# docs/audits/2026-10-01-degen-exit-trial-preregistration.md). Funded Solana entries of the listed
# source_user_ids, decided at or after MEMECOIN_EXIT_TRIAL_V4_FROM, freeze exit-trial-v4-v1. Empty
# list or an empty/unparseable time = off. V4's numbers are constants, NOT env knobs: the
# pre-registration forbids retuning them on trial data, and a retune is a new stamp.
MEMECOIN_EXIT_TRIAL_V4_WALLETS = frozenset(
    w.strip().lower() for w in os.getenv("MEMECOIN_EXIT_TRIAL_V4_WALLETS", "").split(",")
    if w.strip())
MEMECOIN_EXIT_TRIAL_V4_FROM = os.getenv("MEMECOIN_EXIT_TRIAL_V4_FROM", "").strip()
EXIT_TRIAL_V4_TP_PCT = 100.0
EXIT_TRIAL_V4_FLOOR_PCT = 60.0
EXIT_TRIAL_V4_MAX_HOLD_S = 800.0
MEMECOIN_REAL_DIP_RETRY_POLL_S = float(os.getenv("MEMECOIN_REAL_DIP_RETRY_POLL_S", "1"))
MEMECOIN_REAL_DIP_RETRY_MAX_HITS = int(os.getenv("MEMECOIN_REAL_DIP_RETRY_MAX_HITS", "3"))
# The EVM watch re-quotes the refusal's winning rung with this floor between ticks (spec
# 91d9d3dff), and a watch quotes at most MAX_WATCHED live rows per chain. Both bound the load the
# watch puts on the funded RPC pool and on Kyber, which exits share.
# 1.0 s, NOT FASTER, UNTIL MEASURED (Gamma M3, 2026-09-30): a hit is a full decision (4663 ladder
# p50 1,857 ms, n=77) and the dips that were fillable lasted 2.6-15 s, so a faster tick only adds
# load. Lower it only on measured exit-quote p50/p90.
MEMECOIN_REAL_DIP_RETRY_EVM_POLL_S = float(os.getenv("MEMECOIN_REAL_DIP_RETRY_EVM_POLL_S", "1.0"))
# PER CHAIN (2026-10-08, dip retry on every funded chain; design 92f34c9b7): chain 1's blocks are 12 s, so
# a 1 s tick read the same state ~12 times through the custody key. MEMECOIN_REAL_DIP_RETRY_EVM_POLL_S_<chain>
# overrides; unset chains keep MEMECOIN_REAL_DIP_RETRY_EVM_POLL_S.
_DIP_RETRY_EVM_POLL_S_DEFAULTS = {"1": 3.0}


def dip_retry_evm_poll_s_for(chain):
    key = str(chain or "").strip()
    return _chain_float("MEMECOIN_REAL_DIP_RETRY_EVM_POLL_S", key,
                        _DIP_RETRY_EVM_POLL_S_DEFAULTS.get(key, MEMECOIN_REAL_DIP_RETRY_EVM_POLL_S),
                        minimum=0.1)
MEMECOIN_REAL_DIP_RETRY_MAX_WATCHED = int(os.getenv("MEMECOIN_REAL_DIP_RETRY_MAX_WATCHED", "2"))
# Chains the dip retry runs on (network ids, e.g. "solana,4663"). EVM stays off until the operator
# names a chain (spec EVM extension, 2026-09-30). A chain also in _SHADOW_CHAINS is watched and
# logged as `shadow_hit`, and never runs a funded decision (Gamma MEDIUM: no EVM dip evidence yet).
MEMECOIN_REAL_DIP_RETRY_CHAINS = tuple(
    c.strip().lower() for c in os.getenv("MEMECOIN_REAL_DIP_RETRY_CHAINS", "solana").split(",")
    if c.strip())
MEMECOIN_REAL_DIP_RETRY_SHADOW_CHAINS = tuple(
    c.strip().lower() for c in os.getenv("MEMECOIN_REAL_DIP_RETRY_SHADOW_CHAINS", "").split(",")
    if c.strip())
# An entry refused because an EVM RPC answered 429 re-quotes after this many seconds, not at the
# 6-8 s interval or the 3 s GT cooldown: the quota it hit is the one every exit read shares.
MEMECOIN_ENTRY_RETRY_RPC_429_S = float(os.getenv("MEMECOIN_ENTRY_RETRY_RPC_429_S", "20"))
# A BUY REFUSED BEHIND OUR OWN IN-FLIGHT TRANSACTION (2026-09-28, Alpha GO). The leg refuses every
# entry on a chain while any signed reservation there is unresolved. When that transaction fails
# (three slot-lane min-out reverts on 09-28 blocked the J7 copy of the same source buy, 0.3 s
# apart), nothing re-asked. The retry lane now re-asks after this many seconds; the guard refuses
# again while the blocker is still unresolved. 0 = the refusal stays one-shot, as before.
MEMECOIN_ENTRY_RETRY_UNRESOLVED_S = float(os.getenv("MEMECOIN_ENTRY_RETRY_UNRESOLVED_S", "1.0"))
# SHOULD A CHASE-PREMIUM REFUSAL RE-QUEUE AT ALL? (2026-09-24, operator order after STOOK 64575.)
# `exact quote is +N% above source price (cap M%)` is the one retryable class where the refusal is
# about PRICE rather than about route visibility, so re-queueing follows a coin that has already
# run: STOOK refused at +47% (14:24:37Z) and +38% (14:24:48Z) and then FILLED at 14:24:59Z for
# -$101.57. The 2026-09-04 rationale for retrying it was 4663 route visibility -- a thin two-hop at
# t+5 s against a sane direct pool minutes later -- but every live instance of this exact string
# since 09-17 is solana (31 of 31), where route visibility is not the constraint.
# 1 = today's behaviour, so a rolled-back release and any environment without this line keep
# retrying. Unset reads 1. Only "1"/"true"/"yes" keep it on; "0", "false" and ANY other value
# (including a typo) turn it off, which is the safe direction for a knob that admits entries.
# Every other transient class -- visibility, RPC 429, slippage, venue_excluded, sell_sim_* -- is
# untouched by this knob.
MEMECOIN_REAL_CHASE_RETRY_ENABLED = os.getenv(
    "MEMECOIN_REAL_CHASE_RETRY_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
# EVM native-gas reserve (ETH-on-Base / BNB), in native units. Provisional placeholders — the shadow
# phase measures true fees; over-reserve until then. Approval gas is counted separately because the
# EVM leg does an exact-amount approve() before each swap (entry AND exit).
MEMECOIN_EVM_GAS_ENTRY_NATIVE = float(os.getenv("MEMECOIN_EVM_GAS_ENTRY_NATIVE", "0.0005"))
MEMECOIN_EVM_GAS_APPROVAL_NATIVE = float(os.getenv("MEMECOIN_EVM_GAS_APPROVAL_NATIVE", "0.0002"))
MEMECOIN_EVM_GAS_EXIT_NATIVE = float(os.getenv("MEMECOIN_EVM_GAS_EXIT_NATIVE", "0.0005"))
# PER-CHAIN OVERRIDE (2026-09-16, Arc build). Same shape as MEMECOIN_MAX_CONCURRENT_BY_CHAIN: any
# MEMECOIN_EVM_GAS_*_NATIVE_<CHAIN> env var scopes that one chain, unset chains keep the global
# value. Needed because gas units are chain currency, not USD -- Arc's native token is 6-decimal
# USDC-denominated, ~18 orders of magnitude cheaper per unit than Base's 18-decimal ETH, so one
# global constant cannot price both chains' gas correctly.
MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN = {
    k[len("MEMECOIN_EVM_GAS_ENTRY_NATIVE_"):]: float(v)
    for k, v in os.environ.items() if k.startswith("MEMECOIN_EVM_GAS_ENTRY_NATIVE_")
}
MEMECOIN_EVM_GAS_APPROVAL_NATIVE_BY_CHAIN = {
    k[len("MEMECOIN_EVM_GAS_APPROVAL_NATIVE_"):]: float(v)
    for k, v in os.environ.items() if k.startswith("MEMECOIN_EVM_GAS_APPROVAL_NATIVE_")
}
MEMECOIN_EVM_GAS_EXIT_NATIVE_BY_CHAIN = {
    k[len("MEMECOIN_EVM_GAS_EXIT_NATIVE_"):]: float(v)
    for k, v in os.environ.items() if k.startswith("MEMECOIN_EVM_GAS_EXIT_NATIVE_")
}


def gas_entry_native_for(chain):
    return MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN.get(
        str(chain or "").strip(), MEMECOIN_EVM_GAS_ENTRY_NATIVE)


def gas_approval_native_for(chain):
    return MEMECOIN_EVM_GAS_APPROVAL_NATIVE_BY_CHAIN.get(
        str(chain or "").strip(), MEMECOIN_EVM_GAS_APPROVAL_NATIVE)


def gas_exit_native_for(chain):
    return MEMECOIN_EVM_GAS_EXIT_NATIVE_BY_CHAIN.get(
        str(chain or "").strip(), MEMECOIN_EVM_GAS_EXIT_NATIVE)


def _native_wei_env(name, default):
    """Read one native-unit configuration value as exact wei."""
    try:
        value = Decimal(os.getenv(name, default))
    except InvalidOperation as exc:
        raise ValueError(name + " must be a decimal native amount") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(name + " must be a non-negative native amount")
    return int(value * Decimal(10 ** 18))


# CHAINS THAT MUST NAME THEIR OWN GAS RESERVE AMOUNTS (Alpha, 2026-09-30). The global
# MEMECOIN_EVM_GAS_*_NATIVE values are tuned to an L2 (entry 0.00005 ETH); on chain 1 they would
# read as a reserve and be some 50x too small. So chain 1 does not inherit them: until its three
# amounts are set from the fork rehearsal's measured gas units, its entries refuse. Absent is not
# zero, and it is not another chain's number.
_EVM_GAS_NATIVES_REQUIRED = frozenset({"1"})


def evm_gas_natives_unset(chain):
    """Names of the per-chain gas reserve amounts `chain` must set and has not. () elsewhere."""
    key = str(chain or "").strip()
    if key not in _EVM_GAS_NATIVES_REQUIRED:
        return ()
    return tuple("MEMECOIN_EVM_GAS_%s_NATIVE_%s" % (name, key) for name, table in (
        ("ENTRY", MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN),
        ("APPROVAL", MEMECOIN_EVM_GAS_APPROVAL_NATIVE_BY_CHAIN),
        ("EXIT", MEMECOIN_EVM_GAS_EXIT_NATIVE_BY_CHAIN)) if key not in table)


# Reserve native gas before an entry. Zero preserves the calculated book reserve.
MEMECOIN_EVM_GAS_RESERVE_MIN_NATIVE_BY_CHAIN = {
    k[len("MEMECOIN_EVM_GAS_RESERVE_MIN_NATIVE_"):]: _native_wei_env(k, "0") / 10 ** 18
    for k in os.environ if k.startswith("MEMECOIN_EVM_GAS_RESERVE_MIN_NATIVE_")
}


def gas_reserve_min_native_for(chain):
    return MEMECOIN_EVM_GAS_RESERVE_MIN_NATIVE_BY_CHAIN.get(str(chain or "").strip(), 0.0)


MEMECOIN_EVM_GAS_TOPUP_ENABLED = os.getenv(
    "MEMECOIN_EVM_GAS_TOPUP_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_EVM_GAS_TOPUP_PERIOD_S = float(
    os.getenv("MEMECOIN_EVM_GAS_TOPUP_PERIOD_S", "300"))
MEMECOIN_EVM_GAS_TOPUP_LOW_WEI = _native_wei_env(
    "MEMECOIN_EVM_GAS_TOPUP_LOW_NATIVE", "0.002")
MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI = _native_wei_env(
    "MEMECOIN_EVM_GAS_TOPUP_TARGET_NATIVE", "0.003")
MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_WEI = _native_wei_env(
    "MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_NATIVE", "0.010")
MEMECOIN_EVM_GAS_TOPUP_DAILY_CAP_WEI = _native_wei_env(
    "MEMECOIN_EVM_GAS_TOPUP_DAILY_CAP_NATIVE", "0")


def evm_gas_topup_levels_for(chain):
    """{low_wei, target_wei, min_weth_reserve_wei, daily_cap_wei} for one chain's top-up, or None.

    4663 keeps the global MEMECOIN_EVM_GAS_TOPUP_* values. Every other chain must set its own
    `MEMECOIN_EVM_GAS_TOPUP_{LOW,TARGET,MIN_WETH_RESERVE}_NATIVE_<chain>`: None while one is unset,
    malformed, or when the target is not above the low mark. Chain-1 gas is some 50x an L2's, so
    another chain's levels are not a default."""
    key = str(chain or "").strip()
    if key == "4663":
        return {"low_wei": MEMECOIN_EVM_GAS_TOPUP_LOW_WEI,
                "target_wei": MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI,
                "min_weth_reserve_wei": MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_WEI,
                "daily_cap_wei": MEMECOIN_EVM_GAS_TOPUP_DAILY_CAP_WEI}
    out = {}
    for field, name in (("low_wei", "LOW"), ("target_wei", "TARGET"),
                        ("min_weth_reserve_wei", "MIN_WETH_RESERVE")):
        env = "MEMECOIN_EVM_GAS_TOPUP_%s_NATIVE_%s" % (name, key)
        if not str(os.getenv(env, "") or "").strip():
            return None
        try:
            out[field] = _native_wei_env(env, "0")
        except ValueError:
            return None
    if out["low_wei"] <= 0 or out["target_wei"] <= out["low_wei"]:
        return None
    try:
        out["daily_cap_wei"] = _native_wei_env(
            "MEMECOIN_EVM_GAS_TOPUP_DAILY_CAP_NATIVE_%s" % key, "0")
    except ValueError:
        return None
    return out
# NATIVE FLOAT REFILL (2026-09-28, Option B). On a MEMECOIN_EVM_NATIVE_FLOAT_CHAINS chain the gas
# pass refills native up to the TARGET whenever native falls below this, so the float that lets an
# entry skip its WETH unwrap is there again for the next entry. The float check itself still leaves
# GAS_TOPUP_LOW behind. 0 (default) = off; only LOW < value < TARGET takes effect.
MEMECOIN_EVM_NATIVE_FLOAT_REFILL_BELOW_WEI = _native_wei_env(
    "MEMECOIN_EVM_NATIVE_FLOAT_REFILL_BELOW_NATIVE", "0")
# GAS TOP-UP CHAINS (2026-09-30, operator via Alpha; BINF 6fc71d90 refused "WBNB balance $0.01").
# The pass above (top-up unwrap, and the reclaim that wraps native above the target back into the
# quote token) ran on 4663 only, hard-coded in memecoin_runner_loop. A flap Portal exit on 56 pays
# native BNB while entries spend WBNB, so nothing ever moved it back. "4663" = today's pass. Any
# other chain takes its bounds from its own gas settings (memecoin_evm_gas_topup.gas_pass_bounds),
# never from the 4663 ETH values above. Rollback: drop the chain from the list. Same name and
# reader as beta/eth-mainnet's chain-1 top-up (808b0bc37), so the two share one knob.
def memecoin_evm_gas_topup_chains():
    """Chains the WETH <-> native gas pass runs on. MEMECOIN_EVM_GAS_TOPUP_CHAINS, default 4663."""
    return tuple(dict.fromkeys(
        c.strip() for c in os.getenv("MEMECOIN_EVM_GAS_TOPUP_CHAINS", "4663").split(",")
        if c.strip()))


# On those other chains the pass keeps the wallet lock until its own wrap/unwrap has a receipt, up
# to this many seconds, so the next entry never measures its WETH delta across it. An entry that
# arrives meanwhile waits at most this long plus the pass's in-lock reads (2 s deadline each).
MEMECOIN_EVM_GAS_TOPUP_HOLD_S = float(os.getenv("MEMECOIN_EVM_GAS_TOPUP_HOLD_S", "3"))
# The maximum round-trip buy and sell loss is 15%.
# The value includes slippage and token tax.
MEMECOIN_EVM_MAX_TAX_PCT = float(os.getenv("MEMECOIN_EVM_MAX_TAX_PCT", "15"))
# The tax ALLOWANCE the entry's round-trip COST measurement assumes on a V2 sell-back (2026-09-13).
# Not the exit floor above: measuring cost with the 15% worst case made every V2 coin cost >= 15%.
# 4 = 2% a side, the highest real tax among the coins that measurement refused.
MEMECOIN_EVM_ROUNDTRIP_TAX_ALLOWANCE_PCT = float(os.getenv("MEMECOIN_EVM_ROUNDTRIP_TAX_ALLOWANCE_PCT", "4"))
# The tax a V2 EXIT is VALUED at (2026-09-27): what the stop, TP, catastrophe and de-risk rails see.
# The signed minimum output keeps MEMECOIN_EVM_MAX_TAX_PCT. Valuing on that 15% cut read every V2
# bag ~15% low; real sell taxes on 14 BSC exits were 1.0-5.0% (median 1.0%). Rollback: set it to
# MEMECOIN_EVM_MAX_TAX_PCT or higher, which restores the cut value and skips the extra read.
MEMECOIN_EVM_EXIT_VALUE_TAX_PCT = float(os.getenv("MEMECOIN_EVM_EXIT_VALUE_TAX_PCT", "4"))
# EVM custody + routing creds (SEPARATE wallet from Solana, 0600 outside repo). Never hardcode the
# RPC credentials remain env-only. Per-chain RPC override via MEMECOIN_EVM_RPC_URL_<id>.
MEMECOIN_EVM_KEYPAIR = os.getenv(
    "MEMECOIN_EVM_KEYPAIR", str(state_path("data", "evm_keypair.json")))


def evm_rpc_for_chain(chain_id):
    """Per-chain RPC url: MEMECOIN_EVM_RPC_URL_<chain> overrides the generic MEMECOIN_EVM_RPC_URL.
    Read at call time so it tracks env. '' if unset -> the EVM leg for that chain stays a no-op."""
    return (os.getenv(f"MEMECOIN_EVM_RPC_URL_{chain_id}")
            or os.getenv("MEMECOIN_EVM_RPC_URL", ""))


# How stale the market-data provider's window is, in seconds, before it can hold a swap. Below this
# age a source buy is treated as absent from the provider's number and is never subtracted from it.
#
# 15 s is the SMALLEST per-trade index lag measured on any venue of any chain (2026-09-12, 7 pools
# and 149 trades matched against the chain within 7%): 4663 p50 25 s / p90 36 s / max 47 s, Solana
# pumpswap ~15 s, Meteora DLMM ~19 s, Raydium CPMM ~30 s, with 0-30 s of Cloudflare cache on top of
# all of them. It is deliberately the LOW end of that range and not the middle: suppressing the
# subtraction loosens a funded gate, so the guard only fires where non-inclusion is near-certain,
# and one measured floor also covers the chains that have no measurement of their own.
#
# THIS DEFAULT WAS 0 FROM 2026-09-16 TO 2026-09-23 AND THAT MADE THE GUARD DEAD. The rule needs a
# positive lag to fire and `MEMECOIN_PROVIDER_INDEX_LAG_S_<chain>` is set for no chain in .env, so
# every caller passed 0 and the arithmetic the rule exists to prevent ran on every attempt. Measured
# over 839 volume-floor refusals carrying gate inputs since 2026-08-24: 40 attempts on 33 distinct
# coins (4.8%) had a raw provider volume already at or above their own threshold and were refused
# only by this subtraction, at a source age of p50 3.82 s.
PROVIDER_INDEX_LAG_S_DEFAULT = 15.0


def provider_index_lag_s(chain_id):
    """How long the market-data provider takes to index a swap on this chain, in seconds.

    Absent, zero and unreadable are three different answers. An unset variable means the measured
    default above. An explicit 0 is the operator turning the guard OFF for that chain, and is the
    rollback for this behaviour without a deploy. An unreadable value falls back to the default
    rather than to 0, because unknown must not silently mean unguarded: a typo in one env line
    would otherwise restore the exact defect this default fixes.

    WARNING for the revival watch. The entry gate passes `source_age_s` at
    `memecoin_real_runner.py:2477`. The revival call does not pass this value, so the guard cannot
    fire there and the revival lane keeps the old arithmetic. That makes it STRICTER than the gate
    that refused, which is the safe direction: the setting can only refuse a revival, never admit
    one.
    """
    raw = os.getenv("MEMECOIN_PROVIDER_INDEX_LAG_S_%s" % chain_id)
    if raw is None or str(raw).strip() == "":
        return PROVIDER_INDEX_LAG_S_DEFAULT
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return PROVIDER_INDEX_LAG_S_DEFAULT


def evm_tickstream_rpc_for_chain(chain_id):
    """Use a separate RPC for the observation tape when configured."""
    dedicated = os.getenv(f"MEMECOIN_TICKSTREAM_RPC_URL_{chain_id}")
    if str(chain_id) == "4663":
        custody = evm_rpc_for_chain(chain_id)
        if not dedicated or dedicated.rstrip("/") == custody.rstrip("/"):
            return ""
        return dedicated
    return dedicated or evm_rpc_for_chain(chain_id)


# Direct on-chain Uniswap-V3 execution for chain 4663 (2026-07-16). These are the chain-4663
# Uniswap V3 periphery addresses,
# ALL verified on-chain (factory()-matched + selectors exercised live) — see
# docs/research/2026-07-16-robinhood-4663-dex-venue.md. Overridable via env per chain.
MEMECOIN_EVM_V3_QUOTER = {"4663": os.getenv(
    "MEMECOIN_EVM_V3_QUOTER_4663", "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"),
    "8453": os.getenv("MEMECOIN_EVM_V3_QUOTER_8453", "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"),
    # PancakeSwap V3 QuoterV2 on 56. Its `factory()` returns the V3 factory below, proved on chain.
    "56": os.getenv("MEMECOIN_EVM_V3_QUOTER_56", "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997"),
    # Arc (5042, 2026-09-16). Cited from Uniswap Labs' own unified deployments feed
    # (docs.uniswap.org/deployments.json), tier="labs-supported" status="active". Verified: real
    # code (8,273 bytes) on the operator's dedicated Arc RPC, and a live quoteExactInputSingle
    # (1 USDC -> ARGUS, fee=10000) returned $0.032235/ARGUS against DexScreener's contemporaneous
    # $0.03195 -- 0.89% apart.
    "5042": os.getenv("MEMECOIN_EVM_V3_QUOTER_5042", "0x7DfD4F31be6814D2906BDE155c3e1B146EAc1468"),
    "57073": os.getenv("MEMECOIN_EVM_V3_QUOTER_57073", "0x96b572D2d880cf2Fa2563651BD23ADE6f5516652"),
    # Ethereum mainnet (2026-09-30, eth-mainnet spec): QuoterV2, 8,273 bytes; factory() ->
    # 0x1F98431c... == SwapRouter02.factory(), proved on chain.
    "1": os.getenv("MEMECOIN_EVM_V3_QUOTER_1", "0x61fFE014bA17989E743c5F6cB21bF9697530B21e")}
MEMECOIN_EVM_V3_ROUTER = {"4663": os.getenv(
    "MEMECOIN_EVM_V3_ROUTER_4663", "0xcaf681a66d020601342297493863e78c959e5cb2"),
    # Base canonical Uniswap V3 (2026-07-17: the zerox retirement orphaned 8453 — 115 armed
    # signals/6h died at 'no configured leg'; chain currently ~unfunded, leg ready for funding)
    "8453": os.getenv("MEMECOIN_EVM_V3_ROUTER_8453", "0x2626664c2603336E57B271c5C0b26F421741e481"),
    "56": os.getenv("MEMECOIN_EVM_V3_ROUTER_56", "0x1b81D678ffb9C0263b24A97847620C99d213eB14"),
    # Arc (5042, 2026-09-16). Same source and verification as the quoter above: SwapRouter02,
    # tier="labs-supported" status="active", 24,497 bytes of real code on-chain.
    "5042": os.getenv("MEMECOIN_EVM_V3_ROUTER_5042", "0x53BF6B0684Ec7eF91e1387Da3D1a1769bC5A6F77"),
    "57073": os.getenv("MEMECOIN_EVM_V3_ROUTER_57073", "0x177778F19E89dD1012BdBe603F144088A95C4B53"),
    # Ethereum mainnet SwapRouter02, 24,497 bytes; WETH9() == the chain-1 quote token.
    "1": os.getenv("MEMECOIN_EVM_V3_ROUTER_1", "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45")}
# Immutable V3 bridge metadata. Base memecoin pools overwhelmingly quote in canonical WETH while
# custody is canonical USDC, so a single-hop USDC/token quote misses the route. The USDC/WETH 500
# pool is the fixed first/last rung; the token-side fee is still scanned from V3_FEE_TIERS.
_BSC_V3_USDT = "0x55d398326f99059ff775485246999027b3197955"
_BSC_V3_CONNECTOR = os.getenv("MEMECOIN_EVM_V3_CONNECTOR_56", _BSC_V3_USDT)
MEMECOIN_EVM_V3_CONNECTORS = {
    "4663": (("0x39dBED3a2bd333467115dE45665cC57F813C4571", 3000),),
    # THE CONNECTOR IS THE NON-QUOTE CURRENCY, and this default had to move with the quote
    # currency (2026-09-06). It was WETH while Base quoted in USDC, which bridged USDC -> WETH ->
    # coin. With the Base bankroll now WETH, `dex_v3_quote` skips any connector equal to the sell
    # token, so a WETH connector left Base with NO two-hop V3 path at all -- and a USDC-paired
    # Base coin unreachable from the bankroll. USDC is the mirror value; fee 500 is the same
    # WETH/USDC tier this entry always named.
    # WBNB/USDT fee 100 pool verified on chain 2026-10-03: 0x172fcd41... gave 2.42% more
    # bridge output than fee 10000 at AIKOL's clip. Replace the fee in the same batch, not
    # the coin-side tier scan. Custom connectors retain their prior default; explicit fees win.
    "56": ((_BSC_V3_CONNECTOR,
            int(os.getenv("MEMECOIN_EVM_V3_CONNECTOR_FEE_56",
                          "100" if _BSC_V3_CONNECTOR.lower() == _BSC_V3_USDT else "10000"))),),
    "8453": ((os.getenv("MEMECOIN_EVM_V3_CONNECTOR_8453",
                         "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"),
              int(os.getenv("MEMECOIN_EVM_V3_CONNECTOR_FEE_8453", "500"))),),
    # Ethereum mainnet: the non-quote currency (USDC) with the WETH/USDC 0.05% tier, as on Base.
    "1": ((os.getenv("MEMECOIN_EVM_V3_CONNECTOR_1",
                      "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),
           int(os.getenv("MEMECOIN_EVM_V3_CONNECTOR_FEE_1", "500"))),),
}
# STEP 0 (2026-08-30), default OFF. Quote V4 pools whose currency is the WETH ERC-20 as well as
# native. We unwrap WETH to native and quote native only, so a WETH-token pool is unreachable.
# Measured on armed-wallet 4663 coins (n=22): 9% have ONLY a sane WETH-token pool while their
# native pools are fee traps. Off until a two-direction round trip is measured on a live coin.
MEMECOIN_EVM_V4_WETH_CURRENCY = os.getenv(
    "MEMECOIN_EVM_V4_WETH_CURRENCY", "0").strip().lower() in {"1", "true", "yes"}
# Per-chain override for the flag above (2026-09-16, Arc build). Arc's quote currency is an ERC-20
# (USDC), not native, so it needs this ON to quote at all -- but the global flag also changes pool
# selection on 4663/8453, an unassessed blast radius. This set turns it on for named chains only,
# leaving MEMECOIN_EVM_V4_WETH_CURRENCY (and every chain not listed here) untouched.
MEMECOIN_EVM_V4_WETH_CURRENCY_CHAINS = {c.strip() for c in os.getenv(
    "MEMECOIN_EVM_V4_WETH_CURRENCY_CHAINS", "").split(",") if c.strip()}

# Chains where the pre-entry sell check quotes the exact expected bought amount and refuses
# outright on a miss there (2026-09-16). Default "5042": Arc's launch-time hook can block a sell
# outright, and the 1-token/$5 fallback probes can pass on dust while a real clip-sized exit
# can't -- a live false negative, reproduced on a real Arc V4 pool. A chain NOT listed here keeps
# the pre-existing behaviour byte-for-byte: a clip-size miss still falls through to the smaller
# probes, exactly as it did before this check existed. No hook-cap evidence was found on 4663
# (checked directly, 2026-09-16) -- widen this set only on that chain's own live evidence, not by
# assuming Arc's finding generalizes.
# POST-APPROVAL RE-QUOTE THROUGH THE WINNER ONLY (2026-09-28, default OFF; operator flips it). After a
# confirmed exact-amount approval, entry step 3a re-quotes before signing. Today that re-runs the
# whole ladder with its post-hit grace: since 09-06, Base p50 1565 / p90 2551 ms (n=3) against the
# winning rung's own p50 533 / p90 848 ms; BSC p50 419 / p90 1816 ms (n=39) against 642 / 941 ms.
# 1 = re-ask only the winning rung (`_requote`: same amount_atomic, same slippage); a None, an error
# or a mismatched answer falls back to the ladder. Every post-approval gate still runs on the fresh
# quote. Entry only: exits share `_approve` but not this re-quote.
MEMECOIN_EVM_POST_APPROVAL_WINNER_REQUOTE = os.getenv(
    "MEMECOIN_EVM_POST_APPROVAL_WINNER_REQUOTE", "0").strip().lower() in {"1", "true", "yes"}

MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS = {c.strip() for c in os.getenv(
    "MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS", "5042").split(",") if c.strip()} | {"57073"}

_INK_REQUIRED_CAPABILITIES = (
    "market_data", "fee_accounting", "manual_recovery", "state_coherence",
    "token_safety", "canonical_receipts", "source_coverage", "route_full_exit",
    "recovery")


@functools.lru_cache(maxsize=1)
def _ink_capability_manifest():
    path = (Path(__file__).resolve().parents[2] / "docs" / "superpowers" / "research" /
            "2026-09-29-ink-coverage-manifest.json")
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def ink_capability_reason(manifest=None, *, preconfirmed=None):
    """One pinned, local G0 gate. No environment flag can certify an unsupported route."""
    manifest = _ink_capability_manifest() if manifest is None else manifest
    if not isinstance(manifest, dict) or manifest.get("chain_id") != 57073:
        return "ink_manifest_invalid"
    if manifest.get("funded_activation") != "ready":
        return "ink_activation_blocked"
    preconfirmed_enabled = (MEMECOIN_INK_PRECONFIRMED_ADMISSION_ENABLED
                            if preconfirmed is None else preconfirmed)
    if preconfirmed_enabled:
        return "ink_preconfirmed_admission_unsupported"
    caps = manifest.get("required_capabilities") or {}
    for name in _INK_REQUIRED_CAPABILITIES:
        if caps.get(name) != "supported_tested":
            return f"ink_{name}_unsupported"
    if not any(row.get("state") == "supported_tested" and
               all(row.get(name) == "supported_tested" for name in
                   ("entry_proof", "exit_proof", "simulation_proof"))
               for row in manifest.get("venues") or () if isinstance(row, dict)):
        return "ink_route_unsupported"
    return None

# ETHEREUM MAINNET COVERAGE MANIFEST (2026-09-30, eth-mainnet spec decision 5). The Ink pattern,
# with the capabilities chain 1 lacks today. A funded chain-1 entry refuses by name until every one
# is `supported_tested` in the pinned file and `funded_activation` is `ready`; allowlisting "1"
# alone never trades. `submission` waits for the operator's MEV choice (spec decision 6): until
# then a send would reach the public mempool through custody.
_ETH_REQUIRED_CAPABILITIES = (
    "submission", "route_full_exit", "v4_pool_keys", "gas_guard", "fee_accounting", "recovery")


@functools.lru_cache(maxsize=1)
def _eth_capability_manifest():
    path = (Path(__file__).resolve().parents[2] / "docs" / "superpowers" / "research" /
            "2026-09-30-eth-coverage-manifest.json")
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def eth_capability_reason(manifest=None):
    """One pinned, local gate for chain 1. No environment flag can certify a missing capability."""
    manifest = _eth_capability_manifest() if manifest is None else manifest
    if not isinstance(manifest, dict) or manifest.get("chain_id") != 1:
        return "eth_manifest_invalid"
    if manifest.get("funded_activation") != "ready":
        return "eth_activation_blocked"
    caps = manifest.get("required_capabilities") or {}
    for name in _ETH_REQUIRED_CAPABILITIES:
        if caps.get(name) != "supported_tested":
            return f"eth_{name}_unsupported"
    return None


def chain_capability_reason(chain):
    """The pinned manifest refusal for a chain that has one (57073, 1), else None."""
    chain = str(chain)
    if chain == "57073":
        return ink_capability_reason()
    if chain == "1":
        return eth_capability_reason()
    return None


# Chain-4663 launchpad-curve venue (spec 2026-09-17), default OFF. Buys and sells a launchpad
# coin on its own bonding curve, before it graduates to its Uniswap V4 pool -- see
# sniper/execution/evm_launchpad_curve.py. Its own whole-balance quote (getReserves()+feeBps(),
# verified against eth_simulateV1 within 0.1%) IS this rung's clip-size safety check; it does not
# use MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS, which is scoped to the V3/V4 dust-probe rungs only and
# must stay byte-identical on every chain not explicitly widened by its own live evidence.
MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED = os.getenv(
    "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# Its token safety reads once per check (operator 2026-10-14 via Alpha, cuts A+B+C), default OFF:
# curve_state and getReserves once, handed to the entry and exit quotes (OUTPOST: 8 state calls and
# 2 reserve calls), and the ETH price from the executor's mark when <= 1 s old instead of a live
# Binance GET. Read per check; 0 is today's reads. The per-read marks (curve_reads) run either way.
MEMECOIN_EVM_CURVE_SAFETY_ONE_READ = os.getenv(
    "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", "0").strip().lower() in {"1", "true", "yes"}

# Chain-56 flap.sh curve venue (2026-09-20), default OFF, NATIVE-QUOTE MINTS ONLY. Buys and
# sells a flap.sh launchpad coin on the shared Portal contract's own bonding curve -- see
# sniper/execution/evm_flap_curve.py. Phase 1: 10 of the 13 flap.sh coins reviewed on 2026-09-20
# quote against a non-native ERC-20 (ten different tokens), and the module refuses those by name
# rather than mis-quote them; that is a separate, unbuilt phase 2. Its own whole-balance quote
# (getTokenV6-derived reserves and per-token fee, verified against real swapExactInput eth_call
# within 0.1%) IS this rung's clip-size safety check, same reasoning as
# MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED.
MEMECOIN_EVM_FLAP_CURVE_ENABLED = os.getenv(
    "MEMECOIN_EVM_FLAP_CURVE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# Let the flap.sh rung price a curve whose quote token is NOT native, for the tokens whose Portal
# converts native itself (`nativeToQuoteSwapEnabled`). Default OFF: the mechanism is proven on
# chain in both directions, but no on-curve token of this class has been entered end to end yet.
# A JUPITER ROUTE THROUGH A DRAINED PUMP.FUN CURVE IS NOT A LIQUIDITY JUDGEMENT (2026-09-23).
# GIGACAT graduated seven minutes after launch; Jupiter had not yet indexed the new AMM pool, so
# its only route ran through the bonding curve, which held 0.00141732 SOL. The impact cap then read
# 87.16% and refused -- correctly for that route, because that route really did cost 87%. The same
# $175 quoted at 0.0468% once Jupiter found the AMM pool. So the refusal is right and the CODE is
# wrong: this is a transient routing gap, not a coin we should decline, and it belongs in the
# re-quote lane rather than in a terminal refusal.
MEMECOIN_SOL_CURVE_GRADUATION_RERESOLVE = os.getenv(
    "MEMECOIN_SOL_CURVE_GRADUATION_RERESOLVE", "1").strip().lower() in {"1", "true", "yes"}
# Below this the curve is drained and cannot be the venue. GIGACAT's held 0.0014 SOL; a live curve
# at a $47k market cap holds tens of SOL, so the floor is nowhere near either case.
MEMECOIN_SOL_CURVE_DRAINED_SOL = float(
    os.getenv("MEMECOIN_SOL_CURVE_DRAINED_SOL", "0.5") or 0.5)

# THE EXIT DUST-ROUTE GUARD (2026-10-02, operator "fix everything" via Alpha; Gamma MED; rebuilt
# 2026-10-06, operator ruling b2ae12c0). An exit whose PRE-slippage quote values what it sells at or
# under MEMECOIN_EXIT_DUST_FRACTION of its reference (the last good executable price younger than
# MEMECOIN_EXIT_DUST_REF_MAX_AGE_S, else the entry price) is HELD, not signed, unless our own newest
# market read shows the market down too (sniper/services/memecoin_exit_dust_guard.py). No own read
# (8453, 56 today): HELD and paged. DAODUN 68544: Kyber valued a ~$44 bag at $0.16 and the runner
# signed 9 sells on it. 0 = off.
MEMECOIN_EXIT_DUST_GUARD = os.getenv(
    "MEMECOIN_EXIT_DUST_GUARD", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_EXIT_DUST_FRACTION = float(os.getenv("MEMECOIN_EXIT_DUST_FRACTION", "0.10") or 0.10)
MEMECOIN_EXIT_DUST_REF_MAX_AGE_S = float(os.getenv("MEMECOIN_EXIT_DUST_REF_MAX_AGE_S", "3600") or 3600)

MEMECOIN_EVM_FLAP_NATIVE_SWAP_ENABLED = os.getenv(
    "MEMECOIN_EVM_FLAP_NATIVE_SWAP_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# THE FLAP QUOTE-TOKEN EXIT ON THE RUNNER (2026-10-02, option B; operator yes for =1 via Alpha). A
# funded chain-56 flap.sh coin quoted in a token with native swap off (the class the ladder refuses,
# DAODUN 68544) exits through sniper/execution/evm_flap_quote_exit.py: the Portal sells it for its
# quote token, then exactly that delta goes to WBNB on Pancake. For an eligible coin it runs INSTEAD
# of the ladder, whose aggregator route for such a bag is dust. 0 = the ladder only, as before.
MEMECOIN_EVM_FLAP_QUOTE_EXIT = os.getenv(
    "MEMECOIN_EVM_FLAP_QUOTE_EXIT", "0").strip().lower() in {"1", "true", "yes"}

# B: V4 MULTI-HOP through a bridge currency (spec 2026-08-30 Amendment 5). Default OFF.
# 31% of armed-wallet 4663 coins have no sane ETH-side pool but do have a sane USDG-quoted one.
MEMECOIN_EVM_V4_MULTIHOP_ENABLED = os.getenv(
    "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_EVM_RELAY_ENABLED = os.getenv(
    "MEMECOIN_EVM_RELAY_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# Minimum coin-leg depth as a MULTIPLE OF THE CLIP. Measured 2026-08-30 at a $32.81 clip:
#   11x  -> -15.51% round trip   BAD
#   147x ->  -5.52%              GOOD
#   277x ->  -6.34%              GOOD
# EVERYTHING BETWEEN 11x AND 147x IS UNMEASURED. 100x sits inside the measured-good region and
# refuses the unmeasured band rather than guessing inside it; a floor that starts conservative and
# RELAXES on evidence is recoverable, one that starts optimistic and tightens after a loss is not.
# Lower it as evidence accumulates, never on the argument that the gap is probably fine.
MEMECOIN_EVM_V4_MULTIHOP_MIN_DEPTH_CLIPS = float(os.getenv(
    "MEMECOIN_EVM_V4_MULTIHOP_MIN_DEPTH_CLIPS", "100"))
# GAS FOR A V4 MULTIHOP SWAP (2026-09-04). Was hardcoded at 900,000 and FROGE 50786 consumed
# 899,687 of it -- the entire limit -- so the trailing WRAP_ETH got 17,432 gas and ran out.
# `debug_traceTransaction` (QuickNode; Alchemy's free tier refuses the method) shows the V4 swap
# itself SUCCEEDING through the PoolManager and only the WETH call failing:
#
#   CALL UniversalRouter                 ERROR=execution reverted
#     CALL PoolManager                   <- every swap leg fine
#     CALL WETH gas=17432                ERROR=execution reverted
#       DELEGATECALL 0xc6b81b4297        ERROR=out of gas
#
# This chain's WETH is a PROXY, so deposit() delegatecalls and costs far more than a standard
# WETH9. Replaying the exact reverted calldata: 900,000 reverts, 1,200,000 succeeds. The position
# was un-exitable for 34 minutes and five attempts because of one constant.
#
# Headroom over the measured 899,687 rather than a tight fit: the swap cost scales with hops and
# hook work, and an exit that cannot afford its own wrap strands money. Unused gas is refunded.
MEMECOIN_EVM_V4_MULTIHOP_GAS = int(os.getenv("MEMECOIN_EVM_V4_MULTIHOP_GAS", "1500000"))
# FLAP PORTAL SWAPS CARRY THEIR OWN GAS LIMIT (2026-09-30, BINF 67604 on chain 56). The Portal's
# `swapExactInput` tx carried no "gas", so the executor's default of 500,000 applied. A graduated
# flap coin's sell routes through the Portal to the DEX plus the token's tax swap-back: receipt
# 0xb7450a3b... reverted OUT OF GAS at 478,432 of 500,000 with no logs, while eth_estimateGas on
# the same calldata gave 874,137 and the uncapped eth_call paid 0.1238 BNB. Every stuck-bag retry
# failed the same way. Set in the Portal tx itself (buy and sell), so the exit gate's native-balance
# check prices the real limit. Unused gas is refunded.
MEMECOIN_EVM_FLAP_PORTAL_GAS = int(os.getenv("MEMECOIN_EVM_FLAP_PORTAL_GAS", "1500000"))
# EVERY ROUTE BUILDER CARRIES ITS OWN GAS LIMIT (2026-09-30, Gamma's sweep of every funded EVM
# fill 09-16..09-30). The V2/V3 (dex_v3_quote: uniswap_v2/v3, pancake_v3, ramses_v3), V2 fee-on-
# transfer, mixed V3+V2 and launchpad-curve transactions carried no "gas", so the signer used its
# 500,000 default. Tightest measured: 56 pcs_mixed_v3_v2 382,080 of 500,000; 56 v2 338,605; and
# BINF's taxed graduated sell needed 874,137 (the flap Portal, above). 900,000 matches the relay
# and Kyber floor. Unused gas is refunded; the entry and exit gates' native checks price it.
MEMECOIN_EVM_DEFAULT_ROUTE_GAS = int(os.getenv("MEMECOIN_EVM_DEFAULT_ROUTE_GAS", "900000"))
# KYBER (2026-09-30): a 4663 sell (INUTEL 65700) reverted at 2,840,977 of the fixed 3,000,000, then
# filled at 2.08M; the buy took 1.5x Kyber's estimate. Sells now 5,000,000, buys 2.0x the estimate
# (floor 900,000, cap 3,000,000 unchanged, in evm_kyber_quote).
MEMECOIN_EVM_KYBER_SELL_GAS = int(os.getenv("MEMECOIN_EVM_KYBER_SELL_GAS", "5000000"))
MEMECOIN_EVM_KYBER_BUY_GAS_MULT = float(os.getenv("MEMECOIN_EVM_KYBER_BUY_GAS_MULT", "2.0"))
# TAX-AWARE ENTRY MINIMUM (2026-09-30, operator: "make the BSC tax aware"; spec
# 2026-09-30-memecoin-evm-tax-aware-entry-min-out-design.md, Gamma CLEAN, Alpha: pool-bound). A V2
# fee-on-transfer buy checks what the wallet RECEIVED against a minimum set from getAmountsOut,
# which cannot see the coin's transfer tax. Where the source's own receipt decode measured the tax
# on the SAME pair our route buys from, the signed minimum drops by that tax, capped. Empty = off.
MEMECOIN_EVM_TAX_AWARE_MIN_OUT_CHAINS = frozenset(
    c.strip() for c in os.getenv("MEMECOIN_EVM_TAX_AWARE_MIN_OUT_CHAINS", "").split(",") if c.strip())
MEMECOIN_EVM_BUY_TAX_CAP_PCT = float(os.getenv("MEMECOIN_EVM_BUY_TAX_CAP_PCT", "10"))
# N-LEG ROUTE (2026-09-05). A launchpad coin on this chain often has one real pool paired with a
# tokenized stock, and that stock has no fixed-fee ETH pool. Two legs cannot reach it. Measured on
# 2026-09-05: PH costs -12.7% through two legs (LLY/ETH, $28k, 5% fee) and -3.4% through three
# (ETH -> USDG -> LLY -> PH). MAX_LEGS caps the search. DEFAULT 2 KEEPS THE SEARCH INERT: a
# three-leg candidate is never built until an operator raises this, so the merge changes nothing.
MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS = int(os.getenv("MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS", "2"))
# ROUTE SEARCH BOUNDS (2026-09-17, LuluDior 0x46f3c721). The search measures up to POOLS_PER_STEP
# pools at each step and up to MAX_PATHS complete paths, under one deadline and one request count
# for the whole search. The deadline stays below MEMECOIN_EVM_LADDER_WAIT_S (8 s). A request that
# is in progress is not cancelled; the bounds only stop new requests. Pool discovery may not use the
# part of the request count reserved for path quotes (two per path, plus one block read).
MEMECOIN_EVM_V4_MULTIHOP_POOLS_PER_STEP = int(os.getenv("MEMECOIN_EVM_V4_MULTIHOP_POOLS_PER_STEP", "3"))
MEMECOIN_EVM_V4_MULTIHOP_MAX_PATHS = int(os.getenv("MEMECOIN_EVM_V4_MULTIHOP_MAX_PATHS", "12"))
MEMECOIN_EVM_V4_MULTIHOP_SEARCH_DEADLINE_S = float(os.getenv(
    "MEMECOIN_EVM_V4_MULTIHOP_SEARCH_DEADLINE_S", "5"))
MEMECOIN_EVM_V4_MULTIHOP_REQUEST_BUDGET = int(os.getenv("MEMECOIN_EVM_V4_MULTIHOP_REQUEST_BUDGET", "100"))
# Seconds of the search deadline that pool discovery leaves for the fixed-block read and path quotes.
MEMECOIN_EVM_V4_MULTIHOP_QUOTE_TIME_S = float(os.getenv("MEMECOIN_EVM_V4_MULTIHOP_QUOTE_TIME_S", "1.5"))
# Seconds to wait for a confirmed WETH unwrap's native ETH to be VISIBLE on every healthy pool
# member before the swap that spends it is broadcast. A node admits gas*price+value against its own
# committed state, and 4663 members run 5-8 blocks apart, so a swap sent 3 ms after the unwrap
# receipt was rejected for insufficient funds (PAWSINU 0xbcc948e7, 2026-09-17). 0 = no wait.
# HOW LONG TO WAIT FOR A CONFIRMED UNWRAP'S ETH TO BE VISIBLE ON EVERY POOL MEMBER.
# ⚠️ THIS IS A DEADLINE, NOT A SLEEP: `_native_visible_across_pool` returns the moment every
# member reports the balance, so raising it costs NOTHING on the path where the balance is
# already there, and only buys time on the path that would otherwise throw the entry away.
# LINTCHA (4663, 2026-09-23T19:06Z) is why it moved from 1.0: the unwrap was mined at
# 19:06:22.000Z and the entry aborted at 19:06:23.211Z, 1.2 s later, on a balance that had
# already landed. All 5 pre-transactions this system called ambiguous or failed in the 30 days
# to 2026-09-23 succeeded on chain; not one was a real failure.
MEMECOIN_EVM_NATIVE_VISIBLE_WAIT_S = float(os.getenv("MEMECOIN_EVM_NATIVE_VISIBLE_WAIT_S", "3.0"))
# Share of feed samples whose source timestamp is in the FUTURE that still counts as one bad clock
# rather than an untrusted feed. A single future-dated sample used to void the whole lag reading:
# on 2026-09-17 the cockpit said "source clock untrusted" while the median lag was 0.30 s over
# 2,771 samples with 10 (0.36%) future-dated. Above this share the reading is suppressed again.
MEMECOIN_FEED_LAG_FUTURE_SHARE_MAX = float(os.getenv("MEMECOIN_FEED_LAG_FUTURE_SHARE_MAX", "0.05"))
# ...and how many future-dated samples a window must hold before its share may condemn the feed. A
# quiet 5-minute window carries ~16 events, so two bad clocks read as 12.5% while the base rate
# over 2,790 samples is 0.43%. Both conditions must hold (2026-09-17).
MEMECOIN_FEED_LAG_FUTURE_MIN_COUNT = int(os.getenv("MEMECOIN_FEED_LAG_FUTURE_MIN_COUNT", "5"))
# Pair-liquidity ranking reads per decision, and how many run at once. A cold pair is up to 114
# reads; eight at a time on the funded 4663 pool produced a 429 on the next coin's v2 rung.
MEMECOIN_EVM_V4_MULTIHOP_LIQUIDITY_READS = int(os.getenv(
    "MEMECOIN_EVM_V4_MULTIHOP_LIQUIDITY_READS", "32"))
MEMECOIN_EVM_V4_MULTIHOP_LIQUIDITY_WORKERS = int(os.getenv(
    "MEMECOIN_EVM_V4_MULTIHOP_LIQUIDITY_WORKERS", "4"))
# RELATIVE margin by which a measured round trip may exceed the budget and still be RE-ASKED (never
# admitted). 0 = the refusal stays one-shot, which is the behaviour before 2026-09-17.
MEMECOIN_EVM_V4_MULTIHOP_BUDGET_RETRY_MARGIN_PCT = float(os.getenv(
    "MEMECOIN_EVM_V4_MULTIHOP_BUDGET_RETRY_MARGIN_PCT", "10"))
# THE V4 RUNG'S OVER-BUDGET FALLBACK (2026-10-08, KLN). When the max-output pool's same-pool round
# trip fails the budget, measure at most this many of the other pools discovery already quoted, in
# descending output, and take the first within budget. 0 = off (refuse on the winner, as before).
MEMECOIN_EVM_V4_BUDGET_FALLBACK_MAX = int(os.getenv("MEMECOIN_EVM_V4_BUDGET_FALLBACK_MAX", "3"))
# EVERY ENTRY RUNG MEASURES ITS ROUND TRIP, AND THE BEST ONE WINS (2026-09-06). The ladder took
# the first hit in a fixed priority list, so a decoy pool that happened to sit earlier beat a
# better route that had already quoted. Measured on 4663 over 2026-08-23..09-06: of 123 funded
# fills with an exact exit quote inside 15s of fill, 12 were already at or below -30% on that first
# quote, -$571 total -- and TEN came from the rungs that measured nothing (v4 single-hop 4 at
# -$267, v3 2 at -$93, relay 4 at -$45), against one from the measured multihop rung.
# 0 restores the fixed-priority ladder exactly, and a test asserts that.
#
# DEFAULT ON. A default-off switch flipped in `.env` would put the funded entry path on a branch no
# test exercises, which is the opposite of what this change is for. Turning it on required making
# five stubs in `tests/test_memecoin_evm_seams.py` direction-aware: they returned `buyAmount: "1"`
# whichever way they were called, so a measured round trip read about -100% and the entry was
# correctly refused. The stubs predated a ladder that measures.
# STORE-FIRST v4_multihop DISCOVERY (2026-09-27, Alpha GO). When our PoolKey store already holds
# the coin's pools, the multihop search runs on the store alone (no GeckoTerminal / DexScreener
# read) and falls back to the provider search only when that finds no route. 0 = providers first,
# as before. Measured offline on the 7 V4-routed 4663 fills: 0.44-0.85 s, executed pool on 6 of 7.
# DEFAULT OFF since 2026-09-28 (Alpha): the first organic cold search (coin 0x8920d999, 00:27Z) had
# its 32 pair-liquidity reads spent on the first bridge, left the bridge holding the deep coin pool
# unread, and quoted -49.3% where the provider path quoted -2.6%. A fast store hit also ends the
# ladder's grace early, so Kyber/Relay can land outside it. Back on only when cold store-first is
# within 0.5 pp of the provider route on every measured coin.
MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST = os.getenv(
    "MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_EVM_ENTRY_MEASURED_LADDER = os.getenv(
    "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", "1").strip().lower() in {"1", "true", "yes"}
# WHEN THERE IS NO CHART PRICE, JUDGE AGAINST THE TRADER'S (2026-09-06, operator approved). The
# execution-premium gate returned `no_display_price` and FAILED OPEN whenever DexScreener carried
# no price for the coin. Measured over 30 days: that is 289 of 861 funded closes -- one in three,
# not the one in six we had assumed -- for a net -$293.88, every one admitted unjudged. 155 of them
# carry a usable trader price, and that reference orders their returns: +7.52% below 1.15x (n=73),
# then -3.59%, -22.56%, -5.90%. At the same cap it refuses 43 worth -$348.79.
# 0 restores the previous fail-open verdict exactly, and a test asserts it.
# The trader's price floors the chart reference only for NANO coins on Solana (2026-09-13, see
# memecoin_market_state._source_floor_applies). 0 = the floor for every band again.
def gate_exempt_families_for(network_id, env=None, *, source_user_id=None):
    """Chain-wide selection waivers plus the operator's chain/account first-seen allowlist.

    Empty allowlists grant nothing. IDs are exact source account IDs, never handles.
    The caller filters against EXEMPTIBLE_GATE_FAMILIES; this only parses.
    """
    env = os.environ if env is None else env
    chain = str(network_id or "").strip().upper()
    raw = env.get("MEMECOIN_GATE_EXEMPT_FAMILIES_%s" % chain, "")
    families = frozenset(part.strip() for part in str(raw).split(",") if part.strip())
    wallets = env.get("MEMECOIN_FIRST_SEEN_CHASE_EXEMPT_WALLETS_%s" % chain, "")
    if chain and source_user_id and source_user_id in {w.strip() for w in wallets.split(",")}:
        families |= {"chase_first_seen"}
    return families


MEMECOIN_REAL_EXEC_PREMIUM_SOLANA_FLOOR_NANO_ONLY = os.getenv(
    "MEMECOIN_REAL_EXEC_PREMIUM_SOLANA_FLOOR_NANO_ONLY", "1").strip().lower() in {"1", "true", "yes"}
MEMECOIN_REAL_EXEC_PREMIUM_SOURCE_FALLBACK = os.getenv(
    "MEMECOIN_REAL_EXEC_PREMIUM_SOURCE_FALLBACK", "1").strip().lower() in {"1", "true", "yes"}


# Hosts whose SOCKET we never take, per chain. On 4663 it is the public chain-owner endpoint the
# push lane exists to stop polling. On 8453 it is both chain-owner endpoints, for a different and
# simpler reason: MEASURED 2026-09-06, `wss://mainnet.base.org` and `wss://mainnet-preconf.base.org`
# both answer the WebSocket upgrade with HTTP 405. Base's chain owner does not serve `eth_subscribe`
# at all, so deriving a socket from them would produce a URL that can only fail to connect.
_CHAIN_OWNER_HOSTS = {
    "4663": ("rpc.mainnet.chain.robinhood.com",),
    "8453": ("mainnet.base.org", "mainnet-preconf.base.org"),
}
_CHAIN_OWNER_HOST = _CHAIN_OWNER_HOSTS["4663"][0]        # kept: existing readers import this name


def _evm_ws_url(chain="4663"):
    """The WebSocket endpoint for the push-fed on-chain lanes, or None.

    Defaults to the `wss://` form of the chain's configured RPC URL. A SUBSCRIPTION IS NOT A POLL:
    the watcher's old range `eth_getLogs` needed ~1 request/second sustained and was kept off
    custody for that reason, while one subscription costs no range read at all and delivers at
    block time. That is why this may point at the custody endpoint where the poller may not.
    """
    explicit = os.getenv("MEMECOIN_EVM_WS_URL_%s" % chain)
    if explicit and explicit.strip():
        return explicit.strip()
    # DERIVE FROM THE CUSTODY POOL HEAD, NOT FROM `MEMECOIN_EVM_RPC_URL_<chain>`. Checked
    # 2026-09-05: that variable currently holds the CHAIN-OWNER public endpoint, which is the one
    # already rate-limiting under the watcher's old polls and the one this lane exists to stop
    # using. Deriving a socket from it would point the push lane straight back at it. The pool
    # head is the paid custody endpoint, which is what the design means by "the custody pool URL".
    pool = os.getenv("MEMECOIN_EVM_RPC_POOL_URLS_%s" % chain) or ""
    owner_hosts = _CHAIN_OWNER_HOSTS.get(str(chain), ())
    for candidate in [c.strip() for c in pool.split(",") if c.strip()]:
        host = candidate.split("//", 1)[-1].split("/", 1)[0].lower()
        if any(owner in host for owner in owner_hosts):
            continue                       # never a socket on the endpoint we are moving off
        if candidate.startswith("https://"):
            return "wss://" + candidate[len("https://"):]
        if candidate.startswith("http://"):
            return "ws://" + candidate[len("http://"):]
    # FAIL CLOSED. No custody socket means the watcher keeps polling exactly as it does today,
    # which is worse but known -- silently subscribing on the wrong endpoint is neither.
    return None


MEMECOIN_EVM_WS_URL_4663 = _evm_ws_url("4663")
# Base has no default socket: see `_CHAIN_OWNER_HOSTS`. The watcher then keeps polling, which on
# Base's 2-second blocks is the cheap case, and one `MEMECOIN_EVM_WS_URL_8453=<wss url>` turns the
# push lane on with no code change. Two third-party public sockets were measured serving
# `eth_subscribe` correctly on 2026-09-06 if the operator wants one; neither is a chain-owner
# endpoint, so neither is adopted here.
MEMECOIN_EVM_WS_URL_8453 = _evm_ws_url("8453")


def memecoin_evm_ws_url(chain):
    """The configured WebSocket endpoint for one chain, or None. One reader for every chain."""
    return _evm_ws_url(str(chain))


# PUSH-LANE HOST LISTS (2026-10-17, Alpha). MEMECOIN_EVM_WS_URLS_<chain>: comma-separated hosts after
# the configured primary (MEMECOIN_EVM_WS_URL_<chain>, always first). Entries: "publicnode" (the
# primary, when it is a publicnode socket), "alchemy" (the funded RPC pool's Alchemy member as wss),
# or a wss:// URL. Research hosts (nodereal) and anything else are never push hosts. Absent or empty:
# today's single socket. 56 went dark all day 10-17 on publicnode's "-32603 transport" refusals.
_WS_RESEARCH_ONLY = ("nodereal.io",)


def _ws_listed(chain, primary):
    from urllib.parse import urlsplit
    raw = os.getenv("MEMECOIN_EVM_WS_URLS_%s" % chain, "")
    out = []
    for entry in (part.strip() for part in raw.split(",")):
        url = None
        if entry.lower() == "publicnode":
            url = primary if primary and (urlsplit(primary).hostname or "").endswith("publicnode.com") \
                else None
        elif entry.lower() == "alchemy":
            for rpc in MEMECOIN_EVM_RPC_POOL_URLS.get(str(chain), ()):
                parsed = urlsplit(rpc)
                if parsed.scheme == "https" and (parsed.hostname or "").endswith(".g.alchemy.com"):
                    url = "wss://" + rpc[len("https://"):]
                    break
        elif entry.startswith("wss://"):
            url = entry
        host = (urlsplit(url).hostname or "") if url else ""
        if url and not any(host == bad or host.endswith("." + bad) for bad in _WS_RESEARCH_ONLY):
            out.append(url)
    return out


def memecoin_evm_ws_failover(chain):
    """run_push_stream's failover settings: a listed chain leaves a host after
    MEMECOIN_EVM_WS_FAILOVER_AFTER (2) refusals or failed sockets inside MEMECOIN_EVM_WS_FAILOVER_WINDOW_S
    (600) and returns to its primary after MEMECOIN_EVM_WS_PRIMARY_RETURN_S (900) on a backup. Ethereum's
    Infura rotation keeps its first-failure rule and stays on the backup."""
    window = float(os.getenv("MEMECOIN_EVM_WS_FAILOVER_WINDOW_S", "600"))
    if str(chain) == "1":
        return {"failover_after": 1, "failover_window_s": window, "primary_return_s": None}
    back = float(os.getenv("MEMECOIN_EVM_WS_PRIMARY_RETURN_S", "900"))
    return {"failover_after": max(1, int(os.getenv("MEMECOIN_EVM_WS_FAILOVER_AFTER", "2"))),
            "failover_window_s": window, "primary_return_s": back if back > 0 else None}


# THE PUSH-SOCKET CANARY (2026-10-17, Alpha/Gamma): a counted-only logs filter on the same socket, so a
# silent logs subscription is told apart from quiet wallets (56 base rate ~1.7 armed events/h). 56: the
# Sync events of ONE busy PancakeSwap V2 pool, USDT/WBNB 0x16b9...0daE (token0 USDT 0x55d3...7955,
# token1 WBNB, read on-chain 2026-10-17), about one log a second: every WBNB Transfer would be hundreds a
# second on the watcher thread and on a socket that already draws -32701 policy refusals (Gamma LOW).
# MEMECOIN_EVM_WS_CANARY_ADDRESS_56 replaces the pool (a 0x address; anything else turns the canary
# off). Heads arriving with no canary log for MEMECOIN_EVM_WS_CANARY_SILENCE_S (120) is a silent socket
# (rotate / reconnect). MEMECOIN_EVM_WS_CANARY_56=0 turns it off. Other chains: none.
_WS_CANARY_POOL = {"56": "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae"}
_V2_SYNC_TOPIC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"


def memecoin_evm_ws_canary(chain):
    """run_push_stream's canary kwargs for one chain, or {} (off)."""
    import re
    pool = _WS_CANARY_POOL.get(str(chain))
    if pool is None or os.getenv("MEMECOIN_EVM_WS_CANARY_%s" % chain, "1").strip().lower() in {"0", "false", "no"}:
        return {}
    pool = os.getenv("MEMECOIN_EVM_WS_CANARY_ADDRESS_%s" % chain, "").strip().lower() or pool
    if not re.fullmatch(r"0x[0-9a-f]{40}", pool):
        return {}
    return {"canary_filter": {"address": pool, "topics": [_V2_SYNC_TOPIC]},
            "canary_silence_s": float(os.getenv("MEMECOIN_EVM_WS_CANARY_SILENCE_S", "120"))}


def memecoin_evm_ws_pool(chain):
    """Ethereum's configured Infura keys; MEMECOIN_EVM_WS_URLS_<chain> elsewhere; else one socket."""
    primary = memecoin_evm_ws_url(chain)
    urls = [primary] if primary else []
    if primary and str(chain) != "1":
        urls += _ws_listed(str(chain), primary)
    if str(chain) == "1" and primary:
        from urllib.parse import urlsplit
        for rpc in MEMECOIN_EVM_RPC_POOL_URLS.get("1", ()):
            parsed = urlsplit(rpc)
            if parsed.scheme == "https" and parsed.hostname == "mainnet.infura.io" \
                    and parsed.path.startswith("/v3/"):
                urls.append("wss://mainnet.infura.io/ws" + parsed.path)
    return tuple(dict.fromkeys(urls))
# A socket that stays OPEN but stops being serviced raises no exception. 15s is the same window
# the Robinhood volume stream uses: wide enough that block-time jitter never trips it, close
# enough that a stalled feed does not stay dark for the life of the process.
MEMECOIN_EVM_WS_STALE_S = float(os.getenv("MEMECOIN_EVM_WS_STALE_S", "15"))
# THE BOUND POOL AS THE STOP'S NATIVE RUNG on Solana (2026-10-17, Alpha; 73927 a transient -54.6% Jupiter
# route fired b_stop while its SPCX pool read -2.9%; 73930 Jupiter blind 6.5 min). Every position with a
# bound AMM pool. sniper/services/memecoin_native_exit_rung.py. 0 = today's single Jupiter rung.
MEMECOIN_SOLANA_NATIVE_RUNG = os.getenv(
    "MEMECOIN_SOLANA_NATIVE_RUNG", "1").strip().lower() not in {"0", "false", "no"}
MEMECOIN_SOLANA_NATIVE_RUNG_DIVERGENCE_PCT = float(os.getenv("MEMECOIN_SOLANA_NATIVE_RUNG_DIVERGENCE_PCT", "25"))
# The band's fees: HIGH (above PumpSwap's top tier) prices the pessimistic bound, LOW the optimistic one.
MEMECOIN_SOLANA_NATIVE_RUNG_FEE_HIGH_BPS = int(os.getenv("MEMECOIN_SOLANA_NATIVE_RUNG_FEE_HIGH_BPS", "125"))
MEMECOIN_SOLANA_NATIVE_RUNG_FEE_LOW_BPS = int(os.getenv("MEMECOIN_SOLANA_NATIVE_RUNG_FEE_LOW_BPS", "25"))
MEMECOIN_SOLANA_NATIVE_RUNG_MAX_AGE_S = float(os.getenv("MEMECOIN_SOLANA_NATIVE_RUNG_MAX_AGE_S", "60"))
# Native PumpSwap exits settle in USDC through the exact entry-bound pool when Jupiter is blind
# or severely divergent. Default OFF pending real unsigned composite simulation and operator activation.
MEMECOIN_SOLANA_NATIVE_EXIT = os.getenv(
    "MEMECOIN_SOLANA_NATIVE_EXIT", "0").strip().lower() not in {"0", "false", "no", "off"}
# The floor's total fee: above PumpSwap's top tier (lp 20 + protocol 5 + creator 95 measured 2026-10-17),
# so min_quote_amount_out never asks more than the pool pays.
MEMECOIN_SOLANA_NATIVE_EXIT_FEE_BPS = int(os.getenv("MEMECOIN_SOLANA_NATIVE_EXIT_FEE_BPS", "150"))
# A pool quoted in neither SOL nor USDC holds a diverged stop at most this long (Gamma LOW, Alpha): its
# native value prices the quote asset at cached USD, never the quote-token -> SOL route.
MEMECOIN_SOLANA_NATIVE_RUNG_EXOTIC_HOLD_S = float(os.getenv("MEMECOIN_SOLANA_NATIVE_RUNG_EXOTIC_HOLD_S", "120"))
# LOG SILENCE ON A LIVE SOCKET (2026-09-28, observe-only). Heads still arriving while no log on any
# subscription has arrived for this long is reported as `log_silence: suspect` in the EVM watcher's
# heartbeat: 4663's watcher was silent 12:10-12:24Z and missed a watched buy with heads unlogged.
MEMECOIN_EVM_WS_LOG_SILENCE_S = float(os.getenv("MEMECOIN_EVM_WS_LOG_SILENCE_S", "300"))
# LOG SILENCE ACTS (2026-10-16, Alpha; knob ON at deploy): on these chains the push watcher no longer
# only reports `log_silence: suspect` (heads arriving, no log on any subscription, Initialize as the
# reference) but resubscribes its log filters on the live socket, then reconnects if no log returns
# within another window, then waits 900 s. Also trigger B: Initialize delivering while the transfer
# filter has been silent MEMECOIN_EVM_WS_TRANSFER_SILENCE_S resubscribes that filter alone. Empty = the
# observe-only watcher. Only chains with a V4 Initialize reference can act. Read at watcher start.
MEMECOIN_EVM_WS_LOG_SILENCE_ACT_CHAINS = os.getenv("MEMECOIN_EVM_WS_LOG_SILENCE_ACT_CHAINS", "4663")
MEMECOIN_EVM_WS_TRANSFER_SILENCE_S = float(os.getenv("MEMECOIN_EVM_WS_TRANSFER_SILENCE_S", "900"))
# How long the socket may be dark before the watcher falls back to its polling loop. Push resumes
# the moment a head arrives again; the fallback is a floor on coverage, not a switch.
MEMECOIN_EVM_WS_FALLBACK_S = float(os.getenv("MEMECOIN_EVM_WS_FALLBACK_S", "120"))
MEMECOIN_ONCHAIN_PUSH_ENABLED = os.getenv(
    "MEMECOIN_ONCHAIN_PUSH_ENABLED", "1").strip().lower() in {"1", "true", "yes"}


def _evm_v4_launchpads():
    """Registry of launchpad contracts that can name a coin's real pool without a log read.

    `MEMECOIN_EVM_V4_LAUNCHPADS_<chain>` is a comma list of
    `<launchpad>:<info_selector>:<coin_word>:<counter_word>:<fee_word>:<ts_word>:<hook_selector>`.

    WHY THIS EXISTS. At the first feed signal a launchpad coin's pool is 0-30 seconds old, this
    chain's log index surfaces Initialize 38-61 seconds after the block, and the public indexers
    list the fee-trap pools first. Log and indexer discovery therefore miss the first signal BY
    CONSTRUCTION, not by failure. The launchpad already knows the pool it created, so one
    `eth_call` answers what no log read can answer yet.

    A malformed entry is SKIPPED WITH A WARNING rather than raised: a typo in one registry line
    must not stop the runner from importing. An empty list turns the feature off.
    """
    import logging                          # local: this module has no logging surface of its own
    log = logging.getLogger("memecoin")
    prefix = "MEMECOIN_EVM_V4_LAUNCHPADS_"
    raw = {"4663": "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e:3cf28b5a:0:4:6:7:6651812c"}
    for name, value in os.environ.items():
        if name.startswith(prefix) and name[len(prefix):].strip():
            raw[name[len(prefix):].strip()] = value
    out = {}
    for chain, value in raw.items():
        entries = []
        for item in str(value).split(","):
            item = item.strip()
            if not item:
                continue
            parts = item.split(":")
            if len(parts) != 7:
                log.warning("memecoin: launchpad entry ignored (want 7 fields): %s", item)
                continue
            address, info_sel, coin_w, counter_w, fee_w, ts_w, hook_sel = parts
            try:
                words = tuple(int(w) for w in (coin_w, counter_w, fee_w, ts_w))
            except ValueError:
                log.warning("memecoin: launchpad entry ignored (word index): %s", item)
                continue
            address = address.strip().lower()
            info_sel = info_sel.strip().lower().removeprefix("0x")
            hook_sel = hook_sel.strip().lower().removeprefix("0x")
            if (not address.startswith("0x") or len(address) != 42
                    or len(info_sel) != 8 or len(hook_sel) != 8
                    or min(words) < 0):
                log.warning("memecoin: launchpad entry ignored (shape): %s", item)
                continue
            entries.append({
                "address": address, "info_selector": info_sel, "hook_selector": hook_sel,
                "coin_word": words[0], "counter_word": words[1],
                "fee_word": words[2], "tick_spacing_word": words[3]})
        if entries:
            out[str(chain)] = tuple(entries)
    return out


MEMECOIN_EVM_V4_LAUNCHPADS = _evm_v4_launchpads()
# PROBE SIZE for the confirming quote. Small on purpose: it proves the pool answers, it does not
# price the clip. A native counter currency is 18-decimal ETH, so 10^15 is 0.001 ETH.
MEMECOIN_EVM_V4_LAUNCHPAD_PROBE_ATOMIC = int(os.getenv(
    "MEMECOIN_EVM_V4_LAUNCHPAD_PROBE_ATOMIC", str(10 ** 15)))


def _evm_multihop_second_bridges():
    """Per-chain middle currency for a three-leg route: `..._SECOND_BRIDGES_<chain>`, comma list.

    The middle leg joins this currency to the coin's counter-currency. USDG is the default on 4663
    because the stock pools quote against it; a chain with no entry searches two legs only.
    """
    prefix = "MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES_"
    # NO BASE DEFAULT, AND THAT IS A MEASUREMENT, NOT AN OMISSION (2026-09-06). The scope asked for
    # USDC here. Adding it switched on the three-leg search on Base -- `MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS`
    # is 3 in the live config -- and the canary went from 3-44s per quote to 25s, 48s, 101s, 118s
    # and 436s, for ZERO extra routes: every three-leg candidate ended `bridge_pool_unusable` or
    # `coin_leg_missing_or_under_floor`. Base has far more pools per coin than 4663, so the search
    # that is cheap there is combinatorial here, against a public endpoint that then quarantines
    # itself under the load. Set MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES_8453 to turn it on once a
    # dedicated Base endpoint exists and the cost has been measured again.
    out = {"4663": ("0x5fc5360d0400a0fd4f2af552add042d716f1d168",)}
    for name, value in os.environ.items():
        if not name.startswith(prefix):
            continue
        chain = name[len(prefix):].strip()
        parts = tuple(p.strip().lower() for p in str(value).split(",") if p.strip())
        if chain:
            out[chain] = parts
    return out


MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES = _evm_multihop_second_bridges()
# GAS FOR A LEG BEYOND THE SECOND. The two-leg limit above stays the single authoritative number
# and the operator's existing knob keeps working: gas is that value plus this per EXTRA leg, so a
# two-leg build is byte-for-byte unchanged. A separate base constant was rejected -- it would have
# silently overridden `MEMECOIN_EVM_V4_MULTIHOP_GAS` and killed the retune the FROGE incident
# added. The plan's canary reads `eth_estimateGas` on real three-leg builds and sets this default
# from the measured p95 plus 20%; until then a third leg buys one leg of headroom, not a guess.
MEMECOIN_EVM_V4_MULTIHOP_GAS_PER_LEG = int(os.getenv(
    "MEMECOIN_EVM_V4_MULTIHOP_GAS_PER_LEG", "300000"))
# ROUND-TRIP CEILING: the cost we MEASURE, replacing depth as a proxy for it (2026-09-04).
# `multihop_quote` already quotes all four legs and reports `round_trip_pct` -- what a clip loses
# buying in and selling straight back out -- and nothing read it. Depth stood in for that cost and
# is wrong in both directions on this chain: it admits a DEEP pool charging 90% and rejects a
# SHALLOW honest one. FRIES (0x798c78cf) is the case: refused by the 100-clip floor demanding
# $13,900, the route appears at 20 clips, and it quotes a -24.8% round trip against a 25% TP.
#
# The ceiling is a FRACTION OF THE POSITION'S OWN TAKE-PROFIT, not a global percentage, because
# that is the comparison that makes -24.8% a refusal: friction must leave room for the exit the
# position is actually aiming at. 0 disables the gate and restores depth-only screening.
MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC = float(os.getenv(
    "MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC", "0.4"))
# Ceiling used when no take-profit is known for the coin (TP disabled, or an unfrozen entry).
# Without it a TP-relative gate would silently pass everything on a 0% TP.
def quote_over_source_max_pct_for(chain, env=None):
    """Cap on OUR route quote above the copied trader's price, per chain, or None (off).

    2026-09-13 (operator, TWIN 57097): the 4663 chase gate measures the COIN's move (chart vs
    source, `_chase_gap`) and the premium gate floors the chart with the source price, so nothing
    bounded quote/source on the initial quote: TWIN filled +45% above picadura. Funded 4663 closes
    since 09-01 by fill vs source: < 30% n=122 +$394.99; 30-40% n=16 -$24.77; 40-60% n=20 -$81.98;
    60%+ n=19 -$248.64. MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_4663=30.
    """
    env = os.environ if env is None else env
    raw = str(env.get("MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_%s" % str(chain).strip(), "")).strip()
    try:
        value = float(raw) if raw else None
    except ValueError:
        return None
    return value if value and value > 0 else None


def evm_round_trip_tp_frac_for(chain, env=None):
    """Per-chain override of MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC, or None.

    2026-09-13 (operator, PONZI 7ae4df5b): funded 4663 closes whose measured round trip was
    -10..-15% made +$157 over 13 copied trades (09-01..09-04, before the 10% budget), so 4663 takes
    its own fraction. MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC_4663=0.6 -> 15% at a 25% TP.
    """
    env = os.environ if env is None else env
    raw = str(env.get("MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC_%s" % str(chain).strip(), "")).strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_PCT = float(os.getenv(
    "MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_PCT", "10"))
MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT = float(os.getenv(
    "MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT", "8"))

# fee tiers scanned by the quoter (best amountOut wins); 3000 + 10000 observed on our real coins.
MEMECOIN_EVM_V3_FEE_TIERS = tuple(int(x) for x in os.getenv(
    "MEMECOIN_EVM_V3_FEE_TIERS", "100,500,3000,10000").split(",") if x.strip())
# PER-CHAIN TIER ORDER, because the right tiers are a property of the chain's pools and each tier
# costs a quoter call. MEASURED on 56 (census, 2026-09-06): `getPool(USDT, BEN, ...)` returns ZERO
# at 100, 500 and 2500 and a real pool only at 10000 (1%), whose address matches DexScreener's
# reported pair exactly. The low stable-pair tiers are the wrong shape for these coins, so 1% leads
# and the rest follow rather than being dropped -- a tier that is usually empty still costs only a
# call when the deep one already answered.
MEMECOIN_EVM_V3_FEE_TIERS_BY_CHAIN = {
    "56": tuple(int(x) for x in os.getenv(
        "MEMECOIN_EVM_V3_FEE_TIERS_56", "10000,2500,500,100").split(",") if x.strip()),
}


def memecoin_evm_v3_fee_tiers(chain_id):
    """The quoter's fee-tier order for one chain, deepest-first where we have measured it."""
    return MEMECOIN_EVM_V3_FEE_TIERS_BY_CHAIN.get(str(chain_id), MEMECOIN_EVM_V3_FEE_TIERS)

# Exit-only Uniswap V2 fallback for chain 4663. Some launch tokens (VIM is the live proof) have no
# token/WETH V3 pool but do have a deep V2 route through an immutable connector. Keep this OUT of
# the entry venue until fee-on-transfer admission is separately proven; it exists to prevent an
# already-funded bag from becoming unexitable merely because its pool topology is multi-hop V2.
# Base: Uniswap's own V2 router. Address from the Uniswap v2 deployments page and confirmed by a
# non-empty eth_getCode on https://mainnet.base.org (17,891 bytes, 2026-09-06).
MEMECOIN_EVM_V2_ROUTER = {"4663": os.getenv(
    "MEMECOIN_EVM_V2_ROUTER_4663", "0x89e5DB8B5aA49aA85AC63f691524311AEB649eba"),
    "8453": os.getenv("MEMECOIN_EVM_V2_ROUTER_8453",
                      "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24"),
    # PancakeSwap V2 Router02 on 56. V2 IS THE PRIMARY VENUE on this chain, not a fallback: the
    # census found PancakeSwap deepest for 20 of 20 coins and the version split $4.89M V2 (85%)
    # against $884K V3 (15%). Its `factory()` returns the V2 factory, proved on chain.
    "56": os.getenv("MEMECOIN_EVM_V2_ROUTER_56",
                    "0x10ED43C718714eb63d5aA57B78B54704E256024E"),
    # Ethereum mainnet Uniswap V2 Router02, 21,943 bytes; factory() -> 0x5C69bEe7..., WETH() ->
    # the chain-1 quote token, proved on chain 2026-09-30.
    "1": os.getenv("MEMECOIN_EVM_V2_ROUTER_1", "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")}
MEMECOIN_EVM_V2_CONNECTORS = {
    "4663": tuple(x.strip() for x in os.getenv(
        "MEMECOIN_EVM_V2_CONNECTORS_4663",
        "0xc6911796042b15d7fa4f6cde69e245ddcd3d9c31,"  # VIRTUAL
        "0x5fc5360d0400a0fd4f2af552add042d716f1d168",  # USDG
    ).split(",") if x.strip()),
    # 56: WBNB is the quote currency, so a token/WBNB pair is a one-hop V2 path, and USDT is the
    # stable that covers the rest. USDC on BSC is a minor pair and is deliberately not here.
    "56": tuple(x.strip() for x in os.getenv(
        "MEMECOIN_EVM_V2_CONNECTORS_56",
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c,"   # WBNB
        "0x55d398326f99059ff775485246999027b3197955",   # USDT (BSC-USD)
    ).split(",") if x.strip()),
    # Base: the two currencies every V2 pair on this chain routes through. WETH is also the quote
    # currency, so a token/WETH pair is a one-hop V2 path and USDC covers the stable-paired ones.
    "8453": tuple(x.strip() for x in os.getenv(
        "MEMECOIN_EVM_V2_CONNECTORS_8453",
        "0x4200000000000000000000000000000000000006,"  # WETH
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    ).split(",") if x.strip()),
    "1": tuple(x.strip() for x in os.getenv(
        "MEMECOIN_EVM_V2_CONNECTORS_1",
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2,"  # WETH
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
    ).split(",") if x.strip()),
}


def memecoin_evm_v3_addresses(chain_id):
    """{'router','quoter'} for a chain's Uniswap-V3 direct path, or None if either is unset
    (fail-closed: the direct quoter then produces no quote)."""
    router = MEMECOIN_EVM_V3_ROUTER.get(str(chain_id))
    quoter = MEMECOIN_EVM_V3_QUOTER.get(str(chain_id))
    if not router or not quoter:
        return None
    # Chain 56's router is PancakeSwap's ORIGINAL SwapRouter: both entry points carry a deadline
    # inside the params struct. Its QuoterV2 is selector-identical to Uniswap's, so ONLY the swap
    # calldata changes. Verified on-chain 2026-09-08 — see the selector table in evm_dex_quote.
    if str(chain_id) == "56":
        return {"router": router, "quoter": quoter, "protocol": "pancake_v3"}
    return {"router": router, "quoter": quoter}


# ADDITIONAL V3 deployments per chain (2026-08-17, CLOSED 0x034d81…): Robinhood chain runs
# MULTIPLE independent Uniswap-V3 factories — a coin whose only pool lives on a factory our
# single quoter does not serve was unquotable and untradable (entry gave up after 120s of
# clean no-route reads). "quoter:router[:protocol]" pairs, comma-separated. Every pair must be verified
# on-chain before use: quoter/router factory() immutables agree with each other and WETH9()
# matches CHAIN_QUOTE — the same identity discipline the primary deployment went through.


def _parse_evm_v3_extra_deployment(value):
    parts = [part.strip() for part in str(value).split(":", 2)]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    if len(parts) == 3 and parts[2] not in {"ramses_v3", "pancake_v3"}:
        return None
    deployment = {"quoter": parts[0], "router": parts[1]}
    if len(parts) == 3:
        deployment["protocol"] = parts[2]
    return deployment


MEMECOIN_EVM_V3_EXTRA_DEPLOYMENTS = {
    "4663": tuple(
        deployment
        for p in os.getenv("MEMECOIN_EVM_V3_EXTRA_DEPLOYMENTS_4663", "").split(",")
        if (deployment := _parse_evm_v3_extra_deployment(p)) is not None),
}


def memecoin_evm_v3_deployments(chain_id):
    """Every configured {'router','quoter'} V3 deployment for a chain, primary first. The quote
    scan probes each deployment's quoter and the deepest amountOut wins; the swap builds against
    the WINNING deployment's router, so a buy and its later sell route the same deployment."""
    primary = memecoin_evm_v3_addresses(chain_id)
    extras = tuple(MEMECOIN_EVM_V3_EXTRA_DEPLOYMENTS.get(str(chain_id)) or ())
    return ((primary,) if primary else ()) + extras


def memecoin_evm_v3_connectors(chain_id):
    """Configured immutable `(connector_token, quote_pair_fee)` rungs for exact-input V3 paths."""
    return tuple(MEMECOIN_EVM_V3_CONNECTORS.get(str(chain_id)) or ())


def memecoin_evm_v2_exit_config(chain_id):
    """Verified Router02 + immutable connector tokens for an exit-only V2 fallback."""
    chain = str(chain_id)
    router = MEMECOIN_EVM_V2_ROUTER.get(chain)
    connectors = MEMECOIN_EVM_V2_CONNECTORS.get(chain) or ()
    if not router:
        return None
    return {"router": router, "connectors": tuple(connectors)}


# SWAP DEADLINE PER CHAIN (eth-mainnet spec v3 §3.7, 2026-09-30). A chain listed here sends its swaps
# privately (Flashbots on chain 1), where custody cannot see a held transaction; the ONLY proof that a
# sent swap can no longer execute is its own deadline passing. So every swap on such a chain carries a
# SHORT deadline, and a SwapRouter02 exactInput* (which has none) is wrapped in multicall(deadline).
# Every other chain keeps its call site's constant, byte for byte. MEMECOIN_EVM_SWAP_DEADLINE_S_<chain>
# overrides a listed chain; a malformed or non-positive value keeps the code default, never "no
# deadline", and a value above _SWAP_DEADLINE_MAX_S is clamped: the dead proof waits for whatever
# deadline was signed, so a large value would hold a reservation that long.
# The deadline is set from the HOST clock and judged by the chain. Host skew shifts the validity
# window; it cannot break the proof, which compares chain time with the decoded deadline.
_SWAP_DEADLINE_DEFAULTS = {"1": 60}
_SWAP_DEADLINE_MAX_S = 300


_EXPIRY_MARGIN_DEFAULT_S = 24      # 2 blocks past the deadline before a swap is called dead
_EXPIRY_MARGIN_MIN_S = 12          # never under one block


def memecoin_evm_expiry_margin_s(chain_id):
    """Seconds of CHAIN time past a swap's deadline before the dead proof holds.
    MEMECOIN_EVM_EXPIRY_MARGIN_S_<chain> overrides it, never below one block."""
    try:
        value = int(float(os.getenv(f"MEMECOIN_EVM_EXPIRY_MARGIN_S_{chain_id}", "") or 0))
    except ValueError:
        value = 0
    return max(_EXPIRY_MARGIN_MIN_S, value) if value > 0 else _EXPIRY_MARGIN_DEFAULT_S


def memecoin_evm_expiry_chains():
    """Chains whose every swap must carry a short deadline (the dead proof reads it)."""
    return frozenset(_SWAP_DEADLINE_DEFAULTS)


def memecoin_evm_swap_deadline_s(chain_id, default):
    """Seconds from signing to a swap's deadline on `chain_id`; `default` off the expiry chains."""
    chain = str(chain_id)
    if chain not in _SWAP_DEADLINE_DEFAULTS:
        return default
    try:
        value = int(float(os.getenv(f"MEMECOIN_EVM_SWAP_DEADLINE_S_{chain}", "") or 0))
    except ValueError:
        value = 0
    return min(value, _SWAP_DEADLINE_MAX_S) if value > 0 else _SWAP_DEADLINE_DEFAULTS[chain]


def memecoin_evm_exec_venue(chain_id):
    """Funded EVM execution is explicit: verified direct venue or disabled.

    Missing, retired, and unknown values fail closed. Observation/capture remains independent.
    """
    value = os.getenv(f"MEMECOIN_EVM_EXEC_VENUE_{chain_id}", "disabled").strip().lower()
    return value if value == "direct" else "disabled"


def memecoin_slot_lane_resend_offsets_ms():
    """Call-time: when (ms after the first send) the slot lane re-sends its identical signed bytes
    through the TPU sidecar (2026-09-28). MEMECOIN_SLOT_LANE_RESEND=0 turns it off; the offsets must
    be positive and ascending, and only the first two are used. Malformed means off, never a guess."""
    if os.getenv("MEMECOIN_SLOT_LANE_RESEND", "1").strip().lower() in {"0", "false", "no", "off"}:
        return ()
    raw = os.getenv("MEMECOIN_SLOT_LANE_RESEND_OFFSETS_MS", "150,350")
    try:
        offsets = tuple(int(part) for part in raw.split(","))
    except ValueError:
        return ()
    offsets = offsets[:2]
    if not offsets or any(o <= 0 for o in offsets) or list(offsets) != sorted(set(offsets)):
        return ()
    return offsets


def memecoin_evm_native_spendable_chains():
    """Call-time: ETH-quoted chains whose spare native (above the full-book gas reserve) counts as
    spendable cash in sizing, and whose entries spend it before unwrapping WETH (skip the unwrap, or
    unwrap only the shortfall). Chain-1 unstall, 2026-10-08. Empty by default; malformed is empty."""
    raw = os.getenv("MEMECOIN_EVM_NATIVE_SPENDABLE_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_float_liquid_cap_chains():
    """Call-time: chains whose legacy gas pass (4663) caps the native float at the gas need plus
    half of native + WETH, the bound 56 and 8453 already use (2026-10-15, Alpha (B)). Empty by
    default; malformed means empty. Clearing it is today's pass byte for byte."""
    raw = os.getenv("MEMECOIN_EVM_FLOAT_LIQUID_CAP_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_native_float_snapshot():
    """Call-time kill knob (2026-10-21, default on): a BSC/Base fast lane's fresh wallet snapshot
    stands in for a native-float read still pending at the unwrap decision. 0 = today's unwrap."""
    return os.getenv("MEMECOIN_EVM_NATIVE_FLOAT_SNAPSHOT", "1").strip() == "1"


def memecoin_evm_native_float_chains():
    """Call-time: chains whose entries may skip the WETH unwrap when native ETH already covers the
    buy (spec 2026-09-27 4663 native float). Empty by default, and malformed means empty: nothing
    changes until the operator names a chain AND funds the float through the gas top-up marks."""
    raw = os.getenv("MEMECOIN_EVM_NATIVE_FLOAT_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_preapprove_chains():
    """Call-time: chains whose V4 entries approve the token for Permit2 right after booking, on a
    daemon thread, so the exit does not wait for the approval (spec 2026-09-30 4663 approve at
    entry). Empty by default, and malformed means empty. Requires the shared nonce floor:
    Ethereum enables that floor without pipelining Protect swaps; other chains require the
    approval pipeline; Base uses the floor with receipt-first swaps."""
    raw = os.getenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_defer_early_safety_chains():
    """Call-time: chains whose leg reuses the early worker's exact-reserved-clip safety pass instead
    of running its own check at the preliminary size (f31eeb917 on 56; item 2(A), operator speed
    fix via Alpha 2026-10-15). Unset or malformed means {"56"}, today's behaviour; an explicit
    empty value turns it off everywhere, 56 included. A chain other than 56 defers only when the
    early worker's safety has already started (memecoin_evm_leg)."""
    raw = os.getenv("MEMECOIN_EVM_DEFER_EARLY_SAFETY_CHAINS")
    if raw is None:
        return frozenset({"56"})
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if any(not chain.isdigit() for chain in chains):
        return frozenset({"56"})
    return frozenset(chains)


def memecoin_evm_early_sizing_own_gen():
    """Call-time kill knob (2026-10-15, Alpha): an EVM early quote's sizing does not count the
    attempt's OWN entry invalidation as a stale read (`_EntryPreReads.stale`). Default on: it only
    removes a self-inflicted refusal; 0 restores the old rule exactly."""
    return os.getenv("MEMECOIN_EVM_EARLY_SIZING_OWN_GEN", "1").strip() not in ("0", "false", "False", "")


def memecoin_evm_gate_balance_warm_chains():
    """Call-time: EVM chains whose gate balance cache a runner thread refreshes every
    MEMECOIN_EVM_GATE_BALANCE_WARM_S, so an early quote can size from cache on a quiet chain
    (2026-10-15). Empty by default; malformed means empty."""
    raw = os.getenv("MEMECOIN_EVM_GATE_BALANCE_WARM_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_gate_balance_warm_s():
    """Seconds between gate balance warms. Must stay under MEMECOIN_EVM_EARLY_SIZING_CACHE_S;
    malformed, non-finite or under 5 s means 60."""
    try:
        value = float(os.getenv("MEMECOIN_EVM_GATE_BALANCE_WARM_S", "60"))
    except ValueError:
        return 60.0
    return value if math.isfinite(value) and value >= 5.0 else 60.0


def memecoin_evm_quote_price_warmer_chains():
    """Call-time at context build: EVM chains whose quote-token USD price a background warmer keeps
    inside the early quote's 1 s window. Code default "56", which is today's hard-coded chain;
    malformed means the default."""
    raw = os.getenv("MEMECOIN_EVM_QUOTE_PRICE_WARMER_CHAINS", "56")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if any(not chain.isdigit() for chain in chains):
        return frozenset({"56"})
    return frozenset(chains)


def memecoin_evm_source_pair_seed_chains():
    """Call-time: chains whose ENTRY V2 path search tries the source receipt's counter-token before
    DexScreener (2026-10-15, evm_source_route.seed_pairs). Empty by default; malformed means
    empty. Clearing it is today's search exactly."""
    raw = os.getenv("MEMECOIN_EVM_SOURCE_PAIR_SEED_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_bsc_fast_lane_floor_at_cap_chains():
    """Call-time: chains whose BSC fast lane signs its min-out at the worst price the slow lanes
    accept (the lane cap and their post-trade premium bound) and sends ONLY to the private builders
    (2026-10-15, Alpha (B)). Empty by default; malformed means empty: today's floor and send set."""
    raw = os.getenv("MEMECOIN_BSC_FAST_LANE_FLOOR_AT_CAP_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_rh_fast_feed_hold():
    """Call-time: the 4663 feed detector holds a signal's (tx, mint) for the runner's live rh lane at
    the feed's arrival, before its receipt wait, so the on-chain lane cannot start first
    (2026-10-16, Alpha D-b; CLAWDMKT: the on-chain lane began 61 ms before the lane's datagram).
    1/on/true/yes = on; anything else off: the hold starts at the lane's datagram, as before."""
    return os.getenv("MEMECOIN_RH_FAST_FEED_HOLD", "").strip().lower() in ("1", "on", "true", "yes")


def memecoin_evm_refresh_pre_source_state():
    """Call-time: the executor re-asks the winning rung when its quote was priced on state from a
    block BEFORE the source's own trade (`route.state_block` < source block), however young the
    quote is (2026-10-16, Alpha D-c; CLAWDMKT: the Pons counter cache priced 2.8% under the source's
    average, the preflight reverted and the re-quote read +25.5%). Default ON; 0/off/false/no is the
    kill knob, and then only the quote's age triggers a refresh, as before."""
    return os.getenv("MEMECOIN_EVM_REFRESH_PRE_SOURCE_STATE", "1").strip().lower() not in (
        "0", "off", "false", "no")


def memecoin_bsc_fast_low_rtt_reads():
    """Call-time: the 56 fast lane's cold-signal read families that go to the funded 56 pool's Alchemy
    member while it is healthy (Alpha GO 2026-10-16, option b; memecoin_bsc_fast_lane.LowRttReads):
    "facts" (decimals + totalSupply batch) and "safety" (the intent's and cold signal's inline token
    safety read). Default both, ON. Empty, "0", "off" or any unknown name: none (the lane host)."""
    raw = os.getenv("MEMECOIN_BSC_FAST_LOW_RTT_READS", "facts,safety")
    parts = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not parts or parts - {"facts", "safety"}:
        return frozenset()
    return frozenset(parts)


# Per-attempt trace substages a lane measured before the attempt existed: the on-chain lane's
# native_lane_hold (D-b feed hold) and the EVM lane's evm.intent_warm (C1). Measurement only,
# marked in memory as the trace starts (2026-10-17, Alpha). 0 marks nothing.
MEMECOIN_PRE_TRACE_SUBSTAGES = os.getenv("MEMECOIN_PRE_TRACE_SUBSTAGES", "1").strip().lower() not in {"0", "false", "no"}


def memecoin_evm_lane_intent_warm_chains():
    """Call-time: chains whose watcher sends the fast lane an intent datagram at a source log's
    arrival, so the lane starts the coin's inline reads (market stats, facts, the parallel safety
    read) before the receipt decode lands (2026-10-16, Alpha C1). Empty by default; malformed means
    empty: no intent is sent or accepted, and the signal reads as before."""
    raw = os.getenv("MEMECOIN_EVM_LANE_INTENT_WARM_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_fast_lane_native_equity_chains():
    """Call-time: chains whose fast-lane entries (rh_fast_lane, bsc_fast_lane, relay_prebuy) value
    spare native in their sizing equity like every other entry (2026-10-15, operator "whatever
    would be fastest"). Empty by default and malformed means empty: those entries then size on
    wrapped cash alone, as before."""
    raw = os.getenv("MEMECOIN_EVM_FAST_LANE_NATIVE_EQUITY_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_bridge_prefix_chains():
    """Call-time: chains whose source-hinted multihop entry caches the whole cash->X prefix (1 or 2
    legs) and measures it as one pinned path in its own ladder rung, v4_multihop_pinned, beside
    today's discovery (2026-10-15, operator "fill fastest"). Empty by default; malformed means
    empty. Clearing it is today's 2-leg cache exactly."""
    raw = os.getenv("MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_bridge_cache_chains():
    """Call-time: chains whose source-hinted multihop entry first tries the cash->bridge pool that
    last won for that bridge currency, before enumerating bridge pools (2026-10-15, operator "fill
    fastest"; evm_source_route.bridge_for). Empty by default; malformed means empty. Clearing it is
    today's rung exactly."""
    raw = os.getenv("MEMECOIN_EVM_BRIDGE_CACHE_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_lane_shared_hint_chains():
    """Call-time: chains whose rh fast lane puts a v4_shared signal's receipt-bound source pool on
    its chase reference as `hint_pool`, for the entry ladder's source rungs (2026-10-15). Empty by
    default; malformed means empty. Clearing it leaves the post exactly as before."""
    raw = os.getenv("MEMECOIN_EVM_LANE_SHARED_HINT_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


_RELAY_ENVELOPE_BEFORE_CHAIN_1 = frozenset({"4663", "56", "8453"})


def memecoin_evm_relay_envelope_chains():
    """Call-time: chains whose on-chain watcher decodes the Relay Permit2 envelope (any swap target
    behind it) as an armed wallet's buy (memecoin_evm_onchain_watch.relayer_purchase_from_receipt).
    Unset = 4663,56,8453,1: chain 1 since 2026-10-17 (Alpha GO; 69 of 72 J7-proved chain-1 buys in 7
    days were this envelope and the watcher logged every one as no_buy). An explicit empty value or a
    malformed one = 4663,56,8453, the set before chain 1. A listed chain still needs the pinned Relay
    proxy and router code hashes on that chain."""
    raw = os.getenv("MEMECOIN_EVM_RELAY_ENVELOPE_CHAINS")
    if raw is None:
        return _RELAY_ENVELOPE_BEFORE_CHAIN_1 | {"1"}
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return _RELAY_ENVELOPE_BEFORE_CHAIN_1
    return frozenset(chains)


def memecoin_evm_source_route_hint_chains():
    """Call-time: chains whose entry ladder also quotes the pool the source itself bought through
    (its receipt decode on the event), beside today's rungs (2026-10-15, operator "fill fastest").
    Empty by default; malformed means empty. Read per quote, so clearing it is today's ladder."""
    raw = os.getenv("MEMECOIN_EVM_SOURCE_ROUTE_HINT_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def _chains_default_4663(name):
    """Unset means {"4663"}; an explicit empty value turns it off; malformed means off."""
    raw = os.getenv(name)
    if raw is None:
        return frozenset({"4663"})
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_safety_exit_hint_chains():
    """Call-time: chains whose pre-entry safety sell probe also quotes the exit through the V4 pool on
    the source's own decoded receipt, first in priority (2026-10-16, Alpha GO; evm_source_route.
    safety_exit_hint). Clearing it is today's sell probe exactly."""
    return _chains_default_4663("MEMECOIN_EVM_SAFETY_EXIT_HINT_CHAINS")


def memecoin_evm_permit2_slot_prewarm_chains():
    """Call-time: chains whose safety check discovers a Permit2 sell's balance and allowance slot
    LAYOUTS beside the sell quote (2026-10-16, Alpha GO; evm_token_safety). Layout only: the live
    allowance read and the amount decision stay in the simulation step. Clearing it is today's
    serial discovery exactly."""
    return _chains_default_4663("MEMECOIN_EVM_PERMIT2_SLOT_PREWARM_CHAINS")


def memecoin_desk_cluster_per_min():
    """Call-time: desk cluster checks per minute (memecoin_desk_cluster). Each check is one holder
    read plus a first-funder trace per uncached owner on the research Helius RPC. Default 6;
    malformed or negative means 0 (no checks; every ticket stays cluster-unknown)."""
    try:
        value = int(os.getenv("MEMECOIN_DESK_CLUSTER_PER_MIN", "6"))
    except ValueError:
        return 0
    return max(0, value)


_DESK_CLUSTER_CALL_DEFAULTS = {"CANDIDATE": 40, "MIN": 300, "DAY": 20000}


def memecoin_desk_cluster_max_age_s():
    """Call-time: the oldest U emission (seconds) the desk cluster worker still decides. Default 60:
    Lambda's assembler stops waiting for the cluster row 60 s after emission. Malformed = 60."""
    try:
        value = float(os.getenv("MEMECOIN_DESK_CLUSTER_MAX_AGE_S", "60"))
    except ValueError:
        return 60.0
    return value if value > 0 else 60.0


def memecoin_desk_cluster_gap_s():
    """Call-time: the desk cluster worker's own pacer gap on the shared research key (default 1.1, the
    global research GAP_S, which other readers keep). Helius Developer allows 50 req/s; the desk asks
    for a few per second. Floor 0.1 s: a typo never opens the key wide. Malformed = 1.1."""
    try:
        value = float(os.getenv("MEMECOIN_DESK_CLUSTER_GAP_S", "1.1"))
    except ValueError:
        return 1.1
    return min(max(value, 0.1), 10.0) if value == value else 1.1


def memecoin_desk_cluster_funded_by():
    """Call-time kill knob for the desk funded-by fallback (default ON; the trigger cohort froze it, preflight blocks a cohort that
    froze it while this is 0). 0/false/off/no switch it off: the worker then runs the v2 definition under its own cache key."""
    return os.getenv("MEMECOIN_DESK_CLUSTER_FUNDED_BY", "1").strip().lower() not in ("0", "false", "off", "no", "")


def memecoin_desk_cluster_funded_by_audit():
    """Call-time: every Nth parser-resolved owner (stable hash) is also probed through funded-by for a COUNTED comparison, never used
    (default 8; 0 = off; max 1000). Needs 8 spare calls in the check's budget. Malformed = 0."""
    try:
        value = int(os.getenv("MEMECOIN_DESK_CLUSTER_FUNDED_BY_AUDIT_N", "8"))
    except ValueError:
        return 0
    return min(max(value, 0), 1000)


def memecoin_desk_cluster_workers():
    """Call-time: parallel owner traces inside ONE desk cluster check (default 1, max 4). They share
    the check's call budget and the key's pacer, so more workers shorten a check, never add calls."""
    try:
        value = int(os.getenv("MEMECOIN_DESK_CLUSTER_WORKERS", "1"))
    except ValueError:
        return 1
    return min(max(value, 1), 4)


def memecoin_desk_cluster_calls(scope):
    """Call-time research-call caps of the desk cluster worker: MEMECOIN_DESK_CLUSTER_CALLS_PER_
    {CANDIDATE (40), MIN (300), DAY (20000)}. The shared research pacer (1.1 s gap) bounds a minute
    to ~55 calls anyway; the day cap protects the research key's credits. Malformed or negative = 0
    (no calls; every ticket stays cluster-unknown)."""
    try:
        value = int(os.getenv("MEMECOIN_DESK_CLUSTER_CALLS_PER_%s" % scope,
                              str(_DESK_CLUSTER_CALL_DEFAULTS[scope])))
    except ValueError:
        return 0
    return max(0, value)


def memecoin_evm_standing_allowance_chains():
    """Call-time: chains whose entries spend a standing quote-token allowance on the pinned routers
    instead of an exact approve per entry (spec 2026-10-07-bsc-standing-allowance-design.md; only
    56 has pinned routers). Empty by default; malformed means empty. Needs the approval pipeline."""
    raw = os.getenv("MEMECOIN_EVM_STANDING_ALLOWANCE_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_standing_allowance_cap_usd():
    """Call-time: the USD worth of quote token each pinned router's standing allowance is topped
    up to. Never MAX (operator). A malformed or non-positive value turns top-ups off."""
    try:
        cap = float(os.getenv("MEMECOIN_EVM_STANDING_ALLOWANCE_CAP_USD", "500"))
    except ValueError:
        return 0.0
    return cap if cap > 0 else 0.0


def memecoin_rh_fast_lane_mode():
    """Call-time: the 4663 fast lane's mode (spec 2026-10-01-memecoin-4663-fast-lane-design.md).
    "shadow" runs the read-only feed shadow (M0). "runner_shadow" also runs it and starts the
    in-runner lane thread, which signs, reserves and sends nothing (M2 step 1). "live" runs the
    same thread with money (M2 step 3): the $50 pilot clip, at most 20 fills and 6 sends an hour;
    setting it is the operator's call after Gamma's grade. Anything else is off."""
    raw = os.getenv("MEMECOIN_RH_FAST_LANE", "").strip().lower()
    return raw if raw in ("shadow", "runner_shadow", "live") else ""


def memecoin_bsc_fast_lane_mode():
    """Call-time: the BSC 56 fast lane's mode (operator via Alpha 2026-10-02). "runner_shadow" makes
    the 56 watcher send its receipt decisions to the runner's 56 lane thread, which judges them and
    signs, reserves and sends nothing; "live" is the same with money, the operator's call once a 56
    RPC key and the native BNB float exist. Anything else is off (the default)."""
    raw = os.getenv("MEMECOIN_BSC_FAST_LANE", "").strip().lower()
    return raw if raw in ("runner_shadow", "live") else "off"


def memecoin_base_fast_lane_mode():
    """Call-time: the Base 8453 fast lane's mode (spec 2026-10-02-memecoin-base-8453-fast-lane-design,
    rev 3; step 1 plan 2026-10-14). "step0" makes the 8453 watcher decide from each source receipt
    after today's hand-off and RECORD the decision (base_fast_decisions); nothing is sent, signed or
    reserved. "runner_shadow" also sends each enabled would-send decision to the runner's Base lane,
    which judges it with every funded gate and signs, reserves and sends nothing; "live" is the same
    with money (the operator's word after Gamma's shadow read). Anything else is off (the default),
    and the watcher skips the hook."""
    raw = os.getenv("MEMECOIN_BASE_FAST_LANE", "").strip().lower()
    return raw if raw in ("step0", "runner_shadow", "live") else "off"


def _base_fast_routes(raw):
    """MEMECOIN_BASE_FAST_ROUTES: the route kinds the Base lane may act on, default none. v1 knows
    v4_native and v4_weth only (plan 2026-10-14: no hop-A pool, so no v4_bridge); anything else
    refuses the setting rather than being dropped silently."""
    kinds = tuple(k for k in (x.strip().lower() for x in str(raw or "").split(",")) if k)
    bad = [k for k in kinds if k not in ("v4_native", "v4_weth")]
    if bad:
        raise ValueError("MEMECOIN_BASE_FAST_ROUTES names %s; allowed: v4_native, v4_weth"
                         % ",".join(bad))
    return kinds


MEMECOIN_BASE_FAST_ROUTES = _base_fast_routes(os.getenv("MEMECOIN_BASE_FAST_ROUTES", ""))


def memecoin_base_fast_lane_rpc_url():
    """Call-time: the Base lane's OWN 8453 host (operator D1, 2026-10-14) for its safety cache, coin
    facts and wallet snapshot. Empty, or a URL another role or funded pool uses, keeps the lane off."""
    return os.getenv("MEMECOIN_BASE_FAST_LANE_RPC_URL", "").strip()


MEMECOIN_BASE_FAST_LANE_PILOT_FILLS = int(os.getenv("MEMECOIN_BASE_FAST_LANE_PILOT_FILLS", "20"))
MEMECOIN_BASE_FAST_LANE_MAX_SENDS_PER_HOUR = int(os.getenv(
    "MEMECOIN_BASE_FAST_LANE_MAX_SENDS_PER_HOUR", "6"))
# The wallet lock is held through the receipt for at most this long (Base blocks are 2 s).
MEMECOIN_BASE_FAST_LANE_LOCK_RECEIPT_CAP_S = float(os.getenv(
    "MEMECOIN_BASE_FAST_LANE_LOCK_RECEIPT_CAP_S", "4"))
MEMECOIN_BASE_FAST_LANE_SNAPSHOT_MAX_AGE_S = float(os.getenv(
    "MEMECOIN_BASE_FAST_LANE_SNAPSHOT_MAX_AGE_S", "30"))
# Four reads a poll on a free public host: one poll every 10 s is 0.4 calls a second.
MEMECOIN_BASE_FAST_LANE_SNAPSHOT_PERIOD_S = float(os.getenv(
    "MEMECOIN_BASE_FAST_LANE_SNAPSHOT_PERIOD_S", "10"))
# The watcher's ETH/USD mark is peeked at most 30 s old; the lane allows the datagram's stamp this long.
MEMECOIN_BASE_FAST_LANE_ETH_USD_MAX_AGE_S = float(os.getenv(
    "MEMECOIN_BASE_FAST_LANE_ETH_USD_MAX_AGE_S", "60"))
MEMECOIN_BASE_FAST_LANE_GENERATION_MARGIN_S = float(os.getenv(
    "MEMECOIN_BASE_FAST_LANE_GENERATION_MARGIN_S", "1"))
# THE BASE LANE'S NATIVE FLOAT (Alpha/operator D3, 2026-10-14: "adjust for whatever we have"). While
# MEMECOIN_BASE_FAST_LANE=live the 8453 gas pass raises its target by this float, and
# balance_bound_float caps the result at the gas-only target plus half the wallet's liquid
# ETH+WETH above it. Default: the $150 clip at ~$2,500/ETH; 0.0309 ETH on 2026-10-14's balances.
MEMECOIN_BASE_FAST_LANE_FLOAT_WEI = _native_wei_env("MEMECOIN_BASE_FAST_LANE_FLOAT_NATIVE", "0.06")
MEMECOIN_BASE_FAST_SAFETY_TTL_S = float(os.getenv("MEMECOIN_BASE_FAST_SAFETY_TTL_S", "60"))
MEMECOIN_BASE_FAST_SAFETY_MIN_GAP_S = float(os.getenv("MEMECOIN_BASE_FAST_SAFETY_MIN_GAP_S", "0.5"))
MEMECOIN_BASE_FAST_INLINE_SAFETY_DEADLINE_S = float(os.getenv(
    "MEMECOIN_BASE_FAST_INLINE_SAFETY_DEADLINE_S", "2"))


# The clip step 0 sizes its min_out with (the BSC and 4663 lanes' pilot clip). The funded per-entry
# ceiling for 8453 binds it when that ceiling is set (non-zero).
MEMECOIN_BASE_FAST_LANE_CLIP_USD = float(os.getenv("MEMECOIN_BASE_FAST_LANE_CLIP_USD", "50"))


MEMECOIN_BSC_FAST_LANE_PILOT_CLIP_USD = float(os.getenv("MEMECOIN_BSC_FAST_LANE_PILOT_CLIP_USD", "50"))
MEMECOIN_BSC_FAST_LANE_PILOT_FILLS = int(os.getenv("MEMECOIN_BSC_FAST_LANE_PILOT_FILLS", "20"))
MEMECOIN_BSC_FAST_LANE_MAX_SENDS_PER_HOUR = int(os.getenv("MEMECOIN_BSC_FAST_LANE_MAX_SENDS_PER_HOUR", "6"))
# The wallet lock is held through the receipt for at most this long (BSC blocks are 0.45 s).
MEMECOIN_BSC_FAST_LANE_LOCK_RECEIPT_CAP_S = float(os.getenv(
    "MEMECOIN_BSC_FAST_LANE_LOCK_RECEIPT_CAP_S", "3"))
MEMECOIN_BSC_FAST_LANE_SNAPSHOT_MAX_AGE_S = float(os.getenv(
    "MEMECOIN_BSC_FAST_LANE_SNAPSHOT_MAX_AGE_S", "30"))
MEMECOIN_BSC_FAST_LANE_SNAPSHOT_PERIOD_S = float(os.getenv(
    "MEMECOIN_BSC_FAST_LANE_SNAPSHOT_PERIOD_S", "2"))
# The validators' minimum gas price on BSC (0.05 gwei); the lane never bids under it.
MEMECOIN_BSC_FAST_LANE_GAS_FLOOR_WEI = int(os.getenv("MEMECOIN_BSC_FAST_LANE_GAS_FLOOR_WEI", "50000000"))


def _scaled_env(name, default, scale):
    """A non-negative decimal knob as an exact integer (value x `scale`), or None when it is empty,
    malformed, negative or not finite: the caller reads None as off, never as a crash at import."""
    try:
        value = Decimal(str(os.getenv(name, default)).strip())
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0:
        return None
    return int(value * scale)


# 56 FAST-LANE CROWDED GAS BID (operator YES 2026-10-16 via Alpha, a live test; memecoin_bsc_crowded_gas).
# An early private-only send signs gasPrice = max(static, min(target, ceiling)); a quarter of the early
# sends are held out at the static price. Target 0, empty or malformed = off (today's 0.05 gwei); a
# malformed ceiling or daily cap is off too. EARLY_MS: an eligible send's arm time is at most this many
# ms after the source block's header milliTimestamp (Gamma 2026-10-16: 4 of 18 sends within 400 ms,
# 9 of 18 within 600). The daily cap counts each treated send's 400k-gas bound until its receipt's fee
# replaces it; at the cap both arms bid static until UTC midnight.
MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_WEI = _scaled_env("MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_GWEI", "25", 10 ** 9)
MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_MAX_WEI = _scaled_env(
    "MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_MAX_GWEI", "30", 10 ** 9)
MEMECOIN_BSC_FAST_LANE_CROWDED_DAILY_WEI = _scaled_env(
    "MEMECOIN_BSC_FAST_LANE_CROWDED_DAILY_BNB", "0.05", 10 ** 18)
MEMECOIN_BSC_FAST_LANE_CROWDED_EARLY_MS = _scaled_env("MEMECOIN_BSC_FAST_LANE_CROWDED_EARLY_MS", "600", 1)
MEMECOIN_BSC_FAST_SAFETY_TTL_S = float(os.getenv("MEMECOIN_BSC_FAST_SAFETY_TTL_S", "60"))
MEMECOIN_BSC_FAST_SAFETY_MIN_GAP_S = float(os.getenv("MEMECOIN_BSC_FAST_SAFETY_MIN_GAP_S", "0.2"))
# THE LANE'S NATIVE FLOAT ON 56 (Alpha 2026-10-02, option (b)). While MEMECOIN_BSC_FAST_LANE=live the
# 56 gas pass never wraps native below its low-water mark plus this float (the lane pays native BNB,
# and the pass otherwise wraps everything above ~0.007 BNB back into WBNB within 15 s). Size it at
# the clip in BNB plus gas: $50 at $772 is 0.065 BNB.
MEMECOIN_BSC_FAST_LANE_FLOAT_WEI = _native_wei_env("MEMECOIN_BSC_FAST_LANE_FLOAT_NATIVE", "0.1")
# The refill, default OFF: native under this mark is unwrapped from WBNB back up to that target. Used
# only between the low-water mark and the target, like 4663's MEMECOIN_EVM_NATIVE_FLOAT_REFILL_BELOW.
MEMECOIN_BSC_FAST_LANE_FLOAT_REFILL_BELOW_WEI = _native_wei_env(
    "MEMECOIN_BSC_FAST_LANE_FLOAT_REFILL_BELOW_NATIVE", "0")
# A cache miss runs the safety check inline on the lane's own host (Alpha 2026-10-02); past this
# whole-check deadline the signal falls back to today's path.
MEMECOIN_BSC_FAST_INLINE_SAFETY_DEADLINE_S = float(os.getenv(
    "MEMECOIN_BSC_FAST_INLINE_SAFETY_DEADLINE_S", "2"))


def memecoin_bsc_fast_lane_rpc_url():
    """Call-time: the BSC lane's OWN 56 host (the operator's key) for its safety cache, coin facts
    and wallet snapshot. Empty means live refuses at start; never a funded or shared host."""
    return os.getenv("MEMECOIN_BSC_FAST_LANE_RPC_URL", "").strip()


def memecoin_bsc_blockrazor_auth():
    """Call-time: the operator's BlockRazor auth token (optional). Never logged."""
    return os.getenv("MEMECOIN_BSC_BLOCKRAZOR_AUTH", "").strip()


# The quote tokens whose WBNB pairs the bsc_v2_live tape records for the BSC lane's second hop
# (measured 2026-10-02: the meme quotes our 56 sources' PancakeSwap V2 buys used, 21 of 79).
MEMECOIN_BSC_FAST_LANE_HOP_QUOTES = tuple(
    t.strip().lower() for t in os.getenv(
        "MEMECOIN_BSC_FAST_LANE_HOP_QUOTES",
        "0x205812cdbed920aff76c6580abd681a46d11efc7,0x4902c5ebc598265ed2212b559b042de8a5eeec3f,"
        "0x25d887ce7a35172c62febfd67a1856f20faebb00,0x9b8e987e6fec8cf1380c4dca7071e2c7853aeea1"
    ).split(",") if t.strip())


def memecoin_rh_fast_decide_shadow():
    """Call-time: M1 of the 4663 fast lane, the sign-less decide shadow inside the M0 shadow process
    (it needs MEMECOIN_RH_FAST_LANE=shadow as well). Off by default; it reads the tape and the 4663
    research host only, never a funded host."""
    return os.getenv("MEMECOIN_RH_FAST_DECIDE_SHADOW", "").strip().lower() in ("1", "true", "yes", "on")


# 4663 fast lane (spec 2026-10-01 §2.4): a pool state counts as absent when the swap tape's newest
# block is more than this many blocks behind the source's (the tape runs ~0.5 s, 4-5 blocks, behind),
# and the source's payment is cut by this share (Relay + Kyber + hook fees) before its swap is
# modelled, so the estimate errs small. One M0 fill measured 0.26%; M1 measures it.
MEMECOIN_RH_FAST_TAPE_MAX_LAG_BLOCKS = int(os.getenv("MEMECOIN_RH_FAST_TAPE_MAX_LAG_BLOCKS", "8"))
MEMECOIN_RH_FAST_SOURCE_FEE_PCT = float(os.getenv("MEMECOIN_RH_FAST_SOURCE_FEE_PCT", "1"))
# The runner's lane (M2). The pilot (operator 2026-10-01 via Alpha): a $50 clip for the first 20
# lane fills, at most 6 lane sends an hour. The detector's research-host snapshot (balances,
# pending nonce, gas) is trusted for this long and only when read after the last execution on the
# wallet; the tape's ETH/USD (refreshed every 30 s) for this long. Older is absent: a fallback.
MEMECOIN_RH_FAST_LANE_PILOT_CLIP_USD = float(os.getenv("MEMECOIN_RH_FAST_LANE_PILOT_CLIP_USD", "50"))
MEMECOIN_RH_FAST_LANE_PILOT_FILLS = int(os.getenv("MEMECOIN_RH_FAST_LANE_PILOT_FILLS", "20"))
# The pilot sends only on an EXACT post-trade state (the tape already held the source's own swap in
# our pool), never on the single-range estimate (Beta's rule-5 delta 2026-10-01, Gamma (3)).
MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE = os.getenv(
    "MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE", "1").strip().lower() not in ("0", "false", "off", "no")
# 4663 fast lane option C (operator yes 2026-10-07): a Relay buy's initiation proved from the armed
# user's own signed Solana deposit at processed commitment (memecoin_rh_deposit_proof). Read by the
# feed process (the socket; MEMECOIN_RH_FAST_DEPOSIT_HELIUS_URL is its key) and by the runner (the
# lane and source_initiation_refusal). Off: every signal takes the receipt path.
MEMECOIN_RH_FAST_DEPOSIT_PROOF = os.getenv(
    "MEMECOIN_RH_FAST_DEPOSIT_PROOF", "0").strip().lower() in ("1", "on", "true", "yes")
# 4663 RECEIPT HINT (Alpha GO 2026-10-16, memecoin_receipt_hint): a deposit-matched re-ask still waiting
# for the source's receipt proof this long after it was queued asks the 4663 watcher to read that
# receipt now; the watcher runs its own detection on it. Read by the feed process (the sender) and the
# 4663 watcher (the listener), each at start. Default ON (operator 2026-10-15: improvements go live with
# a kill knob); anything but 0/false/off/no is on. Off: no datagram, no listener, today's 2 s wait.
MEMECOIN_RH_FAST_RECEIPT_FETCH = os.getenv(
    "MEMECOIN_RH_FAST_RECEIPT_FETCH", "1").strip().lower() not in ("0", "false", "off", "no")
try:
    MEMECOIN_RH_FAST_RECEIPT_FETCH_AFTER_S = float(os.getenv("MEMECOIN_RH_FAST_RECEIPT_FETCH_AFTER_S", "0.4"))
except ValueError:
    MEMECOIN_RH_FAST_RECEIPT_FETCH_AFTER_S = 0.4
if not (math.isfinite(MEMECOIN_RH_FAST_RECEIPT_FETCH_AFTER_S) and 0 < MEMECOIN_RH_FAST_RECEIPT_FETCH_AFTER_S < 2):
    MEMECOIN_RH_FAST_RECEIPT_FETCH_AFTER_S = 0.4
# 4663 BRIDGE FRESH READ (Alpha GO 2026-10-16). A two-hop buy (native -> X -> coin) is refused
# bridge_state_stale when leg A (the native/X V4 pool) has not swapped within the lag bound, though the
# tape is current and the coin's pool passes: 10 final refusals 10-14..10-16, leg-A age p50 182 blocks.
# Then the decide reads leg A's slot0 + liquidity ONCE per signal (one extsload eth_call at the signal's
# block, or the tape head if newer) from MEMECOIN_ONCHAIN_EVM_TX_READ_URL_4663, bounded by the timeout
# below; no answer keeps today's refusal. Default ON (operator 2026-10-15: improvements go live with a
# kill knob); 0/false/off/no = off. The timeout must be in (0, 1] s, else 0.2.
MEMECOIN_RH_FAST_BRIDGE_FRESH_READ = os.getenv(
    "MEMECOIN_RH_FAST_BRIDGE_FRESH_READ", "1").strip().lower() not in ("0", "false", "off", "no")
try:
    MEMECOIN_RH_FAST_BRIDGE_FRESH_READ_TIMEOUT_S = float(os.getenv("MEMECOIN_RH_FAST_BRIDGE_FRESH_READ_TIMEOUT_S", "0.2"))
except ValueError:
    MEMECOIN_RH_FAST_BRIDGE_FRESH_READ_TIMEOUT_S = 0.2
if not (math.isfinite(MEMECOIN_RH_FAST_BRIDGE_FRESH_READ_TIMEOUT_S) and 0 < MEMECOIN_RH_FAST_BRIDGE_FRESH_READ_TIMEOUT_S <= 1):
    MEMECOIN_RH_FAST_BRIDGE_FRESH_READ_TIMEOUT_S = 0.2
# 4663 RELAY PREWARM (P2) AND PRE-BUY (P3) (spec 2026-10-14-rh-relay-prebuy-design.md; Alpha-approved).
# A verified deposit's Relay request names the destination mint about 260 ms before the fill reaches our
# feed. PREWARM warms the lane for that coin (no decision input). PREBUY buys before the source's fill,
# through the full funded hook, the chase reference replaced by our impact vs the pool's spot.
MEMECOIN_RH_RELAY_PREWARM = os.getenv(
    "MEMECOIN_RH_RELAY_PREWARM", "0").strip().lower() in ("1", "on", "true", "yes")
MEMECOIN_RH_RELAY_PREBUY = os.getenv(
    "MEMECOIN_RH_RELAY_PREBUY", "0").strip().lower() in ("1", "on", "true", "yes")
# OPERATOR ORDER "Live now, small size" (2026-10-14, via Alpha): a fixed $20 clip, at most 4 sends an
# hour, and the pre-buy stops itself after 20 fills (and after 3 consecutive reverts) and reports.
MEMECOIN_RH_RELAY_PREBUY_CLIP_USD = float(os.getenv("MEMECOIN_RH_RELAY_PREBUY_CLIP_USD", "20"))
MEMECOIN_RH_RELAY_PREBUY_MAX_PER_HOUR = int(os.getenv("MEMECOIN_RH_RELAY_PREBUY_MAX_PER_HOUR", "4"))
MEMECOIN_RH_RELAY_PREBUY_PILOT_FILLS = int(os.getenv("MEMECOIN_RH_RELAY_PREBUY_PILOT_FILLS", "20"))
MEMECOIN_RH_RELAY_PREBUY_MAX_REVERTS = int(os.getenv("MEMECOIN_RH_RELAY_PREBUY_MAX_REVERTS", "3"))
# Our own impact against the pool's current spot: the cap (spot x (1 + pct)) and min_out come from it.
MEMECOIN_RH_RELAY_PREBUY_MAX_IMPACT_PCT = float(os.getenv("MEMECOIN_RH_RELAY_PREBUY_MAX_IMPACT_PCT", "5"))
# The Relay request poller: per deposit at most POLL_MAX asks, POLL_PER_S a second (Alpha: 6/s, headroom
# under Relay's 10/s per key beside the watchers' verify calls); the first 429 or 5xx stops it.
MEMECOIN_RH_RELAY_POLL_MAX = int(os.getenv("MEMECOIN_RH_RELAY_POLL_MAX", "8"))
MEMECOIN_RH_RELAY_POLL_PER_S = float(os.getenv("MEMECOIN_RH_RELAY_POLL_PER_S", "6"))
# BSC 56 RELAY PRE-BUY (spec 2026-10-14-bsc-relay-prebuy-design.md; Alpha-approved, same operator order
# "live now, small size"): the 4663 pre-buy's rules and the knobs above (clip, caps, impact, poller), on
# the BSC lane's executor, with counters and streaks of its own. Default off.
MEMECOIN_BSC_RELAY_PREBUY = os.getenv(
    "MEMECOIN_BSC_RELAY_PREBUY", "0").strip().lower() in ("1", "on", "true", "yes")
# Relay option A (spec 2026-10-09 relay deposit proof on the on-chain lane): the funded EVM chains whose
# ON-CHAIN-lane Relay buys may be proved from the armed user's own signed Solana deposit, read from the
# shared store the deposit socket writes (MEMECOIN_RELAY_DEPOSIT_STORE_PATH). The socket also watches
# these chains' armed users. Empty (default): today's Relay-API-only path on every chain. A name that
# is not a funded EVM chain refuses to load: a typo must not read as "off".
_RELAY_DEPOSIT_CHAINS_ALLOWED = ("4663", "8453", "56", "1", "5042")


def _relay_deposit_chains(raw):
    chains = frozenset(part.strip() for part in str(raw or "").split(",") if part.strip())
    bad = sorted(chains - set(_RELAY_DEPOSIT_CHAINS_ALLOWED))
    if bad:
        raise ValueError("MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS names %s; allowed: %s"
                         % (",".join(bad), ",".join(_RELAY_DEPOSIT_CHAINS_ALLOWED)))
    return chains


MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS = _relay_deposit_chains(os.getenv("MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", ""))
MEMECOIN_RELAY_DEPOSIT_STORE_PATH = os.getenv(
    "MEMECOIN_RELAY_DEPOSIT_STORE_PATH", str(state_path("data", "relay_deposit_proofs.db")))
# Relay option A section 5 (Alpha 2026-10-09, option ii; the operator may veto): the label-only
# pre-gate beside the Relay wait (memecoin_relay_pregate).
#   off (default): no label, today's wait.
#   shadow: the label is computed and recorded on the row (relay_pregate) while the row still waits as
#           today; scripts/research/relay_pregate_agreement.py --live grades it against the runner.
#   on (1/true/yes): a labelled row is published at once with the hook's reason; its proof records late.
# Activation (Gamma M2): shadow first; on only after >= 30 graded labels with 0 that refuse what the
# runner admitted.
def _relay_pregate_mode(raw):
    value = str(raw or "").strip().lower()
    if value in ("1", "on", "true", "yes"):
        return "on"
    return "shadow" if value == "shadow" else "off"


MEMECOIN_RELAY_PREGATE = _relay_pregate_mode(os.getenv("MEMECOIN_RELAY_PREGATE", "off"))
# Option C's v4_shared (Alpha GO 2026-10-08, standing approval e97e4343): a deposit-proved buy whose
# source swapped in a V4 pool other than the coin's busiest native pool is bought through THAT pool
# (native, WETH, or native -> X -> it), priced on the source's own tape swap, instead of the shared
# executor that needs the receipt. Off (Gamma: its own step): today's hand-off, refused
# source_fill_unproved without a receipt.
MEMECOIN_RH_FAST_DEPOSIT_SOURCE_POOL = os.getenv(
    "MEMECOIN_RH_FAST_DEPOSIT_SOURCE_POOL", "0").strip().lower() in ("1", "on", "true", "yes")
# A V4 hook's take of a swap's OUTPUT, in bps, by hook address: applied to the lane's estimate of
# what a source-pool buy delivers, never to its cap. 0xe5e702641e: 2.700% of output on our fills and
# the sources' alike (Gamma, brain 661f864e). Format "hook=bps,hook=bps".
MEMECOIN_RH_FAST_HOOK_OUTPUT_TAKE_BPS = {
    k.strip().lower(): int(v) for k, v in (
        item.split("=", 1) for item in os.getenv(
            "MEMECOIN_RH_FAST_HOOK_OUTPUT_TAKE_BPS",
            "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044=270").split(",") if "=" in item)}


def _address_set(raw):
    """Lowercased addresses from a comma list; one malformed entry makes the whole list empty."""
    items = [part.strip().lower() for part in str(raw or "").split(",") if part.strip()]
    ok = all(len(a) == 42 and a.startswith("0x") and all(c in "0123456789abcdef" for c in a[2:])
             for a in items)
    return frozenset(items) if ok else frozenset()


# 4663 V4 hooks whose afterSwap runs its own swaps on the pool inside the user's transaction (the
# launchpad hook 0x4e3468951d's per-token fee processors: a tax sell, sometimes a small buy, after the
# user's buy). With a hook listed, a source transaction's rows on such a pool read as ONE source when
# the first is a buy and every later row comes from one other sender: the source's amounts are the
# first row's, the post state the last row's. Read by the fast decide's tape and by the shared receipt
# decoder (memecoin_evm_post_trade._decode). Empty (default) or malformed = today's refusal.
MEMECOIN_4663_SELF_SWAP_HOOKS = _address_set(os.getenv("MEMECOIN_4663_SELF_SWAP_HOOKS", ""))
# 4663 fast lane cache miss (Alpha GO (c), 2026-10-08): start the inline safety read beside the
# inline inputs instead of after them (memecoin_rh_fast_lane.LiveLane._start_safety). Off: today's
# sequential order.
def memecoin_bsc_fast_parallel_safety():
    """Call-time: the 56 fast lane starts its cold inline safety read beside the gate inputs (Codex
    223c22065), as the 4663 lane does under MEMECOIN_RH_FAST_PARALLEL_SAFETY. Default ON, today's live
    behaviour; only an explicit 0/off/false/no rolls it back to the sequential read in the hook."""
    raw = os.getenv("MEMECOIN_BSC_FAST_PARALLEL_SAFETY", "").strip().lower()
    return raw not in ("0", "off", "false", "no")


MEMECOIN_RH_FAST_PARALLEL_SAFETY = os.getenv(
    "MEMECOIN_RH_FAST_PARALLEL_SAFETY", "0").strip().lower() in ("1", "on", "true", "yes")

# 4663 CACHED-ONLY (spec 2026-10-14 rh-fast-cached-only §3.1): a safety cache miss is refused at once
# and the ordinary path judges the buy. 0 restores today's inline read on a miss.
MEMECOIN_RH_FAST_CACHED_ONLY = os.getenv(
    "MEMECOIN_RH_FAST_CACHED_ONLY", "1").strip().lower() in ("1", "on", "true", "yes")

# 4663 LANE SHARED ROUTES, speed fix 1 (operator yes 2026-10-14 via Alpha). Read per signal; 0 is
# today's path. EARLY_SNAPSHOT sizes the HUSH early worker from the datagram's wallet snapshot, so
# its quote and safety start beside the gates (it was still sizing on 3 of 4 lane shared fills).
# CACHED_SAFETY lets a v4/split coin's fresh passing lane verdict, at a clip from a quarter of its own up to it,
# answer the funded token safety; pons, v3 and curve verdicts never do.
MEMECOIN_RH_FAST_SHARED_EARLY_SNAPSHOT = os.getenv(
    "MEMECOIN_RH_FAST_SHARED_EARLY_SNAPSHOT", "0").strip().lower() in ("1", "on", "true", "yes")
MEMECOIN_RH_FAST_SHARED_CACHED_SAFETY = os.getenv(
    "MEMECOIN_RH_FAST_SHARED_CACHED_SAFETY", "0").strip().lower() in ("1", "on", "true", "yes")

# The 4663 inline host's idle warmer (spec 2026-10-14 rh-fast-cached-only rev 2 §3.6). It reads only
# while MEMECOIN_RH_FAST_CACHED_ONLY is on, when the fill path makes no inline read.
MEMECOIN_RH_FAST_INLINE_WARM = os.getenv(
    "MEMECOIN_RH_FAST_INLINE_WARM", "1").strip().lower() in ("1", "on", "true", "yes")
MEMECOIN_RH_FAST_INLINE_WARM_GAP_S = float(os.getenv("MEMECOIN_RH_FAST_INLINE_WARM_GAP_S", "0.25"))
# The warmer's reads go one at a time, this far apart (<= 10 a second against the shared Chainstack
# account's ~49; measured 2026-10-14). 0 keeps them serial with no gap.
MEMECOIN_RH_FAST_INLINE_WARM_CALL_GAP_S = float(os.getenv("MEMECOIN_RH_FAST_INLINE_WARM_CALL_GAP_S", "0.1"))


def safety_warm_window_s(chain):
    """The safety warm set's tier-1 window for `chain`: coins armed wallets touched within it are
    warmed after the last hour's (MEMECOIN_SAFETY_WARM_WINDOW_S_<chain>, default 24 h)."""
    return float(os.getenv("MEMECOIN_SAFETY_WARM_WINDOW_S_%s" % chain, "86400"))


# One DexScreener stats call per this many seconds across every lane's safety warmer in the runner
# process (lambda + Beta, Alpha GO 2026-10-14): a 429 is per IP and stops every lane stats call.
MEMECOIN_WARM_STATS_GAP_S = float(os.getenv("MEMECOIN_WARM_STATS_GAP_S", "1.0"))
MEMECOIN_RH_FAST_LANE_MAX_SENDS_PER_HOUR = int(os.getenv(
    "MEMECOIN_RH_FAST_LANE_MAX_SENDS_PER_HOUR", "6"))
MEMECOIN_RH_FAST_LANE_SNAPSHOT_MAX_AGE_S = float(os.getenv(
    "MEMECOIN_RH_FAST_LANE_SNAPSHOT_MAX_AGE_S", "30"))
MEMECOIN_RH_FAST_LANE_ETH_USD_MAX_AGE_S = float(os.getenv(
    "MEMECOIN_RH_FAST_LANE_ETH_USD_MAX_AGE_S", "120"))
# The research node follows the sequencer: a snapshot must start this long after the last stamp.
MEMECOIN_RH_FAST_LANE_GENERATION_MARGIN_S = float(os.getenv(
    "MEMECOIN_RH_FAST_LANE_GENERATION_MARGIN_S", "1"))
# EVM-08 intent fence (spec 2026-10-07-evm-generation-intent-fence-design.md, revision 2). Every EVM
# broadcast post writes one intent record first; the fast lanes refuse while a record exists on the
# chain. 0 is the rollback: no records, no bundle maxBlockNumber, readers and the sweep ignore them.
MEMECOIN_EXEC_INTENT_STAMP = os.getenv("MEMECOIN_EXEC_INTENT_STAMP", "1") == "1"
# W (operator 2026-10-07): the sweep looks at a record only this long after it was written; longer
# than the 10 s custody HTTP timeout, so a healthy post always completes its own record first.
MEMECOIN_EXEC_INTENT_RESOLVE_AFTER_S = float(os.getenv("MEMECOIN_EXEC_INTENT_RESOLVE_AFTER_S", "15"))
MEMECOIN_EXEC_INTENT_ALERT_AFTER_S = float(os.getenv("MEMECOIN_EXEC_INTENT_ALERT_AFTER_S", "600"))
MEMECOIN_EXEC_INTENT_SWEEP_PERIOD_S = float(os.getenv("MEMECOIN_EXEC_INTENT_SWEEP_PERIOD_S", "5"))
# K: the BSC lane's BlockRazor bundle is valid to the source block + K (BlockRazor's own default is
# the current block + 100). 56 heads tape 2026-10-07 09:12-09:19Z: 900 blocks in 406 s, 0.451 s a
# block, and 20 blocks spanned 9-10 s in all 881 windows; so K = 20 is about 9 s after the source.
MEMECOIN_BSC_BUNDLE_MAX_BLOCKS = int(os.getenv("MEMECOIN_BSC_BUNDLE_MAX_BLOCKS", "20"))
# The live lane holds the wallet's 4663 execution lock through its receipt for at most this long
# (Gamma's MEDIUM, Alpha 2026-10-01: 4663 receipts p50 211 ms, p99 2.8 s); past it, release, alert,
# and book outside the lock.
MEMECOIN_RH_FAST_LANE_LOCK_RECEIPT_CAP_S = float(os.getenv(
    "MEMECOIN_RH_FAST_LANE_LOCK_RECEIPT_CAP_S", "3"))
# The pre-signal safety cache (operator ruling 1; Alpha option 1): today's token_safety_fn on a
# runner thread, verdicts kept this long, at most one research request per gap (M1 plus the
# cache at or under 1 call a second; a 429 pauses it).
MEMECOIN_RH_FAST_SAFETY_TTL_S = float(os.getenv("MEMECOIN_RH_FAST_SAFETY_TTL_S", "60"))
# Optional discovery only. It never supplies a safety verdict or changes the research budget.
MEMECOIN_RH_PRESIGNAL_DISCOVERY_ENABLED = os.getenv(
    "MEMECOIN_RH_PRESIGNAL_DISCOVERY_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
# Observes existing successful entry receipts; adds no receipt/head polling.
MEMECOIN_RH_EXECUTION_OBSERVER_ENABLED = os.getenv(
    "MEMECOIN_RH_EXECUTION_OBSERVER_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
# Codex EVM-07: the 4663/56 lanes' cached chain totalSupply (their mcap = supply x price) expires
# after this many seconds and is unknown past it (the lane falls back); the safety loop re-reads it
# once it is past half. Decimals never expire.
MEMECOIN_FAST_LANE_SUPPLY_TTL_S = float(os.getenv("MEMECOIN_FAST_LANE_SUPPLY_TTL_S", "300"))
if not math.isfinite(MEMECOIN_FAST_LANE_SUPPLY_TTL_S) or MEMECOIN_FAST_LANE_SUPPLY_TTL_S <= 0:
    raise ValueError("MEMECOIN_FAST_LANE_SUPPLY_TTL_S must be finite and positive")
MEMECOIN_RH_FAST_SAFETY_MIN_GAP_S = float(os.getenv("MEMECOIN_RH_FAST_SAFETY_MIN_GAP_S", "2"))
# A signalled coin's verdict is refreshed this long before it goes stale (Gamma 2026-10-02: one
# verdict is ~8 calls, ~16 s at the 2 s gap, so a coin due only at the TTL can read stale for that
# long after a repeat buy).
MEMECOIN_RH_FAST_SAFETY_REFRESH_LEAD_S = float(os.getenv(
    "MEMECOIN_RH_FAST_SAFETY_REFRESH_LEAD_S", "20"))
# The detector names this many of the busiest routed mints to the cache, after every mint a
# signal or an armed wallet touched in the last hour. Sized to what the research budget keeps
# inside the TTL (Gamma): K <= TTL / (reads per check x gap) = 60 / (6 x 2), about 5.
MEMECOIN_RH_FAST_SAFETY_CANDIDATES_K = int(os.getenv("MEMECOIN_RH_FAST_SAFETY_CANDIDATES_K", "5"))


def memecoin_rh_fast_safety_rpc_url():
    """Call-time: the safety cache's host. MEMECOIN_RH_FAST_SAFETY_RPC_URL when the operator's
    dedicated 4663 key exists, else the 4663 research QuickNode; never a funded read host."""
    return (os.getenv("MEMECOIN_RH_FAST_SAFETY_RPC_URL", "").strip()
            or os.getenv("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", "").strip())


def memecoin_rh_fast_inline_safety_rpc_url():
    """Call-time: the 4663 lane's INLINE safety host, or "" (inline off: a cache miss falls back).
    Operator 2026-10-02 via Alpha: this key does inline checks, never the research cache worker,
    which keeps memecoin_rh_fast_safety_rpc_url() and its gap and K; since rev 2 §3.6 the idle
    inline warmer reads it too (MEMECOIN_RH_FAST_INLINE_WARM_CALL_GAP_S). Live key: Chainstack, the
    BSC 56 lane's account, about 49 reads a second (probe 2026-10-14 20:14Z); it was a free
    QuickNode key at first. One inline verdict is about 10 calls and the shared research QuickNode answers 429
    near 10 calls in 1.5 s, so a URL equal to the research key is no inline host. The lane also
    runs safety_host_refusal on it (any other role's URL, a funded pool member, a free endpoint)."""
    own = os.getenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL", "").strip()
    research = os.getenv("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", "").strip().rstrip("/")
    return own if own and own.rstrip("/").lower() != research.lower() else ""


# A 4663 cache miss checked inline (MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL only): past this
# whole-check deadline the signal falls back to today's path.
MEMECOIN_RH_FAST_INLINE_SAFETY_DEADLINE_S = float(os.getenv(
    "MEMECOIN_RH_FAST_INLINE_SAFETY_DEADLINE_S", "2"))

# The 4663 fast lane's LIVE route kinds (routes rev 3, 6.3): a comma list of v4_native, v4_weth and
# v4_bridge. Unknown names are dropped; empty means the live lane sends no kind (every signal falls
# back). runner_shadow judges every kind whatever this says. Rollback: v4_native, no release.
MEMECOIN_RH_FAST_ROUTES = tuple(k for k in (x.strip() for x in os.getenv(
    "MEMECOIN_RH_FAST_ROUTES", "v4_native").split(",")) if k in ("v4_native", "v4_weth", "v4_bridge", "v3_shared", "v4_shared", "pons_shared"))


def memecoin_evm_balance_beside_ladder_chains():
    """Call-time: chains whose EVM entry starts its pre-trade balance reads at executor entry,
    beside the quote ladder, and joins them where it reads them today (spec 2026-10-01 EVM entry
    speed, C3). Empty by default, and malformed means empty, which is today's order."""
    raw = os.getenv("MEMECOIN_EVM_BALANCE_BESIDE_LADDER_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_exit_anchor_chains():
    """Call-time: EVM chains whose exits hold a sell quote below the anchor (spec 2026-10-01
    anchor-checked exit). Empty by default, which is today's exit byte for byte; malformed means
    empty."""
    raw = os.getenv("MEMECOIN_EVM_EXIT_ANCHOR_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_voldeath_tape_chains():
    """Call-time: EVM chains whose funded volume-death rail reads our own swap tape
    (data/memecoin_onchain_volume.db) instead of the Solana-only mark stream, which left them
    blind_frozen for life (operator order 2026-10-01). Empty by default, and malformed means empty.
    4663 keeps its own WebSocket rail even when listed; Solana cannot be listed (digits only)."""
    raw = os.getenv("MEMECOIN_VOLDEATH_TAPE_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_voldeath_poll_chains():
    """Call-time: EVM chains whose funded volume-death rail is fed by the archive-endpoint poller
    (spec 2026-10-01-memecoin-bsc-swap-feed-design.md; 56 only today). Empty by default, malformed
    means empty, digits only. A chain listed here AND in MEMECOIN_VOLDEATH_TAPE_CHAINS uses the poll.
    4663 keeps its own WebSocket rail."""
    raw = os.getenv("MEMECOIN_VOLDEATH_POLL_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_type2_chains():
    """Call-time: chains whose funded EVM transactions sign type 2 (EIP-1559) instead of legacy
    (spec 2026-09-30-memecoin-evm-type2-signing-design.md). Empty by default, and malformed means
    empty. A listed chain signs type 2 only while `evm_type2_tip_wei_for(chain)` is positive."""
    raw = os.getenv("MEMECOIN_EVM_TYPE2_CHAINS", "1")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def evm_type2_tip_wei_for(chain):
    """maxPriorityFeePerGas in wei for a type-2 chain: MEMECOIN_EVM_TIP_GWEI_<chain>, the knob
    beta/eth-mainnet adds on top of the legacy price. Unset, malformed, not finite or not above 0:
    None (Flashbots Protect drops a 0 tip, so a listed chain without a tip signs legacy)."""
    try:
        gwei = float(os.getenv("MEMECOIN_EVM_TIP_GWEI_%s" % str(chain or "").strip(), ""))
    except ValueError:
        return None
    wei = int(round(gwei * 1e9)) if math.isfinite(gwei) else 0
    return wei if wei > 0 else None


def memecoin_pinned_close_reoffer_chains():
    """Call-time: network ids ("solana", "4663", ...) on which the close recovery re-offers a
    not_found sell's OWN stored bytes (memecoin_exit_rebroadcast). EMPTY BY DEFAULT: the re-offer
    was unreachable from 2026-09-02 until it was wired on 2026-09-30, so turning it on is an
    operator decision per chain. Malformed means empty, never a guess."""
    raw = os.getenv("MEMECOIN_PINNED_CLOSE_REOFFER_CHAINS", "")
    chains = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if not chains or any(chain != "solana" and not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_exit_venue_shadow_hosts(env=None):
    """{chain id: read URL} for the EVM exit-venue shadow (spec 2026-09-30 exit venue selection).
    A chain is shadowed only when MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_<chain> names an http(s)
    host: EMPTY BY DEFAULT, so nothing runs. Every shadow request goes to that URL as given, with
    no pool rotation; whoever sets it chooses whose rate budget it spends."""
    env = os.environ if env is None else env
    hosts = {}
    for name, value in env.items():
        if not name.startswith("MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_"):
            continue
        chain, url = name.rsplit("_", 1)[1], str(value or "").strip()
        if chain.isdigit() and url.startswith(("https://", "http://")):
            hosts[chain] = url
    return hosts


def memecoin_evm_v4_in_swap_unwrap_chains():
    """Call-time: chains whose hooked V4 single-hop buys unwrap WETH INSIDE the router call instead
    of a receipt-first WETH.withdraw pre-transaction (spec 2026-09-28 4663 V4 in-swap unwrap). Empty
    by default, and malformed means empty: today's transaction shape, byte for byte."""
    raw = os.getenv("MEMECOIN_EVM_V4_IN_SWAP_UNWRAP_CHAINS", "")
    chains = [part.strip() for part in raw.split(",") if part.strip()]
    if not chains or any(not chain.isdigit() for chain in chains):
        return frozenset()
    return frozenset(chains)


def memecoin_evm_approval_pipeline(chain_id=None):
    """Call-time flag: broadcast the entry approval WITHOUT waiting for its receipt and sign the
    swap immediately at approval-nonce+1 (nonce order guarantees the approval mines first). The
    approval wait was p50 2.8s of a 9.2s 4663 entry. A chain must also be explicitly allowlisted;
    missing/malformed config stays fail-closed and startup validation reports the error."""
    enabled = os.getenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "0").strip().lower() in (
        "1", "on", "true", "yes")
    if not enabled or chain_id is None:
        return False
    try:
        from sniper.settings.memecoin_submission import evm_approval_pipeline_chains
        return str(chain_id) in evm_approval_pipeline_chains()
    except Exception:
        return False


# EIP-1559 gas-price safety buffer for the EVM executor's legacy txs. A legacy gasPrice is treated as
# maxFeePerGas and REJECTED if it lands under a rising baseFee (observed on Robinhood/4663). Multiply
# the node's eth_gasPrice by this so the tx clears baseFee; memecoin-chain baseFees are tiny so the
# markup is negligible. Raise it if a chain's baseFee is very volatile.
MEMECOIN_EVM_GASPRICE_BUFFER = float(os.getenv("MEMECOIN_EVM_GASPRICE_BUFFER", "1.5"))


def validate_real_exit_policy():
    """Fail closed before an armed runner starts with no terminal or executable exit policy."""
    if not MEMECOIN_REAL_ENABLED:
        return None
    errors = []
    if MEMECOIN_SOLANA_VOLUME_GATE_ENABLED:
        if (not math.isfinite(MEMECOIN_SOLANA_VOLUME_MIN_M5_MCAP_RATIO)
                or not 0 < MEMECOIN_SOLANA_VOLUME_MIN_M5_MCAP_RATIO <= 1):
            errors.append('MEMECOIN_SOLANA_VOLUME_MIN_M5_MCAP_RATIO must be finite and in (0, 1]')
        if (not math.isfinite(MEMECOIN_SOLANA_VOLUME_MAX_AGE_S)
                or not 0 < MEMECOIN_SOLANA_VOLUME_MAX_AGE_S <= 5):
            errors.append('MEMECOIN_SOLANA_VOLUME_MAX_AGE_S must be finite and in (0, 5]')
    if (os.getenv('MEMECOIN_SHYFT_LIVE_ENABLED', '0') != '0'
            or os.getenv('MEMECOIN_SOLANA_INFRA_PROVIDER', 'legacy').strip().lower() == 'shyft'):
        try:
            from sniper.services.memecoin_shyft_signal import live_config
            from sniper.services.memecoin_rabbit_tape import tape_transport
            tape_transport(live_config()[1][0])
            import grpc
            if str(MEMECOIN_SOLANA_SLOT_LANE_ENABLED) != '1' or MEMECOIN_SOLANA_SLOT_LANE_MODE != 'live':
                errors.append('Shyft live feed requires the live Solana slot lane')
        except (ValueError, ImportError) as exc:
            errors.append('Shyft live feed invalid: ' + type(exc).__name__)
    if (MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663 is not None
            and (isinstance(MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663, bool)
                 or not isinstance(MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663, (int, float))
                 or not math.isfinite(MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663)
                 or MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663 < 0)):
        errors.append("MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663 must be finite and >= 0 when set")
    if (not math.isfinite(MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT)
            or not 0 <= MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT < 100):
        errors.append("MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT must be in [0,100)")
    invalid_band_policies = sorted(
        set(MEMECOIN_REAL_BAND_POLICY_BANDS) - {"nano", "micro", "small", "mid"})
    if invalid_band_policies:
        errors.append(
            "MEMECOIN_REAL_BAND_POLICY_BANDS contains unsupported bands: "
            + ",".join(invalid_band_policies))
    if (isinstance(MEMECOIN_MAX_CONCURRENT, bool)
            or not isinstance(MEMECOIN_MAX_CONCURRENT, int)
            or MEMECOIN_MAX_CONCURRENT < 1):
        errors.append("MEMECOIN_MAX_CONCURRENT must be an integer >= 1")
    if (isinstance(MEMECOIN_MAX_TOTAL_EXPOSURE_USD, bool)
            or not isinstance(MEMECOIN_MAX_TOTAL_EXPOSURE_USD, (int, float))
            or not math.isfinite(MEMECOIN_MAX_TOTAL_EXPOSURE_USD)
            or MEMECOIN_MAX_TOTAL_EXPOSURE_USD < 0):
        errors.append("MEMECOIN_MAX_TOTAL_EXPOSURE_USD must be finite and >= 0")
    if (isinstance(MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS, bool)
            or not isinstance(MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS, int)
            or not 0 <= MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS <= 10_000):
        errors.append(
            "MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS must be an integer "
            "from 0 through 10000")
    if not math.isfinite(MEMECOIN_REAL_MAX_HOLD_S) or MEMECOIN_REAL_MAX_HOLD_S <= 0:
        errors.append("MEMECOIN_REAL_MAX_HOLD_S must be finite and > 0")
    if not 1 <= MEMECOIN_SLOT_LANE_CU_LIMIT <= 1_400_000:
        errors.append("MEMECOIN_SLOT_LANE_CU_LIMIT must be 1..1400000 (the per-transaction cap)")
    for cls, limit in MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS.items():
        recorded_max = SLOT_LANE_CU_CLASSES[cls][1]
        if not recorded_max <= limit <= 1_400_000:
            errors.append("%s must be %d..1400000: below the class's recorded max the transaction "
                          "runs out of compute units and the trade fails"
                          % (slot_lane_cu_override_name(*cls), recorded_max))
    if not (math.isfinite(MEMECOIN_ENTRY_ROUNDTRIP_PROBE_USD) and MEMECOIN_ENTRY_ROUNDTRIP_PROBE_USD > 0
            and 0 < MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_TARGET_FRACTION <= 1
            and 0 < MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_MIN_FRACTION < 1):
        errors.append("MEMECOIN_ENTRY_ROUNDTRIP_PROBE_USD must be > 0, RESIZE_TARGET_FRACTION in "
                      "(0, 1], RESIZE_MIN_FRACTION in (0, 1)")
    if (not math.isfinite(MEMECOIN_SOL_EXEC_BUFFER_CAP_USD)
            or MEMECOIN_SOL_EXEC_BUFFER_CAP_USD < 0):
        errors.append("MEMECOIN_SOL_EXEC_BUFFER_CAP_USD must be finite and >= 0")
    if (not math.isfinite(MEMECOIN_SOL_EXEC_BUFFER_HEADROOM_USD)
            or MEMECOIN_SOL_EXEC_BUFFER_HEADROOM_USD < 0):
        errors.append("MEMECOIN_SOL_EXEC_BUFFER_HEADROOM_USD must be finite and >= 0")
    if MEMECOIN_SOL_EXEC_BUFFER_CAP_USD > 0 and (
            not all(math.isfinite(x) for x in (
                MEMECOIN_SOL_EXEC_BUFFER_TOPUP_USD, MEMECOIN_SOL_EXEC_BUFFER_DAILY_CAP_USD,
                MEMECOIN_SOL_EXEC_BUFFER_MIN_USDC_USD, MEMECOIN_SOL_EXEC_BUFFER_PERIOD_S))
            or not 0 < MEMECOIN_SOL_EXEC_BUFFER_TOPUP_USD <= MEMECOIN_SOL_EXEC_BUFFER_CAP_USD
            or MEMECOIN_SOL_EXEC_BUFFER_DAILY_CAP_USD < 0
            or (0 < MEMECOIN_SOL_EXEC_BUFFER_DAILY_CAP_USD < MEMECOIN_SOL_EXEC_BUFFER_TOPUP_USD)
            or MEMECOIN_SOL_EXEC_BUFFER_MIN_USDC_USD < MEMECOIN_SOL_EXEC_BUFFER_CAP_USD
            or MEMECOIN_SOL_EXEC_BUFFER_PERIOD_S < 30):
        errors.append("SOL buffer top-up needs a bounded amount, daily cap (0=uncapped), USDC majority, period")
    if not 1 <= MEMECOIN_SLOT_LANE_CU_PRICE_MICROLAMPORTS <= 50_000_000:
        errors.append("MEMECOIN_SLOT_LANE_CU_PRICE_MICROLAMPORTS must be 1..50000000")
    _entry_cu = MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS
    if _entry_cu is not None and (isinstance(_entry_cu, bool) or not isinstance(_entry_cu, int)
                                  or not 1 <= _entry_cu <= 50_000_000):
        errors.append("MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS must be unset or 1..50000000")
    ladder = list(MEMECOIN_EXIT_SLIPPAGE_LADDER or [])
    if (not ladder or any(not math.isfinite(x) or x <= 0 or x > 100 for x in ladder)
            or any(a >= b for a, b in zip(ladder, ladder[1:]))):
        errors.append("MEMECOIN_EXIT_SLIPPAGE_LADDER must be finite, positive, <=100, increasing")
    if MEMECOIN_REAL_STOP_CONFIRM_SWEEPS < 1:
        errors.append("MEMECOIN_REAL_STOP_CONFIRM_SWEEPS must be >= 1")
    if (isinstance(MEMECOIN_ENGINE_MAX_POSITIONS, bool)
            or not isinstance(MEMECOIN_ENGINE_MAX_POSITIONS, int)
            or MEMECOIN_ENGINE_MAX_POSITIONS < 0):
        errors.append("MEMECOIN_ENGINE_MAX_POSITIONS must be an integer >= 0")
    if (isinstance(MEMECOIN_ENGINE_STATE_MAX_AGE_S, bool)
            or not isinstance(MEMECOIN_ENGINE_STATE_MAX_AGE_S, (int, float))
            or not math.isfinite(MEMECOIN_ENGINE_STATE_MAX_AGE_S)
            or MEMECOIN_ENGINE_STATE_MAX_AGE_S <= 0):
        errors.append("MEMECOIN_ENGINE_STATE_MAX_AGE_S must be finite and > 0")
    if MEMECOIN_EXIT_ENGINE_VERSION != "v5":
        errors.append("MEMECOIN_EXIT_ENGINE_VERSION must be v5 (v2/v3 retired)")
    authoritative_chains = engine_authoritative_chains()
    if authoritative_chains:
        import sys
        from sniper.services import memecoin_v5_replay_gate as v5_gate
        active_revision = v5_gate.active_policy_revision()
        if active_revision is None:
            errors.append("engine-authoritative chains require an accepted active V5 policy")
        elif (active_revision[1].get("registered_parameters")
              != v5_gate.registered_policy_parameters(sys.modules[__name__])):
            errors.append("active V5 policy registered parameters do not match runtime")
    if (MEMECOIN_ROUTE_GONE_STREAK is not None
            and (isinstance(MEMECOIN_ROUTE_GONE_STREAK, bool)
                 or not isinstance(MEMECOIN_ROUTE_GONE_STREAK, int)
                 or MEMECOIN_ROUTE_GONE_STREAK < 1)):
        errors.append("MEMECOIN_ROUTE_GONE_STREAK must be an integer >= 1 when set")
    if (MEMECOIN_NO_PROGRESS_WINDOW_S is not None
            and (isinstance(MEMECOIN_NO_PROGRESS_WINDOW_S, bool)
                 or not isinstance(MEMECOIN_NO_PROGRESS_WINDOW_S, (int, float))
                 or not math.isfinite(MEMECOIN_NO_PROGRESS_WINDOW_S)
                 or MEMECOIN_NO_PROGRESS_WINDOW_S <= 0)):
        errors.append("MEMECOIN_NO_PROGRESS_WINDOW_S must be finite and > 0 when set")
    if (MEMECOIN_HOT_STRUCTURE_WINDOW_S is not None
            and (isinstance(MEMECOIN_HOT_STRUCTURE_WINDOW_S, bool)
                 or not isinstance(MEMECOIN_HOT_STRUCTURE_WINDOW_S, (int, float))
                 or not math.isfinite(MEMECOIN_HOT_STRUCTURE_WINDOW_S)
                 or MEMECOIN_HOT_STRUCTURE_WINDOW_S <= 0)):
        errors.append("MEMECOIN_HOT_STRUCTURE_WINDOW_S must be finite and > 0 when set")
    if (isinstance(MEMECOIN_DERISK_PENDING_STALE_S, bool)
            or not isinstance(MEMECOIN_DERISK_PENDING_STALE_S, (int, float))
            or not math.isfinite(MEMECOIN_DERISK_PENDING_STALE_S)
            or MEMECOIN_DERISK_PENDING_STALE_S <= 0):
        errors.append("MEMECOIN_DERISK_PENDING_STALE_S must be finite and > 0")
    if (isinstance(MEMECOIN_CLOSE_PENDING_STALE_S, bool)
            or not isinstance(MEMECOIN_CLOSE_PENDING_STALE_S, (int, float))
            or not math.isfinite(MEMECOIN_CLOSE_PENDING_STALE_S)
            or MEMECOIN_CLOSE_PENDING_STALE_S <= 0):
        errors.append("MEMECOIN_CLOSE_PENDING_STALE_S must be finite and > 0")
    for name, value in (
        ("MEMECOIN_HOT_EXIT_MAX_DECISION_GAP_S", MEMECOIN_HOT_EXIT_MAX_DECISION_GAP_S),
        ("MEMECOIN_HOT_EXIT_PREWARM_PERIOD_S", MEMECOIN_HOT_EXIT_PREWARM_PERIOD_S),
        ("MEMECOIN_HOT_EVM_POLL_S", MEMECOIN_HOT_EVM_POLL_S),
        ("MEMECOIN_HOT_PREWARM_DEADLINE_S", MEMECOIN_HOT_PREWARM_DEADLINE_S),
    ):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            errors.append(f"{name} must be finite and > 0")
    if (isinstance(MEMECOIN_HOT_PREWARM_WORKERS, bool)
            or not isinstance(MEMECOIN_HOT_PREWARM_WORKERS, int)
            or MEMECOIN_HOT_PREWARM_WORKERS < 1):
        errors.append("MEMECOIN_HOT_PREWARM_WORKERS must be an integer >= 1")
    for name, value, maximum in (
        ("MEMECOIN_HOT_DECODE_MAX_SIGS", MEMECOIN_HOT_DECODE_MAX_SIGS, 1000),
        ("MEMECOIN_HOT_DECODE_MAX_PAGES", MEMECOIN_HOT_DECODE_MAX_PAGES, 100),
    ):
        if (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum):
            errors.append(f"{name} must be an integer in [1,{maximum}]")
    if (MEMECOIN_EXIT_PRIORITY_FEE_MICROLAMPORTS < 0
            or MEMECOIN_EXIT_PRIORITY_FEE_CAP_MICROLAMPORTS < 0
            or MEMECOIN_EXIT_PRIORITY_FEE_MICROLAMPORTS
            > MEMECOIN_EXIT_PRIORITY_FEE_CAP_MICROLAMPORTS):
        errors.append("exit priority fee must be nonnegative and no greater than its cap")
    if (not math.isfinite(MEMECOIN_EXIT_DEPTH_MAX_RUNG_PCT)
            or not 0 < MEMECOIN_EXIT_DEPTH_MAX_RUNG_PCT <= 100):
        errors.append("MEMECOIN_EXIT_DEPTH_MAX_RUNG_PCT must be in (0,100]")
    for name, value, minimum in (
        ("MEMECOIN_FUNDED_CLASSIFICATION_VERSION", MEMECOIN_FUNDED_CLASSIFICATION_VERSION, 1),
        ("MEMECOIN_FUNDED_TAPE_MIN_OBSERVATIONS", MEMECOIN_FUNDED_TAPE_MIN_OBSERVATIONS, 3),
        ("MEMECOIN_FUNDED_REGIME_BASELINE_BUCKETS", MEMECOIN_FUNDED_REGIME_BASELINE_BUCKETS, 1),
        ("MEMECOIN_FUNDED_REGIME_MIN_NONEMPTY_BUCKETS",
         MEMECOIN_FUNDED_REGIME_MIN_NONEMPTY_BUCKETS, 1),
        ("MEMECOIN_FUNDED_REGIME_MIN_BASELINE_BUYS", MEMECOIN_FUNDED_REGIME_MIN_BASELINE_BUYS, 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            errors.append(f"{name} must be an integer >= {minimum}")
    if MEMECOIN_FUNDED_REGIME_MIN_NONEMPTY_BUCKETS > MEMECOIN_FUNDED_REGIME_BASELINE_BUCKETS:
        errors.append("funded regime nonempty buckets cannot exceed baseline buckets")
    for name, value in (
        ("MEMECOIN_FUNDED_TAPE_WINDOW_S", MEMECOIN_FUNDED_TAPE_WINDOW_S),
        ("MEMECOIN_FUNDED_TAPE_MIN_SPAN_S", MEMECOIN_FUNDED_TAPE_MIN_SPAN_S),
        ("MEMECOIN_FUNDED_REGIME_BUCKET_S", MEMECOIN_FUNDED_REGIME_BUCKET_S),
        ("MEMECOIN_FUNDED_REGIME_MAX_STALENESS_S", MEMECOIN_FUNDED_REGIME_MAX_STALENESS_S),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value <= 0:
            errors.append(f"{name} must be finite and > 0")
    if MEMECOIN_FUNDED_TAPE_MIN_SPAN_S > MEMECOIN_FUNDED_TAPE_WINDOW_S:
        errors.append("funded tape minimum span cannot exceed its window")
    scale = MEMECOIN_FUNDED_REGIME_RISK_SCALE
    if (isinstance(scale, bool) or not isinstance(scale, (int, float))
            or not math.isfinite(scale) or not 0 < scale <= 1):
        errors.append("MEMECOIN_FUNDED_REGIME_RISK_SCALE must be in (0,1]")
    for name, value, upper in (
        ("MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT", MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT, None),
        ("MEMECOIN_FUNDED_POST_PEAK_GIVEBACK_PCT",
         MEMECOIN_FUNDED_POST_PEAK_GIVEBACK_PCT, 100.0),
    ):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
                or (upper is not None and value > upper)):
            errors.append(f"{name} is outside its valid range")
    for name, value, upper in (
        ("MEMECOIN_REAL_NANO_DERISK_PCT", MEMECOIN_REAL_NANO_DERISK_PCT, None),
        ("MEMECOIN_REAL_NANO_TRAIL_PCT", MEMECOIN_REAL_NANO_TRAIL_PCT, 100.0),
        ("MEMECOIN_REAL_NANO_SL_PCT", MEMECOIN_REAL_NANO_SL_PCT, 100.0),
        ("MEMECOIN_REAL_MICRO_DERISK_PCT", MEMECOIN_REAL_MICRO_DERISK_PCT, None),
        ("MEMECOIN_REAL_MICRO_TRAIL_PCT", MEMECOIN_REAL_MICRO_TRAIL_PCT, 100.0),
        ("MEMECOIN_REAL_MICRO_SL_PCT", MEMECOIN_REAL_MICRO_SL_PCT, 100.0),
        ("MEMECOIN_REAL_MID_DERISK_PCT", MEMECOIN_REAL_MID_DERISK_PCT, None),
        ("MEMECOIN_REAL_MID_TRAIL_PCT", MEMECOIN_REAL_MID_TRAIL_PCT, 100.0),
        ("MEMECOIN_REAL_MID_SL_PCT", MEMECOIN_REAL_MID_SL_PCT, 100.0),
    ):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
                or (upper is not None and value > upper)):
            errors.append(f"{name} is outside its valid range")
    for name, value in (
        ("MEMECOIN_REAL_MIRROR_HOLDER_WINDOW_S", MEMECOIN_REAL_MIRROR_HOLDER_WINDOW_S),
        ("MEMECOIN_REAL_MIRROR_MIN_REMAINING_USD", MEMECOIN_REAL_MIRROR_MIN_REMAINING_USD),
        ("MEMECOIN_HOLDER_EVIDENCE_MAX_AGE_S", MEMECOIN_HOLDER_EVIDENCE_MAX_AGE_S),
    ):
        if not math.isfinite(value) or value <= 0:
            errors.append(f"{name} must be finite and > 0")
    for name, value, upper in (
        ("MEMECOIN_REAL_STOP_PCT", MEMECOIN_REAL_STOP_PCT, 100.0),
        ("MEMECOIN_REAL_TP_PCT", MEMECOIN_REAL_TP_PCT, None),
        ("MEMECOIN_REAL_DERISK_PCT", MEMECOIN_REAL_DERISK_PCT, None),
        ("MEMECOIN_REAL_LEGACY_TP_PCT", MEMECOIN_REAL_LEGACY_TP_PCT, None),
        ("MEMECOIN_REAL_CATASTROPHE_PCT", MEMECOIN_REAL_CATASTROPHE_PCT, 100.0),
        # Trail knobs (2026-07-18 rollover engine). TRAIL_PCT is a give-back magnitude in (0,100];
        # 0 disables the trail. TRAIL_ARM_PCT is a peak threshold >= 0. Both must be finite — an armed
        # runner that accepted NaN would compute a NaN trail floor and never (or always) fire.
        ("MEMECOIN_REAL_TRAIL_PCT", MEMECOIN_REAL_TRAIL_PCT, 100.0),
        ("MEMECOIN_REAL_TRAIL_ARM_PCT", MEMECOIN_REAL_TRAIL_ARM_PCT, None),
    ):
        if not math.isfinite(value) or value < 0 or (upper is not None and value > upper):
            errors.append(f"{name} is outside its valid range")
    # An armed funded trail requires an explicit aware rollout anchor. Empty/naive timestamps can
    # silently move legacy bags onto the new policy or disable the new policy, so fail closed.
    from sniper.services.memecoin_exits import _parse as _parse_ts
    cutoff = MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT
    parsed_cutoff = _parse_ts(cutoff) if cutoff else None
    cutoff_aware = (parsed_cutoff is not None and parsed_cutoff.tzinfo is not None
                    and parsed_cutoff.utcoffset() is not None)
    if cutoff and not cutoff_aware:
        errors.append("MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT cutoff must be an aware ISO timestamp")
    if MEMECOIN_REAL_ENABLED and MEMECOIN_REAL_TRAIL_PCT > 0 and not cutoff_aware:
        errors.append("funded rollover trail requires an aware cutoff")
    contract_cutoff = MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT
    parsed_contract_cutoff = _parse_ts(contract_cutoff) if contract_cutoff else None
    contract_cutoff_aware = (
        parsed_contract_cutoff is not None and parsed_contract_cutoff.tzinfo is not None
        and parsed_contract_cutoff.utcoffset() is not None
    )
    if contract_cutoff and not contract_cutoff_aware:
        errors.append(
            "MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT cutoff must be an aware ISO timestamp")
    if errors:
        raise RuntimeError("invalid armed memecoin exit policy: " + "; ".join(errors))
    return None

# --- Refused-mint forward capture (2026-08-26) ---------------------------------------------------
# Research only, DEFAULT OFF. Design:
# docs/superpowers/specs/2026-08-26-memecoin-refused-mint-forward-capture-design.md
MEMECOIN_CAPTURE_ENABLED = os.getenv(
    "MEMECOIN_CAPTURE_ENABLED", "0").strip().lower() in ("1", "on", "true", "yes")
# Peak concurrency is the binding constraint, not the daily total: refusals arrive in bursts (7-day
# mean 18/day against 54 on 2026-08-25). On overflow a capture is REFUSED and recorded, never
# queued or thinned -- see `start()`.
MEMECOIN_CAPTURE_MAX_CONCURRENT = int(os.getenv("MEMECOIN_CAPTURE_MAX_CONCURRENT", "6"))
# 5s matches the funded stop confirm (5 sweeps x 5s), so a simulated stop is reproducible wherever
# it fires. Raising this makes stop simulation coarser than the rail it models.
# 25s, not 5s: sized to a Jupiter IP budget rather than to what the series would prefer.
# 6 concurrent / 25s = 864 quotes/hour = 24% of a ~3600/h published lite-tier limit (published,
# NOT observed -- rate_limited is 0 lifetime, so the real wall has never been seen). At the
# original 5s this lane alone drew 4,320/h, which exceeds that whole limit by itself and would
# have 429'd funded exits off the shared IP. Cost is resolution: 32 observations over an 810s
# horizon instead of 162.
MEMECOIN_CAPTURE_CADENCE_S = float(os.getenv("MEMECOIN_CAPTURE_CADENCE_S", "25"))
# The horizon is BOUND to the timecap rail, never hardcoded: MEMECOIN_REAL_MAX_HOLD_S has moved
# 600 -> 2592000 -> 600 inside one day, and a hardcoded horizon would desync from the rail it
# exists to mirror, invisibly. The margin lets a simulation price the exit at realistic latency.
MEMECOIN_CAPTURE_SETTLE_MARGIN_S = float(os.getenv("MEMECOIN_CAPTURE_SETTLE_MARGIN_S", "30"))
# The hypothetical clip. Priced, never taken.
MEMECOIN_CAPTURE_CLIP_USD = float(os.getenv("MEMECOIN_CAPTURE_CLIP_USD", "20"))
# Jupiter takes no API key and limits BY IP, and the funded runner shares this IP. Measured
# 2026-08-26: one observation is 1 Jupiter GET and 0 RPC, so Jupiter is the binding dependency and
# the disjoint capture RPC key protects 1/162 of the load. A 429 shuts the capture for this long.
MEMECOIN_CAPTURE_JUPITER_YIELD_S = float(os.getenv("MEMECOIN_CAPTURE_JUPITER_YIELD_S", "120"))
MEMECOIN_CAPTURE_POLL_S = float(os.getenv("MEMECOIN_CAPTURE_POLL_S", "2"))
# DIP SHADOW (2026-09-30, Alpha go (B) after Gamma's dip-fill read 8c3bbaba2). After a Solana
# chase_quote or slippage_fail refusal, a separate `#dip` series takes an exact BUY quote for the
# reserved clip every DIP_CADENCE_S for DIP_WINDOW_S, on the capture's measurement key. 0 = off.
# Budget: at most DIP_MAX_CONCURRENT / DIP_CADENCE_S quotes a second (1/s at 2 and 2), and about
# 8 refusals a day x 15 quotes. It shares the capture's Jupiter yield, so a 429 pauses both lanes.
MEMECOIN_CAPTURE_DIP_WINDOW_S = float(os.getenv("MEMECOIN_CAPTURE_DIP_WINDOW_S", "30"))
MEMECOIN_CAPTURE_DIP_CADENCE_S = float(os.getenv("MEMECOIN_CAPTURE_DIP_CADENCE_S", "2"))
MEMECOIN_CAPTURE_DIP_MAX_CONCURRENT = int(os.getenv("MEMECOIN_CAPTURE_DIP_MAX_CONCURRENT", "2"))
# EVM CHAINS (2026-09-13). Robinhood and BSC refusals had no follow-up price path. The EVM lane
# reads DexScreener display marks (no wallet, no custody RPC) in ONE batched request per pass, and
# labels every row quote_price_basis='display'. Its capacity is separate, so Solana keeps all six.
# An empty list turns the EVM lane off. Arc (5042) joined 2026-09-17: DexScreener lists it as chainId
# "arc", and the chain adds addresses to the same batched read, not a new request.
MEMECOIN_CAPTURE_EVM_NETWORKS = tuple(
    n.strip() for n in os.getenv("MEMECOIN_CAPTURE_EVM_NETWORKS", "4663,56,5042").split(",") if n.strip())
MEMECOIN_CAPTURE_EVM_MAX_CONCURRENT = int(os.getenv("MEMECOIN_CAPTURE_EVM_MAX_CONCURRENT", "6"))
MEMECOIN_CAPTURE_EVM_CADENCE_S = float(os.getenv("MEMECOIN_CAPTURE_EVM_CADENCE_S", "25"))
# DexScreener limits by IP and the funded runner shares this IP. A 429 shuts the EVM lane this long.
MEMECOIN_CAPTURE_DISPLAY_YIELD_S = float(os.getenv("MEMECOIN_CAPTURE_DISPLAY_YIELD_S", "120"))
# EXACT-ROUTE EVM LANE (2026-09-28, spec 2026-09-28-memecoin-evm-exact-refusal-capture-design.md).
# Our own V4/V3/V2 quoters, read on MEMECOIN_CAPTURE_EVM_READ_URL_<chain> only: a host no other role
# uses, checked at startup. Capacity is its own; the rate and the hold are per read host. 4 calls/s
# is the pace probed live on all three research hosts with no stall on any tape.
MEMECOIN_CAPTURE_EVM_EXACT_MAX_CONCURRENT = int(os.getenv("MEMECOIN_CAPTURE_EVM_EXACT_MAX_CONCURRENT", "6"))
MEMECOIN_CAPTURE_EVM_EXACT_MAX_RPS = float(os.getenv("MEMECOIN_CAPTURE_EVM_EXACT_MAX_RPS", "4"))
MEMECOIN_CAPTURE_EVM_EXACT_YIELD_S = float(os.getenv("MEMECOIN_CAPTURE_EVM_EXACT_YIELD_S", "120"))
# Chains on which the exact lane, when no single-hop venue prices a coin, runs the V4 multihop
# search on our PoolKey store alone (no provider pool list), on its own read host. Empty = off.
MEMECOIN_CAPTURE_EVM_MULTIHOP_CHAINS = tuple(
    n.strip() for n in os.getenv("MEMECOIN_CAPTURE_EVM_MULTIHOP_CHAINS", "4663").split(",")
    if n.strip())
# SLOT-END CHASE SHADOW (2026-09-29, operator via Alpha): the refusal-capture process records, for
# each Solana chase refusal, the price at the END of the source's slot and whether that reference
# would have admitted it (memecoin_slot_end_shadow). Measurement only, research key only, no read
# while a real Solana position is open. Default off.
MEMECOIN_CAPTURE_SLOT_END_ENABLED = os.getenv(
    "MEMECOIN_CAPTURE_SLOT_END_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def memecoin_capture_evm_exact_chains(env=None):
    """EVM chains the exact-route capture lane quotes, as network ids. Read at call time; default
    empty = the lane is off and the worker behaves as before."""
    env = os.environ if env is None else env
    raw = str(env.get("MEMECOIN_CAPTURE_EVM_EXACT_NETWORKS", "") or "")
    from sniper.services.memecoin_chains import canonical_chain_id
    out = set()
    for part in (p.strip() for p in raw.split(",")):
        chain = canonical_chain_id(part) if part else None
        if chain is None or chain == "solana":
            if part:
                logging.getLogger("memecoin").warning(
                    "MEMECOIN_CAPTURE_EVM_EXACT_NETWORKS names %r, which is no EVM chain: ignored", part)
            continue
        out.add(chain)
    return frozenset(out)

# RESIDUAL TAIL STUDY (2026-08-27). Per-close timers that price what a "recover principal, then
# trail the rest" exit would have kept, at 2/5/10/20/30/60s after a funded take-profit.
# DEFAULT OFF: it spawns threads in the funded runner and spends the funded Jupiter key.
MEMECOIN_RESIDUAL_TAIL_ENABLED = os.getenv(
    "MEMECOIN_RESIDUAL_TAIL_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
# Added to the principal before solving for the sold fraction, so "recovered" means recovered net
# of the round trip rather than exactly break-even on the quote.
MEMECOIN_RESIDUAL_TAIL_FEE_BUFFER_PCT = float(
    os.getenv("MEMECOIN_RESIDUAL_TAIL_FEE_BUFFER_PCT", "1.0"))

# WINNER RE-ENTRY (2026-08-27, operator order). The re-entry cooldown was blanket: any coin we
# closed inside the window was refused. The 07-09 study that motivated it never split the
# population by outcome, so it says nothing about re-entering a coin we just made money on. These
# unblock winners and replace the timer with two bounded protections. Policy on operator judgment,
# with the ledger as the grader.
MEMECOIN_REENTRY_WINNER_UNBLOCKED = os.getenv(
    "MEMECOIN_REENTRY_WINNER_UNBLOCKED", "1").strip().lower() in {"1", "true", "yes"}
# Refuse a winner re-entry priced above our own exit by more than this. Protects the case the
# cooldown was really guarding: buying back into a coin that ran away after we sold it.
MEMECOIN_REENTRY_WINNER_MAX_ABOVE_EXIT_PCT = float(
    os.getenv("MEMECOIN_REENTRY_WINNER_MAX_ABOVE_EXIT_PCT", "10"))
# Real campaigns per coin per UTC day. Bounds how far a single coin can be re-entered when the
# price guard keeps admitting it.
MEMECOIN_REENTRY_WINNER_DAILY_CAP = int(
    os.getenv("MEMECOIN_REENTRY_WINNER_DAILY_CAP", "2"))
# WINNER RE-ENTRY SCOPE AND SIZE (2026-09-28, operator order via Alpha; spec
# 2026-09-28-memecoin-winner-reentry-size-ladder-design.md). Winners are unblocked only on the
# canonical chains listed here; EMPTY = no chain, so every chain keeps the 24 h cooldown whatever
# MEMECOIN_REENTRY_WINNER_UNBLOCKED says. Evidence split by chain (07-13..09-16 automatic winner
# re-entries): solana n=48 +$436, 4663 n=26 -$97, 56 n=2 -$74.
MEMECOIN_REENTRY_WINNER_CHAINS = os.getenv("MEMECOIN_REENTRY_WINNER_CHAINS", "")
# The k-th re-entry inside the cooldown window takes factor k of the clip, through the funded
# risk_scale; a rank past the last step is refused. "" = not configured (a winner re-entry keeps
# the full clip, the 08-27 behaviour). A malformed value has NO steps, so every winner re-entry is
# refused: it can never fall back to full size.
MEMECOIN_REENTRY_WINNER_SIZE_LADDER = os.getenv("MEMECOIN_REENTRY_WINNER_SIZE_LADDER", "")


def winner_reentry_chain_on(network_id):
    """Whether winner re-entry is enabled on this coin's chain (canonical ids, aliases accepted)."""
    from sniper.services.memecoin_chains import canonical_chain_id
    chain = canonical_chain_id(network_id)
    return chain is not None and chain in {
        canonical_chain_id(item) for item in str(MEMECOIN_REENTRY_WINNER_CHAINS or "").split(",")
        if item.strip()}


def winner_reentry_ladder():
    """None = not configured; () = malformed (no steps); else the factors, each in (0, 1]."""
    return _parse_reentry_ladder(str(MEMECOIN_REENTRY_WINNER_SIZE_LADDER or ""))


@functools.lru_cache(maxsize=8)
def _parse_reentry_ladder(raw):
    if not raw.strip():
        return None
    try:
        steps = tuple(float(item) for item in raw.split(","))
    except ValueError:
        steps = ()
    if not steps or any(not (math.isfinite(s) and 0.0 < s <= 1.0) for s in steps):
        logging.getLogger("memecoin").warning(
            "MEMECOIN_REENTRY_WINNER_SIZE_LADDER=%r is malformed: winner re-entries are refused", raw)
        return ()
    return steps

# PAPER EXIT QUOTE PLAUSIBILITY (2026-09-17). A paper close refuses a whole-bag route quote above this
# multiple of the larger of the live mark that fired the rail and the position's peak, and books the
# existing censored `_noquote` close instead. STANDARD (4663, 2026-09-15) booked quotes at 7.08x and
# 10.96x the mid: $6,925.60 of paper profit on six $100 positions. Legitimate bracket fills against a
# LIVE mark stayed under 3x; stale-mark closes are not bounded. 0 disables.
MEMECOIN_PAPER_EXIT_MAX_QUOTE_TO_MARK = float(os.getenv("MEMECOIN_PAPER_EXIT_MAX_QUOTE_TO_MARK", "5.0"))

# The separate observer repairs only the m5 floor. Empty means disabled.
# SOLANA HOURLY FLOOR ACTIVITY (S2, operator spec 2026-09-17-solana-hourly-floor-activity-design).
# A DEDICATED research endpoint for one page of mint-address signatures. Empty, public, shared with
# any other RPC variable, or invalid = the probe is off (the runner still starts). The cutoff is the
# reviewed integer 1..1000; unset = evidence only, no exception is ever granted.
MEMECOIN_SOL_H1_ACTIVITY_RPC_URL = os.getenv("MEMECOIN_SOL_H1_ACTIVITY_RPC_URL", "")
MEMECOIN_SOL_H1_ACTIVE_SIGS = os.getenv("MEMECOIN_SOL_H1_ACTIVE_SIGS", "")
# POOL-STORE DEPTH FALLBACK (2026-09-22). How old a cached `memecoin_pool_depth` row may be
# and still fill a MISSING provider liquidity on the funded entry path. 0 disables the
# fallback entirely. It never overrides a provider number, and the cached figure is a floor
# on depth rather than the provider's TVL (see sniper/services/memecoin_pool_depth).
MEMECOIN_POOL_DEPTH_MAX_AGE_S = float(os.getenv("MEMECOIN_POOL_DEPTH_MAX_AGE_S", "3600"))

# Entry impact gate (2026-09-23, operator: "build the impact gate"). Refuse an entry when OUR OWN
# clip would move the bound pool more than this percentage, computed from the cached in-range
# liquidity. DEFAULT 0, which disables it: the gate ships off and the operator turns it on.
#
# Why it exists: the five quiet-hours refusals of 2026-09-23 split on depth, not on the hour. A
# $37.50 clip moved ARENA 0.32% and CSTR 13.32%, and the reconstruction reproduced both figures
# exactly. A clock blocks both; this blocks the second and lets the first through.
#
# The modelled number is a SINGLE-TICK MODEL, neither a floor nor a ceiling (Codex review
# 2026-09-23, F2): it assumes one tick of constant liquidity. A clip that crosses into a thinner
# tick does worse; one that crosses into a deeper tick does better, and by a lot (10.00% modelled
# against 0.30% exact in tests/test_memecoin_pool_depth.py). An impact that cannot be computed
# NEVER blocks.
MEMECOIN_ENTRY_MAX_IMPACT_PCT = float(os.getenv("MEMECOIN_ENTRY_MAX_IMPACT_PCT", "0"))

# J7 TAPE BOUND (spec 2026-10-15-memecoin-m5-provider-lag-design.md option d, operator yes via
# Alpha): a signal with no source tx (J7 `swap:<uuid>`) gets max(0, tape m5 total - the source's
# paid USD) as its m5 floor lower bound instead of no bound. 10-01..10-15: 3 of 7 provider-decided
# m5 floor refusals were wrong by on-chain truth. Read per decision; 0 is today's path.
MEMECOIN_M5_SOURCE_UNKNOWN_TAPE_BOUND = os.getenv(
    "MEMECOIN_M5_SOURCE_UNKNOWN_TAPE_BOUND", "0").strip().lower() in {"1", "true", "yes"}

MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS = {c.strip() for c in os.getenv(
    "MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS", "").split(",") if c.strip()}

# WINDOW BACKFILL (2026-09-22). On every connect or reset the on-chain m5 observer fills the last
# five minutes of PoolManager logs and their headers from MEMECOIN_ONCHAIN_EVM_RPC_URL_<chain>
# before it reads the socket, so a deploy or a stream reset does not leave the floor blind for
# 300 s (JOLLYBOT d058ca73 was refused on the provider's $0.49 inside such a window). 0 turns the
# backfill off; the window then opens at the socket's first header, as before.
MEMECOIN_ONCHAIN_M5_BACKFILL_ENABLED = os.getenv(
    "MEMECOIN_ONCHAIN_M5_BACKFILL_ENABLED", "1").strip().lower() in {"1", "true", "yes"}

# A hop-priced quote unit (an equity token priced through one V4 hop) stays usable by the m5 gate
# for this long; the base prices (native, quote, stables) keep their 60 s. The hop pass itself
# runs every 30 s and takes 20-50 s, so a 60 s limit let priced units flap to `quote_not_priceable`.
MEMECOIN_ONCHAIN_M5_HOP_PRICE_MAX_AGE_S = float(os.getenv("MEMECOIN_ONCHAIN_M5_HOP_PRICE_MAX_AGE_S", "180"))


def _onchain_m5_usd_stables():
    """USD stablecoins the on-chain m5 observer values at $1, per chain: `..._USD_STABLES_<chain>`.

    USDG quotes 3,428 of 6,241 known 4663 pools (2026-09-16). Its DECIMALS ARE READ FROM THE CHAIN
    and cached by the observer, never assumed here: BSC stables are 18-decimal, not 6.
    ponytail: a depeg below $1 would overstate the bound; add a stable price feed if one depegs.
    """
    prefix = "MEMECOIN_ONCHAIN_M5_USD_STABLES_"
    out = {
        "4663": ("0x5fc5360d0400a0fd4f2af552add042d716f1d168",),   # USDG
        # Base USDC, verified on chain 2026-09-22 against base-rpc.publicnode.com:
        # symbol() = "USDC", decimals() = 6.
        "8453": ("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",),
    }
    for name, value in os.environ.items():
        if name.startswith(prefix) and name[len(prefix):].strip():
            out[name[len(prefix):].strip()] = tuple(
                p.strip().lower() for p in str(value).split(",") if p.strip())
    return out


MEMECOIN_ONCHAIN_M5_USD_STABLES = _onchain_m5_usd_stables()
# Page once when the observer has received no block header for this long, and once on recovery.
# The gate then falls back to the provider, so this is lost coverage, not lost money.
MEMECOIN_ONCHAIN_M5_FEED_STALE_ALERT_S = float(
    os.getenv("MEMECOIN_ONCHAIN_M5_FEED_STALE_ALERT_S", "120"))


# --- refusal shadow lane (2026-09-23) -----------------------------------------------------------
# Measurement only: it prices refused coins on our own EVM swap tape and writes its OWN database.
# Default ON because it cannot enter, size, re-ask or grade, and it adds no code to the funded
# entry path -- it reads the durable refusal record the runner already writes.
# LANE A, THE PROMOTION SHADOW (2026-09-23). Replays the reproducible funded gates for signals
# relayed on a wallet that is not armed, and models a position only when they all pass, so a
# disarmed wallet's record answers "would we have taken this" instead of "was the coin good".
# Default OFF; the operator arms it. The clip is FIXED rather than the funded sizer, because a
# promotion read wants one constant across weeks, not a bankroll-dependent size.
MEMECOIN_PROMOTION_LANE_ENABLED = os.getenv(
    "MEMECOIN_PROMOTION_LANE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_PROMOTION_LANE_CLIP_USD = float(
    os.getenv("MEMECOIN_PROMOTION_LANE_CLIP_USD", "100"))
# The lane reads the market NOW to score a decision made THEN, so a signal older than this cannot
# be gated honestly and is skipped rather than scored against a market that has moved.
MEMECOIN_PROMOTION_LANE_MAX_AGE_S = float(
    os.getenv("MEMECOIN_PROMOTION_LANE_MAX_AGE_S", "900"))
# Quotes the lane's own sampler may take in one pass, on the PAPER Jupiter key. The lane admits
# coins we never took, and `memecoin_quote_ledger` covers almost only coins we did take (1 of 113
# never-held mints on 2026-09-23), so without its own samples the lane can price only the subset it
# already chose. A pass that hits this cap must report `capped` beside any coverage share: the cap
# makes "sampled" a fact about our queue.
MEMECOIN_PROMOTION_LANE_SAMPLE_MAX = int(
    os.getenv("MEMECOIN_PROMOTION_LANE_SAMPLE_MAX", "20"))

# AN ABSENT SOURCE TRANSACTION IS NOT AUTOMATICALLY A BLIND TAPE (2026-09-23). The flag only says
# the source's swap did not land on a pool we know for this mint, which happens for a venue we do
# not ingest (MRLN, $3,721 on Pons v2), for an unmapped pool, and for a multi-hop route whose own
# leg produced no tick (MSTR, whose transaction was in the tape 0.88 s BEFORE the gate ran). Calling
# all of them blind stopped the m5 floor binding: 4 of 4 EVM attempts after the 15:29Z deploy read
# absent. The provider's organic m5 separates a venue gap from a quiet coin, and the gate already
# holds it. BOTH must be true to go blind: provider > ours * ratio AND provider - ours > min_usd.
# Set either to 0 to return to "every absent source transaction is blind".
# HOW LONG AN AMBIGUOUS V3 RESOLVE STAYS CACHED (2026-09-23 review, F5). A pool whose token0()
# answered `0x` may be a contract that has no such function, or a node that could not serve the
# state; the row expires after this and the resolver asks again. 0 = never expire, the behaviour
# before the review. A node ERROR that is not a revert writes no row at all and needs no knob.
MEMECOIN_ONCHAIN_V3_SOFT_RETRY_S = float(
    os.getenv("MEMECOIN_ONCHAIN_V3_SOFT_RETRY_S", "21600"))

# A SUBSCRIPTION CAN FALL BEHIND WHILE ITS SOCKET STAYS HEALTHY (2026-09-23). publicnode let the
# address-filtered V4 subscription drift from 0.7 s to 652.9 s of delivery lag over fourteen minutes
# while the topic-only V3 subscription on the SAME socket stayed at 0.6-1.4 s, then dropped 2,754
# blocks outright. `newHeads` stayed current throughout, so `newest_block_age_s` read fresh and the
# lane summed an empty V4 window as a measurement. The ceiling is on the MEDIAN, not the max:
# per-minute MAX lag is 9.8 s at p50 even when healthy and touched 18.7 s on a clean V3 minute, so a
# 15 s max-rule fires on healthy traffic, while the median separates by two orders of magnitude.
# 0 disables the leg.
MEMECOIN_ONCHAIN_M5_PROTOCOL_LAG_S = float(
    os.getenv("MEMECOIN_ONCHAIN_M5_PROTOCOL_LAG_S", "30"))
# A REQUIRED TAPE SUBSCRIPTION (V4 id 3, V3 id 4) THAT GOES SILENT while the same socket keeps
# delivering other logs is re-opened on that socket after this many seconds (2026-09-29: the 8453 V4
# subscription delivered nothing for an hour after a reconnect while V3 carried 1,100-3,400 swaps a
# minute). Healthy silences, excluding gaps that span a reset, max out at 2.1-4.0 s in all four
# families (4663/8453 x V3/V4; Gamma, 2026-09-29). The 62.2 s first cited here was the 02:05:03Z
# process-init gap, not a healthy silence. 0 disables.
MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S = float(
    os.getenv("MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S", "120"))
# PER-CHAIN THRESHOLD (2026-10-17, 4663, operator order via Alpha). publicnode's free 4663 socket drops
# single swap subscriptions while heads and the other topic flow: 1,339 per-subscription re-opens at
# 120 s in the writer log 10-13..10-17, each a 120 s tape hole behind rh fast-lane tape_behind /
# bridge_tape_behind refusals. Healthy silences top out at 2.1-4.0 s, so 4663 uses 30 s.
# MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S_<chain> overrides one chain (kill: _4663=120); global 0 still
# disables every chain without an explicit per-chain value. Read at each socket connect.
MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S_DEFAULT_BY_CHAIN = {"4663": 30.0}


def onchain_sub_silence_s_for(chain):
    key = str(chain or "").strip()
    raw = os.getenv(f"MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S_{key}", "").strip()
    if raw:
        return float(raw)
    if MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S <= 0:
        return 0.0
    return MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S_DEFAULT_BY_CHAIN.get(key, MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S)

MEMECOIN_ONCHAIN_M5_VENUE_GAP_MIN_USD = float(
    os.getenv("MEMECOIN_ONCHAIN_M5_VENUE_GAP_MIN_USD", "100"))
MEMECOIN_ONCHAIN_M5_VENUE_GAP_RATIO = float(
    os.getenv("MEMECOIN_ONCHAIN_M5_VENUE_GAP_RATIO", "2.0"))

MEMECOIN_REFUSAL_SHADOW_ENABLED = os.getenv(
    "MEMECOIN_REFUSAL_SHADOW_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
# Only a buy this big asks the question "should a strong source buy overturn a blocker".
MEMECOIN_REFUSAL_SHADOW_MIN_SOURCE_USD = float(
    os.getenv("MEMECOIN_REFUSAL_SHADOW_MIN_SOURCE_USD", "700"))
# A MODELLING CHOICE, NOT A MEASUREMENT. Chain 4663 measured p50 4.78 s and p90 12.94 s from
# decision to recorded fill (Lambda, 2026-09-23), so 6.0 s sits between them. Solana's p50 is
# 1.89 s and is not sampled by this lane.
MEMECOIN_REFUSAL_SHADOW_FILL_LAG_S = float(
    os.getenv("MEMECOIN_REFUSAL_SHADOW_FILL_LAG_S", "6.0"))
MEMECOIN_REFUSAL_SHADOW_CLIP_USD = float(os.getenv("MEMECOIN_REFUSAL_SHADOW_CLIP_USD", "57.50"))
MEMECOIN_REFUSAL_SHADOW_SWEEP_S = float(os.getenv("MEMECOIN_REFUSAL_SHADOW_SWEEP_S", "15.0"))
MEMECOIN_REFUSAL_SHADOW_HORIZON_S = float(
    os.getenv("MEMECOIN_REFUSAL_SHADOW_HORIZON_S", "1800"))
# THE WINDOW IS BOUNDED BY TAPE RETENTION, NOT BY TASTE. `pool_ticks` in
# data/memecoin_onchain_volume.db retained exactly 60.7 minutes when measured on 2026-09-23
# (08:58:52Z to 09:59:35Z on both 4663 and 8453). A decision 55 min old still has its own
# decision-time bar inside the tape; one 56+ min old may not. Sampling starts at 31 min so the
# 30-minute horizon has tape to run over.
MEMECOIN_REFUSAL_SHADOW_MIN_AGE_S = float(
    os.getenv("MEMECOIN_REFUSAL_SHADOW_MIN_AGE_S", "1860"))
MEMECOIN_REFUSAL_SHADOW_MAX_AGE_S = float(
    os.getenv("MEMECOIN_REFUSAL_SHADOW_MAX_AGE_S", "3300"))

# Our funded custody PUBLIC addresses, comma separated (Solana exact case; EVM any case). Read by
# the wallet map to drop edges that are really our own copying (spec 2026-09-30 wallet map).
# Public keys only: nothing here can sign. Empty = no exclusion.
MEMECOIN_OWN_ADDRESSES = os.getenv("MEMECOIN_OWN_ADDRESSES", "").strip()

# EXIT PRICE TAPE, part A-lite (spec 2026-09-30 §3a). One row per executable exit confirmation:
# the decision preview (or its failure), keyed to the attempt by decision_at == attempted_at.
# In-memory append on the sweep, written by a daemon thread to the tape's own file (see
# memecoin_exit_tape), never the ledger. Default off.
MEMECOIN_EXIT_TAPE_ENABLED = os.getenv(
    "MEMECOIN_EXIT_TAPE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


# FILL TIMING READS (operator via Alpha, 2026-10-02; spec 2026-10-02-memecoin-fill-timing-design.md).
# The resolver (scripts/research/fill_timing_resolver.py) fills a missing source or landing slot with
# reads only when its knob is on; both default OFF until the operator says yes to the budget:
# Solana at most one batched getSignatureStatuses per pass on MEMECOIN_RESEARCH_HELIUS_RPC_URL (flat-
# book exception), EVM at most 2 reads per fill on the 4663 research key / Base tenderly (56 none).
# A row that needs a read with the knob off is 'unread' and reopens when it turns on (24 h window).
# The research Helius key carries the flat-book rule (reads only while no real Solana position is
# open) unless the operator's yes for this knob says otherwise; its reads go through the key's
# SharedPacer, so the gap and a 429 pause are shared with every other reader of it (Gamma LOW2).
# Independent research-endpoint custody logs; measurement only, never an admission gate.
MEMECOIN_EXECUTION_OBSERVER_ENABLED = os.getenv(
    "MEMECOIN_EXECUTION_OBSERVER_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
MEMECOIN_FILL_TIMING_SOLANA_READS = os.getenv(
    "MEMECOIN_FILL_TIMING_SOLANA_READS", "0").strip().lower() in {"1", "true", "yes", "on"}
MEMECOIN_FILL_TIMING_EVM_READS = os.getenv(
    "MEMECOIN_FILL_TIMING_EVM_READS", "0").strip().lower() in {"1", "true", "yes", "on"}

# Measurement-only, head-driven receipt witness. No funded authority or synchronous fallback.
MEMECOIN_BSC_EXECUTION_OBSERVER_ENABLED = os.getenv(
    "MEMECOIN_BSC_EXECUTION_OBSERVER_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def desk_trail_arm_pct(env=None):
    """DESK trail arming (operator 2026-10-26, Gamma's trail-arming replay): a desk position's trail arms only once its
    executable peak reaches +X% over entry. Read at call time; unset, malformed, non-finite or <= 0 = arm as today."""
    src = env if env is not None else os.environ
    try:
        value = float(src.get("MEMECOIN_DESK_TRAIL_ARM_PCT", "0") or 0)
    except ValueError:
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0
