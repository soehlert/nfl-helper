"""Cheatsheet strategy rules evaluation and roster quota deadlines."""

from nfl_helper.models.cheatsheet import CheatsheetContext, PositionalQuotaDeadline
from nfl_helper.models.player import Player


def evaluate_strategy_rule_adjustments(
    player: Player,
    cheatsheet_context: CheatsheetContext | None,
    current_round: int,
    user_drafted_players: list[Player] | None = None,
    next_user_pick: int | None = None,
    remaining_tier_count: int = 1,
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
            p_tier = player.cheatsheet_tier or player.tier or 1

            # Check drafted players of this position
            drafted_pos = [
                dp for dp in (user_drafted_players or []) if str(dp.position).upper() == str(player.position).upper()
            ]

            # Check conditional max caps (e.g. 'if you get a tier 1 only one QB total', 'no second TE if you have tier 1')
            hit_cap = False
            for dp in drafted_pos:
                dp_tier = dp.cheatsheet_tier or dp.tier or 1
                if dp_tier in pos_rule.conditional_max_count:
                    max_allowed = pos_rule.conditional_max_count[dp_tier]
                    if len(drafted_pos) >= max_allowed:
                        delta -= 3.0
                        specific_notes.append(
                            f"Strategy: Max {max_allowed} {pos_rule.position} (Drafted Tier {dp_tier} {dp.name})"
                        )
                        hit_cap = True
                        break
                if pos_rule.no_second_if_top_tier and dp_tier == 1 and len(drafted_pos) >= 1:
                    delta -= 3.0
                    specific_notes.append(f"Strategy: No 2nd {pos_rule.position} (Drafted Tier 1 {dp.name})")
                    hit_cap = True
                    break

            if hit_cap:
                clamped_delta = round(max(-3.0, min(3.0, delta)), 2)
                return clamped_delta, specific_notes[0]

            # Evaluate branches if available
            if pos_rule.branches:
                active_branches = pos_rule.branches
                if drafted_pos:
                    matching_branches = [
                        b
                        for b in pos_rule.branches
                        if sum(b.target_tier_quotas.values()) > len(drafted_pos)
                        and (
                            not b.target_tiers
                            or any(
                                (dp.cheatsheet_tier or dp.tier or 1) in b.target_tiers
                                or (dp.cheatsheet_tier or dp.tier or 1) in b.target_tier_quotas
                                for dp in drafted_pos
                            )
                            or (len(drafted_pos) < 2 and sum(b.target_tier_quotas.values()) >= 2)
                        )
                    ]
                    active_branches = matching_branches

                branch_evaluated = False
                for branch in active_branches:
                    # Check branch target rounds
                    if branch.target_rounds and current_round in branch.target_rounds:
                        if (
                            (branch.top_n_target and (player.cheatsheet_rank or 99) <= branch.top_n_target)
                            or (branch.target_tiers and p_tier in branch.target_tiers)
                            or p_tier == 1
                        ):
                            if (
                                next_user_pick
                                and player.adp
                                and player.adp >= next_user_pick
                                and remaining_tier_count > 1
                            ):
                                delta += 0.70
                                specific_notes.append(
                                    f"Strategy Target: {pos_rule.position} in Rd {current_round} (Safe across turn)"
                                )
                            else:
                                delta += 1.50
                                specific_notes.append(f"Strategy Target: Top {pos_rule.position} in Rd {current_round}")
                            branch_evaluated = True
                            break
                    elif branch.target_rounds and current_round < min(branch.target_rounds):
                        rounds_early = min(branch.target_rounds) - current_round
                        defer_rate = 0.90 if p_tier == 1 else 0.50
                        delta -= rounds_early * defer_rate
                        specific_notes.append(
                            f"Strategy Hint: {pos_rule.position} targeted in Rd {min(branch.target_rounds)}+"
                        )
                        branch_evaluated = True
                        break

                    # Check branch tier quotas (e.g. 1 from Tier 3, 1 from Tier 4)
                    if branch.target_tier_quotas and p_tier in branch.target_tier_quotas:
                        drafted_same_tier = [dp for dp in drafted_pos if (dp.cheatsheet_tier or dp.tier or 1) == p_tier]
                        quota = branch.target_tier_quotas[p_tier]
                        if len(drafted_same_tier) < quota:
                            delta += 1.0
                            specific_notes.append(f"Strategy Target: Tier {p_tier} {pos_rule.position}")
                        else:
                            delta += 0.0
                        branch_evaluated = True
                        break

                if not branch_evaluated:
                    all_target_tiers = pos_rule.target_tiers or [
                        t for b in pos_rule.branches for t in (b.target_tiers or list(b.target_tier_quotas.keys()))
                    ]
                    if all_target_tiers and p_tier not in all_target_tiers:
                        delta -= 0.6
                        specific_notes.append(
                            f"Strategy Hint: Rule prefers Tier {','.join(map(str, sorted(set(all_target_tiers))))} {pos_rule.position}"
                        )

                continue

            # Check if this rule defines a specific round window (e.g. rounds 3-5)
            if pos_rule.target_rounds:
                min_rnd = min(pos_rule.target_rounds)
                if current_round in pos_rule.target_rounds:
                    if (
                        pos_rule.top_n_target and (player.cheatsheet_rank or 99) <= pos_rule.top_n_target
                    ) or p_tier == 1:
                        if next_user_pick and player.adp and player.adp >= next_user_pick and remaining_tier_count > 1:
                            delta += 0.70
                            specific_notes.append(
                                f"Strategy Target: {pos_rule.position} in Rd {current_round} (Safe across turn)"
                            )
                        else:
                            delta += 1.50
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
                    delta -= 1.0
                    specific_notes.append(
                        f"Strategy Hint: Late-Round {pos_rule.position} (targeting Tiers {','.join(map(str, pos_rule.target_tiers))})"
                    )
                elif p_tier in pos_rule.target_tiers:
                    delta += 1.0
                    specific_notes.append(f"Strategy Target: Tier {p_tier} {pos_rule.position}")
                else:
                    delta -= 0.6
                    specific_notes.append(
                        f"Strategy Hint: Rule prefers Tier {','.join(map(str, pos_rule.target_tiers))} {pos_rule.position}"
                    )

    # Clamp total strategy delta to prevent extreme distortions
    clamped_delta = round(max(-3.0, min(3.0, delta)), 2)
    final_note = specific_notes[0] if specific_notes else (general_notes[0] if general_notes else None)
    return clamped_delta, final_note


def calculate_required_positions(
    current_round: int,
    total_rounds: int,
    roster: dict[str, int],
    active_rules: list[str] | None = None,
    capped_positions: set[str] | None = None,
    quota_deadlines: list[PositionalQuotaDeadline] | None = None,
    cheatsheet_context: CheatsheetContext | None = None,
) -> set[str] | None:
    """Calculate mandatory positions that MUST be drafted now to satisfy roster legality & strategy minimums."""
    rounds_remaining = max(1, total_rounds - current_round + 1)
    needed_slots: list[str] = []

    # 1. Mandatory Starters (K, D/ST, QB, TE)
    if roster.get("K", 0) < 1:
        needed_slots.append("K")
    if roster.get("D/ST", 0) < 1:
        needed_slots.append("D/ST")
    if roster.get("QB", 0) < 1:
        needed_slots.append("QB")
    if roster.get("TE", 0) < 1:
        needed_slots.append("TE")

    # 2. Check round target minimums within their specific round window (e.g. Rounds 1-2: at least 1 RB)
    if cheatsheet_context:
        for rt in cheatsheet_context.round_targets:
            if current_round in rt.target_rounds:
                window_end = max(rt.target_rounds)
                rounds_left_in_window = window_end - current_round + 1
                for pos_req, min_cnt in rt.min_counts.items():
                    curr_count = roster.get(pos_req, 0)
                    if curr_count < min_cnt and rounds_left_in_window <= (min_cnt - curr_count):
                        return {pos_req}

    # 3. Strategy Rule Minimums from typed positional strategy rules
    mins: dict[str, int] = {}
    if cheatsheet_context:
        for pr in cheatsheet_context.positional_strategy:
            if (
                pr.position == "QB"
                and ("QB" not in (capped_positions or set()))
                and any(sum(b.target_tier_quotas.values()) >= 2 for b in pr.branches)
            ):
                mins["QB"] = max(mins.get("QB", 0), 2)
            for rt in cheatsheet_context.round_targets:
                for p_min, c_min in rt.min_counts.items():
                    mins[p_min] = max(mins.get(p_min, 0), c_min)

    for pos, target_min in mins.items():
        curr_count = roster.get(pos, 0)
        if curr_count < target_min:
            needed = target_min - curr_count
            already_counted = needed_slots.count(pos)
            additional_needed = max(0, needed - already_counted)
            needed_slots.extend([pos] * additional_needed)

    if rounds_remaining <= len(needed_slots):
        return set(needed_slots)
    return None


def calculate_urgent_quota_positions(
    current_round: int,
    roster: dict[str, int],
    quota_deadlines: list[PositionalQuotaDeadline] | None = None,
    cheatsheet_context: CheatsheetContext | None = None,
) -> set[str]:
    """Calculate positions with urgent strategy quota deadlines (e.g. RB - 4 by Round 10)."""
    urgent: set[str] = set()
    if cheatsheet_context:
        for rt in cheatsheet_context.round_targets:
            if current_round in rt.target_rounds:
                window_end = max(rt.target_rounds)
                rounds_left_in_window = window_end - current_round + 1
                for pos_req, min_cnt in rt.min_counts.items():
                    curr_count = roster.get(pos_req, 0)
                    if curr_count < min_cnt and rounds_left_in_window <= (min_cnt - curr_count):
                        urgent.add(pos_req)

    deadlines = quota_deadlines or (cheatsheet_context.quota_deadlines if cheatsheet_context else [])
    for qd in deadlines:
        pos_dl = qd.position
        quota = qd.required_count
        deadline_rnd = qd.deadline_round
        if current_round <= deadline_rnd:
            rounds_left_to_deadline = deadline_rnd - current_round + 1
            curr_count = roster.get(pos_dl, 0)
            if curr_count < quota and rounds_left_to_deadline <= (quota - curr_count):
                urgent.add(pos_dl)
    return urgent
