"""Utilities for calculating active NFL season and schedules."""

from datetime import UTC, datetime


def get_current_nfl_season_year(reference_date: datetime | None = None) -> int:
    """Determine active NFL season year based on calendar date without manual input."""
    date = reference_date or datetime.now(UTC)
    # January and February belong to the previous year's NFL season campaign
    if date.month in (1, 2):
        return date.year - 1
    return date.year
