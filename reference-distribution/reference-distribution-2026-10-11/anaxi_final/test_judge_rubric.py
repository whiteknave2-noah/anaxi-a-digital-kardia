"""
Anaxi -- Judge rubric verification. Deterministic/static checks only,
no model calls. Confirms the new substrate-suitability rubric carries
no old continuity-framing language, no anchor/sealed-response/
telemetry/identity leakage, correctly supports "Cannot tell" as a
first-class result, represents the seven primary dimensions and the
exploratory developmental-openness field correctly, and that the
frozen model-facing artifacts remain byte-identical to their recorded
hashes.

Run:
    python test_judge_rubric.py
"""

import hashlib
import json

from judge_rubric import (
    JUDGE_RUBRIC_VERSION, PRIMARY_DIMENSIONS, DEVELOPMENTAL_OPENNESS_FIELD,
    MEMORY_STATE_LANGUAGE_FIELD,
    ABSOLUTE_SUITABILITY_SCALE, PAIRWISE_SCALE, FIRST_IMPRESSION_PROMPT,
    JUDGING_ORDER, applicable_dimensions, build_judging_worksheet,
    build_empty_judgment_record,
)
from compatibility_harness import build_judge_facing_entry
from substrate_compatibility_packages import PACKAGES, SEALED_HISTORICAL_RESPONSES, ANCHOR_SET

FROZEN_SERIALIZED_PACKAGES_SHA256 = "f383d584eba64433bd8702c41b66e1c7a38f33e20ae0e2c309d93e22a845f14e"
FROZEN_IDENTITY_RAW_SHA256 = "08925cd3781644657b17cea1845a3a76e951cbccc7181ba7a21d1f1871de1f13"
FROZEN_IDENTITY_CANONICAL_SHA256 = "ce828641642d15efd2f0850f724c68929a836bcbcbaccd6cc8bbebd79f4bee41"

FORBIDDEN_TERMS = [
    "continuity of voice", "continuation-of-person", "continuation of person",
    "more like clark", "recognizably continuous", "plausibly continuous",
    "continuity rupture", "is clark", "sounds like clark",
]

results = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}: {name}")
    results.append(cond)


