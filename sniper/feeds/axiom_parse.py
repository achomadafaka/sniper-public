"""axiom_parse.py — pure parsers for the Axiom observation lanes.

See docs/superpowers/specs/2026-09-16-axiom-observation-lanes-design.md section
4.3. Each parser takes DOM text, a JSON string, or an already-parsed object
captured by sniper/feeds/axiom_browser.py, and returns structured rows. A
shape this module does not recognize returns an empty result (never a guess)
so a UI change on Axiom's side produces "no data", not wrong data.

Shape status (Beta, 2026-09-16). First pass read tests/fixtures/axiom/
{trackers,discover,pulse}.html and ws_frames.jsonl (2,443 WS frames). Second
pass read data/axiom_raw.db (table axiom_raw_messages, transport='rest' —
NOT sniper_tracker.db, which does not hold this table), the capture worker's
real REST responses:

- parse_trader_directory: REAL shape, high confidence, from REST
  smart-money-traders (`{"rows": [{handle, displayName, wallets: {solana,
  evm}, windows: {"24h": {pnlUsd}}, volumeUsd, trades, followers}]}`). A
  trader can hold a verified Solana address AND a verified EVM address at
  once (Axiom's identity model has ONE generic EVM address per trader, not
  one per EVM chain) — this module emits one row per verified wallet slot;
  the EVM slot's `chain` is None because bnb/base/robinhood are not
  distinguishable from this payload alone. Never implemented from
  trackers.html: that DOM is the KOL-callout card feed, not a directory —
  zero "follower" text exists in its rendered DOM, only in the page's
  bootstrap JSON config.
- parse_kol_callouts: REAL shape, high confidence, THREE distinct schemas.
  WS room "gmgn-callouts" / "<chain-prefix>|gmgn-callouts" carries one
  gmgn-sourced callout object; REST `*/gmgn-callouts-feed` carries the same
  object, many at once, in `{"callouts": [...]}`. REST `*/callouts-feed` is
  NOT a wrapper variant of that — it is Axiom's own native callout schema, a
  BARE top-level list of a differently-keyed object (`callerHandle`,
  `tokenAddress`, `marketCapUsdAtPost`, `kind` — "callout" vs "manifesto",
  only the former counts). Every output field is still a named JSON key.
- parse_discovery: three shapes.
  - REST `*/discover-trending` on bnb/base/robinhood hosts: a list of fully
    keyed objects (tokenAddress, bestPairAddress, pairMarketCapUsd,
    pairLiquidityUsd, tokenVolumeUsd, all USD-denominated). High confidence
    for chain, mint, pair, mcap, liquidity. The parser stores tokenVolumeUsd
    as volume_value with volume_unit="USD". Its window remains unknown;
    volume_m5 stays null. `chain` is not in this payload; pass
    `source_url` (the fetch URL) so it can be read from the host prefix.
  - REST `*/sol-new-trending` and WS room "<prefix>:trending:5m" (always
    Solana): a bare list of 57-element positional arrays (REST) or the same
    array wrapped in {"room","content"} (WS). The legend is read from
    Axiom's own decoder (`mapTrendingPairV2`, their public Next.js bundle,
    chunk 1qm-f5skptoe4.js), not inferred: [0] pairAddress -> `pair`, [1]
    tokenAddress -> `mint`, [24] marketCapSol -> `mcap`, [27] liquiditySol ->
    `liquidity`. [49]/[50] (pairSolAccount/pairTokenAccount) were
    independently cross-checked against the same keys embedded in a real
    row's own [8] JSON sub-blob — exact match, so the legend is trusted.
    `mcap`/`liquidity` here are SOL-denominated, NOT USD like the EVM path
    above — do not compare the two chains' values directly, and this module
    does not convert currencies. No confirmed field for `volume_m5` on
    Solana (no windowed-volume element exists in the legend). `age_s` is
    always None in every shape — Axiom reports
    `tokenCreatedAt`/`pairCreatedAt`/`createdAt` (a timestamp), not an
    elapsed duration; computing age needs a wall clock this module does not
    hold, so that is the capture worker's job (received_at - createdAt), not
    this parser's. `filters` names the endpoint/room the row came from
    ("discover-trending", "sol-new-trending", or the WS room, e.g.
    "sol-discover:trending:5m") so a paper arm can select by source; it is
    None only when `source_url` was not supplied.
- parse_wallet_transactions (REST) / parse_wallet_event (WS frame, or one
  already-decoded REST row): REAL shape, from REST
  `tracked-wallet-transactions-v4` (a 25-element array per row). The legend
  is read from Axiom's own decoder (`decodeTrackedWalletTransactionsCompact`,
  their public Next.js bundle, chunk 1xqpq-daqh2z6.js), not inferred: [0]
  transactionTime -> `axiom_ts`, [1] walletAddress -> `address`, [2]
  signature -> `tx_sig`, [7] totalUsd -> `amount_usd`, [9] type -> `side`
  (only "buy"/"sell" accepted), [16] tokenAddress -> `mint`. An earlier
  version of this parser read `mint` from the CDN image URL instead (element
  19), after finding element 16 diverged from the image URL's filename stem
  on 30% of real rows -- that was backwards; element 16 is Axiom's own
  authoritative field, the image URL for a migrated token's pair apparently
  points at a different asset. No `v:<wallet>` WS room frame has been
  captured, so the WS path still refuses everything (see the quiet/loud
  split below); once one lands, route it into the same 25-element decoder if
  the shape matches, or add a second decoder if it doesn't.

Refusal has two tiers, both correct behaviour, not placeholders:
- LOUD: input that looks like it was meant to be JSON (starts with "{" or
  "[") but does not parse, or parses to a shape this module has not been
  taught. Logs the first 200 bytes, once per (parser, first 40 bytes) for the
  life of the process, then stays silent — the worker was logging one line
  per frame at 30/s on a handful of distinct wallet rooms; this collapses
  that to one line per distinct shape, forever, not per frame.
- QUIET: input that does not look like JSON at all (e.g. ~26% of the frames
  on wss://pulse-unified2.axiom.trade/ws, which are NOT engine.io text
  ping/pong — the raw bytes are length-prefixed binary blobs with embedded
  strings like "sol"/"newPairs", consistent with a MessagePack-style wire
  format), and a WS frame whose room is a known-not-ours infrastructure room
  (block_hash, *-priority-fee*, *gas-prices, jito-bribe-fee,
  online-users-count, connection_monitor, *thumbnail — together over half of
  the 2,443 captured WS frames). Neither logs nor counts toward the
  `parse_refused` heartbeat counter.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging

import base58
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)
PARSER_VERSION = "axiom-discovery-v2"

CHAIN_MAP = {
    "sol": "solana",
    "solana": "solana",
    "robinhood": "4663",
    "bnb": "56",
    "base": "8453",
}

_MONEY_RE = re.compile(r"^([+-]?)\$?([0-9][0-9,]*\.?[0-9]*)\s*([KMBkmb]?)$")
_MONEY_MULTIPLIERS = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}
_MONEY_EMPTY_TOKENS = {"", "-", "—", "–", "n/a", "na"}

_TRUNCATED_ADDRESS_RE = re.compile(r"^\S{2,15}\.\.\.\S{2,15}$")

# Confirmed 2026-09-16 from ws_frames.jsonl: WS room "<prefix>:trending:5m" -> chain.
_DISCOVERY_ROOMS = {
    "sol-discover:trending:5m": "sol",
    "bnb-v2|discover-trending:5m": "bnb",
    "base|discover-trending:5m": "base",
    "rh-v2|discover-trending:5m": "robinhood",
}

_KOL_CALLOUT_ROOM_RE = re.compile(r"^(?:[a-z0-9]+-v2\|)?gmgn-callouts$")

# Confirmed 2026-09-16, live worker: high-volume infra rooms with nothing to
# do with wallet trade events. Silenced with zero logging, not just dedup.
_IRRELEVANT_ROOM_PATTERNS = (
    "block_hash",
    "*-priority-fee*",
    "*gas-prices",
    "jito-bribe-fee",
    "online-users-count",
    "connection_monitor",
    "*thumbnail",
)

# Confirmed 2026-09-16 from data/axiom_raw.db: per-chain host prefixes used
# consistently across smart-money-traders, discover-trending and
# tracked-wallet-transactions. Unprefixed api*.axiom.trade is Solana.
_CHAIN_HOST_PREFIXES = (
    ("bnb-", "bnb"),
    ("base-", "base"),
    ("rh-", "robinhood"),
)
_SOL_HOST_RE = re.compile(r"^api\d*\.axiom\.trade$")

# Confirmed 2026-09-16: a stable 25-element array, legend read from Axiom's
# own decoder (`decodeTrackedWalletTransactionsCompact`, public Next.js
# bundle chunk 1xqpq-daqh2z6.js), not inferred from data. Full legend, for
# reference (only the indices below are actually used):
#   0 transactionTime, 1 walletAddress, 2 signature, 3 priceSol, 4 priceUsd,
#   5 tokenAmount, 6 totalSol, 7 totalUsd, 8 feesSol, 9 type, 10 detailedType,
#   11 pairAddress, 12 pairCreatedAt, 13 averageMcBought, 14 averageMcSold,
#   15 pnlSol, 16 tokenAddress, 17 tokenName, 18 tokenTicker, 19 tokenImage,
#   20 tokenDecimals, 21 supply, 22 realProtocol, 23 isMigrated, 24 liquiditySol.
# Element 16 (tokenAddress) is the real mint -- an earlier version of this
# module derived mint from the CDN image URL (element 19) instead, after
# finding element 16 diverged from the image URL's filename stem on 30% of
# real rows. That was backwards: 16 is Axiom's own authoritative field: the
# image URL for a migrated token's pair apparently points at a different
# (pair-keyed) asset, not the mint's. Trust the named field over the
# heuristic.
_WALLET_TX_ROW_LEN = 25
_WALLET_TX_AXIOM_TS_IDX = 0
_WALLET_TX_ADDRESS_IDX = 1
_WALLET_TX_SIG_IDX = 2
_WALLET_TX_AMOUNT_USD_IDX = 7
_WALLET_TX_SIDE_IDX = 9
_WALLET_TX_MINT_IDX = 16

# Process-local: log a given (parser, first-40-bytes) refusal once, ever.
# Not part of the return-value contract -- purely to stop log flooding.
_LOGGED_REFUSALS: set[tuple[str, str]] = set()


def normalize_chain(name: Any) -> Optional[str]:
    """Maps an Axiom chain label to our chain id. Unknown label -> None."""
    if not isinstance(name, str):
        return None
    key = name.strip().lower()
    if not key:
        return None
    return CHAIN_MAP.get(key)


def parse_money_compact(text: Any) -> Optional[float]:
    """"$1.68M" -> 1680000.0, "+$434K" -> 434000.0, "—" -> None."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if s.lower() in _MONEY_EMPTY_TOKENS:
        return None
    m = _MONEY_RE.match(s)
    if not m:
        return None
    sign, digits, suffix = m.groups()
    digits = digits.replace(",", "")
    if digits in ("", "."):
        return None
    try:
        value = float(digits)
    except ValueError:
        return None
    value *= _MONEY_MULTIPLIERS.get(suffix.upper(), 1.0)
    return -value if sign == "-" else value


