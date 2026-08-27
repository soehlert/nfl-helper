"""Unit tests for tactical context engine: usage trends, Vegas lines, and weather."""

from nfl_helper.core.game_context import calculate_game_context_adjustments
from nfl_helper.core.usage_analyzer import calculate_usage_adjustments
from nfl_helper.models.player import (
    GameEnvironment,
    InjuryStatus,
    Player,
    PlayerWeeklyUsage,
    Position,
)


def test_snap_share_drop_triggers_role_demotion() -> None:
    """Ensure sudden drop in rolling snap share penalizes projected points."""
    player = Player(
        id="p1",
        name="Zack Moss",
        position=Position.RB,
        team="CIN",
        projected_points=12.0,
        usage=PlayerWeeklyUsage(snap_percentages=[0.65, 0.60, 0.20]),
    )
    delta, reasons = calculate_usage_adjustments(player)
    assert delta < 0
    assert any("Role Demotion" in r for r in reasons)


def test_snap_share_surge_triggers_role_surge() -> None:
    """Ensure jump in snap share awards a projection bonus."""
    player = Player(
        id="p2",
        name="Jordan Mason",
        position=Position.RB,
        team="SF",
        projected_points=10.0,
        usage=PlayerWeeklyUsage(snap_percentages=[0.15, 0.20, 0.70]),
    )
    delta, reasons = calculate_usage_adjustments(player)
    assert delta > 0
    assert any("Role Surge" in r for r in reasons)


def test_route_participation_and_hvt_bonuses() -> None:
    """Verify route participation penalty and goal-line share bonuses."""
    wr_low = Player(
        id="p3",
        name="Low Route WR",
        position=Position.WR,
        team="MIN",
        projected_points=12.0,
        usage=PlayerWeeklyUsage(route_participation_pct=0.45),
    )
    delta_route, reasons_route = calculate_usage_adjustments(wr_low)
    assert delta_route == -2.0
    assert any("Low Route Participation" in r for r in reasons_route)

    rb_hvt = Player(
        id="p4",
        name="Goal Line RB",
        position=Position.RB,
        team="SF",
        projected_points=12.0,
        usage=PlayerWeeklyUsage(goalline_share_pct=0.70),
    )
    delta_hvt, reasons_hvt = calculate_usage_adjustments(rb_hvt)
    assert delta_hvt >= 1.8
    assert any("High-Value Touches" in r for r in reasons_hvt)


def test_decoy_risk_detection_on_limited_practice() -> None:
    """Ensure DNP -> LP practice pattern triggers decoy penalty."""
    star = Player(
        id="p4",
        name="Christian Kirk",
        position=Position.WR,
        team="JAX",
        projected_points=14.0,
        practice_status=["DNP", "DNP", "LP"],
        injury_status=InjuryStatus.QUESTIONABLE,
    )
    delta, reasons = calculate_usage_adjustments(star)
    assert delta < 0
    assert any("Decoy / Snap-Limit Risk" in r for r in reasons)


def test_vegas_shootout_and_slugfest() -> None:
    """Verify Vegas O/U total adjustments."""
    shootout_p = Player(
        id="p5",
        name="Lamar Jackson",
        position=Position.QB,
        team="BAL",
        projected_points=20.0,
        game_context=GameEnvironment(over_under=52.5),
    )
    delta_s, reasons_s = calculate_game_context_adjustments(shootout_p)
    assert delta_s == 2.0
    assert any("Vegas Shootout" in r for r in reasons_s)

    slugfest_p = Player(
        id="p6",
        name="Will Levis",
        position=Position.QB,
        team="TEN",
        projected_points=12.0,
        game_context=GameEnvironment(over_under=36.5),
    )
    delta_slug, reasons_slug = calculate_game_context_adjustments(slugfest_p)
    assert delta_slug == -1.2
    assert any("Defensive Slugfest" in r for r in reasons_slug)


def test_point_spread_game_script_effects() -> None:
    """Verify heavy favorite RB rush boost and heavy underdog QB pass boost."""
    fav_rb = Player(
        id="p7",
        name="CMC",
        position=Position.RB,
        team="SF",
        projected_points=18.0,
        game_context=GameEnvironment(spread=-8.5),
    )
    delta_rb, reasons_rb = calculate_game_context_adjustments(fav_rb)
    assert delta_rb >= 1.5
    assert any("Rush Volume" in r for r in reasons_rb)

    dog_qb = Player(
        id="p8",
        name="Bryce Young",
        position=Position.QB,
        team="CAR",
        projected_points=14.0,
        game_context=GameEnvironment(spread=8.5),
    )
    delta_qb, reasons_qb = calculate_game_context_adjustments(dog_qb)
    assert delta_qb >= 1.0
    assert any("Pass Volume" in r for r in reasons_qb)


def test_dome_climate_bonus_and_wind_penalty() -> None:
    """Verify dome bonuses for Kickers and outdoor wind penalties."""
    kicker = Player(
        id="p9",
        name="Aubrey",
        position=Position.K,
        team="DAL",
        projected_points=9.0,
        game_context=GameEnvironment(is_dome=True),
    )
    delta_k, reasons_k = calculate_game_context_adjustments(kicker)
    assert delta_k == 0.8
    assert any("Indoor Dome Kicker" in r for r in reasons_k)

    windy_wr = Player(
        id="p10",
        name="London",
        position=Position.WR,
        team="ATL",
        projected_points=14.0,
        game_context=GameEnvironment(wind_mph=22.0, is_dome=False),
    )
    delta_w, reasons_w = calculate_game_context_adjustments(windy_wr)
    assert delta_w < 0
    assert any("High Wind Storm" in r for r in reasons_w)


def test_missing_context_graceful_fallback() -> None:
    """Verify that players with no usage or game context return zero delta cleanly."""
    vanilla_p = Player(id="p11", name="Player Plain", position=Position.RB, team="FA", projected_points=10.0)
    u_delta, u_reasons = calculate_usage_adjustments(vanilla_p)
    g_delta, g_reasons = calculate_game_context_adjustments(vanilla_p)
    assert u_delta == 0.0
    assert u_reasons == []
    assert g_delta == 0.0
    assert g_reasons == []
