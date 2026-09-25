"""Fail-closed loading for ANAXI's required local embedding model.

Ordinary waking must not turn a missing dependency into an implicit network
operation.  Model acquisition belongs to the explicit bootstrap step; runtime
only opens an already-cached model.
"""

from sentence_transformers import SentenceTransformer


class EmbeddingModelUnavailable(RuntimeError):
    """The required embedding model is not present or has the wrong shape."""


def load_cached_embedding_model(model_name: str, expected_dimension: int):
    """Load *model_name* without network access and verify its output shape."""
    try:
        model = SentenceTransformer(model_name, local_files_only=True)
    except Exception as exc:  # sentence-transformers exposes backend-specific errors
        raise EmbeddingModelUnavailable(
            f"Required embedding model {model_name!r} is not available locally. "
            "Run bootstrap_macos.sh while network access is intentionally enabled, "
            "then launch ANAXI again. Ordinary waking will not download models."
        ) from exc

    dimension_fn = getattr(
        model, "get_embedding_dimension", model.get_sentence_embedding_dimension,
    )
    dimension = dimension_fn()
    if dimension != expected_dimension:
        raise EmbeddingModelUnavailable(
            f"Embedding model {model_name!r} reported dimension {dimension!r}; "
            f"ANAXI requires {expected_dimension}. Re-run bootstrap_macos.sh."
        )
    return model
