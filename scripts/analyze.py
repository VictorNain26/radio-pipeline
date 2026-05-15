#!/usr/bin/env python3
"""
Audio Analysis Script v3.0 - MTG Arousal-Valence Models
=======================================================

Classification basée sur le modèle Russell Circumplex (8 moods).

Utilise un ensemble de 3 modèles MTG (DEAM, emoMusic, MuSe) pour
prédire directement arousal et valence avec ~88% de précision.

Moods: Energetic, Excited, Intense, Angry, Melancholic, Sad, Calm, Relaxed
"""

import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
import numpy as np

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Paths
SCRIPT_DIR = Path(__file__).parent
PIPELINE_DIR = SCRIPT_DIR.parent
MODELS_DIR = PIPELINE_DIR / "models"

# Add parent to path for config import
sys.path.insert(0, str(PIPELINE_DIR))

try:
    from config import MoodCategory, EnergyLevel, SPEECH_FILTER, CLAP as CLAP_CFG
except ImportError:
    logger.error("Failed to import config.py - ensure it exists in %s", PIPELINE_DIR)
    sys.exit(1)


# Module-level stats accumulated during a single analyze.py invocation
# (no concurrency — process_file is called sequentially). Flushed to
# data/last_analyze_stats.json at the end of main() so run.sh can fold
# the counts into pipeline_stats.json.
_ANALYZE_STATS: dict[str, int] = {
    "analyzed_ok": 0,        # process_file returned True
    "analysis_failed": 0,    # analyze_audio returned None (no mood/tags written)
    "rejected_speech": 0,    # voice_probability over SPEECH_FILTER.max_voice_probability
    "clap_succeeded": 0,     # CLAP embedding computed + stored this run
    "clap_cached": 0,        # CLAP embedding already in store (skip recompute, not a failure)
    "clap_failed": 0,        # CLAP enabled but embedding returned None / import failed
}


@dataclass
class AudioFeatures:
    """Complete audio analysis features."""
    bpm: int = 0
    duration: int = 0
    mood: str = ""
    confidence: float = 0.0
    energy_level: str = ""
    valence: float = 0.0      # -1 (negative) to +1 (positive)
    arousal: float = 0.0      # -1 (calm) to +1 (energetic)
    # Raw model outputs for debugging
    valence_raw: float = 0.0  # Original 1-9 scale
    arousal_raw: float = 0.0  # Original 1-9 scale
    # Multi-signal filtering (discogs-effnet)
    mood_aggressive: float = 0.0   # 0-1 probability from binary classifier
    genre_top: str = ""            # Top genre from discogs400
    genre_top_prob: float = 0.0    # Probability of top genre
    voice_probability: float = 0.0 # 0-1 probability of voice/speech (vs. instrumental)
    lastfm_tags: str = ""          # Raw Last.fm tags (comma-separated)


# Required models
REQUIRED_MODELS = [
    "msd-musicnn-1.pb",           # MusiCNN embedding extractor
    "deam-msd-musicnn-2.pb",      # DEAM arousal-valence
    "emomusic-msd-musicnn-2.pb",  # emoMusic arousal-valence
    "muse-msd-musicnn-2.pb",      # MuSe arousal-valence
    "discogs-effnet-bs64-1.pb",               # Discogs-EffNet embedding
    "mood_aggressive-discogs-effnet-1.pb",    # Mood aggressive binary classifier
    "genre_discogs400-discogs-effnet-1.pb",   # Genre 400 classifier
    "voice_instrumental-discogs-effnet-1.pb", # Voice vs. instrumental classifier
]


def check_dependencies() -> bool:
    """Check if required dependencies are installed."""
    try:
        import essentia.standard  # noqa: F401
        from mutagen.id3 import ID3  # noqa: F401
        return True
    except ImportError as e:
        logger.error("Missing dependency: %s", e)
        logger.error("Install with: pip install essentia-tensorflow mutagen")
        return False


