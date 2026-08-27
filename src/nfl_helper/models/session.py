"""Multi-manager session and league profile domain models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class PlatformType(StrEnum):
    """Supported fantasy football platforms."""

    ESPN = "espn"
    SLEEPER = "sleeper"


class LeagueProfile(BaseModel):
    """Configuration and credential profile for a specific league/team session."""

    session_id: str
    platform: PlatformType
    league_id: str
    league_name: str
    season_year: int
    team_id: str
    team_name: str
    user_draft_slot: int = 1
    espn_s2: str | None = None
    swid: str | None = None
    invite_code: str
    is_claimed: bool = False
    claimed_at: datetime | None = None
    custom_scoring: dict[str, float] = Field(default_factory=dict)
    roster_slots: dict[str, int] = Field(default_factory=dict)
