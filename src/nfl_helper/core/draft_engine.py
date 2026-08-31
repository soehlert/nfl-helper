"""Deterministic live draft engine coordinator and state constructor."""

from nfl_helper.core.draft_scoring import generate_draft_suggestions
from nfl_helper.core.lookahead import (
    calculate_lookahead,
    calculate_snake_pick_owner,
)
from nfl_helper.core.tier_calculator import (
    assign_global_macro_tiers,
    cluster_position_tiers,
    detect_tier_cliffs,
)
from nfl_helper.core.vorp import calculate_vorp_baselines
from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.draft import DraftPick, DraftState, PlayerTier
from nfl_helper.models.player import Player, Position


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
    user_team_id: str | None = None,
) -> DraftState:
    """Construct full DraftState snapshot with snake lookahead, tiers, cliffs, and suggestions."""
    # Ensure canonical macro tiers across the full player pool
    assign_global_macro_tiers(all_players)

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
        pos_all = [p for p in all_players if p.position == pos]
        all_clustered = cluster_position_tiers(pos_all, pos, cheatsheet_context)
        for t in all_clustered:
            for p in t.players:
                p.tier = t.tier_num

        pos_avail = [p for p in available_players if p.position == pos]
        avail_by_pos[pos] = pos_avail
        clustered = cluster_position_tiers(pos_avail, pos, cheatsheet_context)
        tiers_by_pos[pos] = clustered

    user_picks: list[DraftPick] = []
    for pick in recent_picks:
        pick_owner_slot = calculate_snake_pick_owner(pick.overall_pick, total_teams)
        if user_draft_slot:
            is_user_pick = pick_owner_slot == user_draft_slot
        else:
            is_user_pick = bool(
                (user_team_id and pick.team_id and str(pick.team_id) == str(user_team_id))
                or (user_team_id and pick.team_name and str(user_team_id).lower() in pick.team_name.lower())
            )
        if is_user_pick:
            user_picks.append(pick)

    user_roster_counts: dict[str, int] = {}
    for pick in user_picks:
        p_pos = (pick.position or "").upper()
        if p_pos in ("DEF", "DST", "D/ST"):
            p_pos = "D/ST"
        if p_pos:
            user_roster_counts[p_pos] = user_roster_counts.get(p_pos, 0) + 1

    # Resolve user_drafted_players with attached tiers and notes
    players_by_id = {p.id: p for p in all_players}
    players_by_name = {p.name.lower(): p for p in all_players}
    user_drafted_players: list[Player] = []
    for pick in user_picks:
        matched = players_by_id.get(pick.player_id) or (
            players_by_name.get(pick.player_name.lower()) if pick.player_name else None
        )
        if matched:
            user_drafted_players.append(matched)
        else:
            raw_pos = (pick.position or "").upper()
            pos_enum = (
                Position.QB
                if raw_pos == "QB"
                else (
                    Position.RB
                    if raw_pos == "RB"
                    else (
                        Position.WR
                        if raw_pos == "WR"
                        else (Position.TE if raw_pos == "TE" else (Position.K if raw_pos == "K" else Position.DST))
                    )
                )
            )
            cs_entry = (
                cheatsheet_context.entries.get(pick.player_name.lower())
                if cheatsheet_context and pick.player_name
                else None
            )
            user_drafted_players.append(
                Player(
                    id=pick.player_id,
                    name=pick.player_name,
                    position=pos_enum,
                    team="NFL",
                    projected_points=10.0,
                    cheatsheet_tier=cs_entry.tier if cs_entry else None,
                )
            )

    # Calculate completed / capped positions
    capped_pos: set[str] = set()
    active_rules = cheatsheet_context.strategy_rules if cheatsheet_context else []
    if cheatsheet_context:
        for pr in cheatsheet_context.positional_strategy:
            if pr.position == "QB" and (pr.no_second_if_top_tier or 1 in pr.conditional_max_count):
                for dp in user_drafted_players or []:
                    if str(dp.position).upper() == "QB":
                        capped_pos.add("QB")
            elif pr.position == "TE" and (pr.no_second_if_top_tier or 1 in pr.conditional_max_count):
                for dp in user_drafted_players or []:
                    if str(dp.position).upper() == "TE":
                        capped_pos.add("TE")

    if any("only one qb" in r.lower() for r in active_rules) and user_roster_counts.get("QB", 0) >= 1:
        capped_pos.add("QB")
    if any("no second te" in r.lower() for r in active_rules) and user_roster_counts.get("TE", 0) >= 1:
        capped_pos.add("TE")

    if user_roster_counts.get("QB", 0) >= 2:
        capped_pos.add("QB")
    if user_roster_counts.get("TE", 0) >= 2:
        capped_pos.add("TE")
    if user_roster_counts.get("K", 0) >= 1:
        capped_pos.add("K")
    if user_roster_counts.get("D/ST", 0) >= 1:
        capped_pos.add("D/ST")

    cliffs = detect_tier_cliffs(
        tiers_by_pos,
        picks_until_turn,
        turn_gap,
        on_the_clock,
        current_pick=overall_pick,
        user_roster_counts=user_roster_counts,
        cheatsheet_context=cheatsheet_context,
        user_drafted_players=user_drafted_players,
    )

    next_user_pick = overall_pick + turn_gap + 1 if on_the_clock else overall_pick + picks_until_turn + turn_gap + 1

    current_round = min(total_rounds, (overall_pick - 1) // total_teams + 1)
    strategy_alerts: list[str] = []
    if cheatsheet_context:
        for qd in cheatsheet_context.quota_deadlines:
            curr_cnt = user_roster_counts.get(qd.position, 0)
            if curr_cnt < qd.required_count and current_round <= qd.deadline_round:
                rounds_left = qd.deadline_round - current_round + 1
                needed = qd.required_count - curr_cnt
                if rounds_left <= needed + 2:
                    strategy_alerts.append(
                        f"⚠️ Quota Deadline Alert: Need {needed} more {qd.position}s across next {rounds_left} rounds to meet Round {qd.deadline_round} deadline ({curr_cnt}/{qd.required_count} drafted)."
                    )
        for rt in cheatsheet_context.round_targets:
            if current_round in rt.target_rounds:
                window_end = max(rt.target_rounds)
                rounds_left_window = window_end - current_round + 1
                for pos_req, min_cnt in rt.min_counts.items():
                    curr_cnt = user_roster_counts.get(pos_req, 0)
                    if curr_cnt < min_cnt and rounds_left_window <= (min_cnt - curr_cnt):
                        drafted_other = [
                            f"{cnt} {pos}" for pos, cnt in user_roster_counts.items() if pos != pos_req and cnt > 0
                        ]
                        other_text = f" (Drafted {', '.join(drafted_other)})" if drafted_other else ""
                        strategy_alerts.append(
                            f"⚠️ Strategy Rule Focus: Rule 'Rounds {min(rt.target_rounds)}-{window_end}' prioritizes {pos_req} in Round {current_round}{other_text} ({curr_cnt}/{min_cnt} drafted)."
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
        total_rounds=total_rounds,
        user_drafted_players=user_drafted_players,
        next_user_pick=next_user_pick,
    )

    is_complete = overall_pick > (total_teams * total_rounds)
    if is_complete:
        picks_until_turn = 0
        turn_gap = 0
        on_the_clock = False
        cliffs = []

    return DraftState(
        league_id=league_id,
        draft_id=draft_id,
        is_complete=is_complete,
        total_rounds=total_rounds,
        total_teams=total_teams,
        current_pick=min(total_teams * total_rounds, overall_pick) if is_complete else overall_pick,
        current_round=current_round,
        user_drafted_roster_counts=user_roster_counts,
        user_draft_slot=user_draft_slot,
        user_team_id=user_team_id,
        picks_until_user_turn=picks_until_turn,
        snake_turn_gap=turn_gap,
        is_user_on_the_clock=on_the_clock,
        capped_positions=sorted(capped_pos),
        recent_picks=recent_picks[-10:] if len(recent_picks) > 10 else recent_picks,
        available_players_by_pos=avail_by_pos,
        tiers_by_position=tiers_by_pos,
        cliff_warnings=cliffs,
        top_suggestions=suggestions,
        strategy_alerts=strategy_alerts,
    )
