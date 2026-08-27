"""Draft state, snake lookahead, tier clustering, and cliff models."""

from pydantic import BaseModel, Field

from nfl_helper.models.player import Player


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
    cliff_risk: str = Field(description="CRITICAL, HIGH, MODERATE, or LOW")
    next_tier_drop_points: float = 0.0
    recommended_action: str


class DraftSuggestion(BaseModel):
    """Tactical pick suggestion derived from VORP and tier cliff alerts."""

    rank: int
    player: Player
    reason: str
    vorp: float = 0.0
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
    picks_until_user_turn: int = 0
    is_user_on_the_clock: bool = False
    recent_picks: list[DraftPick] = Field(default_factory=list)
    available_players_by_pos: dict[str, list[Player]] = Field(default_factory=dict)
    tiers_by_position: dict[str, list[PlayerTier]] = Field(default_factory=dict)
    cliff_warnings: list[TierCliffWarning] = Field(default_factory=list)
    top_suggestions: list[DraftSuggestion] = Field(default_factory=list)
