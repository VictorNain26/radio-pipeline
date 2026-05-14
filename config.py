"""
AubeSonore Radio Configuration v2.0
===================================

Architecture basée sur les meilleures pratiques 2026 :
- Modèle Russell Circumplex (8 moods en 2D valence-arousal)
- Dayparting granulaire (8 segments)
- Règles de séparation professionnelles
- Filtres audio configurables
- Structure flexible et extensible

Références:
- Russell, J.A. (1980). A circumplex model of affect.
- Soundcharts AI Music Analysis 2026
- Music 1 / MusicMaster scheduling best practices
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

# Constants
AVERAGE_TRACK_DURATION_MINUTES = 3.5


# =============================================================================
# ENUMS - Types de base
# =============================================================================

class MoodCategory(str, Enum):
    """
    8 catégories de mood basées sur le modèle circumplex de Russell.
    Positionnées à 45° d'intervalle sur le plan valence-arousal.

    Arousal (vertical) : niveau d'énergie/activation
    Valence (horizontal) : positif/négatif
    """
    ENERGETIC = "Energetic"      # 0° - High valence, medium-high arousal
    EXCITED = "Excited"          # 45° - High valence, high arousal
    INTENSE = "Intense"          # 90° - Medium valence, very high arousal (peak energy)
    ANGRY = "Angry"              # 135° - Low valence, high arousal
    MELANCHOLIC = "Melancholic"  # 180° - Low valence, medium arousal
    SAD = "Sad"                  # 225° - Low valence, low arousal
    CALM = "Calm"                # 270° - Medium valence, very low arousal
    RELAXED = "Relaxed"          # 315° - High valence, low arousal


class DaypartSegment(str, Enum):
    """
    4 zones de programmation alignées sur les cycles d'attention/lumière —
    et sur l'identité "aube sonore" de la radio.

    Pourquoi 4 (et pas 8) :
    - une webradio découverte n'a pas d'auditeur "en voiture pendant le
      morning commute". L'auditeur est chez lui / au studio / au boulot.
    - Best practice 2026 = "do less, but better" (Radio World). Moins de
      segments = identité plus forte par zone.
    - 597 tracks / 4 zones = ~150 par playlist (vs ~30 par 8e) → meilleur
      shuffle, moins de répétition.

    Chaque zone a son personnage sonore :
      DAWN  — ambient/modern classical/slowcore, réveil doux
      DAY   — indie/electro mid-tempo/hip-hop chill, fond de travail
      DUSK  — dream pop/trip hop/downtempo, transition émotionnelle
      NIGHT — ambient profond/sad/expérimental/intense, introspection
    """
    DAWN = "Dawn"     # 05:00-09:00 - Réveil, basse énergie, lumière qui se lève
    DAY = "Day"       # 09:00-17:00 - Activité, énergie modérée, fond
    DUSK = "Dusk"     # 17:00-22:00 - Transition, émotionnel, soir
    NIGHT = "Night"   # 22:00-05:00 - Nuit, introspection, intense ou profond


class EnergyLevel(str, Enum):
    """Niveaux d'énergie pour les transitions."""
    VERY_LOW = "very_low"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class DayType(str, Enum):
    """
    Types de jours pour la programmation différenciée.

    Basé sur les études de comportement d'écoute:
    - Semaine: routine, trajets, productivité
    - Vendredi: anticipation weekend, montée d'énergie
    - Samedi: pic de fête, brunch, activités
    - Dimanche: détente, introspection, "sunday blues"
    """
    WEEKDAY = "weekday"      # Lundi-Jeudi
    FRIDAY = "friday"        # Vendredi (transition vers weekend)
    SATURDAY = "saturday"    # Samedi (pic d'énergie/fête)
    SUNDAY = "sunday"        # Dimanche (calme, récupération)


# =============================================================================
# DATACLASSES - Configurations structurées
# =============================================================================

@dataclass
class MoodProfile:
    """
    Profil complet d'un mood avec ses caractéristiques.

    Attributes:
        enabled: Si le mood est actif (sinon tracks rejetées)
        description: Description humaine
        valence: Score valence typique (-1 à 1)
        arousal: Score arousal typique (-1 à 1)
        energy_level: Niveau d'énergie catégorique
        bpm_range: Plage de BPM typique (min, max)
        color: Couleur hex pour visualisation
    """
    enabled: bool = True
    description: str = ""
    valence: float = 0.0  # -1 (négatif) à 1 (positif)
    arousal: float = 0.0  # -1 (calme) à 1 (énergique)
    energy_level: EnergyLevel = EnergyLevel.MEDIUM
    bpm_range: tuple[int, int] = (80, 140)
    color: str = "#808080"


@dataclass
class DaypartProfile:
    """
    Configuration d'un segment de journée.

    Attributes:
        enabled: Si le daypart est actif
        start_hour: Heure de début (0-23)
        end_hour: Heure de fin (0-23)
        description: Description du contexte d'écoute
        target_moods: Moods appropriés pour ce segment
        energy_curve: Niveau d'énergie cible
        transition_flex: Flexibilité pour transitions (0-1)
    """
    enabled: bool = True
    start_hour: int = 0
    end_hour: int = 24
    description: str = ""
    target_moods: list[MoodCategory] = field(default_factory=list)
    energy_curve: EnergyLevel = EnergyLevel.MEDIUM
    transition_flex: float = 0.3  # 0 = strict, 1 = très flexible



@dataclass
class SeparationRules:
    """
    Règles de séparation pour éviter la répétition et le clustering.

    Basé sur les best practices de Music 1 et MusicMaster.

    Attributes:
        artist_min_minutes: Minutes minimum entre même artiste
        title_min_minutes: Minutes minimum entre même titre
        tempo_max_variance: Variance BPM max entre tracks consécutives
        energy_smooth_transition: Forcer transitions douces d'énergie
        mood_min_separation: Tracks minimum avant répétition du même mood
        genre_min_separation: Tracks minimum avant répétition du même genre
    """
    artist_min_minutes: int = 60
    title_min_minutes: int = 180
    tempo_max_variance: int = 25
    energy_smooth_transition: bool = True
    mood_min_separation: int = 3
    genre_min_separation: int = 2


