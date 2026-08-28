"""Cheatsheet dry-run diff engine for comparing candidate cheatsheets against active baseline."""

from nfl_helper.core.cheatsheet import apply_cheatsheet_context
from nfl_helper.core.draft_engine import build_draft_state
from nfl_helper.models.cheatsheet import CheatsheetContext
from nfl_helper.models.diff import CheatsheetDiffReport, PlayerMover
from nfl_helper.models.player import Player


def compute_cheatsheet_diff(
    active_context: CheatsheetContext | None,
    candidate_context: CheatsheetContext,
    player_pool: list[Player],
    top_n: int = 5,
) -> CheatsheetDiffReport:
    """Compute dry-run mover analytics and rule deltas without database mutations."""
    # 1. Evaluate baseline board state (pure player board rankings)
    base_pool = apply_cheatsheet_context([p.model_copy() for p in player_pool], active_context)
    base_ctx_board = (
        active_context.model_copy(update={"strategy_rules": [], "round_targets": [], "positional_strategy": []})
        if active_context
        else None
    )
    base_state = build_draft_state(
        league_id="dry_run_base",
        draft_id=None,
        overall_pick=1,
        user_draft_slot=1,
        total_teams=10,
        total_rounds=15,
        recent_picks=[],
        all_players=base_pool,
        cheatsheet_context=base_ctx_board,
    )
    base_map = {s.player.id: (s.rank, s.player) for s in base_state.top_suggestions}

    # 2. Evaluate candidate board state (pure player board rankings)
    cand_pool = apply_cheatsheet_context([p.model_copy() for p in player_pool], candidate_context)
    cand_ctx_board = candidate_context.model_copy(
        update={"strategy_rules": [], "round_targets": [], "positional_strategy": []}
    )
    cand_state = build_draft_state(
        league_id="dry_run_cand",
        draft_id=None,
        overall_pick=1,
        user_draft_slot=1,
        total_teams=10,
        total_rounds=15,
        recent_picks=[],
        all_players=cand_pool,
        cheatsheet_context=cand_ctx_board,
    )
    cand_map = {s.player.id: (s.rank, s.player) for s in cand_state.top_suggestions}

    # 3. Calculate rank shifts and tier transitions for all tracked players
    all_player_ids = set(base_map.keys()) | set(cand_map.keys())
    movers: list[PlayerMover] = []

    for pid in all_player_ids:
        b_rank, b_player = base_map.get(pid, (len(base_map) + 1, None))
        c_rank, c_player = cand_map.get(pid, (len(cand_map) + 1, None))
        player = c_player or b_player
        if not player:
            continue

        rank_delta = b_rank - c_rank  # Positive = improved rank (e.g. 40 -> 25 is +15)
        old_tier = b_player.tier if b_player else None
        new_tier = c_player.tier if c_player else None
        tier_delta = (old_tier - new_tier) if (old_tier and new_tier) else None

        b_inj = b_player.injury_status not in ("ACTIVE", "Healthy") if b_player else False
        c_inj = c_player.injury_status not in ("ACTIVE", "Healthy") if c_player else False
        injury_changed = b_inj != c_inj

        mover_note = player.cheatsheet_notes or ""
        if not mover_note and rank_delta < 0:
            mover_note = "Displaced by Risers"
        elif not mover_note and rank_delta > 0:
            mover_note = "Cheatsheet Rank"

        if rank_delta != 0 or tier_delta != 0 or injury_changed:
            movers.append(
                PlayerMover(
                    player_name=player.name,
                    position=player.position.value if hasattr(player.position, "value") else str(player.position),
                    team=player.team,
                    old_rank=b_rank,
                    new_rank=c_rank,
                    rank_delta=rank_delta,
                    old_tier=old_tier,
                    new_tier=new_tier,
                    tier_delta=tier_delta,
                    is_injury_update=injury_changed,
                    note=mover_note,
                )
            )

    # 4. Filter Top N Risers and Fallers
    risers = sorted([m for m in movers if m.rank_delta > 0], key=lambda x: x.rank_delta, reverse=True)[:top_n]
    fallers = sorted([m for m in movers if m.rank_delta < 0], key=lambda x: x.rank_delta)[:top_n]
    tier_upgrades = sorted(
        [m for m in movers if m.tier_delta and m.tier_delta > 0], key=lambda x: x.tier_delta, reverse=True
    )
    tier_downgrades = sorted([m for m in movers if m.tier_delta and m.tier_delta < 0], key=lambda x: x.tier_delta)

    # 5. Rule deltas
    old_rules = set(active_context.strategy_rules) if active_context else set()
    new_rules = set(candidate_context.strategy_rules)
    added_rules = [r for r in candidate_context.strategy_rules if r not in old_rules]
    removed_rules = [r for r in (active_context.strategy_rules if active_context else []) if r not in new_rules]

    return CheatsheetDiffReport(
        top_risers=risers,
        top_fallers=fallers,
        tier_upgrades=tier_upgrades,
        tier_downgrades=tier_downgrades,
        added_rules=added_rules,
        removed_rules=removed_rules,
        total_players_affected=len(movers),
        total_rules_affected=len(added_rules) + len(removed_rules),
    )
