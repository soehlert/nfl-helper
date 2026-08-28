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
    assert "Craftroom Draftroom" in response.text


def test_static_logo_served() -> None:
    """Verify static asset mount serves logo.png successfully."""
    response = client.get("/static/logo.png")
    assert response.status_code == 200
    assert "image/png" in response.headers["content-type"]


def test_draft_state_endpoint() -> None:
    """Verify draft state endpoint returns expected schema, cliff alerts, and tier rolls."""
    response = client.get("/api/draft/state")
    assert response.status_code == 200
    data = response.json()
    assert "league_id" in data
    assert "cliff_warnings" in data
    assert "tiers_by_position" in data

    # In standard mode (QA mode off), simulation params are ignored for safety
    client.delete("/api/cheatsheet")
    client.post("/api/admin/qa-mode", json={"enabled": False})
    res_std = client.get("/api/draft/state?simulate_cliff=true")
    assert res_std.status_code == 200
    assert len(res_std.json()["cliff_warnings"]) == 0

    # In QA mode, simulation params execute
    client.post("/api/admin/qa-mode", json={"enabled": True})
    res_cliff = client.get("/api/draft/state?simulate_cliff=true")
    assert res_cliff.status_code == 200
    cliff_data = res_cliff.json()
    assert len(cliff_data["cliff_warnings"]) > 0
    assert cliff_data["cliff_warnings"][0]["position"] == "RB"

    # Test simulate_tier_roll query parameter in QA mode
    res_roll = client.get("/api/draft/state?simulate_tier_roll=true")
    assert res_roll.status_code == 200
    roll_data = res_roll.json()
    assert roll_data["tiers_by_position"]["QB"][0]["tier_num"] == 2
    assert roll_data["tiers_by_position"]["RB"][0]["tier_num"] == 2

    # Reset QA mode to off
    client.post("/api/admin/qa-mode", json={"enabled": False})


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

    # Verify DELETE /api/cheatsheet clears active cheatsheet
    del_res = client.delete("/api/cheatsheet")
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "cleared"

    get_cleared = client.get("/api/cheatsheet")
    assert get_cleared.status_code == 200
    assert get_cleared.json() is None

    # Test history endpoint
    hist_res = client.get("/api/cheatsheet/history")
    assert hist_res.status_code == 200
    hist_data = hist_res.json()
    assert len(hist_data) >= 1
    sheet_id = hist_data[0]["id"]

    # Test activate endpoint
    act_res = client.post(f"/api/cheatsheet/{sheet_id}/activate")
    assert act_res.status_code == 200
    assert act_res.json()["status"] == "activated"

    # Test delete all endpoint
    del_all_res = client.delete("/api/cheatsheet/all")
    assert del_all_res.status_code == 200
    assert del_all_res.json()["status"] == "deleted_all"

    hist_empty = client.get("/api/cheatsheet/history")
    assert hist_empty.status_code == 200
    assert len(hist_empty.json()) == 0


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


def test_multi_cheatsheet_layering_and_toggle_endpoints() -> None:
    """Verify multi-cheatsheet layering, independent toggle endpoints, and active consolidation."""
    client.delete("/api/cheatsheet/all")

    # Upload Sheet #1: Sleepers
    res1 = client.post(
        "/api/cheatsheet/upload",
        json={
            "name": "Sleepers",
            "text": "RB\nBucky Irving TB 80.0\n# Target upside handcuffs in rounds 7-9",
            "layer_mode": True,
        },
    )
    assert res1.status_code == 200

    # Upload Sheet #2: Breakouts (layered on top)
    res2 = client.post(
        "/api/cheatsheet/upload",
        json={
            "name": "Breakouts",
            "text": "WR\nMalik Nabers NYG 25.0\n# Draft WR early",
            "layer_mode": True,
        },
    )
    assert res2.status_code == 200

    # Verify GET /api/cheatsheet returns consolidated layers
    get_res = client.get("/api/cheatsheet")
    assert get_res.status_code == 200
    active_data = get_res.json()
    assert "bucky irving" in active_data["entries"]
    assert "malik nabers" in active_data["entries"]
    assert len(active_data["strategy_rules"]) == 2

    # Check history has both sheets active
    hist_res = client.get("/api/cheatsheet/history")
    assert hist_res.status_code == 200
    history = hist_res.json()
    assert len(history) == 2
    assert all(h["is_active"] == 1 for h in history)
    sheet1_id = next(h["id"] for h in history if h["name"] == "Sleepers")

    # Toggle Sheet #1 OFF
    toggle_res = client.post(f"/api/cheatsheet/{sheet1_id}/toggle", json={"active": False})
    assert toggle_res.status_code == 200
    toggled_data = toggle_res.json()
    assert toggled_data["is_active"] is False
    assert toggled_data["active_count"] == 1

    # Verify GET /api/cheatsheet now only contains Sheet #2 (Breakouts)
    get_res_after = client.get("/api/cheatsheet")
    assert get_res_after.status_code == 200
    active_after = get_res_after.json()
    assert "bucky irving" not in active_after["entries"]
    assert "malik nabers" in active_after["entries"]
    assert len(active_after["strategy_rules"]) == 1

    # Toggle Sheet #1 back ON
    toggle_on = client.post(f"/api/cheatsheet/{sheet1_id}/toggle")
    assert toggle_on.status_code == 200
    assert toggle_on.json()["is_active"] is True
    assert toggle_on.json()["active_count"] == 2
