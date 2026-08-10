from __future__ import annotations

import pytest

from musicsync.matching import (
    Candidate,
    artist_similarity,
    best_match,
    duration_score,
    looks_like_impostor,
    normalize_title,
    score_candidate,
    version_markers,
)


class TestNormalizeTitle:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Bohemian Rhapsody", "bohemian rhapsody"),
            ("Bohemian Rhapsody (2011 Remaster)", "bohemian rhapsody"),
            ("Bohemian Rhapsody - Remastered 2011", "bohemian rhapsody"),
            ("Song (Album Version)", "song"),
            ("Song [Bonus Track]", "song"),
            ("Song (Deluxe Edition)", "song"),
            ("Café del Mar", "cafe del mar"),
            ("Song (feat. Someone)", "song"),
            ("Song feat. Someone", "song"),
            ("Song - feat. Someone", "song"),
            ("  Multiple   Spaces  ", "multiple spaces"),
        ],
    )
    def test_cosmetic_markers_are_removed(self, raw: str, expected: str) -> None:
        assert normalize_title(raw) == expected

    def test_meaningful_markers_are_kept(self) -> None:
        # These denote a different recording and must survive normalization.
        assert "live" in normalize_title("Song (Live)")
        assert "remix" in normalize_title("Song (Club Remix)")
        assert "acoustic" in normalize_title("Song - Acoustic")


class TestVersionMarkers:
    def test_detects_markers(self) -> None:
        assert version_markers("Song (Live at Wembley)") == {"live"}
        assert version_markers("Song (Acoustic Version)") == {"acoustic"}
        assert version_markers("Plain Song") == set()

    def test_remix_does_not_trigger_mix(self) -> None:
        assert version_markers("Song (Remix)") == {"remix"}


class TestArtistSimilarity:
    def test_identical(self) -> None:
        assert artist_similarity(["Daft Punk"], ["Daft Punk"]) == 1.0

    def test_case_and_accent_insensitive(self) -> None:
        assert artist_similarity(["BEYONCÉ"], ["Beyonce"]) == 1.0

    def test_containment_scores_high(self) -> None:
        assert artist_similarity(["Beyoncé"], ["Beyoncé", "Jay-Z"]) >= 0.9

    def test_ordering_does_not_matter(self) -> None:
        assert artist_similarity(["A", "B"], ["B", "A"]) >= 0.9

    def test_unrelated_artists_score_low(self) -> None:
        assert artist_similarity(["Daft Punk"], ["Metallica"]) < 0.4

    def test_empty_inputs(self) -> None:
        assert artist_similarity([], ["Anyone"]) == 0.0
        assert artist_similarity(["Anyone"], []) == 0.0


class TestDurationScore:
    def test_within_tolerance_is_perfect(self) -> None:
        assert duration_score(200, 205, 8) == 1.0

    def test_decays_beyond_tolerance(self) -> None:
        assert 0.0 < duration_score(200, 220, 8) < 1.0

    def test_far_apart_is_zero(self) -> None:
        assert duration_score(200, 300, 8) == 0.0

    def test_unknown_duration_is_neutral(self) -> None:
        assert duration_score(0, 200, 8) == 0.5


def candidate(
    name: str, artists: list[str], duration: int = 200, uri: str = "spotify:track:x"
) -> Candidate:
    return Candidate(uri=uri, name=name, artists=artists, duration_s=duration)


class TestScoreCandidate:
    def test_exact_match_scores_near_one(self) -> None:
        score = score_candidate(
            title="Get Lucky",
            artists=["Daft Punk"],
            duration_s=248,
            candidate=candidate("Get Lucky", ["Daft Punk"], 248),
        )
        assert score > 0.95

    def test_remaster_suffix_still_matches(self) -> None:
        score = score_candidate(
            title="Bohemian Rhapsody",
            artists=["Queen"],
            duration_s=354,
            candidate=candidate("Bohemian Rhapsody - 2011 Remaster", ["Queen"], 355),
        )
        assert score > 0.9

    def test_live_version_is_penalised(self) -> None:
        studio = score_candidate(
            title="Bohemian Rhapsody",
            artists=["Queen"],
            duration_s=354,
            candidate=candidate("Bohemian Rhapsody", ["Queen"], 354),
        )
        live = score_candidate(
            title="Bohemian Rhapsody",
            artists=["Queen"],
            duration_s=354,
            candidate=candidate("Bohemian Rhapsody (Live)", ["Queen"], 354),
        )
        assert live < studio - 0.2

    def test_karaoke_cover_is_rejected(self) -> None:
        score = score_candidate(
            title="Get Lucky",
            artists=["Daft Punk"],
            duration_s=248,
            candidate=candidate(
                "Get Lucky", ["Karaoke Hits Band"], 248
            ),
        )
        assert score < 0.5

    def test_wrong_artist_is_disqualified_despite_perfect_title(self) -> None:
        # Same title, same length, unrelated artist: a very common trap for
        # generic titles. The artist floor must win over title and duration.
        score = score_candidate(
            title="Get Lucky",
            artists=["Daft Punk"],
            duration_s=248,
            candidate=candidate("Get Lucky", ["Some Other Guy"], 248),
        )
        assert score <= 0.35

    def test_artist_floor_tolerates_spelling_differences(self) -> None:
        for source, target in [
            ("Tiësto", "Tiesto"),
            ("P!nk", "Pink"),
            ("JAY-Z", "Jay Z"),
            ("Beyoncé", "Beyoncé, Jay-Z"),
        ]:
            score = score_candidate(
                title="Some Song",
                artists=[source],
                duration_s=200,
                candidate=candidate("Some Song", [target], 200),
            )
            assert score > 0.72, f"{source!r} vs {target!r} scored {score:.2f}"

    def test_score_is_bounded(self) -> None:
        score = score_candidate(
            title="X",
            artists=["Karaoke Band"],
            duration_s=1,
            candidate=candidate("Totally Different", ["Nobody"], 999),
        )
        assert 0.0 <= score <= 1.0


class TestBestMatch:
    def test_picks_the_highest_scorer(self) -> None:
        result = best_match(
            title="Get Lucky",
            artists=["Daft Punk"],
            duration_s=248,
            candidates=[
                candidate("Get Lucky (Live)", ["Daft Punk"], 250, uri="a"),
                candidate("Get Lucky", ["Daft Punk"], 248, uri="b"),
            ],
            threshold=0.72,
        )
        assert result is not None
        assert result.candidate.uri == "b"

    def test_returns_none_below_threshold(self) -> None:
        assert (
            best_match(
                title="Get Lucky",
                artists=["Daft Punk"],
                duration_s=248,
                candidates=[candidate("Something Else", ["Nobody"], 100)],
                threshold=0.72,
            )
            is None
        )

    def test_empty_candidate_list(self) -> None:
        assert (
            best_match(
                title="X", artists=["Y"], duration_s=1, candidates=[], threshold=0.5
            )
            is None
        )


def test_looks_like_impostor() -> None:
    assert looks_like_impostor(["Karaoke All Stars"], "Song")
    assert looks_like_impostor(["Some Band"], "Song (In the Style of Queen)")
    assert not looks_like_impostor(["Queen"], "Bohemian Rhapsody")
