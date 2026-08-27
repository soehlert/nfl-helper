"""Roster management, ILP lineup optimization, and waiver analysis models."""

from enum import StrEnum

from pydantic import BaseModel, Field

from nfl_helper.models.player import Player


class OptimizationStrategy(StrEnum):
    """Tactical optimization mode for start/sit solver."""

    BALANCED = "BALANCED"
    CEILING = "CEILING"
    FLOOR = "FLOOR"


class RosterSlotRequirement(BaseModel):
    """Definition of a starting roster slot requirement."""

    slot_name: str
    count: int
    eligible_positions: list[str] = Field(default_factory=list)


class TeamRoster(BaseModel):
    """Full roster for a fantasy team including starters, bench, and reserve."""

    team_id: str
    team_name: str
    manager_name: str = ""
    players: list[Player] = Field(default_factory=list)
    starters: list[Player] = Field(default_factory=list)
    bench: list[Player] = Field(default_factory=list)
    ir: list[Player] = Field(default_factory=list)


class RosterAdjustment(BaseModel):
    """Recommendation to move an injured or inactive player to IR/bench."""

    player_name: str
    player_id: str
    position: str
    current_slot: str
    suggested_slot: str
    reason: str
    injury_status: str


class LineupSolution(BaseModel):
    """Optimal lineup output solved via PuLP Integer Linear Programming."""

    team_id: str
    strategy: OptimizationStrategy = OptimizationStrategy.BALANCED
    optimal_starters: list[Player] = Field(default_factory=list)
    optimal_bench: list[Player] = Field(default_factory=list)
    current_projected_total: float = 0.0
    optimal_projected_total: float = 0.0
    projected_delta: float = 0.0
    start_recommendations: list[str] = Field(default_factory=list)
    sit_recommendations: list[str] = Field(default_factory=list)
    ir_warnings: list[RosterAdjustment] = Field(default_factory=list)
    anti_correlation_warnings: list[str] = Field(default_factory=list)
    solver_status: str = "Optimal"


class AddDropRecommendation(BaseModel):
    """Actionable pair of free-agent pickup and roster drop candidate."""

    add_player: Player
    drop_player: Player | None = None
    position: str
    net_projected_gain: float = 0.0
    matchup_advantage_3wk: float = 0.0
    reason: str


class StreamingOption(BaseModel):
    """Specialized streaming candidate for D/ST or Kicker with favorable matchup."""

    player: Player
    position: str
    week_matchup: str
    opponent_rank: int
    projected_points: float = 0.0
    tier: int = 1
    reason: str


class WaiverAnalysis(BaseModel):
    """Complete weekly waiver-wire and streaming analysis for a team."""

    team_id: str
    positional_weaknesses: dict[str, float] = Field(default_factory=dict)
    top_add_drop_pairs: list[AddDropRecommendation] = Field(default_factory=list)
    dst_streaming: list[StreamingOption] = Field(default_factory=list)
    kicker_streaming: list[StreamingOption] = Field(default_factory=list)
