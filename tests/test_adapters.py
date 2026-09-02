"""Unit tests for platform adapters (ESPN & Sleeper) with 100% offline mocking."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from nfl_helper.adapters.espn_adapter import ESPNAdapter
from nfl_helper.adapters.sleeper_adapter import SleeperAdapter
from nfl_helper.models.player import InjuryStatus, Position
from nfl_helper.models.session import LeagueProfile, PlatformType


@pytest.fixture
def espn_profile() -> LeagueProfile:
    """Fixture providing a sample ESPN profile."""
    return LeagueProfile(
        session_id="sess_espn_test",
        platform=PlatformType.ESPN,
        league_id="12345678",
        league_name="ESPN Test League",
        season_year=2024,
        team_id="1",
        team_name="Lamar Squad",
        user_draft_slot=3,
        espn_s2="AECb_mock_cookie",
        swid="{MOCK-SWID-1234}",
        invite_code="ESPNTEST",
    )


@pytest.fixture
def sleeper_profile() -> LeagueProfile:
    """Fixture providing a sample Sleeper profile."""
    return LeagueProfile(
        session_id="sess_sleeper_test",
        platform=PlatformType.SLEEPER,
        league_id="9876543210",
        league_name="Sleeper Test League",
        season_year=2024,
        team_id="2",
        team_name="Gridiron Kings",
        user_draft_slot=5,
        invite_code="SLEEPERTEST",
    )


def test_espn_adapter_roster_mapping(espn_profile: LeagueProfile) -> None:
    """Verify ESPN adapter correctly maps ESPN player and team objects into canonical TeamRoster."""
    with patch("nfl_helper.adapters.espn_adapter.League") as mock_league_cls:
        mock_league = MagicMock()
        mock_league_cls.return_value = mock_league

        # Mock ESPN player
        mock_p1 = MagicMock()
        mock_p1.playerId = 3139477
        mock_p1.name = "Patrick Mahomes"
        mock_p1.position = "QB"
        mock_p1.proTeam = "KC"
        mock_p1.projected_total_points = 21.5
        mock_p1.total_points = 19.8
        mock_p1.avg_points = 20.2
        mock_p1.lineupSlot = "QB"
        mock_p1.injured = False
        mock_p1.injuryStatus = "ACTIVE"
        mock_p1.eligibleSlots = ["QB"]

        mock_p2 = MagicMock()
        mock_p2.playerId = 4241457
        mock_p2.name = "Isiah Pacheco"
        mock_p2.position = "RB"
        mock_p2.proTeam = "KC"
        mock_p2.projected_total_points = 14.2
        mock_p2.total_points = 12.0
        mock_p2.avg_points = 13.5
        mock_p2.lineupSlot = "BE"
        mock_p2.injured = True
        mock_p2.injuryStatus = "OUT"
        mock_p2.eligibleSlots = ["RB", "FLEX", "BE", "IR"]

        mock_team = MagicMock()
        mock_team.team_id = 1
        mock_team.team_name = "Lamar Squad"
        mock_team.owners = ["Owner One"]
        mock_team.roster = [mock_p1, mock_p2]

        mock_league.teams = [mock_team]

        adapter = ESPNAdapter(espn_profile)
        roster = adapter.get_roster(team_id="1")

        assert roster.team_id == "1"
        assert roster.team_name == "Lamar Squad"
        assert len(roster.players) == 2
        assert len(roster.starters) == 1
        assert len(roster.bench) == 1
        assert roster.starters[0].name == "Patrick Mahomes"
        assert roster.starters[0].position == Position.QB
        assert roster.bench[0].name == "Isiah Pacheco"
        assert roster.bench[0].injury_status == InjuryStatus.OUT


def test_espn_adapter_draft_state_mapping(espn_profile: LeagueProfile) -> None:
    """Verify ESPN adapter correctly maps draft picks and remaining players into DraftState."""
    with patch("nfl_helper.adapters.espn_adapter.League") as mock_league_cls:
        mock_league = MagicMock()
        mock_league_cls.return_value = mock_league

        # Mock draft picks
        mock_pick = MagicMock()
        mock_pick.round_num = 1
        mock_pick.round_pick = 1
        mock_pick.overall_pick = 1
        mock_pick.team_id = 2
        mock_pick.playerId = 1001
        mock_pick.playerName = "Justin Jefferson"

        mock_league.draft = [mock_pick]
        mock_league.settings.team_count = 12
        mock_league.settings.draft_rounds = 15

        # Mock free agents
        mock_fa = MagicMock()
        mock_fa.playerId = 2001
        mock_fa.name = "CeeDee Lamb"
        mock_fa.position = "WR"
        mock_fa.proTeam = "DAL"
        mock_fa.projected_total_points = 18.0
        mock_fa.injuryStatus = "ACTIVE"
        mock_fa.eligibleSlots = ["WR", "FLEX"]

        mock_league.free_agents.return_value = [mock_fa]

        adapter = ESPNAdapter(espn_profile)
        draft_state = adapter.get_draft_state()

        assert draft_state.total_teams == 12
        assert len(draft_state.recent_picks) == 1
        assert draft_state.recent_picks[0].player_name == "Justin Jefferson"
        assert "WR" in draft_state.available_players_by_pos
        assert draft_state.available_players_by_pos["WR"][0].name == "CeeDee Lamb"


def test_sleeper_adapter_roster_and_draft_mapping(sleeper_profile: LeagueProfile) -> None:
    """Verify Sleeper adapter makes REST calls via mocked transport and parses canonical models."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/league/9876543210/rosters" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "roster_id": 2,
                        "owner_id": "user_123",
                        "players": ["4034", "6794"],
                        "starters": ["4034"],
                        "reserve": ["6794"],
                        "settings": {"wins": 5, "losses": 2},
                    }
                ],
            )
        elif "/league/9876543210/users" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "user_id": "user_123",
                        "display_name": "GridironKing",
                        "metadata": {"team_name": "Gridiron Kings"},
                    }
                ],
            )
        elif "/league/9876543210/drafts" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "draft_id": "draft_999",
                        "status": "drafting",
                        "settings": {"teams": 10, "rounds": 16},
                        "slot_to_roster_id": {"1": 1, "2": 2},
                    }
                ],
            )
        elif "/draft/draft_999/picks" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "round": 1,
                        "draft_slot": 1,
                        "pick_no": 1,
                        "player_id": "4034",
                        "metadata": {
                            "first_name": "Christian",
                            "last_name": "McCaffrey",
                            "position": "RB",
                            "team": "SF",
                        },
                        "roster_id": 1,
                    }
                ],
            )
        elif "/league/9876543210" in url:
            return httpx.Response(
                200,
                json={
                    "league_id": "9876543210",
                    "name": "Sleeper Test League",
                    "total_rosters": 10,
                    "season": "2024",
                    "roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN"],
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(mock_handler)
    client = httpx.Client(transport=transport, base_url="https://api.sleeper.app/v1")

    # Pre-populate sample player database metadata
    player_db = {
        "4034": {
            "player_id": "4034",
            "first_name": "Christian",
            "last_name": "McCaffrey",
            "position": "RB",
            "team": "SF",
            "fantasy_positions": ["RB"],
            "injury_status": None,
        },
        "6794": {
            "player_id": "6794",
            "first_name": "Puka",
            "last_name": "Nacua",
            "position": "WR",
            "team": "LAR",
            "fantasy_positions": ["WR"],
            "injury_status": "IR",
        },
    }

    adapter = SleeperAdapter(sleeper_profile, client=client, player_db=player_db)

    # Test Roster retrieval
    roster = adapter.get_roster(team_id="2")
    assert roster.team_id == "2"
    assert roster.team_name == "Gridiron Kings"
    assert len(roster.players) == 2
    assert len(roster.starters) == 1
    assert roster.starters[0].name == "Christian McCaffrey"
    assert roster.players[1].injury_status == InjuryStatus.IR

    # Test Draft State retrieval
    draft_state = adapter.get_draft_state()
    assert draft_state.total_teams == 10
    assert len(draft_state.recent_picks) == 1
    assert draft_state.recent_picks[0].player_name == "Christian McCaffrey"


def test_espn_invalid_position_raises_error(espn_profile: LeagueProfile) -> None:
    """Verify ESPN adapter raises ValueError on unrecognized position instead of arbitrary fallback."""
    with patch("nfl_helper.adapters.espn_adapter.League") as mock_league_cls:
        mock_league = MagicMock()
        mock_league_cls.return_value = mock_league

        mock_bad_player = MagicMock()
        mock_bad_player.name = "Unknown Pos Player"
        mock_bad_player.position = "INVALID_POS_XYZ"
        mock_team = MagicMock()
        mock_team.team_id = 1
        mock_team.team_name = "Test"
        mock_team.roster = [mock_bad_player]
        mock_league.teams = [mock_team]

        adapter = ESPNAdapter(espn_profile)
        with pytest.raises(ValueError, match="Unrecognized ESPN position"):
            adapter.get_roster(team_id="1")


def test_sleeper_adapter_username_and_display_name_resolution() -> None:
    """Verify SleeperAdapter resolves username/display_name to canonical roster_id and draft slot."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/users" in path:
            return httpx.Response(
                200,
                json=[{"user_id": "uid_soehlert", "username": "soehlert", "display_name": "soehlert"}],
            )
        if "/rosters" in path:
            return httpx.Response(
                200,
                json=[{"roster_id": 10, "owner_id": "uid_soehlert", "starters": []}],
            )
        if "/drafts" in path:
            return httpx.Response(
                200,
                json=[
                    {
                        "draft_id": "d_100",
                        "status": "drafting",
                        "settings": {"teams": 10, "rounds": 15},
                        "draft_order": {"uid_soehlert": 10},
                        "slot_to_roster_id": {"10": 10},
                    }
                ],
            )
        if "/draft/d_100/picks" in path:
            return httpx.Response(
                200,
                json=[
                    {
                        "round": 5,
                        "draft_slot": 10,
                        "pick_no": 50,
                        "roster_id": "10",
                        "player_id": "11564",
                        "metadata": {"first_name": "Drake", "last_name": "Maye", "position": "QB"},
                    }
                ],
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler), base_url="https://api.sleeper.app/v1")
    profile = LeagueProfile(
        session_id="sess_test",
        platform=PlatformType.SLEEPER,
        league_id="league_100",
        team_id="soehlert",  # Passed as username
    )
    adapter = SleeperAdapter(profile, client=client, player_db={})
    draft_state = adapter.get_draft_state(include_player_pool=False)

    assert draft_state.user_team_id == "10"
    assert draft_state.user_draft_slot == 10
    assert len(draft_state.recent_picks) == 1


def test_sleeper_adapter_draft_state_with_no_team_id() -> None:
    """Verify SleeperAdapter handles draft state lookup when team_id is None."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/draft/league_200" in path:
            return httpx.Response(404)
        if "/league/league_200/drafts" in path:
            return httpx.Response(
                200,
                json=[
                    {
                        "draft_id": "d_200",
                        "status": "drafting",
                        "settings": {"teams": 12, "rounds": 16},
                        "draft_order": {},
                    }
                ],
            )
        if "/draft/d_200/picks" in path:
            return httpx.Response(200, json=[])
        if "/league/league_200/rosters" in path:
            return httpx.Response(200, json=[])
        if "/league/league_200/users" in path:
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler), base_url="https://api.sleeper.app/v1")
    profile = LeagueProfile(
        session_id="sess_no_team",
        platform=PlatformType.SLEEPER,
        league_id="league_200",
        team_id=None,
    )
    adapter = SleeperAdapter(profile, client=client, player_db={})
    draft_state = adapter.get_draft_state(include_player_pool=False)

    assert draft_state.league_id == "league_200"
    assert draft_state.draft_id == "d_200"
    assert draft_state.user_team_id is None
    assert draft_state.total_teams == 12
    assert draft_state.total_rounds == 16
