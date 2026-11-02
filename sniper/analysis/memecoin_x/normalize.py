"""Pure normalization for X capture: handles, addresses, coin mentions, and (Task 4) whole
cards. Network is only ever taken from the X content/URL itself — never inferred from a later
buy. A ticker collides, so ticker-only mentions are hypothesis evidence (not eligible). A
contract whose network cannot be independently determined is ambiguous (not eligible). Dex
pair/pool pages supply network context but their path id is not a token mint."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

PARSER_VERSION = 2

_EVM_RE = re.compile(r"(?<![A-Za-z0-9])0x[a-fA-F0-9]{40}(?![A-Za-z0-9])")
_SOL_CA_RE = re.compile(
    r"\b(?i:ca|contract)\s*[:=]\s*([1-9A-HJ-NP-Za-km-z]{32,44})\b"
)
_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Za-z][A-Za-z0-9]{1,14})\b")

# Captured-content chain slug -> memecoin feed network_id.
NETWORK_BY_CHAIN_SLUG = {
    "solana": "solana", "base": "8453", "ethereum": "1", "eth": "1",
    "bsc": "56", "bnb": "56", "avalanche": "43114", "avax": "43114",
    "robinhood": "4663",
}
_CHAIN_MARKERS = (
    (re.compile(r"\b(?:robinhood\s+chain|chain\s*4663)\b", re.I), "4663"),
    (re.compile(r"\b(?:on\s+base|base\s+chain)\b", re.I), "8453"),
    (re.compile(r"\bsolana\b", re.I), "solana"),
    (re.compile(r"\b(?:ethereum|on\s+eth)\b", re.I), "1"),
)


def normalize_handle(handle):
    if handle is None:
        return None
    value = str(handle).strip()
    if value.startswith("@"):
        value = value[1:]
    return value.lower() or None


def is_evm_address(addr: str) -> bool:
    return bool(addr) and bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", addr))


def is_solana_address(addr: str) -> bool:
    return bool(addr) and not addr.startswith("0x") and bool(
        re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr))


def normalize_address(addr: str, network_id=None) -> str:
    if is_evm_address(addr):
        return addr.lower()
    return addr  # Solana / unknown: preserve exact case


def is_valid_network_address(addr: str, network_id) -> bool:
    """Validate exact feed identity without inferring a network from the address."""
    network = str(network_id or "").strip()
    value = str(addr or "").strip()
    if network == "solana":
        return is_solana_address(value)
    if network.isdigit():
        return is_evm_address(value)
    return False


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _url_fact(url: str):
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    # Preserve path bytes/case. Lowercase only routing slugs.
    parts = [unquote(p) for p in parsed.path.split("/") if p]

    if _host_is(host, "dexscreener.com"):
        network = NETWORK_BY_CHAIN_SLUG.get(parts[0].lower()) if parts else None
        # /<chain>/<id> is a pair/pool page. Never promote <id> to a token mint.
        return network, "dex_url", None

    if _host_is(host, "pump.fun") or _host_is(host, "bonk.fun"):
        mint = parts[-1] if len(parts) >= 2 and parts[-2].lower() in {"coin", "token"} else None
        mint = mint if mint and is_solana_address(mint) else None
        return "solana", "launchpad_url", mint

    if _host_is(host, "hood.fun"):
        mint = parts[-1] if len(parts) >= 2 and parts[-2].lower() in {"coin", "token"} else None
        mint = mint if mint and is_evm_address(mint) else None
        return "4663", "launchpad_url", mint

    if _host_is(host, "noxa.fun") or _host_is(host, "fun.noxa.fi"):
        network = "4663" if parts and parts[0].lower() == "robinhood" else None
        mint = parts[1] if network and len(parts) >= 2 and is_evm_address(parts[1]) else None
        return network, "launchpad_url", mint

    return None, None, None


def _content_networks(text: str, urls) -> set:
    found = {network for pattern, network in _CHAIN_MARKERS if pattern.search(text)}
    for url in urls:
        network, _, _ = _url_fact(url)
        if network:
            found.add(network)
    return found


def _mk(network_id, mint, ticker, method):
    eligible = bool(mint) and bool(network_id)
    ambiguous = bool(mint) and not network_id
    return {"network_id": network_id, "mint": mint, "ticker": ticker,
            "extraction_method": method, "ambiguous": ambiguous, "eligible": eligible}


def extract_coin_mentions(text: str, urls) -> list:
    text = text or ""
    urls = urls or []
    out = []
    seen = set()
    networks = _content_networks(text, urls)
    sole_network = next(iter(networks)) if len(networks) == 1 else None

    def add(m):
        key = (m["network_id"], m["mint"], m["ticker"], m["extraction_method"])
        if key not in seen:
            seen.add(key)
            out.append(m)

    for url in urls:
        net, method, mint = _url_fact(url)
        if method and mint:
            add(_mk(net, normalize_address(mint, net), None, method))

    for raw in _EVM_RE.findall(text):
        network = sole_network if sole_network and sole_network != "solana" else None
        add(_mk(network, normalize_address(raw, network), None, "contract_text"))
    for raw in _SOL_CA_RE.findall(text):
        if not is_solana_address(raw):
            continue
        network = "solana" if not networks or networks == {"solana"} else None
        add(_mk(network, normalize_address(raw, network), None, "contract_text"))
    for tk in _TICKER_RE.findall(text):
        add(_mk(None, None, tk.upper(), "ticker_only"))
    return out


_RELATIONSHIPS = {"direct", "repost", "quote", "reply"}


def _parse_posted_at(raw):
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def normalize_card(card: dict) -> dict:
    pid = card.get("canonical_post_id")
    author = normalize_handle(card.get("author_handle"))
    actor = normalize_handle(card.get("actor_handle"))
    relationship = card.get("relationship")
    raw_card = {**card, "urls": list(card.get("urls") or [])}
    mentions = extract_coin_mentions(card.get("full_text", ""), card.get("urls") or [])
    if not bool(card.get("text_complete")):
        mentions = [{**mention, "eligible": False} for mention in mentions]
    rel = {
        "canonical_post_id": pid,
        "actor_handle": actor,
        "relationship": relationship,
        "referenced_author": normalize_handle(card.get("referenced_author")),
        "referenced_post_id": card.get("referenced_post_id"),
    }
    direct_identity_invalid = relationship == "direct" and actor != author
    if (not pid or not author or not actor or relationship not in _RELATIONSHIPS
            or direct_identity_invalid):
        return {"post": None, "relationship": rel, "mentions": mentions,
                "raw_card": raw_card, "eligible_for_forward": False}
    post = {
        "canonical_post_id": pid,
        "url": card.get("url"),
        "author_handle": author,
        "full_text": card.get("full_text"),
        "text_complete": bool(card.get("text_complete")),
        "posted_at_raw": card.get("posted_at_raw"),
        "posted_at": _parse_posted_at(card.get("posted_at_raw")),
        "urls": card.get("urls") or [],
        "parser_version": PARSER_VERSION,
    }
    return {"post": post, "relationship": rel, "mentions": mentions,
            "raw_card": raw_card, "eligible_for_forward": True}
