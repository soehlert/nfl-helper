"""Unit tests for live draft engine, lookahead math, tier clustering, and cliff alerts."""

import time

from nfl_helper.core.cheatsheet import parse_plain_text_cheatsheet
from nfl_helper.core.draft_engine import (
    _calculate_required_positions,
    _evaluate_strategy_rule_adjustments,
    build_draft_state,
    calculate_lookahead,
    calculate_snake_pick_owner,
    calculate_user_draft_schedule,
    calculate_vorp,
    calculate_vorp_baselines,
    generate_draft_suggestions,
)
from nfl_helper.core.tier_calculator import calculate_tier_drop, cluster_position_tiers, detect_tier_cliffs
from nfl_helper.models.draft import CliffType, DraftPick
from nfl_helper.models.player import Player, Position


def test_snake_pick_owner_math() -> None:
    """Verify 1-indexed pick owner calculations across odd and even rounds."""
    # 12-team league
    # Round 1: slots 1 -> 12
    assert calculate_snake_pick_owner(1, 12) == 1
    assert calculate_snake_pick_owner(6, 12) == 6
    assert calculate_snake_pick_owner(12, 12) == 12
    # Round 2: slots 12 -> 1
    assert calculate_snake_pick_owner(13, 12) == 12
    assert calculate_snake_pick_owner(14, 12) == 11
    assert calculate_snake_pick_owner(24, 12) == 1
    # Round 3: slots 1 -> 12
    assert calculate_snake_pick_owner(25, 12) == 1
    assert calculate_snake_pick_owner(36, 12) == 12


def test_user_draft_schedule() -> None:
    """Verify schedule generation for 1st, 12th, and middle draft slots."""
    # Slot 1 in 12-team, 4 rounds
    s1 = calculate_user_draft_schedule(1, 12, 4)
    assert s1 == [1, 24, 25, 48]

    # Slot 12 in 12-team, 4 rounds
    s12 = calculate_user_draft_schedule(12, 12, 4)
    assert s12 == [12, 13, 36, 37]

    # Slot 6 in 12-team, 4 rounds
    s6 = calculate_user_draft_schedule(6, 12, 4)
    assert s6 == [6, 19, 30, 43]


def test_snake_lookahead_on_the_clock() -> None:
    """Verify lookahead math when user is currently on the clock."""
    # Slot 1 on the clock at pick 1
    wait, gap, on_clock = calculate_lookahead(1, 1, 12, 16)
    assert on_clock is True
    assert wait == 0
    assert gap == 22  # Picks 2 to 23 are 22 picks

    # Slot 12 on the clock at pick 12 (back-to-back turnaround)
    wait12, gap12, on_clock12 = calculate_lookahead(12, 12, 12, 16)
    assert on_clock12 is True
    assert wait12 == 0
    assert gap12 == 0  # Next pick is immediately pick 13 (0 opponents)


def test_snake_lookahead_waiting_and_turn_gaps() -> None:
    """Verify lookahead math when user is waiting for upcoming picks."""
    # Slot 6 when current pick is 1
    wait, gap, on_clock = calculate_lookahead(1, 6, 12, 16)
    assert on_clock is False
    assert wait == 5  # Pick 6 - Pick 1 = 5
    assert gap == 12  # Pick 19 - Pick 6 - 1 = 12

    # Slot 6 when current pick is 7 (after first pick, before pick 19)
    wait2, gap2, on_clock2 = calculate_lookahead(7, 6, 12, 16)
    assert on_clock2 is False
    assert wait2 == 12  # Pick 19 - Pick 7 = 12
    assert gap2 == 10  # Pick 30 - Pick 19 - 1 = 10


def test_statistical_tier_clustering() -> None:
    """Verify statistical tier clustering based on point drop-off thresholds."""
    players = [
        Player(id="p1", name="Elite 1", position=Position.RB, team="A", projected_points=21.0),
        Player(id="p2", name="Elite 2", position=Position.RB, team="B", projected_points=20.5),
        # 2.5 pt drop -> new tier
        Player(id="p3", name="Tier2 A", position=Position.RB, team="C", projected_points=18.0),
        Player(id="p4", name="Tier2 B", position=Position.RB, team="D", projected_points=17.5),
        # 3.0 pt drop -> new tier
        Player(id="p5", name="Tier3 A", position=Position.RB, team="E", projected_points=14.5),
    ]

    tiers = cluster_position_tiers(players, "RB")
    assert len(tiers) == 3
    assert tiers[0].tier_num == 1
    assert len(tiers[0].players) == 2
    assert tiers[0].avg_projected == 20.75

    assert tiers[1].tier_num == 2
    assert len(tiers[1].players) == 2
    assert tiers[1].avg_projected == 17.75

    assert tiers[2].tier_num == 3
    assert len(tiers[2].players) == 1

    drop = calculate_tier_drop(tiers[0], tiers[1])
    assert drop == 3.0


