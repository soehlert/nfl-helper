"""Unit tests for live draft engine, lookahead math, tier clustering, and cliff alerts."""

import time

from nfl_helper.core.cheatsheet import parse_plain_text_cheatsheet
from nfl_helper.core.draft_engine import build_draft_state
from nfl_helper.core.draft_rules import (
    calculate_required_positions,
    calculate_urgent_quota_positions,
    evaluate_strategy_rule_adjustments,
)
from nfl_helper.core.draft_scoring import generate_draft_suggestions
from nfl_helper.core.lookahead import (
    calculate_lookahead,
    calculate_snake_pick_owner,
    calculate_user_draft_schedule,
)
from nfl_helper.core.tier_calculator import (
    _evaluate_on_the_clock_cliff,
    calculate_tier_drop,
    cluster_position_tiers,
    detect_tier_cliffs,
)
from nfl_helper.core.vorp import (
    calculate_vorp,
    calculate_vorp_baselines,
)
from nfl_helper.models.draft import CliffType, DraftPick, PlayerTier
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
        pts = 250.0 - (i * 0.15)
        p = Player(id=f"p_{i}", name=f"Player {i}", position=pos, team="TM", projected_points=pts)
        pool.append(p)

    base_state = build_draft_state("test", None, 1, 1, 10, 16, [], [p.model_copy() for p in pool], None)
    base_ranks = {s.player.id: s.rank for s in base_state.top_suggestions}

    # Test Round 1 Bust at rank #3
    p_bust_r1 = [p.model_copy() for p in pool]
    p_bust_r1[2].cheatsheet_notes = "Bust"
    cand_bust_r1 = build_draft_state("test", None, 1, 1, 10, 16, [], p_bust_r1, None)
    bust_r1_ranks = {s.player.id: s.rank for s in cand_bust_r1.top_suggestions}
    bust_r1_shift = base_ranks["p_3"] - bust_r1_ranks["p_3"]
    assert -8 <= bust_r1_shift <= -1

    # Test Rounds 2-3 Bust at rank #20
    p_bust_r2 = [p.model_copy() for p in pool]
    p_bust_r2[19].cheatsheet_notes = "Bust"
    cand_bust_r2 = build_draft_state("test", None, 1, 1, 10, 16, [], p_bust_r2, None)
    bust_r2_ranks = {s.player.id: s.rank for s in cand_bust_r2.top_suggestions}
    bust_r2_shift = base_ranks["p_20"] - bust_r2_ranks["p_20"]
    assert -10 <= bust_r2_shift <= -2

    # Test Middle Round Breakout (~1 round boost, 5-10 picks) at rank #50
    p_breakout = [p.model_copy() for p in pool]
    p_breakout[49].cheatsheet_notes = "Breakout"
    cand_breakout = build_draft_state("test", None, 1, 1, 10, 16, [], p_breakout, None)
    breakout_ranks = {s.player.id: s.rank for s in cand_breakout.top_suggestions}
    breakout_shift = base_ranks["p_50"] - breakout_ranks["p_50"]
    assert 4 <= breakout_shift <= 12

    # Test Middle Round Sleeper (~1 round boost, 3-8 picks) at rank #50
    p_sleeper = [p.model_copy() for p in pool]
    p_sleeper[49].cheatsheet_notes = "Sleeper"
    cand_sleeper = build_draft_state("test", None, 1, 1, 10, 16, [], p_sleeper, None)
    sleeper_ranks = {s.player.id: s.rank for s in cand_sleeper.top_suggestions}
    sleeper_shift = base_ranks["p_50"] - sleeper_ranks["p_50"]
    assert 3 <= sleeper_shift <= 10


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
                target_tiers=[1],
                rule_description="Target Tier 1 QB in round 4",
            ),
        ]
    )

    p_te = Player(
        id="te1", name="Brock Bowers", position=Position.TE, team="LV", projected_points=16.0, adp=18.1, tier=1
    )
    p_qb = Player(
        id="qb1", name="Josh Allen", position=Position.QB, team="BUF", projected_points=23.3, adp=27.4, tier=1
    )

    # In Round 1 (2 rounds early for TE, 3 rounds early for Tier 1 QB)
    d_te_r1, note_te_r1 = evaluate_strategy_rule_adjustments(p_te, ctx, current_round=1)
    d_qb_r1, note_qb_r1 = evaluate_strategy_rule_adjustments(p_qb, ctx, current_round=1)
    assert d_te_r1 == -1.8  # 2 * -0.90
    assert d_qb_r1 == -2.7  # 3 * -0.90
    assert "Strategy Hint: TE targeted in Rd 3+" in (note_te_r1 or "")
    assert "Strategy Hint: QB targeted in Rd 4+" in (note_qb_r1 or "")

    # In Round 3 (Target round for TE, 1 round early for QB)
    d_te_r3, note_te_r3 = evaluate_strategy_rule_adjustments(p_te, ctx, current_round=3)
    d_qb_r3, note_qb_r3 = evaluate_strategy_rule_adjustments(p_qb, ctx, current_round=3)
    assert d_te_r3 == 1.5  # Target round activation bonus
    assert d_qb_r3 == -0.9  # 1 * -0.90
    assert "Strategy Target: Top TE in Rd 3" in (note_te_r3 or "")
    assert "Strategy Hint: QB targeted in Rd 4+" in (note_qb_r3 or "")

    # In Round 4 (Target round for Tier 1 QB)
    d_qb_r4, note_qb_r4 = evaluate_strategy_rule_adjustments(p_qb, ctx, current_round=4)
    assert d_qb_r4 == 1.5  # Target round activation bonus
    assert "Strategy Target: Top QB in Rd 4" in (note_qb_r4 or "")


