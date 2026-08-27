"""Usage, snap share velocity, and touch quality analysis."""

from nfl_helper.models.player import Player, Position


def calculate_usage_adjustments(player: Player) -> tuple[float, list[str]]:
    """Compute projection adjustments and explanation badges based on usage trends."""
    delta = 0.0
    reasons: list[str] = []

    # 1. Practice Status & Decoy / Snap-Limit Detection
    if player.practice_status and player.projected_points > 0:
        recent_practice = [p.upper().strip() for p in player.practice_status]
        # Pattern of DNPs leading to only Limited Practice (LP) before game day
        if (
            recent_practice[-1] == "LP"
            and any(p == "DNP" for p in recent_practice[:-1])
            and player.position in [Position.WR, Position.TE, Position.RB]
        ):
            decoy_penalty = round(-0.25 * player.projected_points, 1)
            delta += decoy_penalty
            reasons.append(f"Decoy / Snap-Limit Risk: Limited Practice ({decoy_penalty:+.1f} pts)")

    if not player.usage:
        return round(delta, 2), reasons

    usage = player.usage

    # 2. Snap Share Velocity (Rolling trend changes)
    if len(usage.snap_percentages) >= 2:
        prev_avg = sum(usage.snap_percentages[:-1]) / len(usage.snap_percentages[:-1])
        recent_snap = usage.snap_percentages[-1]

        # Significant drop in snap percentage (>=25% absolute drop or <=60% of previous workload)
        if (prev_avg - recent_snap >= 0.25) or (prev_avg > 0 and recent_snap <= prev_avg * 0.60):
            penalty = round(-0.30 * player.projected_points, 1)
            delta += penalty
            reasons.append(
                f"Role Demotion: Snaps dropped {int(prev_avg * 100)}% -> {int(recent_snap * 100)}% ({penalty:+.1f} pts)"
            )
        # Significant surge in snap percentage
        elif recent_snap >= 0.55 and recent_snap >= prev_avg * 1.35:
            bonus = min(2.0, round(0.15 * player.projected_points, 1))
            delta += bonus
            reasons.append(f"Role Surge: Snaps increased to {int(recent_snap * 100)}% ({bonus:+.1f} pts)")

    # 3. Route Participation % for WRs and TEs
    if (
        usage.route_participation_pct is not None
        and player.position in [Position.WR, Position.TE]
        and usage.route_participation_pct < 0.55
        and player.projected_points > 5.0
    ):
        delta -= 2.0
        reasons.append("Low Route Participation <55% (-2.0 pts)")

    # 4. High-Value Touches (Goal-Line inside the 5-yard line)
    if usage.goalline_share_pct is not None and usage.goalline_share_pct >= 0.60:
        delta += 1.8
        reasons.append(f"High-Value Touches: {int(usage.goalline_share_pct * 100)}% Goal-Line Share (+1.8 pts)")
    elif usage.goalline_touches_inside_5 >= 2:
        delta += 1.5
        reasons.append("High-Value Touches: Active Goal-Line Role (+1.5 pts)")

    return round(delta, 2), reasons
