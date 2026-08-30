"""CSV cheatsheet format parser."""

import csv
import io

from nfl_helper.core.name_normalizer import normalize_player_name
from nfl_helper.core.strategy_parser import parse_strategy_rule
from nfl_helper.models.cheatsheet import CheatsheetContext, CheatsheetEntry


def _record_player_entry(entry: CheatsheetEntry, pos_key: str, context: CheatsheetContext) -> None:
    """Store parsed player entry and index into positional tier list."""
    context.entries[entry.normalized_name] = entry
    norm_full = normalize_player_name(entry.player_name)
    if norm_full != entry.normalized_name:
        context.entries[norm_full] = entry
    if pos_key and entry.tier is not None and entry.tier > 0:
        if pos_key not in context.positional_tiers:
            context.positional_tiers[pos_key] = []
        while len(context.positional_tiers[pos_key]) < entry.tier:
            context.positional_tiers[pos_key].append([])
        context.positional_tiers[pos_key][entry.tier - 1].append(entry.player_name)


def parse_csv_cheatsheet(csv_text: str) -> CheatsheetContext:
    """Parse CSV cheatsheet with optional strategy rule comment headers (#)."""
    context = CheatsheetContext()
    csv_lines: list[str] = []

    for raw_line in csv_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            rule_str = line.lstrip("#").strip()
            if rule_str:
                context.strategy_rules.append(rule_str)
                rnd_tgt, pos_tgt, dl_quota = parse_strategy_rule(rule_str)
                if rnd_tgt:
                    context.round_targets.append(rnd_tgt)
                if pos_tgt:
                    context.positional_strategy.append(pos_tgt)
                if dl_quota:
                    context.quota_deadlines.append(dl_quota)
            continue

        csv_lines.append(raw_line)

    if not csv_lines:
        return context

    reader = csv.DictReader(io.StringIO("\n".join(csv_lines)))
    for row in reader:
        norm_row = {k.lower().strip(): (v.strip() if v else "") for k, v in row.items() if k}
        name = norm_row.get("player") or norm_row.get("name", "")
        if not name:
            continue

        norm_name = normalize_player_name(name)
        tier_val = int(norm_row.get("tier", 1)) if norm_row.get("tier", "").isdigit() else 1
        adp_raw = norm_row.get("adp", "")
        try:
            adp_val = float(adp_raw) if adp_raw else None
        except ValueError:
            adp_val = None

        pos_str = norm_row.get("position", "").upper()
        entry = CheatsheetEntry(
            player_name=name,
            normalized_name=norm_name,
            position=pos_str,
            team=norm_row.get("team", "").upper(),
            tier=tier_val,
            adp=adp_val,
            notes=norm_row.get("notes"),
        )
        _record_player_entry(entry, pos_str, context)

    return context
