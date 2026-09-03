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
                res = self._client.get(f"/projections/nfl/regular/{season}")
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
                    "x-fantasy-filter": '{"players":{"filterSlotIds":{"value":[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,23,24]},"limit":400,"sortPercOwned":{"sortAsc":false,"sortPriority":1}}}'
                }
                res = self._client.get(url, headers=headers)
                if res.status_code == 200:
                    for item in res.json().get("players", []):
                        p_info = item.get("player", {})
                        full_name = p_info.get("fullName")
                        pos_id = p_info.get("defaultPositionId")
                        adp_num = p_info.get("ownership", {}).get("averageDraftPosition")
                        if full_name and adp_num is not None and float(adp_num) > 0.0:
                            adp_float = round(float(adp_num), 2)
                            self._espn_adp_db[normalize_player_name(full_name)] = adp_float
                            # For team defenses (pos_id == 16), index by team nickname (e.g. 'texans', 'rams')
                            if pos_id == 16:
                                clean_name = full_name.replace(" D/ST", "").strip().lower()
                                self._espn_adp_db[clean_name] = adp_float
                    _GLOBAL_ESPN_ADP_DB = self._espn_adp_db
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
                fpts = round(max(11.0, 25.0 - 3.5 * math.log(r)), 2)
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
        status_raw = str(raw_meta.get("status") or "").upper()

        if injury_raw in ("PUP",) or "PUP" in status_raw or "PHYSICALLY UNABLE" in status_raw:
            injury_status = InjuryStatus.PUP
        elif (
            injury_raw in ("IR",)
            or "INJURED RESERVE" in status_raw
            or "NON FOOTBALL" in status_raw
            or "NFI" in status_raw
        ):
            injury_status = InjuryStatus.IR
        elif injury_raw in ("OUT", "O") or status_raw == "OUT":
            injury_status = InjuryStatus.OUT
        elif injury_raw in ("DOUBTFUL", "D") or status_raw == "DOUBTFUL":
            injury_status = InjuryStatus.DOUBTFUL
        elif injury_raw in ("QUESTIONABLE", "Q") or status_raw == "QUESTIONABLE":
            injury_status = InjuryStatus.QUESTIONABLE
        elif injury_raw in ("SUSPENDED", "SUSP", "SUS") or "SUSPENDED" in status_raw:
            injury_status = InjuryStatus.SUSPENDED
        else:
            injury_status = InjuryStatus.ACTIVE

        team_str = str(raw_meta.get("team") or "FA").upper()
        bye_week = get_team_bye_week(team_str)
        is_dome = is_dome_stadium(team_str)
        espn_adps = self._ensure_espn_adp_db()
        projections = self._ensure_proj_db()
        player_proj = projections.get(str(player_id), {}) if isinstance(projections, dict) else {}
        sleeper_adp_raw = (
            player_proj.get("adp_ppr")
            or player_proj.get("adp_half_ppr")
            or player_proj.get("adp_std")
            or player_proj.get("adp_dd_ppr")
            or player_proj.get("adp_dd_half_ppr")
            or player_proj.get("adp_dd_std")
        )
        sleeper_adp = float(sleeper_adp_raw) if sleeper_adp_raw is not None and float(sleeper_adp_raw) < 900.0 else None

        normalized_name = normalize_player_name(full_name)
        defense_key = last_name.strip().lower()
        espn_adp = espn_adps.get(normalized_name) or (espn_adps.get(defense_key) if pos_enum == Position.DST else None)

        if sleeper_adp is not None and espn_adp is not None:
            blended_adp = round(0.5 * sleeper_adp + 0.5 * espn_adp, 1)
        elif sleeper_adp is not None:
            blended_adp = round(sleeper_adp, 1)
        elif espn_adp is not None:
            blended_adp = round(espn_adp, 1)
        else:
            search_rank = raw_meta.get("search_rank")
            blended_adp = float(search_rank) if search_rank and str(search_rank).isdigit() else None

        return Player(
            id=str(player_id),
            name=full_name,
            position=pos_enum,
            team=team_str,
            projected_points=fpts,
            injury_status=injury_status,
            is_starter=is_starter,
            adp=blended_adp,
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
        """Fetch draft metadata, order, and live picks from Sleeper for explicit league or draft ID."""
        active_draft = None

        # 1. Direct Draft ID lookup
        res_direct_draft = self._client.get(f"/draft/{self.profile.league_id}")
        if res_direct_draft.status_code == 200 and res_direct_draft.json():
            active_draft = res_direct_draft.json()

        # 2. League Drafts lookup
        if not active_draft:
            res_drafts = self._client.get(f"/league/{self.profile.league_id}/drafts")
            if res_drafts.status_code == 200 and res_drafts.json():
                drafts = res_drafts.json()
                active_draft = next((d for d in drafts if d.get("status") in ("drafting", "pre_draft")), drafts[0])

        if not active_draft:
            return DraftState(league_id=self.profile.league_id)

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
        by_pos = self.get_available_players_by_position(limit=700) if include_player_pool else {}

        user_slot = self.profile.user_draft_slot
        resolved_team_id: str | None = str(self.profile.team_id) if self.profile.team_id else None

        draft_order = active_draft.get("draft_order") or {}
        slot_to_roster = active_draft.get("slot_to_roster_id") or {}
        rosters, users_by_id = self._fetch_rosters_and_users()

        target_owner_id: str | None = None
        target_roster_id: str | None = None

        if self.profile.team_id:
            raw_input = str(self.profile.team_id).strip().lower()

            # 1. Match by username or display_name (e.g. "soehlert")
            for uid, u in users_by_id.items():
                uname = str(u.get("username", "")).lower()
                dname = str(u.get("display_name", "")).lower()
                if raw_input in (uname, dname):
                    target_owner_id = uid
                    for r in rosters:
                        if str(r.get("owner_id", "")) == uid:
                            target_roster_id = str(r.get("roster_id", ""))
                            break
                    break

            # 2. Match by roster_id (e.g. "10")
            if not target_roster_id:
                for r in rosters:
                    if str(r.get("roster_id", "")) == str(self.profile.team_id):
                        target_roster_id = str(r.get("roster_id", ""))
                        target_owner_id = str(r.get("owner_id", ""))
                        break

            # 3. Match by owner_id (e.g. Sleeper user ID in draft_order)
            if not target_roster_id and str(self.profile.team_id) in draft_order:
                target_owner_id = str(self.profile.team_id)
                for r in rosters:
                    if str(r.get("owner_id", "")) == target_owner_id:
                        target_roster_id = str(r.get("roster_id", ""))
                        break

        # Resolve user_slot
        if user_slot is None and isinstance(draft_order, dict):
            if target_owner_id and target_owner_id in draft_order:
                user_slot = int(str(draft_order[target_owner_id]))
            elif target_roster_id and target_roster_id in draft_order:
                user_slot = int(str(draft_order[target_roster_id]))
            elif target_roster_id and slot_to_roster:
                for slot_str, r_num in slot_to_roster.items():
                    if str(r_num) == target_roster_id:
                        user_slot = int(slot_str)
                        break
            elif len(draft_order) == 1:
                user_slot = int(next(iter(draft_order.values())))

        if target_roster_id:
            resolved_team_id = target_roster_id

        return DraftState(
            league_id=self.profile.league_id,
            draft_id=draft_id,
            is_complete=is_complete,
            total_teams=total_teams,
            total_rounds=total_rounds,
            current_pick=min(total_teams * total_rounds, current_pick) if is_complete else current_pick,
            current_round=current_round,
            user_draft_slot=user_slot or 1,
            user_team_id=resolved_team_id,
            recent_picks=picks_list,
            available_players_by_pos=by_pos,
        )

    def get_free_agents(self, limit: int = 700) -> list[Player]:
        """Fetch available free agents from Sleeper sorted by multi-platform consensus ADP with guaranteed positional quotas."""
        players = self._ensure_player_db()
        projections = self._ensure_proj_db()
        espn_adps = self._ensure_espn_adp_db()
        valid_candidates: list[tuple[float, str, dict[str, object]]] = []
        for player_id, player_data in players.items():
            if not isinstance(player_data, dict):
                continue
            position_raw = str(player_data.get("position", "")).upper()
            if position_raw in ("QB", "RB", "FB", "WR", "TE", "K", "DEF", "DST", "D/ST"):
                player_proj = projections.get(str(player_id), {}) if isinstance(projections, dict) else {}
                sleeper_adp_raw = (
                    player_proj.get("adp_ppr")
                    or player_proj.get("adp_half_ppr")
                    or player_proj.get("adp_std")
                    or player_proj.get("adp_dd_ppr")
                    or player_proj.get("adp_dd_half_ppr")
                    or player_proj.get("adp_dd_std")
                )
                sleeper_adp = (
                    float(sleeper_adp_raw) if sleeper_adp_raw is not None and float(sleeper_adp_raw) < 900.0 else None
                )

                first_name = str(player_data.get("first_name", ""))
                last_name = str(player_data.get("last_name", ""))
                full_name = f"{first_name} {last_name}".strip() or str(player_data.get("full_name", ""))
                normalized_name = normalize_player_name(full_name)
                espn_adp = espn_adps.get(normalized_name)

                if sleeper_adp is not None and espn_adp is not None:
                    blended_adp = round(0.5 * sleeper_adp + 0.5 * espn_adp, 1)
                elif sleeper_adp is not None:
                    blended_adp = round(sleeper_adp, 1)
                elif espn_adp is not None:
                    blended_adp = round(espn_adp, 1)
                else:
                    blended_adp = 999.0

                valid_candidates.append((blended_adp, player_id, player_data))

        # Group by canonical position to guarantee depth across all positions (including all 32 D/ST teams)
        candidates_by_pos: dict[str, list[tuple[float, str, dict[str, object]]]] = {}
        for rank_val, player_id, player_data in valid_candidates:
            position_raw = str(player_data.get("position", "")).upper()
            position_key = (
                "D/ST" if position_raw in ("DEF", "DST", "D/ST") else ("RB" if position_raw == "FB" else position_raw)
            )
            candidates_by_pos.setdefault(position_key, []).append((rank_val, player_id, player_data))

        pos_quotas = {"QB": 80, "RB": 200, "WR": 280, "TE": 100, "K": 40, "D/ST": 32}
        mapped_players: list[Player] = []
        for pos_key, candidates in candidates_by_pos.items():
            candidates.sort(key=lambda item: item[0])
            quota = pos_quotas.get(pos_key, 50)
            for pos_rank, (_, player_id, player_data) in enumerate(candidates[:quota], start=1):
                mapped_players.append(self._map_player(player_id, meta_override=player_data, pos_rank=pos_rank))

        mapped_players.sort(key=lambda p: p.adp if p.adp is not None else 999)
        return mapped_players
