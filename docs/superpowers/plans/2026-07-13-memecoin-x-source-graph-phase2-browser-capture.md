# Memecoin X Source Graph Phase 2 Browser Capture Implementation Plan

> **Adapter status (2026-07-14):** Superseded for browser mechanics by
> `2026-07-14-memecoin-x-source-graph-hardening.md`. Playwright persistent-profile, CDP,
> extension, and `channel=chrome` attachment could not reliably reuse the authenticated burner
> session. The userscript plus loopback receiver now implements the same evidence, coverage,
> diagnostics, clock, and acceptance gates. This plan remains authoritative for those gates and
> for the required real 24-hour/manual audit.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture the burner's X Following feed through a dedicated Playwright profile, parse real direct/repost/quote/reply DOM fixtures, and persist bounded, clock-honest card observations for a 24-hour read-only dry run.

**Architecture:** A one-time headed login creates an isolated persistent browser profile outside Git. A fixture tool captures sanitized public card fragments without touching Sniper's DB; a pure DOM parser is built against those fixtures. A bounded browser adapter returns facts and coverage diagnostics, while a separate orchestration service takes one corrected-clock snapshot per run and writes only the `memecoin_x_*` tables.

**Tech Stack:** Python 3, Playwright 1.58/Chromium, Beautiful Soup 4.14, SQLite, pytest.

## Global Constraints

- X API integration is prohibited.
- The profile directory must be explicit, absolute, outside the repository, access-restricted, and owned by one process.
- Login is manual and headed. Never automate credentials, two-factor prompts, challenges, or rate-limit recovery.
- Browser actions are limited to `goto`, `reload`, DOM reads, and bounded scroll. Never follow, like, repost, reply, message, compose, or submit forms.
- Fixture capture writes sanitized files only. It does not write a Sniper DB.
- Parser eligibility comes from real captured DOM plus strict Phase-1 normalization. No address reconstruction, OCR, URL following, or later-buy inference.
- One accepted clock snapshot is reused for the entire capture run. Unhealthy clock data is explicitly `legacy_unknown` and cannot become forward evidence.
- Robinhood is network `4663`, never a pooled generic-EVM bucket.
- Phase 2 emits no source score, notification, LLM job, thesis, trade, sizing, veto, or funded-runtime change.
- Every task uses TDD and one logical commit. Do not push, merge, restart services, or touch production DBs in this plan.

---

## File map

- `requirements.txt`: declare the existing Playwright/Beautiful Soup runtime dependencies.
- `sniper/analysis/memecoin_x/dom_fixture.py`: sanitize a single captured public card fragment and reject secret-bearing output.
- `scripts/capture_memecoin_x_fixtures.py`: manual headed login and interactive real-card fixture capture through the dedicated profile.
- `tests/fixtures/memecoin_x/dom/`: four sanitized real DOM fixtures plus public metadata sidecars.
- `sniper/analysis/memecoin_x/dom_parser.py`: pure fixture-driven card parser; no browser, DB, Fomo, or execution imports.
- `sniper/analysis/memecoin_x/browser_adapter.py`: Following-feed guard and bounded visible-card/scroll collection.
- `sniper/analysis/memecoin_x/capture_service.py`: clock snapshot, parse/normalize/persist orchestration.
- `sniper/persistence/db_memecoin_x.py`: capture-run lifecycle, diagnostics, and sentinel queries.
- `scripts/run_memecoin_x_capture.py`: profile-locked `login`, `once`, and bounded `loop` commands.
- `scripts/report_memecoin_x_capture.py`: read-only 24-hour capture report and acceptance checks.
- `tests/test_memecoin_x_{dom_fixture,dom_parser,browser_adapter,capture_service,capture_cli,capture_report}.py`: focused Phase-2 coverage.

---

### Task 1: Capture sanitized real DOM fixtures through the dedicated profile

**Files:**
- Modify: `requirements.txt`
- Create: `sniper/analysis/memecoin_x/dom_fixture.py`
- Create: `scripts/capture_memecoin_x_fixtures.py`
- Create: `tests/test_memecoin_x_dom_fixture.py`
- Create: `tests/fixtures/memecoin_x/dom/direct.html`
- Create: `tests/fixtures/memecoin_x/dom/repost.html`
- Create: `tests/fixtures/memecoin_x/dom/quote.html`
- Create: `tests/fixtures/memecoin_x/dom/reply.html`
- Create: `tests/fixtures/memecoin_x/dom/{direct,repost,quote,reply}.json`

**Interfaces:**
- Produces: `sanitize_card_html(raw_html: str) -> str`
- Produces: `validate_fixture_html(html: str) -> tuple[str, ...]`
- Produces CLI: `capture_memecoin_x_fixtures.py login|capture`
- Fixture sidecar keys: `schema_version`, `kind`, `captured_at`, `source_post_id`,
  `expected_relationship`, `referenced_post_visible`, `reviewed_public_only`

- [ ] **Step 1: Declare exact browser/parser dependencies**

Append under a new browser-capture comment in `requirements.txt`:

```text
# Read-only X source-graph browser capture
playwright==1.58.0
beautifulsoup4==4.14.3
```

- [ ] **Step 2: Write failing sanitizer tests**

Create `tests/test_memecoin_x_dom_fixture.py`:

```python
from sniper.analysis.memecoin_x.dom_fixture import sanitize_card_html, validate_fixture_html


def test_sanitizer_keeps_public_card_landmarks_and_drops_active_content():
    raw = '''<article data-testid="tweet" class="volatile" style="x">
      <script>localStorage.getItem("auth_token")</script>
      <a href="https://x.com/alice/status/123?utm_source=x#frag" data-testid="User-Name">Alice</a>
      <div data-testid="tweetText" lang="en">CA: 0x1111111111111111111111111111111111111111</div>
      <time datetime="2026-07-13T20:00:00.000Z">now</time>
      <input value="secret">
    </article>'''
    clean = sanitize_card_html(raw)
    assert 'data-testid="tweet"' in clean
    assert 'data-testid="tweetText"' in clean
    assert 'datetime="2026-07-13T20:00:00.000Z"' in clean
    assert 'href="https://x.com/alice/status/123"' in clean
    assert "script" not in clean and "input" not in clean
    assert "class=" not in clean and "style=" not in clean
    assert validate_fixture_html(clean) == ()


def test_validator_rejects_session_and_authorization_material():
    assert "secret_material" in validate_fixture_html(
        '<article data-testid="tweet" authorization="Bearer abc">x</article>')


def test_sanitizer_drops_non_http_and_tracking_query_links():
    clean = sanitize_card_html(
        '<article data-testid="tweet"><a href="javascript:alert(1)">x</a>'
        '<a href="https://example.com/token?a=1#x">token</a></article>')
    assert "javascript:" not in clean
    assert 'href="https://example.com/token"' in clean
```

- [ ] **Step 3: Run the tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_x_dom_fixture.py -q
```

Expected: collection fails because `sniper.analysis.memecoin_x.dom_fixture` does not exist.

- [ ] **Step 4: Implement the sanitizer**

Create `sniper/analysis/memecoin_x/dom_fixture.py` with these controls:

```python
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

DROP_TAGS = ("script", "style", "form", "input", "textarea", "iframe", "object", "embed")
KEEP_ATTRS = {"aria-label", "data-testid", "datetime", "dir", "href", "lang", "role"}
FORBIDDEN_ATTRS = {"authorization", "cookie", "set-cookie", "storage_state", "auth_token"}
SECRET_VALUE_MARKERS = ("bearer ", "auth_token=", "sessionid=", "ct0=")


def _safe_href(value: str) -> str | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
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


def validate_fixture_html(html: str) -> tuple[str, ...]:
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
```

Do not weaken the secret check to make a real fixture pass. Reduce the captured fragment instead.

- [ ] **Step 5: Implement manual login and interactive capture**

Create `scripts/capture_memecoin_x_fixtures.py` with two subcommands:

```text
login   --profile-dir ABSOLUTE_DIR
capture --profile-dir ABSOLUTE_DIR --kind {direct,repost,quote,reply} --out-dir tests/fixtures/memecoin_x/dom
```

`--out-dir` defaults exactly to `tests/fixtures/memecoin_x/dom`; an explicit path must still
resolve inside `tests/fixtures/memecoin_x/dom` so fixture capture cannot overwrite arbitrary files.

Required behavior:

```python
PROFILE_ENV = "MEMECOIN_X_PROFILE_DIR"
KINDS = ("direct", "repost", "quote", "reply")
HOME = "https://x.com/home"

# login:
# - require an absolute profile path outside REPO_ROOT
# - mkdir(mode=0o700), then chmod(0o700)
# - launch chromium.launch_persistent_context(profile, headless=False)
# - goto HOME and wait for the operator to press Enter after login + Following selection
# - never call fill(), type(), press(), click(), or form submission

