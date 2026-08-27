"""PuLP Integer Linear Programming starting lineup optimizer with contextual adjustments."""

import pulp

from nfl_helper.core.game_context import calculate_game_context_adjustments
from nfl_helper.core.usage_analyzer import calculate_usage_adjustments
from nfl_helper.models.player import InjuryStatus, Player, Position
from nfl_helper.models.roster import (
    LineupSolution,
    OptimizationStrategy,
    RosterAdjustment,
    RosterSlotRequirement,
    TeamRoster,
)


def get_default_slot_requirements() -> list[RosterSlotRequirement]:
    """Return standard 1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX, 1 K, 1 D/ST requirements."""
    return [
        RosterSlotRequirement(slot_name="QB", count=1, eligible_positions=["QB"]),
        RosterSlotRequirement(slot_name="RB", count=2, eligible_positions=["RB"]),
        RosterSlotRequirement(slot_name="WR", count=2, eligible_positions=["WR"]),
        RosterSlotRequirement(slot_name="TE", count=1, eligible_positions=["TE"]),
        RosterSlotRequirement(slot_name="FLEX", count=1, eligible_positions=["RB", "WR", "TE"]),
        RosterSlotRequirement(slot_name="K", count=1, eligible_positions=["K"]),
        RosterSlotRequirement(slot_name="DST", count=1, eligible_positions=["DST", "D/ST"]),
    ]


def prepare_player_projections(
    player: Player, strategy: OptimizationStrategy = OptimizationStrategy.BALANCED
) -> tuple[float, float, float, float]:
    """Calculate composite adjustments, floor, ceiling, and solver target weight."""
    u_delta, u_reasons = calculate_usage_adjustments(player)
    g_delta, g_reasons = calculate_game_context_adjustments(player)

    base = float(player.projected_points or 0.0)
    adjusted = max(0.0, round(base + u_delta + g_delta, 2))

    # Calculate positional volatility and standard deviation
    pos = str(player.position).upper()
    variance_mult = 0.45 if pos == Position.WR else (0.35 if pos in [Position.RB, Position.TE] else 0.25)
    std_dev = adjusted * variance_mult

    floor_pts = player.floor_points if player.floor_points > 0.0 else max(0.0, round(adjusted - 1.0 * std_dev, 2))
    ceiling_pts = player.ceiling_points if player.ceiling_points > 0.0 else round(adjusted + 1.5 * std_dev, 2)

    # Attach computed values to player object
    player.adjusted_projected_points = adjusted
    player.floor_points = floor_pts
    player.ceiling_points = ceiling_pts
    player.projection_adjustment_reasons = u_reasons + g_reasons

    # Check for inactive / injured status
    is_inactive = str(player.injury_status).upper() in [
        InjuryStatus.OUT,
        InjuryStatus.IR,
        InjuryStatus.SUSPENDED,
    ]
    if is_inactive:
        return adjusted, floor_pts, ceiling_pts, -100.0

    if strategy == OptimizationStrategy.CEILING:
        weight = ceiling_pts
    elif strategy == OptimizationStrategy.FLOOR:
        weight = floor_pts
    else:
        weight = adjusted

    return adjusted, floor_pts, ceiling_pts, weight


def _is_player_eligible_for_slot(player: Player, slot: RosterSlotRequirement) -> bool:
    """Check if player position or eligible_slots match the slot requirement."""
    pos_str = str(player.position).upper()
    if pos_str in [p.upper() for p in slot.eligible_positions]:
        return True
    return any(s.upper() == slot.slot_name.upper() for s in player.eligible_slots)


def check_anti_correlation(starters: list[Player]) -> list[str]:
    """Flag negative correlation traps such as starting D/ST against your starting QB."""
    warnings: list[str] = []
    dst = next((p for p in starters if str(p.position).upper() in ["DST", "D/ST"]), None)
    qb = next((p for p in starters if str(p.position).upper() == "QB"), None)

    if dst and qb:
        dst_opp = (dst.opponent or (dst.game_context.opponent if dst.game_context else "")).upper()
        qb_team = str(qb.team).upper()
        if qb_team and (qb_team in dst_opp or dst_opp in qb_team):
            warnings.append(
                f"Anti-Correlation Warning: {dst.name} ({dst.team}) plays against starting QB {qb.name} ({qb.team})."
            )
    return warnings


def detect_roster_adjustments(roster: TeamRoster, optimal_starters: list[Player]) -> list[RosterAdjustment]:
    """Identify injured starters needing benching, open IR slots, or healthy IR stashes."""
    adjustments: list[RosterAdjustment] = []
    starter_ids = {p.id for p in roster.starters}

    for p in roster.players:
        stat = str(p.injury_status).upper()
        # Starter who is OUT or on IR
        if p.id in starter_ids and stat in [InjuryStatus.OUT, InjuryStatus.IR, InjuryStatus.SUSPENDED]:
            adjustments.append(
                RosterAdjustment(
                    player_name=p.name,
                    player_id=p.id,
                    position=str(p.position),
                    current_slot="STARTER",
                    suggested_slot="BENCH/IR",
                    reason=f"{p.name} is ruled {stat}. Move to Bench or open IR slot.",
                    injury_status=stat,
                )
            )

    # Check for active player trapped in IR
    adjustments.extend(
        RosterAdjustment(
            player_name=p.name,
            player_id=p.id,
            position=str(p.position),
            current_slot="IR",
            suggested_slot="BENCH",
            reason=f"{p.name} is ACTIVE/healthy. Activate from IR to unlock roster transactions.",
            injury_status="ACTIVE",
        )
        for p in roster.ir
        if str(p.injury_status).upper() == InjuryStatus.ACTIVE
    )
    return adjustments


