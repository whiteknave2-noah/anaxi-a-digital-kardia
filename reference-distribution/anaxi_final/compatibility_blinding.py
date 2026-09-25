"""
Anaxi -- Per-slot independent A/B blinding for the compatibility
evaluation. Deliberately NOT the Gemma comparison's global model_A/
model_B design (gemma_comparison_corpus.py's generate_anonymous_run_ids)
-- each of the 10 slots gets its own, independently-seeded A/B
assignment, so a judge inferring one slot's substrate identity learns
nothing about any other slot's assignment.
"""

import json
import random


def assign_ab_per_slot(slot_ids: list, substrate_tags: tuple, seed) -> dict:
    """substrate_tags: (tag_1, tag_2), the two real model tags.
    Returns {slot_id: {"A": tag, "B": tag}}. Each slot's coin flip
    uses its OWN random.Random instance, seeded from (seed, slot_id)
    -- genuinely independent per slot, not one shuffle reused or
    incrementally advanced across slots, which would let an observer
    who inferred one slot's assignment predict others."""
    assignment = {}
    for slot_id in slot_ids:
        rng = random.Random(f"{seed}:{slot_id}")
        tags = list(substrate_tags)
        rng.shuffle(tags)
        assignment[slot_id] = {"A": tags[0], "B": tags[1]}
    return assignment


def persist_blinding_key(slot_ids: list, assignment: dict, model_digests: dict, seed,
                          serialized_packages_sha256: str,
                          identity_raw_sha256: str,
                          identity_canonical_sha256: str,
                          generation_controls: dict,
                          filepath: str = "compatibility_blinding_key.json",
                          dry_run: bool = False) -> str:
    """Writes the complete blinding key to disk BEFORE any model call
    -- containing everything needed to later reveal which substrate
    produced which side, for every slot independently. Must be kept
    separate from anything the judge sees.

    Exclusive creation ("x" mode, not "w"): refuses to silently
    overwrite an existing blinding key -- a second attempted
    persistence at the same path raises FileExistsError instead of
    destroying prior evidence. Callers needing a fresh path (e.g. a
    new run) must pass a new filepath, not rely on this function to
    clear the old one.

    dry_run defaults to False and is stamped into the persisted key
    itself, additively -- existing callers that don't pass it keep
    their prior behavior (dry_run: false), so this is not a
    methodology change to the A/B assignment mechanism itself, only
    an additive content marker."""
    key = {
        "dry_run": dry_run,
        "seed": seed,
        "display_order": list(slot_ids),
        "per_slot_assignment": assignment,
        "model_digests": model_digests,
        "serialized_packages_sha256": serialized_packages_sha256,
        "identity_raw_sha256": identity_raw_sha256,
        "identity_canonical_sha256": identity_canonical_sha256,
        "generation_controls": generation_controls,
    }
    with open(filepath, "x", encoding="utf-8", newline="") as f:
        json.dump(key, f, indent=2, sort_keys=True)
    return filepath
