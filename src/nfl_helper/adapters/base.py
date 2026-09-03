"""Abstract base adapter defining the league integration contract."""

from abc import ABC, abstractmethod

from nfl_helper.models.draft import DraftState
from nfl_helper.models.player import Player
from nfl_helper.models.roster import TeamRoster
from nfl_helper.models.session import LeagueProfile


class BaseLeagueAdapter(ABC):
    """Abstract interface for platform league adapters (ESPN, Sleeper, etc.)."""

    def __init__(self, profile: LeagueProfile) -> None:
        self.profile = profile

    @abstractmethod
    def get_league_info(self) -> LeagueProfile:
        """Fetch and update metadata for the configured league."""

    @abstractmethod
    def get_league_teams(self) -> list[dict[str, str]]:
        """Fetch list of all teams and owner display names in the league."""

    @abstractmethod
    def get_roster(self, team_id: str) -> TeamRoster:
        """Fetch full team roster and starters for a specific team ID."""

    @abstractmethod
    def get_draft_state(self) -> DraftState:
        """Fetch current draft board, picks made, and draft status."""

    @abstractmethod
    def get_free_agents(self, limit: int = 700) -> list[Player]:
        """Fetch available free agents and waiver targets."""

    def get_available_players_by_position(self, limit: int = 700) -> dict[str, list[Player]]:
        """Fetch and group available free agents by position."""
        free_agents = self.get_free_agents(limit=limit)
        by_position: dict[str, list[Player]] = {}
        for player in free_agents:
            pos_key = str(player.position)
            if pos_key not in by_position:
                by_position[pos_key] = []
            by_position[pos_key].append(player)
        return by_position
