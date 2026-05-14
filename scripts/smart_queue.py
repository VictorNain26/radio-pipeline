#!/usr/bin/env python3
"""
Smart sequencing based on CLAP audio embeddings + FAISS similarity.

Two modes:

  - CLI lookup mode:
      python scripts/smart_queue.py similar "Beach House - Space Song" [-k 5]
      python scripts/smart_queue.py walk    "Beach House - Space Song" -n 10

  - Library mode (called from classify.py once Phase D wires it in):
      from smart_queue import build_smart_walk
      ordered = build_smart_walk(seed_track_key, candidates, length=15)

The "walk" mode does greedy nearest-neighbour traversal restricted to
a candidate set (typically the tracks in one daypart), starting from
a seed and never revisiting. Result: a sequence with smooth audio
transitions, no abrupt mood/tempo jumps. This is what makes
config.SEPARATION.tempo_max_variance + mood_min_separation become
*emergent* from the embedding geometry instead of being heuristic.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from audio_embeddings import EmbeddingStore  # noqa: E402

logger = logging.getLogger(__name__)


class SmartQueueIndex:
    """In-memory FAISS index built from an EmbeddingStore."""

    def __init__(self, store: EmbeddingStore):
        self.store = store
        self._faiss_index = None
        self._track_keys: list[str] = []
        self._rebuild()

    def _rebuild(self) -> None:
        keys, emb = self.store.all()
        if emb is None or len(keys) == 0:
            self._faiss_index = None
            self._track_keys = []
            return
        try:
            import faiss
        except ImportError:
            logger.error("faiss-cpu is required for SmartQueueIndex")
            return
        # Inner product on L2-normalised vectors == cosine similarity.
        d = emb.shape[1]
        index = faiss.IndexFlatIP(d)
        index.add(emb.astype(np.float32))
        self._faiss_index = index
        self._track_keys = list(keys)

    def __len__(self) -> int:
        return len(self._track_keys)

    def nearest(self, seed_key: str, k: int = 5, restrict_to: set[str] | None = None) -> list[tuple[str, float]]:
        """
        Returns up to k nearest neighbours to seed_key (excluding the seed itself),
        each as (track_key, similarity ∈ [-1, 1]).

        restrict_to: if provided, only consider these track_keys.
        """
        if self._faiss_index is None or seed_key not in self._track_keys:
            return []
        seed_idx = self._track_keys.index(seed_key)
        seed_emb = self.store.get(seed_key)
        if seed_emb is None:
            return []

        # Over-fetch then filter
        search_k = min(len(self._track_keys), k * 4 + 1)
        scores, idxs = self._faiss_index.search(
            seed_emb.reshape(1, -1).astype(np.float32),
            search_k,
        )
        results: list[tuple[str, float]] = []
        for i, s in zip(idxs[0], scores[0]):
            if i == seed_idx or i < 0:
                continue
            tk = self._track_keys[i]
            if restrict_to is not None and tk not in restrict_to:
                continue
            results.append((tk, float(s)))
            if len(results) >= k:
                break
        return results

    def greedy_walk(self, seed_key: str, length: int, candidates: set[str] | None = None) -> list[str]:
        """
        Greedy nearest-neighbour walk starting from `seed_key`.

        At each step, picks the unused track in `candidates` closest to the
        current track. Returns at most `length` track_keys including the seed.
        """
        if self._faiss_index is None or seed_key not in self._track_keys:
            return []
        used = {seed_key}
        walk = [seed_key]
        current = seed_key
        while len(walk) < length:
            neighbours = self.nearest(
                current, k=length, restrict_to=candidates,
            )
            picked = None
            for tk, _score in neighbours:
                if tk not in used:
                    picked = tk
                    break
            if picked is None:
                break
            walk.append(picked)
            used.add(picked)
            current = picked
        return walk


def build_smart_walk(
    seed_track_key: str,
    candidate_track_keys: list[str],
    length: int,
    data_dir: Path | None = None,
) -> list[str]:
    """High-level entry: build a smart walk from an embedding store on disk."""
    if data_dir is None:
        data_dir = Path(__file__).parent.parent / "data"
    store = EmbeddingStore(data_dir)
    index = SmartQueueIndex(store)
    return index.greedy_walk(
        seed_track_key,
        length=length,
        candidates=set(candidate_track_keys),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_similar(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir or (Path(__file__).parent.parent / "data"))
    store = EmbeddingStore(data_dir)
    index = SmartQueueIndex(store)
    if len(index) == 0:
        print("Embedding store empty — enable CLAP_EMBEDDING in config and run analyze.py first.")
        return 1
    results = index.nearest(args.seed, k=args.k)
    if not results:
        print(f"No neighbours found for {args.seed!r}")
        return 1
    print(f"Top {len(results)} similar to {args.seed!r}:")
    for tk, score in results:
        print(f"  {score:+.3f}  {tk}")
    return 0


def _cmd_walk(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir or (Path(__file__).parent.parent / "data"))
    store = EmbeddingStore(data_dir)
    index = SmartQueueIndex(store)
    if len(index) == 0:
        print("Embedding store empty.")
        return 1
    walk = index.greedy_walk(args.seed, length=args.n)
    if not walk:
        print(f"Cannot walk from {args.seed!r}")
        return 1
    print(f"Smart walk from {args.seed!r} ({len(walk)} tracks):")
    for i, tk in enumerate(walk):
        print(f"  {i+1:2d}. {tk}")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir or (Path(__file__).parent.parent / "data"))
    store = EmbeddingStore(data_dir)
    keys, emb = store.all()
    print(f"Embedding store at {data_dir}")
    print(f"  tracks indexed : {len(keys)}")
    if emb is not None:
        print(f"  matrix shape   : {emb.shape}")
        print(f"  matrix dtype   : {emb.dtype}")
        print(f"  on-disk size   : {(data_dir / 'embeddings.npy').stat().st_size / 1024:.1f} KB"
              if (data_dir / 'embeddings.npy').exists() else "  on-disk size   : (not yet flushed)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_similar = sub.add_parser("similar", help="Show k most-similar tracks to a seed")
    p_similar.add_argument("seed", help='Track key, e.g. "beach house - space song"')
    p_similar.add_argument("-k", type=int, default=5)
    p_similar.set_defaults(func=_cmd_similar)

    p_walk = sub.add_parser("walk", help="Greedy nearest-neighbour walk from a seed")
    p_walk.add_argument("seed", help="Track key")
    p_walk.add_argument("-n", type=int, default=10)
    p_walk.set_defaults(func=_cmd_walk)

    p_info = sub.add_parser("info", help="Show embedding store stats")
    p_info.set_defaults(func=_cmd_info)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
