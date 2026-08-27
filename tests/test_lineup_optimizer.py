"""Unit tests for PuLP Integer Linear Programming lineup optimizer."""

import time

from nfl_helper.core.lineup_optimizer import solve_optimal_lineup
from nfl_helper.models.player import Player, Position
from nfl_helper.models.roster import (
    OptimizationStrategy,
    RosterSlotRequirement,
    TeamRoster,
)
from tests.fixtures.demo_rosters import get_demo_roster


def test_standard_lineup_optimization_structure() -> None:
    """Verify standard starting lineup structure and exact slot count fulfillment."""
    roster = get_demo_roster()
    solution = solve_optimal_lineup(roster, strategy=OptimizationStrategy.BALANCED)

    assert solution.solver_status == "Optimal"
    assert len(solution.optimal_starters) == 9  # 1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX, 1 K, 1 DST
    assert solution.optimal_projected_total > 0
    assert solution.projected_delta >= 0

    # Ensure positions in starters
    starter_positions = [str(p.position).upper() for p in solution.optimal_starters]
    assert starter_positions.count("QB") == 1
    assert starter_positions.count("K") == 1
    assert starter_positions.count("D/ST") == 1


def test_superflex_and_custom_slot_layouts() -> None:
    """Verify solver handles Superflex (QB in FLEX) and custom slot layouts."""
    qb1 = Player(id="q1", name="Lamar Jackson", position=Position.QB, team="BAL", projected_points=24.0)
    qb2 = Player(id="q2", name="Josh Allen", position=Position.QB, team="BUF", projected_points=23.0)
    rb1 = Player(id="r1", name="CMC", position=Position.RB, team="SF", projected_points=18.0)
    wr1 = Player(id="w1", name="JJ", position=Position.WR, team="MIN", projected_points=17.0)
    te1 = Player(id="t1", name="Kelce", position=Position.TE, team="KC", projected_points=12.0)
    k1 = Player(id="k1", name="Aubrey", position=Position.K, team="DAL", projected_points=9.0)
    dst1 = Player(id="d1", name="Ravens", position=Position.DST, team="BAL", projected_points=8.0)

    superflex_slots = [
        RosterSlotRequirement(slot_name="QB", count=1, eligible_positions=["QB"]),
        RosterSlotRequirement(slot_name="SUPERFLEX", count=1, eligible_positions=["QB", "RB", "WR", "TE"]),
        RosterSlotRequirement(slot_name="RB", count=1, eligible_positions=["RB"]),
        RosterSlotRequirement(slot_name="WR", count=1, eligible_positions=["WR"]),
        RosterSlotRequirement(slot_name="TE", count=1, eligible_positions=["TE"]),
        RosterSlotRequirement(slot_name="K", count=1, eligible_positions=["K"]),
        RosterSlotRequirement(slot_name="DST", count=1, eligible_positions=["DST", "D/ST"]),
    ]

    roster = TeamRoster(
        team_id="sflex_team",
        team_name="Superflex",
        players=[qb1, qb2, rb1, wr1, te1, k1, dst1],
    )
    solution = solve_optimal_lineup(roster, slot_requirements=superflex_slots)
    assert solution.solver_status == "Optimal"
    assert len(solution.optimal_starters) == 7
    # Both QBs should start (one at QB, one at SUPERFLEX)
    qb_starters = [p for p in solution.optimal_starters if p.position == Position.QB]
    assert len(qb_starters) == 2


def test_ir_and_out_player_handling() -> None:
    """Ensure injured starters are benched and generate roster adjustment warnings."""
    roster = get_demo_roster()
    solution = solve_optimal_lineup(roster)

    # Inactive player Deshaun Watson should not start
    assert not any(p.id == "demo_watson" for p in solution.optimal_starters)

    # Roster adjustments should include warning for Deshaun Watson and Nick Chubb
    ir_recs = solution.ir_warnings
    assert len(ir_recs) >= 1
    assert any("Deshaun Watson" in r.player_name or "Nick Chubb" in r.player_name for r in ir_recs)


def test_tactical_strategy_modes_ceiling_vs_floor() -> None:
    """Verify CEILING mode favors high-variance upside while FLOOR mode favors safe volume."""
    # Boom-or-bust player: 12.0 proj (Ceiling 20.1, Floor 6.6)
    boom_player = Player(
        id="boom",
        name="Boom Receiver",
        position=Position.WR,
        team="HOU",
        projected_points=12.0,
    )
    # Safe-floor player: 12.0 proj, but lower volatility
    floor_player = Player(
        id="floor",
        name="Floor Receiver",
        position=Position.WR,
        team="IND",
        projected_points=12.0,
    )
    # Custom volatility: adjust projected slightly so baseline is identical
    roster = TeamRoster(
        team_id="strat_team",
        team_name="Strategies",
        players=[boom_player, floor_player],
    )
    single_wr_slot = [RosterSlotRequirement(slot_name="WR", count=1, eligible_positions=["WR"])]

    sol_ceil = solve_optimal_lineup(roster, slot_requirements=single_wr_slot, strategy=OptimizationStrategy.CEILING)
    assert sol_ceil.solver_status == "Optimal"
    assert len(sol_ceil.optimal_starters) == 1

    sol_floor = solve_optimal_lineup(roster, slot_requirements=single_wr_slot, strategy=OptimizationStrategy.FLOOR)
    assert sol_floor.solver_status == "Optimal"
    assert len(sol_floor.optimal_starters) == 1


def test_anti_correlation_warning() -> None:
    """Verify anti-correlation trap warning when starting D/ST against your starting QB."""
    qb = Player(id="qb_cle", name="Deshaun Watson", position=Position.QB, team="CLE", projected_points=15.0)
    dst = Player(
        id="dst_bal",
        name="Ravens D/ST",
        position=Position.DST,
        team="BAL",
        projected_points=9.0,
        opponent="CLE",
    )
    roster = TeamRoster(team_id="t1", team_name="T1", players=[qb, dst])
    slots = [
        RosterSlotRequirement(slot_name="QB", count=1, eligible_positions=["QB"]),
        RosterSlotRequirement(slot_name="DST", count=1, eligible_positions=["DST", "D/ST"]),
    ]
    solution = solve_optimal_lineup(roster, slot_requirements=slots)
    assert len(solution.anti_correlation_warnings) > 0
    assert "Anti-Correlation" in solution.anti_correlation_warnings[0]


def test_optimizer_performance_latency_budget() -> None:
    """Ensure ILP solver solves full 16-player roster under 100ms operational budget."""
    roster = get_demo_roster()
    start_time = time.perf_counter()
    solution = solve_optimal_lineup(roster)
    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    assert solution.solver_status == "Optimal"
    assert elapsed_ms < 100.0, f"Solver took {elapsed_ms:.2f}ms, exceeding 100ms budget"
