"""Sleeper fantasy football league adapter via official Sleeper REST API."""

import math

import httpx

from nfl_helper.adapters.base import BaseLeagueAdapter
from nfl_helper.core.name_normalizer import normalize_player_name
from nfl_helper.core.season_utils import (
    get_current_nfl_season_year,
    get_team_bye_week,
    is_dome_stadium,
)
from nfl_helper.models.draft import DraftPick, DraftState
from nfl_helper.models.player import GameEnvironment, InjuryStatus, Player, Position
from nfl_helper.models.roster import TeamRoster
from nfl_helper.models.session import LeagueProfile

SLEEPER_API_BASE = "https://api.sleeper.app/v1"


_GLOBAL_PLAYER_DB: dict[str, dict[str, object]] = {}
_GLOBAL_PROJ_DB: dict[str, dict[str, object]] = {}
_GLOBAL_ESPN_ADP_DB: dict[str, float] = {}
_GLOBAL_ACTIVE_DRAFT: dict[str, dict[str, object]] = {}

_DEFENSE_CONSENSUS_ADPS: dict[str, float] = {
    "LAR": 88.7,
    "HOU": 97.4,
    "SEA": 105.6,
    "DEN": 119.0,
    "PHI": 125.8,
    "BAL": 131.2,
    "PIT": 133.5,
    "NE": 135.0,
    "KC": 137.4,
    "CLE": 139.8,
    "BUF": 142.0,
    "MIN": 144.5,
    "DET": 146.2,
    "GB": 148.0,
    "DAL": 150.5,
    "SF": 152.0,
    "NYJ": 154.0,
    "CHI": 156.0,
    "TB": 158.0,
    "LAC": 160.0,
    "MIA": 162.0,
    "IND": 164.0,
    "ARI": 166.0,
    "LV": 168.0,
    "CIN": 170.0,
    "NO": 172.0,
    "ATL": 174.0,
    "WAS": 176.0,
    "JAX": 178.0,
    "TEN": 180.0,
    "NYG": 182.0,
    "CAR": 184.0,
}


