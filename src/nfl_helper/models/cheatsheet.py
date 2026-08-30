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


class PositionalQuotaDeadline(BaseModel):
    """Mid-draft positional minimum acquisition deadlines (e.g. 4 RBs in first 10 rounds)."""

    position: str
    required_count: int
    deadline_round: int
    rule_description: str = ""


class PositionalStrategyBranch(BaseModel):
    """Specific conditional strategy branch within a positional rule."""

    branch_id: str = ""
    trigger_drafted_tiers: list[int] = Field(default_factory=list)
    max_position_cap: int | None = None
    target_rounds: list[int] = Field(default_factory=list)
    target_tiers: list[int] = Field(default_factory=list)
    target_tier_quotas: dict[int, int] = Field(default_factory=dict)
    top_n_target: int | None = None


class PositionalStrategyRule(BaseModel):
    """Position-level draft targets, conditional branching, and tier acquisition rules."""

    position: str
    target_rounds: list[int] = Field(default_factory=list)
    target_tiers: list[int] = Field(default_factory=list)
    top_n_target: int | None = None
    conditional_max_count: dict[int, int] = Field(default_factory=dict)
    default_max_cap: int = 2
    branches: list[PositionalStrategyBranch] = Field(default_factory=list)
    no_second_if_top_tier: bool = False
    rule_description: str = ""


class CheatsheetContext(BaseModel):
    """Aggregated cheatsheet ranking, tier, and overall draft strategy metadata."""

    entries: dict[str, CheatsheetEntry] = Field(default_factory=dict)
    strategy_rules: list[str] = Field(default_factory=list)
    round_targets: list[DraftRoundTarget] = Field(default_factory=list)
    positional_strategy: list[PositionalStrategyRule] = Field(default_factory=list)
    quota_deadlines: list[PositionalQuotaDeadline] = Field(default_factory=list)
    positional_tiers: dict[str, list[list[str]]] = Field(default_factory=dict)