def test_strategy_rule_target_tier_fading_and_deadline_minimums() -> None:
    """Verify strategy rules fade non-target tiers and strictly lock suggestions to required positions at draft deadline."""
    rules_text = """
    Rules:
    TE - Target the top 4 in rounds 3-5, no second TE if you have a tier 1 TE
    QB - Get one from tier 3 and one from tier 4 or a tier 1 in round 4
    RB - Get 4 in the first 10 rounds and minimum 4 for the whole draft
    WR - Get 4 minimum
    """
    ctx = parse_plain_text_cheatsheet(rules_text)

    # 1. Target Tier boosting vs non-target tier fading
    p_t2_qb = Player(id="q2", name="Jaxson Dart", position=Position.QB, team="NYG", projected_points=20.6, tier=2)
    p_t3_qb = Player(id="q3", name="Matthew Stafford", position=Position.QB, team="LAR", projected_points=18.5, tier=3)
    p_t4_qb = Player(id="q4", name="Baker Mayfield", position=Position.QB, team="TB", projected_points=16.5, tier=4)

    d_t2, note_t2 = evaluate_strategy_rule_adjustments(p_t2_qb, ctx, current_round=6)
    d_t3, note_t3 = evaluate_strategy_rule_adjustments(p_t3_qb, ctx, current_round=6)
    d_t4, note_t4 = evaluate_strategy_rule_adjustments(p_t4_qb, ctx, current_round=6)

    assert d_t2 == -0.6
    assert "Strategy Hint: Rule prefers Tier 1,3,4 QB" in (note_t2 or "")
    assert d_t3 == 1.0
    assert "Strategy Target: Tier 3 QB" in (note_t3 or "")
    assert d_t4 == 1.0
    assert "Strategy Target: Tier 4 QB" in (note_t4 or "")

    # 2. Deadline minimums calculation
    # In Round 14 of 15 with 0 K and 0 D/ST (2 picks left): must exclusively lock to K and D/ST
    req_rd14 = calculate_required_positions(
        current_round=14,
        total_rounds=15,
        roster={"QB": 2, "RB": 4, "WR": 6, "TE": 1, "K": 0, "D/ST": 0},
        cheatsheet_context=ctx,
    )
    assert req_rd14 == {"K", "D/ST"}

    # In Round 9 with only 2 RBs (rule requires 4 in first 10 rounds, 2 rounds left to deadline)
    urgent_rd9 = calculate_urgent_quota_positions(
        current_round=9,
        roster={"QB": 1, "RB": 2, "WR": 4, "TE": 1, "K": 0, "D/ST": 0},
        cheatsheet_context=ctx,
    )
    assert urgent_rd9 == {"RB"}


def test_roster_aware_conditional_caps_suppression() -> None:
    """Verify drafting Tier 1 QB/TE strictly suppresses subsequent QBs/TEs from top suggestions at Round 10."""
    rules_text = """
    Rounds 1-2 - only RB/WR and at least 1 RB
    QB - Get a tier 1 in round 4 or one from tier 3 and one from tier 4. If you get a tier 1 only one QB total.
    RB - Get 4 in the first 10 rounds and minimum 4 for the whole draft
    WR - Get 4 minimum
    TE - Target the top 4 in rounds 3-5, no second TE if you have a tier 1 TE
    """
    ctx = parse_plain_text_cheatsheet(rules_text)

    # Drafted roster with Tier 1 QB (Burrow) and Tier 1 TE (Loveland)
    p_burrow = Player(
        id="q_burrow", name="Joe Burrow", position=Position.QB, team="CIN", projected_points=22.0, cheatsheet_tier=1
    )
    p_loveland = Player(
        id="t_loveland",
        name="Colston Loveland",
        position=Position.TE,
        team="CHI",
        projected_points=14.0,
        cheatsheet_tier=1,
    )
    drafted_players = [p_burrow, p_loveland]

    # Evaluate Matthew Stafford (Tier 3 QB) and Travis Kelce (Tier 3 TE) in Round 10
    p_stafford = Player(
        id="q_stafford",
        name="Matthew Stafford",
        position=Position.QB,
        team="LAR",
        projected_points=18.9,
        cheatsheet_tier=3,
        adp=83.4,
    )
    p_kelce = Player(
        id="t_kelce",
        name="Travis Kelce",
        position=Position.TE,
        team="KC",
        projected_points=13.0,
        cheatsheet_tier=3,
        adp=96.3,
    )
    p_pittman = Player(
        id="w_pittman",
        name="Michael Pittman",
        position=Position.WR,
        team="PIT",
        projected_points=10.6,
        cheatsheet_tier=5,
        adp=90.4,
    )

    d_q, note_q = evaluate_strategy_rule_adjustments(
        p_stafford, ctx, current_round=10, user_drafted_players=drafted_players
    )
    d_t, note_t = evaluate_strategy_rule_adjustments(
        p_kelce, ctx, current_round=10, user_drafted_players=drafted_players
    )
    _d_w, _note_w = evaluate_strategy_rule_adjustments(
        p_pittman, ctx, current_round=10, user_drafted_players=drafted_players
    )

    # Assert strict suppression of QB and TE
    assert d_q == -3.0
    assert "Strategy: Max 1 QB (Drafted Tier 1 Joe Burrow)" in (note_q or "")
    assert d_t == -3.0
    assert "Strategy: Max 1 TE (Drafted Tier 1 Colston Loveland)" in (note_t or "")

    # Suggestions test at Pick 96 (Round 10)
    avail = [p_stafford, p_kelce, p_pittman]
    baselines = {"QB": 17.0, "RB": 10.0, "WR": 10.0, "TE": 9.0, "K": 8.0, "D/ST": 8.0}
    tiers_by_pos = {
        "QB": cluster_position_tiers([p_stafford], "QB"),
        "TE": cluster_position_tiers([p_kelce], "TE"),
        "WR": cluster_position_tiers([p_pittman], "WR"),
    }
    suggs = generate_draft_suggestions(
        available_players=avail,
        tiers_by_pos=tiers_by_pos,
        cliff_warnings=[],
        baselines=baselines,
        overall_pick=96,
        cheatsheet_context=ctx,
        total_teams=10,
        user_roster_counts={"QB": 1, "TE": 1, "WR": 3, "RB": 4},
        total_rounds=15,
        user_drafted_players=drafted_players,
    )

    # Pittman should be rank #1; Kelce and Stafford should be completely excluded from suggestions
    assert suggs[0].player.id == "w_pittman"
    suggested_ids = {s.player.id for s in suggs}
    assert "q_stafford" not in suggested_ids
    assert "t_kelce" not in suggested_ids