class SleeperAdapter(BaseLeagueAdapter):
    """Sleeper Fantasy Football REST provider adapter."""

    def __init__(
        self,
        profile: LeagueProfile,
        client: httpx.Client | None = None,
        player_db: dict[str, dict[str, object]] | None = None,
        proj_db: dict[str, dict[str, object]] | None = None,
        espn_adp_db: dict[str, float] | None = None,
    ) -> None:
        super().__init__(profile)
        self._client = client or httpx.Client(base_url=SLEEPER_API_BASE, timeout=10.0)
        self._player_db = player_db or _GLOBAL_PLAYER_DB
        self._proj_db = proj_db or _GLOBAL_PROJ_DB
        self._espn_adp_db = espn_adp_db or _GLOBAL_ESPN_ADP_DB

    def _ensure_player_db(self) -> dict[str, dict[str, object]]:
        """Fetch and cache Sleeper NFL player database if not yet populated."""
        global _GLOBAL_PLAYER_DB
        if not self._player_db:
            if _GLOBAL_PLAYER_DB:
                self._player_db = _GLOBAL_PLAYER_DB
                return self._player_db
            try:
                res = self._client.get("/players/nfl")
                if res.status_code == 200:
                    _GLOBAL_PLAYER_DB = res.json()
                    self._player_db = _GLOBAL_PLAYER_DB
            except Exception:
                pass
        return self._player_db

    def _ensure_proj_db(self) -> dict[str, dict[str, object]]:
        """Fetch and cache Sleeper NFL redraft projections and real ADP if not populated."""
        global _GLOBAL_PROJ_DB
        if not self._proj_db:
            if _GLOBAL_PROJ_DB:
                self._proj_db = _GLOBAL_PROJ_DB
                return self._proj_db
            season = self.profile.season_year or get_current_nfl_season_year()
            try:
                res = self._client.get(f"/projections/nfl/regular/{season}/1")
                if res.status_code == 200:
                    _GLOBAL_PROJ_DB = res.json()
                    self._proj_db = _GLOBAL_PROJ_DB
            except Exception:
                pass
        return self._proj_db

    def _ensure_espn_adp_db(self) -> dict[str, float]:
        """Fetch and cache ESPN public redraft consensus ADP mapping by normalized player name."""
        global _GLOBAL_ESPN_ADP_DB
        if not self._espn_adp_db:
            if _GLOBAL_ESPN_ADP_DB:
                self._espn_adp_db = _GLOBAL_ESPN_ADP_DB
                return self._espn_adp_db
            season = self.profile.season_year or get_current_nfl_season_year()
            try:
                url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leaguedefaults/1?view=kona_player_info"
                headers = {
                    "x-fantasy-filter": '{"players":{"filterSlotIds":{"value":[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,23,24]},"limit":250,"sortPercOwned":{"sortAsc":false,"sortPriority":1}}}'
                }
                res = self._client.get(url, headers=headers)
                if res.status_code == 200:
                    for item in res.json().get("players", []):
                        p_info = item.get("player", {})
                        full_name = p_info.get("fullName")
                        adp_num = p_info.get("ownership", {}).get("averageDraftPosition")
                        if full_name and adp_num:
                            _GLOBAL_ESPN_ADP_DB[normalize_player_name(full_name)] = float(adp_num)
                    self._espn_adp_db = _GLOBAL_ESPN_ADP_DB
            except Exception:
                pass
        return self._espn_adp_db

    def _map_player(
        self,
        player_id: str,
        meta_override: dict[str, object] | None = None,
        is_starter: bool = False,
        pos_rank: int | None = None,
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

        pos_enum = (
            Position.DST
            if raw_pos in ("DEF", "DST", "D/ST")
            else (Position.RB if raw_pos == "FB" else Position(raw_pos))
        )

        projs = self._ensure_proj_db()
        proj_meta = projs.get(str(player_id), {})
        stats = proj_meta.get("stats", {}) if isinstance(proj_meta, dict) else {}
        fpts = float(stats.get("pts_ppr", 0.0) or stats.get("pts_half_ppr", 0.0) or stats.get("pts_std", 0.0) or 0.0)

        # Derive realistic baseline fantasy projection differentiated by positional rank
        if fpts <= 0.0:
            r = max(1.0, float(pos_rank) if pos_rank is not None else 40.0)
            if pos_enum == Position.QB:
                fpts = round(max(12.0, 25.5 - 2.5 * math.log(r)), 2)
            elif pos_enum == Position.RB:
                fpts = round(max(6.0, 21.5 - 3.2 * math.log(r)), 2)
            elif pos_enum == Position.WR:
                fpts = round(max(6.0, 20.8 - 2.8 * math.log(r)), 2)
            elif pos_enum == Position.TE:
                fpts = round(max(5.0, 15.2 - 2.4 * math.log(r)), 2)
            elif pos_enum in (Position.K, Position.DST):
                fpts = round(max(5.0, 9.5 - 0.7 * math.log(r)), 2)
            else:
                fpts = 10.0

        injury_raw = str(raw_meta.get("injury_status") or "").upper()
        injury_status = (
            InjuryStatus.QUESTIONABLE
            if injury_raw in ("QUESTIONABLE", "Q")
            else (
                InjuryStatus.DOUBTFUL
                if injury_raw in ("DOUBTFUL", "D")
                else (
                    InjuryStatus.IR
                    if injury_raw == "IR"
                    else (
                        InjuryStatus.OUT
                        if injury_raw in ("OUT", "O")
                        else (InjuryStatus.SUSPENDED if injury_raw in ("SUSPENDED", "SUSP") else InjuryStatus.ACTIVE)
                    )
                )
            )
        )

        team_str = str(raw_meta.get("team") or "FA").upper()
        bye_week = get_team_bye_week(team_str)
        is_dome = is_dome_stadium(team_str)
        espn_adps = self._ensure_espn_adp_db()
        norm_name = normalize_player_name(full_name)
        adp_val = espn_adps.get(norm_name)

        if adp_val is None:
            if pos_enum == Position.DST:
                adp_val = _DEFENSE_CONSENSUS_ADPS.get(team_str)
                if adp_val is None:
                    r = float(pos_rank) if pos_rank is not None else 10.0
                    adp_val = round(88.0 + 8.0 * (r - 1), 1)
            elif pos_enum == Position.K:
                r = float(pos_rank) if pos_rank is not None else 10.0
                adp_val = round(130.0 + 3.0 * (r - 1), 1)
            else:
                raw_adp = raw_meta.get("search_rank") or raw_meta.get("years_exp")
                if raw_adp and str(raw_adp).isdigit():
                    adp_val = float(raw_adp)

        return Player(
            id=str(player_id),
            name=full_name,
            position=pos_enum,
            team=team_str,
            projected_points=fpts,
            injury_status=injury_status,
            is_starter=is_starter,
            adp=adp_val,
            bye_week=bye_week,
            game_context=GameEnvironment(
                is_dome=is_dome,
                stadium_type="DOME" if is_dome else "OUTDOOR",
            ),
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

    def _fetch_rosters_and_users(self) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
        """Fetch rosters and users dictionary for the current league."""
        res_rosters = self._client.get(f"/league/{self.profile.league_id}/rosters")
        rosters = res_rosters.json() if res_rosters.status_code == 200 else []

        res_users = self._client.get(f"/league/{self.profile.league_id}/users")
        users = res_users.json() if res_users.status_code == 200 else []
        users_by_id = {str(u.get("user_id")): u for u in users if isinstance(u, dict) and "user_id" in u}

        return rosters, users_by_id

    def get_league_teams(self) -> list[dict[str, str]]:
        """Fetch all teams in the Sleeper league."""
        rosters, users_by_id = self._fetch_rosters_and_users()
        teams = []
        for r in rosters:
            rid = str(r.get("roster_id", ""))
            oid = str(r.get("owner_id", ""))
            u_meta = users_by_id.get(oid, {})
            disp_name = str(u_meta.get("display_name", "")) or f"Team {rid}"
            teams.append(
                {
                    "team_id": rid,
                    "team_name": disp_name,
                    "owner_name": disp_name,
                }
            )
        return teams

    def _build_team_roster(
        self,
        team_id: str,
        target_roster: dict[str, object],
        user_meta: dict[str, object],
    ) -> TeamRoster:
        """Construct TeamRoster object from raw Sleeper roster dict and user metadata."""
        meta_dict = user_meta.get("metadata") if isinstance(user_meta.get("metadata"), dict) else {}
        team_name = str(meta_dict.get("team_name") or user_meta.get("display_name") or f"Team {team_id}")

        raw_starters = target_roster.get("starters")
        starter_ids = {str(s) for s in raw_starters} if isinstance(raw_starters, list) else set()

        raw_players = target_roster.get("players")
        all_player_ids = [str(p) for p in raw_players] if isinstance(raw_players, list) else []

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

    def get_draft_state(self, include_player_pool: bool = True) -> DraftState:
        """Fetch draft metadata, order, and live picks from Sleeper with smart caching."""
        global _GLOBAL_ACTIVE_DRAFT
        active_draft = _GLOBAL_ACTIVE_DRAFT.get(self.profile.league_id)
        if not active_draft:
            res_drafts = self._client.get(f"/league/{self.profile.league_id}/drafts")
            if res_drafts.status_code != 200 or not res_drafts.json():
                return DraftState(league_id=self.profile.league_id)
            active_draft = res_drafts.json()[0]
            _GLOBAL_ACTIVE_DRAFT[self.profile.league_id] = active_draft

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
        is_complete = (len(picks_list) >= (total_teams * total_rounds)) or (active_draft.get("status") == "complete")
        current_round = min(total_rounds, ((current_pick - 1) // total_teams) + 1)
        by_pos = self.get_available_players_by_position(limit=150) if include_player_pool else {}

        user_slot = self.profile.user_draft_slot
        if user_slot is None:
            draft_order = active_draft.get("draft_order")
            if isinstance(draft_order, dict):
                if self.profile.team_id and str(self.profile.team_id) in draft_order:
                    user_slot = int(str(draft_order[str(self.profile.team_id)]))
                else:
                    rosters, _ = self._fetch_rosters_and_users()
                    for r in rosters:
                        if str(r.get("roster_id")) == str(self.profile.team_id):
                            owner_id = str(r.get("owner_id", ""))
                            if owner_id in draft_order:
                                user_slot = int(str(draft_order[owner_id]))
                                break
                    if user_slot is None and len(draft_order) == 1:
                        user_slot = int(next(iter(draft_order.values())))

        return DraftState(
            league_id=self.profile.league_id,
            draft_id=draft_id,
            is_complete=is_complete,
            total_teams=total_teams,
            total_rounds=total_rounds,
            current_pick=min(total_teams * total_rounds, current_pick) if is_complete else current_pick,
            current_round=current_round,
            user_draft_slot=user_slot or 1,
            recent_picks=picks_list,
            available_players_by_pos=by_pos,
        )

    def get_free_agents(self, limit: int = 250) -> list[Player]:
        """Fetch available free agents from Sleeper sorted by multi-platform consensus ADP with guaranteed positional quotas."""
        db = self._ensure_player_db()
        projs = self._ensure_proj_db()
        espn_adps = self._ensure_espn_adp_db()
        valid_candidates: list[tuple[float, str, dict[str, object]]] = []
        for pid, meta in db.items():
            if not isinstance(meta, dict):
                continue
            pos = str(meta.get("position", "")).upper()
            if pos in ("QB", "RB", "FB", "WR", "TE", "K", "DEF", "DST", "D/ST"):
                p_proj = projs.get(str(pid), {}) if isinstance(projs, dict) else {}
                sleeper_adp = p_proj.get("adp_dd_ppr") or p_proj.get("adp_dd_half_ppr") or p_proj.get("adp_dd_std")
                s_val = float(sleeper_adp) if sleeper_adp is not None and float(sleeper_adp) < 900.0 else None

                first_name = str(meta.get("first_name", ""))
                last_name = str(meta.get("last_name", ""))
                full_name = f"{first_name} {last_name}".strip() or str(meta.get("full_name", ""))
                norm_name = normalize_player_name(full_name)
                e_val = espn_adps.get(norm_name)

                if s_val is not None and e_val is not None:
                    comp_adp = round(0.5 * s_val + 0.5 * e_val, 1)
                elif s_val is not None:
                    comp_adp = round(s_val, 1)
                elif e_val is not None:
                    comp_adp = round(e_val, 1)
                else:
                    comp_adp = 999.0

                valid_candidates.append((comp_adp, pid, meta))

        # Group by canonical position to guarantee depth across all positions (including all 32 D/ST teams)
        by_pos_candidates: dict[str, list[tuple[float, str, dict[str, object]]]] = {}
        for rank_val, pid, meta in valid_candidates:
            raw_pos = str(meta.get("position", "")).upper()
            pos_key = "D/ST" if raw_pos in ("DEF", "DST", "D/ST") else ("RB" if raw_pos == "FB" else raw_pos)
            by_pos_candidates.setdefault(pos_key, []).append((rank_val, pid, meta))

        pos_quotas = {"QB": 32, "RB": 65, "WR": 85, "TE": 32, "K": 20, "D/ST": 32}
        mapped_players: list[Player] = []
        for pos_key, candidates in by_pos_candidates.items():
            candidates.sort(key=lambda x: x[0])
            quota = pos_quotas.get(pos_key, 30)
            for pos_rank, (_, pid, meta) in enumerate(candidates[:quota], start=1):
                mapped_players.append(self._map_player(pid, meta_override=meta, pos_rank=pos_rank))

        mapped_players.sort(key=lambda p: p.adp if p.adp is not None else 999)
        return mapped_players
