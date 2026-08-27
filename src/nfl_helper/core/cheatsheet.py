"""Cheatsheet and custom ranking context ingestion engine."""

import csv
import io
import json
import re

from nfl_helper.core.name_normalizer import normalize_player_name
from nfl_helper.models.cheatsheet import (
    CheatsheetContext,
    CheatsheetEntry,
    DraftRoundTarget,
    PositionalStrategyRule,
)
from nfl_helper.models.player import Player

POSITION_HEADERS = {"QB", "RB", "WR", "TE", "K", "DEF", "DST", "D/ST"}
LINE_PATTERN = re.compile(r"^([A-Za-z\s\-\.'\*]+?)\s+([A-Z]{2,3})\s+(\d+\.?\d*)$")


def _clean_position_header(raw_header: str) -> str | None:
    """Extract standard position from header line (e.g. 'RB con't' -> 'RB')."""
    tokens = raw_header.strip().split()
    if not tokens:
        return None
    candidate = tokens[0].upper()
    if candidate in POSITION_HEADERS:
        return "DST" if candidate in ("DEF", "D/ST") else candidate
    return None


def _parse_player_line(line: str, current_pos: str, current_tier: int) -> CheatsheetEntry | None:
    """Parse a line in format 'PlayerName Team ADP' into a CheatsheetEntry."""
    match = LINE_PATTERN.match(line.strip())
    if not match:
        if "-" in line or ":" in line:
            parts = re.split(r"[-:]", line, maxsplit=1)
            name = parts[0].strip()
            notes = parts[1].strip() if len(parts) > 1 else None
            is_inj = "*" in name
            clean_name = name.replace("*", "").strip()
            return CheatsheetEntry(
                player_name=clean_name,
                normalized_name=normalize_player_name(clean_name),
                position=current_pos,
                tier=current_tier,
                is_injured=is_inj,
                notes=notes,
            )
        return None

    raw_name, team, adp_str = match.groups()
    clean_name = raw_name.replace("*", "").strip()

    return CheatsheetEntry(
        player_name=clean_name,
        normalized_name=normalize_player_name(clean_name),
        position=current_pos,
        team=team.upper(),
        tier=current_tier,
        adp=float(adp_str),
        is_injured="*" in raw_name,
    )


def _parse_strategy_rule(line: str) -> tuple[DraftRoundTarget | None, PositionalStrategyRule | None]:
    """Parse natural language draft strategy rules into structured target models."""
    norm_line = line.strip()
    round_target = None
    pos_target = None

    # E.g. "Rounds 1-2 - Only RB/WR at least 1 RB"
    if norm_line.lower().startswith("rounds"):
        rounds_match = re.search(r"rounds?\s+(\d+)(?:\s*-\s*(\d+))?", norm_line, re.IGNORECASE)
        if rounds_match:
            start_rnd = int(rounds_match.group(1))
            end_rnd = int(rounds_match.group(2)) if rounds_match.group(2) else start_rnd
            target_rounds = list(range(start_rnd, end_rnd + 1))
            allowed = [pos for pos in ("RB", "WR", "QB", "TE") if pos in norm_line.upper()]
            min_counts = {"RB": 1} if "at least 1 rb" in norm_line.lower() else {}
            round_target = DraftRoundTarget(
                target_rounds=target_rounds,
                allowed_positions=allowed,
                min_counts=min_counts,
                rule_description=norm_line,
            )
    # E.g. "TE - Target the top 4 in rounds 3-5 or take 2 from tiers 2-4"
    elif any(norm_line.upper().startswith(f"{pos} -") for pos in ("TE", "QB", "RB", "WR")):
        pos = norm_line[:2].upper()
        top_n = 4 if "top 4" in norm_line.lower() else None
        target_tiers = [2, 3, 4] if "tiers 2-4" in norm_line.lower() else []
        pos_target = PositionalStrategyRule(
            position=pos,
            target_rounds=[3, 4, 5] if "3-5" in norm_line else [],
            target_tiers=target_tiers,
            top_n_target=top_n,
            rule_description=norm_line,
        )

    return round_target, pos_target


