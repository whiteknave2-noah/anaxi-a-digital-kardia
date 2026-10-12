#!/usr/bin/env python3
"""Acquire or verify ANAXI's embedding model during explicit setup."""

from __future__ import annotations

import argparse

from sentence_transformers import SentenceTransformer


MODEL_NAME = "all-MiniLM-L6-v2"
EXPECTED_DIMENSION = 384


def prepare(*, check_only: bool = False) -> None:
    model = SentenceTransformer(MODEL_NAME, local_files_only=check_only)
    dimension_fn = getattr(
        model, "get_embedding_dimension", model.get_sentence_embedding_dimension,
    )
    dimension = dimension_fn()
    if dimension != EXPECTED_DIMENSION:
        raise RuntimeError(
            f"{MODEL_NAME!r} reported dimension {dimension!r}; "
            f"expected {EXPECTED_DIMENSION}."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Require an existing local cache and never contact a model registry.",
    )
    args = parser.parse_args()
    prepare(check_only=args.check_only)
    action = "verified in the local cache" if args.check_only else "prepared and verified"
    print(f"{MODEL_NAME} {action} ({EXPECTED_DIMENSION} dimensions).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
