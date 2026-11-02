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


def test_sanitizer_anchors_relative_x_links_to_absolute():
    # Real X DOM uses root-relative status links; they must survive as absolute so the parser
    # can read the canonical post id.
    clean = sanitize_card_html(
        '<article data-testid="tweet">'
        '<a href="/memekingdom/status/123?s=20" data-testid="User-Name">m</a></article>')
    assert 'href="https://x.com/memekingdom/status/123"' in clean
