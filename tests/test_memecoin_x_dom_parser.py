from pathlib import Path

from sniper.analysis.memecoin_x.dom_parser import parse_article_html, SELECTOR_VERSION
from sniper.analysis.memecoin_x.normalize import normalize_card, PARSER_VERSION

FIX = Path("tests/fixtures/memecoin_x/dom")


def test_localized_attribution_is_versioned_as_parser_v2():
    assert SELECTOR_VERSION == PARSER_VERSION == 2


def _batch(kind):
    return parse_article_html((FIX / f"{kind}.html").read_text())


def test_direct_one_self_authored_card():
    b = _batch("direct")
    assert b.selector_version == SELECTOR_VERSION
    assert len(b.cards) == 1
    c = b.cards[0]
    assert c["relationship"] == "direct"
    assert c["actor_handle"] == c["author_handle"] == "memekingdom"
    assert c["canonical_post_id"] == "2076720348171522273"
    assert c["full_text"].startswith("@artsch00lreject")
    assert c["text_complete"] is True
    assert c["url"] == "https://x.com/memekingdom/status/2076720348171522273"
    assert normalize_card(c)["eligible_for_forward"] is True


def test_repost_actor_is_reposter_author_is_original():
    b = _batch("repost")
    assert len(b.cards) == 1
    c = b.cards[0]
    assert c["relationship"] == "repost"
    assert c["actor_handle"] == "memekingdom"          # "KING reposted"
    assert c["author_handle"] == "AlteredHiz"          # the reposted author
    assert c["canonical_post_id"] == "2076734724178096374"
    assert c["text_complete"] is False                 # "Show more" => truncated
    assert "going to add my two cents" not in c["full_text"]   # nested quote not flattened
    assert normalize_card(c)["eligible_for_forward"] is True


def test_repost_attribution_does_not_depend_on_english_ui_text():
    html = (FIX / "repost.html").read_text().replace(" reposted", " republicó")
    batch = parse_article_html(html)
    assert len(batch.cards) == 1
    assert batch.cards[0]["relationship"] == "repost"
    assert batch.cards[0]["actor_handle"] == "memekingdom"
    assert batch.cards[0]["author_handle"] == "AlteredHiz"


def test_truncation_detection_does_not_depend_on_english_ui_text():
    html = (FIX / "repost.html").read_text().replace("Show more", "Mostrar más")
    batch = parse_article_html(html)
    assert len(batch.cards) == 1
    assert batch.cards[0]["text_complete"] is False
    assert all(not mention["eligible"] for mention in normalize_card(batch.cards[0])["mentions"])


def test_quote_outer_card_only_when_referenced_not_visible():
    b = _batch("quote")
    outer = [c for c in b.cards if c["relationship"] == "direct"]
    assert len(outer) == 1
    c = outer[0]
    assert c["actor_handle"] == c["author_handle"] == "thokani"
    assert c["canonical_post_id"] == "2075543724977721796"
    assert c["full_text"].startswith("Iq test passed")
    assert "my confluence on CASHCAT" not in c["full_text"]     # quoted text not flattened
    assert not [x for x in b.cards if x["relationship"] == "quote"]
    assert b.diagnostics == ("missing_referenced_post_id",)
    assert normalize_card(c)["eligible_for_forward"] is True


def test_quote_with_visible_status_emits_separate_referenced_relationship():
    b = _batch("quote_with_status")
    direct = [card for card in b.cards if card["relationship"] == "direct"]
    quoted = [card for card in b.cards if card["relationship"] == "quote"]
    assert len(direct) == 1 and len(quoted) == 1
    assert direct[0]["canonical_post_id"] == "3001"
    assert direct[0]["full_text"] == "Watching this source."
    assert quoted[0]["canonical_post_id"] == "2001"
    assert quoted[0]["author_handle"] == "poptyedev"
    assert quoted[0]["actor_handle"] == "zinceth"
    assert quoted[0]["referenced_post_id"] == "2001"
    assert quoted[0]["full_text"].startswith("Robinhood Chain CA:")
    assert b.diagnostics == ()


def test_reply_with_visible_parent_emits_separate_referenced_relationship():
    b = _batch("reply")
    direct = [card for card in b.cards if card["relationship"] == "direct"]
    replies = [card for card in b.cards if card["relationship"] == "reply"]
    assert len(direct) == 1 and len(replies) == 1
    assert direct[0]["canonical_post_id"] == "3002"
    assert direct[0]["full_text"] == "I agree with this call."
    assert replies[0]["canonical_post_id"] == "2002"
    assert replies[0]["author_handle"] == "poptyedev"
    assert replies[0]["actor_handle"] == "zinceth"
    assert replies[0]["referenced_post_id"] == "2002"
    assert replies[0]["full_text"].startswith("CA:")
    assert b.diagnostics == ()


def test_spanish_reply_context_is_not_misclassified_as_quote():
    html = (FIX / "reply.html").read_text().replace("Replying to", "En respuesta a")
    batch = parse_article_html(html)
    assert [card["relationship"] for card in batch.cards] == ["direct", "reply"]


def test_no_article_is_empty_with_diagnostic():
    b = parse_article_html("<div>nope</div>")
    assert b.cards == () and b.diagnostics == ("missing_article",)


def test_promoted_card_is_diagnostic_only_even_in_spanish():
    batch = parse_article_html('''
      <article data-testid="tweet">
        <div data-testid="placementTracking">Promocionado</div>
        <a href="https://x.com/poptyedev/status/9001">
          <time datetime="2026-07-14T00:00:00Z">ahora</time>
        </a>
        <div data-testid="tweetText">Robinhood Chain CA:
          0x1111111111111111111111111111111111111111</div>
      </article>
    ''')
    assert batch.cards == ()
    assert batch.diagnostics == ("promoted_card",)
