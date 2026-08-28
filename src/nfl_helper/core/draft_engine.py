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

_POS_DEMAND_WEIGHT: dict[str, float] = {
    "RB": 1.0,
    "WR": 1.0,
    "TE": 0.70,
    "QB": 0.65,
    "K": 0.05,
    "D/ST": 0.05,
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
    idx = schedule.index(next_pick)
    subsequent_pick = schedule[idx + 1] if idx + 1 < len(schedule) else schedule[-1] + total_teams
    turn_gap = max(0, subsequent_pick - next_pick - 1)
    return picks_until_turn, turn_gap, is_on_the_clock


def calculate_vorp_baselines(all_players: list[Player], total_teams: int) -> dict[str, float]:
    """Determine replacement baseline projected points per position based on starter depth."""

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
    general_notes: list[str] = []
    specific_notes: list[str] = []

    # Check if there is a specific positional strategy rule for this player
    has_specific_rule = any(r.position == str(player.position) for r in cheatsheet_context.positional_strategy)

    # 1. Evaluate Round Target Constraints (e.g. Rounds 1-2 only RB/WR)
    for rnd_rule in cheatsheet_context.round_targets:
        if current_round in rnd_rule.target_rounds:
            if rnd_rule.allowed_positions and player.position not in rnd_rule.allowed_positions:
                if not has_specific_rule:
                    delta -= 0.5
                    general_notes.append(
                        f"Strategy Hint: Rd {current_round} prioritizes {', '.join(rnd_rule.allowed_positions)}"
                    )
            elif rnd_rule.allowed_positions and player.position in rnd_rule.allowed_positions:
                delta += 0.5

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
                        delta += 1.5
                        specific_notes.append(f"Strategy Target: {player.name} in Rd {t_rnd}")
                    else:
                        rounds_early = t_rnd - current_round
                        delta -= rounds_early * 0.20
                        specific_notes.append(f"Strategy Hint: Target {player.name} in Rd {t_rnd}")
                    continue

            # Check if this rule defines a specific round window (e.g. rounds 3-5)
            if pos_rule.target_rounds:
                min_rnd = min(pos_rule.target_rounds)
                if current_round in pos_rule.target_rounds:
                    if (
                        pos_rule.top_n_target and (player.cheatsheet_rank or 99) <= pos_rule.top_n_target
                    ) or p_tier == 1:
                        delta += 1.5
                        specific_notes.append(f"Strategy Target: Top {pos_rule.position} in Rd {current_round}")
                    elif pos_rule.target_tiers and p_tier in pos_rule.target_tiers:
                        delta += 0.8
                        specific_notes.append(f"Strategy Target: Tier {p_tier} {pos_rule.position}")
                elif current_round < min_rnd:
                    rounds_early = min_rnd - current_round
                    defer_rate = 0.90 if p_tier == 1 else 0.50
                    delta -= rounds_early * defer_rate
                    specific_notes.append(f"Strategy Hint: {pos_rule.position} targeted in Rd {min_rnd}+")

            # Check if this rule defines specific target tiers (e.g. tiers 3-4 for late-round approach)
            elif pos_rule.target_tiers:
                if current_round <= 3 and min(pos_rule.target_tiers) >= 3:
                    delta -= 1.2
                    specific_notes.append(
                        f"Strategy Hint: Late-Round {pos_rule.position} (targeting Tiers {','.join(map(str, pos_rule.target_tiers))})"
                    )
                elif current_round >= 4 and p_tier in pos_rule.target_tiers:
                    delta += 0.8
                    specific_notes.append(f"Strategy Target: Tier {p_tier} {pos_rule.position}")

    # Clamp total strategy delta to prevent extreme distortions
    clamped_delta = round(max(-3.0, min(3.0, delta)), 2)
    final_note = specific_notes[0] if specific_notes else (general_notes[0] if general_notes else None)
    return clamped_delta, final_note


def _build_suggestion_reason(
    player: Player,
    vorp: float,
    tier_bonus: float,
    scarcity_bonus: float,
    cliff: TierCliffWarning | None,
    adp_delta: float,
    overall_pick: int,
    top_tier_info: dict[str, tuple[int, int, float]],
    rule_delta: float = 0.0,
    rule_note: str | None = None,
    total_teams: int = 12,
) -> str:
    """Generate concise, factual 4-row structured justification showing exact points made/lost."""
    lines: list[str] = []
    p_tier = player.cheatsheet_tier or player.tier or 1

    # Row 1: VORP Baseline
    lines.append(f"+{vorp:.1f} pts VORP (Tier {p_tier} {player.position})")

    # Row 2: Tier & Scarcity Points
    t_pts = tier_bonus + scarcity_bonus
    pos_info = top_tier_info.get(str(player.position))
    if pos_info:
        top_num, remaining_in_top, tier_drop = pos_info
        if p_tier == top_num:
            if scarcity_bonus > 0:
                lines.append(
                    f"+{t_pts:.1f} pts (Tier {top_num} Scarcity: {remaining_in_top} left before -{tier_drop:.1f} drop)"
                )
            else:
                lines.append(f"+{tier_bonus:.1f} pts (Tier {top_num} Value • {remaining_in_top} remaining)")
        else:
            lines.append(f"+{tier_bonus:.1f} pts (Tier {p_tier} • {player.projected_points:.1f} proj)")
    elif cliff:
        lines.append(f"+2.0 pts (Cliff Defense • {cliff.players_remaining} left)")
    else:
        lines.append(f"+{tier_bonus:.1f} pts (Tier {p_tier})")

    # Row 3: ADP Market Context (Market Consensus Round & Pick / Value Steal)
    if player.adp:
        discount = overall_pick - player.adp
        target_round = int((player.adp - 1) // max(1, total_teams)) + 1
        target_pick = int((player.adp - 1) % max(1, total_teams)) + 1

        if discount >= 8.0:
            adp_pts = min(2.0, discount * 0.1)
            lines.append(
                f"+{adp_pts:.1f} pts (Market Steal • Available +{discount:.0f} picks past ADP {player.adp:.1f})"
            )
        elif discount >= 3.0:
            adp_pts = min(2.0, discount * 0.1)
            lines.append(
                f"+{adp_pts:.1f} pts (Market Value • Available +{discount:.0f} picks past ADP {player.adp:.1f})"
            )
        else:
            lines.append(f"Market Consensus: Round {target_round}, Pick {target_pick} (ADP {player.adp:.1f})")

    # Row 4: Tactical Note, Strategy Delta & Stadium Environment
    is_dome = player.game_context and player.game_context.is_dome
    env_label = "Dome Stadium" if is_dome else f"Outdoor ({player.team})"
    note_parts = []
    if player.cheatsheet_notes:
        note_parts.append(player.cheatsheet_notes)
    if rule_note and rule_delta != 0.0:
        sign = "+" if rule_delta > 0 else ""
        note_parts.append(f"{sign}{rule_delta:.1f} pts {rule_note}")
    if note_parts:
        lines.append(f"{' • '.join(note_parts)} • {env_label}")
    else:
        lines.append(env_label)

    return "\n".join(lines)


def _calculate_sliding_note_shift(idx: int, note_type: str) -> int:
    """Calculate calibrated sliding window pick shift tailored for 10-team leagues (2-4 Rd 1, 4-8 Rds 2-3)."""
    if note_type == "bust":
        if idx < 10:  # Round 1 (Picks 1-10)
            return 2 + (idx // 4)  # 2 to 4 picks
        elif idx < 30:  # Rounds 2-3 (Picks 11-30)
            return 4 + int((idx - 10) * 4 / 20)  # 4 to 8 picks
        elif idx < 60:  # Rounds 4-6 (Picks 31-60)
            return 8 + int((idx - 30) * 6 / 30)  # 8 to 14 picks
        else:  # Rounds 7+
            return 14 + min(6, (idx - 60) // 15)  # 14 to 20 picks
    elif note_type == "breakout":
        if idx < 10:  # Round 1
            return 2 + (idx // 5)  # 2 to 3 picks
        elif idx < 30:  # Rounds 2-3
            return 4 + int((idx - 10) * 3 / 20)  # 4 to 7 picks
        elif idx < 60:  # Rounds 4-6
            return 7 + int((idx - 30) * 5 / 30)  # 7 to 12 picks
        else:  # Rounds 7+
            return 12 + min(6, (idx - 60) // 15)  # 12 to 18 picks
    elif note_type == "sleeper":
        if idx < 10:  # Round 1
            return 1 + (idx // 6)  # 1 to 2 picks
        elif idx < 30:  # Rounds 2-3
            return 3 + int((idx - 10) * 2 / 20)  # 3 to 5 picks
        elif idx < 60:  # Rounds 4-6
            return 5 + int((idx - 30) * 4 / 30)  # 5 to 9 picks
        else:  # Rounds 7+
            return 9 + min(5, (idx - 60) // 15)  # 9 to 14 picks
    return 0


def generate_draft_suggestions(
    available_players: list[Player],
    tiers_by_pos: dict[str, list[PlayerTier]],
    cliff_warnings: list[TierCliffWarning],
    baselines: dict[str, float],
    overall_pick: int,
    top_n: int = 150,
    cheatsheet_context: CheatsheetContext | None = None,
    total_teams: int = 12,
    user_roster_counts: dict[str, int] | None = None,
) -> list[DraftSuggestion]:
    """Generate ranked tactical draft suggestions balancing VORP, cliffs, rules, roster needs, and ADP value."""
    vorp_scores = calculate_vorp(available_players, baselines)
    cliff_by_pos = {w.position: w for w in cliff_warnings}
    current_round = (overall_pick - 1) // total_teams + 1
    roster = user_roster_counts or {}

    top_tier_info: dict[str, tuple[int, int, float]] = {}
    for pos, pos_tiers in tiers_by_pos.items():
        if pos_tiers:
            top_t = pos_tiers[0]
            next_t = pos_tiers[1] if len(pos_tiers) > 1 else None
            t_drop = calculate_tier_drop(top_t, next_t)
            top_tier_info[pos] = (top_t.tier_num, len(top_t.players), t_drop)

    # Pass 1: Compute baseline score without note_delta to establish board density & ranks
    raw_scored: list[tuple[float, Player, float, float, float, TierCliffWarning | None, float, float, str | None]] = []

    for p in available_players:
        vorp = vorp_scores.get(p.id, 0.0)
        cliff = cliff_by_pos.get(str(p.position))
        is_cliff_defense = cliff is not None and (cliff.current_tier == (p.cheatsheet_tier or p.tier or 1))

        demand_weight = _POS_DEMAND_WEIGHT.get(str(p.position), 1.0)
        p_tier = p.cheatsheet_tier or p.tier or 1

        base_score = vorp + (p.projected_points * 0.005)
        if is_cliff_defense:
            base_score += 2.0 if cliff.cliff_risk == "CRITICAL" else 1.2

        tier_bonus = 0.0
        if p_tier == 1:
            tier_bonus = 1.5 * demand_weight
        elif p_tier == 2:
            tier_bonus = 0.8 * demand_weight
        base_score += tier_bonus

        # Roster needs demand adjustment (satisfaction penalties for single-starter positions)
        pos_str = str(p.position).upper()
        if pos_str == "QB" and roster.get("QB", 0) >= 1:
            base_score -= 3.5 if current_round < 12 else 1.0
        elif pos_str == "TE" and roster.get("TE", 0) >= 1:
            base_score -= 2.5 if current_round < 10 else 0.8
        elif pos_str in ("K", "D/ST", "DEF", "DST") and roster.get(pos_str, 0) >= 1:
            base_score -= 4.0

        # Positional Scarcity Weighting: only when ADP is in reachable range for current pick
        scarcity_bonus = 0.0
        pos_info = top_tier_info.get(str(p.position))
        is_adp_in_range = (p.adp is None) or (p.adp <= overall_pick + 6)
        if pos_info and is_adp_in_range:
            top_num, remaining_in_top, tier_drop = pos_info
            if p_tier == top_num and remaining_in_top <= 2 and tier_drop >= 1.2:
                scarcity_val = 1.5 if remaining_in_top == 1 else 0.8
                scarcity_bonus = scarcity_val * demand_weight
                base_score += scarcity_bonus

        # Market reach penalty / value steal bonus
        if p.adp and p.adp > (overall_pick + 6):
            reach_penalty = min(2.5, (p.adp - (overall_pick + 6)) * 0.08)
            base_score -= reach_penalty
        elif p.adp and overall_pick > p.adp:
            steal_bonus = min(1.5, (overall_pick - p.adp) * 0.08)
            base_score += steal_bonus

        # Strategy Rules Adjustment
        rule_delta, rule_note = _evaluate_strategy_rule_adjustments(p, cheatsheet_context, current_round)
        base_score += rule_delta

        adp_delta = 0.0
        if p.cheatsheet_rank:
            adp_delta = overall_pick - p.cheatsheet_rank
            if adp_delta > 0:
                base_score += min(1.5, adp_delta * 0.08)

        raw_scored.append(
            (
                base_score,
                p,
                vorp,
                tier_bonus,
                scarcity_bonus,
                cliff if is_cliff_defense else None,
                adp_delta,
                rule_delta,
                rule_note,
            )
        )

    # Sort baseline board by base_score to get baseline ranks
    raw_scored.sort(key=lambda item: item[0], reverse=True)
    base_scores = [item[0] for item in raw_scored]

    # Pass 2: Apply realistic sliding-window note adjustments
    scored_players: list[
        tuple[float, Player, float, float, float, TierCliffWarning | None, float, float, str | None]
    ] = []

    for idx, (b_score, p, vorp, t_bonus, s_bonus, cliff, adp_delta, r_delta, r_note) in enumerate(raw_scored):
        note_delta = 0.0

        # Check strategy target round anchoring when pick is earlier than designated target round
        target_round_match = re.search(r"(?:targeted in Rd|Target .* in Rd)\s+(\d+)", r_note or "")
        if target_round_match:
            t_rnd = int(target_round_match.group(1))
            if current_round < t_rnd:
                min_target_idx = (t_rnd - 1) * total_teams
                if idx < min_target_idx and min_target_idx < len(base_scores):
                    target_idx = min(len(base_scores) - 1, min_target_idx + 1 + (idx % 6))
                    note_delta = (base_scores[target_idx] - b_score) - 0.0001

        if note_delta == 0.0 and p.cheatsheet_notes:
            nl = p.cheatsheet_notes.lower()
            if "breakout" in nl:
                shift = _calculate_sliding_note_shift(idx, "breakout")
                target_idx = max(0, idx - shift)
                target_score = base_scores[target_idx]
                note_delta = (target_score - b_score) + 0.0001
            elif "sleeper" in nl or "pick" in nl or "target" in nl:
                shift = _calculate_sliding_note_shift(idx, "sleeper")
                target_idx = max(0, idx - shift)
                target_score = base_scores[target_idx]
                note_delta = (target_score - b_score) + 0.0001
            elif "bust" in nl or "fade" in nl:
                shift = _calculate_sliding_note_shift(idx, "bust")
                target_idx = min(len(base_scores) - 1, idx + shift)
                note_delta = (base_scores[target_idx] - b_score) - 0.0001

        final_score = b_score + note_delta
        scored_players.append(
            (
                final_score,
                p,
                vorp,
                t_bonus,
                s_bonus,
                cliff,
                adp_delta,
                r_delta,
                r_note,
            )
        )

    scored_players.sort(key=lambda item: item[0], reverse=True)

    suggestions: list[DraftSuggestion] = []
    for rank, (score_val, player, vorp, t_bonus, s_bonus, cliff, adp_delta, r_delta, r_note) in enumerate(
        scored_players[:top_n], start=1
    ):
        reason = _build_suggestion_reason(
            player,
            vorp,
            t_bonus,
            s_bonus,
            cliff,
            adp_delta,
            overall_pick,
            top_tier_info,
            r_delta,
            r_note,
            total_teams=total_teams,
        )

        suggestions.append(
            DraftSuggestion(
                rank=rank,
                player=player,
                reason=reason,
                vorp=vorp,
                score=round(score_val, 2),
                is_cliff_defense=cliff is not None,
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
    drafted_names = {pick.player_name.lower() for pick in recent_picks if pick.player_name}
    available_players = [p for p in all_players if p.id not in drafted_ids and p.name.lower() not in drafted_names]

    picks_until_turn, turn_gap, on_the_clock = calculate_lookahead(
        overall_pick, user_draft_slot, total_teams, total_rounds
    )

    positions = ["QB", "RB", "WR", "TE", "K", "D/ST"]
    tiers_by_pos: dict[str, list[PlayerTier]] = {}
    avail_by_pos: dict[str, list[Player]] = {}

    for pos in positions:
        pos_avail = [p for p in available_players if p.position == pos]
        avail_by_pos[pos] = pos_avail
        clustered = cluster_position_tiers(pos_avail, pos, cheatsheet_context)
        tiers_by_pos[pos] = clustered
        for t in clustered:
            for p in t.players:
                p.tier = t.tier_num

    user_picks: list[DraftPick] = []
    for pick in recent_picks:
        pick_owner_slot = calculate_snake_pick_owner(pick.overall_pick, total_teams)
        if pick_owner_slot == user_draft_slot:
            user_picks.append(pick)

    user_roster_counts: dict[str, int] = {}
    for pick in user_picks:
        p_pos = (pick.position or "").upper()
        if p_pos in ("DEF", "DST", "D/ST"):
            p_pos = "D/ST"
        if p_pos:
            user_roster_counts[p_pos] = user_roster_counts.get(p_pos, 0) + 1

    cliffs = detect_tier_cliffs(
        tiers_by_pos,
        picks_until_turn,
        turn_gap,
        on_the_clock,
        current_pick=overall_pick,
        user_roster_counts=user_roster_counts,
    )

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
        user_roster_counts=user_roster_counts,
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
