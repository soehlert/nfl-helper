"""Integration tests for FastAPI application endpoints and static UI mounting."""

import io

from fastapi.testclient import TestClient

from nfl_helper.main import app

client = TestClient(app)


def test_health_endpoint() -> None:
    """Verify health check endpoint returns 200 and version."""
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.1.0"


def test_root_serves_html() -> None:
    """Verify root URL serves the single-page frontend application."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Fantasy War Room" in response.text


def test_draft_state_endpoint() -> None:
    """Verify draft state endpoint returns expected schema."""
    response = client.get("/api/draft/state")
    assert response.status_code == 200
    data = response.json()
    assert "league_id" in data
    assert "cliff_warnings" in data
    assert "tiers_by_position" in data


def test_lineup_optimize_endpoint() -> None:
    """Verify lineup optimization endpoint returns expected schema."""
    response = client.get("/api/lineup/optimize")
    assert response.status_code == 200
    data = response.json()
    assert "optimal_starters" in data
    assert "projected_delta" in data
    assert len(data["optimal_starters"]) == 9


def test_lineup_optimize_strategy_modes_and_randomizer() -> None:
    """Verify strategy parameters and randomizer query flags work via API."""
    res_ceil = client.get("/api/lineup/optimize?strategy=CEILING")
    assert res_ceil.status_code == 200
    assert res_ceil.json()["strategy"] == "CEILING"

    res_floor = client.get("/api/lineup/optimize?strategy=FLOOR")
    assert res_floor.status_code == 200
    assert res_floor.json()["strategy"] == "FLOOR"

    res_rand = client.get("/api/lineup/optimize?randomize=true")
    assert res_rand.status_code == 200
    assert len(res_rand.json()["optimal_starters"]) == 9


def test_waiver_recommendations_endpoint() -> None:
    """Verify waiver recommendations endpoint returns expected schema."""
    response = client.get("/api/waiver/recommendations")
    assert response.status_code == 200
    data = response.json()
    assert "positional_weaknesses" in data
    assert "top_add_drop_pairs" in data


def test_cheatsheet_upload_and_get_endpoints() -> None:
    """Verify cheatsheet upload parsing and subsequent retrieval."""
    cheatsheet_text = """
QB
Allen BUF 34.8

RB
Gibbs DET 1.1

Rounds 1-2 - Only RB/WR at least 1 RB
"""
    res = client.post("/api/cheatsheet/upload", json={"text": cheatsheet_text})
    assert res.status_code == 200
    data = res.json()
    assert len(data["entries"]) >= 2
    assert len(data["strategy_rules"]) >= 1

    get_res = client.get("/api/cheatsheet")
    assert get_res.status_code == 200
    get_data = get_res.json()
    assert get_data is not None
    assert len(get_data["entries"]) >= 2


def test_cheatsheet_file_upload_endpoint() -> None:
    """Verify file upload endpoint parsing text/csv content."""
    csv_bytes = b"Player,Position,Team,Tier,ADP,Notes\nBreece Hall,RB,NYJ,1,8.9,Elite\n"
    res = client.post(
        "/api/cheatsheet/upload-file",
        files={"file": ("cheatsheet.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert res.status_code == 200
    data = res.json()
    assert "breece hall" in data["entries"]


def test_get_league_teams_endpoint() -> None:
    """Verify /api/league/teams returns list of teams with IDs and names."""
    res = client.get("/api/league/teams?platform=espn&league_id=12345678")
    assert res.status_code == 200
    teams = res.json()
    assert isinstance(teams, list)
    assert len(teams) >= 1
    assert "team_id" in teams[0]
    assert "team_name" in teams[0]