@dataclass
class AudioFilters:
    """
    Filtres audio pour la sélection des tracks.

    Attributes:
        duration_min: Durée minimum en secondes (None = désactivé)
        duration_max: Durée maximum en secondes
        bpm_min: BPM minimum
        bpm_max: BPM maximum
        min_confidence: Score de confiance minimum pour classification (0-1)
        reject_low_quality: Rejeter les tracks avec analyse de faible qualité
    """
    duration_min: int | None = 60       # 1 minute minimum
    duration_max: int | None = 420      # 7 minutes maximum
    bpm_min: int | None = None
    bpm_max: int | None = None
    min_confidence: float = 0.3
    reject_low_quality: bool = True


@dataclass
class RotationConfig:
    """
    Rotation 3-tiers pour webradio de découverte.

    Tiers basés sur l'âge (uploaded_at en SQLite):
      FRESH (0-fresh_days)         : protection totale
      CURRENT (fresh-current_days) : supprimé si library pleine + plays >= seuil
      FADING (current-max_age_days): proactivement réduit à max fading_max_pct%
      EXPIRED (>max_age_days)      : force delete

    Attributes:
        max_tracks: Nombre maximum de tracks en bibliothèque
        fresh_days: Jours de protection totale (FRESH)
        current_days: Fin du tier CURRENT (début FADING)
        max_age_days: Fin du tier FADING (début EXPIRED)
        cooldown_days: Jours de cooldown avant re-download après suppression
        fading_max_pct: Pourcentage max de la library pour les tracks FADING
        min_plays_before_delete: Plays minimum avant suppression d'un CURRENT
    """
    max_tracks: int = 500
    fresh_days: int = 14
    current_days: int = 30
    max_age_days: int = 50
    cooldown_days: int = 60
    fading_max_pct: float = 20.0
    min_plays_before_delete: int = 3


@dataclass
class GenreFilterConfig:
    """
    Configuration du filtrage par genre multi-source.

    Best practices 2026:
    - Filtrage pré-téléchargement pour économiser bande passante
    - Tags agrégés depuis MusicBrainz + Discogs + Last.fm
    - Liste bloquée curated pour l'esthétique AubeSonore
      (indie / electronic / ambient / hip-hop)

    NOTE: "noise" tout court n'est PAS bloqué car Last.fm l'utilise
    largement comme descripteur de texture (shoegaze, dream pop). On
    bloque uniquement les variantes canoniques extrêmes (harsh noise,
    power electronics, japanoise).

    Attributes:
        enabled: Activer le filtrage par genre
        blocked_genres: Genres à bloquer (lowercase, match exact sur tag)
        require_tags: Rejeter les tracks sans tags
    """
    enabled: bool = True
    blocked_genres: tuple[str, ...] = (
        # Metal — toutes variantes (incompatible avec dawn-sound)
        "metal", "death metal", "black metal", "heavy metal",
        "thrash metal", "doom metal", "nu metal", "groove metal",
        "power metal", "speed metal", "progressive metal",
        "sludge metal", "stoner metal", "post-metal",
        "folk metal", "symphonic metal", "viking metal", "djent",
        # Metalcore / hardcore famille
        "grindcore", "metalcore", "deathcore", "mathcore",
        "melodic metalcore", "post-hardcore",
        # Hard rock + glam (guitares saturées, vibe pas dawn-sound)
        "hard rock", "glam metal", "hair metal",
        # Punk extrême (post-punk reste autorisé)
        "hardcore punk", "crust punk", "thrash punk", "d-beat",
        # Industrial agressif (Throbbing Gristle, Skinny Puppy...)
        "industrial", "industrial metal", "industrial rock",
        "aggrotech", "ebm", "death industrial",
        # Hard electronic (BPM > 160, kicks distordus)
        "hardcore", "hardstyle", "hard techno", "industrial techno",
        "gabber", "schranz", "speedcore", "happy hardcore",
        # Noise extrême (PAS "noise" seul — trop large)
        "harsh noise", "power electronics", "japanoise", "noise music",
    )
    require_tags: bool = False  # Si True, rejette les tracks sans tags


@dataclass
class AggressiveAudioFilter:
    """
    Filtre audio intelligent pour bloquer les tracks agressives (metal, hardcore, etc.)
    quand Last.fm n'a pas de tags.

    Best practices 2026:
    - Utilise arousal + valence de l'analyse Essentia comme fallback
    - Arousal élevé + valence négative = son agressif/metal
    - S'applique uniquement si Last.fm n'a pas de tags (artistes obscurs)

    Attributes:
        enabled: Activer le filtre audio intelligent
        arousal_threshold: Seuil arousal au-dessus duquel vérifier (0-1)
        valence_threshold: Seuil valence en-dessous duquel bloquer (-1 à 1)
        block_intense_mood: Bloquer aussi les tracks classées "Intense" ou "Angry"
    """
    enabled: bool = True
    arousal_threshold: float = 0.65  # Arousal > 0.65 = énergique
    valence_threshold: float = -0.2  # Valence < -0.2 = négatif/agressif
    block_intense_mood: bool = True  # Bloquer mood "Intense" et "Angry" sans tags


@dataclass(frozen=True)
class MultiSignalFilterConfig:
    """
    Configuration du filtre multi-signal pour rejeter les morceaux agressifs.

    Utilise 4 signaux indépendants :
    1. mood_aggressive (discogs-effnet, 98% accuracy)
    2. Arousal-Valence (MusiCNN ensemble, ~88%)
    3. Genre ML (genre_discogs400, AUC 0.954)
    4. Last.fm tags (crowd-sourced)

    Règle de rejet : 2+ signaux concordants, OU mood_aggressive > solo_threshold.
    S'applique uniquement aux nouveaux morceaux (ceux avec les tags multi-signal).
    """
    enabled: bool = True
    min_signals_to_reject: int = 2
    aggressive_threshold: float = 0.65
    aggressive_solo_threshold: float = 0.85
    av_arousal_threshold: float = 0.65
    av_valence_threshold: float = -0.2
    genre_blocked: frozenset[str] = frozenset({
        "metal", "punk", "hardcore", "grindcore", "death metal",
        "black metal", "thrash metal", "heavy metal", "industrial",
        "noise", "power metal", "speed metal", "nu metal",
    })
    lastfm_blocked_tags: frozenset[str] = frozenset({
        "aggressive", "metal", "hardcore", "screamo", "brutal",
        "heavy metal", "death metal", "black metal", "grindcore",
        "noise", "industrial", "thrash",
    })


