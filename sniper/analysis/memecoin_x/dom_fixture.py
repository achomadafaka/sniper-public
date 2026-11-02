"""Sanitize a single captured public X card fragment into a safe, stable test fixture.

Keeps only public structural landmarks (data-testid, datetime, href, aria/role/lang/dir) and
drops all active content and any session/authorization material. Fixtures are public DOM only —
never cookies, tokens, or storage state. Do not weaken the secret check to make a real fixture
pass; reduce the captured fragment instead.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

DROP_TAGS = ("script", "style", "form", "input", "textarea", "iframe", "object", "embed")
KEEP_ATTRS = {"aria-label", "data-testid", "datetime", "dir", "href", "lang", "role"}
FORBIDDEN_ATTRS = {"authorization", "cookie", "set-cookie", "storage_state", "auth_token"}
SECRET_VALUE_MARKERS = ("bearer ", "auth_token=", "sessionid=", "ct0=")


def _safe_href(value: str):
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    # X serves internal links as root-relative ("/user/status/123"). Anchor them to x.com so
    # the canonical post id survives sanitization; drop query/fragment. Non-root/relative or
    # non-http schemes (javascript:, //host, "foo") are dropped.
    if not parts.scheme and not parts.netloc:
        return "https://x.com" + parts.path if parts.path.startswith("/") else None
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, "", ""))


def sanitize_card_html(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")
    for tag in list(soup.find_all(DROP_TAGS)):
        tag.decompose()
    for tag in soup.find_all(True):
        kept = {}
        for key, value in list(tag.attrs.items()):
            if key not in KEEP_ATTRS:
                continue
            rendered = " ".join(value) if isinstance(value, list) else str(value)
            if key == "href":
                rendered = _safe_href(rendered)
                if rendered is None:
                    continue
            kept[key] = rendered
        tag.attrs = kept
    clean = str(soup)
    errors = validate_fixture_html(clean)
    if errors:
        raise ValueError(",".join(errors))
    return clean


def validate_fixture_html(html: str) -> tuple:
    soup = BeautifulSoup(html or "", "html.parser")
    errors = []
    for tag in soup.find_all(True):
        for key, value in tag.attrs.items():
            rendered = " ".join(value) if isinstance(value, list) else str(value)
            if key.lower() in FORBIDDEN_ATTRS or any(
                    marker in rendered.lower() for marker in SECRET_VALUE_MARKERS):
                errors.append("secret_material")
    if soup.select_one('article[data-testid="tweet"]') is None:
        errors.append("not_tweet_article")
    return tuple(dict.fromkeys(errors))
