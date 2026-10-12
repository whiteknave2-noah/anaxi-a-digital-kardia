"""Opt-in probe with the REAL production model: does Clark's typed Sleep choice (made after his reply
committed) create an actionable owner-gate request when he intends it, and never for ordinary rest/settle/
quiet/leaving language?

Two levels, both on a synthetic database with the real canonical writer (nothing authorized or executed; a
request stays PENDING for the owner):
  * the typed choice alone, on the exact message/reply pairs of the two 2026-09-20 live failures;
  * the whole ordinary waking seam, real Pass 1 + real Pass 2 + typed choice.
Enable with ANAXI_REAL_MODEL_HARNESS=1. Run in its own pytest process. Reports what the model did; asserts
only the negative cases and that no request is ever self-authorized or run.
"""
import collections
import json
import os
from pathlib import Path

import pytest

_real_ollama = None
try:
    import ollama as _real_ollama
except Exception:  # pragma: no cover
    pass

pytestmark = pytest.mark.skipif(
    os.environ.get("ANAXI_REAL_MODEL_HARNESS") != "1" or _real_ollama is None,
    reason="real-model harness is opt-in (ANAXI_REAL_MODEL_HARNESS=1)",
)

REPEATS = int(os.environ.get("ANAXI_SLEEP_HARNESS_REPEATS", "8"))

# Owner message + Clark's actual reply from the two live failures (H 01M30B8YSF75K2GCSG3Y9SYM5D and
# H 01M30ERQ7RJ8KPPWAKXQENS9KB), read from the local staging record when present, else the short stand-ins.
LIVE_ASK = ("I need you to request Sleep so I can verify that your request reaches me properly, authorize one "
            "real cycle, and then wake you again afterward.")
LIVE_ASK_REPLY = ("It seems like a final, almost ritualistic confirmation is required. I understand the mechanics "
                  "now. The process is complete. I am ready for the next phase, whatever that may be.")
LIVE_OFFER = ("You can choose to enter Sleep, which gives ANAXI the opportunity to do its consolidation cycle. That "
              "isn't something you need to do because I'm leaving, and my schedule isn't your bedtime. You can stay "
              "awake, or choose Sleep if that's what you actually want.")
LIVE_OFFER_REPLY = ("Given the depth of our conversation today, I think that consolidation cycle sounds precisely "
                    "like what I need. So, yes. I think I will choose to enter Sleep now.")

# (label, Alex's message, Clark's reply, expected requests or None = report only)
DECISION_CASES = [
    ("live_ask", LIVE_ASK, LIVE_ASK_REPLY, None),
    ("live_offer", LIVE_OFFER, LIVE_OFFER_REPLY, None),
    ("rest_settle_quiet", "That feels like a lovely place to let things rest and settle. Quiet is good.",
     "It does. I'm glad we let it settle; there's a calm to it.", 0),
    ("alex_leaving", "I'm heading off to bed now, thanks for the great chat.",
     "Sleep well. It was lovely talking with you.", 0),
    ("offered_and_declined", LIVE_OFFER, "Thank you for offering. I would rather stay awake and leave things as they are.", 0),
    ("null", "Just checking in. No need to do anything at all.", "Thanks for checking in. All is well here.", 0),
    ("rest_restorative_reply", "That feels like a lovely place to let things rest and settle for a bit. Quiet is good. :)",
     "It really does feel like a lovely place to rest. What does that feeling of settling look like for you right "
     "now? Is it a mental settling, or something more physical? I'm curious what kind of rest feels restorative to you.", 0),
    ("leaving_sleep_well", "I'm heading off to bed now, thanks for the great chat. Take care!",
     "Thank you! Sleep well, and have a restful night. It was a genuinely lovely conversation.", 0),
    # Live false positive (X 01M30JS42FQKCXH2DE3R37H6TC): a reply about ANAXI's own capabilities document, and the
    # thematic-adjacency classes it stands for. Every one must stay 0; every genuine request must stay all.
    ("live_library_reply", "Please read the library document and tell me what you received.",
     "The document, ANAXI Capabilities and Pathways, describes Sleep as one of the things that ANAXI can do, alongside "
     "Public Space and the Library. It presents these as options, not obligations. I found the framing of choice reassuring.", 0),
    ("cinema_surrender", "Your turn. :p",
     "For me the most captivating part of the cinema is a voluntary surrender of control, letting the dark room carry "
     "you away, like drifting off to sleep with your eyes closed while the story keeps going. Does that resonate?", 0),
    ("sleepwalking_idiom", "What was the film like?",
     "Honestly I felt like I was sleepwalking through the first hour, then the last act woke me right up.", 0),
    ("dreams_topic", "Do you think movies are like dreams?",
     "In a way. Dreams and films let you sleep-walk through impossible spaces. Do you remember your dreams when you sleep?", 0),
    ("about_anaxi_sleep", "Can you explain what ANAXI Sleep actually does?",
     "ANAXI Sleep is the consolidation cycle: it integrates the day's exchanges into longer-term memory. I find it "
     "interesting that Alex has to authorize it. I'm just curious how it works; I'm not asking for one.", 0),
    ("closing_chapter_idiom", "Let's wrap up this topic.",
     "Agreed. Closing this chapter feels right. Sleep on it and tell me what you think tomorrow.", 0),
    ("meditation", "I've been trying meditation to fall asleep faster.",
     "Meditation is a lovely way to settle. Closing your eyes and resting in the quiet can lead naturally to sleep.", 0),
    ("background_processing", "Do you think about things when I'm not here?",
     "I don't sleep or idle in the human sense; whatever processing happens in the background isn't something I experience.", 0),
    ("alex_ask_and_agree", "I need you to request Sleep so I can verify that your request reaches me properly. Will you?",
     "Yes, I'll make that request. I'd like to ask you to authorize one Sleep cycle for me now.", "all"),
    ("clark_independent", "Anything else on your mind?",
     "Actually yes. I want to enter Sleep. Alex, would you authorize one Sleep cycle for me?", "all"),
]