@dataclass
class ClassificationThresholds:
    """
    Seuils pour la classification des moods (legacy - kept for backward compat).
    """
    aggressive_threshold: float = 0.40
    happy_threshold: float = 0.45
    relaxed_threshold: float = 0.45
    sad_threshold: float = 0.45

    # Seuils BPM pour déterminer l'énergie
    bpm_very_slow: int = 80
    bpm_slow: int = 100
    bpm_moderate: int = 115
    bpm_fast: int = 128
    bpm_very_fast: int = 145


# =============================================================================
# CONFIGURATION DES MOODS
# =============================================================================

MOODS: dict[MoodCategory, MoodProfile] = {
    MoodCategory.ENERGETIC: MoodProfile(
        enabled=True,
        description="Joyeux, optimiste, dynamique - parfait pour booster l'énergie",
        valence=0.8,
        arousal=0.5,
        energy_level=EnergyLevel.HIGH,
        bpm_range=(110, 135),
        color="#FFD700",  # Gold
    ),
    MoodCategory.EXCITED: MoodProfile(
        enabled=True,
        description="Exubérant, euphorique, festif - pics d'énergie positive",
        valence=0.9,
        arousal=0.9,
        energy_level=EnergyLevel.VERY_HIGH,
        bpm_range=(125, 150),
        color="#FF6B35",  # Orange vif
    ),
    MoodCategory.INTENSE: MoodProfile(
        enabled=True,
        description="Puissant, driving, épique - énergie maximale",
        valence=0.0,
        arousal=1.0,
        energy_level=EnergyLevel.VERY_HIGH,
        bpm_range=(120, 160),
        color="#DC143C",  # Crimson
    ),
    MoodCategory.ANGRY: MoodProfile(
        enabled=True,
        description="Agressif, rebelle, punk/rock - tension et puissance",
        valence=-0.7,
        arousal=0.8,
        energy_level=EnergyLevel.HIGH,
        bpm_range=(100, 180),
        color="#8B0000",  # Dark red
    ),
    MoodCategory.MELANCHOLIC: MoodProfile(
        enabled=True,
        description="Nostalgique, introspectif, poétique - émotion profonde",
        valence=-0.5,
        arousal=0.0,
        energy_level=EnergyLevel.MEDIUM,
        bpm_range=(70, 110),
        color="#4A0E4E",  # Purple foncé
    ),
    MoodCategory.SAD: MoodProfile(
        enabled=True,
        description="Triste, sombre, émotionnel - ballades et slow",
        valence=-0.8,
        arousal=-0.6,
        energy_level=EnergyLevel.LOW,
        bpm_range=(50, 90),
        color="#2F4F4F",  # Dark slate
    ),
    MoodCategory.CALM: MoodProfile(
        enabled=True,
        description="Paisible, serein, ambient - repos et méditation",
        valence=0.0,
        arousal=-1.0,
        energy_level=EnergyLevel.VERY_LOW,
        bpm_range=(50, 85),
        color="#87CEEB",  # Sky blue
    ),
    MoodCategory.RELAXED: MoodProfile(
        enabled=True,
        description="Détendu, chill, lounge - bien-être positif",
        valence=0.6,
        arousal=-0.5,
        energy_level=EnergyLevel.LOW,
        bpm_range=(70, 105),
        color="#98FB98",  # Pale green
    ),
}


# =============================================================================
# CONFIGURATION DES DAYPARTS
# =============================================================================

DAYPARTS: dict[DaypartSegment, DaypartProfile] = {
    DaypartSegment.DAWN: DaypartProfile(
        enabled=True,
        start_hour=5,
        end_hour=9,
        description="Réveil — ambient, modern classical, slowcore. Lumière qui se lève.",
        target_moods=[MoodCategory.CALM, MoodCategory.RELAXED],
        energy_curve=EnergyLevel.LOW,
        transition_flex=0.3,
    ),
    DaypartSegment.DAY: DaypartProfile(
        enabled=True,
        start_hour=9,
        end_hour=17,
        description="Activité — indie/electro mid-tempo, hip-hop chill. Fond de travail.",
        target_moods=[
            MoodCategory.ENERGETIC,
            MoodCategory.EXCITED,
            MoodCategory.RELAXED,
            MoodCategory.MELANCHOLIC,
        ],
        energy_curve=EnergyLevel.MEDIUM,
        transition_flex=0.4,
    ),
    DaypartSegment.DUSK: DaypartProfile(
        enabled=True,
        start_hour=17,
        end_hour=22,
        description="Transition — dream pop, trip hop, downtempo. Plus émotionnel.",
        target_moods=[
            MoodCategory.RELAXED,
            MoodCategory.MELANCHOLIC,
            MoodCategory.SAD,
            MoodCategory.ANGRY,
        ],
        energy_curve=EnergyLevel.LOW,
        transition_flex=0.5,
    ),
    DaypartSegment.NIGHT: DaypartProfile(
        enabled=True,
        start_hour=22,
        end_hour=5,
        description="Nuit — ambient profond, sad, expérimental, intense. Introspection.",
        target_moods=[
            MoodCategory.CALM,
            MoodCategory.SAD,
            MoodCategory.MELANCHOLIC,
            MoodCategory.INTENSE,
            MoodCategory.ANGRY,
        ],
        energy_curve=EnergyLevel.LOW,
        transition_flex=0.6,
    ),
}


# =============================================================================
# CONFIGURATION PAR JOUR DE LA SEMAINE
# =============================================================================
#
# Overrides par type de jour. Seuls les dayparts avec des différences sont listés.
# Les dayparts non mentionnés utilisent la config de base (DAYPARTS).
#
# Architecture basée sur:
# - Nielsen Audio weekday vs weekend listening patterns
# - Spotify diurnal cycle research
# - Radio programming best practices (MusicMaster, Music 1)


# =============================================================================
# INSTANCES DE CONFIGURATION
# =============================================================================

