"""Integration and unit tests for WebSocket connection manager, endpoints, and draft poller."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from nfl_helper.adapters.base import BaseLeagueAdapter
from nfl_helper.api.draft_poller import DraftPoller
from nfl_helper.api.ws_manager import ConnectionManager
from nfl_helper.main import app
from nfl_helper.models.draft import DraftPick, DraftState
from nfl_helper.models.player import Player, Position
from nfl_helper.models.session import LeagueProfile, PlatformType


@pytest.mark.asyncio
async def test_ws_manager_connect_disconnect_and_isolation() -> None:
    """Verify WebSocket room isolation and connection counting."""
    manager = ConnectionManager()

    ws_alpha1 = AsyncMock()
    ws_alpha2 = AsyncMock()
    ws_beta = AsyncMock()

    # Connect to room alpha
    await manager.connect(ws_alpha1, "alpha")
    await manager.connect(ws_alpha2, "alpha")
    assert manager.get_room_client_count("alpha") == 2

    # Connect to room beta
    await manager.connect(ws_beta, "beta")
    assert manager.get_room_client_count("beta") == 1

    # Broadcast to room alpha
    payload_alpha = {"type": "alpha_event", "val": 100}
    await manager.broadcast_json("alpha", payload_alpha)

    ws_alpha1.send_json.assert_called_once_with(payload_alpha)
    ws_alpha2.send_json.assert_called_once_with(payload_alpha)
    ws_beta.send_json.assert_not_called()

    # Disconnect
    manager.disconnect(ws_alpha1, "alpha")
    assert manager.get_room_client_count("alpha") == 1
    manager.disconnect(ws_alpha2, "alpha")
    assert manager.get_room_client_count("alpha") == 0


@pytest.mark.asyncio
async def test_ws_manager_dead_socket_cleanup() -> None:
    """Verify dead sockets that raise exceptions are cleaned up during broadcast."""
    manager = ConnectionManager()
    broken_ws = AsyncMock()
    broken_ws.send_json.side_effect = RuntimeError("Socket closed")
    healthy_ws = AsyncMock()

    await manager.connect(broken_ws, "room1")
    await manager.connect(healthy_ws, "room1")
    assert manager.get_room_client_count("room1") == 2

    # Broadcast should succeed on healthy_ws and discard broken_ws
    await manager.broadcast_json("room1", {"msg": "hello"})
    assert healthy_ws.send_json.called
    assert manager.get_room_client_count("room1") == 1


def test_fastapi_websocket_endpoint() -> None:
    """Verify FastAPI /ws/draft/{session_id} accepts connection and echoes heartbeat."""
    client = TestClient(app)
    with client.websocket_connect("/ws/draft/session_test_123") as ws:
        ws.send_text("heartbeat")
        data = ws.receive_json()
        assert data["event"] == "ack"
        assert data["session_id"] == "session_test_123"
        assert data["message"] == "heartbeat"


@pytest.mark.asyncio
async def test_draft_poller_pick_diff_detection() -> None:
    """Verify poller detects pick diffs and broadcasts updates to WebSocket manager."""
    mock_ws_mgr = AsyncMock(spec=ConnectionManager)

    mock_profile = LeagueProfile(
        session_id="poller_sess",
        platform=PlatformType.SLEEPER,
        league_id="111222",
        team_id="1",
    )
    mock_adapter = MagicMock(spec=BaseLeagueAdapter)
    mock_adapter.profile = mock_profile

    # Initial state with 1 pick
    pick1 = DraftPick(
        round_num=1,
        round_pick=1,
        overall_pick=1,
        team_id="1",
        team_name="T1",
        player_id="p1",
        player_name="Player 1",
        position="RB",
    )
    mock_adapter.get_draft_state.return_value = DraftState(
        league_id="111222",
        draft_id="draft_888",
        current_pick=2,
        recent_picks=[pick1],
        total_teams=12,
        total_rounds=16,
    )
    mock_adapter.get_free_agents.return_value = [
        Player(id="p1", name="Player 1", position=Position.RB, team="SF", projected_points=20.0),
        Player(id="p2", name="Player 2", position=Position.WR, team="DET", projected_points=18.0),
    ]

    poller = DraftPoller(
        session_id="poller_sess",
        adapter=mock_adapter,
        user_slot=1,
        poll_interval=1.0,
        ws_mgr=mock_ws_mgr,
    )

    # First poll triggers initial broadcast
    updated = await poller.poll_once()
    assert updated is True
    assert poller.last_pick_count == 1
    assert mock_ws_mgr.broadcast_draft_state.call_count == 1

    # Second poll with no new picks returns False
    updated_again = await poller.poll_once()
    assert updated_again is False
    assert mock_ws_mgr.broadcast_draft_state.call_count == 1

    # Third poll when a new pick is made
    pick2 = DraftPick(
        round_num=1,
        round_pick=2,
        overall_pick=2,
        team_id="2",
        team_name="T2",
        player_id="p2",
        player_name="Player 2",
        position="WR",
    )
    mock_adapter.get_draft_state.return_value = DraftState(
        league_id="111222",
        draft_id="draft_888",
        current_pick=3,
        recent_picks=[pick1, pick2],
        total_teams=12,
        total_rounds=16,
    )
    updated_third = await poller.poll_once()
    assert updated_third is True
    assert poller.last_pick_count == 2
    assert mock_ws_mgr.broadcast_draft_state.call_count == 2
