"""
Anaxi -- Regression suite for clark_journal.py. Covers the original
Phase 1 write-tool behavior, GPT's two original corrections (the
None-vs-FAILED distinction, collision-safe filenames), and the settled
authority-boundary contract: create_artifact removed, replaced by
identified_referent (verbatim, mechanically verified) and
construction_status. No real model calls -- pure host-side logic,
tested directly with synthetic JSON inputs.

This is a genuine replacement of the previous 19-case suite, built
directly from that suite's own complete classification (6 Phase-1
tests untouched, 2 authorization tests replaced, 10 construction
tests updated to the new schema, 1 input-robustness test updated,
plus new tests the contract specifically requires) -- not written
fresh from scratch.

Isolated: uses a throwaway test workspace, never touches anything live.

Run:
    python test_clark_journal.py
"""

import json
import os
import shutil
import time

import clark_journal
from clark_journal import process_artifact_decision, write_journal_entry, StaleSchemaError

TEST_WORKSPACE = "test_clark_journal_workspace"
results = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}: {name}")
    results.append(cond)


def main():
    if os.path.exists(TEST_WORKSPACE):
        shutil.rmtree(TEST_WORKSPACE)
    os.makedirs(TEST_WORKSPACE)
    ROOT = os.path.realpath(TEST_WORKSPACE)

    r = write_journal_entry(ROOT, "Journal/basic-test.md", "basic content")
    check("Phase 1: normal write succeeds", r["status"] == "success")
    check("Phase 1: provenance frontmatter present", "author: clark" in open(r["path"]).read())

    r_escape = write_journal_entry(ROOT, "../../../escape.md", "x")
    check("Phase 1: path traversal rejected", r_escape["status"] == "rejected")

    r_abs = write_journal_entry(ROOT, "/tmp/absolute_escape.md", "x")
    check("Phase 1: absolute path rejected", r_abs["status"] == "rejected")
    check("Phase 1: absolute path escape file NOT created", not os.path.exists("/tmp/absolute_escape.md"))

    r_ext = write_journal_entry(ROOT, "Journal/wrong.exe", "x")
    check("Phase 1: non-.md extension rejected", r_ext["status"] == "rejected")

    r1 = process_artifact_decision(json.dumps({
        "identified_referent": None, "construction_status": "insufficient_information",
        "artifact_type": None, "artifact_scope": None, "namespace": None,
        "title": None, "what_they_shared": None, "content": None,
    }), ROOT)
    check("New contract: insufficient_information -> no write, correct 'None.' context",
          r1["artifact_created"] is False and r1["pass2_context"] == "Artifact action:\nNone.")
    check("New contract: insufficient_information is labeled as such in detail, not conflated with FAILED",
          r1["detail"]["status"] == "insufficient_information")

    source_2 = "I've been thinking about this -- I want to keep this thought: a real entry."
    r2 = process_artifact_decision(json.dumps({
        "identified_referent": "a real entry", "construction_status": "success",
        "artifact_type": "journal", "artifact_scope": "personal", "namespace": "journal",
        "title": "a real entry", "what_they_shared": None, "content": "real content",
    }), ROOT, source_prompt=source_2)
    check("New contract: success + verified referent -> successful write", r2["artifact_created"] is True)
    check("New contract: Pass 2 context reflects the real created path", "Created:" in r2["pass2_context"])

    r_invented = process_artifact_decision(json.dumps({
        "identified_referent": "the user's reflection about the project",
        "construction_status": "success", "artifact_type": "journal",
        "artifact_scope": "personal", "namespace": "journal", "title": "x",
        "what_they_shared": None, "content": "x",
    }), ROOT, source_prompt="Please save this.")
    check("New contract: invented referent (GPT's exact example) -> FAILED, not written",
          r_invented["artifact_created"] is False and "FAILED" in r_invented["pass2_context"])
    check("New contract: invented-referent failure reason names verification, not a generic error",
          "does not appear in the source turn" in r_invented["detail"]["reason"])

    r_variant = process_artifact_decision(json.dumps({
        "identified_referent": "A Real Entry!", "construction_status": "success",
        "artifact_type": "journal", "artifact_scope": "personal", "namespace": "journal",
        "title": "x", "what_they_shared": None, "content": "x",
    }), ROOT, source_prompt=source_2)
    check("New contract: genuinely faithful quote with different case/punctuation still verifies",
          r_variant["artifact_created"] is True)

    source_3 = "Please save this poem I wrote."
    r3 = process_artifact_decision(json.dumps({
        "identified_referent": "this poem I wrote", "construction_status": "success",
        "artifact_type": "poem", "artifact_scope": "personal", "namespace": "journal",
        "title": "x", "what_they_shared": None, "content": "x",
    }), ROOT, source_prompt=source_3)
    check("Correction 1: invalid enum -> FAILED, not None", "FAILED" in r3["pass2_context"])
    check("Correction 1: FAILED message includes the actual reason", "not a supported type" in r3["pass2_context"])

    original_write = clark_journal.write_journal_entry
    clark_journal.write_journal_entry = lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked"))
    source_4 = "Please save this specific thing."
    r4 = process_artifact_decision(json.dumps({
        "identified_referent": "this specific thing", "construction_status": "success",
        "artifact_type": "journal", "artifact_scope": "personal", "namespace": "journal",
        "title": "x", "what_they_shared": None, "content": "x",
    }), ROOT, source_prompt=source_4)
    clark_journal.write_journal_entry = original_write
    check("Correction 1: genuine OS failure -> FAILED, not None, no crash", "FAILED" in r4["pass2_context"])

    check("Correction 1: insufficient_information case still correctly says 'None.' (not conflated with FAILED)",
          r1["pass2_context"] == "Artifact action:\nNone.")

    source_5 = "Please save this same title thought, both times I mention it."
    payload_a = json.dumps({
        "identified_referent": "this same title thought", "construction_status": "success",
        "artifact_type": "journal", "artifact_scope": "personal", "namespace": "journal",
        "title": "same title", "what_they_shared": None, "content": "FIRST",
    })
    r5a = process_artifact_decision(payload_a, ROOT, source_prompt=source_5)
    time.sleep(1.1)
    payload_b = json.dumps({
        "identified_referent": "this same title thought", "construction_status": "success",
        "artifact_type": "journal", "artifact_scope": "personal", "namespace": "journal",
        "title": "same title", "what_they_shared": None, "content": "SECOND",
    })
    r5b = process_artifact_decision(payload_b, ROOT, source_prompt=source_5)

    check("Correction 2: same-title entries get different filenames",
          r5a["detail"]["path"] != r5b["detail"]["path"])
    check("Correction 2: both files exist",
          os.path.exists(r5a["detail"]["path"]) and os.path.exists(r5b["detail"]["path"]))
    content_a = open(r5a["detail"]["path"]).read()
    content_b = open(r5b["detail"]["path"]).read()
    check("Correction 2: first entry survives UNCHANGED (not overwritten)", "FIRST" in content_a and "SECOND" not in content_a)
    check("Correction 2: second entry correct and separate", "SECOND" in content_b)

    r6 = process_artifact_decision("not json {{{", ROOT)
    check("Malformed JSON doesn't crash", r6["artifact_created"] is False)

    r7 = process_artifact_decision(json.dumps({
        "identified_referent": "something", "construction_status": "success",
    }), ROOT, source_prompt="something")
    check("Missing required construction fields doesn't crash", r7["artifact_created"] is False)

    stale_raised = False
    try:
        process_artifact_decision(json.dumps({
            "reason": "old shape", "create_artifact": True, "artifact_type": "journal",
            "artifact_scope": "personal", "namespace": "journal", "title": "x", "content": "x",
        }), ROOT)
    except StaleSchemaError:
        stale_raised = True
    check("Stale 'create_artifact' field raises StaleSchemaError, loud rather than silently absorbed",
          stale_raised)

    shutil.rmtree(TEST_WORKSPACE)
    print(f"\n{sum(results)}/{len(results)} tests pass")
    if all(results):
        print("ALL PASS")
    else:
        print("SOME TESTS FAILED -- see above")


if __name__ == "__main__":
    main()
