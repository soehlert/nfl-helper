"""Cheatsheet and custom ranking context ingestion engine."""

import csv
import io
import json
import re

from pypdf import PdfReader

from nfl_helper.core.name_normalizer import normalize_player_name
from nfl_helper.models.cheatsheet import (
    CheatsheetContext,
    CheatsheetEntry,
    DraftRoundTarget,
    PositionalStrategyRule,
)
from nfl_helper.models.player import Player

POSITION_HEADERS = {
    "QB": "QB",
    "QUARTERBACK": "QB",
    "QUARTERBACKS": "QB",
    "RB": "RB",
    "RUNNING": "RB",
    "RUNNINGBACK": "RB",
    "RUNNINGBACKS": "RB",
    "RBS": "RB",
    "WR": "WR",
    "WIDE": "WR",
    "RECEIVER": "WR",
    "RECEIVERS": "WR",
    "WRS": "WR",
    "TE": "TE",
    "TIGHT": "TE",
    "TIGHTEND": "TE",
    "TIGHTENDS": "TE",
    "TES": "TE",
    "K": "K",
    "KICKER": "K",
    "KICKERS": "K",
    "PK": "K",
    "DEF": "DST",
    "DST": "DST",
    "D/ST": "DST",
    "DEFENSE": "DST",
    "DEFENSES": "DST",
}

NFL_TEAMS = {
    "ARI",
    "ATL",
    "BAL",
    "BUF",
    "CAR",
    "CHI",
    "CIN",
    "CLE",
    "DAL",
    "DEN",
    "DET",
    "GB",
    "HOU",
    "IND",
    "JAX",
    "KC",
    "LAC",
    "LAR",
    "LV",
    "MIA",
    "MIN",
    "NE",
    "NO",
    "NYG",
    "NYJ",
    "PHI",
    "PIT",
    "SEA",
    "SF",
    "TB",
    "TEN",
    "WAS",
    "WSH",
    "FA",
}


def _clean_position_header(raw_header: str) -> str | None:
    """Extract standard position from header line (e.g. 'RUNNING BACKS', 'RB con't' -> 'RB')."""
    cleaned = re.sub(r"[^A-Za-z0-9/ ]", "", raw_header).strip().upper()
    tokens = cleaned.split()
    if not tokens:
        return None

    first = tokens[0]
    if first in POSITION_HEADERS:
        return POSITION_HEADERS[first]
    if len(tokens) >= 2 and f"{tokens[0]} {tokens[1]}" in ("RUNNING BACKS", "WIDE RECEIVERS", "TIGHT ENDS"):
        return POSITION_HEADERS[tokens[0]]

    return None


def _parse_player_line(line: str, current_pos: str, current_tier: int) -> CheatsheetEntry | None:
    """Parse flexible line formats into CheatsheetEntry."""
    cleaned = line.strip()
    if not cleaned or len(cleaned) < 2:
        return None

    # Check for notes separated by hyphen or colon
    notes = None
    if " - " in cleaned:
        parts = cleaned.split(" - ", 1)
        cleaned = parts[0].strip()
        notes = parts[1].strip()
    elif " : " in cleaned or (":" in cleaned and not cleaned.startswith("Tier")):
        parts = cleaned.split(":", 1)
        cleaned = parts[0].strip()
        notes = parts[1].strip()

    # Strip leading rank numbers / bullets (e.g. '1. ', '1) ', '#1 ')
    cleaned = re.sub(r"^\s*#?\d+[\.\)\:\-]?\s*", "", cleaned).strip()

    # Check for injury marker
    is_inj = "*" in cleaned or "(Q)" in cleaned or "(IR)" in cleaned or "(O)" in cleaned
    cleaned = re.sub(r"[\*\(\)]", " ", cleaned).strip()

    tokens = cleaned.split()
    if not tokens:
        return None

    # Extract ADP if last token is numeric
    adp_val = None
    if tokens and re.match(r"^\d+(\.\d+)?$", tokens[-1]):
        try:
            adp_val = float(tokens[-1])
            tokens = tokens[:-1]
        except ValueError:
            pass

    # Extract Position & Team tokens
    pos_found = current_pos
    team_found = None

    filtered_tokens: list[str] = []
    for tok in tokens:
        tok_upper = tok.upper()
        if tok_upper in POSITION_HEADERS:
            pos_found = POSITION_HEADERS[tok_upper]
        elif tok_upper in NFL_TEAMS:
            team_found = tok_upper
        elif re.match(r"^\d+$", tok) and len(tok) <= 2:
            # Bye week number (e.g. 9 or 12)
            continue
        else:
            filtered_tokens.append(tok)

    if not filtered_tokens:
        return None

    player_name = " ".join(filtered_tokens).strip()
    if len(player_name) < 2:
        return None

    return CheatsheetEntry(
        player_name=player_name,
        normalized_name=normalize_player_name(player_name),
        position=pos_found or current_pos or "WR",
        team=team_found or "",
        tier=current_tier,
        adp=adp_val,
        is_injured=is_inj,
        notes=notes,
    )


