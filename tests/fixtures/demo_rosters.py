"""Demo roster test fixture and scenario generator for testing and offline sandbox."""

import random

from nfl_helper.models.player import (
    GameEnvironment,
    InjuryStatus,
    Player,
    PlayerWeeklyUsage,
    Position,
)
from nfl_helper.models.roster import TeamRoster


def get_demo_roster() -> TeamRoster:
    """Return a comprehensive 16-player fantasy roster covering all tactical scenarios."""
    # Starters
    lamar = Player(
        id="demo_lamar",
        name="Lamar Jackson",
        position=Position.QB,
        team="BAL",
        projected_points=22.4,
        is_starter=True,
        game_context=GameEnvironment(opponent="vs NYG", over_under=52.5, spread=-7.5),
    )
    cmc = Player(
        id="demo_cmc",
        name="Christian McCaffrey",
        position=Position.RB,
        team="SF",
        projected_points=19.8,
        is_starter=True,
        game_context=GameEnvironment(opponent="@ LAR", over_under=47.5, spread=-7.5),
        usage=PlayerWeeklyUsage(snap_percentages=[0.85, 0.82, 0.88], goalline_share_pct=0.75),
    )
    breece = Player(
        id="demo_breece",
        name="Breece Hall",
        position=Position.RB,
        team="NYJ",
        projected_points=16.5,
        is_starter=True,
        game_context=GameEnvironment(opponent="vs NE", over_under=44.0, spread=-3.5),
        usage=PlayerWeeklyUsage(snap_percentages=[0.70, 0.72, 0.75], goalline_touches_inside_5=3),
    )
    jj = Player(
        id="demo_jj",
        name="Justin Jefferson",
        position=Position.WR,
        team="MIN",
        projected_points=18.0,
        is_starter=True,
        game_context=GameEnvironment(opponent="@ GB", over_under=46.5, spread=2.5),
        usage=PlayerWeeklyUsage(route_participation_pct=0.92),
    )
    amonra = Player(
        id="demo_amonra",
        name="Amon-Ra St. Brown",
        position=Position.WR,
        team="DET",
        projected_points=16.8,
        is_starter=True,
        game_context=GameEnvironment(opponent="vs CHI", is_dome=True, over_under=50.5),
        usage=PlayerWeeklyUsage(route_participation_pct=0.90),
    )
    mcbride = Player(
        id="demo_mcbride",
        name="Trey McBride",
        position=Position.TE,
        team="ARI",
        projected_points=12.8,
        is_starter=True,
        game_context=GameEnvironment(opponent="@ SEA", over_under=45.0, spread=4.5),
        usage=PlayerWeeklyUsage(route_participation_pct=0.88),
    )
    zack_moss = Player(
        id="demo_moss",
        name="Zack Moss",
        position=Position.RB,
        team="CIN",
        projected_points=11.2,
        is_starter=True,
        game_context=GameEnvironment(opponent="vs WAS", over_under=43.0),
        usage=PlayerWeeklyUsage(snap_percentages=[0.60, 0.55, 0.18]),
    )
    aubrey = Player(
        id="demo_aubrey",
        name="Brandon Aubrey",
        position=Position.K,
        team="DAL",
        projected_points=9.5,
        is_starter=True,
        game_context=GameEnvironment(opponent="vs NYG", is_dome=True),
    )
    ravens_dst = Player(
        id="demo_ravens_dst",
        name="Ravens D/ST",
        position=Position.DST,
        team="BAL",
        projected_points=8.5,
        is_starter=True,
        opponent="NYG",
        game_context=GameEnvironment(opponent="NYG"),
    )

    # Bench & IR Players
    jmason = Player(
        id="demo_jmason",
        name="Jordan Mason",
        position=Position.RB,
        team="SF",
        projected_points=8.0,
        is_starter=False,
        ceiling_points=18.0,
        floor_points=8.5,
        game_context=GameEnvironment(opponent="@ LAR", spread=-7.5),
        usage=PlayerWeeklyUsage(snap_percentages=[0.45, 0.50, 0.65], goalline_touches_inside_5=2),
    )
    pickens = Player(
        id="demo_pickens",
        name="George Pickens",
        position=Position.WR,
        team="PIT",
        projected_points=12.0,
        is_starter=False,
        ceiling_points=24.5,
        floor_points=4.0,
        game_context=GameEnvironment(opponent="vs IND", over_under=42.0),
    )
    boyd = Player(
        id="demo_boyd",
        name="Tyler Boyd",
        position=Position.WR,
        team="TEN",
        projected_points=12.0,
        is_starter=False,
        ceiling_points=14.5,
        floor_points=10.5,
        game_context=GameEnvironment(opponent="vs MIA", over_under=44.0),
    )

    kirk = Player(
        id="demo_kirk",
        name="Christian Kirk",
        position=Position.WR,
        team="JAX",
        projected_points=12.5,
        is_starter=False,
        practice_status=["DNP", "DNP", "LP"],
        injury_status=InjuryStatus.QUESTIONABLE,
        game_context=GameEnvironment(opponent="vs HOU"),
    )
    london = Player(
        id="demo_london",
        name="Drake London",
        position=Position.WR,
        team="ATL",
        projected_points=13.8,
        is_starter=False,
        game_context=GameEnvironment(opponent="@ PHI", wind_mph=24.0, is_dome=False),
    )
    watson = Player(
        id="demo_watson",
        name="Deshaun Watson",
        position=Position.QB,
        team="CLE",
        projected_points=0.0,
        injury_status=InjuryStatus.OUT,
        is_starter=False,
    )
    chubb = Player(
        id="demo_chubb",
        name="Nick Chubb",
        position=Position.RB,
        team="CLE",
        projected_points=10.0,
        injury_status=InjuryStatus.ACTIVE,
        is_starter=False,
    )
    browns_dst = Player(
        id="demo_browns_dst",
        name="Browns D/ST",
        position=Position.DST,
        team="CLE",
        projected_points=7.0,
        is_starter=False,
        opponent="BAL",
        game_context=GameEnvironment(opponent="BAL"),
    )

    starters = [lamar, cmc, breece, jj, amonra, mcbride, zack_moss, aubrey, ravens_dst]
    bench = [jmason, pickens, boyd, kirk, london, watson, browns_dst]
    ir = [chubb]

    return TeamRoster(
        team_id="demo_team",
        team_name="Championship Squad (Demo)",
        players=starters + bench + ir,
        starters=starters,
        bench=bench,
        ir=ir,
    )


def generate_randomized_roster(seed: int | None = None) -> TeamRoster:
    """Generate randomized weekly scenario variations for testing optimizer reactivity."""
    rng = random.Random(seed)
    roster = get_demo_roster()

    for p in roster.players:
        if p.game_context:
            p.game_context.over_under = round(rng.uniform(36.0, 54.0), 1)
            p.game_context.spread = round(rng.uniform(-10.5, 9.5), 1)
            p.game_context.wind_mph = round(rng.uniform(0.0, 26.0), 1)
            p.game_context.is_dome = rng.choice([True, False, False])
    return roster