def solve_optimal_lineup(
    roster: TeamRoster,
    slot_requirements: list[RosterSlotRequirement] | None = None,
    strategy: OptimizationStrategy = OptimizationStrategy.BALANCED,
) -> LineupSolution:
    """Solve the optimal starting lineup via binary ILP under dynamic league slot constraints."""
    slots = slot_requirements or get_default_slot_requirements()
    all_players = roster.players or (roster.starters + roster.bench + roster.ir)

    if not all_players:
        return LineupSolution(team_id=roster.team_id, strategy=strategy, solver_status="No Players")

    weights: dict[str, float] = {}
    for p in all_players:
        _, _, _, w = prepare_player_projections(p, strategy=strategy)
        weights[p.id] = w

    # Binary Integer Linear Programming Formulation
    prob = pulp.LpProblem("Fantasy_Lineup_Optimization", pulp.LpMaximize)

    # Decision variables: x[(p.id, s.slot_name, copy_idx)]
    x_vars: dict[tuple[str, str, int], pulp.LpVariable] = {}
    for p in all_players:
        for s in slots:
            if _is_player_eligible_for_slot(p, s):
                for idx in range(s.count):
                    x_vars[(p.id, s.slot_name, idx)] = pulp.LpVariable(
                        f"x_{p.id}_{s.slot_name}_{idx}", cat=pulp.LpBinary
                    )

    # Objective: Maximize sum of player weights assigned to starting slots
    prob += pulp.lpSum(x_vars[(p_id, s_name, idx)] * weights[p_id] for (p_id, s_name, idx) in x_vars)

    # Constraint 1: Each player can occupy at most ONE starting slot
    for p in all_players:
        p_slots = [x_vars[(p_id, s_name, idx)] for (p_id, s_name, idx) in x_vars if p_id == p.id]
        if p_slots:
            prob += pulp.lpSum(p_slots) <= 1

    # Constraint 2: Each required starting slot copy is assigned to at most ONE player
    for s in slots:
        for idx in range(s.count):
            slot_players = [
                x_vars[(p_id, s_name, i)] for (p_id, s_name, i) in x_vars if s_name == s.slot_name and i == idx
            ]
            if slot_players:
                prob += pulp.lpSum(slot_players) <= 1

    # Solve using CBC solver with disabled console output
    prob.solve(pulp.PULP_CBC_CMD(msg=False))

    optimal_starters: list[Player] = []
    chosen_ids: set[str] = set()

    for (p_id, s_name, idx), var in x_vars.items():
        if pulp.value(var) is not None and pulp.value(var) > 0.5 and p_id not in chosen_ids:
            p_obj = next(p for p in all_players if p.id == p_id)
            slot_def = next((s for s in slots if s.slot_name == s_name), None)
            if slot_def and slot_def.count > 1 and s_name in ["RB", "WR", "TE"]:
                p_obj.assigned_slot = f"{s_name}{idx + 1}"
            else:
                p_obj.assigned_slot = s_name
            optimal_starters.append(p_obj)
            chosen_ids.add(p_id)

    canonical_order = [
        "QB",
        "QB1",
        "QB2",
        "RB",
        "RB1",
        "RB2",
        "RB3",
        "WR",
        "WR1",
        "WR2",
        "WR3",
        "TE",
        "TE1",
        "TE2",
        "FLEX",
        "FLEX1",
        "FLEX2",
        "SUPERFLEX",
        "K",
        "K1",
        "DST",
        "D/ST",
    ]

    def _slot_rank(p: Player) -> int:
        s = (p.assigned_slot or "").upper()
        return canonical_order.index(s) if s in canonical_order else 99

    optimal_starters.sort(key=_slot_rank)
    optimal_bench = [p for p in all_players if p.id not in chosen_ids]

    # Calculate projected totals and deltas
    current_starters = roster.starters if roster.starters else optimal_starters
    current_total = round(sum(p.adjusted_projected_points for p in current_starters), 2)
    optimal_total = round(sum(p.adjusted_projected_points for p in optimal_starters), 2)
    delta = round(max(0.0, optimal_total - current_total), 2)

    # Start and Sit recommendations
    curr_ids = {p.id for p in current_starters}
    start_recs = [
        f"Start {p.name} ({p.position}, {p.team} - {p.adjusted_projected_points} pts)"
        for p in optimal_starters
        if p.id not in curr_ids
    ]
    sit_recs = [
        f"Sit {p.name} ({p.position}, {p.team} - {p.adjusted_projected_points} pts)"
        for p in current_starters
        if p.id not in chosen_ids
    ]

    ir_warnings = detect_roster_adjustments(roster, optimal_starters)
    anti_corr = check_anti_correlation(optimal_starters)

    return LineupSolution(
        team_id=roster.team_id,
        strategy=strategy,
        optimal_starters=optimal_starters,
        optimal_bench=optimal_bench,
        current_projected_total=current_total,
        optimal_projected_total=optimal_total,
        projected_delta=delta,
        start_recommendations=start_recs,
        sit_recommendations=sit_recs,
        ir_warnings=ir_warnings,
        anti_correlation_warnings=anti_corr,
        solver_status="Optimal" if prob.status == 1 else "Suboptimal",
    )
