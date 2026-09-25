"""Caret/Mac expression boundary under the SHARED ORDINARY EXPRESSION SEAM.

History: 2026-09-21 a Caret reply carried a transcript of host/control state; a shape classifier
was rejected (it condemned legitimate parenthetical speech); a model-typed begin/end marker
protocol then failed ordinary turns closed; the 2026-09-22 JSON container that replaced it produced
demeanor descriptions instead of replies.  The established repair (conversation_direction SHARED
ORDINARY EXPRESSION SEAM) is structural, not a judgement about words: the expression pass is a plain
chat completion; host facts ride only in the system message; the human's exact words are the only
user-role text; no grammar and no voluntary markers.  Nothing here inspects what Clark says.
outward_expression remains a pure re-check (non-blank, no leaked retired sentinel).
"""
import json

import pytest

import caret_owner_status as cos
import conversation_projection as cp
import conversation_direction as cd
import native_provenance_writer as npw
import outward_expression as oe
from test_caret_correspondent_binding import MEMBER_AUTHOR, _enter_family_mode, _from, _map_member
from test_caret_inbound_seam import WORDS, _remote
from test_caret_wake_service import ROUTE, _count, _rows, _serve, _service, _setup

BEGIN = cd.PASS2_EXPRESSION_MARKER   # retired protocol strings: now only extraction-bug sentinels
END = cd.PASS2_COMPLETION_MARKER

SCAFFOLD_LINES = [
    "(Thinking to self: a paragraph of planning that is not addressed to anyone.)",
    "(Selecting act: some_act)",
    "(Direction: unknown)",
    "(Direction request: None)",
    "(Caret occasion: reply_through_caret)",
    "(Drafting response...)",
]
SCAFFOLD_ONLY = "\n\n".join(SCAFFOLD_LINES)
PLAIN = "Hello, it is good to hear from you (mostly because it was quiet today).\n\nHow are you?"

# Legitimate parenthetical outward expression. NONE of these are classified at all.
ASIDE_SOLE_WORD = "(laughs)"
ASIDE_SOLE_ADDRESSED = "(Yes, I can hear you.)"
ASIDE_WITH_COLON_QUOTE = "(He said: I'm leaving.)"
ASIDE_WITH_COLON_NOTE = "(Note: that was stranger than I expected.)"
MIXED_PROSE_AND_ASIDE = "Hello there.\n\n(laughs)\n\nGood to see you."

LEGITIMATE_EXPRESSIONS = [
    ASIDE_SOLE_WORD, ASIDE_SOLE_ADDRESSED, ASIDE_WITH_COLON_QUOTE, ASIDE_WITH_COLON_NOTE,
    MIXED_PROSE_AND_ASIDE, PLAIN,
]


# ------------------------------------------------------------- the predicate (pure)

def test_predicate_is_a_pure_marker_leak_recheck_never_content_classification():
    """outward_expression no longer inspects wording, punctuation, or bracket shape at all --
    it only re-verifies a value already extracted upstream is non-blank and carries no leaked
    envelope sentinel. Every legitimate expression, including ones a shape-based rule would
    have wrongly condemned, is judged identically: True."""
    for legit in LEGITIMATE_EXPRESSIONS + [SCAFFOLD_ONLY]:   # even scaffold WORDING, once extracted, is just text
        assert oe.is_conversational_expression(legit) and oe.is_outward_safe_expression(legit)
    assert not oe.is_conversational_expression("") and not oe.is_outward_safe_expression("   \n ")
    # the only thing this module still refuses: literal, un-stripped envelope markers -- proof
    # of an extraction bug, never something Clark chose to say.
    assert not oe.is_conversational_expression(f"{BEGIN}\nleaked begin marker")
    assert not oe.is_conversational_expression(f"leaked end marker\n{END}")


# ------------------------------------------------------- the seam boundary (pure)

@pytest.mark.parametrize("legit", LEGITIMATE_EXPRESSIONS + [SCAFFOLD_ONLY])
def test_every_completed_reply_is_carried_byte_exact_without_judgement(legit):
    """No wording, bracket or colon is ever inspected: a completed reply is the reply."""
    out, failure = cd.validate_plain_reply("\n" + legit + "\n")
    assert failure is None and out["expression"] == legit


def test_a_blank_completion_establishes_no_expression():
    assert cd.validate_plain_reply("  \n\t") == (None, cd.DirectionFailure.EMPTY_EXPRESSION)
    assert cd.validate_plain_reply(None) == (None, cd.DirectionFailure.MALFORMED_EXPRESSION)


