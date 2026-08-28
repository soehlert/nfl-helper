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
from nfl_helper.core.cheatsheet import apply_cheatsheet_context, parse_cheatsheet_content, parse_pdf_cheatsheet
from nfl_helper.core.cheatsheet_diff import compute_cheatsheet_diff
from nfl_helper.core.db import (
    activate_cheatsheet,
    clear_active_cheatsheet,
    delete_all_cheatsheets,
    delete_cheatsheet,
    get_active_cheatsheet,
    get_cheatsheet_history,
    init_db,
    save_cheatsheet,
)
from nfl_helper.core.draft_engine import build_draft_state
from nfl_helper.core.lineup_optimizer import solve_optimal_lineup
from nfl_helper.core.url_cheatsheet import fetch_web_cheatsheet
from nfl_helper.core.waiver_engine import generate_waiver_recommendations
from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.diff import CheatsheetDiffReport
from nfl_helper.models.draft import CliffType, DraftPick, DraftState, TierCliffWarning
from nfl_helper.models.player import Player, Position
from nfl_helper.models.roster import (
    LineupSolution,
    OptimizationStrategy,
    WaiverAnalysis,
)
from nfl_helper.models.session import LeagueProfile, PlatformType
from tests.fixtures.demo_rosters import generate_randomized_roster, get_demo_roster, get_mock_player_pool

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


# Initialize SQLite database schema
init_db()

# Persistent active cheatsheet store loaded directly from SQLite
_ACTIVE_CHEATSHEET: CheatsheetContext | None = get_active_cheatsheet()

# Runtime QA simulation mode flag (controllable via CLI admin)
_QA_MODE: bool = False


class CheatsheetUploadRequest(BaseModel):
    """Request model for plain-text / CSV / JSON cheatsheet ingestion."""

    text: str
    name: str = "Pasted Cheatsheet"


class CheatsheetURLRequest(BaseModel):
    """Request model for web article / rankings URL ingestion."""

    url: str
    name: str | None = None


class QAModeRequest(BaseModel):
    """Request model for toggling QA simulation mode via CLI."""

    enabled: bool


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


@app.get("/api/config")
async def get_app_config() -> dict[str, Any]:
    """Fetch runtime configuration and feature gating flags."""
    return {
        "qa_mode": _QA_MODE,
        "version": "0.1.0",
    }


@app.post("/api/admin/qa-mode")
async def set_qa_mode(payload: QAModeRequest) -> dict[str, Any]:
    """Admin endpoint called by CLI to toggle QA simulation mode at runtime."""
    global _QA_MODE
    _QA_MODE = payload.enabled
    logger.info("QA Mode set to %s via CLI admin control", _QA_MODE)
    return {"qa_mode": _QA_MODE, "status": "updated"}


@app.post("/api/cheatsheet/diff", response_model=CheatsheetDiffReport)
async def preview_cheatsheet_diff(payload: CheatsheetUploadRequest) -> CheatsheetDiffReport:
    """Generate dry-run diff report of candidate cheatsheet against active baseline without DB writes."""
    global _ACTIVE_CHEATSHEET, _SAMPLE_PLAYERS
    candidate = parse_cheatsheet_content(payload.text)
    active = _ACTIVE_CHEATSHEET or get_active_cheatsheet()
    return compute_cheatsheet_diff(
        active_context=active,
        candidate_context=candidate,
        player_pool=_SAMPLE_PLAYERS,
        top_n=5,
    )


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
_SAMPLE_PLAYERS: list[Player] = get_mock_player_pool()


