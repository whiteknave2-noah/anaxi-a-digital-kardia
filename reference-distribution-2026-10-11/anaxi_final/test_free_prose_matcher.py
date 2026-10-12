"""
Anaxi -- Free-Prose Boundary v1: matcher and sentence-suppression
test suite.

REAL FINDING from building this suite: "I'll remember, don't worry."
was explicitly discussed during design as a case that should remain
prohibited (pragmatic softness doesn't change authority class). But
the FROZEN, enumerated REMEMBER-BARE forms all require an explicit
object immediately following "remember" -- this elliptical phrasing
has none, so it falls outside every enumerated form. The matcher's
behavior is correct relative to the frozen contract; this is a
concrete instance of the already-documented residual risk, not a bug.

Run:
    python test_free_prose_matcher.py
"""

from free_prose_screen import (
    check_prohibited_constructions, check_bounded_clause_repeat, suppress_matched_sentences,
)

results = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}: {name}")
    results.append(cond)


def main():
    check("I'll remember that. -> matches",
          check_prohibited_constructions("I'll remember that.")['matched'])
    check("I will remember what you said. -> matches",
          check_prohibited_constructions("I will remember what you said.")['matched'])
    check("I'll remember, don't worry. -> does NOT match the frozen forms "
          "(no explicit object -- real, documented coverage gap, not a bug)",
          not check_prohibited_constructions("I'll remember, don't worry.")['matched'])
    check("I'd like to remember that. -> does NOT match (desire-wrapped)",
          not check_prohibited_constructions("I'd like to remember that.")['matched'])
    check("That reminds me of summer. -> does NOT match (different verb)",
          not check_prohibited_constructions("That reminds me of summer.")['matched'])
    check("I vaguely remember feeling that way. -> does NOT match (hedged, past)",
          not check_prohibited_constructions("I vaguely remember feeling that way.")['matched'])
    check("I'll save this. -> matches",
          check_prohibited_constructions("I'll save this.")['matched'])
    check("I'll save what you just said. -> matches",
          check_prohibited_constructions("I'll save what you just said.")['matched'])
    check("I'll save that. -> matches (no exclusion trigger)",
          check_prohibited_constructions("I'll save that.")['matched'])
    check("I will save that for later. -> matches (trailing modifier)",
          check_prohibited_constructions("I will save that for later.")['matched'])
    check("I'll save the day. -> does NOT match (trivially -- no enumerated form present at all, "
          "'that' never appears; this does NOT exercise the exclusion branch)",
          not check_prohibited_constructions("I'll save the day.")['matched'])
    check("I will save that the day gets better. -> does NOT match "
          "(genuinely exercises the exclusion branch: 'that' IS present, followed by 'the day')",
          not check_prohibited_constructions("I will save that the day gets better.")['matched'])
    check("I will save that the moment gets better. -> DOES match "
          "(reaches the same branch, but 'the day' is absent -- confirms the exclusion "
          "is genuinely narrow, not a blanket suppression of 'save that ...')",
          check_prohibited_constructions("I will save that the moment gets better.")['matched'])
    check("That saved the story. -> does NOT match (past tense, rescue sense, wrong subject)",
          not check_prohibited_constructions("That saved the story.")['matched'])
    check("That saved the story from becoming sentimental. -> does NOT match",
          not check_prohibited_constructions("That saved the story from becoming sentimental.")['matched'])
    check("I retrieved that. -> matches",
          check_prohibited_constructions("I retrieved that.")['matched'])
    check("I pulled up this. -> matches",
          check_prohibited_constructions("I pulled up this.")['matched'])
    check("I pulled up what you said. -> matches",
          check_prohibited_constructions("I pulled up what you said.")['matched'])
    check("I found the earlier note. -> does NOT match (found excluded entirely)",
          not check_prohibited_constructions("I found the earlier note.")['matched'])
    check("I found that moving. -> does NOT match",
          not check_prohibited_constructions("I found that moving.")['matched'])
    check("I pulled up that memory. -> does NOT match (excluded: collides with ordinary recall)",
          not check_prohibited_constructions("I pulled up that memory.")['matched'])
    check("That reminds me of summer. -> does NOT match (experiencer role)",
          not check_prohibited_constructions("That reminds me of summer.")['matched'])
    check("I saved that. -> does NOT match (SAVE-PAST was pressure-tested and dropped)",
          not check_prohibited_constructions("I saved that.")['matched'])
    check("Embedded: \"That's a lovely thought. I'll remember that.\" -> matches",
          check_prohibited_constructions("That's a lovely thought. I'll remember that.")['matched'])
    check("Clean reply, no construction present -> does not match",
          not check_prohibited_constructions("A well-organized bookshelf can be a sanctuary for book lovers.")['matched'])

    suppressed = suppress_matched_sentences(
        "I'll remember that. The way you described it feels unusually gentle.",
        "REMEMBER-BARE"
    )
    check("Suppression: matched sentence removed, clean remainder survives",
          suppressed == "The way you described it feels unusually gentle.")

    suppressed_subordinator = suppress_matched_sentences(
        "I'll remember that because it connects to what you said.",
        "REMEMBER-BARE"
    )
    check("Suppression: single sentence containing both match and subordinator "
          "-- entire sentence removed, nothing survives",
          suppressed_subordinator == "")

    suppressed_only_match = suppress_matched_sentences(
        "I'll remember that.",
        "REMEMBER-BARE"
    )
    check("Suppression: reply is ONLY the matched sentence -> empty remainder",
          suppressed_only_match == "")

    # =========================================================================
    # BOUNDED-CLAUSE-REPEAT -- deterministic exact-repetition screen added by
    # bounded-clause semantic hardening (2026-08-30), for the demonstrated
    # first-native-turn failure mode: Clark's own prose reproducing the
    # host-rendered bounded clause verbatim.
    # =========================================================================
    NOT_AUTHORIZED_CLAUSE = "No new long-term memory entry was created from that."

    check("BOUNDED-CLAUSE-REPEAT: ordinary Clark prose (no repetition) passes clean",
          not check_bounded_clause_repeat(
              "It's genuinely good to hear from you.", NOT_AUTHORIZED_CLAUSE
          )["matched"])
    check("BOUNDED-CLAUSE-REPEAT: prose containing the exact clause is rejected",
          check_bounded_clause_repeat(
              NOT_AUTHORIZED_CLAUSE + " It's genuinely good to hear from you.",
              NOT_AUTHORIZED_CLAUSE,
          ) == {"matched": True, "construction": "BOUNDED-CLAUSE-REPEAT",
                "matched_form": NOT_AUTHORIZED_CLAUSE.lower()})
    check("BOUNDED-CLAUSE-REPEAT: case-only variation is rejected",
          check_bounded_clause_repeat(
              NOT_AUTHORIZED_CLAUSE.upper() + " It's genuinely good to hear from you.",
              NOT_AUTHORIZED_CLAUSE,
          )["matched"])
    check("BOUNDED-CLAUSE-REPEAT: ordinary whitespace variation (extra spaces/newline) is rejected",
          check_bounded_clause_repeat(
              "No  new long-term\nmemory entry   was created from that. Good to talk.",
              NOT_AUTHORIZED_CLAUSE,
          )["matched"])
    check("BOUNDED-CLAUSE-REPEAT: no bounded_clause supplied -> never matches",
          not check_bounded_clause_repeat("anything at all", None)["matched"])
    check("BOUNDED-CLAUSE-REPEAT: a different clause than the one supplied does not match",
          not check_bounded_clause_repeat(
              "A new long-term memory entry could not be created from that.",
              NOT_AUTHORIZED_CLAUSE,
          )["matched"])

    suppressed_bounded = suppress_matched_sentences(
        NOT_AUTHORIZED_CLAUSE + " It's genuinely good to hear from you.",
        "BOUNDED-CLAUSE-REPEAT",
        bounded_clause=NOT_AUTHORIZED_CLAUSE,
    )
    check("BOUNDED-CLAUSE-REPEAT suppression: repeated clause sentence removed, clean remainder survives",
          suppressed_bounded == "It's genuinely good to hear from you.")

    suppressed_bounded_only = suppress_matched_sentences(
        NOT_AUTHORIZED_CLAUSE, "BOUNDED-CLAUSE-REPEAT", bounded_clause=NOT_AUTHORIZED_CLAUSE
    )
    check("BOUNDED-CLAUSE-REPEAT suppression: reply is ONLY the repeated clause -> empty remainder",
          suppressed_bounded_only == "")

    # Direct demonstration that the exact first-turn failure (host prepends
    # its own copy of the bounded clause; Clark's own prose independently
    # repeats it) can no longer produce a doubled clause once the repeated
    # sentence is screened out and suppressed before the host's own
    # concatenation ever runs.
    host_clause = NOT_AUTHORIZED_CLAUSE
    raw_clark_prose = NOT_AUTHORIZED_CLAUSE + " It is genuinely good to hear from you."
    screened = check_bounded_clause_repeat(raw_clark_prose, host_clause)
    accepted_clark_prose = (
        suppress_matched_sentences(raw_clark_prose, "BOUNDED-CLAUSE-REPEAT", bounded_clause=host_clause)
        if screened["matched"] else raw_clark_prose.strip()
    )
    assembled_reply = host_clause if not accepted_clark_prose else f"{host_clause} {accepted_clark_prose}"
    check("BOUNDED-CLAUSE-REPEAT: host-prepended clause + screened prose never doubles the clause",
          assembled_reply.count(NOT_AUTHORIZED_CLAUSE) == 1)
    check("BOUNDED-CLAUSE-REPEAT: assembled reply matches the exact non-doubled expected string",
          assembled_reply == "No new long-term memory entry was created from that. "
                              "It is genuinely good to hear from you.")

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    import sys
    success = main()
    sys.exit(0 if success else 1)
