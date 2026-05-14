"""
CLAP audio embeddings for content-based similarity.

We use LAION-CLAP HTSAT-FUSED — the most popular open-source
text↔audio contrastive model (paper: NeurIPS '23). It produces
512-dim L2-normalised embeddings that capture timbre, instrumentation
and overall "sounds-like" similarity, robust across genres and
codecs.

The same embedding space is shared with text prompts, so a future
listener-facing feature can take natural language ("ambient
melancholic 60 BPM") and find matching tracks — that integration is
not exposed yet, but the data is already there.

Inference cost on CPU is ~3-5 s per 30-second clip. We crop to the
middle 30 s of each track for speed and to avoid silence-padded
intros/outros.

Persistence: embeddings are stored in:
  data/embeddings.npy           — (N, 512) float32 numpy array, append-only
  data/embeddings_index.json    — {"track_keys": [...]}  positional index
This keeps the FAISS-friendly layout dead simple. No external DB,
no migration. Rebuild the FAISS index by reading the npy file.

Failure mode is graceful: returns None on any error, caller decides
whether to skip the track or fail.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

CLAP_MODEL_NAME = "laion/clap-htsat-fused"
EMBEDDING_DIM = 512
CLIP_DURATION_SEC = 30  # CLAP's native chunk length
CLAP_SAMPLE_RATE = 48000  # CLAP expects 48 kHz input

# Lazy-loaded singletons (cold-start cost ~5-10 s, then ~3-5 s per inference)
_MODEL: Any = None
_PROCESSOR: Any = None
_MODEL_LOCK = threading.Lock()


def _load_model() -> tuple[Any, Any] | None:
    """Lazy-load the CLAP model + processor (one-time, thread-safe)."""
    global _MODEL, _PROCESSOR
    if _MODEL is not None:
        return _MODEL, _PROCESSOR
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL, _PROCESSOR
        try:
            from transformers import ClapModel, ClapProcessor
        except ImportError as e:
            logger.warning(
                "transformers not installed — CLAP embeddings disabled: %s", e
            )
            return None
        logger.info("Loading CLAP model %s (first-call cold start)…", CLAP_MODEL_NAME)
        try:
            _PROCESSOR = ClapProcessor.from_pretrained(CLAP_MODEL_NAME)
            _MODEL = ClapModel.from_pretrained(CLAP_MODEL_NAME)
            _MODEL.eval()
        except (OSError, RuntimeError) as e:
            logger.error("CLAP model load failed: %s", e)
            return None
        logger.info("CLAP loaded.")
    return _MODEL, _PROCESSOR


def compute_text_embedding(text: str) -> np.ndarray | None:
    """
    Embed an arbitrary text query in the same 512-dim space as the
    audio embeddings. Enables natural-language search like
    "ambient melancholic 60bpm" via cosine similarity against the
    audio side.

    Returns a 512-dim L2-normalised float32 numpy array, or None on
    failure (missing dep, model error).
    """
    loaded = _load_model()
    if loaded is None:
        return None
    model, processor = loaded
    try:
        import torch
    except ImportError:
        return None
    try:
        try:
            inputs = processor(text=text, return_tensors="pt", padding=True)
        except TypeError:
            inputs = processor(texts=text, return_tensors="pt", padding=True)
        with torch.no_grad():
            out = model.get_text_features(**inputs)
            if hasattr(out, "pooler_output") and out.pooler_output is not None:
                pooled = out.pooler_output
            elif isinstance(out, tuple) and len(out) > 1:
                pooled = out[1]
            else:
                pooled = out
            embedding = pooled[0].cpu().numpy().astype(np.float32)
    except (RuntimeError, ValueError, AttributeError) as e:
        logger.warning("CLAP text inference failed: %s", e)
        return None
    norm = float(np.linalg.norm(embedding))
    if norm == 0.0:
        return None
    return embedding / norm


def compute_embedding(audio_path: Path) -> np.ndarray | None:
    """
    Returns a 512-dim L2-normalised float32 numpy array, or None on
    any failure (missing dep, decode error, model crash).
    """
    loaded = _load_model()
    if loaded is None:
        return None
    model, processor = loaded

    try:
        import torch
    except ImportError as e:
        logger.warning("torch missing: %s", e)
        return None
    try:
        import librosa
    except ImportError as e:
        logger.warning("librosa missing: %s", e)
        return None

    # librosa handles every audio codec via ffmpeg/soundfile transparently.
    # torchaudio 2.11+ requires torchcodec and is a moving target — keep
    # the lighter dep we already declare in requirements.txt.
    try:
        waveform_np, _sr = librosa.load(
            str(audio_path),
            sr=CLAP_SAMPLE_RATE,
            mono=True,
            duration=CLIP_DURATION_SEC * 2 + 10,  # over-read a bit for midpoint trim
        )
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning("librosa.load failed for %s: %s", audio_path, e)
        return None

    # Middle 30 s slice (faster + skips silent intros / fade-outs)
    samples_needed = CLAP_SAMPLE_RATE * CLIP_DURATION_SEC
    if waveform_np.shape[0] >= samples_needed:
        start = (waveform_np.shape[0] - samples_needed) // 2
        waveform_np = waveform_np[start: start + samples_needed]
    # Shorter clips are passed as-is — CLAP pads internally.

    try:
        # transformers 5.x renamed the kwarg `audios` -> `audio`.
        try:
            inputs = processor(
                audio=waveform_np,
                sampling_rate=CLAP_SAMPLE_RATE,
                return_tensors="pt",
            )
        except TypeError:
            inputs = processor(
                audios=waveform_np,
                sampling_rate=CLAP_SAMPLE_RATE,
                return_tensors="pt",
            )
        with torch.no_grad():
            # transformers 5.x: get_audio_features returns a
            # BaseModelOutputWithPooling whose `.pooler_output` is the
            # 512-dim audio-side contrastive embedding (already projected
            # via the model's audio_projection head).
            out = model.get_audio_features(**inputs)
            if hasattr(out, "pooler_output") and out.pooler_output is not None:
                pooled = out.pooler_output
            elif isinstance(out, tuple) and len(out) > 1:
                pooled = out[1]
            else:
                logger.warning("CLAP output shape unexpected: %s", type(out).__name__)
                return None
            embedding = pooled[0].cpu().numpy().astype(np.float32)
    except (RuntimeError, ValueError, AttributeError) as e:
        logger.warning("CLAP inference failed: %s", e)
        return None

    # L2-normalise for cosine similarity == inner product (FAISS-friendly)
    norm = float(np.linalg.norm(embedding))
    if norm == 0.0:
        return None
    return embedding / norm


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class EmbeddingStore:
    """
    Append-only numpy-backed store keyed by track_key.

    Storage:
      embeddings.npy           — (N, 512) float32
      embeddings_index.json    — {"track_keys": [k0, k1, ...]}
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.npy_path = self.data_dir / "embeddings.npy"
        self.idx_path = self.data_dir / "embeddings_index.json"
        self._lock = threading.Lock()
        self._track_keys, self._embeddings = self._load()

    def _load(self) -> tuple[list[str], np.ndarray | None]:
        if self.idx_path.exists():
            try:
                idx = json.loads(self.idx_path.read_text(encoding="utf-8"))
                track_keys = idx.get("track_keys", [])
            except (json.JSONDecodeError, OSError):
                track_keys = []
        else:
            track_keys = []

        if self.npy_path.exists() and track_keys:
            try:
                emb = np.load(self.npy_path)
            except (OSError, ValueError):
                emb = None
        else:
            emb = None
        return track_keys, emb

    def _flush(self) -> None:
        # Atomic-ish: write to .tmp then rename.
        # np.save() auto-appends ".npy" to string paths — pass a file
        # handle so it leaves our tmp path alone.
        tmp_npy = self.npy_path.with_name(self.npy_path.name + ".tmp")
        tmp_idx = self.idx_path.with_name(self.idx_path.name + ".tmp")
        if self._embeddings is not None and len(self._embeddings):
            with open(tmp_npy, "wb") as fh:
                np.save(fh, self._embeddings, allow_pickle=False)
            tmp_npy.replace(self.npy_path)
        tmp_idx.write_text(
            json.dumps({"track_keys": self._track_keys}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_idx.replace(self.idx_path)

    def has(self, track_key: str) -> bool:
        return track_key in self._track_keys

    def add(self, track_key: str, embedding: np.ndarray) -> None:
        """Append or replace one embedding."""
        with self._lock:
            if track_key in self._track_keys:
                # Overwrite in place
                i = self._track_keys.index(track_key)
                if self._embeddings is None:
                    self._embeddings = embedding.reshape(1, -1).astype(np.float32)
                else:
                    self._embeddings[i] = embedding.astype(np.float32)
            else:
                row = embedding.reshape(1, -1).astype(np.float32)
                if self._embeddings is None or len(self._embeddings) == 0:
                    self._embeddings = row
                else:
                    self._embeddings = np.vstack([self._embeddings, row])
                self._track_keys.append(track_key)
            self._flush()

    def get(self, track_key: str) -> np.ndarray | None:
        if track_key not in self._track_keys or self._embeddings is None:
            return None
        return self._embeddings[self._track_keys.index(track_key)]

    def all(self) -> tuple[list[str], np.ndarray | None]:
        return list(self._track_keys), self._embeddings

    def prune(self, valid_keys: set[str]) -> int:
        """Drop entries whose track_key is no longer in valid_keys. Returns count removed."""
        with self._lock:
            if not self._track_keys or self._embeddings is None:
                return 0
            keep_idx = [i for i, k in enumerate(self._track_keys) if k in valid_keys]
            removed = len(self._track_keys) - len(keep_idx)
            if removed == 0:
                return 0
            self._track_keys = [self._track_keys[i] for i in keep_idx]
            self._embeddings = self._embeddings[keep_idx]
            self._flush()
            return removed
