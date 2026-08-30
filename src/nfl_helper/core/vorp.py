"""Value Over Replacement Player (VORP) calculations and baselines."""

import math

from nfl_helper.models.player import Player

# Positional starter depth multipliers applied to league size to find replacement baseline
STARTER_DEPTH_MULTIPLIERS: dict[str, float] = {
    "QB": 1.5,
    "RB": 2.25,
    "WR": 2.75,
    "TE": 1.25,
    "K": 1.0,
    "D/ST": 1.0,
}

# Demand weights to scale raw VORP points based on weekly lineup requirements
POSITION_DEMAND_WEIGHTS: dict[str, float] = {
    "RB": 1.0,
    "WR": 1.0,
    "TE": 0.70,
    "QB": 0.65,
    "K": 0.05,
    "D/ST": 0.05,
}


def calculate_vorp_baselines(all_players: list[Player], total_teams: int) -> dict[str, float]:
    """Determine replacement baseline projected points per position based on starter depth."""
    baselines: dict[str, float] = {}
    by_pos: dict[str, list[Player]] = {}
    for p in all_players:
        by_pos.setdefault(str(p.position), []).append(p)

    for pos, multiplier in STARTER_DEPTH_MULTIPLIERS.items():
        players = sorted(by_pos.get(pos, []), key=lambda x: x.projected_points, reverse=True)
        if not players:
            baselines[pos] = 0.0
            continue
        baseline_idx = min(len(players) - 1, max(0, math.ceil(total_teams * multiplier) - 1))
        baselines[pos] = round(players[baseline_idx].projected_points, 2)
    return baselines


def calculate_vorp(available_players: list[Player], baselines: dict[str, float]) -> dict[str, float]:
    """Calculate VORP score for each available player against baselines weighted by roster demand."""
    vorp_map: dict[str, float] = {}
    for p in available_players:
        base = baselines.get(str(p.position), 0.0)
        demand_weight = POSITION_DEMAND_WEIGHTS.get(str(p.position), 1.0)
        raw_vorp = max(0.0, p.projected_points - base)
        vorp_map[p.id] = round(raw_vorp * demand_weight, 2)
    return vorp_map
