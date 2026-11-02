"""Pure DOM parser: one sanitized X card fragment -> ParseBatch of card-fact dicts.

No browser, DB, Fomo, or execution imports. Each emitted card is accepted directly by
normalize_card(). Attribution comes only from the captured DOM — never inferred:

- direct : one card, actor == canonical author.
- repost : one source-post card; actor = reposter (socialContext link), author = original.
- quote  : outer authored card (direct); plus a referenced-source card (relationship 'quote')
           ONLY when the quoted tweet's id is present in the DOM; else a diagnostic.
- reply  : outer authored card (direct); referenced parent card when the parent id is visible.

Nested quoted/replied text is never flattened into the outer author's full_text.
"""
from __future__ import annotations

import re
from collections import namedtuple

from bs4 import BeautifulSoup

SELECTOR_VERSION = 2

ParseBatch = namedtuple("ParseBatch", ["cards", "diagnostics", "selector_version"])

_STATUS_RE = re.compile(r"https?://x\.com/([A-Za-z0-9_]{1,15})/status/(\d+)")
_PROFILE_RE = re.compile(r"https?://x\.com/([A-Za-z0-9_]{1,15})$")
_REPOST_CONTEXT_RE = re.compile(
    r"\b(?:reposted|republic[oó]|repost[eé]|repostou|hat repostet)\b", re.I)
_REPLY_CONTEXT_RE = re.compile(
    r"(?:\breplying to\b|\ben respuesta a\b|\ben réponse à\b|"
    r"\bem resposta a\b|\bantwort an\b|\bin risposta a\b|返信先|답글 대상)", re.I)


def _in_quotebox(node) -> bool:
    return node.find_parent(
        lambda t: t.name == "div" and t.get("role") == "link") is not None


def _card(pid, author, actor, relationship, full_text, text_complete, posted_at, urls,
          ref_author=None, ref_id=None):
    return {
        "canonical_post_id": pid,
        "url": f"https://x.com/{author}/status/{pid}",
        "author_handle": author,
        "actor_handle": actor,
        "relationship": relationship,
        "full_text": full_text,
        "text_complete": text_complete,
        "posted_at_raw": posted_at,
        "urls": urls,
        "referenced_author": ref_author,
        "referenced_post_id": ref_id,
    }


def _text_complete(text_node) -> bool:
    if text_node is None:
        return True
    scope = text_node.parent or text_node
    if scope.select_one('[data-testid="tweet-text-show-more-link"]') is not None:
        return False
    return not re.search(r"\b(?:show more|mostrar más)\b", scope.get_text(), re.I)


def _referenced_card(text_node, *, actor, relationship):
    if text_node is None:
        return None
    parent = text_node.find_parent(
        lambda tag: tag.name == "div" and tag.get("role") == "link")
    anchor = next((a for a in parent.select('a[href*="/status/"]')), None) if parent else None
    match = _STATUS_RE.search(anchor.get("href", "")) if anchor else None
    if not match:
        return None
    author, post_id = match.groups()
    time_el = parent.find("time") if parent else None
    posted_at = time_el.get("datetime") if time_el else None
    urls = []
    for link in parent.select("a[href]") if parent else []:
        href = link.get("href", "")
        if (href.startswith("http") and "x.com" not in href and "twitter.com" not in href
                and href not in urls):
            urls.append(href)
    return _card(
        post_id, author, actor, relationship, text_node.get_text().strip(),
        _text_complete(text_node), posted_at, urls, ref_author=author, ref_id=post_id)


def parse_article_html(html: str) -> ParseBatch:
    soup = BeautifulSoup(html or "", "html.parser")
    art = soup.select_one('article[data-testid="tweet"]')
    diags = []
    if art is None:
        return ParseBatch((), ("missing_article",), SELECTOR_VERSION)
    if art.select_one('[data-testid="placementTracking"]') is not None:
        return ParseBatch((), ("promoted_card",), SELECTOR_VERSION)

    # canonical author + id: the permalink anchor that wraps the <time>
    tanchor = next((a for a in art.select('a[href*="/status/"]') if a.find("time")), None)
    m = _STATUS_RE.search(tanchor.get("href", "")) if tanchor else None
    if not m:
        return ParseBatch((), ("missing_post_id",), SELECTOR_VERSION)
    author, pid = m.group(1), m.group(2)
    time_el = art.find("time")
    posted_at = time_el.get("datetime") if time_el else None

    # outer text = first tweetText NOT inside a quote box (never flatten the quoted body)
    outer_tt = [tt for tt in art.select('[data-testid="tweetText"]') if not _in_quotebox(tt)]
    full_text = outer_tt[0].get_text().strip() if outer_tt else ""
    text_complete = _text_complete(outer_tt[0] if outer_tt else None)

    urls = []
    for a in art.select("a[href]"):
        href = a.get("href", "")
        if (href.startswith("http") and "x.com" not in href and "twitter.com" not in href
                and not _in_quotebox(a) and href not in urls):
            urls.append(href)

    sc = art.select_one('[data-testid="socialContext"]')
    sc_anchor = sc.find_parent("a", href=True) if sc else None
    sc_profile = _PROFILE_RE.match(sc_anchor.get("href", "")) if sc_anchor else None
    reposter = sc_profile.group(1) if sc_profile else None
    repost_label = bool(sc and _REPOST_CONTEXT_RE.search(sc.get_text()))
    is_repost = bool(sc and reposter and (
        reposter.lower() != author.lower() or repost_label))
    is_reply = any(
        _REPLY_CONTEXT_RE.search(str(node)) and not _in_quotebox(node.parent)
        for node in art.find_all(string=_REPLY_CONTEXT_RE)
    )
    qbox_tt = next((tt for tt in art.select('[data-testid="tweetText"]')
                    if _in_quotebox(tt)), None)

    cards = []
    if repost_label and not reposter:
        return ParseBatch((), ("missing_actor",), SELECTOR_VERSION)
    if is_repost:
        cards.append(_card(pid, author, reposter, "repost", full_text, text_complete,
                           posted_at, urls, ref_author=author, ref_id=pid))
        if qbox_tt:
            diags.append("missing_referenced_post_id")
    else:
        # direct / quote / reply all begin with the outer authored card (direct)
        cards.append(_card(pid, author, author, "direct", full_text, text_complete,
                           posted_at, urls))
        if is_reply:
            referenced = _referenced_card(qbox_tt, actor=author, relationship="reply")
            if referenced:
                cards.append(referenced)
            else:
                diags.append("missing_referenced_post_id")
        elif qbox_tt:
            referenced = _referenced_card(qbox_tt, actor=author, relationship="quote")
            if referenced:
                cards.append(referenced)
            else:
                diags.append("missing_referenced_post_id")

    return ParseBatch(tuple(cards), tuple(diags), SELECTOR_VERSION)
