"""Deciding which Spotify track corresponds to a Deezer track.

An ISRC match is authoritative and skips all of this. Everything here is the
fallback for tracks whose ISRC is missing on one side or simply differs
between the two catalogues, which is common for reissues and compilations.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

# Cosmetic edition markers. Removing these helps "Song (2011 Remaster)" match
# plain "Song". Markers that denote a genuinely different recording -- live,
# remix, acoustic and friends -- are deliberately NOT in this list; they are
# compared instead, further down.
_NOISE_PATTERNS = [
    r"\d{2,4}\s*(?:digital\s*)?remaster(?:ed)?(?:\s*version)?",
    r"(?:digitally\s*)?remaster(?:ed)?(?:\s*\d{2,4})?(?:\s*version)?",
    r"album\s*version",
    r"single\s*version",
    r"original\s*version",
    r"standard\s*version",
    r"deluxe(?:\s*edition)?",
    r"expanded\s*edition",
    r"bonus\s*track",
    r"explicit(?:\s*version)?",
    r"clean(?:\s*version)?",
    r"mono(?:\s*version)?",
    r"stereo(?:\s*version)?",
    r"anniversary\s*edition",
]
_NOISE_RE = re.compile(
    r"[\(\[]\s*(?:" + "|".join(_NOISE_PATTERNS) + r")\s*[\)\]]",
    re.IGNORECASE,
)
_TRAILING_NOISE_RE = re.compile(
    r"\s*-\s*(?:" + "|".join(_NOISE_PATTERNS) + r")\s*$",
    re.IGNORECASE,
)

# "feat." credits live in the artist field on one platform and the title on the
# other, so they are stripped from titles and compared separately.
_FEAT_RE = re.compile(
    r"[\(\[]\s*(?:feat|ft|featuring|with)\.?\s+[^\)\]]*[\)\]]|"
    r"\s+-\s+(?:feat|ft|featuring)\.?\s+.*$|"
    r"\s+(?:feat|ft|featuring)\.?\s+.*$",
    re.IGNORECASE,
)

# Markers that mean "different recording". If one side has one and the other
# does not, the pair is penalised hard.
_VERSION_MARKERS = {
    "live", "remix", "acoustic", "instrumental", "karaoke", "unplugged",
    "demo", "reprise", "edit", "mix", "cover", "session", "sped", "slowed",
}
_VERSION_MARKER_RE = re.compile(
    r"\b(" + "|".join(sorted(_VERSION_MARKERS)) + r")\b", re.IGNORECASE
)

# Artists that produce sound-alike recordings; never an acceptable substitute.
_IMPOSTOR_RE = re.compile(
    r"\b(karaoke|tribute|made\s*famous|in\s*the\s*style\s*of|"
    r"cover\s*band|backing\s*track|instrumental\s*version)\b",
    re.IGNORECASE,
)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# Below this artist agreement a candidate is disqualified outright. Titles are
# shared by countless unrelated songs ("Alone", "Closer", "Home"), so a strong
# title and duration match must never be able to outvote a wrong artist.
ARTIST_FLOOR = 0.45
_DISQUALIFIED_SCORE = 0.35


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Lowercase, de-accent, drop punctuation, collapse whitespace."""
    text = strip_accents(text or "").lower()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def normalize_title(title: str) -> str:
    """Normalize a track title, removing cosmetic edition markers and credits."""
    text = title or ""
    text = _NOISE_RE.sub(" ", text)
    text = _TRAILING_NOISE_RE.sub(" ", text)
    text = _FEAT_RE.sub(" ", text)
    return normalize(text)


def normalize_artist(artist: str) -> str:
    return normalize(artist)


def version_markers(title: str) -> set[str]:
    """Version words present in a title, e.g. {'live'} for 'Song (Live)'."""
    return {m.lower() for m in _VERSION_MARKER_RE.findall(title or "")}


def looks_like_impostor(artists: list[str], title: str) -> bool:
    haystack = " ".join(artists) + " " + (title or "")
    return bool(_IMPOSTOR_RE.search(haystack))


def similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def artist_similarity(left_artists: list[str], right_artists: list[str]) -> float:
    """Best pairwise similarity between two artist credit lists.

    Compilations and features mean the lists rarely line up positionally, so
    the strongest single pairing wins, with a bonus when one name is contained
    in the other ("Beyonce" vs "Beyonce, Jay-Z").
    """
    left = [normalize_artist(a) for a in left_artists if a and a.strip()]
    right = [normalize_artist(a) for a in right_artists if a and a.strip()]
    if not left or not right:
        return 0.0

    best = 0.0
    for a in left:
        for b in right:
            score = similarity(a, b)
            if a and b and (a in b or b in a):
                score = max(score, 0.92)
            best = max(best, score)

    # Comparing the joined credits catches "A & B" vs "A, B".
    best = max(best, similarity(" ".join(sorted(left)), " ".join(sorted(right))))
    return best


def duration_score(left_s: int, right_s: int, tolerance_s: int) -> float:
    """1.0 inside the tolerance, fading to 0.0 by tolerance + 30s."""
    if left_s <= 0 or right_s <= 0:
        return 0.5  # Unknown duration should neither help nor hurt much.
    delta = abs(left_s - right_s)
    if delta <= tolerance_s:
        return 1.0
    fade = 30.0
    return max(0.0, 1.0 - (delta - tolerance_s) / fade)


@dataclass(frozen=True)
class Candidate:
    """A Spotify search result, reduced to what matching needs."""

    uri: str
    name: str
    artists: list[str]
    duration_s: int
    album: str = ""
    isrc: str | None = None


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    score: float
    reason: str


def score_candidate(
    *,
    title: str,
    artists: list[str],
    duration_s: int,
    candidate: Candidate,
    duration_tolerance_s: int = 8,
) -> float:
    """Score a Spotify candidate against a Deezer track, in the range 0..1."""
    source_title = normalize_title(title)
    target_title = normalize_title(candidate.name)

    title_score = similarity(source_title, target_title)
    artists_score = artist_similarity(artists, candidate.artists)
    length_score = duration_score(duration_s, candidate.duration_s, duration_tolerance_s)

    score = 0.50 * title_score + 0.35 * artists_score + 0.15 * length_score

    # An artist mismatch is disqualifying, not merely a deduction.
    if artists and candidate.artists and artists_score < ARTIST_FLOOR:
        return min(score, _DISQUALIFIED_SCORE)

    # A live/remix/acoustic marker on exactly one side means these are almost
    # certainly different recordings.
    if version_markers(title) != version_markers(candidate.name):
        score -= 0.25

    if looks_like_impostor(candidate.artists, candidate.name) and not looks_like_impostor(
        artists, title
    ):
        score -= 0.5

    return max(0.0, min(1.0, score))


def best_match(
    *,
    title: str,
    artists: list[str],
    duration_s: int,
    candidates: list[Candidate],
    threshold: float,
    duration_tolerance_s: int = 8,
) -> ScoredCandidate | None:
    """Pick the highest-scoring candidate above the threshold, if any."""
    best: ScoredCandidate | None = None
    for candidate in candidates:
        score = score_candidate(
            title=title,
            artists=artists,
            duration_s=duration_s,
            candidate=candidate,
            duration_tolerance_s=duration_tolerance_s,
        )
        if best is None or score > best.score:
            best = ScoredCandidate(
                candidate=candidate,
                score=score,
                reason=f"score={score:.2f}",
            )
    if best is None or best.score < threshold:
        return None
    return best