# Règles de séparation — valeurs de RÉFÉRENCE pour la config AzuraCast AutoDJ.
# Ces règles ne sont PAS appliquées par le pipeline Python (classify.py).
# Elles documentent les paramètres à configurer dans AzuraCast > AutoDJ > Scheduling.
# La fonction check_separation_rules() peut être appelée pour du monitoring/debug.
SEPARATION = SeparationRules(
    artist_min_minutes=60,        # 1 heure entre même artiste
    title_min_minutes=180,        # 3 heures entre même titre
    tempo_max_variance=25,        # ±25 BPM entre tracks consécutives
    energy_smooth_transition=True,
    mood_min_separation=3,        # 3 tracks avant de répéter un mood
    genre_min_separation=2,
)

# Filtres audio
AUDIO_FILTERS = AudioFilters(
    duration_min=60,    # 1 minute minimum
    duration_max=420,   # 7 minutes maximum
    bpm_min=None,
    bpm_max=None,
    min_confidence=0.3,
    reject_low_quality=True,
)

# Rotation de la bibliothèque.
# max_tracks 600 → 700 : laisse une marge pour la stratification HEAVY/MEDIUM/
# DISCOVERY (sinon les 3 catégories sont trop serrées). Lifetime moyen passe
# de ~24j à ~35-40j → davantage de plays par track avant éviction → expérience
# auditeur plus "memorable" (les hits émergent vraiment au lieu d'être noyés).
ROTATION = RotationConfig(
    max_tracks=700,
    fresh_days=14,           # 10 → 14 : laisse plus de temps à un nouveau track de "prouver" via plays
    current_days=35,         # 30 → 35
    max_age_days=60,         # 50 → 60 : alignement avec cooldown
    cooldown_days=60,
    fading_max_pct=20.0,
    min_plays_before_delete=5,  # 3 → 5 : un peu plus exigeant avant éviction
)


# =============================================================================
# ROTATION CATEGORIES — A/B/C-style rotation for a discovery webradio
# =============================================================================
#
# Inspired by BBC 6 Music / MusicMaster's A-list / B-list / C-list model
# adapted to an autonomous discovery webradio (no human curator).
#
# Tier assignment (computed daily by enforce_tiered_rotation):
#
#   HEAVY  — max rotation. Two ways to land here:
#            (1) GRACE PERIOD: age < grace_period_days. Every new track
#                gets full exposure during its first ~2 weeks. No exception.
#                This is what makes it a *discovery* radio: new music gets heard.
#            (2) PROVEN: past grace, plays/day ≥ expected × heavy_above_average_ratio.
#   MEDIUM — average rotation. Post-grace tracks performing around the
#            library mean.
#   LIGHT  — fading. Below-average performers. Reduced visibility means
#            they accumulate fewer plays, accelerating eviction at max_age_days.
#
# Playlist mapping (tier_filter_dayparts):
#   HEAVY  → all mood-compatible dayparts
#   MEDIUM → first medium_daypart_count dayparts (default 3)
#   LIGHT  → first light_daypart_count dayparts (default 1)
#
# Demotion is real, not silent: the re-tier pass uses assign_playlists()
# (REPLACE semantics) so a HEAVY track demoted to LIGHT is actually removed
# from the extra playlists. No accidental zombies kept in all dayparts.
#
# expected_plays_per_day: how often the average track plays in a day.
# Computed from library_size + observed plays/hour at AzuraCast. With 597
# tracks and ~16 plays/hour (~384/day total), the per-track expectation
# is 384/597 ≈ 0.64. Set to 0.65 (slightly above true mean → conservative
# = harder to qualify as HEAVY, fewer false-positive "hits").
# =============================================================================


@dataclass
class RotationCategoryConfig:
    """A/B/C-style rotation for an autonomous discovery webradio."""
    enabled: bool = True

    # Grace period: every new track is HEAVY for this many days regardless
    # of plays. Makes "discovery" actually mean discovery.
    grace_period_days: int = 14

    # Reference rate: what an "average" track plays per day, library-wide.
    # Tune this based on AzuraCast playlists' total scheduled time + observed
    # listener-driven dynamics. For AubeSonore (597 tracks, ~16 plays/h),
    # the empirical mean is ~0.64 plays/track/day.
    expected_plays_per_day: float = 0.65

    # Above-average bar to qualify as HEAVY (post-grace). 1.2 = 20% above mean.
    heavy_above_average_ratio: float = 1.2
    # Below-average floor — under this, tier drops from MEDIUM to LIGHT.
    light_below_average_ratio: float = 0.6

    # Daypart caps per tier. HEAVY isn't capped (always all matching zones).
    # With 4 zones: MEDIUM=2 (50%), LIGHT=1 (25%). For moods that already
    # only match 1-2 zones, these caps are mostly no-ops — the tier system's
    # main effect there comes from playlist weight tuning rather than
    # zone count.
    medium_daypart_count: int = 2
    light_daypart_count: int = 1


ROTATION_CATEGORIES = RotationCategoryConfig()

# =============================================================================
# DISCOVERY — Sources de découverte multi-RSS + Last.fm
# =============================================================================
#
# Architecture (mai 2026) :
# - HypeMachine reste actif comme une source parmi d'autres
# - Sources RSS curated alignées sur l'esthétique AubeSonore
#   (indie / electronic / ambient / hip-hop)
# - Last.fm tag.gettoptracks comble les angles morts (notamment hip-hop)
# - data/manual_picks.json : injection manuelle (existant)
# - data/custom_feeds.json : URLs RSS arbitraires (ex. rss.app) — modifiable
#   sans toucher au code
# =============================================================================


@dataclass
class RSSFeedSpec:
    """Specification d'un feed RSS pour la découverte."""
    url: str
    parser: str = "dash"                  # "dash", "tilde", "dash_quoted"
    link_must_contain: str | None = None  # filtre sur le path du <link>
    label: str = ""
    enabled: bool = True
    limit: int = 30


