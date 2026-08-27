"""Positional tier clustering and 3-scenario cliff detection."""

from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.draft import CliffType, PlayerTier, TierCliffWarning
from nfl_helper.models.player import Player

# Refined realistic drop-off thresholds (single step drop, maximum tier span)
_DROP_THRESHOLDS: dict[str, tuple[float, float]] = {
    "QB": (2.2, 4.5),
    "RB": (1.8, 4.0),
    "WR": (1.8, 4.0),
    "TE": (2.0, 4.5),
    "K": (0.8, 1.8),
    "D/ST": (0.8, 1.8),
}
_DEFAULT_THRESHOLD: tuple[float, float] = (1.8, 4.0)


def _cluster_hybrid(players: list[Player], position: str) -> list[PlayerTier]:
    """Hybrid clustering: combines empirical point drop-offs with cheatsheet tier guidance."""
    if not players:
        return []

    sorted_p = sorted(players, key=lambda x: x.projected_points, reverse=True)
    single_drop, max_span = _DROP_THRESHOLDS.get(position.upper(), _DEFAULT_THRESHOLD)

    clusters: list[list[Player]] = [[sorted_p[0]]]
    current_tier_max = sorted_p[0].projected_points
    current_cs_tier = sorted_p[0].cheatsheet_tier or sorted_p[0].tier

    for p in sorted_p[1:]:
        last_pts = clusters[-1][-1].projected_points
        curr_pts = p.projected_points
        step_drop = last_pts - curr_pts
        span_drop = current_tier_max - curr_pts
        p_cs_tier = p.cheatsheet_tier or p.tier

        # Cheatsheet tier transition supported by a non-trivial difference
        cs_transition = (
            p_cs_tier is not None
            and current_cs_tier is not None
            and p_cs_tier > current_cs_tier
            and (step_drop >= 0.4 or span_drop >= 1.5)
        )

        # Statistical step or span drop
        stat_transition = step_drop >= single_drop or span_drop >= max_span

        if stat_transition or cs_transition:
            clusters.append([p])
            current_tier_max = curr_pts
            current_cs_tier = p_cs_tier
        else:
            clusters[-1].append(p)

    tiers: list[PlayerTier] = []
    first_tier_hint = sorted_p[0].cheatsheet_tier or sorted_p[0].tier or 1
    for idx, cluster in enumerate(clusters, start=first_tier_hint):
        avg_pts = sum(p.projected_points for p in cluster) / len(cluster)
        tiers.append(
            PlayerTier(
                tier_num=idx,
                position=position,
                players=cluster,
                avg_projected=round(avg_pts, 2),
                count=len(cluster),
            )
        )
    return tiers


def cluster_position_tiers(
    players: list[Player], position: str, cheatsheet_context: CheatsheetContext | None = None
) -> list[PlayerTier]:
    """Group available position players into ordered tiers using hybrid clustering."""
    pos_players = [p for p in players if p.position == position]
    if not pos_players:
        return []

    return _cluster_hybrid(pos_players, position)


def calculate_tier_drop(current_tier: PlayerTier, next_tier: PlayerTier | None) -> float:
    """Compute average projection drop between current and subsequent tier."""
    if not next_tier or next_tier.avg_projected <= 0.0:
        return 0.0
    return max(0.0, round(current_tier.avg_projected - next_tier.avg_projected, 1))


