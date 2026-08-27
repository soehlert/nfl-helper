"""Deterministic live draft engine with snake lookahead, VORP, and cliff defense."""

import math

from nfl_helper.core.tier_calculator import cluster_position_tiers, detect_tier_cliffs
from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.draft import DraftPick, DraftState, DraftSuggestion, PlayerTier, TierCliffWarning
from nfl_helper.models.player import Player

# Positional starter depth multipliers for baseline VORP calculation
_STARTER_DEPTH: dict[str, float] = {
    "QB": 1.0,
    "RB": 2.5,
    "WR": 3.5,
    "TE": 1.2,
    "K": 1.0,
    "D/ST": 1.0,
}


def calculate_snake_pick_owner(overall_pick: int, total_teams: int) -> int:
    """Return the 1-indexed draft slot owner for an overall pick number."""
    if overall_pick < 1 or total_teams < 1:
        raise ValueError("Pick number and team count must be positive integers")
    round_num = (overall_pick - 1) // total_teams + 1
    round_pick = (overall_pick - 1) % total_teams + 1
    return round_pick if round_num % 2 == 1 else (total_teams - round_pick + 1)


def calculate_user_draft_schedule(user_slot: int, total_teams: int, total_rounds: int = 16) -> list[int]:
    """Pre-compute the list of overall pick numbers for a given draft slot."""
    if user_slot < 1 or user_slot > total_teams:
        raise ValueError(f"User slot {user_slot} is out of bounds for {total_teams} teams")
    schedule: list[int] = []
    for r in range(1, total_rounds + 1):
        round_pick = user_slot if r % 2 == 1 else (total_teams - user_slot + 1)
        schedule.append((r - 1) * total_teams + round_pick)
    return schedule


def calculate_lookahead(
    overall_pick: int, user_slot: int, total_teams: int, total_rounds: int = 16
) -> tuple[int, int, bool]:
    """Compute picks until user turn, subsequent turn gap, and on-the-clock status."""
    schedule = calculate_user_draft_schedule(user_slot, total_teams, total_rounds)
    if overall_pick in schedule:
        is_on_the_clock = True
        picks_until_turn = 0
        idx = schedule.index(overall_pick)
        subsequent_pick = schedule[idx + 1] if idx + 1 < len(schedule) else schedule[-1] + total_teams
        turn_gap = max(0, subsequent_pick - overall_pick - 1)
        return picks_until_turn, turn_gap, is_on_the_clock

    is_on_the_clock = False
    future_picks = [p for p in schedule if p > overall_pick]
    if not future_picks:
        return 0, 0, False

    next_pick = future_picks[0]
    picks_until_turn = next_pick - overall_pick
    subsequent_pick = future_picks[1] if len(future_picks) > 1 else next_pick + total_teams
    turn_gap = max(0, subsequent_pick - next_pick - 1)
    return picks_until_turn, turn_gap, is_on_the_clock


def calculate_vorp_baselines(all_players: list[Player], total_teams: int) -> dict[str, float]:
    """Compute and return positional replacement baseline scores."""
    baselines: dict[str, float] = {}
    by_pos: dict[str, list[Player]] = {}
    for p in all_players:
        by_pos.setdefault(str(p.position), []).append(p)

    for pos, multiplier in _STARTER_DEPTH.items():
        players = sorted(by_pos.get(pos, []), key=lambda x: x.projected_points, reverse=True)
        if not players:
            baselines[pos] = 0.0
            continue
        baseline_idx = min(len(players) - 1, max(0, math.ceil(total_teams * multiplier) - 1))
        baselines[pos] = round(players[baseline_idx].projected_points, 2)
    return baselines


def calculate_vorp(available_players: list[Player], baselines: dict[str, float]) -> dict[str, float]:
    """Calculate VORP score for each available player against cached baselines."""
    vorp_map: dict[str, float] = {}
    for p in available_players:
        base = baselines.get(str(p.position), 0.0)
        vorp_map[p.id] = round(max(0.0, p.projected_points - base), 2)
    return vorp_map


def _build_suggestion_reason(
    player: Player,
    vorp: float,
    cliff: TierCliffWarning | None,
    adp_delta: float,
) -> str:
    """Generate concise deterministic justification for draft recommendation."""
    reasons: list[str] = []
    if cliff:
        reasons.append(f"Tier {cliff.current_tier} Scarcity ({cliff.players_remaining} left)")
    if adp_delta >= 3.0:
        reasons.append(f"Value Steal (+{round(adp_delta, 1)} past ADP)")
    elif vorp > 0:
        reasons.append(f"+{vorp:.1f} VORP over baseline")
    else:
        reasons.append(f"Top {player.position} ({player.projected_points:.1f} pts)")
    return " • ".join(reasons)