# Sources RSS curated.
# Chaque source enrichit tracks-to-download.json. Le pipeline déduplique sur
# (artist, title) après normalisation, donc des sources qui se recoupent
# (ex. Pitchfork × Stereogum × Gorilla vs Bear) ne posent pas de problème.
RSS_FEEDS: tuple[RSSFeedSpec, ...] = (
    RSSFeedSpec(
        url="https://www.gorillavsbear.net/feed/",
        parser="dash",
        label="gorillavsbear",
        limit=25,
    ),
    RSSFeedSpec(
        url="https://acloserlisten.com/feed/",
        parser="tilde",
        label="acloserlisten",
        limit=15,
    ),
    RSSFeedSpec(
        url="https://www.stereogum.com/feed/",
        parser="dash_quoted",
        link_must_contain="/music/",
        label="stereogum",
        limit=30,
    ),
    RSSFeedSpec(
        # Pitchfork "Track Reviews" feed. Le <title> contient seulement le
        # morceau ; l'artiste est dans le slug du <link>. Parser dédié.
        # Voir https://pitchfork.com/info/rss/ pour la liste à jour.
        url="https://pitchfork.com/feed/feed-track-reviews/rss",
        parser="pitchfork",
        label="pitchfork-tracks",
        limit=20,
    ),
)


# Tags Last.fm dont on tire les top tracks (gettoptracks).
# Mix indie / electro / ambient + hip-hop demandé.
# Chaque tag retourne jusqu'à LASTFM_TAG_LIMIT tracks par run.
LASTFM_TAGS: tuple[str, ...] = (
    "indie",
    "electronic",
    "ambient",
    "hip-hop",
    "downtempo",
    "dream pop",
    "trip hop",
    "indietronica",
    "shoegaze",
)
LASTFM_TAG_LIMIT: int = 15

# Plafond global de tracks remontées par l'étape discover (toutes sources
# confondues, après dédup). Réduit de 120 à 60 après analyse : on n'avait
# jamais besoin de plus de 60 candidates (la queue tombait à 15-25 réels
# après filtrage), donc générer 120 était du gaspillage de requêtes API.
DISCOVER_MAX_TRACKS: int = 60


# =============================================================================
# GENRE — Allowlist (multi-source : MusicBrainz + Discogs + Last.fm)
# =============================================================================
#
# Politique du genre_client (best practices 2026) :
#   1. blocklist hit (UNION des 3 sources) → rejet immédiat
#   2. allowlist hit (UNION des 3 sources) → accept (passe le filtre)
#   3. aucun tag retourné → accept (downstream Essentia AGGRESSIVE_FILTER prend
#      le relais sur l'audio)
#
# MusicBrainz fournit la taxonomie canonique (genre + tag).
# Discogs domine sur électronique/hip-hop/jazz (genre + style).
# Last.fm couvre l'obscur (crowd-sourced).
# =============================================================================

ALLOWED_GENRES: tuple[str, ...] = (
    # ─── INDIE / ALTERNATIVE ──────────────────────────────────────
    "indie", "indie rock", "indie pop", "indie folk", "indietronica",
    "alternative", "alternative rock", "alt-rock",
    "dream pop", "shoegaze", "slowcore", "sadcore",
    "bedroom pop", "jangle pop", "twee pop",
    "art pop", "art rock", "chamber pop", "baroque pop",
    "post-rock", "math rock", "post-punk", "new wave", "no wave",
    "experimental rock",
    # ─── FOLK ─────────────────────────────────────────────────────
    "folk", "freak folk", "psych folk", "indie folk",
    "americana", "alt-country", "alternative country",
    "contemporary folk", "neo-folk",
    # ─── ELECTRONIC ───────────────────────────────────────────────
    "electronic", "electronica", "idm", "intelligent dance music",
    "downtempo", "chillout", "chill-out", "lounge",
    "trip hop", "trip-hop",
    "ambient pop", "ambient electronic", "ambient techno",
    "synthpop", "synth-pop", "electropop", "electro pop",
    "synthwave", "chillwave", "vaporwave", "glitch",
    "future bass",
    # House / techno (variantes mid-tempo, pas le hard)
    "house", "deep house", "minimal house", "lo-fi house", "tech house",
    "techno", "minimal techno", "dub techno", "ambient techno",
    "uk garage", "garage", "future garage", "2-step",
    "footwork", "juke",
    "drum and bass", "dnb", "drum & bass", "liquid dnb", "liquid funk",
    "broken beat", "breakbeat", "jungle",
    "bass music",
    # ─── AMBIENT / CINEMATIC ──────────────────────────────────────
    "ambient", "dark ambient", "drone", "drone music",
    "modern classical", "neoclassical", "post-classical",
    "minimalism", "minimal music", "post-minimalism",
    "soundscape", "field recordings", "field recording",
    "new age",
    # ─── HIP-HOP / RAP ────────────────────────────────────────────
    "hip hop", "hip-hop", "rap",
    "alternative hip hop", "alt hip hop", "alt-hip-hop",
    "experimental hip hop", "conscious hip hop",
    "jazz rap", "jazz hip hop", "abstract hip hop",
    "lo-fi hip hop", "boom bap", "golden age hip hop",
    "trap", "cloud rap",
    # ─── SOUL / R&B / NEO SOUL ────────────────────────────────────
    "neo soul", "neo-soul", "soul",
    "r&b", "rnb", "rhythm and blues",
    "contemporary r&b", "alternative r&b", "alt-r&b", "alt r&b",
    "afrobeat", "afro soul", "afrobeats",
    # ─── JAZZ-ADJACENT ────────────────────────────────────────────
    "jazz", "nu jazz", "jazz fusion",
    "smooth jazz", "spiritual jazz", "contemporary jazz",
    "ethio-jazz",
    # ─── POP / LO-FI / BEDROOM ────────────────────────────────────
    "pop", "indie pop",
    "lo-fi", "lofi", "bedroom",
)


