"""WebSocket connection room manager with session isolation and error recovery."""

import logging
from collections import defaultdict

from fastapi import WebSocket

from nfl_helper.models.draft import DraftState

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages isolated WebSocket client rooms partitioned by session_id."""

    def __init__(self) -> None:
        """Initialize empty room registry."""
        self._rooms: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, websocket: WebSocket, session_id: str) -> None:
        """Accept connection and register websocket to the session room."""
        await websocket.accept()
        self._rooms[session_id].add(websocket)
        logger.info("WebSocket connected to session '%s'. Active in room: %d", session_id, len(self._rooms[session_id]))

    def disconnect(self, websocket: WebSocket, session_id: str) -> None:
        """Remove a websocket connection from its session room."""
        if session_id in self._rooms:
            self._rooms[session_id].discard(websocket)
            if not self._rooms[session_id]:
                del self._rooms[session_id]
        logger.info("WebSocket disconnected from session '%s'", session_id)

    def get_room_client_count(self, session_id: str) -> int:
        """Return the number of active clients in a session room."""
        return len(self._rooms.get(session_id, set()))

    async def broadcast_json(self, session_id: str, data: dict) -> None:
        """Broadcast JSON payload to all active clients in a specific session room."""
        if session_id not in self._rooms:
            return

        dead_sockets: list[WebSocket] = []
        for ws in list(self._rooms[session_id]):
            try:
                await ws.send_json(data)
            except Exception as e:
                logger.warning("Error broadcasting to socket in session '%s': %s", session_id, e)
                dead_sockets.append(ws)

        for dead_ws in dead_sockets:
            self.disconnect(dead_ws, session_id)

    async def broadcast_draft_state(self, session_id: str, draft_state: DraftState) -> None:
        """Broadcast updated DraftState snapshot to the session room."""
        payload = {
            "event": "draft_update",
            "session_id": session_id,
            "data": draft_state.model_dump(mode="json"),
        }
        await self.broadcast_json(session_id, payload)


# Global singleton instance
ws_manager = ConnectionManager()