def test_round_target_window_quota_and_exclusivity() -> None:
    """Verify under 'Rounds 1-2 - only RB/WR and at least 1 RB', drafting WR in Round 1 strictly locks Round 2 to RB."""
    rules_text = """
    Rounds 1-2 - only RB/WR and at least 1 RB
    QB - Get a tier 1 in round 4 or one from tier 3 and one from tier 4. If you get a tier 1 only one QB total.
    RB - Get 4 in the first 10 rounds and minimum 4 for the whole draft
    WR - Get 4 minimum
    TE - Target the top 4 in rounds 3-5, no second TE if you have a tier 1 TE
    """
    ctx = parse_plain_text_cheatsheet(rules_text)

    p_wr1 = Player(id="w1", name="CeeDee Lamb", position=Position.WR, team="DAL", projected_points=18.0, adp=2.0)
    p_wr2 = Player(id="w2", name="Justin Jefferson", position=Position.WR, team="MIN", projected_points=17.5, adp=4.0)
    p_rb1 = Player(id="r1", name="Bijan Robinson", position=Position.RB, team="ATL", projected_points=17.0, adp=3.0)
    p_qb1 = Player(id="q1", name="Josh Allen", position=Position.QB, team="BUF", projected_points=24.0, adp=15.0)

    avail = [p_wr1, p_wr2, p_rb1, p_qb1]
    baselines = {"QB": 17.0, "RB": 10.0, "WR": 10.0, "TE": 9.0, "K": 8.0, "D/ST": 8.0}
    tiers_by_pos = {
        "WR": cluster_position_tiers([p_wr1, p_wr2], "WR"),
        "RB": cluster_position_tiers([p_rb1], "RB"),
        "QB": cluster_position_tiers([p_qb1], "QB"),
    }

    # 1. In Round 1 (0 players drafted): Suggestions include RB and WR, but exclude QB
    suggs_r1 = generate_draft_suggestions(
        available_players=avail,
        tiers_by_pos=tiers_by_pos,
        cliff_warnings=[],
        baselines=baselines,
        overall_pick=1,
        cheatsheet_context=ctx,
        total_teams=10,
        user_roster_counts={},
        total_rounds=15,
        user_drafted_players=[],
    )
    r1_pos = {s.player.position for s in suggs_r1}
    assert Position.WR in r1_pos
    assert Position.RB in r1_pos
    assert Position.QB not in r1_pos

    # 2. In Round 2 after drafting WR in Round 1: Suggestions MUST exclusively be RB (no WRs)
    suggs_r2 = generate_draft_suggestions(
        available_players=[p_wr2, p_rb1, p_qb1],
        tiers_by_pos=tiers_by_pos,
        cliff_warnings=[],
        baselines=baselines,
        overall_pick=11,  # Round 2
        cheatsheet_context=ctx,
        total_teams=10,
        user_roster_counts={"WR": 1},
        total_rounds=15,
        user_drafted_players=[p_wr1],
    )
    r2_pos = {s.player.position for s in suggs_r2}
    assert r2_pos == {Position.RB}
    assert all(s.player.position == Position.RB for s in suggs_r2)
    assert suggs_r2[0].player.id == "r1"


def test_two_qb_quota_evaluation_when_tier3_drafted() -> None:
    """Verify when user drafts Tier 3 QB, remaining Tier 3 QBs are not boosted while Tier 4 QBs are boosted."""
    rules_text = """
    QB - Get a tier 1 in round 4 or one from tier 3 and one from tier 4. If you get a tier 1 only one QB total.
    """
    ctx = parse_plain_text_cheatsheet(rules_text)

    # User already drafted a Tier 3 QB
    p_goff = Player(
        id="q_goff", name="Jared Goff", position=Position.QB, team="DET", projected_points=18.0, cheatsheet_tier=3
    )
    drafted_players = [p_goff]

    p_stafford = Player(
        id="q_stafford",
        name="Matthew Stafford",
        position=Position.QB,
        team="LAR",
        projected_points=18.5,
        cheatsheet_tier=3,
    )
    p_mayfield = Player(
        id="q_mayfield",
        name="Baker Mayfield",
        position=Position.QB,
        team="TB",
        projected_points=16.5,
        cheatsheet_tier=4,
    )

    d_t3, note_t3 = evaluate_strategy_rule_adjustments(
        p_stafford, ctx, current_round=7, user_drafted_players=drafted_players
    )
    d_t4, note_t4 = evaluate_strategy_rule_adjustments(
        p_mayfield, ctx, current_round=7, user_drafted_players=drafted_players
    )

    assert d_t3 == 0.0
    assert note_t3 is None or "Strategy" not in (note_t3 or "")
    assert d_t4 == 1.0
    assert "Strategy Target: Tier 4 QB" in (note_t4 or "")


