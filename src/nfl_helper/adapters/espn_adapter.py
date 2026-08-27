"""ESPN fantasy football league adapter wrapping espn-api."""

from typing import Any

from espn_api.football import League

from nfl_helper.adapters.base import BaseLeagueAdapter
from nfl_helper.models.draft import DraftPick, DraftState
from nfl_helper.models.player import InjuryStatus, Player, Position
from nfl_helper.models.roster import TeamRoster
from nfl_helper.models.session import LeagueProfile


class ESPNAdapter(BaseLeagueAdapter):
    """ESPN Fantasy Football provider adapter."""

    def __init__(self, profile: LeagueProfile) -> None:
        super().__init__(profile)
        self._league: League | None = None

    def _get_league(self) -> League:
        """Instantiate or return cached ESPN League client."""
        if self._league is None:
            self._league = League(
                league_id=int(self.profile.league_id),
                year=self.profile.season_year,
                espn_s2=self.profile.espn_s2,
                swid=self.profile.swid,
            )
        return self._league

    def _map_player(self, espn_player: Any, is_starter: bool = False) -> Player:
        """Convert an espn-api Player object to canonical Player model."""
        pos_str = str(getattr(espn_player, "position", "")).upper()
        try:
            pos_enum = Position(pos_str)
        except ValueError:
            pos_enum = Position.FLEX if "FLEX" in pos_str else Position.WR

        raw_injury = str(getattr(espn_player, "injuryStatus", "ACTIVE")).upper()
        try:
            injury_enum = InjuryStatus(raw_injury)
        except ValueError:
            injury_enum = InjuryStatus.ACTIVE

        proj_pts = float(getattr(espn_player, "projected_total_points", 0.0) or 0.0)
        actual_pts = float(getattr(espn_player, "total_points", 0.0) or 0.0)
        avg_pts = float(getattr(espn_player, "avg_points", 0.0) or 0.0)
        slots = list(getattr(espn_player, "eligibleSlots", []) or [pos_str])

        return Player(
            id=str(getattr(espn_player, "playerId", "0")),
            name=str(getattr(espn_player, "name", "Unknown Player")),
            position=pos_enum,
            team=str(getattr(espn_player, "proTeam", "FA")),
            projected_points=round(proj_pts, 2),
            actual_points=round(actual_pts, 2),
            average_points=round(avg_pts, 2),
            injury_status=injury_enum,
            eligible_slots=slots,
            is_starter=is_starter,
        )

    def get_league_info(self) -> LeagueProfile:
        """Fetch league information and update profile metadata."""
        league = self._get_league()
        if hasattr(league, "settings") and hasattr(league.settings, "name"):
            self.profile.league_name = str(league.settings.name)
        return self.profile

    def get_roster(self, team_id: str) -> TeamRoster:
        """Fetch full team roster and starters for a specific ESPN team ID."""
        league = self._get_league()
        target_team = None
        for team in league.teams:
            if str(team.team_id) == str(team_id):
                target_team = team
                break

        if not target_team:
            return TeamRoster(team_id=team_id, team_name=f"Team {team_id}")

        all_players: list[Player] = []
        starters: list[Player] = []
        bench: list[Player] = []
        ir: list[Player] = []

        for p in getattr(target_team, "roster", []):
            slot = str(getattr(p, "lineupSlot", "BE")).upper()
            is_start = slot not in ["BE", "BENCH", "IR"]
            canon_p = self._map_player(p, is_starter=is_start)
            all_players.append(canon_p)

            if slot in ["IR"]:
                ir.append(canon_p)
            elif is_start:
                starters.append(canon_p)
            else:
                bench.append(canon_p)

        return TeamRoster(
            team_id=str(team_id),
            team_name=str(getattr(target_team, "team_name", f"Team {team_id}")),
            players=all_players,
            starters=starters,
            bench=bench,
            ir=ir,
        )

    def get_draft_state(self) -> DraftState:
        """Fetch current draft board, picks taken, and remaining available players."""
        league = self._get_league()
        raw_draft = getattr(league, "draft", []) or []

        picks_list = [
            DraftPick(
                round_num=int(getattr(pick, "round_num", 1)),
                round_pick=int(getattr(pick, "round_pick", 1)),
                overall_pick=int(getattr(pick, "overall_pick", 1)),
                team_id=str(getattr(pick, "team_id", "")),
                team_name=f"Team {getattr(pick, 'team_id', '')}",
                player_id=str(getattr(pick, "playerId", "")),
                player_name=str(getattr(pick, "playerName", "")),
                position="",
            )
            for pick in raw_draft
        ]

        total_teams = getattr(getattr(league, "settings", None), "team_count", len(league.teams) or 12)
        total_rounds = getattr(getattr(league, "settings", None), "draft_rounds", 16)
        current_pick = len(picks_list) + 1
        current_round = ((current_pick - 1) // total_teams) + 1

        free_agents = self.get_free_agents(limit=150)
        by_pos: dict[str, list[Player]] = {}
        for fa in free_agents:
            pos_key = str(fa.position)
            if pos_key not in by_pos:
                by_pos[pos_key] = []
            by_pos[pos_key].append(fa)

        return DraftState(
            league_id=self.profile.league_id,
            total_teams=total_teams,
            total_rounds=total_rounds,
            current_pick=current_pick,
            current_round=current_round,
            user_draft_slot=self.profile.user_draft_slot,
            recent_picks=picks_list,
            available_players_by_pos=by_pos,
        )

    def get_free_agents(self, limit: int = 100) -> list[Player]:
        """Fetch top available free agent players from ESPN."""
        league = self._get_league()
        try:
            fa_list = league.free_agents(size=limit)
            return [self._map_player(fa) for fa in fa_list]
        except Exception:
            return []
