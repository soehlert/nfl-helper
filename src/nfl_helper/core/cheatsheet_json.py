"""JSON cheatsheet format parser."""

import json

from nfl_helper.core.name_normalizer import normalize_player_name
from nfl_helper.core.strategy_parser import parse_strategy_rule
from nfl_helper.models.cheatsheet import CheatsheetContext, CheatsheetEntry


def parse_json_cheatsheet(json_text: str) -> CheatsheetContext:
    """Parse structured JSON cheatsheet with embedded players and strategy rules."""
    data = json.loads(json_text)
    entries: dict[str, CheatsheetEntry] = {}
    rules = list(data.get("strategy_rules", []))
    context = CheatsheetContext(entries=entries, strategy_rules=rules)

    for r_str in rules:
        rnd_tgt, pos_tgt, dl_quota = parse_strategy_rule(r_str)
        if rnd_tgt:
            context.round_targets.append(rnd_tgt)
        if pos_tgt:
            context.positional_strategy.append(pos_tgt)
        if dl_quota:
            context.quota_deadlines.append(dl_quota)

    for p in data.get("players", []):
        name = str(p.get("name", ""))
        if not name:
            continue
        norm_name = normalize_player_name(name)
        pos_str = str(p.get("position", "")).upper()
        tier_val = int(p.get("tier", 1))
        entry = CheatsheetEntry(
            player_name=name,
            normalized_name=norm_name,
            position=pos_str,
            team=str(p.get("team", "")).upper(),
            tier=tier_val,
            adp=float(p.get("adp")) if p.get("adp") is not None else None,
            notes=p.get("notes"),
        )
        entries[norm_name] = entry
        norm_full = normalize_player_name(entry.player_name)
        if norm_full != entry.normalized_name:
            entries[norm_full] = entry

        if pos_str and tier_val > 0:
            if pos_str not in context.positional_tiers:
                context.positional_tiers[pos_str] = []
            while len(context.positional_tiers[pos_str]) < tier_val:
                context.positional_tiers[pos_str].append([])
            context.positional_tiers[pos_str][tier_val - 1].append(entry.player_name)

    context.entries = entries
    return context
