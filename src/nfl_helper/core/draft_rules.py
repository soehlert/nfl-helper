"""Cheatsheet strategy rules evaluation and roster quota deadlines."""

import re

from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.player import Player


def evaluate_strategy_rule_adjustments(
    player: Player,
    cheatsheet_context: CheatsheetContext | None,
    current_round: int,
    user_drafted_players: list[Player] | None = None,
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
                    # Check player targets in branch
                    for target_p_name, target_rnd in branch.target_player_names:
                        if target_p_name.lower() in player.name.lower():
                            if current_round >= target_rnd:
                                delta += 1.5
                                specific_notes.append(f"Strategy Target: {player.name} in Rd {target_rnd}")
                            else:
                                rounds_early = target_rnd - current_round
                                delta -= rounds_early * 0.20
                                specific_notes.append(f"Strategy Hint: Target {player.name} in Rd {target_rnd}")
                            branch_evaluated = True
                            break
                    if branch_evaluated:
                        break

                    # Check branch target rounds
                    if branch.target_rounds and current_round in branch.target_rounds:
                        if (
                            (branch.top_n_target and (player.cheatsheet_rank or 99) <= branch.top_n_target)
                            or (branch.target_tiers and p_tier in branch.target_tiers)
                            or p_tier == 1
                        ):
                            delta += 1.5
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
                            # Quota for this tier already met; do not bump or penalize
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
    active_rules: list[str],
    capped_positions: set[str] | None = None,
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

    # 2. Strategy Rule Minimums
    mins: dict[str, int] = {}
    for r in active_rules:
        r_lower = r.lower()
        if ("one from tier 3 and one from tier 4" in r_lower or "two qb" in r_lower or "2 qb" in r_lower) and not (
            capped_positions and "QB" in capped_positions
        ):
            mins["QB"] = max(mins.get("QB", 0), 2)
        m = re.search(r"(QB|RB|WR|TE|K|D/ST|DEF)\s*[-:]?\s*.*minimum\s+(\d+)", r, re.IGNORECASE)
        if not m:
            m = re.search(r"(QB|RB|WR|TE|K|D/ST|DEF)\s*[-:]?\s*Get\s+(\d+)", r, re.IGNORECASE)
        if m:
            pos = m.group(1).upper()
            pos = "D/ST" if pos in ("DEF", "DST") else pos
            count = int(m.group(2))
            mins[pos] = max(mins.get(pos, 0), count)

    for pos, target_min in mins.items():
        curr_count = roster.get(pos, 0)
        if curr_count < target_min:
            needed = target_min - curr_count
            already_counted = needed_slots.count(pos)
            additional_needed = max(0, needed - already_counted)
            needed_slots.extend([pos] * additional_needed)

    # 3. Check mid-draft round deadlines (e.g. 4 RBs in the first 10 rounds)
    for r in active_rules:
        m = re.search(r"(RB|WR|QB|TE)\s*[-:]?\s*Get\s+(\d+)\s+in\s+the\s+first\s+(\d+)\s+rounds", r, re.IGNORECASE)
        if m:
            pos = m.group(1).upper()
            quota = int(m.group(2))
            deadline_rnd = int(m.group(3))
            if current_round <= deadline_rnd:
                rounds_left_to_deadline = deadline_rnd - current_round + 1
                curr_count = roster.get(pos, 0)
                if curr_count < quota and rounds_left_to_deadline <= (quota - curr_count):
                    return {pos}

    if rounds_remaining <= len(needed_slots):
        return set(needed_slots)
    return None