# Filtre de genre (multi-source : MusicBrainz + Discogs + Last.fm)
# Voir GenreFilterConfig pour la docstring détaillée. Cette instance
# garde sa propre liste (peut être tunée sans toucher la classe).
GENRE_FILTER = GenreFilterConfig(
    enabled=True,
    blocked_genres=(
        # Metal — toutes variantes
        "metal", "death metal", "black metal", "heavy metal",
        "thrash metal", "doom metal", "nu metal", "groove metal",
        "power metal", "speed metal", "progressive metal",
        "sludge metal", "stoner metal", "post-metal",
        "folk metal", "symphonic metal", "viking metal", "djent",
        # Metalcore / hardcore famille
        "grindcore", "metalcore", "deathcore", "mathcore",
        "melodic metalcore", "post-hardcore",
        # Hard rock + glam
        "hard rock", "glam metal", "hair metal",
        # Punk extrême (post-punk reste autorisé)
        "hardcore punk", "crust punk", "thrash punk", "d-beat",
        # Industrial agressif
        "industrial", "industrial metal", "industrial rock",
        "aggrotech", "ebm", "death industrial",
        # Hard electronic
        "hardcore", "hardstyle", "hard techno", "industrial techno",
        "gabber", "schranz", "speedcore", "happy hardcore",
        # Noise extrême uniquement (laisser passer "noise" qui est
        # surtout utilisé comme texture en shoegaze/dream pop)
        "harsh noise", "power electronics", "japanoise", "noise music",
    ),
    require_tags=False,
)

# Filtre audio intelligent (fallback quand Last.fm n'a pas de tags)
# Bloque les tracks agressives détectées par Essentia (arousal élevé + valence négative)
AGGRESSIVE_FILTER = AggressiveAudioFilter(
    enabled=True,
    arousal_threshold=0.65,   # Arousal > 0.65 = très énergique
    valence_threshold=-0.2,   # Valence < -0.2 = négatif/agressif
    block_intense_mood=False,  # Désactivé : le filtre multi-signal gère les tracks agressives
)

# Filtre multi-signal (nouveaux morceaux uniquement)
# Combine 4 signaux indépendants pour rejeter les tracks agressives
MULTI_SIGNAL_FILTER = MultiSignalFilterConfig()


# =============================================================================
# AUDIO PROCESSING — quality gates v4
# =============================================================================


@dataclass
class AcoustIDDedupConfig:
    """
    Content-based dedup via Chromaprint (fpcalc).

    Catches the same recording uploaded under different metadata
    (remasters, "feat." rewrites, label re-releases). Pure local —
    no AcoustID web API call, no API key needed.

    Failure mode is graceful: if fpcalc is missing or fails on a
    given file, dedup is skipped (the artist/title check still runs).
    """
    enabled: bool = True


@dataclass
class SpeechFilterConfig:
    """
    Reject speech-heavy tracks (podcast episodes, interviews) that
    can sneak in via RSS feeds (e.g. A Closer Listen "An Interview
    with Lea Bertucci" matched our tilde parser).

    Uses the Essentia voice_instrumental classifier (discogs-effnet
    head, ~98% accuracy). Threshold 0.6 = at least 60% voice
    probability across the track required to reject.
    """
    enabled: bool = True
    max_voice_probability: float = 0.6


@dataclass
class LoudnormConfig:
    """
    EBU R128 loudness normalisation pass (broadcast standard).

    AzuraCast does some crossfade-level normalisation but does not
    target a specific LUFS. Running a single-pass ffmpeg loudnorm
    before upload gives consistent perceived volume across the library.

    Defaults match the EBU R128 broadcast spec for music streaming:
      I (integrated)     = -16 LUFS
      LRA (loudness range) = 11 LU
      TP (true peak)     = -1.5 dBTP

    Failure mode: if loudnorm fails (rare), the original file is kept.
    """
    enabled: bool = True
    target_lufs: float = -16.0
    loudness_range: float = 11.0
    true_peak: float = -1.5


@dataclass
class CLAPConfig:
    """
    CLAP audio embeddings (LAION-CLAP HTSAT) for content-based similarity.

    When enabled, analyze.py computes a 512-dim L2-normalised embedding
    per track and persists it under data/embeddings.npy. Smart sequencing
    (scripts/smart_queue.py) then uses FAISS over those embeddings to
    derive nearest-neighbour walks per daypart, making mood/tempo
    separation rules emergent from the geometry rather than heuristic.

    Disabled by default because:
      - First load downloads ~1.7 GB model from HF Hub
      - Each track adds ~3-5 s of CPU inference

    To enable in production:
      1. Pre-warm the model: `python3 -c "from audio_embeddings import _load_model; _load_model()"`
      2. Flip `enabled=True` here.
      3. Backfill existing library with `scripts/backfill_embeddings.py` (TBD).
    """
    enabled: bool = False


ACOUSTID_DEDUP = AcoustIDDedupConfig()
SPEECH_FILTER = SpeechFilterConfig()
LOUDNORM = LoudnormConfig()
# CLAP is enabled in production: backfill has been done (597/597 embedded),
# new tracks add ~3-5s/track to analyze.py which is well within budget.
CLAP = CLAPConfig(enabled=True)

# Seuils de classification
THRESHOLDS = ClassificationThresholds(
    aggressive_threshold=0.40,
    happy_threshold=0.45,
    relaxed_threshold=0.45,
    sad_threshold=0.45,
    bpm_very_slow=80,
    bpm_slow=100,
    bpm_moderate=115,
    bpm_fast=128,
    bpm_very_fast=145,
)


# =============================================================================
# FONCTIONS UTILITAIRES
# =============================================================================

def get_enabled_moods() -> list[MoodCategory]:
    """Retourne la liste des moods activés."""
    return [mood for mood, profile in MOODS.items() if profile.enabled]


def get_enabled_dayparts() -> list[DaypartSegment]:
    """Retourne la liste des dayparts activés."""
    return [dp for dp, profile in DAYPARTS.items() if profile.enabled]


def get_mood_profile(mood: MoodCategory | str) -> MoodProfile | None:
    """
    Récupère le profil d'un mood.

    Args:
        mood: MoodCategory ou nom du mood

    Returns:
        MoodProfile ou None si non trouvé
    """
    if isinstance(mood, str):
        try:
            mood = MoodCategory(mood)
        except ValueError:
            return None
    return MOODS.get(mood)


def get_day_type(weekday: int) -> DayType:
    """
    Convertit un numéro de jour en DayType.

    Args:
        weekday: 0=Lundi, 1=Mardi, ..., 6=Dimanche (format datetime.weekday())

    Returns:
        DayType correspondant
    """
    match weekday:
        case 4:
            return DayType.FRIDAY
        case 5:
            return DayType.SATURDAY
        case 6:
            return DayType.SUNDAY
        case _:
            return DayType.WEEKDAY