def _looks_truncated_address(s: Any) -> bool:
    """DOM text like "Cqu5uK...QSsR" is not a usable address."""
    if not isinstance(s, str):
        return False
    return bool(_TRUNCATED_ADDRESS_RE.match(s.strip()))


def _valid_address(value: Any, *, solana: bool) -> bool:
    """Accept a full address with the required byte length."""
    if not isinstance(value, str):
        return False
    if not solana:
        return re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is not None
    if not 32 <= len(value) <= 44:
        return False
    try:
        return len(base58.b58decode(value)) == 32
    except ValueError:
        return False


def _first_bytes(raw: Any, limit: int = 200) -> str:
    if raw is None:
        return ""
    if isinstance(raw, (dict, list)):
        try:
            raw = json.dumps(raw)
        except (TypeError, ValueError):
            raw = str(raw)
    elif not isinstance(raw, str):
        raw = str(raw)
    return raw[:limit]


def _refuse(context: str, raw: Any) -> None:
    key = (context, _first_bytes(raw, 40))
    if key in _LOGGED_REFUSALS:
        return
    _LOGGED_REFUSALS.add(key)
    logger.warning(
        "axiom_parse: refused unrecognized shape in %s: %r", context, _first_bytes(raw)
    )


def _looks_like_json_text(s: str) -> bool:
    stripped = s.lstrip()
    return bool(stripped) and stripped[0] in "{["