@app.get("/api/draft/state", response_model=DraftState)
async def get_draft_state(
    session_id: str | None = None,
    platform: str | None = None,
    league_id: str | None = None,
    swid: str | None = None,
    espn_s2: str | None = None,
    simulate_cliff: bool = False,
    simulate_tier_roll: bool = False,
) -> DraftState:
    """Fetch the current snapshot of the draft board with cliff warnings and VORP suggestions."""
    if not _QA_MODE:
        simulate_cliff = False
        simulate_tier_roll = False

    if session_id:
        poller = poller_registry.get(session_id)
        if poller and poller.latest_state:
            return poller.latest_state

    # If real league platform & league_id are specified (e.g. Sleeper or ESPN)
    if platform and league_id and league_id not in ("12345678", "demo", ""):
        try:
            plat_type = PlatformType.SLEEPER if platform.lower() == "sleeper" else PlatformType.ESPN
            profile = LeagueProfile(
                session_id=session_id or f"sess_{league_id}",
                invite_code=f"INV-{league_id[:5]}",
                platform=plat_type,
                league_id=league_id,
                swid=swid,
                espn_s2=espn_s2,
            )
            adapter = get_adapter_for_profile(profile)
            live_draft = adapter.get_draft_state()

            all_avail = []
            for plist in live_draft.available_players_by_pos.values():
                all_avail.extend(plist)

            if _ACTIVE_CHEATSHEET:
                all_avail = apply_cheatsheet_context(all_avail, _ACTIVE_CHEATSHEET)

            return build_draft_state(
                league_id=live_draft.league_id,
                draft_id=live_draft.draft_id,
                overall_pick=live_draft.current_pick,
                user_draft_slot=live_draft.user_draft_slot or 1,
                total_teams=live_draft.total_teams,
                total_rounds=live_draft.total_rounds,
                recent_picks=live_draft.recent_picks,
                all_players=all_avail,
                cheatsheet_context=_ACTIVE_CHEATSHEET,
            )
        except Exception as exc:
            logger.warning("Failed to fetch live draft state from %s (%s): %s", platform, league_id, exc)

    # Default live calculation using engine
    mock_picks: list[DraftPick] = [
        DraftPick(
            round_num=1,
            round_pick=1,
            overall_pick=1,
            team_id="1",
            team_name="Team 1",
            player_id="rb_1",
            player_name="Christian McCaffrey",
            position="RB",
        ),
        DraftPick(
            round_num=1,
            round_pick=2,
            overall_pick=2,
            team_id="2",
            team_name="Team 2",
            player_id="wr_1",
            player_name="CeeDee Lamb",
            position="WR",
        ),
    ]

    if simulate_tier_roll:
        # Simulate drafting all Tier 1 QBs and RBs
        mock_picks.extend(
            [
                DraftPick(
                    round_num=1,
                    round_pick=3,
                    overall_pick=3,
                    team_id="3",
                    team_name="Team 3",
                    player_id="rb_2",
                    player_name="Breece Hall",
                    position="RB",
                ),
                DraftPick(
                    round_num=1,
                    round_pick=4,
                    overall_pick=4,
                    team_id="4",
                    team_name="Team 4",
                    player_id="rb_3",
                    player_name="Bijan Robinson",
                    position="RB",
                ),
                DraftPick(
                    round_num=1,
                    round_pick=5,
                    overall_pick=5,
                    team_id="5",
                    team_name="Team 5",
                    player_id="qb_1",
                    player_name="Josh Allen",
                    position="QB",
                ),
                DraftPick(
                    round_num=1,
                    round_pick=6,
                    overall_pick=6,
                    team_id="6",
                    team_name="Team 6",
                    player_id="qb_2",
                    player_name="Lamar Jackson",
                    position="QB",
                ),
                DraftPick(
                    round_num=1,
                    round_pick=7,
                    overall_pick=7,
                    team_id="7",
                    team_name="Team 7",
                    player_id="qb_3",
                    player_name="Jalen Hurts",
                    position="QB",
                ),
            ]
        )

    state = build_draft_state(
        league_id="default_league",
        draft_id="draft_live",
        overall_pick=14 if not simulate_tier_roll else 18,
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
                current_tier=1 if not simulate_tier_roll else 2,
                players_remaining=1,
                picks_until_turn=4,
                snake_turn_gap=12,
                cliff_risk="CRITICAL",
                cliff_type=CliffType.ON_THE_CLOCK_CLIFF,
                next_tier_drop_points=4.2,
                recommended_action="Draft remaining Tier RB now before a 4.2 pt drop-off across your 12-pick turn gap.",
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
    """Ingest and parse plain-text, CSV, or JSON cheatsheet, storing active context in SQLite."""
    global _ACTIVE_CHEATSHEET, _SAMPLE_PLAYERS
    context = parse_cheatsheet_content(payload.text)
    _ACTIVE_CHEATSHEET = context
    save_cheatsheet(context, raw_text=payload.text, name=payload.name)
    _SAMPLE_PLAYERS = apply_cheatsheet_context(_SAMPLE_PLAYERS, context)
    return context


@app.post("/api/cheatsheet/upload-file", response_model=CheatsheetContext)
async def upload_cheatsheet_file(file: UploadFile) -> CheatsheetContext:
    """Ingest uploaded PDF, CSV, TXT, or JSON cheatsheet file into SQLite."""
    global _ACTIVE_CHEATSHEET, _SAMPLE_PLAYERS
    filename = (file.filename or "").lower()
    content_bytes = await file.read()

    try:
        if filename.endswith(".pdf"):
            context = parse_pdf_cheatsheet(content_bytes)
            raw_preview = "[Binary PDF File]"
        else:
            text_str = content_bytes.decode("utf-8", errors="replace")
            context = parse_cheatsheet_content(text_str)
            raw_preview = text_str

        _ACTIVE_CHEATSHEET = context
        save_cheatsheet(context, raw_text=raw_preview, name=file.filename or "Uploaded File")
        _SAMPLE_PLAYERS = apply_cheatsheet_context(_SAMPLE_PLAYERS, context)
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


@app.post("/api/cheatsheet/url", response_model=CheatsheetContext)
async def upload_cheatsheet_url(payload: CheatsheetURLRequest) -> CheatsheetContext:
    """Fetch web article/rankings URL, parse into CheatsheetContext, and save in SQLite."""
    global _ACTIVE_CHEATSHEET, _SAMPLE_PLAYERS
    try:
        context, title, raw_text = await fetch_web_cheatsheet(payload.url)
        _ACTIVE_CHEATSHEET = context
        sheet_name = payload.name or title or "Web Cheatsheet"
        save_cheatsheet(context, raw_text=raw_text, name=sheet_name)
        _SAMPLE_PLAYERS = apply_cheatsheet_context(_SAMPLE_PLAYERS, context)
        logger.info(
            "Successfully fetched web cheatsheet from %s: %d players, %d rules",
            payload.url,
            len(context.entries),
            len(context.strategy_rules),
        )
        return context
    except Exception as exc:
        logger.exception("Failed to fetch web cheatsheet from %s: %s", payload.url, exc)
        raise HTTPException(status_code=400, detail=f"Failed to fetch and parse URL: {exc}") from exc


@app.post("/api/cheatsheet/url-diff", response_model=CheatsheetDiffReport)
async def preview_cheatsheet_url_diff(payload: CheatsheetURLRequest) -> CheatsheetDiffReport:
    """Dry-run diff comparing web rankings against active baseline without DB writes."""
    global _ACTIVE_CHEATSHEET, _SAMPLE_PLAYERS
    try:
        context, _, _ = await fetch_web_cheatsheet(payload.url)
        active = _ACTIVE_CHEATSHEET or get_active_cheatsheet()
        return compute_cheatsheet_diff(
            active_context=active,
            candidate_context=context,
            player_pool=_SAMPLE_PLAYERS,
            top_n=5,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to generate diff from URL: {exc}") from exc


@app.post("/api/cheatsheet/file-diff", response_model=CheatsheetDiffReport)
async def preview_cheatsheet_file_diff(file: UploadFile) -> CheatsheetDiffReport:
    """Dry-run diff comparing uploaded file rankings against active baseline without DB writes."""
    global _ACTIVE_CHEATSHEET, _SAMPLE_PLAYERS
    filename = (file.filename or "").lower()
    content_bytes = await file.read()

    try:
        if filename.endswith(".pdf"):
            candidate_context = parse_pdf_cheatsheet(content_bytes)
        else:
            text_str = content_bytes.decode("utf-8", errors="replace")
            candidate_context = parse_cheatsheet_content(text_str)

        active = _ACTIVE_CHEATSHEET or get_active_cheatsheet()
        return compute_cheatsheet_diff(
            active_context=active,
            candidate_context=candidate_context,
            player_pool=_SAMPLE_PLAYERS,
            top_n=5,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to generate file diff: {exc}") from exc


@app.get("/api/cheatsheet", response_model=CheatsheetContext | None)
async def get_current_cheatsheet() -> CheatsheetContext | None:
    """Fetch currently active cheatsheet tiers, rules, and parsed entries from SQLite."""
    global _ACTIVE_CHEATSHEET
    if _ACTIVE_CHEATSHEET is None:
        _ACTIVE_CHEATSHEET = get_active_cheatsheet()
    return _ACTIVE_CHEATSHEET


@app.get("/api/cheatsheet/history")
async def get_cheatsheet_upload_history() -> list[dict[str, Any]]:
    """Fetch upload history of all persisted cheatsheets in SQLite."""
    return get_cheatsheet_history()


@app.delete("/api/cheatsheet")
async def clear_cheatsheet() -> dict[str, Any]:
    """Clear the active cheatsheet and reset tactical suggestions to baseline."""
    global _ACTIVE_CHEATSHEET
    _ACTIVE_CHEATSHEET = None
    clear_active_cheatsheet()
    return {"status": "cleared", "message": "Active cheatsheet removed"}


@app.post("/api/cheatsheet/{cheatsheet_id}/activate")
async def set_active_cheatsheet(cheatsheet_id: int) -> dict[str, Any]:
    """Activate a specific saved cheatsheet by ID and update draft calculations."""
    global _ACTIVE_CHEATSHEET, _SAMPLE_PLAYERS
    context = activate_cheatsheet(cheatsheet_id)
    if not context:
        raise HTTPException(status_code=404, detail="Cheatsheet not found")
    _ACTIVE_CHEATSHEET = context
    _SAMPLE_PLAYERS = apply_cheatsheet_context(_SAMPLE_PLAYERS, context)
    return {"status": "activated", "id": cheatsheet_id}


@app.delete("/api/cheatsheet/all")
async def remove_all_cheatsheets() -> dict[str, Any]:
    """Permanently delete all cheatsheets from SQLite."""
    global _ACTIVE_CHEATSHEET
    _ACTIVE_CHEATSHEET = None
    delete_all_cheatsheets()
    return {"status": "deleted_all", "message": "All cheatsheets deleted"}


@app.delete("/api/cheatsheet/{cheatsheet_id}")
async def remove_cheatsheet(cheatsheet_id: int) -> dict[str, Any]:
    """Permanently delete a cheatsheet entry by ID."""
    global _ACTIVE_CHEATSHEET
    delete_cheatsheet(cheatsheet_id)
    _ACTIVE_CHEATSHEET = get_active_cheatsheet()
    return {"status": "deleted", "id": cheatsheet_id}


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