def test_cheatsheet_tier_clustering() -> None:
    """Verify tier clustering respects custom cheatsheet tiers when attached."""
    players = [
        Player(id="p1", name="Player 1", position=Position.WR, team="A", projected_points=18.0, cheatsheet_tier=1),
        Player(id="p2", name="Player 2", position=Position.WR, team="B", projected_points=17.0, cheatsheet_tier=1),
        Player(id="p3", name="Player 3", position=Position.WR, team="C", projected_points=16.0, cheatsheet_tier=2),
    ]

    tiers = cluster_position_tiers(players, "WR")
    assert len(tiers) == 2
    assert tiers[0].tier_num == 1
    assert len(tiers[0].players) == 2
    assert tiers[1].tier_num == 2
    assert len(tiers[1].players) == 1


def test_tier_cliffs_actionable_scenarios() -> None:
    """Verify detection of ON_THE_CLOCK_CLIFF and UPCOMING_TURN_CLIFF across skill and special teams."""
    # Scenario 1: ON_THE_CLOCK_CLIFF (User on the clock, only 1 Tier-1 RB left with 12-pick turn gap)
    t1_rb = [Player(id="rb1", name="CMC", position=Position.RB, team="SF", projected_points=20.0, tier=1)]
    t2_rb = [Player(id="rb2", name="JT", position=Position.RB, team="IND", projected_points=16.0, tier=2)]
    tiers_on_clock = {
        "RB": cluster_position_tiers(t1_rb + t2_rb, "RB"),
    }
    cliffs_on_clock = detect_tier_cliffs(tiers_on_clock, picks_until_turn=0, snake_turn_gap=12, is_on_the_clock=True)
    assert len(cliffs_on_clock) == 1
    assert cliffs_on_clock[0].cliff_type == CliffType.ON_THE_CLOCK_CLIFF
    assert cliffs_on_clock[0].cliff_risk == "CRITICAL"
    assert cliffs_on_clock[0].next_tier_drop_points == 4.0

    # Scenario 2: UPCOMING_TURN_CLIFF (User 2 picks away, 3 Tier-1 players left, turn gap is 12)
    t1_wr = [
        Player(id="wr1", name="Ceedee", position=Position.WR, team="DAL", projected_points=19.0, tier=1),
        Player(id="wr2", name="Tyreek", position=Position.WR, team="MIA", projected_points=18.5, tier=1),
        Player(id="wr3", name="JJ", position=Position.WR, team="MIN", projected_points=18.0, tier=1),
    ]
    t2_wr = [Player(id="wr4", name="Evans", position=Position.WR, team="TB", projected_points=15.0, tier=2)]
    tiers_upcoming = {"WR": cluster_position_tiers(t1_wr + t2_wr, "WR")}
    cliffs_upcoming = detect_tier_cliffs(
        tiers_upcoming, picks_until_turn=2, snake_turn_gap=12, is_on_the_clock=False, current_pick=4
    )
    assert len(cliffs_upcoming) == 1
    assert cliffs_upcoming[0].cliff_type == CliffType.UPCOMING_TURN_CLIFF
    assert "Target Tier 1" in cliffs_upcoming[0].recommended_action


def test_vorp_and_suggestion_generation() -> None:
    """Verify baseline replacement calculations and draft suggestions ranking."""
    players = [
        Player(id="qb1", name="Josh Allen", position=Position.QB, team="BUF", projected_points=24.0),
        Player(id="qb2", name="Lamar Jackson", position=Position.QB, team="BAL", projected_points=22.0),
        Player(id="qb12", name="Goff", position=Position.QB, team="DET", projected_points=17.0),
        Player(id="rb1", name="Bijan", position=Position.RB, team="ATL", projected_points=19.0),
        Player(id="rb30", name="Singletary", position=Position.RB, team="NYG", projected_points=10.0),
    ]

    baselines = calculate_vorp_baselines(players, total_teams=12)
    vorp_scores = calculate_vorp(players, baselines)

    # Bijan (19.0 - 10.0 = 9.0 VORP) vs Josh Allen (24.0 - 17.0 = 7.0 VORP * 0.65 QB demand weight = 4.55)
    assert vorp_scores["rb1"] == 9.0
    assert vorp_scores["qb1"] == 4.55

    tiers = {"RB": cluster_position_tiers([players[3]], "RB"), "QB": cluster_position_tiers([players[0]], "QB")}
    suggestions = generate_draft_suggestions(players, tiers, [], baselines, overall_pick=1, top_n=3)

    assert len(suggestions) == 3
    assert suggestions[0].player.id == "rb1"
    assert suggestions[0].vorp == 9.0


