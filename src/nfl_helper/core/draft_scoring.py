"""Draft candidate multi-factor scoring and suggestion ranking."""

import re

from nfl_helper.core.draft_rationale import (
    build_suggestion_rationale,
)
from nfl_helper.core.draft_rules import (
    calculate_required_positions,
    calculate_urgent_quota_positions,
    evaluate_strategy_rule_adjustments,
)
from nfl_helper.core.tier_calculator import calculate_tier_drop
from nfl_helper.core.vorp import POSITION_DEMAND_WEIGHTS, calculate_vorp
from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.draft import DraftSuggestion, PlayerTier, TierCliffWarning
from nfl_helper.models.player import Player


def _is_season_ending_injury(player: Player) -> bool:
    """Check if player has a season-ending injury to completely exclude from redraft boards."""
    if player.cheatsheet_notes:
        n_low = player.cheatsheet_notes.lower()
        if any(
            phrase in n_low
            for phrase in (
                "out for season",
                "out for the season",
                "season-ending",
                "season ending",
                "torn acl",
                "torn achilles",
                "achilles tear",
            )
        ):
            return True
    return False


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
    next_user_pick: int | None = None,
) -> list[DraftSuggestion]:
    """Generate ranked tactical draft suggestions balancing VORP, cliffs, rules, roster needs, and ADP value."""
    # Completely filter out players with season-ending injuries in redraft leagues
    available_players = [p for p in available_players if not _is_season_ending_injury(p)]

    vorp_scores = calculate_vorp(available_players, baselines)
    cliff_by_pos = {w.position: w for w in cliff_warnings}
    current_round = (overall_pick - 1) // total_teams + 1
    roster = user_roster_counts or {}
    rounds_remaining = max(1, total_rounds - current_round + 1)

    # Check conditional caps from strategy rules and single-starter roster limits
    capped_positions: set[str] = set()
    if cheatsheet_context:
        for pr in cheatsheet_context.positional_strategy:
            pos_upper = pr.position.upper()
            drafted_pos = [p for p in (user_drafted_players or []) if str(p.position).upper() == pos_upper]
            if not drafted_pos:
                continue

            # 1. If rule specifies max 1 for Tier 1, cap position ONLY if user drafted a Tier 1 player
            tier1_drafted = [p for p in drafted_pos if (p.cheatsheet_tier == 1 or p.tier == 1)]
            if tier1_drafted and (pr.no_second_if_top_tier or pr.conditional_max_count.get(1) == 1):
                capped_positions.add(pos_upper)
                continue

            # 2. Check if any branch tier quotas are fully satisfied by drafted players
            branch_satisfied = False
            for b in pr.branches:
                if b.target_tier_quotas:
                    all_quotas_met = True
                    for req_tier, req_cnt in b.target_tier_quotas.items():
                        drafted_in_tier = len(
                            [p for p in drafted_pos if (p.cheatsheet_tier == req_tier or p.tier == req_tier)]
                        )
                        if drafted_in_tier < req_cnt:
                            all_quotas_met = False
                            break
                    if all_quotas_met:
                        branch_satisfied = True
                        break
            if branch_satisfied:
                capped_positions.add(pos_upper)
                continue

    # Hard single-starter and standard maximum position caps once filled (filter completely, no penalties)
    if roster.get("QB", 0) >= 2:
        capped_positions.add("QB")
    if roster.get("TE", 0) >= 2:
        capped_positions.add("TE")
    if roster.get("K", 0) >= 1:
        capped_positions.add("K")
    if roster.get("D/ST", 0) >= 1:
        capped_positions.add("D/ST")

    # Hard-filter capped positions upfront so completed positions never show
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

    # End-of-draft roster legality enforcement: hard filter ONLY when remaining rounds equals mandatory unfilled starter slots
    mandatory_starter_reqs = calculate_required_positions(
        current_round, total_rounds, roster, capped_positions=capped_positions, cheatsheet_context=cheatsheet_context
    )
    if mandatory_starter_reqs:
        available_players = [
            p
            for p in available_players
            if str(p.position).upper() in mandatory_starter_reqs
            or (str(p.position).upper() in ("DEF", "DST") and "D/ST" in mandatory_starter_reqs)
        ]

    # Mid-draft strategy quota deadlines: soft urgency boost (+2.50) without deleting falling value steals
    urgent_quota_positions = calculate_urgent_quota_positions(
        current_round, roster, cheatsheet_context=cheatsheet_context
    )

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

    mins: dict[str, int] = {"K": 1, "D/ST": 1}
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
        tuple[
            float,
            Player,
            float,
            float,
            float,
            TierCliffWarning | None,
            float,
            float,
            str | None,
            str | None,
            float,
            str | None,
            float,
        ]
    ] = []

    for p in available_players:
        vorp = vorp_scores.get(p.id, 0.0)
        cliff = cliff_by_pos.get(str(p.position))
        is_cliff_defense = cliff is not None and (cliff.current_tier == (p.cheatsheet_tier or p.tier or 1))

        pos_str = (p.position.value if hasattr(p.position, "value") else str(p.position)).upper()
        if pos_str in ("D/ST", "DEF", "DST") or "DST" in pos_str or "DEF" in pos_str:
            pos_str = "D/ST"

        demand_weight = POSITION_DEMAND_WEIGHTS.get(pos_str, 1.0)
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

        # Position-specific final round elevation and early-round suppression for K and D/ST
        if pos_str in ("K", "D/ST"):
            if current_round <= 9:
                base_score -= 8.0  # Heavily suppress K/DST in rounds 1-9
            elif current_round <= 11:
                base_score -= 4.0  # Suppress K/DST in rounds 10-11
            elif roster.get(pos_str, 0) < 1:
                if rounds_remaining == 2 and p_tier == 1:
                    base_score += 1.20  # Mild boost for top K/DST in penultimate round without crowding out skill depth
                elif rounds_remaining <= 1:
                    base_score += 8.00  # Highest priority to fill mandatory starter in final round

        # Handcuff synergy bonus for backup / committee RBs on same NFL team as drafted starter
        handcuff_note: str | None = None
        if pos_str == "RB" and user_drafted_players and p.team and p.team != "FA":
            for dp in user_drafted_players:
                if str(dp.position).upper() == "RB" and dp.team == p.team and dp.id != p.id:
                    dp_tier = dp.cheatsheet_tier or dp.tier or 1
                    p_tier_val = p.cheatsheet_tier or p.tier or 1
                    if dp_tier <= p_tier_val and dp.projected_points >= p.projected_points:
                        base_score += 0.50  # Small tactical boost for securing backfield handcuff
                        handcuff_note = f"Handcuff ({dp.name})"
                        break

        # Unified, non-stacking roster quota urgency & round deadline weighting
        quota_urgency_bonus = 0.0
        quota_urgency_note: str | None = None

        if pos_str in urgent_quota_positions or (pos_str in ("DEF", "DST") and "D/ST" in urgent_quota_positions):
            quota_urgency_bonus = 1.80
            quota_urgency_note = f"Quota Urgency: {pos_str} required by deadline"

        for pos_dl, q_dl, dl_round in deadline_quotas:
            if pos_str == pos_dl and current_round <= dl_round:
                curr_dl_cnt = roster.get(pos_str, 0)
                if curr_dl_cnt < q_dl:
                    rounds_left = dl_round - current_round + 1
                    needed_dl = q_dl - curr_dl_cnt
                    if rounds_left <= needed_dl + 2:
                        calc_bonus = min(1.80, 0.8 + (needed_dl / max(1, rounds_left)) * 0.8)
                        if calc_bonus > quota_urgency_bonus:
                            quota_urgency_bonus = calc_bonus
                            quota_urgency_note = f"Quota Urgency: Need {needed_dl} {pos_str} by Rd {dl_round}"

        if cheatsheet_context and pos_str in mins:
            req_min = mins[pos_str]
            curr_pos_cnt = roster.get(pos_str, 0)
            if curr_pos_cnt < req_min and current_round >= 7:
                needed_min = req_min - curr_pos_cnt
                calc_bonus = min(1.60, 0.6 + (needed_min * 0.4))
                if calc_bonus > quota_urgency_bonus:
                    quota_urgency_bonus = calc_bonus
                    quota_urgency_note = f"Quota Urgency: Need {needed_min} {pos_str}"
            elif curr_pos_cnt >= req_min + 1:
                has_unmet_mins = (
                    any(roster.get(p, 0) < m for p, m in mins.items() if p != pos_str)
                    or (roster.get("D/ST", 0) < 1)
                    or (roster.get("K", 0) < 1)
                )
                if has_unmet_mins and current_round >= 7:
                    surplus_penalty = 2.5 if current_round >= 12 else 1.5
                    base_score -= surplus_penalty

        base_score += quota_urgency_bonus

        # Positional Scarcity Weighting: only when ADP is in reachable range for current pick
        scarcity_bonus = 0.0
        is_adp_in_range = (p.adp is not None) and (p.adp <= overall_pick + 6)

        # Count remaining players in this player's tier
        remaining_in_tier = 1
        for t in tiers_by_pos.get(str(p.position), []):
            if t.tier_num == p_tier:
                remaining_in_tier = t.count
                break

        # Tier Depth / Micro-Supply bonus
        tier_bonus = 0.0
        if is_adp_in_range and remaining_in_tier > 1:
            tier_bonus = min(1.0, (remaining_in_tier - 1) * 0.25)
            base_score += tier_bonus

        # User Cheatsheet Overrides & Target Tier Match
        rule_delta, rule_note = evaluate_strategy_rule_adjustments(
            p,
            cheatsheet_context,
            current_round=current_round,
            next_user_pick=next_user_pick,
            user_drafted_players=user_drafted_players,
        )
        base_score += rule_delta

        # Custom Cheatsheet Rank / Top N Bonus
        adp_delta = 0.0
        if p.cheatsheet_rank and p.cheatsheet_rank <= 50:
            adp_delta = overall_pick - p.cheatsheet_rank
            if adp_delta > 0:
                base_score += min(1.5, adp_delta * 0.08)

        # Market reach penalty / value steal bonus
        reach_penalty = 0.0
        if p.adp and p.adp > (overall_pick + 6):
            reach_penalty = min(2.5, (p.adp - (overall_pick + 6)) * 0.08)
            base_score -= reach_penalty
        elif p.adp and overall_pick > p.adp:
            steal_bonus = min(1.5, (overall_pick - p.adp) * 0.08)
            base_score += steal_bonus
        elif p.adp is None and overall_pick <= 120:
            base_score -= 2.5

        # Injury discount penalty based on severity and expected missed time
        injury_penalty = 0.0
        injury_note: str | None = None
        inj_status_str = (p.injury_status.value if hasattr(p.injury_status, "value") else str(p.injury_status)).upper()
        if "QUESTIONABLE" in inj_status_str or inj_status_str == "Q":
            injury_penalty = 0.40
            injury_note = "Injury: Questionable"
        elif "DOUBTFUL" in inj_status_str or inj_status_str == "D":
            injury_penalty = 1.00
            injury_note = "Injury: Doubtful"
        elif "OUT" in inj_status_str or inj_status_str == "O":
            injury_penalty = 1.50
            injury_note = "Injury: Out"
        elif "PUP" in inj_status_str:
            injury_penalty = 2.50
            injury_note = "Injury: PUP (Out 4+ Wks)"
        elif "IR" in inj_status_str or "NFI" in inj_status_str:
            injury_penalty = 2.50
            injury_note = "Injury: IR (4+ Wks)"
        elif "SUSPENDED" in inj_status_str or "SUSP" in inj_status_str:
            injury_penalty = 2.50
            injury_note = "Suspended"

        base_score -= injury_penalty

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
                quota_urgency_bonus,
                quota_urgency_note,
                reach_penalty,
                injury_penalty,
                injury_note,
            )
        )

    # Sort baseline board by base_score to get baseline ranks
    raw_scored.sort(key=lambda item: item[0], reverse=True)
    base_scores = [item[0] for item in raw_scored]

    # Pass 2: Apply realistic sliding-window note adjustments
    scored_players: list[
        tuple[
            float,
            Player,
            float,
            float,
            float,
            TierCliffWarning | None,
            float,
            float,
            str | None,
            str | None,
            float,
            str | None,
            float,
            float,
            str | None,
        ]
    ] = []

    for idx, (
        b_score,
        p,
        vorp,
        t_bonus,
        s_bonus,
        cliff,
        adp_delta,
        r_delta,
        r_note,
        h_note,
        q_bonus,
        q_note,
        reach_pen,
        inj_pen,
        inj_note,
    ) in enumerate(raw_scored):
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
                note_delta = 0.90
            elif "sleeper" in clean_note:
                note_delta = 0.60
            elif "bust" in clean_note or "fade" in clean_note or "avoid" in clean_note:
                note_delta = -0.75

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
                q_bonus,
                q_note,
                reach_pen,
                inj_pen,
                inj_note,
            )
        )

    scored_players.sort(key=lambda item: item[0], reverse=True)

    suggestions: list[DraftSuggestion] = []
    for rank, (
        score_val,
        player,
        vorp,
        t_bonus,
        s_bonus,
        cliff,
        adp_delta,
        r_delta,
        r_note,
        h_note,
        q_bonus,
        q_note,
        reach_pen,
        inj_pen,
        inj_note,
    ) in enumerate(scored_players[:top_n], start=1):
        reason = build_suggestion_rationale(
            player,
            vorp,
            t_bonus,
            s_bonus,
            cliff,
            adp_delta,
            overall_pick,
            top_tier_info,
            rule_delta=r_delta,
            rule_note=r_note,
            handcuff_note=h_note,
            quota_urgency_bonus=q_bonus,
            quota_urgency_note=q_note,
            reach_penalty=reach_pen,
            injury_penalty=inj_pen,
            injury_note=inj_note,
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
