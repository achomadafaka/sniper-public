# tests/test_brain_graph_import_sources.py
from bot.brain_graph import import_sources as S

MEM = """---
name: feedback-paste-counts-never-retype-them-2026-09-28
description: Two report errors in one night came from reading a truncated line and retyping a count; paste numbers from tool output
metadata:
  type: feedback
  originSessionId: d933c54a-0557-416a-a15a-834f35eef5a7
  modified: 2026-09-28T05:22:14.767Z
---
Body.
**How to apply:**
- Paste every count in a report from the tool output. Never retype one.
- Never make a claim from a line that shows a cut or truncation mark.
"""


def test_a_memory_file_yields_its_headline_bullets_and_provenance():
    m = S.memory_source("feedback-paste-counts-never-retype-them-2026-09-28.md", MEM)
    assert m["kind"] == "rule" and m["type"] == "feedback"
    assert m["headline"].startswith("Two report errors in one night")
    assert m["bullets"] == ["Paste every count in a report from the tool output. Never retype one.",
                            "Never make a claim from a line that shows a cut or truncation mark."]
    assert m["session"] == "d933c54a-0557-416a-a15a-834f35eef5a7"
    assert m["date"] == "2026-09-28T05:22:14.767000+00:00" and m["marker"] is None


def test_the_name_date_is_the_fallback_and_a_retraction_is_marked():
    text = MEM.replace("  modified: 2026-09-28T05:22:14.767Z\n", "").replace(
        "description: Two", "description: RETRACTED 2026-09-29. Two")
    m = S.memory_source("finding-x-2026-09-20.md", text)
    assert m["date"] == "2026-09-20T00:00:00+00:00" and m["marker"] == "marked"
    assert S.memory_source("finding-y.md", MEM.replace("description: Two", "description: STALE: two"))["marker"] == "marked"
    live_rule = MEM.replace("description: Two", "description: RETRACTED FIX, do not move the lane. Two")
    assert S.memory_source("onchain-detections.md", live_rule)["marker"] == "marked"   # judged, never closed


def test_normalize_and_the_episode_key_are_stable():
    assert S.normalize("  Never   `git stash`\n the fix. ") == "Never `git stash` the fix."
    k = S.episode_key("memory/a.md", "body")
    assert k == S.episode_key("memory/a.md", "body") and k.startswith("memory/a.md#") and len(k) == len("memory/a.md#") + 12


def test_split_keeps_whole_sentences_and_the_full_text():
    long = " ".join(["Sentence number %03d is here." % i for i in range(40)])
    st, detail = S.split_statement(long)
    assert len(st) <= 600 and st.endswith(".") and detail == long
    assert S.split_statement("Short rule.") == ("Short rule.", None)
    st, _ = S.split_statement("x" * 700)
    assert len(st) <= 600 and st.endswith("...")


def test_doctrine_rules_carry_line_and_source():
    text = "# T\n\n- Never `git add -A` in this repo.\n- The sky is blue today.\n"
    rules = S.doctrine_rules(text, "CLAUDE.md")
    assert rules == [{"statement": "Never `git add -A` in this repo.", "line": 3, "source": "CLAUDE.md"}]


def test_verify_by_is_staggered_but_deterministic():
    a = S.verify_by("trap", "2026-09-01T00:00:00+00:00", "k1")
    assert a == S.verify_by("trap", "2026-09-01T00:00:00+00:00", "k1")
    assert "2026-10-01" <= a[:10] <= "2026-10-15"
    assert S.verify_by("settled", "2026-09-01T00:00:00+00:00", "k1")[:10] >= "2026-10-31"


def test_handoff_author_is_the_first_named_role():
    assert S.handoff_author("- 2026-09-28T13:02Z Beta: DARK WALLETS WRITTEN") == "beta"
    assert S.handoff_author("- 2026-09-28T13:50Z OPERATOR: (1) brain shape") == "operator"
    assert S.handoff_author("- 2026-09-28T13:50Z Codex: pushed") is None
    assert S.handoff_author("- 20:24Z Lambda (operator flagged the GRAMR block):") == "lambda"


def test_source_date_preserves_the_instant_in_non_utc_offsets():
    assert S._iso('2026-09-28T19:31:02-04:00') == '2026-09-28T23:31:02+00:00'


# The "How to apply" block of the real memory file
# feedback-a-display-thread-must-never-call-the-funded-jupiter-seam-2026-09-27.md, verbatim.
WRAPPED = """---
name: feedback-a-display-thread-must-never-call-the-funded-jupiter-seam-2026-09-27
description: A display thread must never call the funded Jupiter seam
metadata:
  type: feedback
---
**How to apply:**
- Before calling any `solana_real_seams` or Jupiter helper from a non-funded thread, grep it for
  `_jup_host_cooldown`, `JUPITER_LOAD`, `_sink_note` and `_funded_inflight`.
- Leave a test that a 429 and a 401 leave those byte-identical, proven red on the bad version.

SAME DAY, AGAIN (Beta, 2026-09-27 ~23:20Z): two SHADOW slot-lane legs were wired to
`pump_curve_buy._funded_jup_call`: the CPMM ZEC leg (f6c6652d, ~1.1 calls/s when on) and LaunchLab's
on-demand quote leg (45598cf9). Alpha's rule: a SHADOW path never calls the funded seam. Shadow buys
nothing in return for the 60 s cooldown risk. Fixed in 9b71ca05: the on-demand leg uses the paper
key through its own client (on a 429 or 401: skip, then back off 60 s, never fall back), and the ZEC
leg is gated off until the venue is live. A test patches `_funded_jup_call` to fail on every shadow
path.
- "Counted under slot_leg" is a label, not permission. Shadow, display and measurement are all
  non-funded.

Related: [[finding-a-rejected-key-falls-back-keyless-and-reads-as-rate-limiting-2026-09-24]],
[[finding-funded-jupiter-already-429-at-26-percent-2026-08-27]].
"""


def test_a_wrapped_bullet_keeps_its_continuation_lines():
    """Gamma trap f865b165: apply_rules kept only lines starting '- ', so 116 of 521 imported rules
    ended mid-sentence (1c318b8c 'grep it for'). A continuation line joins its open bullet; a blank
    line closes it, so the narrative paragraph after it is not glued onto a rule."""
    assert S.memory_source("x.md", WRAPPED)["bullets"] == [
        "Before calling any `solana_real_seams` or Jupiter helper from a non-funded thread, grep it for "
        "`_jup_host_cooldown`, `JUPITER_LOAD`, `_sink_note` and `_funded_inflight`.",
        "Leave a test that a 429 and a 401 leave those byte-identical, proven red on the bad version.",
        "\"Counted under slot_leg\" is a label, not permission. Shadow, display and measurement are all "
        "non-funded."]
