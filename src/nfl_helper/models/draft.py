"""Draft state, snake lookahead, tier clustering, and cliff models."""

from enum import StrEnum

from pydantic import BaseModel, Field

from nfl_helper.models.player import Player


class CliffType(StrEnum):
    """Type of cliff scenario based on pick distance and snake turn gap."""

    ON_THE_CLOCK_CLIFF = "ON_THE_CLOCK_CLIFF"
    UPCOMING_TURN_CLIFF = "UPCOMING_TURN_CLIFF"
    DEPLETED_BEFORE_TURN = "DEPLETED_BEFORE_TURN"


class DraftPick(BaseModel):
    """Represents a single pick made in a fantasy draft."""

    round_num: int
    round_pick: int
    overall_pick: int
    team_id: str
    team_name: str
    player_id: str
    player_name: str
    position: str


class PlayerTier(BaseModel):
    """Group of players clustered in the same projection/value tier for a position."""

    tier_num: int
    position: str
    players: list[Player] = Field(default_factory=list)
    avg_projected: float = 0.0
    count: int = 0


class TierCliffWarning(BaseModel):
    """Alert triggered when an elite or current positional tier is about to deplete."""

    position: str
    current_tier: int
    players_remaining: int
    picks_until_turn: int
    snake_turn_gap: int = 0
    cliff_risk: str = Field(description="CRITICAL, HIGH, MODERATE, or LOW")
    cliff_type: CliffType | str = CliffType.ON_THE_CLOCK_CLIFF
    next_tier_drop_points: float = 0.0
    recommended_action: str


class DraftSuggestion(BaseModel):
    """Tactical pick suggestion derived from VORP, tiers, cliffs, rules, and ADP value."""

    rank: int
    player: Player
    reason: str
    vorp: float = 0.0
    score: float = 0.0
    is_cliff_defense: bool = False


class DraftState(BaseModel):
    """Full real-time snapshot of the draft board and calculated recommendations."""

    league_id: str
    draft_id: str | None = None
    is_complete: bool = False
    total_rounds: int = 16
    total_teams: int = 12
    current_pick: int = 1
    current_round: int = 1
    user_draft_slot: int = 1
    user_team_id: str | None = None
    picks_until_user_turn: int = 0
    snake_turn_gap: int = 0
    is_user_on_the_clock: bool = False
    capped_positions: list[str] = Field(default_factory=list)
    user_drafted_roster_counts: dict[str, int] = Field(default_factory=dict)
    recent_picks: list[DraftPick] = Field(default_factory=list)
    available_players_by_pos: dict[str, list[Player]] = Field(default_factory=dict)
    tiers_by_position: dict[str, list[PlayerTier]] = Field(default_factory=dict)
    cliff_warnings: list[TierCliffWarning] = Field(default_factory=list)
    top_suggestions: list[DraftSuggestion] = Field(default_factory=list)
    strategy_alerts: list[str] = Field(default_factory=list)
