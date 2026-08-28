"""Utilities for calculating active NFL season, team byes, and stadium climate."""

from datetime import UTC, datetime

# Active NFL season bye week mapping by team
NFL_TEAM_BYES: dict[str, int] = {
    "ARI": 11,
    "ATL": 12,
    "BAL": 14,
    "BUF": 12,
    "CAR": 11,
    "CHI": 7,
    "CIN": 12,
    "CLE": 10,
    "DAL": 7,
    "DEN": 14,
    "DET": 5,
    "GB": 10,
    "HOU": 14,
    "IND": 14,
    "JAX": 12,
    "KC": 6,
    "LAC": 5,
    "LAR": 6,
    "LV": 10,
    "MIA": 6,
    "MIN": 6,
    "NE": 14,
    "NO": 12,
    "NYG": 11,
    "NYJ": 12,
    "PHI": 5,
    "PIT": 9,
    "SEA": 10,
    "SF": 9,
    "TB": 11,
    "TEN": 5,
    "WAS": 14,
    "WSH": 14,
}

# NFL teams with indoor dome or retractable-roof home stadiums
DOME_STADIUM_TEAMS: set[str] = {
    "ARI",
    "ATL",
    "DAL",
    "DET",
    "HOU",
    "IND",
    "LAC",
    "LAR",
    "LV",
    "MIN",
    "NO",
}


def get_current_nfl_season_year(reference_date: datetime | None = None) -> int:
    """Determine active NFL season year based on calendar date without manual input."""
    date = reference_date or datetime.now(UTC)
    # January and February belong to the previous year's NFL season campaign
    if date.month in (1, 2):
        return date.year - 1
    return date.year


def get_team_bye_week(team: str) -> int | None:
    """Return the NFL regular season bye week for a given team abbreviation."""
    return NFL_TEAM_BYES.get(team.upper())


def is_dome_stadium(team: str) -> bool:
    """Determine whether the specified NFL team plays in a dome or indoor stadium."""
    return team.upper() in DOME_STADIUM_TEAMS