def test_canonical_writer_refuses_an_empty_or_tampered_reply_body():
    """The canonical writer's own re-check (already-extracted text, envelope already consumed
    upstream): it refuses a blank body and a body that no longer matches Clark's validated
    words, and accepts real, already-extracted expression -- including a legitimate
    colon-bearing parenthetical."""
    with pytest.raises(npw.IncompleteEventBundleError):
        npw._validate_caret_reply_binding("reply_through_caret", "d", "", "", ["e"])
    with pytest.raises(npw.IncompleteEventBundleError):
        npw._validate_caret_reply_binding("reply_through_caret", "d", "tampered", PLAIN, ["e"])
    npw._validate_caret_reply_binding("reply_through_caret", "d", ASIDE_WITH_COLON_QUOTE, ASIDE_WITH_COLON_QUOTE, ["e"])


# ------------------------------------------------------------- live path, every principal

def _wire(monkeypatch, tmp_path, who):
    s = _setup(monkeypatch, tmp_path)
    if who == "owner":
        _serve(s, _remote())
    elif who == "owner_in_family":
        _enter_family_mode(s)
        _serve(s, _remote())
    else:
        member = _enter_family_mode(s)
        _map_member(s, member)
        _serve(s, _from(MEMBER_AUTHOR, "wife_handle", content="Hello Clark."))
    return s


def _outward(s):
    return _count(s, "SELECT COUNT(*) FROM events WHERE event_type='clark_outward_act'")


@pytest.mark.parametrize("who", ["owner", "owner_in_family", "member"])
def test_a_blank_completion_with_the_route_chosen_fails_closed_and_sends_nothing(monkeypatch, tmp_path, who):
    """Expression not established (mechanical): no X, no route binding, nothing sent -- never
    treated as Clark choosing silence (the occasion stays pending for its bounded retry)."""
    s = _wire(monkeypatch, tmp_path, who)
    s.pass2_raw = "   \n"
    s.pass1_script.append(dict(ROUTE))
    result = _service(s, tmp_path).step()
    assert result[0] != "delivered"
    assert s.fake.posts == [] and _outward(s) == 0
    assert _count(s, "SELECT COUNT(*) FROM event_components WHERE component_kind='discord_message_text'") == 0
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='waking_turn'") == 0


@pytest.mark.parametrize("who", ["owner", "owner_in_family", "member"])
def test_the_expression_pass_keeps_host_facts_out_of_the_user_role(monkeypatch, tmp_path, who):
    """The structural half of the boundary: on every principal's Caret path the ONLY user-role
    text of the expression pass is the correspondent's exact words; transport/provenance facts
    are system-role host framing; no grammar and no protocol marker shape the reply."""
    s = _wire(monkeypatch, tmp_path, who)
    s.pass1_script.append(dict(ROUTE))
    assert _service(s, tmp_path).step()[0] == "delivered"
    messages = s.pass2_prompts[-1]
    user_texts = [m["content"] for m in messages if m["role"] == "user"]
    assert user_texts[-1] in (WORDS, "Hello Clark.")
    assert all(BEGIN not in m["content"] and END not in m["content"] for m in messages)


@pytest.mark.parametrize("who", ["owner", "owner_in_family", "member"])
def test_route_intent_can_exist_without_a_send(monkeypatch, tmp_path, who):
    """Item 11: Clark's Pass-1 route choice is recorded even when a validly-enveloped, fully
    legitimate reply is too long to dispatch -- route intent without a send, entirely
    independent of any content judgement."""
    s = _wire(monkeypatch, tmp_path, who)
    s.pass2_text = "x" * 6001     # a real, completed reply -- simply over the transport cap
    s.pass1_script.append(dict(ROUTE))
    assert _service(s, tmp_path).step()[0] == "delivered"
    (turn,) = [r[0] for r in _rows(s, "SELECT event_id FROM events WHERE event_type='waking_turn'")]
    comps = dict(_rows(s, "SELECT component_kind, component_text FROM event_components WHERE event_id=?", turn))
    assert comps["caret_reply_route_intent"] == "reply_through_caret"        # the choice is recorded
    assert "discord_correspondence_request" not in comps and "discord_message_text" not in comps
    assert s.fake.posts == [] and _outward(s) == 0