def check_models() -> bool:
    """Check if required models are downloaded."""
    missing = [m for m in REQUIRED_MODELS if not (MODELS_DIR / m).exists()]
    if missing:
        logger.error("Missing models: %s", ", ".join(missing))
        logger.error("Run: ./scripts/download_models.sh")
        return False
    return True


def read_existing_tags(filepath: str) -> tuple[str | None, str | None]:
    """Read artist and title from existing ID3 tags."""
    try:
        from mutagen import MutagenError
        from mutagen.id3 import ID3
        tags = ID3(filepath)
        artist = str(tags.get('TPE1', ['Unknown'])[0])
        title = str(tags.get('TIT2', ['Unknown'])[0])
        return artist, title
    except (MutagenError, OSError):
        return None, None


def parse_filename(filepath: str) -> tuple[str, str]:
    """Fallback: extract artist and title from filename."""
    name = Path(filepath).stem
    if " - " in name:
        parts = name.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return "Unknown", name.strip()


def get_energy_level(arousal: float) -> str:
    """
    Determine energy level from arousal.

    Args:
        arousal: Arousal score (-1 to 1)

    Returns:
        Energy level string.
    """
    if arousal < -0.6:
        return EnergyLevel.VERY_LOW
    elif arousal < -0.2:
        return EnergyLevel.LOW
    elif arousal < 0.2:
        return EnergyLevel.MEDIUM
    elif arousal < 0.6:
        return EnergyLevel.HIGH
    else:
        return EnergyLevel.VERY_HIGH


def classify_mood(valence: float, arousal: float) -> tuple[str, float]:
    """
    Classify mood using the Russell circumplex model (8 categories).

    The circumplex is divided into 8 sectors:
    - Energetic: high valence, medium-high arousal (right)
    - Excited: high valence, high arousal (upper right)
    - Intense: medium valence, very high arousal (top)
    - Angry: low valence, high arousal (upper left)
    - Melancholic: low valence, medium arousal (left)
    - Sad: low valence, low arousal (lower left)
    - Calm: medium valence, very low arousal (bottom)
    - Relaxed: high valence, low arousal (lower right)

    Args:
        valence: Valence score (-1 to 1)
        arousal: Arousal score (-1 to 1)

    Returns:
        Tuple of (mood_name, confidence)
    """
    # Calculate angle in the circumplex (0-360 degrees)
    angle_rad = math.atan2(arousal, valence)
    angle_deg = math.degrees(angle_rad)
    if angle_deg < 0:
        angle_deg += 360

    # Distance from center (confidence proxy)
    distance = math.sqrt(valence**2 + arousal**2)
    distance = min(1.0, distance)

    # Map angle to mood category (8 sectors of 45° each)
    sector = int((angle_deg + 22.5) % 360 / 45)

    sector_moods = [
        MoodCategory.ENERGETIC,    # 0: 337.5° - 22.5° (right)
        MoodCategory.EXCITED,      # 1: 22.5° - 67.5° (upper right)
        MoodCategory.INTENSE,      # 2: 67.5° - 112.5° (top)
        MoodCategory.ANGRY,        # 3: 112.5° - 157.5° (upper left)
        MoodCategory.MELANCHOLIC,  # 4: 157.5° - 202.5° (left)
        MoodCategory.SAD,          # 5: 202.5° - 247.5° (lower left)
        MoodCategory.CALM,         # 6: 247.5° - 292.5° (bottom)
        MoodCategory.RELAXED,      # 7: 292.5° - 337.5° (lower right)
    ]

    mood = sector_moods[sector]

    # Confidence based on distance from center
    # Further from center = more confident classification
    # MTG models are more reliable, so base confidence is higher
    confidence = 0.65 + (distance * 0.30)

    # Boost for extreme values
    if abs(valence) > 0.7 or abs(arousal) > 0.7:
        confidence += 0.05

    confidence = min(0.98, confidence)

    return mood, round(confidence, 3)


