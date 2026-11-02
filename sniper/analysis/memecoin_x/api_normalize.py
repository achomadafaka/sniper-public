"""Normalize one X API Post envelope into a curated cashtag event."""
from __future__ import annotations

from typing import Any

from sniper.analysis.memecoin_x.normalize import extract_coin_mentions, normalize_handle


_REFERENCE_RELATIONSHIP = {
    "replied_to": "reply",
    "quoted": "quote",
    "retweeted": "repost",
}


def _cashtags(post: dict) -> list[str]:
    tags = []
    seen = set()
    for entity in ((post.get("entities") or {}).get("cashtags") or []):
        tag = str(entity.get("tag") or "").strip().upper()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def _urls(post: dict) -> list[str]:
    urls = []
    for entity in ((post.get("entities") or {}).get("urls") or []):
        value = entity.get("expanded_url") or entity.get("unwound_url") or entity.get("url")
        if value:
            urls.append(str(value))
    return urls


def _relationship(post: dict) -> tuple[str, str | None]:
    references = post.get("referenced_tweets") or []
    for relationship_type in ("retweeted", "quoted", "replied_to"):
        for reference in references:
            if reference.get("type") == relationship_type:
                return _REFERENCE_RELATIONSHIP[relationship_type], str(reference.get("id") or "") or None
    return "direct", None


def _exact_identity(text: str, urls: list[str]) -> tuple[str, str | None, str | None]:
    exact = {
        (str(row["network_id"]), str(row["mint"]))
        for row in extract_coin_mentions(text, urls)
        if row.get("eligible") and row.get("network_id") and row.get("mint")
    }
    if len(exact) == 1:
        network_id, mint = next(iter(exact))
        return "exact_coin", network_id, mint
    return "ticker_only", None, None


def normalize_api_payload(
    payload: dict[str, Any], *, roster_by_user_id: dict[str, dict], observed_at: str
) -> dict[str, Any] | None:
    """Return one eligible curated-roster cashtag event, else ``None``.

    Structured ``entities.cashtags`` are authoritative. Plain-text dollar
    patterns are never used to decide whether a Post is in scope.
    """
    post = payload.get("data") or {}
    actor_user_id = str(post.get("author_id") or "")
    roster_account = roster_by_user_id.get(actor_user_id)
    if not actor_user_id or roster_account is None:
        return None

    includes = payload.get("includes") or {}
    users = {str(row.get("id")): row for row in includes.get("users") or []}
    actor = users.get(actor_user_id) or {}
    actor_handle = normalize_handle(actor.get("username") or roster_account.get("handle"))
    post_id = str(post.get("id") or "")
    if not actor_handle or not post_id:
        return None

    relationship, referenced_post_id = _relationship(post)
    actor_tags = _cashtags(post)
    relevant_post = post
    if actor_tags:
        event_type = "cashtag_mention"
        cashtag_source = "actor_text"
        tags = actor_tags
    else:
        if relationship not in {"quote", "repost"} or not referenced_post_id:
            return None
        referenced = {
            str(row.get("id")): row for row in includes.get("tweets") or []
        }.get(referenced_post_id)
        if referenced is None:
            return None
        tags = _cashtags(referenced)
        if not tags:
            return None
        relevant_post = referenced
        event_type = "cashtag_amplification"
        cashtag_source = "referenced_post"

    text = str(relevant_post.get("text") or "")
    urls = _urls(relevant_post)
    identity_state, network_id, mint = _exact_identity(text, urls)
    edit_history = post.get("edit_history_tweet_ids") or []
    edit_root_id = str(edit_history[0]) if edit_history else post_id
    actor_text = str(post.get("text") or "")

    return {
        "event_type": event_type,
        "identity_state": identity_state,
        "network_id": network_id,
        "mint": mint,
        "ticker": tags[0],
        "cashtags": tags,
        "cashtag_source": cashtag_source,
        "relationship": relationship,
        "actor_user_id": actor_user_id,
        "actor_handle": actor_handle,
        "actor_role": roster_account.get("role"),
        "post_id": post_id,
        "edit_root_id": edit_root_id,
        "referenced_post_id": referenced_post_id,
        "post_url": f"https://x.com/{actor_handle}/status/{post_id}",
        "excerpt": actor_text[:280],
        "text": actor_text,
        "urls": _urls(post),
        "x_created_at": post.get("created_at"),
        "observed_at": observed_at,
        "public_metrics": dict(post.get("public_metrics") or {}),
        "matching_rules": list(payload.get("matching_rules") or []),
    }
