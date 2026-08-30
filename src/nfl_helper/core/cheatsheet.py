"""Cheatsheet coordinator and player metadata attachment engine."""

from nfl_helper.core.cheatsheet_csv import parse_csv_cheatsheet
from nfl_helper.core.cheatsheet_json import parse_json_cheatsheet
from nfl_helper.core.cheatsheet_text import parse_plain_text_cheatsheet
from nfl_helper.core.name_normalizer import normalize_player_name
from nfl_helper.models.cheatsheet import (
    CheatsheetContext,
    CheatsheetEntry,
    DraftRoundTarget,
    PositionalQuotaDeadline,
    PositionalStrategyRule,
)
from nfl_helper.models.player import Player

_DIRECTIONAL_NOTE_KEYWORDS = {"sleeper", "bust", "breakout", "fade", "avoid"}


def _is_directional_tag(segment: str) -> bool:
    """Identify if a note segment is a directional fantasy classification tag (e.g. ESPN Bust, Sleeper)."""
    s = segment.lower().strip()
    words = s.split()
    return len(words) <= 3 and any(
        k in words or any(w.endswith(k) or w.startswith(k) for w in words) for k in _DIRECTIONAL_NOTE_KEYWORDS
    )


def _merge_player_notes(existing: str | None, new_note: str | None) -> str:
    """Merge player notes across layers, allowing newer directional tags (Sleeper/Bust/Breakout) to replace older ones."""
    if not existing:
        return new_note or ""
    if not new_note:
        return existing

    exist_segments = [s.strip() for s in existing.split(";") if s.strip()]
    new_segments = [s.strip() for s in new_note.split(";") if s.strip()]

    new_has_directional = any(_is_directional_tag(ns) for ns in new_segments)

    filtered_existing = []
    for es in exist_segments:
        if new_has_directional and _is_directional_tag(es):
            continue
        if es not in new_segments:
            filtered_existing.append(es)

    combined = filtered_existing + [ns for ns in new_segments if ns not in filtered_existing]
    return "; ".join(combined)


def parse_cheatsheet_content(content: str, sheet_name: str | None = None) -> CheatsheetContext:
    """Auto-detect content format (JSON, CSV, or plain-text) and parse accordingly."""
    cleaned = content.strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            return parse_json_cheatsheet(cleaned)
        except Exception:
            pass
    if "," in cleaned and any(h in cleaned.lower() for h in ("player,", "position,", "tier,")):
        try:
            return parse_csv_cheatsheet(cleaned)
        except Exception:
            pass
    return parse_plain_text_cheatsheet(cleaned, sheet_name=sheet_name)


def apply_cheatsheet_context(players: list[Player], context: CheatsheetContext) -> list[Player]:
    """Attach cheatsheet notes, tiers, and flags without overwriting raw projections."""
    if not context or not context.entries:
        return players

    for player in players:
        norm = normalize_player_name(player.name)
        entry = context.entries.get(norm)

        # If not exact match, check partial/last-name match with strict team and position validation
        if not entry:
            for k, e in context.entries.items():
                pos_match = not e.position or e.position == (
                    player.position.value if hasattr(player.position, "value") else str(player.position)
                )
                team_match = not e.team or not player.team or e.team == player.team
                if pos_match and team_match and (norm.endswith(k) or k in norm):
                    entry = e
                    break

        if entry:
            player.cheatsheet_tier = entry.tier
            if entry.adp is not None:
                player.cheatsheet_rank = int(entry.adp)
                if player.adp is not None:
                    # 80% platform consensus ADP + 20% cheatsheet ADP
                    player.adp = round(0.80 * player.adp + 0.20 * entry.adp, 1)
                else:
                    player.adp = entry.adp
            if entry.notes:
                player.cheatsheet_notes = entry.notes
            if entry.is_injured:
                player.injury_status = "IR"
                if not player.cheatsheet_notes:
                    player.cheatsheet_notes = "Injured (multi-week recovery / out a while)"

    return players


def merge_cheatsheet_contexts(contexts: list[CheatsheetContext]) -> CheatsheetContext:
    """Consolidate multiple active cheatsheet contexts into a single unified context."""
    valid_contexts = [c for c in contexts if c is not None]
    if not valid_contexts:
        return CheatsheetContext()
    if len(valid_contexts) == 1:
        return valid_contexts[0].model_copy(deep=True)

    merged_entries: dict[str, CheatsheetEntry] = {}
    merged_rules: list[str] = []
    seen_rules: set[str] = set()
    merged_round_targets: list[DraftRoundTarget] = []
    seen_round_keys: set[tuple[tuple[int, ...], tuple[str, ...]]] = set()
    merged_pos_strategy: list[PositionalStrategyRule] = []
    seen_pos_keys: set[tuple[str, tuple[int, ...]]] = set()
    merged_deadlines: list[PositionalQuotaDeadline] = []
    seen_deadline_keys: set[tuple[str, int, int]] = set()
    merged_positional_tiers: dict[str, list[list[str]]] = {}

    for ctx in valid_contexts:
        for key, entry in ctx.entries.items():
            if key not in merged_entries:
                merged_entries[key] = entry.model_copy()
            else:
                existing = merged_entries[key]
                if entry.notes:
                    existing.notes = _merge_player_notes(existing.notes, entry.notes)
                if entry.tier is not None:
                    existing.tier = entry.tier
                if entry.adp is not None:
                    existing.adp = entry.adp
                existing.is_injured = existing.is_injured or entry.is_injured
                if not existing.position and entry.position:
                    existing.position = entry.position
                if not existing.team and entry.team:
                    existing.team = entry.team

        for rule in ctx.strategy_rules:
            if rule not in seen_rules:
                seen_rules.add(rule)
                merged_rules.append(rule)

        for rt in ctx.round_targets:
            rkey = (tuple(rt.target_rounds), tuple(rt.allowed_positions))
            if rkey not in seen_round_keys:
                seen_round_keys.add(rkey)
                merged_round_targets.append(rt.model_copy())

        for ps in ctx.positional_strategy:
            pkey = (ps.position, tuple(ps.target_rounds))
            if pkey not in seen_pos_keys:
                seen_pos_keys.add(pkey)
                merged_pos_strategy.append(ps.model_copy())

        for qd in ctx.quota_deadlines:
            dkey = (qd.position, qd.required_count, qd.deadline_round)
            if dkey not in seen_deadline_keys:
                seen_deadline_keys.add(dkey)
                merged_deadlines.append(qd.model_copy())

        for pos, tiers in ctx.positional_tiers.items():
            merged_positional_tiers[pos] = [list(t) for t in tiers]

    return CheatsheetContext(
        entries=merged_entries,
        strategy_rules=merged_rules,
        round_targets=merged_round_targets,
        positional_strategy=merged_pos_strategy,
        quota_deadlines=merged_deadlines,
        positional_tiers=merged_positional_tiers,
    )
