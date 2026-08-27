"""Positional tier clustering and 3-scenario cliff detection."""

from collections import defaultdict

from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.draft import CliffType, PlayerTier, TierCliffWarning
from nfl_helper.models.player import Player

# Thresholds for statistical drop-off clustering
_DROP_THRESHOLDS: dict[str, tuple[float, float]] = {
    "QB": (1.8, 3.5),
    "RB": (1.4, 2.8),
    "WR": (1.4, 2.8),
    "TE": (1.8, 3.5),
    "K": (1.2, 2.0),
    "D/ST": (1.2, 2.0),
}
_DEFAULT_THRESHOLD: tuple[float, float] = (1.5, 3.0)


def _cluster_by_cheatsheet(players: list[Player], position: str) -> list[PlayerTier]:
    """Group players by explicit cheatsheet tiers and smoothly cluster unranked players."""
    grouped: dict[int, list[Player]] = defaultdict(list)
    unranked: list[Player] = []

    for p in players:
        tier_val = p.cheatsheet_tier if (p.cheatsheet_tier is not None and p.cheatsheet_tier > 0) else p.tier
        if tier_val is not None and tier_val > 0:
            grouped[tier_val].append(p)
        else:
            unranked.append(p)

    # If no players had a valid tier, fall back to statistical clustering
    if not grouped:
        return _cluster_statistically(players, position)

    # If unranked players exist, cluster them into sequential tiers following max tier
    if unranked:
        max_tier = max(grouped.keys())
        unranked_tiers = _cluster_statistically(unranked, position)
        for idx, u_tier in enumerate(unranked_tiers, start=max_tier + 1):
            grouped[idx].extend(u_tier.players)

    tiers: list[PlayerTier] = []
    for tier_num in sorted(grouped.keys()):
        tier_players = sorted(grouped[tier_num], key=lambda x: x.projected_points, reverse=True)
        avg_pts = sum(p.projected_points for p in tier_players) / len(tier_players) if tier_players else 0.0
        tiers.append(
            PlayerTier(
                tier_num=tier_num,
                position=position,
                players=tier_players,
                avg_projected=round(avg_pts, 2),
                count=len(tier_players),
            )
        )
    return tiers


def _cluster_statistically(players: list[Player], position: str) -> list[PlayerTier]:
    """Group players into tiers using point drop-off thresholds."""
    if not players:
        return []

    sorted_p = sorted(players, key=lambda x: x.projected_points, reverse=True)
    single_drop, max_span = _DROP_THRESHOLDS.get(position.upper(), _DEFAULT_THRESHOLD)

    clusters: list[list[Player]] = [[sorted_p[0]]]
    current_tier_max = sorted_p[0].projected_points

    for p in sorted_p[1:]:
        last_pts = clusters[-1][-1].projected_points
        curr_pts = p.projected_points
        step_drop = last_pts - curr_pts
        span_drop = current_tier_max - curr_pts

        if step_drop >= single_drop or span_drop >= max_span:
            clusters.append([p])
            current_tier_max = curr_pts
        else:
            clusters[-1].append(p)

    tiers: list[PlayerTier] = []
    for idx, cluster in enumerate(clusters, start=1):
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
    """Group available position players into ordered tiers."""
    pos_players = [p for p in players if p.position == position]
    if not pos_players:
        return []

    # Use explicit cheatsheet tiering if players at this position have cheatsheet tiers
    has_cheatsheet_tiers = any(p.cheatsheet_tier is not None and p.cheatsheet_tier > 0 for p in pos_players)
    if has_cheatsheet_tiers:
        return _cluster_by_cheatsheet(pos_players, position)

    return _cluster_statistically(pos_players, position)


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
    if drop < 1.0:
        return None

    remaining = len(tier.players)
    tier_size = max(remaining, tier.count)
    is_percentage_scarce = (remaining / tier_size) <= 0.30 if tier_size > 0 else False
    is_gap_scarce = remaining <= max(2, (snake_turn_gap + 2) // 3)

    if not (is_percentage_scarce or is_gap_scarce or remaining <= 2):
        return None

    risk = "CRITICAL" if (remaining == 1 or drop >= 3.0) else "HIGH"
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
    tier: PlayerTier, next_tier: PlayerTier | None, picks_until_turn: int, snake_turn_gap: int
) -> TierCliffWarning | None:
    """Evaluate cliff risk when user is waiting for upcoming pick."""
    drop = calculate_tier_drop(tier, next_tier)
    if drop < 1.0:
        return None

    remaining = len(tier.players)
    tier_size = max(remaining, tier.count)

    # Only alert for actionable upcoming turn cliffs (tier survives to your pick but depletes during turn gap)
    survives_to_turn = remaining > picks_until_turn
    wipes_in_gap = remaining <= (picks_until_turn + max(2, (snake_turn_gap + 2) // 3))
    is_tier_draining = (remaining / tier_size) <= 0.40 or remaining <= 4

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
            warning = _evaluate_waiting_cliff(top_tier, next_tier, picks_until_turn, snake_turn_gap)

        if warning:
            warnings.append(warning)

    warnings.sort(key=lambda w: (risk_rank.get(w.cliff_risk, 4), -w.next_tier_drop_points))
    return warnings
