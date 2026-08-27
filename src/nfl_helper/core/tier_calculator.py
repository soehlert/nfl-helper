"""Positional tier clustering and 3-scenario cliff detection."""

import math
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
    """Group players by explicit cheatsheet tiers."""
    grouped: dict[int, list[Player]] = defaultdict(list)
    for p in players:
        tier_num = p.cheatsheet_tier if p.cheatsheet_tier is not None else 99
        grouped[tier_num].append(p)

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

    has_cheatsheet_tiers = any(p.cheatsheet_tier is not None for p in pos_players)
    if has_cheatsheet_tiers or (cheatsheet_context and cheatsheet_context.entries):
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
    remaining = len(tier.players)
    threshold = max(1, math.ceil(snake_turn_gap / 2))
    if remaining > threshold and not (snake_turn_gap >= 4 and remaining <= 3):
        return None

    drop = calculate_tier_drop(tier, next_tier)
    risk = "CRITICAL" if (remaining == 1 or drop >= 3.0) else "HIGH"
    next_num = next_tier.tier_num if next_tier else tier.tier_num + 1
    action = (
        f"Only {remaining} Tier {tier.tier_num} {tier.position} remaining before a {snake_turn_gap}-pick "
        f"turn gap. Draft now to avoid dropping {drop} pts to Tier {next_num}."
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
    """Evaluate cliff risk when user is waiting for their pick."""
    remaining = len(tier.players)
    drop = calculate_tier_drop(tier, next_tier)
    next_num = next_tier.tier_num if next_tier else tier.tier_num + 1

    if remaining <= picks_until_turn:
        risk = "CRITICAL" if remaining == 1 else "HIGH"
        action = (
            f"Only {remaining} Tier {tier.tier_num} {tier.position} remaining with {picks_until_turn} picks "
            f"until your turn. Tier is expected to deplete before your pick. Prepare to target Tier {next_num} or pivot."
        )
        return TierCliffWarning(
            position=tier.position,
            current_tier=tier.tier_num,
            players_remaining=remaining,
            picks_until_turn=picks_until_turn,
            snake_turn_gap=snake_turn_gap,
            cliff_risk=risk,
            cliff_type=CliffType.DEPLETED_BEFORE_TURN,
            next_tier_drop_points=drop,
            recommended_action=action,
        )

    turn_reach = picks_until_turn + max(1, math.ceil(snake_turn_gap / 2))
    if remaining <= turn_reach or (snake_turn_gap >= 4 and remaining <= picks_until_turn + 3):
        risk = "HIGH" if remaining <= picks_until_turn + 1 else "MODERATE"
        action = (
            f"{remaining} Tier {tier.tier_num} {tier.position} remaining. Tier will survive to your pick in "
            f"{picks_until_turn} picks, but will deplete during your {snake_turn_gap}-pick turn gap. "
            f"Target {tier.position} at your upcoming turn."
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