def test_build_draft_state_performance_and_completeness() -> None:
    """Verify build_draft_state runs completely in < 50ms."""
    # Generate 150 players
    mock_players: list[Player] = []
    positions = ["QB", "RB", "WR", "TE", "K", "D/ST"]
    for i in range(150):
        pos = positions[i % len(positions)]
        pts = round(25.0 - (i * 0.12), 1)
        mock_players.append(Player(id=f"p_{i}", name=f"Player {i}", position=pos, team="NFL", projected_points=pts))

    picks = [
        DraftPick(
            round_num=1,
            round_pick=1,
            overall_pick=1,
            team_id="t1",
            team_name="Team 1",
            player_id="p_0",
            player_name="Player 0",
            position="QB",
        )
    ]

    start_time = time.perf_counter()
    state = build_draft_state(
        league_id="test_league",
        draft_id="draft_123",
        overall_pick=2,
        user_draft_slot=6,
        total_teams=12,
        total_rounds=16,
        recent_picks=picks,
        all_players=mock_players,
    )
    elapsed_ms = (time.perf_counter() - start_time) * 1000

    assert state.current_pick == 2
    assert state.current_round == 1
    assert state.user_draft_slot == 6
    assert state.picks_until_user_turn == 4
    assert elapsed_ms < 50.0  # Well within the 100ms algorithmic budget


def test_cheatsheet_note_calibrated_board_movements() -> None:
    """Verify cheatsheet notes produce calibrated big-board movements across rounds."""
    pool: list[Player] = []
    for i in range(1, 151):
        pos = Position.WR if i % 2 == 0 else Position.RB
        pts = 300.0 - (i**0.65) * 8.0
        p = Player(id=f"p_{i}", name=f"Player {i}", position=pos, team="TM", projected_points=pts)
        pool.append(p)

    base_state = build_draft_state("test", None, 1, 1, 12, 16, [], [p.model_copy() for p in pool], None)
    base_ranks = {s.player.id: s.rank for s in base_state.top_suggestions}

    # Test Round 1 Bust (1-3 pick drop) at rank #3
    p_bust_r1 = [p.model_copy() for p in pool]
    p_bust_r1[2].cheatsheet_notes = "Bust"
    cand_bust_r1 = build_draft_state("test", None, 1, 1, 10, 16, [], p_bust_r1, None)
    bust_r1_ranks = {s.player.id: s.rank for s in cand_bust_r1.top_suggestions}
    bust_r1_shift = base_ranks["p_3"] - bust_r1_ranks["p_3"]
    assert -3 <= bust_r1_shift <= -1

    # Test Rounds 2-3 Bust (4-8 pick drop) at rank #20
    p_bust_r2 = [p.model_copy() for p in pool]
    p_bust_r2[19].cheatsheet_notes = "Bust"
    cand_bust_r2 = build_draft_state("test", None, 1, 1, 10, 16, [], p_bust_r2, None)
    bust_r2_ranks = {s.player.id: s.rank for s in cand_bust_r2.top_suggestions}
    bust_r2_shift = base_ranks["p_20"] - bust_r2_ranks["p_20"]
    assert -8 <= bust_r2_shift <= -4

    # Test Middle Round Breakout (7-12 pick boost) at rank #50
    p_breakout = [p.model_copy() for p in pool]
    p_breakout[49].cheatsheet_notes = "Breakout"
    cand_breakout = build_draft_state("test", None, 1, 1, 10, 16, [], p_breakout, None)
    breakout_ranks = {s.player.id: s.rank for s in cand_breakout.top_suggestions}
    breakout_shift = base_ranks["p_50"] - breakout_ranks["p_50"]
    assert 7 <= breakout_shift <= 12

    # Test Middle Round Sleeper (5-9 pick boost) at rank #50
    p_sleeper = [p.model_copy() for p in pool]
    p_sleeper[49].cheatsheet_notes = "Sleeper"
    cand_sleeper = build_draft_state("test", None, 1, 1, 10, 16, [], p_sleeper, None)
    sleeper_ranks = {s.player.id: s.rank for s in cand_sleeper.top_suggestions}
    sleeper_shift = base_ranks["p_50"] - sleeper_ranks["p_50"]
    assert 5 <= sleeper_shift <= 9