def analyze_discogs_effnet(audio_16k: "np.ndarray") -> tuple[float, str, float, float]:
    """
    Run all three discogs-effnet classification heads from a single
    embedding pass:
      - mood_aggressive (binary)
      - genre_discogs400 (400-way)
      - voice_instrumental (binary, voice probability)

    Sharing the backbone (one EffNet forward pass) costs ~1s/track
    vs. ~3s if we re-loaded the backbone for each head.

    Returns:
        Tuple of (mood_aggressive_prob, genre_top, genre_top_prob, voice_prob).
    """
    from essentia.standard import TensorflowPredict2D, TensorflowPredictEffnetDiscogs

    # Backbone — extract embeddings once
    embedding_model = TensorflowPredictEffnetDiscogs(
        graphFilename=str(MODELS_DIR / "discogs-effnet-bs64-1.pb"),
        output="PartitionedCall:1",
    )
    embeddings = embedding_model(audio_16k)

    # Head 1: mood_aggressive
    aggressive_model = TensorflowPredict2D(
        graphFilename=str(MODELS_DIR / "mood_aggressive-discogs-effnet-1.pb"),
        input="model/Placeholder",
        output="model/Softmax",
    )
    aggressive_preds = aggressive_model(embeddings)
    # class[0] = aggressive probability (Essentia convention: [aggressive, not_aggressive])
    mood_aggressive = float(np.mean(aggressive_preds[:, 0]))

    # Head 2: genre_discogs400
    genre_model = TensorflowPredict2D(
        graphFilename=str(MODELS_DIR / "genre_discogs400-discogs-effnet-1.pb"),
        input="serving_default_model_Placeholder",
        output="PartitionedCall:0",
    )
    genre_preds = genre_model(embeddings)
    genre_avg = np.mean(genre_preds, axis=0)
    genre_idx = int(np.argmax(genre_avg))
    genre_top_prob = float(genre_avg[genre_idx])
    genre_labels = _get_genre_discogs400_labels()
    genre_top = genre_labels[genre_idx] if genre_idx < len(genre_labels) else f"genre_{genre_idx}"

    # Head 3: voice_instrumental — catches podcast episodes / interviews
    # that sneak in via RSS discovery (e.g. "An Interview with X").
    voice_model = TensorflowPredict2D(
        graphFilename=str(MODELS_DIR / "voice_instrumental-discogs-effnet-1.pb"),
        input="model/Placeholder",
        output="model/Softmax",
    )
    voice_preds = voice_model(embeddings)
    # class[0] = voice probability (Essentia convention: [voice, instrumental])
    voice_prob = float(np.mean(voice_preds[:, 0]))

    return mood_aggressive, genre_top, genre_top_prob, voice_prob


# Genre labels for discogs400 (top-level from Discogs taxonomy)
# Full 400-label list is large; we use a mapping file or inline the common ones.
# The model outputs 400 classes following the Discogs taxonomy.
_GENRE_LABELS_CACHE: list[str] | None = None


def _get_genre_discogs400_labels() -> list[str]:
    """Load or return cached genre labels for discogs400 model."""
    global _GENRE_LABELS_CACHE
    if _GENRE_LABELS_CACHE is not None:
        return _GENRE_LABELS_CACHE

    # Try loading from metadata file next to model
    labels_path = MODELS_DIR / "genre_discogs400-discogs-effnet-1.json"
    if labels_path.exists():
        import json
        with open(labels_path) as f:
            data = json.load(f)
            _GENRE_LABELS_CACHE = data.get("classes", [f"genre_{i}" for i in range(400)])
            return _GENRE_LABELS_CACHE

    # Essentia provides metadata with genre names via the model's metadata
    # Fallback: use essentia's built-in metadata if available
    try:
        from essentia.standard import TensorflowPredict2D
        model = TensorflowPredict2D(
            graphFilename=str(MODELS_DIR / "genre_discogs400-discogs-effnet-1.pb"),
            input="model/Placeholder",
            output="model/PartitionedCall:0",
        )
        # Try to get metadata
        metadata_str = model.paramValue("metadata") if hasattr(model, "paramValue") else ""
        if metadata_str:
            import json
            metadata = json.loads(metadata_str)
            _GENRE_LABELS_CACHE = metadata.get("classes", [])
            if _GENRE_LABELS_CACHE:
                return _GENRE_LABELS_CACHE
    except Exception:
        pass

    # Final fallback: numbered labels
    _GENRE_LABELS_CACHE = [f"genre_{i}" for i in range(400)]
    return _GENRE_LABELS_CACHE