def test_tier_cliff_suppression_when_tier1_qb_drafted() -> None:
    """Verify drafting a Tier 1 QB suppresses subsequent QB cliff warnings under Max 1 QB strategy."""
    rules_text = """
    QB - Get a tier 1 in round 4 or one from tier 3 and one from tier 4. If you get a tier 1 only one QB total.
    """
    ctx = parse_plain_text_cheatsheet(rules_text)

    p_hurts = Player(
        id="q_hurts", name="Jalen Hurts", position=Position.QB, team="PHI", projected_points=23.0, cheatsheet_tier=1
    )
    p_kyler = Player(
        id="q_kyler", name="Kyler Murray", position=Position.QB, team="ARI", projected_points=19.5, cheatsheet_tier=2
    )
    p_stafford = Player(
        id="q_stafford",
        name="Matthew Stafford",
        position=Position.QB,
        team="LAR",
        projected_points=17.5,
        cheatsheet_tier=3,
    )

    tiers_by_pos = {
        "QB": [
            PlayerTier(tier_num=2, position="QB", players=[p_kyler], avg_projected=19.5, count=1),
            PlayerTier(tier_num=3, position="QB", players=[p_stafford], avg_projected=17.5, count=1),
        ]
    }

    # At Pick 85 (Round 9), user has drafted Jalen Hurts
    cliffs = detect_tier_cliffs(
        tiers_by_pos=tiers_by_pos,
        picks_until_turn=0,
        snake_turn_gap=10,
        is_on_the_clock=True,
        current_pick=85,
        user_roster_counts={"QB": 1},
        cheatsheet_context=ctx,
        user_drafted_players=[p_hurts],
    )

    # Must be strictly suppressed (0 QB cliff warnings)
    qb_cliffs = [w for w in cliffs if w.position == "QB"]
    assert len(qb_cliffs) == 0


def test_calibrated_scarcity_thresholds_ignore_minor_drops() -> None:
    """Verify minor -0.7 pt drops for single-player tiers in late rounds do not trigger cliff warnings."""
    p_single = Player(id="rb_single", name="Single RB", position=Position.RB, team="FA", projected_points=12.2, tier=4)
    p_next = Player(id="rb_next", name="Next RB", position=Position.RB, team="FA", projected_points=11.5, tier=5)

    tier_4 = PlayerTier(tier_num=4, position="RB", players=[p_single], avg_projected=12.2, count=1)
    tier_5 = PlayerTier(tier_num=5, position="RB", players=[p_next], avg_projected=11.5, count=1)

    # Pick 76 (Round 8): drop is only 0.7 pts (12.2 - 11.5)
    warning = _evaluate_on_the_clock_cliff(tier_4, tier_5, snake_turn_gap=8, current_pick=76)
    assert warning is None