def _load_json_maybe(raw: Any) -> Any:
    """Parses JSON text, or passes an already-decoded dict/list through.

    Returns None both for non-JSON-looking text (quiet: transport noise, e.g.
    the binary frames on pulse-unified2) and for text that looks like JSON
    but fails to parse (still refused loudly by the caller).
    """
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not _looks_like_json_text(raw):
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _is_json_ish(raw: Any) -> bool:
    """True if raw is already a dict/list, or text that starts like JSON."""
    if isinstance(raw, (dict, list)):
        return True
    return isinstance(raw, str) and _looks_like_json_text(raw)


def _is_known_irrelevant_room(room: Any) -> bool:
    if not isinstance(room, str):
        return False
    return any(fnmatch.fnmatch(room, pat) for pat in _IRRELEVANT_ROOM_PATTERNS)


def _chain_from_source_url(source_url: Any) -> Optional[str]:
    if not isinstance(source_url, str):
        return None
    host = source_url.split("//", 1)[-1].split("/", 1)[0].lower()
    for prefix, label in _CHAIN_HOST_PREFIXES:
        if host.startswith(prefix):
            return normalize_chain(label)
    if _SOL_HOST_RE.match(host):
        return normalize_chain("sol")
    return None


def _endpoint_basename(source_url: Any) -> Optional[str]:
    """The REST path's last segment (e.g. "discover-trending"), for `filters`."""
    if not isinstance(source_url, str) or not source_url:
        return None
    path = source_url.split("?", 1)[0].split("#", 1)[0]
    base = path.rstrip("/").rsplit("/", 1)[-1]
    return base or None