def _evaluate_on_the_clock_cliff(
    tier: PlayerTier, next_tier: PlayerTier | None, snake_turn_gap: int
) -> TierCliffWarning | None:
    """Evaluate cliff risk when user is currently on the clock."""
    drop = calculate_tier_drop(tier, next_tier)
    if drop < 0.8:
        return None

    remaining = len(tier.players)
    tier_size = max(remaining, tier.count)
    is_percentage_scarce = (remaining / tier_size) <= 0.35 if tier_size > 0 else False
    is_gap_scarce = remaining <= max(2, (snake_turn_gap + 2) // 3)

    if not (is_percentage_scarce or is_gap_scarce or remaining <= 2):
        return None

    risk = "CRITICAL" if (remaining == 1 or drop >= 2.5) else "HIGH"
    next_num = next_tier.tier_num if next_tier else tier.tier_num + 1
    action = (
        f"Only {remaining} of {tier_size} Tier {tier.tier_num} {tier.position} remaining ({round(remaining / tier_size * 100)}% left) "
        f"before a {snake_turn_gap}-pick turn gap. Draft now to avoid dropping -{drop:.1f} pts to Tier {next_num}."
    )
    return TierCliffWarning(
        position=tier.position,
        current_tier=tier.tier_num,
        players_remaining=remaining,
        picks_until_turn=0,
        snake_turn_gap=snake_turn_gap,
        cliff_risk=risk,
        cliff_type=CliffType.ON_THE_CLOCK_CLIFF,
        next_tier_drop_points=drop,
        recommended_action=action,
    )


def _evaluate_waiting_cliff(
    tier: PlayerTier,
    next_tier: PlayerTier | None,
    picks_until_turn: int,
    snake_turn_gap: int,
    current_pick: int = 1,
) -> TierCliffWarning | None:
    """Evaluate cliff risk when user is waiting for upcoming pick with dynamic ADP proximity."""
    drop = calculate_tier_drop(tier, next_tier)
    if drop < 0.8:
        return None

    remaining = len(tier.players)
    tier_size = max(remaining, tier.count)

    # Dynamic ADP Proximity: check if this tier is expected to be drafted in the upcoming window
    adps = [p.adp for p in tier.players if p.adp is not None]
    if adps:
        avg_adp = sum(adps) / len(adps)
        in_draft_range = (avg_adp <= current_pick + snake_turn_gap + 15) or (remaining < tier_size)
        if not in_draft_range:
            return None

    # Only alert for actionable upcoming turn cliffs (tier survives to your pick but depletes during turn gap)
    survives_to_turn = remaining > picks_until_turn
    wipes_in_gap = remaining <= (picks_until_turn + max(2, (snake_turn_gap + 2) // 3))
    is_tier_draining = (remaining / tier_size) <= 0.50 or remaining <= 3

    if survives_to_turn and wipes_in_gap and is_tier_draining and snake_turn_gap >= 4:
        risk = "HIGH" if remaining <= picks_until_turn + 1 else "MODERATE"
        action = (
            f"{remaining} of {tier_size} Tier {tier.tier_num} {tier.position} left. Tier will survive to your pick in {picks_until_turn} turns "
            f"but will deplete during your {snake_turn_gap}-pick turn gap. Target Tier {tier.tier_num} at your upcoming pick."
        )
        return TierCliffWarning(
            position=tier.position,
            current_tier=tier.tier_num,
            players_remaining=remaining,
            picks_until_turn=picks_until_turn,
            snake_turn_gap=snake_turn_gap,
            cliff_risk=risk,
            cliff_type=CliffType.UPCOMING_TURN_CLIFF,
            next_tier_drop_points=drop,
            recommended_action=action,
        )

    return None


def detect_tier_cliffs(
    tiers_by_pos: dict[str, list[PlayerTier]],
    picks_until_turn: int,
    snake_turn_gap: int,
    is_on_the_clock: bool,
    current_pick: int = 1,
) -> list[TierCliffWarning]:
    """Identify 3-scenario positional tier cliffs across available player tiers."""
    warnings: list[TierCliffWarning] = []
    risk_rank = {"CRITICAL": 0, "HIGH": 1, "MODERATE": 2, "LOW": 3}

    for tiers in tiers_by_pos.values():
        active_tiers = [t for t in tiers if len(t.players) > 0]
        if not active_tiers:
            continue

        top_tier = active_tiers[0]
        next_tier = active_tiers[1] if len(active_tiers) > 1 else None

        warning: TierCliffWarning | None = None
        if is_on_the_clock or picks_until_turn <= 0:
            warning = _evaluate_on_the_clock_cliff(top_tier, next_tier, snake_turn_gap)
        else:
            warning = _evaluate_waiting_cliff(top_tier, next_tier, picks_until_turn, snake_turn_gap, current_pick)

        if warning:
            warnings.append(warning)

    warnings.sort(key=lambda w: (risk_rank.get(w.cliff_risk, 4), -w.next_tier_drop_points))
    return warnings
