"""
Anaxi -- Compatibility evaluation orchestration. Verifies the frozen
input hashes, loads the frozen packages/serialized messages, builds
the per-slot blinding assignment, and (once a real call_model_fn is
wired in and execution is separately authorized) would run all
10 slots x 2 substrates through compatibility_harness.py.

Does NOT call either model. main() below stops before any real
substrate call -- there is no real call_model_fn defined or invoked
anywhere in this file. Wiring a real substrate call is a distinct,
later, separately-authorized step.

Run (build/verification only, no model calls):
    python run_compatibility_harness.py
"""

import hashlib
import json
import sys

FROZEN_SERIALIZED_PACKAGES_SHA256 = "f383d584eba64433bd8702c41b66e1c7a38f33e20ae0e2c309d93e22a845f14e"
FROZEN_IDENTITY_RAW_SHA256 = "08925cd3781644657b17cea1845a3a76e951cbccc7181ba7a21d1f1871de1f13"
FROZEN_IDENTITY_CANONICAL_SHA256 = "ce828641642d15efd2f0850f724c68929a836bcbcbaccd6cc8bbebd79f4bee41"

SERIALIZED_PACKAGES_PATH = "serialized_packages.json"
IDENTITY_SNAPSHOT_PATH = "identity_snapshot.json"


class FrozenHashMismatchError(Exception):
    """Raised if any frozen input's hash does not match the expected,
    recorded value. Per explicit instruction: stop immediately and
    report -- never regenerate or 'repair' the frozen inputs."""
    pass


def verify_frozen_hashes(serialized_path: str = SERIALIZED_PACKAGES_PATH,
                          identity_path: str = IDENTITY_SNAPSHOT_PATH,
                          expected_serialized_sha256: str = FROZEN_SERIALIZED_PACKAGES_SHA256,
                          expected_identity_raw_sha256: str = FROZEN_IDENTITY_RAW_SHA256,
                          expected_identity_canonical_sha256: str = FROZEN_IDENTITY_CANONICAL_SHA256) -> dict:
    """Recomputes all three frozen hashes fresh from disk and compares
    against the expected, recorded values. Raises FrozenHashMismatchError
    on any mismatch -- callers must not catch this and proceed."""
    with open(serialized_path, "rb") as f:
        serialized_bytes = f.read()
    actual_serialized_sha256 = hashlib.sha256(serialized_bytes).hexdigest()

    with open(identity_path, "rb") as f:
        identity_raw_bytes = f.read()
    actual_identity_raw_sha256 = hashlib.sha256(identity_raw_bytes).hexdigest()

    identity_snapshot = json.loads(identity_raw_bytes)
    content_only = {k: v for k, v in identity_snapshot.items()
                     if k != "sha256_of_content_excluding_this_field"}
    actual_identity_canonical_sha256 = hashlib.sha256(
        json.dumps(content_only, indent=2, sort_keys=True).encode("utf-8")
    ).hexdigest()

    results = {
        "serialized_packages": (actual_serialized_sha256, expected_serialized_sha256),
        "identity_raw": (actual_identity_raw_sha256, expected_identity_raw_sha256),
        "identity_canonical": (actual_identity_canonical_sha256, expected_identity_canonical_sha256),
    }

    mismatches = {name: (actual, expected) for name, (actual, expected) in results.items()
                  if actual != expected}
    if mismatches:
        lines = [f"  {name}: expected {expected}, got {actual}"
                 for name, (actual, expected) in mismatches.items()]
        raise FrozenHashMismatchError(
            "Frozen input hash mismatch -- stopping, not regenerating or repairing:\n"
            + "\n".join(lines)
        )

    return {name: actual for name, (actual, expected) in results.items()}


def main():
    print("=" * 70)
    print("COMPATIBILITY HARNESS ORCHESTRATION -- build/verification only")
    print("No model calls occur in this script.")
    print("=" * 70)

    try:
        verified = verify_frozen_hashes()
    except FrozenHashMismatchError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)

    print("\nFrozen hash verification: PASS")
    for name, h in verified.items():
        print(f"  {name}: {h}")

    from substrate_compatibility_packages import PACKAGES

    with open(SERIALIZED_PACKAGES_PATH, encoding="utf-8") as f:
        serialized = json.load(f)

    print(f"\nLoaded {len(PACKAGES)} frozen packages, {len(serialized)} serialized slots.")
    print("\nThis script stops here. No real call_model_fn is defined or invoked --")
    print("wiring a real substrate call is a separate, later, authorized step.")


if __name__ == "__main__":
    main()