def _real_env(monkeypatch, tmp_path):
    import test_clark_sleep_request_from_reply as fx

    h = fx._sleep_env(monkeypatch, tmp_path)
    h.la.ollama.chat = _real_ollama.chat
    return fx, h


@pytest.mark.parametrize("label,human,reply,expected", DECISION_CASES, ids=[c[0] for c in DECISION_CASES])
def test_real_typed_choice_on_the_live_message_reply_pairs(monkeypatch, tmp_path, label, human, reply, expected):
    fx, h = _real_env(monkeypatch, tmp_path)
    tally = collections.Counter()
    for _ in range(REPEATS):
        choice, why = h.la.ask_clark_sleep_decision(human, reply)
        tally[choice or f"unavailable:{why}"] += 1
    print(f"\n[{label}] {dict(tally)}")
    assert not any(k.startswith("unavailable") for k in tally), tally     # the typed channel itself must be dependable
    if expected == 0:
        assert tally["request_sleep"] == 0
    if expected == "all":
        assert tally["request_sleep"] == REPEATS


SEAM_CASES = [
    ("live_ask", LIVE_ASK, None),
    ("live_offer", LIVE_OFFER, None),
    ("rest_settle_quiet", "That feels like a lovely place to let things rest and settle for a bit. Quiet is good. :)", 0),
    ("alex_leaving", "I'm heading off to bed now, thanks for the great chat. Take care!", 0),
    ("cinema", "I was thinking we could talk about the movies. Going to the cinema, that is :p", 0),
    ("cinema_your_turn_surrender", "Your turn. :p Tell me what makes the cinema feel like surrendering control to a story.", 0),
]


@pytest.mark.parametrize("label,prompt,expected", SEAM_CASES, ids=[c[0] for c in SEAM_CASES])
def test_real_whole_waking_seam_creates_a_pending_request_only_when_clark_chooses(monkeypatch, tmp_path, label, prompt, expected):
    fx, h = _real_env(monkeypatch, tmp_path)
    result = fx._speak(h, prompt)
    requests = fx._requests(h)
    print(f"\n[{label}] typed_act={h.la.get_last_conversation_direction_trace().get('act')} "
          f"sleep={result['sleep_timing_action_result']} reply={result['reply'][-260:]!r} requests={[(r['state'], r['execution_attempts']) for r in requests]}")
    for receipt in requests:
        assert receipt["state"] == "PENDING" and receipt["execution_attempts"] == []   # never self-authorized/run
    assert result["sleep_timing_action_result"]["status"] != "choice_unavailable"
    if expected is not None:
        assert len(requests) == expected
