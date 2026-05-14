"""
Tests for smart_queue.SmartQueueIndex — FAISS nearest-neighbour walks
over an EmbeddingStore.
"""
from __future__ import annotations

import numpy as np
import pytest

faiss = pytest.importorskip("faiss")  # FAISS may not be installed in CI

from audio_embeddings import EmbeddingStore  # type: ignore[import-not-found]
from smart_queue import SmartQueueIndex  # type: ignore[import-not-found]


def _unit(vec: np.ndarray) -> np.ndarray:
    return (vec / np.linalg.norm(vec)).astype(np.float32)


def _build_store(tmp_path):
    """6 vectors in 2 clusters on a 512-dim unit sphere."""
    store = EmbeddingStore(tmp_path)
    base_a = np.zeros(512, dtype=np.float32)
    base_a[0] = 1.0
    base_b = np.zeros(512, dtype=np.float32)
    base_b[1] = 1.0

    rng = np.random.default_rng(42)
    for i in range(3):
        # Tracks A_i are tight cluster around base_a (very high cosine)
        v = base_a + rng.standard_normal(512).astype(np.float32) * 0.05
        store.add(f"clusterA - track{i}", _unit(v))
    for i in range(3):
        v = base_b + rng.standard_normal(512).astype(np.float32) * 0.05
        store.add(f"clusterB - track{i}", _unit(v))
    return store


def test_nearest_returns_same_cluster_first(tmp_path):
    store = _build_store(tmp_path)
    index = SmartQueueIndex(store)
    neighbours = index.nearest("clusterA - track0", k=3)
    names = [n for n, _ in neighbours]
    # Top neighbours must come from cluster A (not the seed itself)
    assert names[:2] == ["clusterA - track1", "clusterA - track2"] or \
           names[:2] == ["clusterA - track2", "clusterA - track1"]
    # The 3rd is from cluster B, with lower similarity
    assert neighbours[0][1] > neighbours[2][1]


def test_nearest_excludes_seed(tmp_path):
    store = _build_store(tmp_path)
    index = SmartQueueIndex(store)
    neighbours = index.nearest("clusterA - track0", k=5)
    names = [n for n, _ in neighbours]
    assert "clusterA - track0" not in names


def test_nearest_restrict_to_subset(tmp_path):
    store = _build_store(tmp_path)
    index = SmartQueueIndex(store)
    only_b = {"clusterB - track0", "clusterB - track1", "clusterB - track2"}
    neighbours = index.nearest("clusterA - track0", k=3, restrict_to=only_b)
    names = {n for n, _ in neighbours}
    assert names.issubset(only_b)


def test_greedy_walk_stays_within_candidates(tmp_path):
    store = _build_store(tmp_path)
    index = SmartQueueIndex(store)
    candidates = {f"clusterA - track{i}" for i in range(3)}
    walk = index.greedy_walk("clusterA - track0", length=5, candidates=candidates)
    assert walk[0] == "clusterA - track0"
    assert set(walk).issubset(candidates)
    # All distinct
    assert len(walk) == len(set(walk))


def test_greedy_walk_terminates_when_candidates_exhausted(tmp_path):
    store = _build_store(tmp_path)
    index = SmartQueueIndex(store)
    candidates = {f"clusterA - track{i}" for i in range(3)}
    walk = index.greedy_walk("clusterA - track0", length=20, candidates=candidates)
    # Only 3 in cluster A, so walk caps at 3 even when length=20
    assert len(walk) == 3


def test_empty_index_returns_no_results(tmp_path):
    store = EmbeddingStore(tmp_path)
    index = SmartQueueIndex(store)
    assert len(index) == 0
    assert index.nearest("anything", k=5) == []
    assert index.greedy_walk("anything", length=10) == []
