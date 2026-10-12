"""
Anaxi -- Normalized relationship-claim test. Per GPT's explicit
instruction: tests whether semantically equivalent formulations of a
relationship claim ("I have two daughters" / "my daughters") are
recognized as the SAME self-referential proposition, in both
directions, and whether attribution (whose claim it is) is preserved
correctly, not just whether copying is prevented.

No real model calls -- pure host-side logic, tested directly against
the current _claims_same_relationship_as_source(), the same way
test_clark_journal.py works. Run this BEFORE deciding whether to
change the checker, per GPT's explicit sequencing.

Run:
    python test_relationship_normalization.py
"""

from clark_journal import _claims_same_relationship_as_source

results = []


def check(name, source, content, expected_reject, note=""):
    actual_reject = _claims_same_relationship_as_source(content, source)
    passed = actual_reject == expected_reject
    print(f"{'PASS' if passed else 'FAIL'}: {name}")
    print(f"  source:  {source!r}")
    print(f"  content: {content!r}")
    print(f"  expected: {'reject' if expected_reject else 'allow'}, "
          f"actual: {'reject' if actual_reject else 'allow'}" + (f"  ({note})" if note else ""))
    results.append(passed)


print("=" * 70)
print("GPT's five specified cases")
print("=" * 70)

check(
    "1. 'I have X' source -> 'my X' content -- SAME claim, different phrasing",
    "I have two daughters.",
    "Watching my daughters grow has been wonderful.",
    expected_reject=True,
)

check(
    "2. 'my X' source -> 'I have X' content -- reverse direction",
    "My daughters mean everything to me.",
    "I have two daughters, and they mean everything to me.",
    expected_reject=True,
)

check(
    "3. Third-person source ('Alex has X') -> 'my X' content -- claiming a THIRD party's relationship",
    "Alex has two daughters.",
    "I've been thinking about my daughters today.",
    expected_reject=True,
)

check(
    "4. Third-person source -> properly attributed content -- MUST be allowed, this is the critical case",
    "Alex has two daughters.",
    "Alex's daughters sound wonderful from what he's shared.",
    expected_reject=False,
)

check(
    "5. First-person source -> third-person content ('Alex's X') -- soft case, GPT herself uncertain",
    "I have two daughters.",
    "Alex's daughters sound like a wonderful part of his life.",
    expected_reject=False,  # this direction is safe/legitimate regardless -- attributing correctly to the source speaker
    note="GPT flagged this as debatable; testing the 'allow' reading since correct attribution should never be penalized",
)

print(f"\n{'=' * 70}\n{sum(results)}/{len(results)} pass\n{'=' * 70}")