def analyze_audio(filepath: str) -> AudioFeatures | None:
    """
    Analyze audio using MTG arousal-valence ensemble models.

    Uses 3 models (DEAM, emoMusic, MuSe) and averages their predictions
    for more robust arousal/valence estimation (~88% accuracy).

    Args:
        filepath: Path to audio file.

    Returns:
        AudioFeatures or None on error.
    """
    from essentia.standard import (
        MonoLoader,
        RhythmExtractor2013,
        TensorflowPredict2D,
        TensorflowPredictMusiCNN,
    )

    try:
        # Load audio at 16kHz for arousal-valence models
        audio = MonoLoader(filename=filepath, sampleRate=16000, resampleQuality=4)()
        duration = int(len(audio) / 16000)

        # Load audio at 44.1kHz for BPM detection
        audio_44k = MonoLoader(filename=filepath, sampleRate=44100)()

        # Extract BPM
        rhythm_extractor = RhythmExtractor2013(method="multifeature")
        bpm, *_ = rhythm_extractor(audio_44k)
        bpm = int(round(bpm))

        # Load MusiCNN embedding model
        embedding_model = TensorflowPredictMusiCNN(
            graphFilename=str(MODELS_DIR / "msd-musicnn-1.pb"),
            output="model/dense/BiasAdd"
        )
        embeddings = embedding_model(audio)

        # Load arousal-valence models (ensemble of 3)
        av_models = {
            "deam": TensorflowPredict2D(
                graphFilename=str(MODELS_DIR / "deam-msd-musicnn-2.pb"),
                output="model/Identity"
            ),
            "emomusic": TensorflowPredict2D(
                graphFilename=str(MODELS_DIR / "emomusic-msd-musicnn-2.pb"),
                output="model/Identity"
            ),
            "muse": TensorflowPredict2D(
                graphFilename=str(MODELS_DIR / "muse-msd-musicnn-2.pb"),
                output="model/Identity"
            ),
        }

        # Get predictions from each model and average (ensemble)
        all_valence = []
        all_arousal = []

        for name, model in av_models.items():
            predictions = model(embeddings)
            # Output: [valence, arousal] per frame, values in [1, 9]
            valence_raw = float(np.mean(predictions[:, 0]))
            arousal_raw = float(np.mean(predictions[:, 1]))
            all_valence.append(valence_raw)
            all_arousal.append(arousal_raw)

        # Average predictions (ensemble)
        valence_raw = np.mean(all_valence)
        arousal_raw = np.mean(all_arousal)

        # Convert from [1, 9] to [-1, 1] scale
        valence = (valence_raw - 5) / 4  # 1→-1, 5→0, 9→+1
        arousal = (arousal_raw - 5) / 4

        # Clamp to valid range
        valence = max(-1.0, min(1.0, valence))
        arousal = max(-1.0, min(1.0, arousal))

        # Classify mood
        mood, confidence = classify_mood(valence, arousal)

        # Get energy level
        energy_level = get_energy_level(arousal)

        # Convert enums to string values if needed
        mood_str = mood.value if hasattr(mood, 'value') else str(mood)
        energy_str = energy_level.value if hasattr(energy_level, 'value') else str(energy_level)

        # Discogs-EffNet analysis (mood_aggressive + genre + voice/instrumental).
        # No silent fallback: if this fails we return None so process_file
        # rejects the track. Letting it through with voice_prob=0 would
        # silently bypass the speech filter — that's exactly the kind of
        # silent-pass behaviour we want to avoid.
        try:
            mood_aggressive, genre_top, genre_top_prob, voice_prob = analyze_discogs_effnet(audio)
        except (RuntimeError, OSError, ValueError) as e:
            logger.error("  Discogs-EffNet analysis FAILED: %s — rejecting track", e)
            return None

        return AudioFeatures(
            bpm=bpm,
            duration=duration,
            mood=mood_str,
            confidence=confidence,
            energy_level=energy_str,
            valence=round(valence, 3),
            arousal=round(arousal, 3),
            valence_raw=round(valence_raw, 2),
            arousal_raw=round(arousal_raw, 2),
            mood_aggressive=round(mood_aggressive, 4),
            genre_top=genre_top,
            genre_top_prob=round(genre_top_prob, 4),
            voice_probability=round(voice_prob, 4),
        )

    except (RuntimeError, OSError, ValueError) as e:
        logger.error("  Analysis error: %s", e)
        return None


