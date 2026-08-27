"""Platform league adapters export."""

from nfl_helper.adapters.base import BaseLeagueAdapter
from nfl_helper.adapters.espn_adapter import ESPNAdapter
from nfl_helper.adapters.sleeper_adapter import SleeperAdapter
from nfl_helper.models.session import LeagueProfile, PlatformType


def get_adapter_for_profile(profile: LeagueProfile) -> BaseLeagueAdapter:
    """Factory returning the configured platform adapter for a given league profile."""
    if profile.platform == PlatformType.SLEEPER:
        return SleeperAdapter(profile)
    return ESPNAdapter(profile)


__all__ = [
    "BaseLeagueAdapter",
    "ESPNAdapter",
    "SleeperAdapter",
    "get_adapter_for_profile",
]
