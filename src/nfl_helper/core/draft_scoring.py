"""Draft candidate multi-factor scoring and suggestion ranking."""

import re

from nfl_helper.core.draft_rationale import (
    build_suggestion_rationale,
    calculate_sliding_note_shift,
)
from nfl_helper.core.draft_rules import (
    calculate_required_positions,
    evaluate_strategy_rule_adjustments,
)
from nfl_helper.core.tier_calculator import calculate_tier_drop
from nfl_helper.core.vorp import (
    POSITION_DEMAND_WEIGHTS,
    calculate_vorp,
)
from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.draft import DraftSuggestion, PlayerTier, TierCliffWarning
from nfl_helper.models.player import Player


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
    total_rounds: int = 16,
    user_drafted_players: list[Player] | None = None,
) -> list[DraftSuggestion]:
    """Generate ranked tactical draft suggestions balancing VORP, cliffs, rules, roster needs, and ADP value."""
    vorp_scores = calculate_vorp(available_players, baselines)
    cliff_by_pos = {w.position: w for w in cliff_warnings}
    current_round = (overall_pick - 1) // total_teams + 1
    roster = user_roster_counts or {}
    active_rules = cheatsheet_context.strategy_rules if cheatsheet_context else []
    rounds_remaining = max(1, total_rounds - current_round + 1)

    # Check conditional caps from strategy rules and single-starter roster limits
    capped_positions: set[str] = set()
    if cheatsheet_context:
        for pr in cheatsheet_context.positional_strategy:
            if pr.position == "QB" and (pr.no_second_if_top_tier or 1 in pr.conditional_max_count):
                for dp in user_drafted_players or []:
                    if str(dp.position).upper() == "QB":
                        capped_positions.add("QB")
            elif pr.position == "TE" and (pr.no_second_if_top_tier or 1 in pr.conditional_max_count):
                for dp in user_drafted_players or []:
                    if str(dp.position).upper() == "TE":
                        capped_positions.add("TE")

    if any("only one qb" in r.lower() for r in active_rules) and roster.get("QB", 0) >= 1:
        capped_positions.add("QB")
    if any("no second te" in r.lower() for r in active_rules) and roster.get("TE", 0) >= 1:
        capped_positions.add("TE")

    # Hard single-starter and standard maximum position caps once filled (filter completely, no penalties)
    if roster.get("QB", 0) >= 2:
        capped_positions.add("QB")
    if roster.get("TE", 0) >= 2:
        capped_positions.add("TE")
    if roster.get("K", 0) >= 1:
        capped_positions.add("K")
    if roster.get("D/ST", 0) >= 1:
        capped_positions.add("D/ST")

    if capped_positions:
        available_players = [
            p
            for p in available_players
            if str(p.position).upper() not in capped_positions
            and not (str(p.position).upper() in ("DEF", "DST") and "D/ST" in capped_positions)
        ]

    # Exclusivity: filter to allowed positions for active round target windows (e.g. Rounds 1-2 only RB/WR)
    if cheatsheet_context:
        active_rt = next(
            (
                rt
                for rt in reversed(cheatsheet_context.round_targets)
                if current_round in rt.target_rounds and rt.allowed_positions
            ),
            None,
        )
        if active_rt and active_rt.allowed_positions:
            allowed = set(active_rt.allowed_positions)
            if "D/ST" in allowed:
                allowed.update(["DEF", "DST"])
            available_players = [p for p in available_players if str(p.position).upper() in allowed]

    # Draft deadline & quota enforcement: filter suggestions when remaining rounds equals required slots
    required_positions = calculate_required_positions(
        current_round, total_rounds, roster, capped_positions=capped_positions, cheatsheet_context=cheatsheet_context
    )
    if required_positions:
        available_players = [
            p
            for p in available_players
            if str(p.position).upper() in required_positions
            or (str(p.position).upper() in ("DEF", "DST") and "D/ST" in required_positions)
        ]

    top_tier_info: dict[str, tuple[int, int, float]] = {}
    for pos, pos_tiers in tiers_by_pos.items():
        if pos_tiers:
            top_t = pos_tiers[0]
            next_t = pos_tiers[1] if len(pos_tiers) > 1 else None
            top_tier_info[pos] = (
                top_t.tier_num,
                len(top_t.players),
                calculate_tier_drop(top_t, next_t),
            )

    mins: dict[str, int] = {}
    deadline_quotas: list[tuple[str, int, int]] = []
    if cheatsheet_context:
        for pr in cheatsheet_context.positional_strategy:
            if (
                pr.position == "QB"
                and "QB" not in capped_positions
                and any(sum(b.target_tier_quotas.values()) >= 2 for b in pr.branches)
            ):
                mins["QB"] = max(mins.get("QB", 0), 2)
            for rt in cheatsheet_context.round_targets:
                for p_min, c_min in rt.min_counts.items():
                    mins[p_min] = max(mins.get(p_min, 0), c_min)
        for qd in cheatsheet_context.quota_deadlines:
            mins[qd.position] = max(mins.get(qd.position, 0), qd.required_count)
            deadline_quotas.append((qd.position, qd.required_count, qd.deadline_round))

    # Pass 1: Compute baseline score without note_delta to establish board density & ranks
    raw_scored: list[
        tuple[float, Player, float, float, float, TierCliffWarning | None, float, float, str | None, str | None]
    ] = []

    for p in available_players:
        vorp = vorp_scores.get(p.id, 0.0)
        cliff = cliff_by_pos.get(str(p.position))
        is_cliff_defense = cliff is not None and (cliff.current_tier == (p.cheatsheet_tier or p.tier or 1))

        demand_weight = POSITION_DEMAND_WEIGHTS.get(str(p.position), 1.0)
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

        # Position-specific final round elevation for unfilled mandatory single-starters
        pos_str = str(p.position).upper()
        if pos_str in ("D/ST", "DEF", "DST"):
            pos_str = "D/ST"

        if pos_str in ("K", "D/ST"):
            if rounds_remaining == 2:
                base_score += 3.0  # Elevate top D/ST or K in penultimate round (Round 14 of 15)
            elif rounds_remaining <= 1:
                base_score += 8.0  # Highest priority to fill mandatory starter in final round (Round 15)

        # Handcuff synergy bonus for backup / committee RBs on same NFL team as drafted starter
        handcuff_note: str | None = None
        if pos_str == "RB" and user_drafted_players and p.team and p.team != "FA":
            for dp in user_drafted_players:
                if str(dp.position).upper() == "RB" and dp.team == p.team and dp.id != p.id:
                    base_score += 0.50  # Small tactical boost for securing backfield handcuff
                    handcuff_note = f"Handcuff ({dp.name})"
                    break

        # Roster quota urgency & round deadline weighting
        for pos_dl, q_dl, dl_round in deadline_quotas:
            if pos_str == pos_dl and current_round <= dl_round:
                curr_dl_cnt = roster.get(pos_str, 0)
                if curr_dl_cnt < q_dl:
                    rounds_left = dl_round - current_round + 1
                    needed_dl = q_dl - curr_dl_cnt
                    if rounds_left <= needed_dl + 2:
                        urgency_bonus = min(3.5, 1.5 + (needed_dl / max(1, rounds_left)) * 1.5)
                        base_score += urgency_bonus

        if pos_str in mins:
            req_min = mins[pos_str]
            curr_pos_cnt = roster.get(pos_str, 0)
            if curr_pos_cnt < req_min:
                if current_round >= 7:
                    needed_min = req_min - curr_pos_cnt
                    urgency_bonus = min(3.5, 1.0 + (needed_min * 0.8) + (current_round - 7) * 0.2)
                    base_score += urgency_bonus
            elif curr_pos_cnt >= req_min + 1:
                has_unmet_mins = (
                    any(roster.get(p, 0) < m for p, m in mins.items() if p != pos_str)
                    or (roster.get("D/ST", 0) < 1)
                    or (roster.get("K", 0) < 1)
                )
                if has_unmet_mins and current_round >= 7:
                    surplus_penalty = 3.5 if current_round >= 12 else 2.5
                    base_score -= surplus_penalty

        # Positional Scarcity Weighting: only when ADP is in reachable range for current pick
        scarcity_bonus = 0.0
        pos_info = top_tier_info.get(str(p.position))
        is_adp_in_range = (p.adp is None) or (p.adp <= overall_pick + 6)
        if pos_info and is_adp_in_range:
            top_num, remaining_in_top, tier_drop = pos_info
            if p_tier == top_num and remaining_in_top <= 2 and tier_drop >= 0.7:
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
        rule_delta, rule_note = evaluate_strategy_rule_adjustments(
            p, cheatsheet_context, current_round, user_drafted_players=user_drafted_players
        )
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
                handcuff_note,
            )
        )

    # Sort baseline board by base_score to get baseline ranks
    raw_scored.sort(key=lambda item: item[0], reverse=True)
    base_scores = [item[0] for item in raw_scored]

    # Pass 2: Apply realistic sliding-window note adjustments
    scored_players: list[
        tuple[float, Player, float, float, float, TierCliffWarning | None, float, float, str | None, str | None]
    ] = []

    for idx, (b_score, p, vorp, t_bonus, s_bonus, cliff, adp_delta, r_delta, r_note, h_note) in enumerate(raw_scored):
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
            # Strip bracketed platform prefixes (e.g. [ESPN SLEEPER]) before scanning qualitative tags
            clean_note = re.sub(r"\[.*?\]", "", p.cheatsheet_notes).strip().lower()
            if "breakout" in clean_note:
                shift = calculate_sliding_note_shift(idx, "breakout")
                target_idx = max(0, idx - shift)
                target_score = base_scores[target_idx]
                note_delta = (target_score - b_score) + 0.0001
            elif "sleeper" in clean_note:
                shift = calculate_sliding_note_shift(idx, "sleeper")
                target_idx = max(0, idx - shift)
                target_score = base_scores[target_idx]
                note_delta = (target_score - b_score) + 0.0001
            elif "bust" in clean_note or "fade" in clean_note or "avoid" in clean_note:
                shift = calculate_sliding_note_shift(idx, "bust")
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
                h_note,
            )
        )

    scored_players.sort(key=lambda item: item[0], reverse=True)

    suggestions: list[DraftSuggestion] = []
    for rank, (score_val, player, vorp, t_bonus, s_bonus, cliff, adp_delta, r_delta, r_note, h_note) in enumerate(
        scored_players[:top_n], start=1
    ):
        reason = build_suggestion_rationale(
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
            handcuff_note=h_note,
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