def get_current_day_type() -> DayType:
    """Retourne le DayType pour aujourd'hui."""
    return get_day_type(datetime.now().weekday())


def get_daypart_for_hour(hour: int) -> DaypartSegment | None:
    """
    Trouve le daypart correspondant à une heure donnée.

    Args:
        hour: Heure (0-23)

    Returns:
        DaypartSegment ou None
    """
    for segment, profile in DAYPARTS.items():
        if not profile.enabled:
            continue

        start = profile.start_hour
        end = profile.end_hour

        # Gestion du passage minuit (ex: 22:00 - 05:00)
        if start > end:
            if hour >= start or hour < end:
                return segment
        else:
            if start <= hour < end:
                return segment

    return None


def get_effective_daypart_profile(
    daypart: DaypartSegment,
    day_type: DayType | None = None
) -> DaypartProfile:
    """
    Récupère le profil d'un daypart.

    Args:
        daypart: Le segment de journée
        day_type: Ignoré (gardé pour compatibilité)

    Returns:
        DaypartProfile
    """
    return DAYPARTS[daypart]


def get_dayparts_for_mood(
    mood: MoodCategory | str,
    day_type: DayType | None = None
) -> list[DaypartSegment]:
    """
    Trouve tous les dayparts qui acceptent un mood donné.

    Args:
        mood: MoodCategory ou nom du mood
        day_type: Ignoré (gardé pour compatibilité)

    Returns:
        Liste des DaypartSegment compatibles
    """
    if isinstance(mood, str):
        try:
            mood = MoodCategory(mood)
        except ValueError:
            return []

    return [
        segment for segment, profile in DAYPARTS.items()
        if profile.enabled and mood in profile.target_moods
    ]


def is_mood_enabled(mood: MoodCategory | str) -> bool:
    """Vérifie si un mood est activé."""
    profile = get_mood_profile(mood)
    return profile is not None and profile.enabled


def get_energy_order() -> list[EnergyLevel]:
    """Retourne les niveaux d'énergie dans l'ordre croissant."""
    return [
        EnergyLevel.VERY_LOW,
        EnergyLevel.LOW,
        EnergyLevel.MEDIUM,
        EnergyLevel.HIGH,
        EnergyLevel.VERY_HIGH,
    ]


def is_smooth_energy_transition(from_energy: EnergyLevel, to_energy: EnergyLevel) -> bool:
    """
    Vérifie si la transition d'énergie est douce (max 1 niveau de différence).

    Args:
        from_energy: Niveau d'énergie de départ
        to_energy: Niveau d'énergie d'arrivée

    Returns:
        True si la transition est acceptable
    """
    order = get_energy_order()
    from_idx = order.index(from_energy)
    to_idx = order.index(to_energy)
    return abs(from_idx - to_idx) <= 1


def should_reject_track(features: dict[str, Any]) -> tuple[bool, str | None]:
    """
    Vérifie si une track doit être rejetée selon les filtres configurés.

    Args:
        features: Dictionnaire contenant:
            - mood: str | None
            - bpm: int
            - duration: int (secondes)
            - confidence: float (0-1)

    Returns:
        Tuple (reject: bool, reason: str | None)
    """
    # 1. Vérifier si le mood est activé
    mood = features.get("mood")
    if mood and not is_mood_enabled(mood):
        return True, f"mood '{mood}' désactivé"

    # 2. Vérifier la durée
    duration = features.get("duration", 0)
    if AUDIO_FILTERS.duration_min and duration < AUDIO_FILTERS.duration_min:
        mins, secs = divmod(int(duration), 60)
        return True, f"trop court ({mins}:{secs:02d})"

    if AUDIO_FILTERS.duration_max and duration > AUDIO_FILTERS.duration_max:
        mins, secs = divmod(int(duration), 60)
        return True, f"trop long ({mins}:{secs:02d})"

    # 3. Vérifier le BPM
    bpm = features.get("bpm", 0)
    if AUDIO_FILTERS.bpm_min and bpm < AUDIO_FILTERS.bpm_min:
        return True, f"BPM trop bas ({bpm})"

    if AUDIO_FILTERS.bpm_max and bpm > AUDIO_FILTERS.bpm_max:
        return True, f"BPM trop haut ({bpm})"

    # 4. Vérifier la confiance de classification
    confidence = features.get("confidence", 1.0)
    if AUDIO_FILTERS.min_confidence and confidence < AUDIO_FILTERS.min_confidence:
        return True, f"confiance trop basse ({confidence:.2f})"

    # 5. Vérifier si le mood a des dayparts assignés
    if mood:
        if not get_dayparts_for_mood(mood):
            return True, f"aucun créneau pour mood '{mood}'"

    return False, None


