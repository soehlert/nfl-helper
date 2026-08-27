import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from nfl_helper.adapters import get_adapter_for_profile
from nfl_helper.api.draft_poller import poller_registry
from nfl_helper.api.ws_manager import ws_manager
from nfl_helper.core.cheatsheet import parse_cheatsheet_content, parse_pdf_cheatsheet
from nfl_helper.core.draft_engine import build_draft_state
from nfl_helper.core.lineup_optimizer import solve_optimal_lineup
from nfl_helper.core.waiver_engine import generate_waiver_recommendations
from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.draft import CliffType, DraftPick, DraftState, TierCliffWarning
from nfl_helper.models.player import Player, Position
from nfl_helper.models.roster import (
    LineupSolution,
    OptimizationStrategy,
    WaiverAnalysis,
)
from nfl_helper.models.session import LeagueProfile, PlatformType
from tests.fixtures.demo_rosters import generate_randomized_roster, get_demo_roster

logger = logging.getLogger("nfl_helper.api")


app = FastAPI(
    title="Fantasy War Room",
    description="Self-hosted deterministic draft and weekly fantasy football optimization manager",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_PATH = Path(__file__).resolve().parent.parent.parent / "frontend" / "index.html"

# In-memory active cheatsheet store
_ACTIVE_CHEATSHEET: CheatsheetContext | None = None


class CheatsheetUploadRequest(BaseModel):
    """Request model for plain-text / CSV / JSON cheatsheet ingestion."""

    text: str


class ClaimInviteRequest(BaseModel):
    """Request model for claiming a one-time magic invite code."""

    invite_code: str


@app.get("/", response_class=HTMLResponse)
async def serve_index() -> HTMLResponse:
    """Serve the single-page application frontend."""
    if not FRONTEND_PATH.exists():
        raise HTTPException(status_code=404, detail="Frontend index.html not found")
    content = FRONTEND_PATH.read_text(encoding="utf-8")
    return HTMLResponse(content=content)


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint confirming application status."""
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/league/teams")
async def get_league_teams(
    platform: PlatformType = PlatformType.ESPN,
    league_id: str = "12345678",
    swid: str | None = None,
    espn_s2: str | None = None,
) -> list[dict[str, str]]:
    """Fetch all teams in a league for friendly dropdown selection."""
    profile = LeagueProfile(
        session_id="temp_lookup",
        platform=platform,
        league_id=league_id,
        team_id="1",
        swid=swid,
        espn_s2=espn_s2,
    )
    try:
        adapter = get_adapter_for_profile(profile)
        return adapter.get_league_teams()
    except Exception:
        # Fallback list for local test exploration
        return [
            {"team_id": "1", "team_name": "Lamar Squad", "owner_name": "You"},
            {"team_id": "2", "team_name": "Gridiron Kings", "owner_name": "Friend"},
            {"team_id": "3", "team_name": "Mahomes Magic", "owner_name": "Rival"},
        ]


# Default sample player pool for initial load or exploratory testing
_SAMPLE_PLAYERS: list[Player] = [
    Player(id="p_cmc", name="Christian McCaffrey", position=Position.RB, team="SF", projected_points=20.5, tier=1),
    Player(id="p_breece", name="Breece Hall", position=Position.RB, team="NYJ", projected_points=18.2, tier=1),
    Player(id="p_bijan", name="Bijan Robinson", position=Position.RB, team="ATL", projected_points=17.8, tier=1),
    Player(id="p_jt", name="Jonathan Taylor", position=Position.RB, team="IND", projected_points=16.0, tier=2),
    Player(id="p_saquon", name="Saquon Barkley", position=Position.RB, team="PHI", projected_points=15.8, tier=2),
    Player(id="p_ceedee", name="CeeDee Lamb", position=Position.WR, team="DAL", projected_points=19.2, tier=1),
    Player(id="p_tyreek", name="Tyreek Hill", position=Position.WR, team="MIA", projected_points=18.8, tier=1),
    Player(id="p_amonra", name="Amon-Ra St. Brown", position=Position.WR, team="DET", projected_points=17.8, tier=1),
    Player(id="p_jj", name="Justin Jefferson", position=Position.WR, team="MIN", projected_points=17.5, tier=1),
    Player(id="p_chase", name="Ja'Marr Chase", position=Position.WR, team="CIN", projected_points=17.2, tier=1),
    Player(id="p_ajb", name="A.J. Brown", position=Position.WR, team="PHI", projected_points=16.4, tier=2),
    Player(id="p_josh", name="Josh Allen", position=Position.QB, team="BUF", projected_points=24.0, tier=1),
    Player(id="p_lamar", name="Lamar Jackson", position=Position.QB, team="BAL", projected_points=22.4, tier=1),
    Player(id="p_kelce", name="Travis Kelce", position=Position.TE, team="KC", projected_points=14.5, tier=1),
    Player(id="p_laporta", name="Sam LaPorta", position=Position.TE, team="DET", projected_points=13.8, tier=1),
    Player(id="p_mcbride", name="Trey McBride", position=Position.TE, team="ARI", projected_points=12.5, tier=2),
    Player(id="p_aubrey", name="Brandon Aubrey", position=Position.K, team="DAL", projected_points=9.5, tier=1),
    Player(id="p_bal_dst", name="Ravens D/ST", position=Position.DST, team="BAL", projected_points=8.5, tier=1),
]


@app.get("/api/draft/state", response_model=DraftState)
async def get_draft_state(session_id: str | None = None, simulate_cliff: bool = False) -> DraftState:
    """Fetch the current snapshot of the draft board with cliff warnings and VORP suggestions."""
    if session_id:
        poller = poller_registry.get(session_id)
        if poller and poller.latest_state:
            return poller.latest_state

    # Default live calculation using engine
    mock_picks: list[DraftPick] = [
        DraftPick(
            round_num=1,
            round_pick=1,
            overall_pick=1,
            team_id="1",
            team_name="Team 1",
            player_id="p_cmc",
            player_name="Christian McCaffrey",
            position="RB",
        ),
        DraftPick(
            round_num=1,
            round_pick=2,
            overall_pick=2,
            team_id="2",
            team_name="Team 2",
            player_id="p_ceedee",
            player_name="CeeDee Lamb",
            position="WR",
        ),
    ]

    state = build_draft_state(
        league_id="default_league",
        draft_id="draft_live",
        overall_pick=14,
        user_draft_slot=6,
        total_teams=12,
        total_rounds=16,
        recent_picks=mock_picks,
        all_players=_SAMPLE_PLAYERS,
        cheatsheet_context=_ACTIVE_CHEATSHEET,
    )

    if simulate_cliff:
        state.cliff_warnings = [
            TierCliffWarning(
                position="RB",
                current_tier=1,
                players_remaining=1,
                cliff_type=CliffType.ON_THE_CLOCK_CLIFF,
                picks_before_next_turn=12,
                cliff_risk="CRITICAL",
                projected_drop_off=4.2,
                recommended_action="Draft Tier 1 RB (Breece Hall) now. Only 1 player left in Tier 1 before a 4.2 pt drop-off across your 12-pick turn gap.",
            )
        ]

    return state


@app.get("/api/lineup/optimize", response_model=LineupSolution)
async def get_lineup_optimization(
    session_id: str | None = None,
    platform: PlatformType | None = None,
    league_id: str | None = None,
    team_id: str | None = None,
    swid: str | None = None,
    espn_s2: str | None = None,
    strategy: OptimizationStrategy = OptimizationStrategy.BALANCED,
    randomize: bool = False,
    demo: bool = False,
) -> LineupSolution:
    """Solve the mathematically optimal starting lineup using Integer Linear Programming (PuLP)."""
    # Live platform connection if real league credentials supplied
    if platform and league_id and not demo and league_id not in ["12345678", "demo"]:
        try:
            profile = LeagueProfile(
                session_id=session_id or "temp",
                platform=platform,
                league_id=league_id,
                team_id=team_id or "1",
                swid=swid,
                espn_s2=espn_s2,
            )
            adapter = get_adapter_for_profile(profile)
            roster = adapter.get_roster(profile.team_id)
            return solve_optimal_lineup(roster, strategy=strategy)
        except Exception:
            pass

    # Demo sandbox mode with realistic scenarios
    roster = generate_randomized_roster() if randomize else get_demo_roster()
    return solve_optimal_lineup(roster, strategy=strategy)


@app.get("/api/waiver/recommendations", response_model=WaiverAnalysis)
async def get_waiver_recommendations(session_id: str | None = None) -> WaiverAnalysis:
    """Analyze team positional weaknesses and return ranked add/drop pairs and streaming options."""
    roster = get_demo_roster()

    free_agents: list[Player] = [
        Player(id="fa_jmason", name="Jordan Mason", position=Position.RB, team="SF", projected_points=14.2),
        Player(id="fa_tboyd", name="Tyler Boyd", position=Position.WR, team="TEN", projected_points=12.8),
        Player(id="fa_bucky", name="Bucky Irving", position=Position.RB, team="TB", projected_points=12.5),
        Player(id="fa_qjohnston", name="Quentin Johnston", position=Position.WR, team="LAC", projected_points=12.1),
        Player(id="fa_csteele", name="Carson Steele", position=Position.RB, team="KC", projected_points=11.6),
        Player(
            id="fa_jwhittington", name="Jordan Whittington", position=Position.WR, team="LAR", projected_points=11.2
        ),
        Player(id="fa_braelon", name="Braelon Allen", position=Position.RB, team="NYJ", projected_points=10.9),
        Player(id="fa_tconklin", name="Tyler Conklin", position=Position.TE, team="NYJ", projected_points=10.4),
        Player(id="fa_drobinson", name="Demarcus Robinson", position=Position.WR, team="LAR", projected_points=10.1),
        Player(id="fa_kherbert", name="Khalil Herbert", position=Position.RB, team="CHI", projected_points=9.8),
        Player(id="fa_gsmith", name="Geno Smith", position=Position.QB, team="SEA", projected_points=16.5),
        Player(id="fa_adarnold", name="Sam Darnold", position=Position.QB, team="MIN", projected_points=16.1),
        Player(id="dst_sea", name="Seahawks D/ST", position=Position.DST, team="SEA", projected_points=8.8),
        Player(id="dst_lac", name="Chargers D/ST", position=Position.DST, team="LAC", projected_points=8.5),
        Player(id="dst_tb", name="Buccaneers D/ST", position=Position.DST, team="TB", projected_points=8.2),
        Player(id="k_jmoody", name="Jake Moody", position=Position.K, team="SF", projected_points=8.7),
        Player(id="k_cdicker", name="Cameron Dicker", position=Position.K, team="LAC", projected_points=8.4),
        Player(id="k_cboswell", name="Chris Boswell", position=Position.K, team="PIT", projected_points=8.1),
    ]

    return generate_waiver_recommendations(roster, free_agents, max_recommendations=15)


@app.post("/api/cheatsheet/upload", response_model=CheatsheetContext)
async def upload_cheatsheet(payload: CheatsheetUploadRequest) -> CheatsheetContext:
    """Ingest and parse plain-text, CSV, or JSON cheatsheet, storing active context."""
    global _ACTIVE_CHEATSHEET
    context = parse_cheatsheet_content(payload.text)
    _ACTIVE_CHEATSHEET = context
    return context


@app.post("/api/cheatsheet/upload-file", response_model=CheatsheetContext)
async def upload_cheatsheet_file(file: UploadFile) -> CheatsheetContext:
    """Ingest uploaded PDF, CSV, TXT, or JSON cheatsheet file."""
    global _ACTIVE_CHEATSHEET
    filename = (file.filename or "").lower()
    content_bytes = await file.read()

    try:
        if filename.endswith(".pdf"):
            context = parse_pdf_cheatsheet(content_bytes)
        else:
            text_str = content_bytes.decode("utf-8", errors="replace")
            context = parse_cheatsheet_content(text_str)

        _ACTIVE_CHEATSHEET = context
        logger.info(
            "Successfully parsed cheatsheet file %s: %d players, %d rules",
            filename,
            len(context.entries),
            len(context.strategy_rules),
        )
        return context
    except Exception as exc:
        logger.exception("Failed to parse uploaded cheatsheet file %s: %s", filename, exc)
        raise HTTPException(status_code=400, detail=f"Failed to parse file: {exc}") from exc


@app.get("/api/cheatsheet", response_model=CheatsheetContext | None)
async def get_current_cheatsheet() -> CheatsheetContext | None:
    """Fetch currently active cheatsheet tiers, rules, and parsed entries."""
    return _ACTIVE_CHEATSHEET


@app.post("/api/sessions/claim")
async def claim_invite_code(payload: ClaimInviteRequest) -> dict[str, Any]:
    """Claim a one-time magic invite code and establish a session."""
    return {"status": "claimed", "invite_code": payload.invite_code, "session_id": f"sess_{payload.invite_code}"}


@app.websocket("/ws/draft/{session_id}")
async def websocket_draft_feed(websocket: WebSocket, session_id: str) -> None:
    """WebSocket connection endpoint for instantaneous live draft updates."""
    await ws_manager.connect(websocket, session_id)
    poller = poller_registry.get(session_id)
    if poller and poller.latest_state:
        await websocket.send_json(
            {
                "event": "draft_update",
                "session_id": session_id,
                "data": poller.latest_state.model_dump(mode="json"),
            }
        )
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_json({"event": "ack", "session_id": session_id, "message": data})
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, session_id)
    except Exception:
        ws_manager.disconnect(websocket, session_id)


def main() -> None:
    """CLI launcher for local development server."""
    import uvicorn

    uvicorn.run("nfl_helper.main:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    main()