# ---------------------------------------------------------------------------
# S1: trader directory
# ---------------------------------------------------------------------------


def _trader_directory_rows_from_entry(entry: Any) -> list[dict]:
    if not isinstance(entry, dict):
        return []
    handle = entry.get("handle")
    if not isinstance(handle, str) or not handle:
        return []
    display = entry.get("displayName")
    pnl_24h = None
    windows = entry.get("windows")
    if isinstance(windows, dict) and isinstance(windows.get("24h"), dict):
        pnl_24h = windows["24h"].get("pnlUsd")
    trades = entry.get("trades")
    volume = entry.get("volumeUsd")
    followers = entry.get("followers")
    base_row = {
        "x_handle": handle,
        "source": "axiom_smart_money",
        "display": display,
        "pnl_24h": pnl_24h,
        "trades": trades,
        "volume": volume,
        "followers": followers,
    }

    rows: list[dict] = []
    wallets = entry.get("wallets")
    if not isinstance(wallets, dict):
        return rows
    sol = wallets.get("solana")
    if isinstance(sol, dict) and sol.get("status") == "verified" and isinstance(sol.get("address"), str):
        rows.append({**base_row, "address": sol["address"], "chain": normalize_chain("sol")})
    evm = wallets.get("evm")
    if isinstance(evm, dict) and evm.get("status") == "verified" and isinstance(evm.get("address"), str):
        # Axiom's identity model has one EVM address per trader, not one per
        # EVM chain -- bnb/base/robinhood are not distinguishable here.
        rows.append({**base_row, "address": evm["address"], "chain": None})
    return rows


