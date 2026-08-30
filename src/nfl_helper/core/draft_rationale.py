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


def calculate_sliding_note_shift(idx: int, note_type: str) -> int:
    """Calculate calibrated sliding window pick shift tailored for 10-team leagues (2-4 Rd 1, 4-8 Rds 2-3)."""
    if note_type == "bust":
        if idx < 10:
            return 2 + (idx // 4)
        elif idx < 30:
            return 4 + int((idx - 10) * 4 / 20)
        elif idx < 60:
            return 8 + int((idx - 30) * 6 / 30)
        else:
            return 14 + min(6, (idx - 60) // 15)
    elif note_type == "breakout":
        if idx < 10:
            return 2 + (idx // 5)
        elif idx < 30:
            return 4 + int((idx - 10) * 3 / 20)
        elif idx < 60:
            return 7 + int((idx - 30) * 5 / 30)
        else:
            return 12 + min(6, (idx - 60) // 15)
    elif note_type == "sleeper":
        if idx < 10:
            return 1 + (idx // 6)
        elif idx < 30:
            return 3 + int((idx - 10) * 2 / 20)
        elif idx < 60:
            return 5 + int((idx - 30) * 4 / 30)
        else:
            return 9 + min(5, (idx - 60) // 15)
    return 0