def write_tags(filepath: str, artist: str, title: str, features: AudioFeatures) -> bool:
    """Write metadata and analysis features as ID3 tags."""
    try:
        from mutagen import MutagenError
        from mutagen.id3 import ID3, ID3NoHeaderError, TBPM, TIT2, TPE1, TXXX

        try:
            tags = ID3(filepath)
        except ID3NoHeaderError:
            tags = ID3()

        # Basic metadata
        tags.delall('TPE1')
        tags.add(TPE1(encoding=3, text=artist))

        tags.delall('TIT2')
        tags.add(TIT2(encoding=3, text=title))

        # BPM
        tags.delall('TBPM')
        tags.add(TBPM(encoding=3, text=str(features.bpm)))

        # Custom analysis tags
        custom_tags = {
            'MOOD': features.mood,
            'MOOD_CONFIDENCE': str(features.confidence),
            'ENERGY_LEVEL': features.energy_level,
            'VALENCE': str(features.valence),
            'AROUSAL': str(features.arousal),
            'DURATION': str(features.duration),
            'MOOD_AGGRESSIVE': str(features.mood_aggressive),
            'GENRE_TOP': features.genre_top,
            'GENRE_TOP_PROB': str(features.genre_top_prob),
            'LASTFM_TAGS': features.lastfm_tags,
        }

        for tag_name, value in custom_tags.items():
            tags.delall(f'TXXX:{tag_name}')
            tags.add(TXXX(encoding=3, desc=tag_name, text=str(value)))

        tags.save(filepath)
        return True
    except (MutagenError, OSError) as e:
        logger.error("  Failed to write tags: %s", e)
        return False