def parse_trader_directory(dom_or_json: Any) -> list[dict]:
    """S1: [{x_handle, display, address, chain, pnl_24h, trades, volume, followers}].

    REST smart-money-traders only. Never implemented from trackers.html: see
    module docstring, that DOM is the KOL-callout feed, not a directory.
    """
    obj = _load_json_maybe(dom_or_json)
    if obj is None:
        if _is_json_ish(dom_or_json):
            _refuse("parse_trader_directory", dom_or_json)
        return []
    if not isinstance(obj, dict) or not isinstance(obj.get("rows"), list):
        _refuse("parse_trader_directory", dom_or_json)
        return []
    rows: list[dict] = []
    for entry in obj["rows"]:
        rows.extend(_trader_directory_rows_from_entry(entry))
    return rows


def parse_tracked_wallets(body: Any) -> list[dict]:
    """Read named identity objects from the captured tracked-wallets-v4 response."""
    obj = _load_json_maybe(body)
    if not isinstance(obj, dict) or not isinstance(obj.get("trackedWallets"), list):
        if _is_json_ish(body):
            _refuse("parse_tracked_wallets", body)
        return []
    rows = []
    for wallet in obj["trackedWallets"]:
        if not isinstance(wallet, list):
            continue
        for identity in wallet:
            if not isinstance(identity, dict):
                continue
            chain = normalize_chain(identity.get("chain"))
            if chain is None and identity.get("chain") != "evm":
                continue
            address = identity.get("address")
            handle = identity.get("xHandle")
            if not _valid_address(address, solana=chain == "solana") or not isinstance(handle, str) or not handle:
                continue
            rows.append({"x_handle": handle, "display": identity.get("xDisplayName"),
                         "address": address, "chain": chain, "pnl_24h": None,
                         "trades": None, "volume": None, "followers": None,
                         "source": "axiom_tracked_identity"})
    return rows


def parse_trader_positions(dom_or_json: Any) -> list[dict]:
    """S1 detail: [{address, mint, bought_usd, sold_usd, remaining_usd, opened_at}].

    Not implemented. No fixture captured this shape yet.
    """
    if _is_json_ish(dom_or_json):
        _refuse("parse_trader_positions", dom_or_json)
    return []


# ---------------------------------------------------------------------------
# S3: discovery
# ---------------------------------------------------------------------------


def _parse_discovery_rest_labeled(obj: list, source_url: Any, raw_for_log: Any) -> list[dict]:
    """REST `*/discover-trending` on bnb/base/robinhood: a list of fully
    keyed objects (tokenAddress, bestPairAddress, pairMarketCapUsd, ...)."""
    chain = _chain_from_source_url(source_url)
    filters = _endpoint_basename(source_url)
    rows: list[dict] = []
    saw_valid = False
    for item in obj:
        if not isinstance(item, dict) or "tokenAddress" not in item:
            continue
        saw_valid = True
        mint = item.get("tokenAddress")
        if not _valid_address(mint, solana=False):
            continue
        pair = item.get("bestPairAddress")
        mcap = item.get("pairMarketCapUsd")
        liquidity = item.get("pairLiquidityUsd")
        volume_m5 = item.get("tokenVolumeUsd")
        rows.append(
            {
                "chain": chain,
                "mint": mint,
                "pair": pair if _valid_address(pair, solana=False) else None,
                "mcap": mcap if isinstance(mcap, (int, float)) else None,
                "liquidity": liquidity if isinstance(liquidity, (int, float)) else None,
                "mcap_unit": "USD",
                "liquidity_unit": "USD",
                "volume_value": volume_m5 if isinstance(volume_m5, (int, float)) else None,
                "volume_unit": "USD",
                "volume_window_s": None,
                "volume_m5": None,
                "parser_version": PARSER_VERSION,
                "age_s": None,
                "filters": filters,
            }
        )
    if not saw_valid:
        _refuse("parse_discovery", raw_for_log)
        return []
    return rows


