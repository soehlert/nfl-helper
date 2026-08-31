"""Tactical draft suggestion rationale generator and note shift calculators."""

from nfl_helper.models.draft import TierCliffWarning
from nfl_helper.models.player import Player


def build_suggestion_rationale(
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
    handcuff_note: str | None = None,
    quota_urgency_bonus: float = 0.0,
    quota_urgency_note: str | None = None,
    reach_penalty: float = 0.0,
    injury_penalty: float = 0.0,
    injury_note: str | None = None,
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

    # Row 3: ADP Market Context (Market Consensus Round & Pick / Value Steal / Reach)
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
        elif reach_penalty > 0.0:
            lines.append(
                f"-{reach_penalty:.1f} pts (Market Reach • ADP {player.adp:.1f} is +{player.adp - overall_pick:.0f} picks ahead)"
            )
        else:
            lines.append(f"Market Consensus: Round {target_round}, Pick {target_pick} (ADP {player.adp:.1f})")

    # Row 4: Tactical Note, Strategy Delta, Quota Urgency, Injury & Stadium Environment
    is_dome = player.game_context and player.game_context.is_dome
    env_label = "Dome Stadium" if is_dome else f"Outdoor ({player.team})"
    note_parts = []
    if injury_note and injury_penalty > 0.0:
        note_parts.append(f"-{injury_penalty:.1f} pts {injury_note}")
    if quota_urgency_note and quota_urgency_bonus > 0.0:
        note_parts.append(f"+{quota_urgency_bonus:.1f} pts {quota_urgency_note}")
    if handcuff_note:
        note_parts.append(handcuff_note)
    if rule_note and rule_delta != 0.0:
        sign = "+" if rule_delta > 0 else ""
        note_parts.append(f"{sign}{rule_delta:.1f} pts {rule_note}")
    if env_label:
        note_parts.append(env_label)

    lines.append(" • ".join(note_parts))

    return "\n".join(lines)