def _record_player_entry(entry: CheatsheetEntry, pos_key: str, context: CheatsheetContext) -> None:
    """Store parsed player entry and index into positional tier list."""
    context.entries[entry.normalized_name] = entry
    if pos_key:
        if pos_key not in context.positional_tiers:
            context.positional_tiers[pos_key] = []
        while len(context.positional_tiers[pos_key]) < entry.tier:
            context.positional_tiers[pos_key].append([])
        context.positional_tiers[pos_key][entry.tier - 1].append(entry.player_name)


def parse_plain_text_cheatsheet(text: str) -> CheatsheetContext:
    """Parse plain-text cheatsheet tracking blank-line tiers, ADPs, and strategy rules."""
    context = CheatsheetContext()
    current_pos = ""
    current_tier = 1
    previous_was_blank = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current_pos and not previous_was_blank:
                current_tier += 1
            previous_was_blank = True
            continue

        previous_was_blank = False

        pos_header = _clean_position_header(line)
        if pos_header and len(line.split()) <= 3 and not re.search(r"\d", line):
            current_pos = pos_header
            current_tier = 1
            continue

        if any(line.startswith(prefix) for prefix in ("Rounds", "TE -", "QB -", "RB -", "WR -", "* =", "Strategy:")):
            context.strategy_rules.append(line)
            rnd_rule, pos_rule = _parse_strategy_rule(line)
            if rnd_rule:
                context.round_targets.append(rnd_rule)
            if pos_rule:
                context.positional_strategy.append(pos_rule)
            continue

        entry = _parse_player_line(line, current_pos, current_tier)
        if entry and entry.normalized_name:
            _record_player_entry(entry, entry.position or current_pos, context)

    return context


def parse_csv_cheatsheet(csv_text: str) -> CheatsheetContext:
    """Parse CSV format cheatsheet with Player, Position, Team, Tier, ADP, Notes headers."""
    entries: dict[str, CheatsheetEntry] = {}
    reader = csv.DictReader(io.StringIO(csv_text.strip()))

    for row in reader:
        norm_row = {k.lower().strip(): v.strip() for k, v in row.items() if k}
        name = norm_row.get("player") or norm_row.get("name", "")
        if not name:
            continue

        norm_name = normalize_player_name(name)
        tier_val = int(norm_row.get("tier", 1)) if norm_row.get("tier", "").isdigit() else 1
        adp_val = float(norm_row.get("adp")) if norm_row.get("adp") else None

        entry = CheatsheetEntry(
            player_name=name,
            normalized_name=norm_name,
            position=norm_row.get("position", "").upper(),
            team=norm_row.get("team", "").upper(),
            tier=tier_val,
            adp=adp_val,
            notes=norm_row.get("notes"),
        )
        entries[norm_name] = entry

    return CheatsheetContext(entries=entries)


def parse_json_cheatsheet(json_text: str) -> CheatsheetContext:
    """Parse structured JSON cheatsheet."""
    data = json.loads(json_text)
    entries: dict[str, CheatsheetEntry] = {}
    rules = list(data.get("strategy_rules", []))

    for p in data.get("players", []):
        name = str(p.get("name", ""))
        if not name:
            continue
        norm_name = normalize_player_name(name)
        entry = CheatsheetEntry(
            player_name=name,
            normalized_name=norm_name,
            position=str(p.get("position", "")).upper(),
            team=str(p.get("team", "")).upper(),
            tier=int(p.get("tier", 1)),
            adp=float(p.get("adp")) if p.get("adp") is not None else None,
            notes=p.get("notes"),
        )
        entries[norm_name] = entry

    return CheatsheetContext(entries=entries, strategy_rules=rules)


def parse_cheatsheet_content(content: str) -> CheatsheetContext:
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
    return parse_plain_text_cheatsheet(cleaned)


def apply_cheatsheet_context(players: list[Player], context: CheatsheetContext) -> list[Player]:
    """Attach cheatsheet notes, tiers, and flags without overwriting raw projections."""
    if not context or not context.entries:
        return players

    for player in players:
        norm = normalize_player_name(player.name)
        entry = context.entries.get(norm)

        if not entry:
            entry = next((e for k, e in context.entries.items() if k in norm or norm in k), None)

        if entry:
            player.cheatsheet_tier = entry.tier
            if entry.notes:
                player.cheatsheet_notes = entry.notes
            if entry.is_injured and player.injury_status == "ACTIVE":
                player.injury_status = "QUESTIONABLE"

    return players