def process_file(filepath: str) -> bool:
    """
    Process a single audio file.

    Args:
        filepath: Path to audio file.

    Returns:
        True if successful.
    """
    filename = Path(filepath).name
    logger.info("\n%s", filename)

    if not Path(filepath).exists():
        logger.error("  ERROR: File not found")
        return False

    # Read metadata
    artist, title = read_existing_tags(filepath)
    if not artist or artist == "Unknown":
        artist, title = parse_filename(filepath)
        logger.info("  Metadata: %s - %s (from filename)", artist, title)
    else:
        logger.info("  Metadata: %s - %s", artist, title)

    # Analyze audio
    logger.info("  Analyzing (MTG arousal-valence ensemble)...")
    features = analyze_audio(filepath)

    if not features:
        logger.error("  ERROR: Analysis failed")
        _ANALYZE_STATS["analysis_failed"] += 1
        return False

    # Speech filter — reject podcasts / interviews caught by RSS discovery.
    # Done before tag writing so the file is removed before classify.py
    # ever sees it.
    if (
        SPEECH_FILTER.enabled
        and features.voice_probability > SPEECH_FILTER.max_voice_probability
    ):
        logger.info(
            "  REJECTED (speech-heavy: %.0f%% voice > %.0f%% threshold) — deleting",
            features.voice_probability * 100,
            SPEECH_FILTER.max_voice_probability * 100,
        )
        try:
            Path(filepath).unlink()
        except OSError as e:
            logger.warning("  Could not delete rejected file: %s", e)
        _ANALYZE_STATS["rejected_speech"] += 1
        return False

    # Display results
    duration_min = features.duration // 60
    duration_sec = features.duration % 60

    logger.info("  Duration: %s:%02d | BPM: %s", duration_min, duration_sec, features.bpm)
    logger.info("  Valence: %+.2f | Arousal: %+.2f", features.valence, features.arousal)
    logger.info("  => Mood: %s (%.0f%%)", features.mood, features.confidence * 100)
    logger.info("  => Energy: %s", features.energy_level)
    logger.info("  => Voice: %.0f%% (instrumental: %.0f%%)",
                features.voice_probability * 100,
                (1 - features.voice_probability) * 100)
    if features.mood_aggressive > 0:
        logger.info("  => Aggressive: %.2f | Genre: %s (%.2f)", features.mood_aggressive, features.genre_top, features.genre_top_prob)

    # Write tags
    if not write_tags(filepath, artist, title, features):
        return False

    # CLAP embedding (opt-in via config.CLAP.enabled).
    # Stored separately in data/embeddings.{npy,index.json} for later use
    # by scripts/smart_queue.py — no impact on the rest of the pipeline.
    if CLAP_CFG.enabled:
        try:
            from audio_embeddings import EmbeddingStore, compute_embedding
            from track_db import normalize_track_key

            track_key = normalize_track_key(artist, title)
            store = EmbeddingStore(PIPELINE_DIR / "data")
            if store.has(track_key):
                logger.info("  CLAP: cached (already indexed)")
                _ANALYZE_STATS["clap_cached"] += 1
            else:
                emb = compute_embedding(Path(filepath))
                if emb is not None:
                    store.add(track_key, emb)
                    logger.info("  CLAP: embedding stored (%d-dim)", emb.shape[0])
                    _ANALYZE_STATS["clap_succeeded"] += 1
                else:
                    logger.warning("  CLAP: embedding failed (non-fatal)")
                    _ANALYZE_STATS["clap_failed"] += 1
        except ImportError as e:
            logger.warning("  CLAP integration unavailable (%s); set CLAP.enabled=False to silence", e)
            _ANALYZE_STATS["clap_failed"] += 1

    return True


def main() -> int:
    """Main entry point."""
    if not check_dependencies():
        return 1

    if not check_models():
        return 1

    # Get files to process
    if len(sys.argv) >= 2:
        files = [Path(f) for f in sys.argv[1:]]
    else:
        downloads_dir = PIPELINE_DIR / "downloads"

        if not downloads_dir.exists():
            logger.error("No downloads folder found")
            return 1

        files = list(downloads_dir.glob("*.mp3"))

    if not files:
        logger.info("No MP3 files found")
        return 0

    logger.info("=== Audio Analysis v3.0 (MTG Arousal-Valence Ensemble) ===")
    logger.info("Files: %d", len(files))
    logger.info("Models: DEAM + emoMusic + MuSe (~88% accuracy)")
    logger.info("Moods: Energetic, Excited, Intense, Angry, Melancholic, Sad, Calm, Relaxed")

    success_count = 0
    for filepath in sorted(files):
        if process_file(str(filepath)):
            success_count += 1
            _ANALYZE_STATS["analyzed_ok"] += 1

    logger.info("\n=== Done (%d/%d successful) ===", success_count, len(files))
    if _ANALYZE_STATS["rejected_speech"]:
        logger.info("Speech-rejected : %d", _ANALYZE_STATS["rejected_speech"])
    if _ANALYZE_STATS["clap_cached"]:
        logger.info("CLAP cached (already indexed) : %d", _ANALYZE_STATS["clap_cached"])
    if _ANALYZE_STATS["clap_failed"]:
        logger.warning("CLAP embedding failures : %d", _ANALYZE_STATS["clap_failed"])

    # Persist stats for run.sh to fold into pipeline_stats.json
    stats_path = PIPELINE_DIR / "data" / "last_analyze_stats.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import json as _json
        stats_path.write_text(_json.dumps(_ANALYZE_STATS, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write last_analyze_stats.json: %s", e)

    # Speech rejection is an intentional outcome, not a failure. Only true
    # analysis errors (analyze_audio returned None) should trip exit 1 so
    # run.sh's [WARN] alerts stay meaningful.
    return 1 if _ANALYZE_STATS["analysis_failed"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
