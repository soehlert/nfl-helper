"""Sleeper fantasy football league adapter via official Sleeper REST API."""

import math

import httpx

from nfl_helper.adapters.base import BaseLeagueAdapter
from nfl_helper.core.season_utils import get_current_nfl_season_year
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
        player_db: dict[str, dict[str, object]] | None = None,
    ) -> None:
        super().__init__(profile)
        self._client = client or httpx.Client(base_url=SLEEPER_API_BASE, timeout=10.0)
        self._player_db = player_db or {}

    def _ensure_player_db(self) -> dict[str, dict[str, object]]:
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
        meta_override: dict[str, object] | None = None,
        is_starter: bool = False,
    ) -> Player:
        """Convert Sleeper player ID and metadata dictionary into canonical Player model."""
        db = self._ensure_player_db()
        raw_meta = meta_override or db.get(str(player_id), {})

        first_name = str(raw_meta.get("first_name", ""))
        last_name = str(raw_meta.get("last_name", ""))
        full_name = f"{first_name} {last_name}".strip() or str(raw_meta.get("full_name", f"Player {player_id}"))

        raw_pos = str(raw_meta.get("position", "")).upper()
        if not raw_pos:
            raise ValueError(f"Missing position on Sleeper player {player_id} ({full_name})")

        if raw_pos in ("DEF", "DST"):
            pos_str = "D/ST"
        elif raw_pos == "FB":
            pos_str = "RB"
        else:
            pos_str = raw_pos

        try:
            pos_enum = Position(pos_str)
        except ValueError as err:
            raise ValueError(f"Unrecognized Sleeper position '{pos_str}' for player {player_id} ({full_name})") from err

        raw_injury = str(raw_meta.get("injury_status", "ACTIVE") or "ACTIVE").upper()
        try:
            injury_enum = InjuryStatus(raw_injury)
        except ValueError:
            injury_enum = InjuryStatus.ACTIVE

        team = str(raw_meta.get("team", "FA") or "FA")
        raw_slots = raw_meta.get("fantasy_positions")
        slots = list(raw_slots) if isinstance(raw_slots, list) else [pos_str]
        raw_proj = float(str(raw_meta.get("projected_points", 0.0) or 0.0))
        search_rank = raw_meta.get("search_rank")
        adp_val = float(search_rank) if search_rank is not None else None

        # Derive realistic baseline fantasy projection if platform database lacks raw weekly projections
        if raw_proj <= 0.0:
            rank = adp_val if adp_val is not None and adp_val > 0 else 100.0
            if pos_str == "QB":
                proj_pts = max(12.0, 24.5 - 2.8 * math.log(max(1.0, rank * 0.15)))
            elif pos_str == "RB":
                proj_pts = max(6.0, 20.5 - 3.2 * math.log(max(1.0, rank * 0.2)))
            elif pos_str == "WR":
                proj_pts = max(6.0, 19.5 - 3.0 * math.log(max(1.0, rank * 0.2)))
            elif pos_str == "TE":
                proj_pts = max(5.0, 14.5 - 2.5 * math.log(max(1.0, rank * 0.1)))
            elif pos_str in ("K", "D/ST", "DST", "DEF"):
                proj_pts = max(5.0, 9.5 - 0.8 * math.log(max(1.0, rank * 0.05)))
            else:
                proj_pts = 10.0
        else:
            proj_pts = raw_proj

        return Player(
            id=str(player_id),
            name=full_name,
            position=pos_enum,
            team=team,
            projected_points=round(proj_pts, 2),
            adp=adp_val,
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
            raw_season = data.get("season")
            self.profile.season_year = int(raw_season) if raw_season else get_current_nfl_season_year()
        return self.profile

    def get_league_teams(self) -> list[dict[str, str]]:
        """Fetch all team IDs, names, and managers from Sleeper."""
        rosters, users_by_id = self._fetch_rosters_and_users()
        results = []
        for r in rosters:
            roster_id = str(r.get("roster_id", ""))
            owner_id = str(r.get("owner_id", ""))
            user_meta = users_by_id.get(owner_id, {})
            raw_meta = user_meta.get("metadata")
            team_name_meta = raw_meta.get("team_name") if isinstance(raw_meta, dict) else None
            team_name = str(team_name_meta or user_meta.get("display_name") or f"Team {roster_id}")
            owner_name = str(user_meta.get("display_name", ""))
            results.append(
                {
                    "team_id": roster_id,
                    "team_name": team_name,
                    "owner_name": owner_name,
                }
            )
        return results

    def _fetch_rosters_and_users(self) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
        """Fetch raw rosters and users dictionaries from Sleeper REST endpoints."""
        res_rosters = self._client.get(f"/league/{self.profile.league_id}/rosters")
        res_users = self._client.get(f"/league/{self.profile.league_id}/users")

        rosters = res_rosters.json() if res_rosters.status_code == 200 else []
        users_by_id = {}
        if res_users.status_code == 200:
            users_by_id = {str(u.get("user_id")): u for u in res_users.json() if "user_id" in u}
        return rosters, users_by_id

    def _build_team_roster(
        self,
        team_id: str,
        target_roster: dict[str, object],
        user_meta: dict[str, object],
    ) -> TeamRoster:
        """Transform raw Sleeper roster dict into structured TeamRoster model."""
        raw_meta = user_meta.get("metadata")
        team_name_meta = raw_meta.get("team_name") if isinstance(raw_meta, dict) else None
        team_name = str(team_name_meta or user_meta.get("display_name") or f"Team {team_id}")

        raw_players = target_roster.get("players")
        all_player_ids = [str(pid) for pid in raw_players] if isinstance(raw_players, list) else []

        raw_starters = target_roster.get("starters")
        starter_ids = {str(s) for s in raw_starters} if isinstance(raw_starters, list) else set()

        raw_reserve = target_roster.get("reserve")
        reserve_ids = {str(r) for r in raw_reserve} if isinstance(raw_reserve, list) else set()

        all_players: list[Player] = []
        starters: list[Player] = []
        bench: list[Player] = []
        ir: list[Player] = []

        for pid in all_player_ids:
            is_start = pid in starter_ids
            parsed_player = self._map_player(pid, is_starter=is_start)
            all_players.append(parsed_player)

            if pid in reserve_ids:
                ir.append(parsed_player)
            elif is_start:
                starters.append(parsed_player)
            else:
                bench.append(parsed_player)

        return TeamRoster(
            team_id=str(team_id),
            team_name=team_name,
            manager_name=str(user_meta.get("display_name", "")),
            players=all_players,
            starters=starters,
            bench=bench,
            ir=ir,
        )

    def get_roster(self, team_id: str) -> TeamRoster:
        """Fetch roster, starters, bench, and reserve for a specific Sleeper team/roster ID."""
        rosters, users_by_id = self._fetch_rosters_and_users()
        target_roster = next((r for r in rosters if str(r.get("roster_id")) == str(team_id)), None)

        if not target_roster:
            return TeamRoster(team_id=team_id, team_name=f"Team {team_id}")

        owner_id = str(target_roster.get("owner_id", ""))
        user_meta = users_by_id.get(owner_id, {})
        return self._build_team_roster(team_id, target_roster, user_meta)

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

        raw_settings = active_draft.get("settings")
        settings = raw_settings if isinstance(raw_settings, dict) else {}
        total_teams = int(str(settings.get("teams", 12)))
        total_rounds = int(str(settings.get("rounds", 16)))
        current_pick = len(picks_list) + 1
        current_round = ((current_pick - 1) // total_teams) + 1
        by_pos = self.get_available_players_by_position(limit=150)

        return DraftState(
            league_id=self.profile.league_id,
            draft_id=draft_id,
            total_teams=total_teams,
            total_rounds=total_rounds,
            current_pick=current_pick,
            current_round=current_round,
            user_draft_slot=self.profile.user_draft_slot,
            recent_picks=picks_list,
            available_players_by_pos=by_pos,
        )

    def get_free_agents(self, limit: int = 150) -> list[Player]:
        """Fetch available free agents from Sleeper sorted by active search rank."""
        db = self._ensure_player_db()
        valid_candidates: list[tuple[int, str, dict[str, object]]] = []
        for pid, meta in db.items():
            if not isinstance(meta, dict):
                continue
            pos = str(meta.get("position", "")).upper()
            if pos in ("QB", "RB", "FB", "WR", "TE", "K", "DEF", "DST", "D/ST"):
                search_rank = meta.get("search_rank")
                rank_val = int(search_rank) if search_rank is not None else 99999
                valid_candidates.append((rank_val, pid, meta))

        valid_candidates.sort(key=lambda x: x[0])
        return [self._map_player(pid, meta_override=meta) for _, pid, meta in valid_candidates[:limit]]
