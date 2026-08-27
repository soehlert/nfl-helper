"""Unit tests for domain data models."""

from datetime import UTC, datetime

from nfl_helper.models.draft import CliffType, DraftPick, DraftState, DraftSuggestion, PlayerTier, TierCliffWarning
from nfl_helper.models.player import InjuryStatus, Player, PlayerMatchupScore, Position
from nfl_helper.models.roster import (
    AddDropRecommendation,
    LineupSolution,
    RosterAdjustment,
    StreamingOption,
    TeamRoster,
    WaiverAnalysis,
)
from nfl_helper.models.session import LeagueProfile, PlatformType


def test_player_model_creation() -> None:
    """Test Player model instantiation and defaults."""
    matchup = PlayerMatchupScore(week=1, opponent="NYG", opponent_rank=28, difficulty_rating=8.5)
    player = Player(
        id="101",
        name="Justin Jefferson",
        position=Position.WR,
        team="MIN",
        projected_points=18.4,
        eligible_slots=["WR", "FLEX"],
        matchups_3wk=[matchup],
    )
    assert player.id == "101"
    assert player.name == "Justin Jefferson"
    assert player.position == "WR"
    assert player.injury_status == InjuryStatus.ACTIVE
    assert len(player.matchups_3wk) == 1
    assert player.matchups_3wk[0].opponent == "NYG"


def test_draft_models_and_cliff_types() -> None:
    """Test DraftState, PlayerTier, and refined TierCliffWarning scenarios."""
    pick = DraftPick(
        round_num=1,
        round_pick=1,
        overall_pick=1,
        team_id="t1",
        team_name="Team 1",
        player_id="p1",
        player_name="Christian McCaffrey",
        position="RB",
    )
    player = Player(id="p2", name="Breece Hall", position=Position.RB, team="NYJ", projected_points=17.5)
    tier = PlayerTier(tier_num=1, position="RB", players=[player], avg_projected=17.5, count=1)

    # 1. On The Clock Cliff
    cliff_otc = TierCliffWarning(
        position="RB",
        current_tier=1,
        players_remaining=1,
        picks_until_turn=0,
        snake_turn_gap=9,
        cliff_risk="CRITICAL",
        cliff_type=CliffType.ON_THE_CLOCK_CLIFF,
        next_tier_drop_points=3.2,
        recommended_action="Draft RB now; tier 1 will deplete across the 9-pick snake turn gap.",
    )
    # 2. Upcoming Turn Cliff
    cliff_upcoming = TierCliffWarning(
        position="WR",
        current_tier=1,
        players_remaining=3,
        picks_until_turn=2,
        snake_turn_gap=10,
        cliff_risk="HIGH",
        cliff_type=CliffType.UPCOMING_TURN_CLIFF,
        next_tier_drop_points=2.8,
        recommended_action="Prepare to draft WR; tier will deplete during your next round turn gap.",
    )
    # 3. Depleted Before Turn Alert
    cliff_depleted = TierCliffWarning(
        position="TE",
        current_tier=1,
        players_remaining=1,
        picks_until_turn=5,
        snake_turn_gap=8,
        cliff_risk="CRITICAL",
        cliff_type=CliffType.DEPLETED_BEFORE_TURN,
        next_tier_drop_points=4.0,
        recommended_action="Tier 1 TE will be depleted before your turn in 5 picks. Pivot draft queue to Tier 2 TE or WR.",
    )

    suggestion = DraftSuggestion(
        rank=1,
        player=player,
        reason="Tier 1 RB with imminent cliff risk",
        vorp=5.2,
        is_cliff_defense=True,
    )
    draft_state = DraftState(
        league_id="12345",
        total_teams=12,
        current_pick=2,
        user_draft_slot=6,
        picks_until_user_turn=4,
        snake_turn_gap=8,
        recent_picks=[pick],
        tiers_by_position={"RB": [tier]},
        cliff_warnings=[cliff_otc, cliff_upcoming, cliff_depleted],
        top_suggestions=[suggestion],
    )
    assert draft_state.total_teams == 12
    assert len(draft_state.recent_picks) == 1
    assert len(draft_state.cliff_warnings) == 3
    assert draft_state.cliff_warnings[0].cliff_type == CliffType.ON_THE_CLOCK_CLIFF
    assert draft_state.cliff_warnings[2].cliff_type == CliffType.DEPLETED_BEFORE_TURN


def test_roster_and_lineup_models() -> None:
    """Test TeamRoster, LineupSolution, and WaiverAnalysis models."""
    p1 = Player(id="p1", name="Lamar Jackson", position=Position.QB, team="BAL", projected_points=22.0)
    p2 = Player(
        id="p2",
        name="Deshaun Watson",
        position=Position.QB,
        team="CLE",
        projected_points=14.0,
        injury_status=InjuryStatus.OUT,
    )

    roster = TeamRoster(team_id="t1", team_name="My Team", players=[p1, p2], starters=[p1], bench=[p2])
    assert len(roster.players) == 2

    ir_adj = RosterAdjustment(
        player_name="Deshaun Watson",
        player_id="p2",
        position="QB",
        current_slot="BENCH",
        suggested_slot="IR",
        reason="Player is ruled OUT; move to IR to free bench spot.",
        injury_status="OUT",
    )
    lineup = LineupSolution(
        team_id="t1",
        optimal_starters=[p1],
        optimal_bench=[p2],
        current_projected_total=22.0,
        optimal_projected_total=22.0,
        projected_delta=0.0,
        start_recommendations=["Start Lamar Jackson (22.0 pts)"],
        sit_recommendations=[],
        ir_warnings=[ir_adj],
    )
    assert lineup.optimal_projected_total == 22.0
    assert len(lineup.ir_warnings) == 1
    assert lineup.ir_warnings[0].suggested_slot == "IR"

    add_drop = AddDropRecommendation(
        add_player=p1,
        drop_player=p2,
        position="QB",
        net_projected_gain=8.0,
        matchup_advantage_3wk=3.5,
        reason="Significant projected upgrade",
    )
    dst_stream = StreamingOption(
        player=Player(id="dst1", name="Ravens D/ST", position=Position.DST, team="BAL", projected_points=9.0),
        position="D/ST",
        week_matchup="vs NYG",
        opponent_rank=30,
        projected_points=9.0,
        tier=1,
        reason="Top streaming defense facing 30th ranked offense",
    )
    waiver = WaiverAnalysis(
        team_id="t1",
        positional_weaknesses={"QB": 4.2},
        top_add_drop_pairs=[add_drop],
        dst_streaming=[dst_stream],
        kicker_streaming=[],
    )
    assert waiver.team_id == "t1"
    assert len(waiver.top_add_drop_pairs) == 1
    assert len(waiver.dst_streaming) == 1


def test_session_profile_model_with_one_time_claim() -> None:
    """Test LeagueProfile model validation and one-time invite claim fields."""
    profile = LeagueProfile(
        session_id="sess_abc123",
        platform=PlatformType.SLEEPER,
        league_id="987654321",
        league_name="Championship League",
        season_year=2024,
        team_id="team_99",
        team_name="Gridiron Gurus",
        user_draft_slot=4,
        invite_code="CHAMP2024",
        is_claimed=False,
    )
    assert profile.platform == PlatformType.SLEEPER
    assert profile.invite_code == "CHAMP2024"
    assert not profile.is_claimed

    # Claim invite code
    profile.is_claimed = True
    profile.claimed_at = datetime.now(UTC)
    assert profile.is_claimed
    assert profile.claimed_at is not None