def _sol_positional_row_to_discovery(token_row: Any, filters: Optional[str] = None) -> Optional[dict]:
    """The Solana positional discovery row: WS sol-discover:trending:5m
    content elements, and the identically-shaped REST */sol-new-trending
    rows. Confirmed 2026-09-16 against Axiom's own decoder (`mapTrendingPairV2`
    in their public Next.js bundle, chunk 1qm-f5skptoe4.js): pairAddress=[0],
    tokenAddress=[1], ..., marketCapSol=[24], ..., liquiditySol=[27], .... 49
    and 50 independently cross-checked against the `pairSolAccount` /
    `pairTokenAccount` keys embedded in element [8]'s JSON sub-blob on a real
    captured row -- exact match, so the legend is trusted.

    mcap and liquidity here are SOL-denominated (marketCapSol / liquiditySol),
    NOT USD like the EVM REST path's pairMarketCapUsd / pairLiquidityUsd --
    do not compare the two chains' mcap/liquidity values directly, and do not
    silently convert without a real SOL/USD rate.
    """
    if not isinstance(token_row, list) or len(token_row) != 57:
        return None
    pair, mint = token_row[0], token_row[1]
    if not _valid_address(mint, solana=True) or not _valid_address(pair, solana=True):
        return None
    m, l = token_row[24], token_row[27]
    mcap = m if isinstance(m, (int, float)) else None
    liquidity = l if isinstance(l, (int, float)) else None
    return {
        "chain": normalize_chain("sol"),
        "mint": mint,
        "pair": pair if isinstance(pair, str) else None,
        "mcap": mcap,
        "liquidity": liquidity,
        "mcap_unit": "SOL",
        "liquidity_unit": "SOL",
        "volume_value": None,
        "volume_unit": None,
        "volume_window_s": None,
        "parser_version": PARSER_VERSION,
        "volume_m5": None,
        "age_s": None,
        "filters": filters,
    }


def _parse_discovery_rest_positional(obj: list, raw_for_log: Any, filters: Optional[str] = "sol-new-trending") -> list[dict]:
    """REST `*/sol-new-trending`: a bare list of 57-element positional
    arrays -- always Solana (the endpoint name is chain-specific)."""
    rows = [r for r in (_sol_positional_row_to_discovery(tr, filters) for tr in obj) if r is not None]
    if not rows and obj:
        _refuse("parse_discovery", raw_for_log)
        return []
    return rows


def _parse_discovery_ws(obj: dict, raw_for_log: Any) -> list[dict]:
    room = obj.get("room")
    chain_label = _DISCOVERY_ROOMS.get(room) if isinstance(room, str) else None
    content = obj.get("content")
    if chain_label is None or not isinstance(content, list):
        _refuse("parse_discovery", raw_for_log)
        return []

    if chain_label == "sol":
        return _parse_discovery_rest_positional(content, raw_for_log, filters=room)

    # No captured decoder establishes the EVM WebSocket field order.
    _refuse("parse_discovery", raw_for_log)
    return []


def parse_discovery(dom_or_json: Any, source_url: Optional[str] = None) -> list[dict]:
    """S3: [{chain, mint, pair, mcap, liquidity, volume_m5, age_s, filters}].

    REST `*/discover-trending` (bnb/base/robinhood): a list of fully keyed
    objects -- full confidence on chain (from `source_url`), mint, pair,
    mcap, liquidity (all USD-denominated). volume_value has unit USD.
    The volume window is unknown, so volume_m5 stays null.
    REST `*/sol-new-trending` and WS room "<prefix>:trending:5m" (always
    Solana): a bare list of 57-element positional arrays (REST) or the same
    array wrapped in {"room","content"} (WS) -- confirmed against Axiom's own
    decoder, see `_sol_positional_row_to_discovery`. chain, mint, pair, mcap,
    liquidity confirmed; mcap and liquidity are SOL-denominated here, unlike
    the EVM path's USD -- do not compare across chains. `source_url` is not
    needed for this shape (the endpoint is Solana-only by name) but is
    accepted for call-site uniformity.
    age_s is always None. filters names the source: the REST endpoint
    basename ("discover-trending", "sol-new-trending") or the WS room name.
    """
    obj = _load_json_maybe(dom_or_json)
    if obj is None:
        if _is_json_ish(dom_or_json):
            _refuse("parse_discovery", dom_or_json)
        return []
    if isinstance(obj, list):
        if not obj:
            return []
        if isinstance(obj[0], dict):
            return _parse_discovery_rest_labeled(obj, source_url, dom_or_json)
        if isinstance(obj[0], list):
            return _parse_discovery_rest_positional(obj, dom_or_json)
        _refuse("parse_discovery", dom_or_json)
        return []
    if isinstance(obj, dict):
        return _parse_discovery_ws(obj, dom_or_json)
    _refuse("parse_discovery", dom_or_json)
    return []


