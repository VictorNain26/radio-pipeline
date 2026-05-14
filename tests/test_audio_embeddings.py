"""
Tests for the EmbeddingStore — the pure-IO half of audio_embeddings.py.

We do NOT test compute_embedding() here: it pulls a 1.7 GB CLAP model
from HF Hub and runs CPU inference, both unsuitable for unit tests.
Live validation has already been done end-to-end in this codebase
(see commit log).
"""
from __future__ import annotations

import numpy as np
import pytest

from audio_embeddings import EmbeddingStore  # type: ignore[import-not-found]


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(512).astype(np.float32)
    return v / np.linalg.norm(v)


def test_store_add_and_get(tmp_path):
    store = EmbeddingStore(tmp_path)
    v = _vec(1)
    store.add("artist - title", v)
    assert store.has("artist - title")
    got = store.get("artist - title")
    np.testing.assert_array_equal(got, v)


def test_store_persists_to_disk_and_reloads(tmp_path):
    store1 = EmbeddingStore(tmp_path)
    store1.add("a - x", _vec(1))
    store1.add("a - y", _vec(2))
    store1.add("b - z", _vec(3))

    # Fresh instance reads the same data
    store2 = EmbeddingStore(tmp_path)
    keys, emb = store2.all()
    assert keys == ["a - x", "a - y", "b - z"]
    assert emb.shape == (3, 512)


def test_store_overwrites_existing_key(tmp_path):
    store = EmbeddingStore(tmp_path)
    store.add("k", _vec(1))
    new = _vec(2)
    store.add("k", new)
    keys, emb = store.all()
    assert keys == ["k"]
    assert emb.shape == (1, 512)
    np.testing.assert_array_equal(emb[0], new)


def test_store_prune_drops_stale_keys(tmp_path):
    store = EmbeddingStore(tmp_path)
    for i, k in enumerate(["a", "b", "c", "d"]):
        store.add(k, _vec(i))
    removed = store.prune(valid_keys={"b", "d"})
    assert removed == 2
    keys, emb = store.all()
    assert set(keys) == {"b", "d"}
    assert emb.shape == (2, 512)


def test_store_empty_at_start(tmp_path):
    store = EmbeddingStore(tmp_path)
    keys, emb = store.all()
    assert keys == []
    assert emb is None
    assert store.get("nothing") is None
    assert not store.has("nothing")
