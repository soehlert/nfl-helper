"""Game environment, Vegas lines, stadium dome status, and weather adjustments."""

from nfl_helper.models.player import Player, Position


def calculate_game_context_adjustments(player: Player) -> tuple[float, list[str]]:
    """Compute projection adjustments and badges based on Vegas odds and stadium weather."""
    if not player.game_context:
        return 0.0, []

    ctx = player.game_context
    delta = 0.0
    reasons: list[str] = []

    # 1. Vegas Implied Game Total (Over/Under)
    if ctx.over_under is not None and player.projected_points > 0:
        if ctx.over_under >= 49.5:
            shootout_bonus = round(0.10 * player.projected_points, 1)
            delta += shootout_bonus
            reasons.append(f"Vegas Shootout: O/U {ctx.over_under} ({shootout_bonus:+.1f} pts)")
        elif ctx.over_under <= 38.5 and player.projected_points > 5.0:
            slugfest_dampener = round(-0.10 * player.projected_points, 1)
            delta += slugfest_dampener
            reasons.append(f"Defensive Slugfest: O/U {ctx.over_under} ({slugfest_dampener:+.1f} pts)")

    # 2. Point Spread & Game Script
    if ctx.spread is not None and player.projected_points > 0:
        # Heavy Favorite (<= -6.5): Leading 4th quarter script
        if ctx.spread <= -6.5:
            if player.position == Position.RB:
                delta += 1.5
                reasons.append("Rush Volume (+1.5 pts)")
            elif player.position == Position.QB:
                delta -= 0.5
                reasons.append("Pass Volume (-0.5 pt)")

        # Heavy Underdog (>= +6.5): Trailing pass-heavy script
        elif ctx.spread >= 6.5:
            if player.position in [Position.QB, Position.WR, Position.TE]:
                delta += 1.0
                reasons.append("Pass Volume (+1.0 pt)")
            elif player.position == Position.RB:
                is_pass_catching = bool(player.usage and (player.usage.target_share_pct or 0) >= 0.12)
                if is_pass_catching:
                    delta += 1.2
                    reasons.append("Pass Volume (+1.2 pts)")
                else:
                    delta -= 1.5
                    reasons.append("Rush Volume (-1.5 pts)")

    # 3. Dome / Indoor Stadium Bonus
    is_indoor = ctx.is_dome or ctx.stadium_type.upper() in ["DOME", "RETRACTABLE_CLOSED"]
    if is_indoor:
        if player.position == Position.K:
            delta += 0.8
            reasons.append("Indoor Dome Kicker (+0.8 pt)")
        elif player.position in [Position.QB, Position.WR, Position.TE]:
            delta += 0.5
            reasons.append("Indoor Dome Environment (+0.5 pt)")
    else:
        # 4. Outdoor Weather & High Wind Dampener
        if ctx.wind_mph >= 18.0 and player.projected_points > 0:
            wind_penalty = round(-0.15 * player.projected_points, 1)
            delta += wind_penalty
            reasons.append(f"High Wind Storm: {int(ctx.wind_mph)} mph ({wind_penalty:+.1f} pts)")
        elif ctx.weather_condition.upper() in ["BLIZZARD", "HEAVY_SNOW"]:
            if player.position in [Position.QB, Position.WR, Position.K] and player.projected_points > 0:
                snow_penalty = round(-0.15 * player.projected_points, 1)
                delta += snow_penalty
                reasons.append(f"Blizzard / Heavy Snow ({snow_penalty:+.1f} pts)")
            elif player.position == Position.RB:
                delta += 1.0
                reasons.append("Snow Game Heavy Rush Script (+1.0 pt)")

    return round(delta, 2), reasons
