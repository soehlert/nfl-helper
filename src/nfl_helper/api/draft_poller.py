"""Rate-limited background draft pick poller with pick-diff checks and WebSocket dispatch."""

import asyncio
import logging

from nfl_helper.adapters.base import BaseLeagueAdapter
from nfl_helper.api.ws_manager import ConnectionManager, ws_manager
from nfl_helper.core.draft_engine import build_draft_state
from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.draft import DraftState

logger = logging.getLogger(__name__)


class DraftPoller:
    """Async background worker polling league adapter for pick diffs."""

    def __init__(
        self,
        session_id: str,
        adapter: BaseLeagueAdapter,
        user_slot: int = 1,
        cheatsheet_context: CheatsheetContext | None = None,
        poll_interval: float = 4.5,
        ws_mgr: ConnectionManager = ws_manager,
    ) -> None:
        """Initialize poller state and configuration."""
        self.session_id = session_id
        self.adapter = adapter
        self.user_slot = user_slot
        self.cheatsheet_context = cheatsheet_context
        self.poll_interval = poll_interval
        self.ws_manager = ws_mgr
        self.last_pick_count = -1
        self.latest_state: DraftState | None = None
        self._is_running = False
        self._task: asyncio.Task[None] | None = None

    async def poll_once(self) -> bool:
        """Query adapter, check pick diffs, compute state, and broadcast updates."""
        try:
            adapter_state = self.adapter.get_draft_state()
            current_pick_count = len(adapter_state.recent_picks)

            if current_pick_count == self.last_pick_count and self.latest_state is not None:
                return False

            all_players = self.adapter.get_free_agents(limit=250)
            current_overall = adapter_state.current_pick or (current_pick_count + 1)

            draft_state = build_draft_state(
                league_id=self.adapter.profile.league_id,
                draft_id=adapter_state.draft_id,
                overall_pick=current_overall,
                user_draft_slot=self.user_slot,
                total_teams=adapter_state.total_teams or 12,
                total_rounds=adapter_state.total_rounds or 16,
                recent_picks=adapter_state.recent_picks,
                all_players=all_players,
                cheatsheet_context=self.cheatsheet_context,
            )

            self.latest_state = draft_state
            self.last_pick_count = current_pick_count
            await self.ws_manager.broadcast_draft_state(self.session_id, draft_state)
            logger.info("Draft update broadcast for session '%s' (pick #%d)", self.session_id, current_overall)
            return True
        except Exception as err:
            logger.error("Error during draft polling for session '%s': %s", self.session_id, err)
            return False

    async def _run_loop(self) -> None:
        """Continuous polling loop with error handling and sleep intervals."""
        consecutive_errors = 0
        while self._is_running:
            success = await self.poll_once()
            if success or self.latest_state is not None:
                consecutive_errors = 0
                await asyncio.sleep(self.poll_interval)
            else:
                consecutive_errors += 1
                backoff = min(20.0, self.poll_interval * (1.5**consecutive_errors))
                logger.warning("Polling error encountered; backing off for %.1fs", backoff)
                await asyncio.sleep(backoff)

    def start(self) -> asyncio.Task[None]:
        """Start poller background task."""
        if not self._is_running:
            self._is_running = True
            self._task = asyncio.create_task(self._run_loop())
        return self._task  # type: ignore[return-value]

    def stop(self) -> None:
        """Stop poller background task."""
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None


class DraftPollerRegistry:
    """Registry to manage active draft pollers per session."""

    def __init__(self) -> None:
        """Initialize empty poller registry."""
        self._pollers: dict[str, DraftPoller] = {}

    def get(self, session_id: str) -> DraftPoller | None:
        """Retrieve active poller for a session."""
        return self._pollers.get(session_id)

    def register(self, session_id: str, poller: DraftPoller) -> None:
        """Register and start a poller for a session."""
        if session_id in self._pollers:
            self._pollers[session_id].stop()
        self._pollers[session_id] = poller
        poller.start()

    def remove(self, session_id: str) -> None:
        """Stop and unregister poller for a session."""
        poller = self._pollers.pop(session_id, None)
        if poller:
            poller.stop()


# Global singleton poller registry
poller_registry = DraftPollerRegistry()