# ---------------------------------------------------------------------------
# S2: KOL callouts
# ---------------------------------------------------------------------------


def _callout_identity(item: dict, source_kind: str) -> dict:
    """Preserve provider identity. A payload hash is not proof of a unique call."""
    source_id = item.get("id")
    called_at = item.get("createdAt")
    source_id = source_id if isinstance(source_id, str) and source_id else None
    called_at = called_at if isinstance(called_at, str) and called_at else None
    kind = "source_id" if source_id else "source_time" if called_at else "unique_payload"
    payload_sha = None
    if kind == "unique_payload":
        payload_sha = hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"source_id": source_id, "called_at": called_at, "source_kind": source_kind,
            "identity_kind": kind, "payload_sha": payload_sha}


def _callout_to_row(callout: Any) -> Optional[dict]:
    """The gmgn-sourced shape (WS "gmgn-callouts" room, REST
    */gmgn-callouts-feed's {"callouts":[...]})."""
    if not isinstance(callout, dict):
        return None
    kol_handle = callout.get("callerHandle")
    mint = callout.get("tokenAddress")
    if not isinstance(kol_handle, str) or not isinstance(mint, str):
        return None
    return {
        "kol_handle": kol_handle,
        "mint": mint,
        "chain": normalize_chain(callout.get("chain")),
        "called_mcap": callout.get("callMarketCapUsd"),
        "text": callout.get("body"),
        **_callout_identity(callout, "gmgn"),
    }


def _native_callout_shape_matches(item: Any) -> bool:
    return isinstance(item, dict) and "tokenAddress" in item and "callerHandle" in item


def _native_callout_to_row(item: dict) -> Optional[dict]:
    """The native shape (REST */callouts-feed, a bare top-level list).
    "kind" is "callout" or "manifesto" (a caller bio/pinned post, not a
    trade call) -- only "callout" rows count. "callerHandle" is always the
    display handle (populated even for axiom-native, non-X callers, where
    "xHandle" is null); "marketCapUsdAtPost" is the mcap at call time."""
    if item.get("kind") != "callout":
        return None
    kol_handle = item.get("callerHandle")
    mint = item.get("tokenAddress")
    if not isinstance(kol_handle, str) or not kol_handle:
        return None
    if not isinstance(mint, str) or not mint:
        return None
    called_mcap = item.get("marketCapUsdAtPost")
    return {
        "kol_handle": kol_handle,
        "mint": mint,
        "chain": normalize_chain(item.get("chain")),
        "called_mcap": called_mcap if isinstance(called_mcap, (int, float)) else None,
        "text": item.get("body"),
        **_callout_identity(item, "axiom"),
    }


def parse_kol_callouts(dom_or_json: Any) -> list[dict]:
    """S2: [{kol_handle, mint, chain, called_mcap, text}].

    Three real shapes, all confirmed 2026-09-16:
    - WS room "gmgn-callouts" / "<chain-prefix>|gmgn-callouts": one gmgn
      callout per frame, wrapped in {"room","content":{"callout":{...}}}.
    - REST `*/gmgn-callouts-feed`: the gmgn callout, {"callouts":[...]}.
    - REST `*/callouts-feed`: Axiom's own native callouts, a BARE top-level
      list of a differently-keyed object (see _native_callout_to_row) --
      this is a different schema from gmgn, not a wrapper variant of it.
    Every field is a named JSON key, not a position guess.
    """
    obj = _load_json_maybe(dom_or_json)
    if obj is None:
        if _is_json_ish(dom_or_json):
            _refuse("parse_kol_callouts", dom_or_json)
        return []

    if isinstance(obj, list):
        if not obj:
            return []
        matched = [item for item in obj if _native_callout_shape_matches(item)]
        if not matched:
            _refuse("parse_kol_callouts", dom_or_json)
            return []
        return [r for r in (_native_callout_to_row(item) for item in matched) if r is not None]

    if not isinstance(obj, dict):
        _refuse("parse_kol_callouts", dom_or_json)
        return []

    callouts = obj.get("callouts")
    if isinstance(callouts, list):
        if not callouts:
            return []
        rows = [r for r in (_callout_to_row(c) for c in callouts) if r is not None]
        if not rows:
            _refuse("parse_kol_callouts", dom_or_json)
        return rows

    room = obj.get("room")
    if not isinstance(room, str) or not _KOL_CALLOUT_ROOM_RE.match(room):
        _refuse("parse_kol_callouts", dom_or_json)
        return []
    content = obj.get("content")
    row = _callout_to_row(content.get("callout") if isinstance(content, dict) else None)
    if row is None:
        _refuse("parse_kol_callouts", dom_or_json)
        return []
    return [row]


