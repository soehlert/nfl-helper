"""Player and position domain models with strict Python 3.13 typing."""

from enum import StrEnum

from pydantic import BaseModel, Field


class Position(StrEnum):
    """Standard NFL fantasy football positions."""

    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    K = "K"
    DST = "D/ST"
    FLEX = "FLEX"
    SUPERFLEX = "SUPERFLEX"


class InjuryStatus(StrEnum):
    """Player injury and availability status classifications."""

    ACTIVE = "ACTIVE"
    QUESTIONABLE = "QUESTIONABLE"
    DOUBTFUL = "DOUBTFUL"
    OUT = "OUT"
    IR = "IR"
    SUSPENDED = "SUSPENDED"


class PlayerMatchupScore(BaseModel):
    """Matchup difficulty rating for an upcoming game."""

    week: int
    opponent: str
    opponent_rank: int = Field(description="Defensive rank vs position, 1=toughest, 32=easiest")
    difficulty_rating: float = Field(default=5.0, description="Normalized difficulty score from 1.0 (hard) to 10.0 (easy)")


class Player(BaseModel):
    """Canonical player model across ESPN and Sleeper platforms."""

    id: str
    name: str
    position: Position | str
    team: str
    projected_points: float = 0.0
    actual_points: float = 0.0
    average_points: float = 0.0
    tier: int = 1
    injury_status: InjuryStatus | str = InjuryStatus.ACTIVE
    eligible_slots: list[str] = Field(default_factory=list)
    opponent: str | None = None
    opponent_rank: int | None = None
    matchups_3wk: list[PlayerMatchupScore] = Field(default_factory=list)
    cheatsheet_notes: str | None = None
    cheatsheet_tier: int | None = None
    cheatsheet_rank: int | None = None
    is_starter: bool = False
