"""Positional tier clustering and 3-scenario cliff detection."""

from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.draft import CliffType, PlayerTier, TierCliffWarning
from nfl_helper.models.player import Player

# Refined realistic drop-off thresholds (single step drop, maximum tier span, max tier cluster size)
_DROP_THRESHOLDS: dict[str, tuple[float, float, int]] = {
    "QB": (0.6, 1.1, 4),
    "RB": (0.8, 1.5, 4),
    "WR": (0.8, 1.5, 4),
    "TE": (0.7, 1.3, 4),
    "K": (0.3, 0.6, 3),
    "D/ST": (0.3, 0.6, 3),
}
_DEFAULT_THRESHOLD: tuple[float, float, int] = (0.8, 1.5, 4)


def _cluster_hybrid(players: list[Player], position: str) -> list[PlayerTier]:
    """Hybrid clustering: combines empirical point drop-offs with cheatsheet tier guidance."""
    if not players:
        return []

    sorted_p = sorted(players, key=lambda x: x.projected_points, reverse=True)
    single_drop, max_span, max_size = _DROP_THRESHOLDS.get(position.upper(), _DEFAULT_THRESHOLD)

    clusters: list[list[Player]] = [[sorted_p[0]]]
    current_tier_max = sorted_p[0].projected_points
    current_cs_tier = sorted_p[0].cheatsheet_tier or sorted_p[0].tier

    for p in sorted_p[1:]:
        last_pts = clusters[-1][-1].projected_points
        curr_pts = p.projected_points
        step_drop = last_pts - curr_pts
        span_drop = current_tier_max - curr_pts
        p_cs_tier = p.cheatsheet_tier or p.tier

        # Cheatsheet tier transition supported by a non-trivial difference
        cs_transition = (
            p_cs_tier is not None
            and current_cs_tier is not None
            and p_cs_tier > current_cs_tier
            and (step_drop >= 0.3 or span_drop >= 0.8)
        )

        # Statistical step or span drop or max cluster size
        stat_transition = (
            step_drop >= single_drop
            or span_drop >= max_span
            or (len(clusters[-1]) >= max_size and (step_drop >= 0.25 or span_drop >= 0.5))
        )

        if stat_transition or cs_transition:
            clusters.append([p])
            current_tier_max = curr_pts
            current_cs_tier = p_cs_tier
        else:
            clusters[-1].append(p)

    tiers: list[PlayerTier] = []
    first_tier_hint = sorted_p[0].cheatsheet_tier or sorted_p[0].tier or 1
    for idx, cluster in enumerate(clusters, start=first_tier_hint):
        avg_pts = sum(p.projected_points for p in cluster) / len(cluster)
        tiers.append(
            PlayerTier(
                tier_num=idx,
                position=position,
                players=cluster,
                avg_projected=round(avg_pts, 2),
                count=len(cluster),
            )
        )
    return tiers


MACRO_TIER_BOUNDS: dict[str, list[int]] = {
    "QB": [4, 9, 16, 24],
    "TE": [3, 8, 15, 23],
    "RB": [4, 10, 18, 26, 36, 46, 56],
    "WR": [5, 12, 20, 30, 42, 55, 70],
    "K": [4, 10, 18],
    "D/ST": [4, 10, 18],
}


def assign_global_macro_tiers(all_players: list[Player]) -> None:
    """Assign canonical macro tiers (5 QB/TE, 8 RB/WR, 4 K/DST) based on positional pool rankings."""
    for pos, bounds in MACRO_TIER_BOUNDS.items():
        pos_all = sorted([p for p in all_players if p.position == pos], key=lambda x: x.projected_points, reverse=True)
        for rank_idx, p in enumerate(pos_all, start=1):
            if p.cheatsheet_tier is not None:
                p.tier = p.cheatsheet_tier
                continue
            tier_num = 1
            for b in bounds:
                if rank_idx > b:
                    tier_num += 1
                else:
                    break
            p.tier = tier_num