def generate_draft_suggestions(
    available_players: list[Player],
    tiers_by_pos: dict[str, list[PlayerTier]],
    cliff_warnings: list[TierCliffWarning],
    baselines: dict[str, float],
    overall_pick: int,
    top_n: int = 5,
) -> list[DraftSuggestion]:
    """Generate ranked tactical draft suggestions balancing VORP, cliffs, and ADP value."""
    vorp_scores = calculate_vorp(available_players, baselines)
    cliff_by_pos = {w.position: w for w in cliff_warnings}

    scored_players: list[tuple[float, Player, float, bool, TierCliffWarning | None, float]] = []

    for p in available_players:
        vorp = vorp_scores.get(p.id, 0.0)
        cliff = cliff_by_pos.get(str(p.position))
        is_cliff_defense = cliff is not None and (cliff.current_tier == (p.cheatsheet_tier or p.tier or 1))

        score = vorp
        if is_cliff_defense:
            score += 3.5 if cliff.cliff_risk == "CRITICAL" else 2.0

        if p.cheatsheet_tier == 1:
            score += 1.5
        elif p.cheatsheet_tier == 2:
            score += 0.8

        adp_delta = 0.0
        if p.cheatsheet_rank:
            adp_delta = overall_pick - p.cheatsheet_rank
            if adp_delta > 0:
                score += min(2.0, adp_delta * 0.1)

        scored_players.append((score, p, vorp, is_cliff_defense, cliff, adp_delta))

    scored_players.sort(key=lambda item: item[0], reverse=True)

    suggestions: list[DraftSuggestion] = []
    for rank, (_, player, vorp, is_cliff, cliff, adp_delta) in enumerate(scored_players[:top_n], start=1):
        reason = _build_suggestion_reason(player, vorp, cliff if is_cliff else None, adp_delta)
        suggestions.append(
            DraftSuggestion(
                rank=rank,
                player=player,
                reason=reason,
                vorp=vorp,
                is_cliff_defense=is_cliff,
            )
        )
    return suggestions


def build_draft_state(
    league_id: str,
    draft_id: str | None,
    overall_pick: int,
    user_draft_slot: int,
    total_teams: int,
    total_rounds: int,
    recent_picks: list[DraftPick],
    all_players: list[Player],
    cheatsheet_context: CheatsheetContext | None = None,
) -> DraftState:
    """Construct full DraftState snapshot with snake lookahead, tiers, cliffs, and suggestions."""
    drafted_ids = {pick.player_id for pick in recent_picks}
    available_players = [p for p in all_players if p.id not in drafted_ids]

    picks_until_turn, turn_gap, on_the_clock = calculate_lookahead(
        overall_pick, user_draft_slot, total_teams, total_rounds
    )

    positions = ["QB", "RB", "WR", "TE", "K", "D/ST"]
    tiers_by_pos: dict[str, list[PlayerTier]] = {}
    avail_by_pos: dict[str, list[Player]] = {}

    for pos in positions:
        pos_avail = [p for p in available_players if p.position == pos]
        avail_by_pos[pos] = pos_avail
        tiers_by_pos[pos] = cluster_position_tiers(pos_avail, pos, cheatsheet_context)

    cliffs = detect_tier_cliffs(tiers_by_pos, picks_until_turn, turn_gap, on_the_clock)
    baselines = calculate_vorp_baselines(all_players, total_teams)
    suggestions = generate_draft_suggestions(available_players, tiers_by_pos, cliffs, baselines, overall_pick, top_n=5)

    current_round = (overall_pick - 1) // total_teams + 1
    is_complete = overall_pick > (total_teams * total_rounds)

    return DraftState(
        league_id=league_id,
        draft_id=draft_id,
        is_complete=is_complete,
        total_rounds=total_rounds,
        total_teams=total_teams,
        current_pick=overall_pick,
        current_round=current_round,
        user_draft_slot=user_draft_slot,
        picks_until_user_turn=picks_until_turn,
        snake_turn_gap=turn_gap,
        is_user_on_the_clock=on_the_clock,
        recent_picks=recent_picks[-10:] if len(recent_picks) > 10 else recent_picks,
        available_players_by_pos=avail_by_pos,
        tiers_by_position=tiers_by_pos,
        cliff_warnings=cliffs,
        top_suggestions=suggestions,
    )
