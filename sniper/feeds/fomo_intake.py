"""Pure adaptation of public Fomo discovery rows; rendered coverage is partial."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from sniper.analysis.memecoin_x.normalize import (
    NETWORK_BY_CHAIN_SLUG,
    is_valid_network_address,
    normalize_address,
)

LIST_LABELS = {
    "trending24h": "En tendencia",
    "blue_chips": "Blue chips",
    "most_holders": "Con más holders",
    "graduated": "Graduados",
    "bonding": "Bonding",
}


def _parse_row(item):
    if not isinstance(item, dict):
        return None
    href = item.get("href")
    if not isinstance(href, str) or re.search(r"[\s?#]", href):
        return None
    try:
        route = urlsplit(href)
    except ValueError:
        return None
    if route.scheme or route.netloc:
        if route.scheme != "https" or route.netloc.lower() not in {
            "fomo.family", "fomo.family:443",
        }:
            return None
    elif not href.startswith("/") or href.startswith("//"):
        return None
    identity = re.fullmatch(r"/tokens/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)", route.path)
    if identity is None:
        return None
    chain, mint = identity.groups()
    network = NETWORK_BY_CHAIN_SLUG.get(chain.lower())
    if network is not None and not is_valid_network_address(mint, network):
        return None
    mint = normalize_address(mint, network)
    row_id = item.get("row_id")
    if row_id is not None:
        if not isinstance(row_id, str):
            return None
        identity = re.fullmatch(r"thesis-trigger-default-thesis-(.+):([0-9]+)", row_id)
        if identity is None:
            return None
        row_mint, row_chain = identity.groups()
        row_chain = "solana" if row_chain == "1399811149" else row_chain
        if (network is not None and row_chain != network) or normalize_address(row_mint, network) != mint:
            return None
    return {
        "network_id": network, "chain_raw": chain, "mint": mint, "venue": None,
        "raw": {"transport": "dom", "coverage": "partial", "item": item},
    }


def parse_dom_rows(rows):
    """Keep malformed row slots for writer accounting; never invent an empty poll."""
    if not isinstance(rows, list):
        raise ValueError("Fomo DOM rows must be a list")
    return [_parse_row(item) for item in rows]


def halt_reason(url, text, authenticated, status=None):
    """Classify observed page refusals without accepting exception text or credentials."""
    if status in {401, 403, 429}:
        return {401: "login_expired", 403: "http_403", 429: "http_429"}[status]
    try:
        route = urlsplit(url or "")
    except ValueError:
        return "unexpected_origin"
    if route.scheme != "https" or route.netloc.lower() not in {"fomo.family", "fomo.family:443"}:
        return "unexpected_origin"
    path = route.path.lower()
    content = (text or "").casefold()
    if any(marker in path for marker in ("/cdn-cgi/", "/challenge", "/captcha")) or any(
        marker in content for marker in (
            "captcha", "just a moment", "verify you are human", "checking your browser",
            "verifica que eres humano", "verifica que eres una persona",
            "comprobando tu navegador", "attention required", "cloudflare",
        )
    ):
        return "challenge"
    if authenticated is False or any(
        part in {"login", "signin", "sign-in"} for part in path.split("/")
    ) or any(marker in content for marker in (
        "iniciar sesión", "inicia sesión", "iniciar sesion", "inicia sesion", "sign in", "log in",
    )):
        return "login_required"
    if authenticated is not True:
        return "auth_unproven"
    return None