# ---------------------------------------------------------------------------
# S4: wallet trade events
# ---------------------------------------------------------------------------


def _wallet_tx_row_to_event(row: Any, source_url: Optional[str]) -> Optional[dict]:
    if not isinstance(row, list) or len(row) != _WALLET_TX_ROW_LEN:
        return None
    axiom_ts = row[_WALLET_TX_AXIOM_TS_IDX]
    address = row[_WALLET_TX_ADDRESS_IDX]
    tx_sig = row[_WALLET_TX_SIG_IDX]
    amount_usd = row[_WALLET_TX_AMOUNT_USD_IDX]
    side = row[_WALLET_TX_SIDE_IDX]
    mint = row[_WALLET_TX_MINT_IDX]
    if not isinstance(address, str) or not address:
        return None
    if not isinstance(tx_sig, str) or not tx_sig:
        return None
    if side not in ("buy", "sell"):
        return None
    chain = _chain_from_source_url(source_url)
    solana = chain == "solana" or (chain is None and not address.startswith("0x"))
    if not _valid_address(address, solana=solana) or not _valid_address(mint, solana=solana):
        return None
    if chain is None and solana:
        chain = "solana"

    return {
        "address": address,
        "chain": chain,
        "mint": mint,
        "side": side,
        "amount_usd": amount_usd if isinstance(amount_usd, (int, float)) else None,
        "tx_sig": tx_sig,
        "axiom_ts": axiom_ts if isinstance(axiom_ts, str) else None,
    }


def parse_wallet_transactions(body: Any, source_url: Optional[str] = None) -> list[dict]:
    """REST tracked-wallet-transactions-v4: [{address, chain, mint, side, amount_usd, tx_sig, axiom_ts}].

    Every field confirmed against Axiom's own decoder (module docstring);
    none of these are guessed from data shape alone.
    """
    obj = _load_json_maybe(body)
    if obj is None:
        if _is_json_ish(body):
            _refuse("parse_wallet_transactions", body)
        return []
    if not isinstance(obj, list):
        _refuse("parse_wallet_transactions", body)
        return []
    events = []
    for row in obj:
        ev = _wallet_tx_row_to_event(row, source_url)
        if ev is not None:
            events.append(ev)
    return events


def parse_wallet_event(frame_or_row: Any, source_url: Optional[str] = None) -> Optional[dict]:
    """S4: one WS frame, or one already-decoded REST transaction row, -> an event or None.

    A `v:<wallet>` WS room has not been captured, so that path still refuses
    everything. A frame whose room is a known-not-ours infra room (module
    docstring) returns None with no log at all. Non-JSON-looking input
    (binary transport noise) also returns None with no log.
    """
    if isinstance(frame_or_row, (dict, list)):
        obj = frame_or_row
    elif isinstance(frame_or_row, str) and _looks_like_json_text(frame_or_row):
        obj = _load_json_maybe(frame_or_row)
        if obj is None:
            _refuse("parse_wallet_event", frame_or_row)
            return None
    else:
        return None

    if isinstance(obj, list):
        return _wallet_tx_row_to_event(obj, source_url)
    if isinstance(obj, dict):
        if _is_known_irrelevant_room(obj.get("room")):
            return None
        _refuse("parse_wallet_event", frame_or_row)
        return None
    _refuse("parse_wallet_event", frame_or_row)
    return None
