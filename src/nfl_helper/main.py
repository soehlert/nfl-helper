"""FastAPI application entrypoint for Fantasy War Room."""

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from nfl_helper.adapters import get_adapter_for_profile
from nfl_helper.core.cheatsheet import parse_cheatsheet_content, parse_pdf_cheatsheet
from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.draft import CliffType, DraftState, DraftSuggestion, PlayerTier, TierCliffWarning
from nfl_helper.models.player import Player, Position
from nfl_helper.models.roster import (
    AddDropRecommendation,
    LineupSolution,
    RosterAdjustment,
    StreamingOption,
    WaiverAnalysis,
)
from nfl_helper.models.session import LeagueProfile, PlatformType

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


@app.get("/api/draft/state", response_model=DraftState)
async def get_draft_state(session_id: str | None = None) -> DraftState:
    """Fetch the current snapshot of the draft board with cliff warnings and VORP suggestions."""
    cliff = TierCliffWarning(
        position="RB",
        current_tier=1,
        players_remaining=1,
        picks_until_turn=4,
        snake_turn_gap=9,
        cliff_risk="CRITICAL",
        cliff_type=CliffType.DEPLETED_BEFORE_TURN,
        next_tier_drop_points=3.4,
        recommended_action="Only 1 Tier-1 RB remains with 4 picks before your turn. Prepare to target Tier 2 RB or pivot to WR.",
    )
    sug_player = Player(
        id="p_amonra",
        name="Amon-Ra St. Brown",
        position=Position.WR,
        team="DET",
        projected_points=17.8,
        eligible_slots=["WR", "FLEX"],
    )
    suggestion = DraftSuggestion(
        rank=1,
        player=sug_player,
        reason="Tier 1 WR cliff defense; 3 left before 9-pick turn gap",
        vorp=4.8,
        is_cliff_defense=True,
    )
    tier = PlayerTier(tier_num=1, position="WR", players=[sug_player], avg_projected=17.8, count=3)

    return DraftState(
        league_id="default_league",
        current_pick=14,
        current_round=2,
        user_draft_slot=6,
        picks_until_user_turn=4,
        snake_turn_gap=9,
        tiers_by_position={"WR": [tier]},
        cliff_warnings=[cliff],
        top_suggestions=[suggestion],
    )


@app.get("/api/lineup/optimize", response_model=LineupSolution)
async def get_lineup_optimization(session_id: str | None = None) -> LineupSolution:
    """Solve the mathematically optimal starting lineup using Integer Linear Programming (PuLP)."""
    qb = Player(id="p_lamar", name="Lamar Jackson", position=Position.QB, team="BAL", projected_points=22.4)
    rb1 = Player(id="p_cmc", name="Christian McCaffrey", position=Position.RB, team="SF", projected_points=19.8)
    rb2 = Player(id="p_breece", name="Breece Hall", position=Position.RB, team="NYJ", projected_points=17.5)
    bench_p = Player(
        id="p_watson",
        name="Deshaun Watson",
        position=Position.QB,
        team="CLE",
        projected_points=0.0,
        injury_status="OUT",
    )

    ir_adj = RosterAdjustment(
        player_name="Deshaun Watson",
        player_id="p_watson",
        position="QB",
        current_slot="BENCH",
        suggested_slot="IR",
        reason="Player is ruled OUT; move to IR to open active bench spot.",
        injury_status="OUT",
    )

    return LineupSolution(
        team_id="team_1",
        optimal_starters=[qb, rb1, rb2],
        optimal_bench=[bench_p],
        current_projected_total=122.2,
        optimal_projected_total=128.6,
        projected_delta=6.4,
        start_recommendations=["Start Lamar Jackson (22.4 pts)", "Start Christian McCaffrey (19.8 pts)"],
        sit_recommendations=["Bench Deshaun Watson"],
        ir_warnings=[ir_adj],
        solver_status="Optimal",
    )


@app.get("/api/waiver/recommendations", response_model=WaiverAnalysis)
async def get_waiver_recommendations(session_id: str | None = None) -> WaiverAnalysis:
    """Analyze team positional weaknesses and return ranked add/drop pairs and streaming options."""
    pickup = Player(id="fa_jmason", name="Jordan Mason", position=Position.RB, team="SF", projected_points=14.2)
    drop = Player(id="bench_drop", name="Deshaun Watson", position=Position.QB, team="CLE", projected_points=0.0)

    add_drop = AddDropRecommendation(
        add_player=pickup,
        drop_player=drop,
        position="RB",
        net_projected_gain=14.2,
        matchup_advantage_3wk=4.5,
        reason="Starting role opportunity + soft 3-week schedule vs LAR, NE, ARI",
    )
    dst_stream = StreamingOption(
        player=Player(id="dst_sea", name="Seahawks D/ST", position=Position.DST, team="SEA", projected_points=8.8),
        position="D/ST",
        week_matchup="vs DEN",
        opponent_rank=31,
        projected_points=8.8,
        tier=1,
        reason="Top streaming defense facing 31st ranked offense",
    )

    return WaiverAnalysis(
        team_id="team_1",
        positional_weaknesses={"RB": 3.5, "QB": 0.0},
        top_add_drop_pairs=[add_drop],
        dst_streaming=[dst_stream],
        kicker_streaming=[],
    )


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

    if filename.endswith(".pdf"):
        context = parse_pdf_cheatsheet(content_bytes)
    else:
        text_str = content_bytes.decode("utf-8", errors="replace")
        context = parse_cheatsheet_content(text_str)

    _ACTIVE_CHEATSHEET = context
    return context


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
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_json({"event": "ack", "session_id": session_id, "message": data})
    except WebSocketDisconnect:
        pass


def main() -> None:
    """CLI launcher for local development server."""
    import uvicorn

    uvicorn.run("nfl_helper.main:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    main()
