"""Domain models for cheatsheet rankings, tiers, and strategic drafting rules."""

from pydantic import BaseModel, Field


class CheatsheetEntry(BaseModel):
    """Parsed cheatsheet record for an individual player."""

    player_name: str
    normalized_name: str
    position: str = ""
    team: str | None = ""
    tier: int | None = None
    adp: float | None = None
    is_injured: bool = False
    notes: str | None = None


class DraftRoundTarget(BaseModel):
    """Strategic positional guidance for specific draft rounds."""

    target_rounds: list[int] = Field(default_factory=list)
    allowed_positions: list[str] = Field(default_factory=list)
    min_counts: dict[str, int] = Field(default_factory=dict)
    rule_description: str = ""


class PositionalStrategyRule(BaseModel):
    """Position-level draft targets and tier acquisition rules."""

    position: str
    target_rounds: list[int] = Field(default_factory=list)
    target_tiers: list[int] = Field(default_factory=list)
    top_n_target: int | None = None
    rule_description: str = ""


class CheatsheetContext(BaseModel):
    """Aggregated cheatsheet ranking, tier, and overall draft strategy metadata."""

    entries: dict[str, CheatsheetEntry] = Field(default_factory=dict)
    strategy_rules: list[str] = Field(default_factory=list)
    round_targets: list[DraftRoundTarget] = Field(default_factory=list)
    positional_strategy: list[PositionalStrategyRule] = Field(default_factory=list)
    positional_tiers: dict[str, list[list[str]]] = Field(default_factory=dict)
