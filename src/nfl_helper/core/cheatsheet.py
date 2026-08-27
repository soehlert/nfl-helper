"""Cheatsheet and custom ranking context ingestion engine."""

import csv
import io
import json
import re

from pydantic import BaseModel, Field

from nfl_helper.core.name_normalizer import normalize_player_name
from nfl_helper.models.player import Player

POSITION_HEADERS = {"QB", "RB", "WR", "TE", "K", "DEF", "DST", "D/ST"}
LINE_PATTERN = re.compile(r"^([A-Za-z\s\-\.'\*]+?)\s+([A-Z]{2,3})\s+(\d+\.?\d*)$")


class CheatsheetEntry(BaseModel):
    """Parsed cheatsheet record for a single player."""

    player_name: str
    normalized_name: str
    position: str = ""
    team: str = ""
    tier: int = 1
    adp: float | None = None
    is_injured: bool = False
    notes: str | None = None


class CheatsheetContext(BaseModel):
    """Aggregated cheatsheet ranking, tier, and strategy rules metadata."""

    entries: dict[str, CheatsheetEntry] = Field(default_factory=dict)
    strategy_rules: list[str] = Field(default_factory=list)
    positional_tiers: dict[str, list[list[str]]] = Field(default_factory=dict)


def _clean_position_header(raw_header: str) -> str | None:
    """Extract standard position from header line."""
    tokens = raw_header.strip().split()
    if not tokens:
        return None
    candidate = tokens[0].upper()
    if candidate in POSITION_HEADERS:
        return "DST" if candidate in ("DEF", "D/ST") else candidate
    return None


def _parse_player_line(line: str, current_pos: str, current_tier: int) -> CheatsheetEntry | None:
    """Parse a line in format PlayerName Team ADP into a CheatsheetEntry."""
    match = LINE_PATTERN.match(line.strip())
    if not match:
        if "-" in line or ":" in line:
            parts = re.split(r"[-:]", line, maxsplit=1)
            name = parts[0].strip()
            notes = parts[1].strip() if len(parts) > 1 else None
            is_inj = "*" in name
            clean_name = name.replace("*", "").strip()
            norm = normalize_player_name(clean_name)
            return CheatsheetEntry(
                player_name=clean_name,
                normalized_name=norm,
                position=current_pos,
                tier=current_tier,
                is_injured=is_inj,
                notes=notes,
            )
        return None

    raw_name, team, adp_str = match.groups()
    is_inj = "*" in raw_name
    clean_name = raw_name.replace("*", "").strip()
    norm = normalize_player_name(clean_name)

    return CheatsheetEntry(
        player_name=clean_name,
        normalized_name=norm,
        position=current_pos,
        team=team.upper(),
        tier=current_tier,
        adp=float(adp_str),
        is_injured=is_inj,
    )


def parse_plain_text_cheatsheet(text: str) -> CheatsheetContext:
    """Parse plain-text cheatsheet tracking blank-line tiers, ADPs, and strategy rules."""
    entries: dict[str, CheatsheetEntry] = {}
    strategy_rules: list[str] = []
    positional_tiers: dict[str, list[list[str]]] = {}

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
            if current_pos not in positional_tiers:
                positional_tiers[current_pos] = []
            continue

        if any(line.startswith(prefix) for prefix in ("Rounds", "TE -", "QB -", "RB -", "WR -", "* =", "Strategy:")):
            strategy_rules.append(line)
            continue

        entry = _parse_player_line(line, current_pos, current_tier)
        if entry and entry.normalized_name:
            entries[entry.normalized_name] = entry
            pos_key = entry.position or current_pos
            if pos_key:
                if pos_key not in positional_tiers:
                    positional_tiers[pos_key] = []
                while len(positional_tiers[pos_key]) < entry.tier:
                    positional_tiers[pos_key].append([])
                positional_tiers[pos_key][entry.tier - 1].append(entry.player_name)

    return CheatsheetContext(
        entries=entries,
        strategy_rules=strategy_rules,
        positional_tiers=positional_tiers,
    )


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
