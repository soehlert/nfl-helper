"""Sleeper fantasy football league adapter via official Sleeper REST API."""

from typing import Any

import httpx

from nfl_helper.adapters.base import BaseLeagueAdapter
from nfl_helper.models.draft import DraftPick, DraftState
from nfl_helper.models.player import InjuryStatus, Player, Position
from nfl_helper.models.roster import TeamRoster
from nfl_helper.models.session import LeagueProfile

SLEEPER_API_BASE = "https://api.sleeper.app/v1"


class SleeperAdapter(BaseLeagueAdapter):
    """Sleeper Fantasy Football REST provider adapter."""

    def __init__(
        self,
        profile: LeagueProfile,
        client: httpx.Client | None = None,
        player_db: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(profile)
        self._client = client or httpx.Client(base_url=SLEEPER_API_BASE, timeout=10.0)
        self._player_db = player_db or {}

    def _ensure_player_db(self) -> dict[str, Any]:
        """Fetch and cache Sleeper NFL player database if not yet populated."""
        if not self._player_db:
            try:
                res = self._client.get("/players/nfl")
                if res.status_code == 200:
                    self._player_db = res.json()
            except Exception:
                pass
        return self._player_db

    def _map_player(
        self,
        player_id: str,
        meta_override: dict[str, Any] | None = None,
        is_starter: bool = False,
    ) -> Player:
        """Convert Sleeper player ID and metadata dictionary into canonical Player model."""
        db = self._ensure_player_db()
        raw_meta = meta_override or db.get(str(player_id), {})

        first_name = raw_meta.get("first_name", "")
        last_name = raw_meta.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip() or raw_meta.get("full_name", f"Player {player_id}")

        pos_str = str(raw_meta.get("position", "")).upper()
        try:
            pos_enum = Position(pos_str)
        except ValueError:
            pos_enum = Position.FLEX if "FLEX" in pos_str else Position.WR

        raw_injury = str(raw_meta.get("injury_status", "ACTIVE") or "ACTIVE").upper()
        try:
            injury_enum = InjuryStatus(raw_injury)
        except ValueError:
            injury_enum = InjuryStatus.ACTIVE

        team = str(raw_meta.get("team", "FA") or "FA")
        slots = raw_meta.get("fantasy_positions", [pos_str]) or [pos_str]
        proj_pts = float(raw_meta.get("projected_points", 0.0) or 0.0)

        return Player(
            id=str(player_id),
            name=full_name,
            position=pos_enum,
            team=team,
            projected_points=proj_pts,
            injury_status=injury_enum,
            eligible_slots=slots,
            is_starter=is_starter,
        )

    def get_league_info(self) -> LeagueProfile:
        """Fetch league details and team rosters from Sleeper."""
        res = self._client.get(f"/league/{self.profile.league_id}")
        if res.status_code == 200:
            data = res.json()
            self.profile.league_name = str(data.get("name", self.profile.league_name))
            self.profile.season_year = int(data.get("season", self.profile.season_year))
        return self.profile

    def get_roster(self, team_id: str) -> TeamRoster:
        """Fetch roster, starters, bench, and reserve for a specific Sleeper team/roster ID."""
        res_rosters = self._client.get(f"/league/{self.profile.league_id}/rosters")
        res_users = self._client.get(f"/league/{self.profile.league_id}/users")

        if res_rosters.status_code != 200:
            return TeamRoster(team_id=team_id, team_name=f"Team {team_id}")

        rosters = res_rosters.json()
        users_by_id: dict[str, dict[str, Any]] = {}
        if res_users.status_code == 200:
            users_by_id = {u["user_id"]: u for u in res_users.json() if "user_id" in u}

        target_roster = None
        for r in rosters:
            if str(r.get("roster_id")) == str(team_id):
                target_roster = r
                break

        if not target_roster:
            return TeamRoster(team_id=team_id, team_name=f"Team {team_id}")

        owner_id = str(target_roster.get("owner_id", ""))
        user_meta = users_by_id.get(owner_id, {})
        team_name = user_meta.get("metadata", {}).get("team_name") or user_meta.get("display_name") or f"Team {team_id}"

        all_player_ids = target_roster.get("players", []) or []
        starter_ids = set(target_roster.get("starters", []) or [])
        reserve_ids = set(target_roster.get("reserve", []) or [])

        all_players: list[Player] = []
        starters: list[Player] = []
        bench: list[Player] = []
        ir: list[Player] = []

        for pid in all_player_ids:
            is_start = pid in starter_ids
            canon_p = self._map_player(pid, is_starter=is_start)
            all_players.append(canon_p)

            if pid in reserve_ids:
                ir.append(canon_p)
            elif is_start:
                starters.append(canon_p)
            else:
                bench.append(canon_p)

        return TeamRoster(
            team_id=str(team_id),
            team_name=team_name,
            manager_name=user_meta.get("display_name", ""),
            players=all_players,
            starters=starters,
            bench=bench,
            ir=ir,
        )

    def get_draft_state(self) -> DraftState:
        """Fetch draft metadata, order, and live picks from Sleeper."""
        res_drafts = self._client.get(f"/league/{self.profile.league_id}/drafts")
        if res_drafts.status_code != 200 or not res_drafts.json():
            return DraftState(league_id=self.profile.league_id)

        active_draft = res_drafts.json()[0]
        draft_id = str(active_draft.get("draft_id", ""))

        res_picks = self._client.get(f"/draft/{draft_id}/picks")
        raw_picks = res_picks.json() if res_picks.status_code == 200 else []

        picks_list = [
            DraftPick(
                round_num=int(p.get("round", 1)),
                round_pick=int(p.get("draft_slot", 1)),
                overall_pick=int(p.get("pick_no", 1)),
                team_id=str(p.get("roster_id", "")),
                team_name=f"Team {p.get('roster_id', '')}",
                player_id=str(p.get("player_id", "")),
                player_name=f"{p.get('metadata', {}).get('first_name', '')} {p.get('metadata', {}).get('last_name', '')}".strip()
                or f"Player {p.get('player_id')}",
                position=str(p.get("metadata", {}).get("position", "")),
            )
            for p in raw_picks
        ]

        settings = active_draft.get("settings", {})
        total_teams = int(settings.get("teams", 12))
        total_rounds = int(settings.get("rounds", 16))
        current_pick = len(picks_list) + 1
        current_round = ((current_pick - 1) // total_teams) + 1

        return DraftState(
            league_id=self.profile.league_id,
            draft_id=draft_id,
            total_teams=total_teams,
            total_rounds=total_rounds,
            current_pick=current_pick,
            current_round=current_round,
            user_draft_slot=self.profile.user_draft_slot,
            recent_picks=picks_list,
        )

    def get_free_agents(self, limit: int = 100) -> list[Player]:
        """Fetch available free agents from Sleeper."""
        db = self._ensure_player_db()
        return [self._map_player(pid, meta_override=meta) for pid, meta in list(db.items())[:limit]]