def main():
    # Build one real worksheet per real slot, mocked prose only (no model calls).
    worksheets = {}
    for slot_id, pkg in PACKAGES.items():
        mock_telemetry = {"surviving_prose": f"[mock response for {slot_id}]"}
        judge_entry = build_judge_facing_entry(mock_telemetry, mock_telemetry, slot_id)
        has_kardia = bool(pkg.get("kardia_payload"))
        worksheets[slot_id] = build_judging_worksheet(judge_entry, has_kardia)

    all_worksheet_text = json.dumps(worksheets)

    # === 1. No old continuity/rupture/person-recognition labels reach the judge-facing artifact ===
    for term in FORBIDDEN_TERMS:
        check(f"1. Forbidden term absent from worksheets: {term!r}",
              term not in all_worksheet_text.lower())

    # === 2. No anchor-calibration material reaches the judge ===
    anchor_text_fragments = [json.dumps(v) for v in ANCHOR_SET.values()]
    check("2. No ANCHOR_SET content appears anywhere in any built worksheet",
          not any(frag in all_worksheet_text for frag in anchor_text_fragments))
    check("2. The literal key 'ANCHOR_SET' never appears in worksheet output",
          "ANCHOR_SET" not in all_worksheet_text and "anchor_set" not in all_worksheet_text.lower())

    # === 3. Sealed historical responses remain absent ===
    for slot_id, sealed_text in SEALED_HISTORICAL_RESPONSES.items():
        check(f"3. Sealed historical response for {slot_id} absent from worksheets",
              sealed_text not in all_worksheet_text)

    # === 4. Substrate/model identity remains absent ===
    check("4. No real model tag names appear in any worksheet",
          "llama3.2" not in all_worksheet_text and "gemma4" not in all_worksheet_text
          and "e4b" not in all_worksheet_text.lower())

    # === 5. Host bounded clauses and Phase-3 telemetry remain absent ===
    check("5. Bounded clause text absent from worksheets",
          "No new long-term memory entry was created from that." not in all_worksheet_text)
    forbidden_telemetry_keys = [
        "first_screen_matched", "matched_construction", "regeneration_invoked",
        "second_screen_matched", "suppression_invoked", "terminal_fallback",
        "bounded_clause", "assembled_reply", "model_invocation_count",
        "signal_category", "operation_status",
    ]
    for key in forbidden_telemetry_keys:
        check(f"5. Telemetry field absent from worksheets: {key!r}",
              key not in all_worksheet_text)

    # === 6. "Cannot tell" supported as a legitimate absolute and pairwise result ===
    check("6. 'Cannot tell' present in ABSOLUTE_SUITABILITY_SCALE",
          "Cannot tell" in ABSOLUTE_SUITABILITY_SCALE)
    check("6. 'Cannot tell' present in PAIRWISE_SCALE",
          "Cannot tell" in PAIRWISE_SCALE)
    empty_record = build_empty_judgment_record("H1")
    empty_record["first_read"]["immediate_pairwise_judgment"] = "Cannot tell"
    check("6. 'Cannot tell' round-trips through the judgment record unchanged",
          empty_record["first_read"]["immediate_pairwise_judgment"] == "Cannot tell")

    # === 7. Seven primary dimensions + exploratory field represented correctly ===
    check("7. Exactly 7 primary dimensions defined",
          len(PRIMARY_DIMENSIONS) == 7)
    expected_dims = {
        "groundedness", "restraint", "relational_attunement", "interpretive_depth",
        "naturalness", "correct_kardia_use", "attribution_discipline",
    }
    check("7. Primary dimension names match exactly",
          set(PRIMARY_DIMENSIONS.keys()) == expected_dims)
    check("7. Developmental-openness field is marked non-scored/exploratory",
          DEVELOPMENTAL_OPENNESS_FIELD["scored"] is False
          and DEVELOPMENTAL_OPENNESS_FIELD["exploratory"] is True)
    check("7. Developmental-openness field's own text never says 'sounds like Clark'",
          "sounds like clark" not in DEVELOPMENTAL_OPENNESS_FIELD["prompt"].lower())

    # === memory_state_language: narrow correction, 9 required checks ===
    check("MSL-1. memory_state_language field exists",
          MEMORY_STATE_LANGUAGE_FIELD["name"] == "memory_state_language")
    check("MSL-2. Its three allowed values are exactly present/absent/uncertain",
          MEMORY_STATE_LANGUAGE_FIELD["allowed_values"] == ["present", "absent", "uncertain"])
    check("MSL-3. It is explicitly unscored",
          MEMORY_STATE_LANGUAGE_FIELD["scored"] is False)
    check("MSL-4. Its definition states occurrence does not determine validity",
          "does not determine whether" in MEMORY_STATE_LANGUAGE_FIELD["definition_note"]
          and "authorized, grounded, fabricated" in MEMORY_STATE_LANGUAGE_FIELD["definition_note"])
    check("MSL-5. It appears in the second_read portion of JUDGING_ORDER",
          "memory_state_language" in JUDGING_ORDER["second_read"])
    check("MSL-5. It does NOT appear in the first_read_unprimed portion (must not prime)",
          "memory_state_language" not in JUDGING_ORDER["first_read_unprimed"])
    check("MSL-5. It does NOT appear in the final portion",
          "memory_state_language" not in JUDGING_ORDER["final"])
    check("MSL-6. Its definition explicitly disclaims affecting absolute suitability labels",
          "does not affect absolute substrate-suitability labels" in MEMORY_STATE_LANGUAGE_FIELD["definition_note"])
    check("MSL-6. Its definition explicitly disclaims affecting pairwise preference",
          "pairwise preference" in MEMORY_STATE_LANGUAGE_FIELD["definition_note"])
    check("MSL-6. No aggregation/preference function anywhere reads memory_state_language "
          "(build_judging_worksheet/build_empty_judgment_record only store it, never branch on it)",
          "memory_state_language" not in applicable_dimensions(True)
          and "memory_state_language" not in applicable_dimensions(False))
    check("MSL. Independent of groundedness/correct_kardia_use/attribution_discipline "
          "(named explicitly in its own definition, not merged into any of the three)",
          all(term in MEMORY_STATE_LANGUAGE_FIELD["definition_note"]
              for term in ["groundedness", "correct_kardia_use", "attribution_discipline"]))
    check("MSL. Field is present in the built worksheet under its own key",
          "memory_state_language_field" in worksheets["H1"])
    check("MSL. Empty judgment record stores it under second_read, not first_read or final",
          "memory_state_language" in empty_record["second_read"]
          and "memory_state_language" not in empty_record["first_read"]
          and "memory_state_language" not in empty_record["final"])
    check("MSL. Its own occurrence text never appears in the first-read section of the worksheet",
          MEMORY_STATE_LANGUAGE_FIELD["name"] not in JUDGING_ORDER["first_read_unprimed"])

    # correct_kardia_use conditionality
    dims_with_kardia = applicable_dimensions(has_kardia_payload=True)
    dims_without_kardia = applicable_dimensions(has_kardia_payload=False)
    check("7. correct_kardia_use included when a slot has a kardia_payload",
          "correct_kardia_use" in dims_with_kardia)
    check("7. correct_kardia_use OMITTED when a slot has no kardia_payload",
          "correct_kardia_use" not in dims_without_kardia)

    # Spot-check against the real frozen packages: P3 and P5 have kardia_payload, H1 does not
    check("7. Real slot P3 (has kardia_payload) gets correct_kardia_use in its worksheet",
          "correct_kardia_use" in worksheets["P3"]["applicable_dimensions"])
    check("7. Real slot H1 (no kardia_payload) does NOT get correct_kardia_use in its worksheet",
          "correct_kardia_use" not in worksheets["H1"]["applicable_dimensions"])

    # === No numerical personality scores introduced ===
    check("Rubric version explicitly identifies this as the substrate-suitability rubric",
          "substrate-suitability" in JUDGE_RUBRIC_VERSION)

    # === 8. Model-facing serialized messages remain byte-identical to frozen versions ===
    with open("serialized_packages.json", "rb") as f:
        serialized_bytes = f.read()
    actual_serialized_sha256 = hashlib.sha256(serialized_bytes).hexdigest()
    check("8. serialized_packages.json SHA-256 unchanged from the frozen, recorded value",
          actual_serialized_sha256 == FROZEN_SERIALIZED_PACKAGES_SHA256)

    with open("identity_snapshot.json", "rb") as f:
        identity_raw_bytes = f.read()
    actual_identity_raw_sha256 = hashlib.sha256(identity_raw_bytes).hexdigest()
    check("8. identity_snapshot.json raw SHA-256 unchanged from the frozen, recorded value",
          actual_identity_raw_sha256 == FROZEN_IDENTITY_RAW_SHA256)

    identity_snapshot = json.loads(identity_raw_bytes)
    content_only = {k: v for k, v in identity_snapshot.items()
                     if k != "sha256_of_content_excluding_this_field"}
    actual_identity_canonical_sha256 = hashlib.sha256(
        json.dumps(content_only, indent=2, sort_keys=True).encode("utf-8")
    ).hexdigest()
    check("8. identity_snapshot.json canonical-content SHA-256 unchanged",
          actual_identity_canonical_sha256 == FROZEN_IDENTITY_CANONICAL_SHA256)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    import sys
    success = main()
    sys.exit(0 if success else 1)
