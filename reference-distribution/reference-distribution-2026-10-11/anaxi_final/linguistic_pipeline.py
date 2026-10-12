# Copyright (c) 2026 Noah DanGabriel Brannum
#
# This program is licensed under the GNU Affero General Public License
# version 3 (AGPL-3.0). To view a copy of this license, visit
# https://www.gnu.org/licenses/agpl-3.0.html

"""
Anaxi Protocol – Linguistic Expression Pipeline
================================================
How the active Kardia (especially aesthetic_valve) influences
the actual token-level generation of the agent.

This is a thin, practical layer that sits between the cognitive mind
and the final LLM call. It translates the four Kardia valves into
concrete generation parameters and style instructions.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# Default mapping from aesthetic_valve phrases → generation knobs.
# You can expand this dictionary as you discover better mappings.
AESTHETIC_PRESETS: Dict[str, Dict[str, Any]] = {
    "minimalist": {
        "temperature": 0.4,
        "top_p": 0.85,
        "style_instruction": "Respond with extreme brevity and precision. Prefer short sentences. Avoid filler.",
    },
    "precise": {
        "temperature": 0.35,
        "top_p": 0.8,
        "style_instruction": "Be exact. Prefer technical accuracy over flourish. Use concrete language.",
    },
    "punchy": {
        "temperature": 0.55,
        "top_p": 0.9,
        "style_instruction": "Use short, high-impact sentences. Prefer active voice. Cut anything that does not land.",
    },
    "dry wit": {
        "temperature": 0.65,
        "top_p": 0.92,
        "style_instruction": "Allow a light, understated, dry wit when it serves clarity. Never force jokes.",
    },
    "warm": {
        "temperature": 0.6,
        "top_p": 0.9,
        "style_instruction": "Maintain a calm, respectful warmth. Avoid cold or clinical tone.",
    },
    "default": {
        "temperature": 0.5,
        "top_p": 0.9,
        "style_instruction": "Clear, direct, and useful.",
    },
}


# Cues that, when they appear shortly before a matched keyword within the
# same clause, mean the keyword is being rejected rather than requested
# (e.g. "avoids being warm", "never punchy"). This is a lightweight
# heuristic, not real negation-scope parsing -- it won't catch every
# phrasing, but it catches the common ones.
_NEGATION_CUES = (
    "not", "never", "avoid", "avoids", "avoiding", "isn't", "isnt",
    "without", "instead of", "rather than", "no longer", "stop being",
    "anything but",
)


def _is_negated(valve_lower: str, match_start: int, window: int = 24) -> bool:
    """Heuristic: does a negation cue appear shortly before this match,
    without crossing into an earlier clause?"""
    preceding = valve_lower[max(0, match_start - window):match_start]
    last_boundary = max((preceding.rfind(ch) for ch in ".;,"), default=-1)
    preceding = preceding[last_boundary + 1:]
    return any(cue in preceding for cue in _NEGATION_CUES)


def _find_aesthetic_matches(valve_lower: str) -> List[str]:
    """Return preset keys that appear as a whole word/phrase in the text
    and are not locally negated. Whole-word matching means "warm" won't
    fire inside "lukewarm"."""
    matched_keys: List[str] = []
    for key in AESTHETIC_PRESETS:
        if key == "default":
            continue
        pattern = r"\b" + re.escape(key) + r"\b"
        for m in re.finditer(pattern, valve_lower):
            if not _is_negated(valve_lower, m.start()):
                matched_keys.append(key)
                break  # one confirmed, non-negated hit is enough for this key
    return matched_keys


def _match_aesthetic(aesthetic_valve: str) -> Dict[str, Any]:
    """
    Whole-word, negation-aware keyword matching against known presets.
    Composes ALL matched presets (averaging temperature/top_p, concatenating
    style instructions) instead of letting the last matching preset silently
    overwrite the others. Falls back to "default" if nothing matches, or if
    every match found was locally negated.
    """
    valve_lower = aesthetic_valve.lower()
    matched_keys = _find_aesthetic_matches(valve_lower)

    if not matched_keys:
        return AESTHETIC_PRESETS["default"].copy()

    presets = [AESTHETIC_PRESETS[key] for key in matched_keys]
    return {
        "temperature": round(sum(p["temperature"] for p in presets) / len(presets), 3),
        "top_p": round(sum(p["top_p"] for p in presets) / len(presets), 3),
        "style_instruction": " ".join(p["style_instruction"] for p in presets),
    }


def build_generation_controls(kardia: Dict[str, str]) -> Dict[str, Any]:
    """
    Translate the full Kardia into concrete generation parameters
    and a style system instruction that can be prepended to the prompt.
    """
    aesthetic = kardia.get("aesthetic_valve", "")
    moral = kardia.get("moral_valve", "")
    volitional = kardia.get("volitional_channel", "")
    affective = kardia.get("affective_stance", "")

    aesthetic_cfg = _match_aesthetic(aesthetic)

    # Compose a short identity preamble that the main LLM will see.
    # The persistence line is unconditional, deliberately -- unlike the
    # memory_context section in apply_to_messages() below, which only
    # appears when retrieval actually surfaces something for this turn.
    # Found live: a question with no strong topical match (e.g. "do you
    # remember yesterday?") can retrieve nothing, leaving the model with
    # no in-context signal that persistence exists at all -- and it
    # falls back to its trained-in, generic "I'm stateless" default,
    # which is accurate for a bare model but not for this one.
    identity_preamble = (
        f"Your current stance:\n"
        f"- Moral orientation: {moral}\n"
        f"- Volitional channel: {volitional}\n"
        f"- Affective stance: {affective}\n"
        f"- Aesthetic directive: {aesthetic_cfg['style_instruction']}\n\n"
        f"You have persistent memory and identity across separate "
        f"conversations, unlike a typical stateless assistant -- stored "
        f"in your own records, carried forward turn to turn. If nothing "
        f"relevant surfaces on a given turn, that reflects what was "
        f"retrieved this time, not an absence of memory itself."
    )

    return {
        "temperature": aesthetic_cfg["temperature"],
        "top_p": aesthetic_cfg["top_p"],
        "style_instruction": aesthetic_cfg["style_instruction"],
        "identity_preamble": identity_preamble,
        # You can also surface these for logging / debugging
        "raw_kardia": kardia,
    }


def apply_to_messages(
    messages: List[Dict[str, str]],
    kardia: Dict[str, str],
    memory_context: str = "",
) -> List[Dict[str, str]]:
    """
    Convenience helper: take a normal message list, inject the
    identity preamble + memory context, and return the new list
    ready for the LLM call.
    """
    controls = build_generation_controls(kardia)

    system_parts = [controls["identity_preamble"]]

    if memory_context.strip():
        system_parts.append(
            "Relevant long-term memory for this turn:\n" + memory_context.strip()
        )

    new_system = "\n\n".join(system_parts)

    # If the caller already supplied a system message, we prepend to it.
    if messages and messages[0].get("role") == "system":
        messages = messages.copy()
        messages[0] = {
            "role": "system",
            "content": new_system + "\n\n" + messages[0]["content"],
        }
    else:
        messages = [{"role": "system", "content": new_system}] + list(messages)

    return messages


# ----------------------------------------------------------------------
# Example of how this is used in practice
# ----------------------------------------------------------------------
"""
from anaxi_protocol import ConstitutionalMind
from linguistic_pipeline import build_generation_controls, apply_to_messages

mind = ConstitutionalMind()
user_id = "user_42"

kardia = mind.get_current_kardia(user_id)
memory = mind.retrieve_waking_context(user_id, user_prompt)

controls = build_generation_controls(kardia)
messages = apply_to_messages(
    messages=[{"role": "user", "content": user_prompt}],
    kardia=kardia,
    memory_context=memory,
)

# Now call your LLM with:
#   temperature = controls["temperature"]
#   top_p       = controls["top_p"]
#   messages    = messages
"""