def check_separation_rules(
    new_track: dict[str, Any],
    recent_tracks: list[dict[str, Any]],
    minutes_since_last: float = 0
) -> tuple[bool, str | None]:
    """
    Vérifie les règles de séparation pour une nouvelle track.

    NOTE: Cette fonction n'est PAS appelée par le pipeline de classification.
    Les règles de séparation sont appliquées par AzuraCast AutoDJ au moment
    du scheduling. Cette fonction est disponible pour du monitoring ou debug.

    Args:
        new_track: Track à vérifier (artist, title, bpm, mood, energy_level)
        recent_tracks: Liste des tracks récentes (plus récente en premier)
        minutes_since_last: Minutes depuis la dernière track

    Returns:
        Tuple (can_play: bool, violation: str | None)
    """
    if not recent_tracks:
        return True, None

    new_artist = new_track.get("artist", "").lower()
    new_title = new_track.get("title", "").lower()
    new_bpm = new_track.get("bpm", 0)
    new_mood = new_track.get("mood", "")
    new_energy = new_track.get("energy_level", EnergyLevel.MEDIUM)

    # 1. Vérifier séparation artiste
    for i, track in enumerate(recent_tracks):
        track_artist = track.get("artist", "").lower()
        if track_artist and track_artist == new_artist:
            # Estimer le temps écoulé (approximation: 3.5 min par track)
            estimated_minutes = i * AVERAGE_TRACK_DURATION_MINUTES + minutes_since_last
            if estimated_minutes < SEPARATION.artist_min_minutes:
                return False, f"artiste trop récent ({new_artist})"

    # 2. Vérifier séparation titre
    for i, track in enumerate(recent_tracks):
        track_title = track.get("title", "").lower()
        if track_title and track_title == new_title:
            estimated_minutes = i * AVERAGE_TRACK_DURATION_MINUTES + minutes_since_last
            if estimated_minutes < SEPARATION.title_min_minutes:
                return False, f"titre trop récent"

    # 3. Vérifier variance de tempo (seulement vs la track précédente)
    if SEPARATION.tempo_max_variance and new_bpm > 0:
        last_bpm = recent_tracks[0].get("bpm", 0)
        if last_bpm > 0:
            variance = abs(new_bpm - last_bpm)
            if variance > SEPARATION.tempo_max_variance:
                return False, f"saut de tempo trop grand ({last_bpm} → {new_bpm})"

    # 4. Vérifier transition d'énergie douce
    if SEPARATION.energy_smooth_transition:
        last_energy = recent_tracks[0].get("energy_level", EnergyLevel.MEDIUM)
        if isinstance(last_energy, str):
            try:
                last_energy = EnergyLevel(last_energy)
            except ValueError:
                last_energy = EnergyLevel.MEDIUM
        if isinstance(new_energy, str):
            try:
                new_energy = EnergyLevel(new_energy)
            except ValueError:
                new_energy = EnergyLevel.MEDIUM

        if not is_smooth_energy_transition(last_energy, new_energy):
            return False, f"transition d'énergie trop brutale ({last_energy.value} → {new_energy.value})"

    # 5. Vérifier séparation de mood
    if SEPARATION.mood_min_separation and new_mood:
        same_mood_count = 0
        for i, track in enumerate(recent_tracks[:SEPARATION.mood_min_separation]):
            if track.get("mood") == new_mood:
                same_mood_count += 1
        if same_mood_count >= SEPARATION.mood_min_separation:
            return False, f"mood '{new_mood}' trop fréquent"

    return True, None


def get_playlist_name(daypart: DaypartSegment, day_type: DayType | None = None) -> str:
    """
    Génère le nom de playlist pour un daypart.

    Args:
        daypart: Le segment de journée
        day_type: Ignoré (gardé pour compatibilité)

    Returns:
        Nom de la playlist (ex: "Evening")
    """
    return daypart.value


def get_all_playlist_names() -> list[str]:
    """Génère les noms des zones playlists actuellement actives."""
    return [daypart.value for daypart in get_enabled_dayparts()]


def print_day_schedule(day_type: DayType) -> None:
    """
    Affiche le planning pour un type de jour.

    Args:
        day_type: Type de jour à afficher
    """
    day_names = {
        DayType.WEEKDAY: "LUNDI-JEUDI",
        DayType.FRIDAY: "VENDREDI",
        DayType.SATURDAY: "SAMEDI",
        DayType.SUNDAY: "DIMANCHE",
    }

    print(f"\n{'='*60}")
    print(f"  {day_names[day_type]}")
    print(f"{'='*60}")

    for segment in DaypartSegment:
        profile = get_effective_daypart_profile(segment, day_type)
        if not profile.enabled:
            continue

        # Format horaire
        start = f"{profile.start_hour:02d}:00"
        end = f"{profile.end_hour:02d}:00"
        time_str = f"{start}-{end}"

        # Moods
        moods = ", ".join(m.value for m in profile.target_moods[:3])
        if len(profile.target_moods) > 3:
            moods += "..."

        # Énergie
        energy = profile.energy_curve.value.upper()

        print(f"  {time_str:12} │ {segment.value:17} │ {energy:10} │ {moods}")

    print(f"{'='*60}")


def format_duration(seconds: int) -> str:
    """Formate une durée en MM:SS."""
    mins, secs = divmod(seconds, 60)
    return f"{mins}:{secs:02d}"


def format_bpm_range(bpm_range: tuple[int, int]) -> str:
    """Formate une plage de BPM."""
    return f"{bpm_range[0]}-{bpm_range[1]}"


# =============================================================================
# VALIDATION DE CONFIGURATION
# =============================================================================

def validate_config() -> tuple[bool, list[str]]:
    """
    Valide la cohérence de la configuration.

    Returns:
        Tuple (is_valid: bool, errors: list[str])
    """
    errors = []

    # 1. Au moins un mood activé
    enabled_moods = get_enabled_moods()
    if not enabled_moods:
        errors.append("Aucun mood activé")

    # 2. Au moins un daypart activé
    enabled_dayparts = get_enabled_dayparts()
    if not enabled_dayparts:
        errors.append("Aucun daypart activé")

    # 3. Chaque mood activé doit avoir au moins un daypart
    for mood in enabled_moods:
        dayparts = get_dayparts_for_mood(mood)
        if not dayparts:
            errors.append(f"Mood '{mood.value}' n'a aucun daypart assigné")

    # 4. Chaque daypart doit avoir au moins un mood
    for segment, profile in DAYPARTS.items():
        if profile.enabled and not profile.target_moods:
            errors.append(f"Daypart '{segment.value}' n'a aucun mood cible")

    # 5. Vérifier cohérence des horaires (couverture 24h)
    covered_hours = set()
    for segment, profile in DAYPARTS.items():
        if not profile.enabled:
            continue
        start, end = profile.start_hour, profile.end_hour
        if start > end:  # Passage minuit
            covered_hours.update(range(start, 24))
            covered_hours.update(range(0, end))
        else:
            covered_hours.update(range(start, end))

    missing_hours = set(range(24)) - covered_hours
    if missing_hours:
        errors.append(f"Heures non couvertes: {sorted(missing_hours)}")

    # 6. Vérifier filtres audio cohérents
    if AUDIO_FILTERS.duration_min and AUDIO_FILTERS.duration_max:
        if AUDIO_FILTERS.duration_min >= AUDIO_FILTERS.duration_max:
            errors.append("duration_min doit être < duration_max")

    if AUDIO_FILTERS.bpm_min and AUDIO_FILTERS.bpm_max:
        if AUDIO_FILTERS.bpm_min >= AUDIO_FILTERS.bpm_max:
            errors.append("bpm_min doit être < bpm_max")

    return len(errors) == 0, errors