def _parse_strategy_rule(line: str) -> tuple[DraftRoundTarget | None, PositionalStrategyRule | None]:
    """Parse natural language draft strategy rules into structured target models."""
    norm_line = line.strip()
    round_target = None
    pos_target = None

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
    elif any(norm_line.upper().startswith(f"{pos} -") for pos in ("TE", "QB", "RB", "WR")):
        pos = norm_line[:2].upper()
        top_n = 4 if "top 4" in norm_line.lower() else None
        target_tiers = []
        tier_range_match = re.search(r"tiers?\s+(\d+)\s*-\s*(\d+)", norm_line, re.IGNORECASE)
        if tier_range_match:
            t_start, t_end = int(tier_range_match.group(1)), int(tier_range_match.group(2))
            target_tiers = list(range(t_start, t_end + 1))
        elif "tier 3" in norm_line.lower() and "tier 4" in norm_line.lower():
            target_tiers = [3, 4]
        elif "tier 1" in norm_line.lower():
            target_tiers = [1]

        target_rounds = []
        rnd_match = re.search(r"rounds?\s+(\d+)(?:\s*-\s*(\d+))?", norm_line, re.IGNORECASE)
        if rnd_match:
            r_start = int(rnd_match.group(1))
            r_end = int(rnd_match.group(2)) if rnd_match.group(2) else r_start
            target_rounds = list(range(r_start, r_end + 1))

        pos_target = PositionalStrategyRule(
            position=pos,
            target_rounds=target_rounds,
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

        # Check for continuation lines (e.g. 'or take 2 from tiers 2-4', 'or get Allen in round 4')
        is_continuation = (
            bool(context.strategy_rules)
            and not pos_header
            and (
                line.lower().startswith("or ")
                or line.lower().startswith("and ")
                or line.startswith("- ")
                or line.startswith("• ")
                or (raw_line.startswith(" ") and not _clean_position_header(line))
            )
        )
        if is_continuation:
            combined = f"{context.strategy_rules[-1]} {line}"
            context.strategy_rules[-1] = combined
            rnd_rule, pos_rule = _parse_strategy_rule(combined)
            if rnd_rule:
                if context.round_targets:
                    context.round_targets[-1] = rnd_rule
                else:
                    context.round_targets.append(rnd_rule)
            if pos_rule:
                if context.positional_strategy:
                    context.positional_strategy[-1] = pos_rule
                else:
                    context.positional_strategy.append(pos_rule)
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


def parse_pdf_cheatsheet(pdf_bytes: bytes) -> CheatsheetContext:
    """Extract text from PDF file and parse positional tiers, ADPs, and strategy rules."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    page_texts: list[str] = []
    for page in reader.pages:
        try:
            txt = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            txt = page.extract_text() or ""
        page_texts.append(txt)
    full_text = "\n".join(page_texts)
    return parse_plain_text_cheatsheet(full_text)


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