def cluster_position_tiers(
    players: list[Player], position: str, cheatsheet_context: CheatsheetContext | None = None
) -> list[PlayerTier]:
    """Group available position players into ordered tiers using cheatsheet tiers or statistical clustering."""
    pos_players = [p for p in players if p.position == position]
    if not pos_players:
        return []

    # If players have explicit cheatsheet tier assignments, group directly by cheatsheet tier
    cs_grouped: dict[int, list[Player]] = {}
    unranked: list[Player] = []
    for p in pos_players:
        if p.cheatsheet_tier is not None:
            cs_grouped.setdefault(p.cheatsheet_tier, []).append(p)
        else:
            unranked.append(p)

    if cs_grouped:
        tiers: list[PlayerTier] = []
        for t_num in sorted(cs_grouped.keys()):
            plist = sorted(cs_grouped[t_num], key=lambda x: x.projected_points, reverse=True)
            avg = sum(p.projected_points for p in plist) / len(plist)
            tiers.append(
                PlayerTier(
                    tier_num=t_num,
                    position=position,
                    players=plist,
                    avg_projected=round(avg, 2),
                    count=len(plist),
                )
            )
        if unranked:
            next_t = max(cs_grouped.keys()) + 1
            unranked_sorted = sorted(unranked, key=lambda x: x.projected_points, reverse=True)
            avg = sum(p.projected_points for p in unranked_sorted) / len(unranked_sorted)
            tiers.append(
                PlayerTier(
                    tier_num=next_t,
                    position=position,
                    players=unranked_sorted,
                    avg_projected=round(avg, 2),
                    count=len(unranked_sorted),
                )
            )
        return tiers

    if any(p.tier > 1 for p in pos_players):
        tier_grouped: dict[int, list[Player]] = {}
        for p in pos_players:
            tier_grouped.setdefault(p.tier, []).append(p)
        macro_tiers: list[PlayerTier] = []
        for t_num in sorted(tier_grouped.keys()):
            plist = sorted(tier_grouped[t_num], key=lambda x: x.projected_points, reverse=True)
            avg = sum(p.projected_points for p in plist) / len(plist)
            macro_tiers.append(
                PlayerTier(
                    tier_num=t_num,
                    position=position,
                    players=plist,
                    avg_projected=round(avg, 2),
                    count=len(plist),
                )
            )
        return macro_tiers

    return _cluster_hybrid(pos_players, position)


def calculate_tier_drop(current_tier: PlayerTier, next_tier: PlayerTier | None) -> float:
    """Compute average projection drop between current and subsequent tier."""
    if not next_tier or next_tier.avg_projected <= 0.0:
        return 0.0
    return max(0.0, round(current_tier.avg_projected - next_tier.avg_projected, 1))


