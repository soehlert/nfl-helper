"""Unit and integration tests for cheatsheet dry-run diff engine and QA mode gating."""

from fastapi.testclient import TestClient

from nfl_helper.core.cheatsheet import parse_plain_text_cheatsheet
from nfl_helper.core.cheatsheet_diff import compute_cheatsheet_diff
from nfl_helper.main import app
from tests.fixtures.demo_rosters import get_mock_player_pool

client = TestClient(app)


def test_cheatsheet_diff_top_movers_calculation() -> None:
    """Verify compute_cheatsheet_diff calculates top risers, fallers, and rule deltas without DB writes."""
    base_text = """
    1.1 Christian McCaffrey
    1.2 CeeDee Lamb
    1.3 Bijan Robinson
    1.4 Justin Jefferson

    Rules:
    Rounds 1-2: Target RB or WR
    """
    candidate_text = """
    1.1 Josh Allen
    1.2 Bijan Robinson
    1.3 Christian McCaffrey
    1.4 Malik Nabers

    Rules:
    Round 1: Target QB
    """
    base_ctx = parse_plain_text_cheatsheet(base_text)
    cand_ctx = parse_plain_text_cheatsheet(candidate_text)
    pool = get_mock_player_pool()

    report = compute_cheatsheet_diff(
        active_context=base_ctx,
        candidate_context=cand_ctx,
        player_pool=pool,
        top_n=5,
    )

    assert len(report.top_risers) == 5
    assert all(r.position == "QB" for r in report.top_risers)
    assert all(r.rank_delta > 50 for r in report.top_risers)
    assert len(report.top_fallers) == 5
    assert all(f.rank_delta < 0 for f in report.top_fallers)

    # Rule deltas
    assert "Round 1: Target QB" in report.added_rules
    assert "Rounds 1-2: Target RB or WR" in report.removed_rules


def test_api_cheatsheet_diff_endpoint() -> None:
    """Verify POST /api/cheatsheet/diff returns structured diff without mutating active cheatsheet."""
    cand_text = """
    1.1 Josh Allen
    1.2 Lamar Jackson
    1.3 Brock Bowers
    """
    res = client.post("/api/cheatsheet/diff", json={"text": cand_text})
    assert res.status_code == 200
    data = res.json()
    assert "top_risers" in data
    assert "top_fallers" in data
    assert "added_rules" in data


def test_qa_mode_toggle_and_config_endpoint() -> None:
    """Verify QA mode toggle endpoint and simulation gating."""
    # Check config
    res_cfg = client.get("/api/config")
    assert res_cfg.status_code == 200
    assert "qa_mode" in res_cfg.json()

    # Enable QA mode
    res_on = client.post("/api/admin/qa-mode", json={"enabled": True})
    assert res_on.status_code == 200
    assert res_on.json()["qa_mode"] is True

    # When QA mode is enabled, simulate_cliff works
    res_cliff = client.get("/api/draft/state?simulate_cliff=true")
    assert res_cliff.status_code == 200
    assert len(res_cliff.json()["cliff_warnings"]) > 0

    # Disable QA mode
    res_off = client.post("/api/admin/qa-mode", json={"enabled": False})
    assert res_off.status_code == 200
    assert res_off.json()["qa_mode"] is False

    # When QA mode is disabled, simulate_cliff is ignored
    res_normal = client.get("/api/draft/state?simulate_cliff=true")
    assert res_normal.status_code == 200
    assert len(res_normal.json()["cliff_warnings"]) == 0