@pytest.mark.parametrize("who", ["owner", "owner_in_family", "member"])
def test_valid_prose_is_sent_exactly_and_multipart_only_carries_validated_prose(monkeypatch, tmp_path, who):
    """Plain prose is sent exactly on every principal's Caret path, and multipart transport only
    ever carries a completed reply -- a blank completion is never split or sent."""
    s = _wire(monkeypatch, tmp_path, who)
    s.pass2_text = PLAIN
    s.pass1_script.append(dict(ROUTE))
    assert _service(s, tmp_path).step()[0] == "delivered"
    assert len(s.fake.posts) == 1 and json.loads(s.fake.posts[0][2]) == {"content": PLAIN}

    s2 = _wire(monkeypatch, tmp_path / "multi", who)
    long_body = ("Paragraph one. " * 90).strip() + "\n\n" + ("Paragraph two. " * 90).strip()
    s2.pass2_text = long_body
    s2.pass1_script.append(dict(ROUTE))
    assert _service(s2, tmp_path / "multi").step()[0] == "delivered"
    assert len(s2.fake.posts) >= 2
    sent = "".join(json.loads(p[2])["content"] for p in s2.fake.posts)
    assert sent.replace("\n", "").replace(" ", "") == long_body.replace("\n", "").replace(" ", "")

    s3 = _wire(monkeypatch, tmp_path / "multi_bad", who)
    s3.pass2_raw = "\n\n   "                         # blank completion: expression not established
    s3.pass1_script.append(dict(ROUTE))
    _service(s3, tmp_path / "multi_bad").step()
    assert s3.fake.posts == []


@pytest.mark.parametrize("who", ["owner", "owner_in_family", "member"])
@pytest.mark.parametrize("legit", LEGITIMATE_EXPRESSIONS)
def test_legitimate_parenthetical_expression_is_sent_exactly_on_every_caret_principal(monkeypatch, tmp_path, who, legit):
    """None of these legitimate bodies is judged, on any principal's Caret path."""
    s = _wire(monkeypatch, tmp_path, who)
    s.pass2_text = legit
    s.pass1_script.append(dict(ROUTE))
    assert _service(s, tmp_path).step()[0] == "delivered"
    assert len(s.fake.posts) == 1 and json.loads(s.fake.posts[0][2]) == {"content": legit}


def test_ordinary_mac_waking_expression_is_unchanged_for_real_prose(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    s.pass2_text = PLAIN
    reply = s.turn("Just chatting.")
    assert reply["reply"] == PLAIN


@pytest.mark.parametrize("legit", LEGITIMATE_EXPRESSIONS)
def test_ordinary_mac_waking_does_not_suppress_legitimate_parenthetical_expression(monkeypatch, tmp_path, legit):
    """The shared seam never swallows genuine parenthetical/colon-bearing speech on Mac."""
    s = _setup(monkeypatch, tmp_path)
    s.pass2_text = legit
    reply = s.turn("Just chatting.")
    assert reply["reply"] == legit
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='waking_turn'") == 1


def test_ordinary_mac_waking_also_fails_a_blank_completion_closed(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    s.pass2_raw = "  "
    with pytest.raises(Exception):
        s.turn("Just chatting.")
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='waking_turn'") == 0


def test_browser_projection_unaffected_by_the_repair(monkeypatch, tmp_path):
    """Item 12: browser projection behavior is unchanged by this repair either way -- a Caret
    occasion's prose still never projects into the ordinary Mac conversation view (existing,
    unrelated design rule), and it does not newly start projecting there just because the
    prose is now recognized as legitimate speech."""
    s = _wire(monkeypatch, tmp_path, "owner")
    s.pass2_text = MIXED_PROSE_AND_ASIDE
    s.pass1_script.append(dict(ROUTE))
    assert _service(s, tmp_path).step()[0] == "delivered"
    shown = "\n".join(r["content"] for r in cp.project_waking_conversation(s.h.db_path))
    assert "(laughs)" not in shown and "Hello there." not in shown

    # On the ordinary Mac path (not a Caret occasion), the same legitimate aside DOES project,
    # exactly as ordinary prose always has.
    s2 = _setup(monkeypatch, tmp_path / "mac")
    s2.pass2_text = MIXED_PROSE_AND_ASIDE
    s2.turn("Just chatting.")
    shown2 = "\n".join(r["content"] for r in cp.project_waking_conversation(s2.h.db_path))
    assert "(laughs)" in shown2 and "Hello there." in shown2
