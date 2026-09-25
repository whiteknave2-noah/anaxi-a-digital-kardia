"""The ordinary embedding path is local-only; setup owns acquisition."""

import pytest

import embedding_model
import prepare_embedding_model


class _Model:
    def __init__(self, dimension=384):
        self.dimension = dimension

    def get_sentence_embedding_dimension(self):
        return self.dimension

    def get_embedding_dimension(self):
        return self.dimension


def test_runtime_requires_local_cache(monkeypatch):
    calls = []

    def constructor(name, **kwargs):
        calls.append((name, kwargs))
        return _Model()

    monkeypatch.setattr(embedding_model, "SentenceTransformer", constructor)
    model = embedding_model.load_cached_embedding_model("synthetic-model", 384)
    assert isinstance(model, _Model)
    assert calls == [("synthetic-model", {"local_files_only": True})]


def test_runtime_reports_missing_cache_without_retrying_online(monkeypatch):
    calls = []

    def unavailable(name, **kwargs):
        calls.append((name, kwargs))
        raise OSError("synthetic cache miss")

    monkeypatch.setattr(embedding_model, "SentenceTransformer", unavailable)
    with pytest.raises(embedding_model.EmbeddingModelUnavailable, match="will not download"):
        embedding_model.load_cached_embedding_model("synthetic-model", 384)
    assert calls == [("synthetic-model", {"local_files_only": True})]


def test_runtime_rejects_wrong_dimension(monkeypatch):
    monkeypatch.setattr(
        embedding_model,
        "SentenceTransformer",
        lambda *args, **kwargs: _Model(dimension=128),
    )
    with pytest.raises(embedding_model.EmbeddingModelUnavailable, match="requires 384"):
        embedding_model.load_cached_embedding_model("synthetic-model", 384)


def test_explicit_prepare_may_acquire_but_check_only_may_not(monkeypatch):
    calls = []

    def constructor(name, **kwargs):
        calls.append((name, kwargs))
        return _Model()

    monkeypatch.setattr(prepare_embedding_model, "SentenceTransformer", constructor)
    prepare_embedding_model.prepare(check_only=False)
    prepare_embedding_model.prepare(check_only=True)
    assert calls == [
        (prepare_embedding_model.MODEL_NAME, {"local_files_only": False}),
        (prepare_embedding_model.MODEL_NAME, {"local_files_only": True}),
    ]
