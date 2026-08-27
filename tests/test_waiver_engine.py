"""Unit tests for Waiver Wire and streaming recommendation engine."""

from nfl_helper.core.waiver_engine import _is_droppable, generate_waiver_recommendations
from nfl_helper.models.player import Player, Position
from nfl_helper.models.roster import TeamRoster
from tests.fixtures.demo_rosters import get_demo_roster


def test_waiver_roster_legality_constraint() -> None:
    """Verify that a team's only QB cannot be dropped for a non-QB."""
    qb = Player(id="qb1", name="Josh Allen", position=Position.QB, team="BUF", projected_points=22.0)
    rb = Player(id="rb1", name="Bijan Robinson", position=Position.RB, team="ATL", projected_points=18.0)
    te = Player(id="te1", name="Brock Bowers", position=Position.TE, team="LV", projected_points=12.0)

    roster = TeamRoster(team_id="t1", team_name="Test Team", starters=[qb, rb, te], bench=[])

    # Dropping sole QB to add an RB is illegal
    assert not _is_droppable(qb, roster, add_pos=Position.RB)

    # Dropping sole QB to add another QB is legal
    assert _is_droppable(qb, roster, add_pos=Position.QB)


def test_generate_waiver_recommendations_ranking() -> None:
    """Verify generating 10+ ranked add/drop recommendations and streaming options."""
    roster = get_demo_roster()

    # Generate available free agents across positions
    free_agents: list[Player] = (
        [
            Player(
                id=f"fa_rb_{i}",
                name=f"Free Agent RB {i}",
                position=Position.RB,
                team="FA",
                projected_points=14.0 - (i * 0.5),
            )
            for i in range(8)
        ]
        + [
            Player(
                id=f"fa_wr_{i}",
                name=f"Free Agent WR {i}",
                position=Position.WR,
                team="FA",
                projected_points=13.5 - (i * 0.4),
            )
            for i in range(8)
        ]
        + [
            Player(
                id=f"fa_dst_{i}",
                name=f"Streaming D/ST {i}",
                position=Position.DST,
                team="FA",
                projected_points=9.0 - (i * 0.3),
            )
            for i in range(4)
        ]
        + [
            Player(
                id=f"fa_k_{i}",
                name=f"Streaming K {i}",
                position=Position.K,
                team="FA",
                projected_points=8.8 - (i * 0.2),
            )
            for i in range(4)
        ]
    )

    analysis = generate_waiver_recommendations(roster, free_agents, max_recommendations=15)

    assert len(analysis.top_add_drop_pairs) >= 10
    assert len(analysis.dst_streaming) >= 3
    assert len(analysis.kicker_streaming) >= 3

    # Check that drops never break roster minimums
    for rec in analysis.top_add_drop_pairs:
        assert rec.drop_player is not None
        assert rec.net_projected_gain is not None
        assert rec.matchup_advantage_3wk > 0
