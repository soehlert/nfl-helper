"""Deterministic live draft engine with snake lookahead, VORP, and cliff defense."""

import math
import re

from nfl_helper.core.tier_calculator import calculate_tier_drop, cluster_position_tiers, detect_tier_cliffs
from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.draft import DraftPick, DraftState, DraftSuggestion, PlayerTier, TierCliffWarning
from nfl_helper.models.player import Player

# Positional starter depth multipliers for baseline VORP calculation
_STARTER_DEPTH: dict[str, float] = {
    "QB": 1.5,
    "RB": 2.25,
    "WR": 2.75,
    "TE": 1.25,
    "K": 1.0,
    "D/ST": 1.0,
}

# Positional starting demand weights for single-QB / 1-TE format (1 QB vs 5-6 RB/WR)
_POS_DEMAND_WEIGHT: dict[str, float] = {
    "RB": 1.0,
    "WR": 1.0,
    "QB": 0.65,
    "TE": 0.75,
    "K": 0.35,
    "D/ST": 0.35,
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
    """Calculate VORP score for each available player against cached baselines weighted by roster demand."""
    vorp_map: dict[str, float] = {}
    for p in available_players:
        base = baselines.get(str(p.position), 0.0)
        demand_weight = _POS_DEMAND_WEIGHT.get(str(p.position), 1.0)
        raw_vorp = max(0.0, p.projected_points - base)
        vorp_map[p.id] = round(raw_vorp * demand_weight, 2)
    return vorp_map


def _evaluate_strategy_rule_adjustments(
    player: Player,
    cheatsheet_context: CheatsheetContext | None,
    current_round: int,
) -> tuple[float, str | None]:
    """Calculate deterministic score delta and reason note from active strategy rules dynamically."""
    if not cheatsheet_context:
        return 0.0, None

    delta = 0.0
    notes: list[str] = []

    # 1. Evaluate Round Target Constraints (e.g. Rounds 1-2 only RB/WR)
    for rnd_rule in cheatsheet_context.round_targets:
        if current_round in rnd_rule.target_rounds:
            if rnd_rule.allowed_positions and player.position not in rnd_rule.allowed_positions:
                delta -= 5.0
                notes.append(f"Deprioritized: Rd {current_round} targets {', '.join(rnd_rule.allowed_positions)}")
            elif rnd_rule.allowed_positions and player.position in rnd_rule.allowed_positions:
                delta += 1.0

    # 2. Evaluate Positional and Round Target Strategies dynamically
    for pos_rule in cheatsheet_context.positional_strategy:
        if pos_rule.position == str(player.position):
            rule_desc = pos_rule.rule_description
            p_tier = player.cheatsheet_tier or player.tier or 1

            # Check dynamic player name targets in rule (e.g. 'or get Allen in round 4', 'target Mahomes in round 3')
            name_target_match = re.search(
                r"(?:get|target)\s+([A-Za-z]+)\s+in\s+round\s+(\d+)", rule_desc, re.IGNORECASE
            )
            if name_target_match:
                t_name, t_rnd = name_target_match.group(1).lower(), int(name_target_match.group(2))
                if t_name in player.name.lower():
                    if current_round >= t_rnd:
                        delta += 3.0
                        notes.append(f"Strategy Target: {player.name} in Rd {t_rnd}")
                    else:
                        delta -= 4.0
                        notes.append(f"Hold: Target {player.name} in Rd {t_rnd}")
                    continue

            # Check if this rule defines a specific round window (e.g. rounds 3-5)
            if pos_rule.target_rounds:
                if current_round in pos_rule.target_rounds:
                    if (
                        pos_rule.top_n_target and (player.cheatsheet_rank or 99) <= pos_rule.top_n_target
                    ) or p_tier == 1:
                        delta += 2.5
                        notes.append(f"Strategy Target: Top {pos_rule.position} in Rd {current_round}")
                    elif pos_rule.target_tiers and p_tier in pos_rule.target_tiers:
                        delta += 1.5
                        notes.append(f"Strategy Target: Tier {p_tier} {pos_rule.position}")
                elif current_round < min(pos_rule.target_rounds):
                    delta -= 3.5
                    notes.append(f"Deprioritized: {pos_rule.position} targeted in Rd {min(pos_rule.target_rounds)}+")

            # Check if this rule defines specific target tiers (e.g. tiers 3-4 for late-round approach)
            elif pos_rule.target_tiers:
                if current_round <= 3 and min(pos_rule.target_tiers) >= 3:
                    delta -= 4.0
                    notes.append(
                        f"Deprioritized: Late-Round {pos_rule.position} (targeting Tiers {','.join(map(str, pos_rule.target_tiers))})"
                    )
                elif current_round >= 4 and p_tier in pos_rule.target_tiers:
                    delta += 2.0
                    notes.append(f"Strategy Target: Tier {p_tier} {pos_rule.position}")

    final_note = notes[0] if notes else None
    return delta, final_note


def _build_suggestion_reason(
    player: Player,
    vorp: float,
    cliff: TierCliffWarning | None,
    adp_delta: float,
    overall_pick: int,
    top_tier_info: dict[str, tuple[int, int, float]],
    rule_note: str | None = None,
) -> str:
    """Generate concise, factual, and informative multi-part justification for draft recommendation."""
    reasons: list[str] = []
    p_tier = player.cheatsheet_tier or player.tier or 1

    # 1. VORP & Tier tag
    if vorp > 0:
        reasons.append(f"+{vorp:.1f} VORP (Tier {p_tier} {player.position})")
    else:
        reasons.append(f"Tier {p_tier} {player.position} ({player.projected_points:.1f} pts)")

    # 2. Strategy Rule context if active
    if rule_note:
        reasons.append(rule_note)

    # 3. Positional Scarcity / Tier State
    pos_info = top_tier_info.get(str(player.position))
    if pos_info:
        top_num, remaining_in_top, tier_drop = pos_info
        if p_tier == top_num:
            if remaining_in_top <= 2 and tier_drop >= 1.2:
                reasons.append(f"Tier {top_num} Scarcity ({remaining_in_top} left before -{tier_drop:.1f} pt drop)")
            elif remaining_in_top > 2:
                reasons.append(f"{remaining_in_top} Tier {top_num} available")

    # 4. Cliff Defense if active
    if cliff:
        reasons.append(f"Cliff Defense ({cliff.players_remaining} left)")

    # 5. ADP Value
    if player.adp:
        discount = overall_pick - player.adp
        if discount >= 2.0:
            reasons.append(f"+{discount:.1f} pick discount vs {player.adp:.1f} ADP")
        elif discount <= -3.0:
            reasons.append(f"ADP {player.adp:.1f} (-{abs(discount):.1f} reach)")
        else:
            reasons.append(f"ADP {player.adp:.1f}")
    elif player.cheatsheet_notes:
        reasons.append(player.cheatsheet_notes)

    return " • ".join(reasons)


def generate_draft_suggestions(
    available_players: list[Player],
    tiers_by_pos: dict[str, list[PlayerTier]],
    cliff_warnings: list[TierCliffWarning],
    baselines: dict[str, float],
    overall_pick: int,
    top_n: int = 150,
    cheatsheet_context: CheatsheetContext | None = None,
    total_teams: int = 12,
) -> list[DraftSuggestion]:
    """Generate ranked tactical draft suggestions balancing VORP, cliffs, rules, and ADP value."""
    vorp_scores = calculate_vorp(available_players, baselines)
    cliff_by_pos = {w.position: w for w in cliff_warnings}
    current_round = (overall_pick - 1) // total_teams + 1

    top_tier_info: dict[str, tuple[int, int, float]] = {}
    for pos, pos_tiers in tiers_by_pos.items():
        if pos_tiers:
            top_t = pos_tiers[0]
            next_t = pos_tiers[1] if len(pos_tiers) > 1 else None
            t_drop = calculate_tier_drop(top_t, next_t)
            top_tier_info[pos] = (top_t.tier_num, len(top_t.players), t_drop)

    scored_players: list[tuple[float, Player, float, bool, TierCliffWarning | None, float, str | None]] = []

    for p in available_players:
        vorp = vorp_scores.get(p.id, 0.0)
        cliff = cliff_by_pos.get(str(p.position))
        is_cliff_defense = cliff is not None and (cliff.current_tier == (p.cheatsheet_tier or p.tier or 1))

        score = vorp
        if is_cliff_defense:
            score += 3.5 if cliff.cliff_risk == "CRITICAL" else 2.0

        p_tier = p.cheatsheet_tier or p.tier or 1
        if p_tier == 1:
            score += 1.5
        elif p_tier == 2:
            score += 0.8

        # Positional Scarcity Weighting: if player is in top active tier and only 1-2 players remain
        pos_info = top_tier_info.get(str(p.position))
        if pos_info:
            top_num, remaining_in_top, tier_drop = pos_info
            if p_tier == top_num and remaining_in_top <= 2 and tier_drop >= 1.2:
                score += 2.0 if remaining_in_top == 1 else 1.2

        # Strategy Rules Adjustment
        rule_delta, rule_note = _evaluate_strategy_rule_adjustments(p, cheatsheet_context, current_round)
        score += rule_delta

        adp_delta = 0.0
        if p.cheatsheet_rank:
            adp_delta = overall_pick - p.cheatsheet_rank
            if adp_delta > 0:
                score += min(2.0, adp_delta * 0.1)

        scored_players.append((score, p, vorp, is_cliff_defense, cliff, adp_delta, rule_note))

    scored_players.sort(key=lambda item: item[0], reverse=True)

    suggestions: list[DraftSuggestion] = []
    for rank, (_, player, vorp, is_cliff, cliff, adp_delta, r_note) in enumerate(scored_players[:top_n], start=1):
        reason = _build_suggestion_reason(
            player, vorp, cliff if is_cliff else None, adp_delta, overall_pick, top_tier_info, r_note
        )
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

    cliffs = detect_tier_cliffs(tiers_by_pos, picks_until_turn, turn_gap, on_the_clock, current_pick=overall_pick)

    baselines = calculate_vorp_baselines(all_players, total_teams)
    suggestions = generate_draft_suggestions(
        available_players,
        tiers_by_pos,
        cliffs,
        baselines,
        overall_pick,
        top_n=len(available_players),
        cheatsheet_context=cheatsheet_context,
        total_teams=total_teams,
    )

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