def _evaluate_on_the_clock_cliff(
    tier: PlayerTier,
    next_tier: PlayerTier | None,
    snake_turn_gap: int,
    current_pick: int = 1,
) -> TierCliffWarning | None:
    """Evaluate cliff risk when user is currently on the clock."""
    drop = calculate_tier_drop(tier, next_tier)
    min_drop = 0.9 if current_pick <= 40 else (0.7 if current_pick <= 80 else 0.5)
    if drop < min_drop:
        return None

    remaining = len(tier.players)
    tier_size = max(remaining, tier.count)

    # 1. Never alert on a full, undrained tier with multiple players (0 players drafted)
    if remaining >= tier_size and remaining > 1:
        return None

    # 2. Dynamic ADP Proximity: do not alert if tier is far out of draft range for current pick
    adps = [p.adp for p in tier.players if p.adp is not None]
    if adps:
        avg_adp = sum(adps) / len(adps)
        if avg_adp > current_pick + 8 and remaining > 1:
            return None

    # 3. Require true tier depletion (drained tier or down to the last player)
    is_drained = (remaining < tier_size and remaining <= 2) or ((remaining / tier_size) <= 0.50) or remaining == 1
    is_gap_scarce = remaining <= max(2, (snake_turn_gap + 2) // 3)

    if not (is_drained and is_gap_scarce):
        return None

    risk = "CRITICAL" if (remaining == 1 or drop >= 2.5) else ("HIGH" if remaining <= 2 else "MODERATE")
    next_num = next_tier.tier_num if next_tier else tier.tier_num + 1
    action = (
        f"Only {remaining} of {tier_size} Tier {tier.tier_num} {tier.position} remaining "
        f"before a {snake_turn_gap}-pick turn gap. Draft now to avoid dropping -{drop:.1f} pts to Tier {next_num}."
    )

    return TierCliffWarning(
        position=tier.position,
        current_tier=tier.tier_num,
        players_remaining=remaining,
        picks_until_turn=0,
        snake_turn_gap=snake_turn_gap,
        cliff_risk=risk,
        cliff_type=CliffType.ON_THE_CLOCK_CLIFF,
        next_tier_drop_points=drop,
        recommended_action=action,
    )


def _evaluate_waiting_cliff(
    tier: PlayerTier,
    next_tier: PlayerTier | None,
    picks_until_turn: int,
    snake_turn_gap: int,
    current_pick: int = 1,
) -> TierCliffWarning | None:
    """Evaluate cliff risk when user is waiting for upcoming pick with dynamic ADP proximity."""
    drop = calculate_tier_drop(tier, next_tier)
    min_drop = 0.9 if current_pick <= 40 else (0.7 if current_pick <= 80 else 0.5)
    if drop < min_drop:
        return None

    remaining = len(tier.players)
    tier_size = max(remaining, tier.count)

    # 1. At draft start (Picks 1-3), never alert on a full, untouched tier with 3+ players
    if current_pick <= 3 and remaining >= tier_size and remaining >= 3:
        return None

    # 2. Dynamic ADP Proximity: check if this tier is expected to be drafted in the upcoming window
    adps = [p.adp for p in tier.players if p.adp is not None]
    if adps:
        avg_adp = sum(adps) / len(adps)
        in_draft_range = avg_adp <= current_pick + picks_until_turn + snake_turn_gap + 5
        if not in_draft_range:
            return None

    # 3. Only alert for actionable upcoming turn cliffs (tier survives to your pick but depletes during turn gap)
    survives_to_turn = remaining > picks_until_turn
    wipes_in_gap = remaining <= (picks_until_turn + max(2, (snake_turn_gap + 2) // 3))

    if survives_to_turn and wipes_in_gap and snake_turn_gap >= 4:
        risk = "HIGH" if remaining <= picks_until_turn + 1 else "MODERATE"
        action = (
            f"{remaining} of {tier_size} Tier {tier.tier_num} {tier.position} left. Tier will survive to your pick in {picks_until_turn} turns "
            f"but will deplete during your {snake_turn_gap}-pick turn gap. Target Tier {tier.tier_num} at your upcoming pick."
        )
        return TierCliffWarning(
            position=tier.position,
            current_tier=tier.tier_num,
            players_remaining=remaining,
            picks_until_turn=picks_until_turn,
            snake_turn_gap=snake_turn_gap,
            cliff_risk=risk,
            cliff_type=CliffType.UPCOMING_TURN_CLIFF,
            next_tier_drop_points=drop,
            recommended_action=action,
        )

    return None


def detect_tier_cliffs(
    tiers_by_pos: dict[str, list[PlayerTier]],
    picks_until_turn: int,
    snake_turn_gap: int,
    is_on_the_clock: bool,
    current_pick: int = 1,
    user_roster_counts: dict[str, int] | None = None,
    cheatsheet_context: CheatsheetContext | None = None,
) -> list[TierCliffWarning]:
    """Identify 3-scenario positional tier cliffs across available player tiers."""
    warnings: list[TierCliffWarning] = []
    risk_rank = {"CRITICAL": 0, "HIGH": 1, "MODERATE": 2, "LOW": 3}
    roster = user_roster_counts or {}
    rules = cheatsheet_context.strategy_rules if cheatsheet_context else []
    has_2qb_rule = any("one from tier 4" in r.lower() or "two qb" in r.lower() or "2nd qb" in r.lower() for r in rules)

    for pos, tiers in tiers_by_pos.items():
        pos_upper = pos.upper()
        # Suppress single-starter positions if user already filled their starting spot (unless 2nd QB rule active in late rounds)
        if (
            pos_upper == "QB"
            and roster.get("QB", 0) >= 1
            and not (has_2qb_rule and current_pick >= 80 and roster.get("QB", 0) < 2)
        ):
            continue
        if pos_upper == "TE" and roster.get("TE", 0) >= 1:
            continue
        if pos_upper in ("K", "D/ST", "DEF", "DST") and roster.get(pos_upper, 0) >= 1:
            continue

        active_tiers = [t for t in tiers if len(t.players) > 0]
        if not active_tiers:
            continue

        top_tier = active_tiers[0]
        next_tier = active_tiers[1] if len(active_tiers) > 1 else None

        warning: TierCliffWarning | None = None
        if is_on_the_clock or picks_until_turn <= 0:
            warning = _evaluate_on_the_clock_cliff(top_tier, next_tier, snake_turn_gap, current_pick=current_pick)
        else:
            warning = _evaluate_waiting_cliff(top_tier, next_tier, picks_until_turn, snake_turn_gap, current_pick)

        if warning:
            warnings.append(warning)

    warnings.sort(key=lambda w: (risk_rank.get(w.cliff_risk, 4), -w.next_tier_drop_points))
    return warnings