def test_player_pool_depth_preserves_non_qbs_late_rounds() -> None:
    """Verify in round 9+ (pick 85+) that deep player pools provide available RBs, WRs, and TEs."""
    from tests.fixtures.demo_rosters import get_mock_player_pool

    full_pool = get_mock_player_pool()
    assert len(full_pool) >= 250

    # Simulate 84 picks made across positions
    drafted_picks = [
        DraftPick(
            round_num=(i // 10) + 1,
            round_pick=(i % 10) + 1,
            overall_pick=i + 1,
            team_id=str((i % 10) + 1),
            team_name=f"Team {(i % 10) + 1}",
            player_id=full_pool[i].id,
            player_name=full_pool[i].name,
            position=str(full_pool[i].position),
        )
        for i in range(84)
    ]

    state = build_draft_state(
        league_id="test_league",
        draft_id="draft_1",
        overall_pick=85,
        user_draft_slot=5,
        total_teams=10,
        total_rounds=15,
        recent_picks=drafted_picks,
        all_players=full_pool,
    )

    # Assert multiple positions remain available in top suggestions and pools
    avail_rb = state.available_players_by_pos.get("RB", [])
    avail_wr = state.available_players_by_pos.get("WR", [])
    avail_te = state.available_players_by_pos.get("TE", [])

    assert len(avail_rb) >= 15
    assert len(avail_wr) >= 20
    assert len(avail_te) >= 10

    # Top suggestions should not be exclusively QBs
    suggested_positions = {s.player.position for s in state.top_suggestions[:10]}
    assert len(suggested_positions) >= 3


def test_draft_turn_qb_suppression_when_drafted_on_first_turn_pick() -> None:
    """Verify drafting a QB on the first pick of a turn (e.g. pick 50 in slot 10) suppresses QBs on pick 51."""
    from tests.fixtures.demo_rosters import get_mock_player_pool

    rules_text = """
    QB - Get a tier 1 in round 4 or one from tier 3 and one from tier 4. If you get a tier 1 only one QB total.
    """
    ctx = parse_plain_text_cheatsheet(rules_text)
    pool = get_mock_player_pool()

    maye = next(p for p in pool if "Maye" in p.name)

    picks = [
        DraftPick(
            round_num=5,
            round_pick=10,
            overall_pick=50,
            team_id="10",
            team_name="My Team",
            player_id=maye.id,
            player_name=maye.name,
            position="QB",
        )
    ]

    state = build_draft_state(
        league_id="test_league",
        draft_id="d1",
        overall_pick=51,
        user_draft_slot=10,
        total_teams=10,
        total_rounds=15,
        recent_picks=picks,
        all_players=pool,
        cheatsheet_context=ctx,
        user_team_id="10",
    )

    # User now has 1 QB; subsequent QBs (Hurts, Burrow) must NOT be promoted as strategy targets in top suggestions
    top_3_positions = [s.player.position for s in state.top_suggestions[:3]]
    assert Position.QB not in top_3_positions

    # Verify no strategy target bonus for other QBs
    for s in state.top_suggestions:
        if s.player.position == Position.QB:
            assert "Strategy Target: Tier 1 QB" not in s.reason


def test_quota_urgency_and_surplus_fading() -> None:
    """Verify unfulfilled minimum quotas and round deadlines elevate RB/DST while fading surplus WRs."""
    rules_text = """
    Rounds 1-2 - only RB/WR and at least 1 RB
    QB - Get a tier 1 in round 4 or one from tier 3 and one from tier 4. If you get a tier 1 only one QB total.
    RB - Get 4 in the first 10 rounds and minimum 4 for the whole draft
    WR - Get 4 minimum
    TE - Target the top 4 in rounds 3-5, no second TE if you have a tier 1 TE
    """
    ctx = parse_plain_text_cheatsheet(rules_text)

    # In Round 13 of 15, user has 3 RBs (needs 4), 6 WRs (exceeds 4), 0 D/ST
    p_rb = Player(id="r1", name="Test RB", position=Position.RB, team="FA", projected_points=10.0)
    p_wr = Player(id="w1", name="Test WR", position=Position.WR, team="FA", projected_points=10.0, adp=104.0)
    p_dst = Player(id="d1", name="Test DST", position=Position.DST, team="FA", projected_points=8.0)

    baselines = {"QB": 17.0, "RB": 10.0, "WR": 10.0, "TE": 9.0, "K": 8.0, "D/ST": 8.0}
    tiers_by_pos = {
        "RB": cluster_position_tiers([p_rb], "RB"),
        "WR": cluster_position_tiers([p_wr], "WR"),
        "D/ST": cluster_position_tiers([p_dst], "D/ST"),
    }

    suggs = generate_draft_suggestions(
        available_players=[p_rb, p_wr, p_dst],
        tiers_by_pos=tiers_by_pos,
        cliff_warnings=[],
        baselines=baselines,
        overall_pick=121,  # Round 13 Pick 1
        cheatsheet_context=ctx,
        total_teams=10,
        user_roster_counts={"QB": 1, "TE": 1, "K": 1, "WR": 6, "RB": 3, "D/ST": 0},
        total_rounds=15,
        user_drafted_players=[],
    )

    # D/ST and RB should outrank surplus WR in late rounds
    ranks = {s.player.position: s.rank for s in suggs}
    assert ranks[Position.DST] < ranks[Position.WR]
    assert ranks[Position.RB] < ranks[Position.WR]


def test_tier_cliff_and_quota_urgency_round_7_rb() -> None:
    """Verify that a 0.8 pt drop with 2 players remaining triggers a cliff warning and boosts RB ranking."""
    p_rb1 = Player(id="r1", name="RB One", position=Position.RB, team="A", projected_points=11.2, adp=65.0)
    p_rb2 = Player(id="r2", name="RB Two", position=Position.RB, team="B", projected_points=11.2, adp=67.0)
    p_rb3 = Player(id="r3", name="RB Three", position=Position.RB, team="C", projected_points=10.4, adp=80.0)

    t4 = PlayerTier(tier_num=4, position="RB", players=[p_rb1, p_rb2], avg_projected=11.2, count=4)
    t5 = PlayerTier(tier_num=5, position="RB", players=[p_rb3], avg_projected=10.4, count=6)

    # 1. On-the-clock cliff warning triggers on 0.8 pt drop
    warnings = detect_tier_cliffs(
        tiers_by_pos={"RB": [t4, t5]},
        picks_until_turn=0,
        snake_turn_gap=4,
        is_on_the_clock=True,
        current_pick=68,
        user_roster_counts={"RB": 2, "WR": 2, "QB": 1, "TE": 1},
    )
    assert len(warnings) == 1
    assert warnings[0].position == "RB"
    assert warnings[0].current_tier == 4
    assert warnings[0].players_remaining == 2
    assert warnings[0].next_tier_drop_points == 0.8


def test_rb_handcuff_synergy_boost() -> None:
    """Verify that backup RBs on the same NFL team as a drafted RB receive a handcuff synergy boost."""
    p_swift = Player(id="swift", name="D'Andre Swift", position=Position.RB, team="CHI", projected_points=12.0)
    p_monangai = Player(
        id="monangai", name="Kyle Monangai", position=Position.RB, team="CHI", projected_points=10.0, adp=110.0
    )
    p_other_rb = Player(id="other", name="Other RB", position=Position.RB, team="DEN", projected_points=10.0, adp=110.0)

    tiers_by_pos = {
        "RB": cluster_position_tiers([p_monangai, p_other_rb], "RB"),
    }
    baselines = {"RB": 10.0, "WR": 10.0, "QB": 15.0, "TE": 9.0, "K": 8.0, "D/ST": 8.0}

    suggs = generate_draft_suggestions(
        available_players=[p_other_rb, p_monangai],
        tiers_by_pos=tiers_by_pos,
        cliff_warnings=[],
        baselines=baselines,
        overall_pick=105,
        user_drafted_players=[p_swift],
        user_roster_counts={"RB": 1},
        total_teams=10,
        total_rounds=15,
    )

    assert len(suggs) == 2
    assert suggs[0].player.name == "Kyle Monangai"
    assert suggs[0].score > suggs[1].score
    assert "Handcuff (D'Andre Swift)" in suggs[0].reason


def test_turn_lookahead_and_soft_quota_urgency() -> None:
    """Verify dynamic turn lookahead moderates boosts when safe, and soft quota urgency preserves falling value."""
    rules_text = """
    QB - Target Tier 1 in round 4
    RB - Get 4 in the first 10 rounds
    WR - Get 4 minimum
    """
    ctx = parse_plain_text_cheatsheet(rules_text)

    # 1. Turn lookahead test: Burrow (ADP 54) with next pick at 47 vs Maye (ADP 40) with next pick at 47
    p_burrow = Player(
        id="burrow", name="Joe Burrow", position=Position.QB, team="CIN", projected_points=20.0, adp=54.0, tier=1
    )
    p_maye = Player(
        id="maye", name="Drake Maye", position=Position.QB, team="NE", projected_points=21.0, adp=40.0, tier=1
    )

    d_safe, note_safe = evaluate_strategy_rule_adjustments(
        p_burrow, ctx, current_round=4, next_user_pick=47, remaining_tier_count=2
    )
    d_urgent, note_urgent = evaluate_strategy_rule_adjustments(
        p_maye, ctx, current_round=4, next_user_pick=47, remaining_tier_count=2
    )

    assert d_safe == 0.25
    assert "Safe across turn" in (note_safe or "")
    assert d_urgent == 1.50
    assert "Top QB in Rd 4" in (note_urgent or "")

    # 2. Soft quota urgency test: Falling star WR (Mike Evans) remains visible alongside boosted RB
    p_evans = Player(id="evans", name="Mike Evans", position=Position.WR, team="TB", projected_points=14.5, adp=58.0)
    p_stevenson = Player(
        id="stevenson", name="Rhamondre Stevenson", position=Position.RB, team="NE", projected_points=11.0, adp=83.0
    )

    tiers_by_pos = {
        "WR": cluster_position_tiers([p_evans], "WR"),
        "RB": cluster_position_tiers([p_stevenson], "RB"),
    }
    baselines = {"RB": 10.0, "WR": 10.0, "QB": 15.0, "TE": 9.0, "K": 8.0, "D/ST": 8.0}

    suggs = generate_draft_suggestions(
        available_players=[p_evans, p_stevenson],
        tiers_by_pos=tiers_by_pos,
        cliff_warnings=[],
        baselines=baselines,
        overall_pick=67,
        cheatsheet_context=ctx,
        user_roster_counts={"QB": 1, "TE": 1, "WR": 2, "RB": 2},
        total_teams=10,
        total_rounds=15,
        next_user_pick=74,
    )

    # Both players are present and ranked (Mike Evans is not deleted)
    suggested_names = [s.player.name for s in suggs]
    assert "Mike Evans" in suggested_names
    assert "Rhamondre Stevenson" in suggested_names


def test_round_14_preserves_skill_positions_alongside_kickers() -> None:
    """Verify that in Round 14 with 1 D/ST and 0 K (2 picks left), skill players remain visible alongside top kickers."""
    p_kicker = Player(
        id="k1", name="Brandon Aubrey", position=Position.K, team="DAL", projected_points=8.5, adp=135.0, tier=1
    )
    p_wr_sleeper = Player(
        id="w_sleep", name="Jalil Farooq", position=Position.WR, team="FA", projected_points=10.0, adp=136.0, tier=4
    )
    p_rb_backup = Player(
        id="r_back", name="Blake Corum", position=Position.RB, team="LAR", projected_points=9.5, adp=137.0, tier=4
    )

    tiers_by_pos = {
        "K": cluster_position_tiers([p_kicker], "K"),
        "WR": cluster_position_tiers([p_wr_sleeper], "WR"),
        "RB": cluster_position_tiers([p_rb_backup], "RB"),
    }
    baselines = {"RB": 10.0, "WR": 10.0, "QB": 15.0, "TE": 9.0, "K": 8.0, "D/ST": 8.0}

    suggs = generate_draft_suggestions(
        available_players=[p_kicker, p_wr_sleeper, p_rb_backup],
        tiers_by_pos=tiers_by_pos,
        cliff_warnings=[],
        baselines=baselines,
        overall_pick=134,  # Round 14, Pick 4 in 10-team league (2 picks left: Rd 14, Rd 15)
        user_roster_counts={"QB": 1, "TE": 1, "WR": 5, "RB": 5, "D/ST": 1, "K": 0},
        total_teams=10,
        total_rounds=15,
    )

    suggested_names = [s.player.name for s in suggs]
    # Kicker is present and elevated, but skill players are NOT hard-filtered or removed
    assert "Brandon Aubrey" in suggested_names
    assert "Jalil Farooq" in suggested_names
    assert "Blake Corum" in suggested_names


def test_reason_breakdown_transparency_in_suggestions() -> None:
    """Verify that reason strings contain explicit point breakdown line items for strategy, quota urgency, and reaches."""
    from nfl_helper.core.draft_rationale import build_suggestion_rationale

    p = Player(id="p1", name="Test Player", position=Position.RB, team="CIN", projected_points=12.0, adp=85.0)
    top_tier_info = {"RB": (1, 3, 1.2)}

    reason = build_suggestion_rationale(
        player=p,
        vorp=2.5,
        tier_bonus=1.5,
        scarcity_bonus=0.0,
        cliff=None,
        adp_delta=0.0,
        overall_pick=67,
        top_tier_info=top_tier_info,
        rule_delta=1.5,
        rule_note="Strategy Target: Top RB in Rd 7",
        quota_urgency_bonus=1.8,
        quota_urgency_note="Quota Urgency: Need 2 RB by Rd 10",
        reach_penalty=1.2,
    )

    assert "+2.5 pts VORP (Tier 1 RB)" in reason
    assert "+1.5 pts Strategy Target: Top RB in Rd 7" in reason
    assert "+1.8 pts Quota Urgency: Need 2 RB by Rd 10" in reason
    assert "-1.2 pts (Market Reach" in reason


def test_season_ending_injury_exclusion_and_active_injury_score_deductions() -> None:
    """Verify season-ending injured players are completely blanked out, and recovering players receive calibrated deductions."""
    from nfl_helper.models.player import InjuryStatus

    p_healthy = Player(id="h1", name="Healthy Player", position=Position.RB, team="SF", projected_points=12.0, tier=1)
    p_ques = Player(
        id="q1",
        name="Questionable Player",
        position=Position.RB,
        team="SF",
        projected_points=12.0,
        tier=1,
        injury_status=InjuryStatus.QUESTIONABLE,
    )
    p_ir = Player(
        id="ir1",
        name="IR Player",
        position=Position.RB,
        team="SF",
        projected_points=12.0,
        tier=1,
        injury_status=InjuryStatus.IR,
    )
    p_season_out = Player(
        id="so1",
        name="Season Out Player",
        position=Position.RB,
        team="SF",
        projected_points=15.0,
        tier=1,
        cheatsheet_notes="Torn ACL - out for the season",
    )

    tiers_by_pos = {
        "RB": cluster_position_tiers([p_healthy, p_ques, p_ir, p_season_out], "RB"),
    }
    baselines = {"RB": 10.0, "WR": 10.0, "QB": 15.0, "TE": 9.0, "K": 8.0, "D/ST": 8.0}

    suggs = generate_draft_suggestions(
        available_players=[p_season_out, p_ir, p_ques, p_healthy],
        tiers_by_pos=tiers_by_pos,
        cliff_warnings=[],
        baselines=baselines,
        overall_pick=15,
        total_teams=10,
        total_rounds=15,
    )

    suggested_names = [s.player.name for s in suggs]
    # 1. Season-ending injured player must be COMPLETELY excluded
    assert "Season Out Player" not in suggested_names

    # 2. Ranking order: Healthy > Questionable > IR
    assert suggested_names[0] == "Healthy Player"
    assert suggested_names[1] == "Questionable Player"
    assert suggested_names[2] == "IR Player"

    # 3. Explicit point deduction reasons
    ques_sugg = next(s for s in suggs if s.player.name == "Questionable Player")
    ir_sugg = next(s for s in suggs if s.player.name == "IR Player")
    assert "-0.4 pts Injury: Questionable" in ques_sugg.reason
    assert "-2.5 pts Injury: IR (4+ Wks)" in ir_sugg.reason


def test_universal_strategy_alert_banners_for_arbitrary_rules() -> None:
    """Verify strategy alerts generate clear, informative banners across round targets, target tiers, and tier caps."""
    rules_text = """
    Rounds 1-2 - only RB/WR and at least 1 RB
    TE - Target the top 4 in rounds 3-5, no second TE if you have a tier 1 TE
    QB - Get a tier 1 in round 4 or one from tier 3 and one from tier 4. If you get a tier 1 only one QB total.
    RB - Get 4 in the first 10 rounds
    """
    ctx = parse_plain_text_cheatsheet(rules_text)

    p_burrow = Player(
        id="burrow", name="Joe Burrow", position=Position.QB, team="CIN", projected_points=22.0, cheatsheet_tier=1
    )
    p_wr1 = Player(id="wr1", name="CeeDee Lamb", position=Position.WR, team="DAL", projected_points=18.0)

    # 1. In Round 2 with 1 WR drafted: alerts user to prioritize RB in Round 2
    pick_wr1 = DraftPick(
        round_num=1,
        round_pick=1,
        overall_pick=1,
        team_id="user_team",
        team_name="User Team",
        player_id="wr1",
        player_name="CeeDee Lamb",
        position="WR",
    )
    dummy_picks_r1 = [
        DraftPick(
            round_num=1,
            round_pick=i,
            overall_pick=i,
            team_id=f"team_{i}",
            team_name=f"Team {i}",
            player_id=f"p_{i}",
            player_name=f"Player {i}",
            position="RB",
        )
        for i in range(2, 11)
    ]
    state_r2 = build_draft_state(
        league_id="test_league",
        draft_id="test_draft",
        overall_pick=11,
        user_draft_slot=1,
        total_teams=10,
        total_rounds=15,
        recent_picks=[pick_wr1, *dummy_picks_r1],  # 10 picks completed, now at pick 11 (Round 2)
        all_players=[p_burrow, p_wr1],
        cheatsheet_context=ctx,
        user_team_id="user_team",
    )
    alerts_r2 = " ".join(state_r2.strategy_alerts)
    assert "Rule 'Rounds 1-2' prioritizes RB in Round 2" in alerts_r2

    # 2. In Round 3 with no TE drafted: alerts user about Top 4 TE target window
    dummy_picks_20 = [
        DraftPick(
            round_num=(i - 1) // 10 + 1,
            round_pick=(i - 1) % 10 + 1,
            overall_pick=i,
            team_id="other_team",
            team_name="Other Team",
            player_id=f"dummy_{i}",
            player_name=f"Dummy {i}",
            position="WR",
        )
        for i in range(1, 21)
    ]
    state_r3 = build_draft_state(
        league_id="test_league",
        draft_id="test_draft",
        overall_pick=21,
        user_draft_slot=1,
        total_teams=10,
        total_rounds=15,
        recent_picks=dummy_picks_20,  # Pick 21 (Round 3)
        all_players=[p_burrow, p_wr1],
        cheatsheet_context=ctx,
        user_team_id="user_team",
    )
    alerts_r3 = " ".join(state_r3.strategy_alerts)
    assert "Rule targets Top 4 TE in Rounds 3-5" in alerts_r3

    # 3. After drafting Tier 1 QB Burrow: alerts user that no more QBs are needed
    p_pick = DraftPick(
        round_num=1,
        round_pick=1,
        overall_pick=1,
        team_id="user_team",
        team_name="User Team",
        player_id="burrow",
        player_name="Joe Burrow",
        position="QB",
    )
    state_capped = build_draft_state(
        league_id="test_league",
        draft_id="test_draft",
        overall_pick=2,
        user_draft_slot=1,
        total_teams=10,
        total_rounds=15,
        recent_picks=[p_pick],
        all_players=[p_burrow, p_wr1],
        cheatsheet_context=ctx,
        user_team_id="user_team",
    )
    alerts_capped = " ".join(state_capped.strategy_alerts)
    assert "Drafted Tier 1 Joe Burrow. You don't need any more QBs." in alerts_capped


def test_te_target_rule_safe_across_turn_boost_calibration() -> None:
    """Verify TE target rule applies gentle +0.25 boost when player is safe across upcoming turn."""
    rules_text = "TE - Target the top 4 in rounds 3-5, no second TE if you have a tier 1 TE"
    ctx = parse_plain_text_cheatsheet(rules_text)

    # Colston Loveland: Tier 1 TE, ADP 41.2 (Pick 41)
    loveland = Player(
        id="loveland",
        name="Colston Loveland",
        position=Position.TE,
        team="CHI",
        projected_points=12.6,
        cheatsheet_tier=1,
        adp=41.2,
    )
    # Nico Collins: Tier 2 WR, ADP 25.6 (Pick 25)
    collins = Player(
        id="collins",
        name="Nico Collins",
        position=Position.WR,
        team="HOU",
        projected_points=14.3,
        cheatsheet_tier=2,
        adp=25.6,
    )

    # Pick 27 (Round 3, Slot 7). Next user pick is Pick 34 (Round 4, Slot 4)
    # Loveland ADP 41.2 >= next pick 34 -> safe across turn
    dummy_picks_26 = [
        DraftPick(
            round_num=(i - 1) // 10 + 1,
            round_pick=(i - 1) % 10 + 1,
            overall_pick=i,
            team_id="other_team",
            team_name="Other Team",
            player_id=f"dummy_{i}",
            player_name=f"Dummy {i}",
            position="RB",
        )
        for i in range(1, 27)
    ]
    # Supporting pool for realistic VORP baselines
    extra_wrs = [
        Player(
            id=f"wr_extra_{i}",
            name=f"WR {i}",
            position=Position.WR,
            team="TM",
            projected_points=15.0 - i * 0.2,
            cheatsheet_tier=2,
            adp=20.0 + i,
        )
        for i in range(1, 30)
    ]
    extra_tes = [
        Player(
            id=f"te_extra_{i}",
            name=f"TE {i}",
            position=Position.TE,
            team="TM",
            projected_points=13.0 - i * 0.3,
            cheatsheet_tier=2,
            adp=35.0 + i,
        )
        for i in range(1, 15)
    ]

    state = build_draft_state(
        league_id="test_league",
        draft_id="test_draft",
        overall_pick=27,
        user_draft_slot=7,
        total_teams=10,
        total_rounds=15,
        recent_picks=dummy_picks_26,
        all_players=[loveland, collins, *extra_wrs, *extra_tes],
        cheatsheet_context=ctx,
        user_team_id="user_team",
    )

    sug_map = {s.player.id: s for s in state.top_suggestions}
    assert "loveland" in sug_map
    assert "collins" in sug_map
    # Collins (active round-appropriate WR) should score higher than Loveland (safe across turn)
    assert sug_map["collins"].score > sug_map["loveland"].score
    assert (
        "+0.2 pts Strategy Target" in sug_map["loveland"].reason
        or "+0.25" in sug_map["loveland"].reason
        or "Safe across turn" in sug_map["loveland"].reason
    )


def test_full_untouched_tier_cliff_suppression() -> None:
    """Verify full/untouched tiers (e.g. 6 RBs, 8 WRs remaining) do NOT trigger false upcoming turn cliff alarms."""
    # Build 6 Tier 4 RBs and 8 Tier 4 WRs
    rbs = [
        Player(
            id=f"rb_{i}",
            name=f"RB {i}",
            position=Position.RB,
            team="TM",
            projected_points=11.0,
            cheatsheet_tier=4,
            adp=55.0 + i,
        )
        for i in range(1, 7)
    ]
    wrs = [
        Player(
            id=f"wr_{i}",
            name=f"WR {i}",
            position=Position.WR,
            team="TM",
            projected_points=11.5,
            cheatsheet_tier=4,
            adp=52.0 + i,
        )
        for i in range(1, 9)
    ]

    # At pick 50 (Round 5), 4 picks away from Slot 7 with a 12-pick turn gap
    dummy_picks_49 = [
        DraftPick(
            round_num=(i - 1) // 10 + 1,
            round_pick=(i - 1) % 10 + 1,
            overall_pick=i,
            team_id="other_team",
            team_name="Other Team",
            player_id=f"dummy_{i}",
            player_name=f"Dummy {i}",
            position="QB",
        )
        for i in range(1, 50)
    ]
    state = build_draft_state(
        league_id="test_league",
        draft_id="test_draft",
        overall_pick=50,
        user_draft_slot=7,
        total_teams=10,
        total_rounds=15,
        recent_picks=dummy_picks_49,
        all_players=[*rbs, *wrs],
        cheatsheet_context=None,
        user_team_id="user_team",
    )

    # Full tiers of 6 RBs and 8 WRs should NOT trigger cliff warnings
    cliff_positions = [c.position for c in state.cliff_warnings]
    assert "RB" not in cliff_positions
    assert "WR" not in cliff_positions