def test_strategy_rule_round_deferral_and_activation() -> None:
    """Verify strategy rules apply clean deferral in early rounds and activate boosts in target rounds."""
    from nfl_helper.models.cheatsheet import CheatsheetContext, PositionalStrategyRule

    ctx = CheatsheetContext(
        positional_strategy=[
            PositionalStrategyRule(
                position="TE",
                target_rounds=[3, 4, 5],
                top_n_target=4,
                rule_description="Target top 4 TE in rounds 3-5",
            ),
            PositionalStrategyRule(
                position="QB",
                target_rounds=[4],
                rule_description="Get Allen in round 4",
            ),
        ]
    )

    p_te = Player(id="te1", name="Brock Bowers", position=Position.TE, team="LV", projected_points=16.0, adp=18.1)
    p_qb = Player(id="qb1", name="Josh Allen", position=Position.QB, team="BUF", projected_points=23.3, adp=27.4)

    # In Round 1 (2 rounds early for TE, 3 rounds early for Allen)
    d_te_r1, note_te_r1 = _evaluate_strategy_rule_adjustments(p_te, ctx, current_round=1)
    d_qb_r1, note_qb_r1 = _evaluate_strategy_rule_adjustments(p_qb, ctx, current_round=1)
    assert d_te_r1 == -1.8  # 2 * -0.90
    assert d_qb_r1 == -0.6  # 3 * -0.20
    assert "Strategy Hint: TE targeted in Rd 3+" in (note_te_r1 or "")
    assert "Strategy Hint: Target Josh Allen in Rd 4" in (note_qb_r1 or "")

    # In Round 3 (Target round for TE, 1 round early for Allen)
    d_te_r3, note_te_r3 = _evaluate_strategy_rule_adjustments(p_te, ctx, current_round=3)
    d_qb_r3, note_qb_r3 = _evaluate_strategy_rule_adjustments(p_qb, ctx, current_round=3)
    assert d_te_r3 == 1.5  # Target round activation bonus
    assert d_qb_r3 == -0.2  # 1 * -0.20
    assert "Strategy Target: Top TE in Rd 3" in (note_te_r3 or "")
    assert "Strategy Hint: Target Josh Allen in Rd 4" in (note_qb_r3 or "")

    # In Round 4 (Target round for Allen)
    d_qb_r4, note_qb_r4 = _evaluate_strategy_rule_adjustments(p_qb, ctx, current_round=4)
    assert d_qb_r4 == 1.5  # Target round activation bonus
    assert "Strategy Target: Josh Allen in Rd 4" in (note_qb_r4 or "")


def test_strategy_rule_target_tier_fading_and_deadline_minimums() -> None:
    """Verify strategy rules fade non-target tiers and strictly lock suggestions to required positions at draft deadline."""
    rules_text = """
    Rules:
    TE - Target the top 4 in rounds 3-5, no second TE if you have a tier 1 TE
    QB - Get one from tier 3 and one from tier 4 or Josh Allen in the fourth round
    RB - Get 4 in the first 10 rounds and minimum 4 for the whole draft
    WR - Get 4 minimum
    """
    ctx = parse_plain_text_cheatsheet(rules_text)

    # 1. Target Tier boosting vs non-target tier fading
    p_t2_qb = Player(id="q2", name="Jaxson Dart", position=Position.QB, team="NYG", projected_points=20.6, tier=2)
    p_t3_qb = Player(id="q3", name="Matthew Stafford", position=Position.QB, team="LAR", projected_points=18.5, tier=3)
    p_t4_qb = Player(id="q4", name="Baker Mayfield", position=Position.QB, team="TB", projected_points=16.5, tier=4)

    d_t2, note_t2 = _evaluate_strategy_rule_adjustments(p_t2_qb, ctx, current_round=6)
    d_t3, note_t3 = _evaluate_strategy_rule_adjustments(p_t3_qb, ctx, current_round=6)
    d_t4, note_t4 = _evaluate_strategy_rule_adjustments(p_t4_qb, ctx, current_round=6)

    assert d_t2 == -2.0
    assert "Strategy Fade: Rule targets Tier 3,4 QB" in (note_t2 or "")
    assert d_t3 == 1.5
    assert "Strategy Target: Tier 3 QB" in (note_t3 or "")
    assert d_t4 == 1.5
    assert "Strategy Target: Tier 4 QB" in (note_t4 or "")

    # 2. Deadline minimums calculation
    # In Round 14 of 15 with 0 K and 0 D/ST (2 picks left): must exclusively lock to K and D/ST
    req_rd14 = _calculate_required_positions(
        current_round=14,
        total_rounds=15,
        roster={"QB": 2, "RB": 4, "WR": 6, "TE": 1, "K": 0, "D/ST": 0},
        active_rules=ctx.strategy_rules,
    )
    assert req_rd14 == {"K", "D/ST"}

    # In Round 9 with only 2 RBs (rule requires 4 in first 10 rounds, 2 rounds left to deadline)
    req_rd9 = _calculate_required_positions(
        current_round=9,
        total_rounds=15,
        roster={"QB": 1, "RB": 2, "WR": 4, "TE": 1, "K": 0, "D/ST": 0},
        active_rules=ctx.strategy_rules,
    )
    assert req_rd9 == {"RB"}
