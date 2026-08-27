"""Unit tests for season year calculation and player name normalization."""

from datetime import UTC, datetime

from nfl_helper.core.name_normalizer import normalize_player_name
from nfl_helper.core.season_utils import get_current_nfl_season_year


def test_season_year_calculation() -> None:
    """Verify automatic NFL season year calculation across calendar boundaries."""
    # September 2024 belongs to 2024 season
    sep_date = datetime(2024, 9, 15, tzinfo=UTC)
    assert get_current_nfl_season_year(sep_date) == 2024

    # January 2025 (playoffs) belongs to 2024 regular season
    jan_date = datetime(2025, 1, 20, tzinfo=UTC)
    assert get_current_nfl_season_year(jan_date) == 2024

    # February 2025 (Super Bowl) belongs to 2024 regular season
    feb_date = datetime(2025, 2, 10, tzinfo=UTC)
    assert get_current_nfl_season_year(feb_date) == 2024

    # March 2025 (new league year) belongs to 2025 season
    mar_date = datetime(2025, 3, 15, tzinfo=UTC)
    assert get_current_nfl_season_year(mar_date) == 2025


def test_name_normalizer_accents_and_punctuation() -> None:
    """Verify stripping accents, diacritics, and punctuation."""
    assert normalize_player_name("Christian McCaffréy") == "christian mccaffrey"
    assert normalize_player_name("De'Von Achane") == "devon achane"
    assert normalize_player_name("A.J. Brown") == "aj brown"
    assert normalize_player_name("Amon-Ra St. Brown") == "amon-ra st brown"


def test_name_normalizer_suffixes() -> None:
    """Verify stripping suffixes (Jr, III, etc.)."""
    assert normalize_player_name("Kenneth Walker III") == "kenneth walker"
    assert normalize_player_name("Travis Etienne Jr.") == "travis etienne"
    assert normalize_player_name("Marvin Harrison Jr") == "marvin harrison"
    assert normalize_player_name("Patrick Mahomes II") == "patrick mahomes"
