"""Domain models for cheatsheet dry-run diffs and mover analytics."""

from pydantic import BaseModel, Field


class PlayerMover(BaseModel):
    """Represents a player whose rank or tier shifted under a candidate cheatsheet."""

    player_name: str
    position: str
    team: str = ""
    old_rank: int
    new_rank: int
    rank_delta: int
    old_tier: int | None = None
    new_tier: int | None = None
    tier_delta: int | None = None
    is_injury_update: bool = False
    note: str = ""


class CheatsheetDiffReport(BaseModel):
    """Structured report highlighting key impact movers and rule changes."""

    top_risers: list[PlayerMover] = Field(default_factory=list)
    top_fallers: list[PlayerMover] = Field(default_factory=list)
    tier_upgrades: list[PlayerMover] = Field(default_factory=list)
    tier_downgrades: list[PlayerMover] = Field(default_factory=list)
    added_rules: list[str] = Field(default_factory=list)
    removed_rules: list[str] = Field(default_factory=list)
    total_players_affected: int = 0
    total_rules_affected: int = 0