# capture:
# - launch the same profile headed
# - require page URL under x.com/home and the selected tab text to be Following
# - enumerate visible article[data-testid="tweet"] cards in the terminal using index,
#   public author text, status ID, and a 120-character text preview
# - ask the operator for one numeric index
# - ask whether the quoted/replied source post ID is visibly present in that captured article;
#   store the answer as referenced_post_visible rather than inferring it
# - call locator.nth(index).evaluate("node => node.outerHTML")
# - sanitize_card_html before any disk write
# - write kind.html and the seven-key JSON sidecar atomically; expected_relationship equals kind
#   and reviewed_public_only must be explicitly confirmed by the operator
# - perform no DB access and no X interaction beyond goto/DOM reads/bounded wheel scroll
```

Import Playwright inside `main()` so pure sanitizer tests do not require a browser process.

- [ ] **Step 6: Verify sanitizer GREEN and install Chromium**

Run:

```bash
python3 -m pytest tests/test_memecoin_x_dom_fixture.py -q
python3 -m playwright install chromium
```

Expected: sanitizer tests pass; Chromium installation exits 0.

- [ ] **Step 7: Create the dedicated profile and capture four real fixtures**

Run from the worktree:

```bash
export MEMECOIN_X_PROFILE_DIR="$HOME/.local/state/sniper/x-source-graph-profile"
python3 scripts/capture_memecoin_x_fixtures.py login --profile-dir "$MEMECOIN_X_PROFILE_DIR"
python3 scripts/capture_memecoin_x_fixtures.py capture --profile-dir "$MEMECOIN_X_PROFILE_DIR" --kind direct
python3 scripts/capture_memecoin_x_fixtures.py capture --profile-dir "$MEMECOIN_X_PROFILE_DIR" --kind repost
python3 scripts/capture_memecoin_x_fixtures.py capture --profile-dir "$MEMECOIN_X_PROFILE_DIR" --kind quote
python3 scripts/capture_memecoin_x_fixtures.py capture --profile-dir "$MEMECOIN_X_PROFILE_DIR" --kind reply
```

Do not proceed with a synthetic substitute. If a relationship type is absent from the current Following feed, leave the task open and retry a later bounded pass.

- [ ] **Step 8: Review the fixtures before commit**

Run:

```bash
for f in tests/fixtures/memecoin_x/dom/*.html; do
  python3 -c 'import pathlib,sys; from sniper.analysis.memecoin_x.dom_fixture import validate_fixture_html; p=pathlib.Path(sys.argv[1]); assert validate_fixture_html(p.read_text()) == (), p' "$f"
done
rg -n -i '<script|<input|<form| authorization=| cookie=| set-cookie=| storage_state=' tests/fixtures/memecoin_x/dom
```

Expected: validation exits 0; `rg` returns no matches. Manually confirm the four sidecars contain public post IDs only and `reviewed_public_only=true`.

- [ ] **Step 9: Commit Task 1**

```bash
git add requirements.txt sniper/analysis/memecoin_x/dom_fixture.py scripts/capture_memecoin_x_fixtures.py tests/test_memecoin_x_dom_fixture.py tests/fixtures/memecoin_x/dom
git commit -m "test(memecoin-x): capture sanitized real X card fixtures"
```

---

### Task 2: Build the pure DOM parser against the real fixtures

**Files:**
- Create: `sniper/analysis/memecoin_x/dom_parser.py`
- Create: `tests/test_memecoin_x_dom_parser.py`

**Interfaces:**
- Produces: `ParseBatch(cards: tuple[dict, ...], diagnostics: tuple[str, ...], selector_version: int)`
- Produces: `parse_article_html(html: str) -> ParseBatch`
- Each card dict is accepted directly by `normalize_card(card)`.

**Relationship contract:**
- Direct article: one direct card, actor equals canonical author.
- Repost article: one source-post card, actor is reposter, author is original author, relationship is `repost`.
- Quote article: emit an outer direct card for authored outer text and, when the nested source ID/text is visible, one referenced-source card with relationship `quote`.
- Reply article: emit an outer direct card for authored reply text and, when the parent ID/text is visible, one referenced-source card with relationship `reply`.
- Never flatten nested quoted/replied text into the outer author's `full_text`.

- [ ] **Step 1: Write fixture-driven failing tests**

Create `tests/test_memecoin_x_dom_parser.py`:

```python
from pathlib import Path
import json

import pytest

from sniper.analysis.memecoin_x.dom_parser import parse_article_html
from sniper.analysis.memecoin_x.normalize import normalize_card

FIXTURES = Path("tests/fixtures/memecoin_x/dom")


@pytest.mark.parametrize("kind", ["direct", "repost", "quote", "reply"])
def test_real_fixture_produces_only_normalizer_compatible_cards(kind):
    batch = parse_article_html((FIXTURES / f"{kind}.html").read_text())
    meta = json.loads((FIXTURES / f"{kind}.json").read_text())
    assert meta["expected_relationship"] == kind
    assert meta["reviewed_public_only"] is True
    assert batch.selector_version == 1
    assert batch.cards
    if meta["referenced_post_visible"]:
        assert not batch.diagnostics
    else:
        assert set(batch.diagnostics) <= {"missing_referenced_post_id"}
    for card in batch.cards:
        assert card["canonical_post_id"].isdigit()
        assert card["author_handle"]
        assert card["actor_handle"]
        assert card["relationship"] in {"direct", "repost", "quote", "reply"}
        assert card["url"].endswith(card["canonical_post_id"])
        assert normalize_card(card)["eligible_for_forward"] is True


def test_direct_actor_is_author():
    batch = parse_article_html((FIXTURES / "direct.html").read_text())
    assert len(batch.cards) == 1
    assert batch.cards[0]["relationship"] == "direct"
    assert batch.cards[0]["actor_handle"] == batch.cards[0]["author_handle"]


def test_repost_preserves_actor_and_original_author():
    card = parse_article_html((FIXTURES / "repost.html").read_text()).cards[0]
    assert card["relationship"] == "repost"
    assert card["actor_handle"] != card["author_handle"]


@pytest.mark.parametrize("kind,relationship", [("quote", "quote"), ("reply", "reply")])
def test_nested_relationship_never_flattens_source_text(kind, relationship):
    batch = parse_article_html((FIXTURES / f"{kind}.html").read_text())
    meta = json.loads((FIXTURES / f"{kind}.json").read_text())
    cards = batch.cards
    outer = [c for c in cards if c["relationship"] == "direct"]
    nested = [c for c in cards if c["relationship"] == relationship]
    assert outer
    if meta["referenced_post_visible"]:
        assert nested
        assert outer[0]["canonical_post_id"] != nested[0]["canonical_post_id"]
        assert outer[0]["full_text"] != nested[0]["full_text"]
    else:
        assert not nested
        assert "missing_referenced_post_id" in batch.diagnostics


def test_missing_status_id_is_diagnostic_only():
    batch = parse_article_html('<article data-testid="tweet"><div data-testid="tweetText">x</div></article>')
    assert batch.cards == ()
    assert "missing_post_id" in batch.diagnostics
```

Add synthetic boundary fixtures proving:

- an image/video card with no `tweetText` emits `media_without_text_identity` and no invented CA;
- zero-width, homoglyph, or spaced address-like text emits `obfuscated_identity`, preserves the
  original text, and produces no eligible normalized mention;
- a promoted label emits `promoted_card`; its author cannot be promoted to Tier-1 by parsing;
- a captured "Show more" marker sets `text_complete=False`;
- conflicting/nested post roots never leak one root's URLs or text into the other root;
- a shortened external URL is retained as text and never resolved by the parser.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_x_dom_parser.py -q
```

Expected: import failure because `dom_parser.py` does not exist.

- [ ] **Step 3: Inspect the four real fixture landmarks**

```bash
rg -n 'data-testid="(tweet|tweetText|User-Name|socialContext)"|/status/|<time|aria-label=' tests/fixtures/memecoin_x/dom/*.html
```

Use only landmarks present in the real fixtures. Stable v1 primitives are status links, `time[datetime]`, `data-testid="tweetText"`, `data-testid="User-Name"`, and captured social-context text. Do not add an unproven fallback selector.

- [ ] **Step 4: Implement the pure parser**

Create `sniper/analysis/memecoin_x/dom_parser.py` with:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

SELECTOR_VERSION = 1
STATUS_RE = re.compile(r"^/([^/]+)/status/(\d+)$")


@dataclass(frozen=True)
class ParseBatch:
    cards: tuple[dict, ...]
    diagnostics: tuple[str, ...]
    selector_version: int = SELECTOR_VERSION


def _status_fact(anchor: Tag) -> tuple[str, str, str] | None:
    href = str(anchor.get("href") or "")
    path = urlsplit(urljoin("https://x.com", href)).path
    match = STATUS_RE.fullmatch(path)
    if not match:
        return None
    handle, post_id = match.groups()
    return handle, post_id, f"https://x.com/{handle}/status/{post_id}"


def _public_urls(root: Tag) -> list[str]:
    out = []
    for anchor in root.select("a[href]"):
        href = str(anchor.get("href") or "")
        value = urljoin("https://x.com", href)
        parts = urlsplit(value)
        if parts.scheme in {"http", "https"} and parts.netloc:
            clean = f"{parts.scheme}://{parts.netloc.lower()}{parts.path}"
            if clean not in out:
                out.append(clean)
    return out


```

Implement `parse_article_html` directly from the Task-1 fixture landmarks. It selects exactly one
`article[data-testid="tweet"]`; absence returns `ParseBatch((), ("missing_article",))`. Split outer
and nested status/text roots before collecting text, return tuples, and deduplicate diagnostics in
first-seen order. Never call `get_text()` on the whole article. Apply these hard gates:

- canonical ID must come from that post root's own `/status/<digits>` link;
- `full_text` comes only from that root's `tweetText`, excluding nested roots;
- posted time comes only from that root's `time[datetime]`;
- actor for repost comes only from captured social-context identity;
- missing actor/author/post ID emits a diagnostic and no eligible card;
- `text_complete=False` if the fixture contains a captured "Show more"/truncation marker;
- URLs come from `_public_urls(root)` and are data only;
- return the exact Phase-1 card keys, including empty `referenced_*` fields when absent.

Do not alter `normalize_card` to accommodate a weak parse. The DOM parser must satisfy its contract.

- [ ] **Step 5: Run parser and Phase-1 regression tests**

```bash
python3 -m pytest tests/test_memecoin_x_dom_parser.py tests/test_memecoin_x_normalize.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add sniper/analysis/memecoin_x/dom_parser.py tests/test_memecoin_x_dom_parser.py
git commit -m "feat(memecoin-x): parse real Following-feed card fixtures"
```

---

### Task 3: Implement the guarded bounded-pass browser adapter

**Files:**
- Create: `sniper/analysis/memecoin_x/browser_adapter.py`
- Create: `tests/test_memecoin_x_browser_adapter.py`

**Interfaces:**
- Produces: `BrowserPass(status, articles, cards_seen, scrolls, prior_sentinel, newest_sentinel, sentinel_found, diagnostics)`
- Produces: `collect_bounded_pass(page, *, prior_sentinel, max_cards=50, max_scrolls=3) -> BrowserPass`
- Status values: `ok`, `no_new`, `coverage_gap`, `wrong_feed`, `unauthenticated`, `challenge`, `rate_limited`, `parser_error`

- [ ] **Step 1: Write failing bounded-pass tests with a fake page**

Test these exact behaviors in `tests/test_memecoin_x_browser_adapter.py`:

- `wrong_feed_returns_no_articles`: selected tab `For you` returns `wrong_feed`, zero articles,
  and zero scroll calls.
- `login_challenge_and_rate_limit_fail_closed`: each page-state fixture returns its matching
  operator status, zero articles, and zero scroll calls.
- `stops_when_prior_sentinel_is_seen`: the second batch contains the prior post ID; status is
  `ok`, `sentinel_found=True`, and no third scroll occurs.
- `card_limit_before_sentinel_is_coverage_gap`: 51 unique articles with limit 50 return exactly
  50 articles and `coverage_gap`.
- `scroll_limit_before_sentinel_is_coverage_gap`: three unsuccessful scrolls return
  `coverage_gap` and `scrolls == 3`.
- `duplicate_visible_articles_are_returned_once`: duplicate canonical IDs yield one article.
- `no_new_when_newest_equals_prior_sentinel`: status is `no_new` and `sentinel_found=True`.
- `all_unparseable_articles_is_parser_error`: status is `parser_error`, zero eligible articles,
  and a structural diagnostic is retained.

The fake page exposes only `url`, `selected_tab_text()`, `page_state_text()`, `visible_article_html()`, and `bounded_scroll()`. This keeps tests independent of Playwright internals.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_x_browser_adapter.py -q
```

Expected: import failure because the adapter does not exist.

- [ ] **Step 3: Implement adapter types and pure pass loop**

Use this boundary in `browser_adapter.py`:

```python
@dataclass(frozen=True)
class BrowserPass:
    status: str
    articles: tuple[str, ...]
    cards_seen: int
    scrolls: int
    prior_sentinel: str | None
    newest_sentinel: str | None
    sentinel_found: bool | None
    diagnostics: tuple[str, ...]


class FeedPage(Protocol):
    url: str
    def selected_tab_text(self) -> str: raise NotImplementedError
    def page_state_text(self) -> str: raise NotImplementedError
    def visible_article_html(self) -> list[str]: raise NotImplementedError
    def bounded_scroll(self) -> None: raise NotImplementedError
```

`collect_bounded_pass` parses each article with `parse_article_html` only to obtain canonical IDs and structural health. It must gather the complete pass before any persistence. Stop at the prior sentinel or configured limits. If every visible article is structurally unparseable, return `parser_error` and no articles.

For quote/reply cards the sentinel is the outer displayed post ID, never the nested referenced
post ID. For a repost it is the displayed original/source post ID.

Add a thin `PlaywrightFeedPage` wrapper whose allowed browser operations are locator reads, `evaluate("node => node.outerHTML")`, and one fixed-height `mouse.wheel(0, 900)` per bounded scroll. It must not expose click/fill/type/press/evaluate-JavaScript outside the fixed outerHTML read.

- [ ] **Step 4: Run focused tests**

```bash
python3 -m pytest tests/test_memecoin_x_browser_adapter.py tests/test_memecoin_x_dom_parser.py -q
```

Expected: all pass.

- [ ] **Step 5: Add a static read-only API guard**

Add a test that reads `browser_adapter.py` and fails if it contains `.click(`, `.fill(`, `.type(`, `.press(`, `keyboard.`, `request.`, or `expect_download`. This is defense-in-depth; headed manual login remains isolated in the fixture/CLI command.

- [ ] **Step 6: Commit Task 3**

```bash
git add sniper/analysis/memecoin_x/browser_adapter.py tests/test_memecoin_x_browser_adapter.py
git commit -m "feat(memecoin-x): bound read-only Following-feed passes"
```

---

### Task 4: Wire card observations, run lifecycle, and diagnostics

**Files:**
- Modify: `sniper/persistence/db_memecoin_x.py`
- Modify: `sniper/analysis/memecoin_x/clock_health.py`
- Modify: `sniper/analysis/memecoin_x/normalize.py`
- Create: `sniper/analysis/memecoin_x/capture_service.py`
- Modify: `tests/test_memecoin_x_store.py`
- Modify: `tests/test_memecoin_x_clock_health.py`
- Modify: `tests/test_memecoin_x_normalize.py`
- Create: `tests/test_memecoin_x_capture_service.py`
- Modify: `index/schema_registry.json` via generator
- Modify: `index/dependency_index.json` via generator

**Interfaces:**
- Produces: `capture_clock_snapshot(path=None, now=None) -> CaptureClockSnapshot`
- Produces store functions: `start_capture_run`, `finish_capture_run`, `record_capture_diagnostic`, `latest_capture_sentinel`
- Produces: `run_capture_once(conn, collect_fn, *, clock_fn, run_id, parser_version) -> CaptureSummary`
- `CaptureSummary` fields: `run_id`, `status`, `cards_seen`, `new_posts`,
  `new_relationships`, `new_observations`, `parse_failures`, `newest_sentinel`,
  `sentinel_found`, `clock_domain`, `diagnostic_codes`

- [ ] **Step 1: Write failing schema/lifecycle tests**

Add tests proving:

- `capture_diagnostics_table_is_created`: `PRAGMA table_info` matches the DDL below and the
  run/code index exists.
- `start_then_finish_capture_run_is_crash_auditable`: start commits `status='running'`; finish
  updates the same row to its terminal status without inserting another row.
- `latest_sentinel_uses_only_completed_non_error_run`: later `running`/`error` rows are ignored;
  the newest completed `ok`/`no_new` sentinel is returned.
- `diagnostic_row_keeps_code_and_sanitized_card_json`: known code inserts JSON; unknown code raises
  `ValueError`; no HTML/profile material is stored.

Required Phase-2 table:

```sql
CREATE TABLE IF NOT EXISTS memecoin_x_capture_diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_run_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    clock_domain TEXT NOT NULL DEFAULT 'legacy_unknown',
    diagnostic_code TEXT NOT NULL,
    card_index INTEGER,
    parser_version INTEGER NOT NULL,
    canonical_post_id TEXT,
    article_fingerprint TEXT,
    details_json TEXT,
    raw_card_json TEXT
);
```

Index `(capture_run_id, diagnostic_code)`. Keep diagnostic codes in a Python allowlist and reject unknown codes before SQL.

The exact v1 allowlist is:

```python
CAPTURE_DIAGNOSTIC_CODES = frozenset({
    "missing_article", "missing_post_id", "missing_author", "missing_actor",
    "missing_referenced_post_id", "invalid_relationship", "incomplete_text",
    "media_without_text_identity", "obfuscated_identity", "promoted_card",
    "wrong_feed", "unauthenticated", "challenge", "rate_limited", "parser_error",
    "coverage_gap", "profile_locked", "network_error", "capture_error",
})
```

- [ ] **Step 2: Write failing clock snapshot tests**

Add tests for:

- `healthy_capture_snapshot_uses_corrected_time_and_real_domain`: accepted offset produces the
  corrected ISO timestamp and `observed_real_utc`.
- `unhealthy_capture_snapshot_keeps_host_time_but_labels_legacy_unknown`: missing/stale offset
  produces host ISO plus `legacy_unknown` and the exact reason.
- `capture_service_calls_clock_once_per_run`: an injected counter remains exactly one after all
  cards persist.

The unhealthy timestamp is diagnostic host UTC only. Its explicit `legacy_unknown` domain makes it ineligible for Phase-3 forward correlation.

- [ ] **Step 3: Write failing orchestration tests**

Create `tests/test_memecoin_x_capture_service.py` proving:

- a successful pass starts the run before collection and finishes it after persistence;
- direct/repost/quote/reply facts reach `memecoin_x_card_observations` with the run's exact observation timestamp;
- one article producing outer+nested cards creates separate observations without text flattening;
- wrong-feed/challenge/parser-error passes write run diagnostics but no forward posts/mentions;
- a coverage-gap pass may persist successfully parsed visible cards but the run remains `coverage_gap`;
- an incomplete canonical card persists its observation/relationship and diagnostic, but all
  extracted mentions are ineligible; a later complete delivery may promote the same exact mention
  only with the later `source_observation_id`;
- a later incomplete delivery cannot demote or retimestamp earlier complete mention evidence;
- invalid card identity creates a diagnostic row and no canonical observation;
- an unhealthy clock persists observations as `legacy_unknown` and Phase-1 mention identity is unchanged;
- replaying the same run ID is idempotent or rejected visibly, never duplicated.

- [ ] **Step 4: Run RED**

```bash
python3 -m pytest tests/test_memecoin_x_store.py tests/test_memecoin_x_clock_health.py tests/test_memecoin_x_normalize.py tests/test_memecoin_x_capture_service.py -q
```

Expected: failures for missing table/functions/service.

- [ ] **Step 5: Gate incomplete-text mentions**

Add a normalization regression: an otherwise valid card with `text_complete=False` and an exact
Robinhood `4663` CA retains the mention but sets `eligible=False`. Add store regressions proving a
later complete same-parser observation promotes the exact mention and moves
`source_observation_id` to that later observation, while a later incomplete observation cannot
change earlier complete evidence.

Implement the smallest change in `normalize_card` and `persist_card`. Do not make the whole card
ineligible because the immutable delivery and relationship still need to be recorded.

In `normalize_card`, copy each extracted mention and force `eligible=False` when
`text_complete` is false. In `persist_card`, after the existing NULL-safe identity insert, update
an existing same-parser mention only when the incoming post is complete, the incoming mention is
eligible, and the stored mention is ineligible. That update sets `eligible=1`, the incoming
ambiguity value, and the later `source_observation_id`. Never update an already eligible mention
from an incomplete or later duplicate delivery.

- [ ] **Step 6: Implement store lifecycle and diagnostics**

Add the diagnostics table/index in `ensure_memecoin_x_tables`. Implement these exact signatures:

- `start_capture_run(conn, *, run_id, parser_version, started_at, clock_snapshot,
  prior_sentinel)`: insert and commit one `status='running'` row.
- `finish_capture_run(conn, *, run_id, finished_at, status, cards_seen, new_posts,
  new_relationships, parse_failures, newest_sentinel, sentinel_found, error_detail=None)`: update
  exactly one running row, require `rowcount == 1`, then commit.
- `record_capture_diagnostic(conn, *, capture_run_id, observed_at, clock_domain,
  diagnostic_code, parser_version, card_index=None, canonical_post_id=None,
  article_fingerprint=None, details=None, raw_card=None)`: validate the code, serialize mappings
  with sorted JSON keys, insert, and commit the diagnostic row.
- `latest_capture_sentinel(conn)`: return `newest_sentinel` from the latest finished `ok` or
  `no_new` run; return `None` when none exists.

Preserve `record_capture_run` for Phase-1 compatibility. Do not overload `error_detail` with raw fixture HTML.

- [ ] **Step 7: Implement one-read clock snapshot**

Add to `clock_health.py`:

```python
@dataclass(frozen=True)
class CaptureClockSnapshot:
    observed_at: str
    clock_domain: str
    healthy: bool
    offset_s: float | None
    offset_age_s: float | None
    reason: str


def capture_clock_snapshot(path=None, now=None) -> CaptureClockSnapshot:
    host_now = time.time() if now is None else float(now)
    observed, health = x_observed_at_or_none(path=path, now=host_now)
    if observed is not None:
        return CaptureClockSnapshot(observed, "observed_real_utc", True,
                                    health["offset_s"], health["offset_age_s"], "ok")
    host_iso = datetime.fromtimestamp(host_now, tz=timezone.utc).isoformat()
    return CaptureClockSnapshot(host_iso, "legacy_unknown", False,
                                health.get("offset_s"), health.get("offset_age_s"),
                                health["reason"])
```

- [ ] **Step 8: Implement capture orchestration**

`capture_service.py` must:

1. take one clock snapshot;
2. start and commit a `running` capture run;
3. call injected `collect_fn(prior_sentinel)` once;
4. reject guard/parser failure before eligible persistence;
5. parse every returned article, normalize each emitted card, and call `persist_card` with the same `observed_at`, `clock_domain`, and run ID;
6. record diagnostic-only cards and article fingerprints without raw HTML;
7. finish the run with exact counts/status;
8. on exception, finish `error`, record a bounded exception class/message, then re-raise.

No Fomo, Telegram, LLM, route, strategy, or execution imports are allowed.

- [ ] **Step 9: Run GREEN and regenerate indexes**

```bash
python3 -m pytest tests/test_memecoin_x_store.py tests/test_memecoin_x_clock_health.py tests/test_memecoin_x_normalize.py tests/test_memecoin_x_capture_service.py -q
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
python3 scripts/brain_index.py schema memecoin_x_capture_diagnostics
```

Expected: tests pass, no schema violations, and the diagnostics table/index are shown.

- [ ] **Step 10: Commit Task 4**

```bash
git add sniper/persistence/db_memecoin_x.py sniper/analysis/memecoin_x/clock_health.py sniper/analysis/memecoin_x/normalize.py sniper/analysis/memecoin_x/capture_service.py tests/test_memecoin_x_store.py tests/test_memecoin_x_clock_health.py tests/test_memecoin_x_normalize.py tests/test_memecoin_x_capture_service.py index/schema_registry.json index/dependency_index.json
git commit -m "feat(memecoin-x): persist bounded browser capture runs"
```

---

### Task 5: Add the profile-locked capture runner

**Files:**
- Create: `scripts/run_memecoin_x_capture.py`
- Create: `tests/test_memecoin_x_capture_cli.py`

**Interfaces:**
- CLI: `login`, `once`, `loop`
- `once`: exactly one bounded pass
- `loop`: randomized 120-240 second interval, optional `--duration-hours`, clean stop
- Exit codes: `0` completed/no-new, `2` operator action required, `3` coverage/parser degradation, `4` profile locked/config invalid

- [ ] **Step 1: Write failing CLI/config/lock tests**

Cover these exact assertions:

- relative, repository-contained, and absent profile paths raise `ValueError` before Playwright;
- a second nonblocking lock attempt returns `profile_locked` while the first owns the file;
- `login` passes `headless=False` and never invokes the injected DB opener;
- `once` opens one persistent context and invokes one pass;
- 1,000 injected jitter draws all fall in inclusive `[120, 240]`;
- elapsed duration prevents an extra collection call at or after the deadline;
- login/challenge/rate-limit exits after one pass with no relaunch;
- source scan finds no funded execution, strategy, Telegram, LLM, `.click(`, `.fill(`, `.type(`,
  `.press(`, `keyboard.`, or `request.` use.

Inject Playwright/context/sleep/random functions in tests; do not launch a real browser in pytest.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_x_capture_cli.py -q
```

Expected: import/path failure because the runner does not exist.

- [ ] **Step 3: Implement configuration and profile lock**

Use:

```python
PROFILE_ENV = "MEMECOIN_X_PROFILE_DIR"
DEFAULT_MIN_INTERVAL_S = 120
DEFAULT_MAX_INTERVAL_S = 240
DEFAULT_MAX_CARDS = 50
DEFAULT_MAX_SCROLLS = 3
```

Resolve the profile path and reject relative paths or any path under `REPO_ROOT`. Create with mode `0700`. Acquire nonblocking `fcntl.flock` on a sibling `.x-source-graph-profile.lock` before launching Chromium and hold it for the process lifetime.

`login` launches headed, opens `https://x.com/home`, and waits for operator Enter. It performs no DB open and no automated login interaction. `once` and `loop` are also headed in Phase 2; do not add a headless variation before the dry-run gate.

Use `time.monotonic()` for loop deadlines and sleep scheduling. Wall-clock correction is only for
captured evidence timestamps.

`once`/`loop` use `sniper.persistence.db_core.get_connection(args.db)` and call `ensure_memecoin_x_tables` plus `run_capture_once`. They may call `page.goto`, `page.reload`, locator reads, and bounded scroll only.

- [ ] **Step 4: Add fail-closed browser state handling**

On `unauthenticated`, `challenge`, `rate_limited`, or `profile_locked`, exit once with code 2/4. Do not relaunch. A clean unexpected browser close may receive one relaunch per process, but any second close exits with a recorded gap.

- [ ] **Step 5: Run CLI and full Phase-2 focused tests**

```bash
python3 -m pytest tests/test_memecoin_x_dom_fixture.py tests/test_memecoin_x_dom_parser.py tests/test_memecoin_x_browser_adapter.py tests/test_memecoin_x_capture_service.py tests/test_memecoin_x_capture_cli.py -q
```

Expected: all pass without opening Chromium.

- [ ] **Step 6: Run one headed read-only pass against a disposable X-only DB**

```bash
export MEMECOIN_X_PROFILE_DIR="$HOME/.local/state/sniper/x-source-graph-profile"
export MEMECOIN_X_DRY_DB="$HOME/.local/state/sniper/memecoin_x_capture_dry.sqlite"
python3 scripts/run_memecoin_x_capture.py once --profile-dir "$MEMECOIN_X_PROFILE_DIR" --db "$MEMECOIN_X_DRY_DB"
```

Inspect only the X tables:

```bash
sqlite3 "$MEMECOIN_X_DRY_DB" "SELECT run_id,status,cards_seen,new_posts,new_relationships,parse_failures,sentinel_found FROM memecoin_x_capture_runs ORDER BY id DESC LIMIT 1;"
sqlite3 "$MEMECOIN_X_DRY_DB" "SELECT relationship,COUNT(*) FROM memecoin_x_card_observations GROUP BY relationship;"
```

Expected: one finished run; no non-`memecoin_x_*` table was created by this command.

- [ ] **Step 7: Commit Task 5**

```bash
git add scripts/run_memecoin_x_capture.py tests/test_memecoin_x_capture_cli.py
git commit -m "feat(memecoin-x): run profile-locked read-only capture"
```

---

### Task 6: Add the 24-hour dry-run report and execute the gate

**Files:**
- Create: `scripts/report_memecoin_x_capture.py`
- Create: `tests/test_memecoin_x_capture_report.py`
- Create after run: `docs/audits/2026-07-14-memecoin-x-phase2-dry-run.md`

**Interfaces:**
- CLI: `report_memecoin_x_capture.py --db PATH --since ISO --json`
- Read-only report keys: `wall_hours`, `runs`, `status_counts`, `cards_seen`, `observations`, `relationships`, `parse_failure_rate`, `healthy_clock_rate`, `sentinel_found_rate`, `coverage_gaps`, `coverage_gap_rate`, `max_gap_seconds`, `profile_locks`, `diagnostic_counts`, `secret_scan_hits`, `forbidden_source_hits`, `acceptance`

- [ ] **Step 1: Write failing report tests**

Create fixture SQLite rows and prove:

- monkeypatched `get_readonly_connection` is called and no table count changes;
- rates use finished runs, ignore `running`, and return `None` rather than divide by zero;
- gaps/profile locks appear in their own counts and do not become zero-activity evidence;
- forbidden JSON keys such as `cookies`, `storage_state`, `authorization`, `headers`, or
  `profile_dir` increment `secret_scan_hits`, while the same words inside public `full_text` do not;
- acceptance is false at 23:59:59, 89 runs, 94.99% healthy clock, 5.01% coverage gaps,
  a 961-second maximum gap, one profile lock, 1.01% parse failures, one secret hit, or one
  forbidden source hit, and true exactly at every inclusive boundary.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_x_capture_report.py -q
```

Expected: import/path failure because the report does not exist.

- [ ] **Step 3: Implement the read-only report**

Use `get_readonly_connection(args.db)`. Never create/ensure tables in the reporter. Report `insufficient_activity` rather than failure when the feed contains too few public cards to audit. The automated acceptance gate is:

- at least 24 wall-clock hours between first start and last finish;
- at least 90 finished runs (supersedes the pre-v1.5 300-run target);
- at least 95% of finished runs on `observed_real_utc`;
- coverage-gap/degraded runs no more than 5% of finished runs;
- no unobserved interval longer than 960 seconds and zero profile-lock events;
- parse failures no more than 1% of cards seen;
- zero secret scan hits;
- zero forbidden browser actions/imports;
- every gap, cutoff, operator state, and profile lock counted explicitly.

Sentinel rate and observation volume are reported, not hard-coded as quality proof.

- [ ] **Step 4: Run report tests and all Phase-1/2 tests**

```bash
python3 -m pytest tests/test_memecoin_x_store.py tests/test_memecoin_x_normalize.py tests/test_memecoin_x_clock_health.py tests/test_memecoin_x_roster.py tests/test_memecoin_x_dom_fixture.py tests/test_memecoin_x_dom_parser.py tests/test_memecoin_x_browser_adapter.py tests/test_memecoin_x_capture_service.py tests/test_memecoin_x_capture_cli.py tests/test_memecoin_x_capture_report.py tests/test_brain_index.py -q
python3 scripts/validate_schema_refs.py
git diff --check
```

Expected: all pass, no schema violations, no whitespace errors.

- [ ] **Step 5: Commit report code before the live dry run**

```bash
git add scripts/report_memecoin_x_capture.py tests/test_memecoin_x_capture_report.py
git commit -m "feat(memecoin-x): report browser capture coverage"
```

- [ ] **Step 6: Start the bounded 24-hour read-only run**

Use the external profile and disposable DB. Keep notifications, LLM, Fomo correlation, and all execution modules disabled:

```bash
export MEMECOIN_X_PROFILE_DIR="$HOME/.local/state/sniper/x-source-graph-profile"
export MEMECOIN_X_DRY_DB="$HOME/.local/state/sniper/memecoin_x_capture_dry.sqlite"
python3 scripts/run_memecoin_x_capture.py loop --profile-dir "$MEMECOIN_X_PROFILE_DIR" --db "$MEMECOIN_X_DRY_DB" --duration-hours 24 --min-interval 120 --max-interval 240
```

If X shows a login/challenge/rate-limit state, let the command stop. Record the gap, recover manually in headed login mode, and start a new bounded run; never describe the combined period as gap-free.

- [ ] **Step 7: Generate and manually audit the dry-run report**

```bash
SINCE=$(sqlite3 "$MEMECOIN_X_DRY_DB" "SELECT MIN(started_at) FROM memecoin_x_capture_runs;")
python3 scripts/report_memecoin_x_capture.py --db "$MEMECOIN_X_DRY_DB" --since "$SINCE" --json
```

Manually compare at least 25 stored observations against the visible X cards, covering every relationship type present. Verify actor, author, canonical post ID, referenced identity, text boundaries, URLs, `observed_at`, clock domain, and Robinhood `4663` where present. Fixture agreement alone is not enough.

- [ ] **Step 8: Record the audit without copying private browser data**

Create `docs/audits/2026-07-14-memecoin-x-phase2-dry-run.md` containing:

- exact branch SHA and run window;
- report JSON totals, gap intervals, and acceptance result;
- number and relationship distribution of manually checked observations;
- each mismatch by observation ID and parser version;
- explicit statement that no browser actions, notifications, LLM calls, Fomo joins, or trading effects occurred;
- whether Phase 3 is blocked or allowed to enter planning.

Do not include cookies, screenshots, raw profile paths, authorization data, or non-public account data.

- [ ] **Step 9: Commit the audit only after verification**

```bash
git add docs/audits/2026-07-14-memecoin-x-phase2-dry-run.md
git commit -m "docs(memecoin-x): record Phase 2 browser dry run"
```

---

## Edge-case coverage map

| Authoritative spec register | Phase-2 disposition |
|---|---|
| §11.1 capture/profile | Tasks 1, 3, and 5 cover secrets, profile ownership, wrong feed, stale/reordered cards, bounded cutoffs, challenge/operator states, partial DOM, duplicate/overlapping passes, network/crash handling, and immutable edits. |
| §11.2 extraction/identity | Task 2 plus existing `test_memecoin_x_normalize.py` cover media-only, obfuscation, strict addresses, ticker/bare-EVM ambiguity, conflicting chains, pair URLs, nested authors, multiple identities, Robinhood `4663`, invalid Unicode/address boundaries, and later complete observations. |
| §11.3 correlation/causality | Phase 2 preserves exact observation/network/role inputs only. Forward linking, causal lanes, gap-aware denominators, stable link identity, and six-hour measurement tests remain blocked for the Phase-3 plan. |
| §11.4 alerts/LLM | Phase 2 emits neither. Stable alert watermarks, async job ordering, injection, hallucination, budgets, provider failure, stale jobs, and model/version gates remain blocked for the Phase-4 plan. |
| §11.5 adversarial/operations | Tasks 1-6 cover malicious links as inert text, identity anomaly diagnostics, DB/clock/scheduler failure visibility, and no silent persistence. Token-safety interpretation and coordinated-source reporting remain later display-only work. |

---

## Phase-2 completion gate

Phase 2 is complete only when:

1. all four committed fixtures came from the dedicated profile's real Following feed and passed secret review;
2. parser tests prove direct/repost/quote/reply attribution without nested-text flattening;
3. browser code passes the read-only static guard and bounded-pass failure fixtures;
4. capture writes only `memecoin_x_*` tables using one clock snapshot per run;
5. the 24-hour report satisfies every automated gate; any named blocking reason leaves Phase 2
   incomplete;
6. the manual 25-observation audit has no unresolved identity/timestamp mismatch; and
7. no notification, LLM, correlation, execution, funded-runtime, or production-DB behavior was enabled.

Phase 3 remains blocked on its separate forward-link stable-key migration and correlator plan, even after Phase 2 passes.
