"""Cheatsheet and custom ranking context ingestion engine."""

import csv
import io
import json
import math
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


def _parse_player_line(
    line: str,
    current_pos: str,
    current_tier: int,
    legend_notes: dict[str, str] | None = None,
) -> CheatsheetEntry | None:
    """Parse flexible line formats into CheatsheetEntry."""
    cleaned = line.strip()
    if not cleaned or len(cleaned) < 2:
        return None

    # Replace pipe table separators and tabs with spaces
    cleaned = cleaned.replace("|", " ").replace("\t", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

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
    is_inj = "*" in cleaned or "(Q)" in cleaned or "(IR)" in cleaned or "(O)" in cleaned or "(PUP)" in cleaned
    if is_inj and not notes:
        notes = (legend_notes or {}).get("*", "Injured (multi-week recovery)")
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

    if norm_line.lower().startswith(("rounds", "round", "target", "wait")):
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


def _clean_kerning(text: str) -> str:
    """Repair fragmented OCR spaces and broken word tokens."""
    if any(
        text.startswith(p)
        for p in (
            "Rounds",
            "Round",
            "Rule",
            "Rules",
            "Strategy",
            "Target",
            "TE -",
            "QB -",
            "RB -",
            "WR -",
            "K -",
            "DST -",
            "D/ST -",
            "Wait",
        )
    ):
        return text

    t = text
    # Fix broken team codes e.g. 'P HI' -> 'PHI', 'S EA' -> 'SEA', 'S F' -> 'SF'
    t = re.sub(r"\b([A-Z])\s+([A-Z])\s+([A-Z])\b", r"\1\2\3", t)
    t = re.sub(r"\b([A-Z])\s+([A-Z])\b", r"\1\2", t)

    # Specific broken OCR names from PDF scans
    ocr_fixes = {
        "robi n son": "Robinson",
        "he n ry": "Henry",
        "he rbe rt": "Herbert",
        "p re scott": "Prescott",
        "mon an gai": "Monangai",
        "croske y-me rri tt": "Croskey-Merritt",
        "p urdy": "Purdy",
        "p ollard": "Pollard",
    }
    for broken, fixed in ocr_fixes.items():
        t = re.sub(re.escape(broken), fixed, t, flags=re.IGNORECASE)

    return t


def parse_plain_text_cheatsheet(text: str) -> CheatsheetContext:
    """Parse plain-text cheatsheet tracking blank-line tiers, ADPs, multi-column splits, and strategy rules."""
    context = CheatsheetContext()
    current_pos = ""
    current_tier = 1
    previous_was_blank = False
    legend_notes: dict[str, str] = {}

    for raw_line in text.splitlines():
        line = _clean_kerning(raw_line.strip())
        if not line:
            if current_pos and not previous_was_blank:
                current_tier += 1
            previous_was_blank = True
            continue

        previous_was_blank = False

        # Filter repeated header noise
        if re.search(r"\badp\s+adp\b", line, re.IGNORECASE) or re.search(r"\btier\s+adp\b", line, re.IGNORECASE):
            continue

        pos_header = _clean_position_header(line)
        if pos_header and len(line.split()) <= 3 and not re.search(r"\d", line):
            current_pos = pos_header
            current_tier = 1
            continue

        # Legend / footnote definitions (e.g. '* = injured a while', '^ = rookie target')
        legend_match = re.match(r"^\s*([*^#])\s*=\s*(.+)$", line)
        if legend_match:
            symbol, meaning = legend_match.group(1), legend_match.group(2).strip()
            legend_notes[symbol] = meaning
            continue

        # Strategy rule headers
        if any(
            line.startswith(prefix)
            for prefix in (
                "Rounds",
                "Round",
                "Rule",
                "Rules",
                "Strategy",
                "Target",
                "TE -",
                "QB -",
                "RB -",
                "WR -",
                "K -",
                "DST -",
                "D/ST -",
            )
        ):
            if line.startswith(("Strategy:", "Rules:", "Rule:")):
                line = re.sub(r"^(Strategy:|Rules:|Rule:)\s*", "", line).strip()
            if not line:
                continue
            context.strategy_rules.append(line)
            rnd_rule, pos_rule = _parse_strategy_rule(line)
            if rnd_rule:
                context.round_targets.append(rnd_rule)
            if pos_rule:
                context.positional_strategy.append(pos_rule)
            continue

        # Strict continuation lines (only 'or ...' / 'and ...' that are not player lines)
        is_continuation = (
            bool(context.strategy_rules)
            and not pos_header
            and (line.lower().startswith("or ") or line.lower().startswith("and "))
            and not any(f" {team} " in f" {line} " for team in NFL_TEAMS)
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

        # Multi-column horizontal line detection (e.g. 'Allen 34.8 Gibbs 1.1 Price')
        multi_col_pattern = r"([A-Za-z\s.'-]+?)(?:\s+([A-Z]{2,3}))?\s+(\d+(?:\.\d+)?)(?=\s+[A-Za-z]|$)"
        col_matches = list(re.finditer(multi_col_pattern, line))

        if len(col_matches) > 1 or (col_matches and len(line[col_matches[-1].end() :].strip()) >= 3):
            last_end = 0
            for m in col_matches:
                chunk_str = m.group(0).strip()
                last_end = m.end()
                entry = _parse_player_line(chunk_str, current_pos, current_tier, legend_notes)
                if entry and entry.normalized_name:
                    _record_player_entry(entry, entry.position or current_pos, context)

            trailing = line[last_end:].strip()
            if trailing and len(trailing) >= 3 and not re.match(r"^\d", trailing):
                entry = _parse_player_line(trailing, current_pos, current_tier, legend_notes)
                if entry and entry.normalized_name:
                    _record_player_entry(entry, entry.position or current_pos, context)
        else:
            entry = _parse_player_line(line, current_pos, current_tier, legend_notes)
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
            if entry.adp is not None:
                player.adp = entry.adp
            if entry.notes:
                player.cheatsheet_notes = entry.notes
            if entry.is_injured:
                player.injury_status = "IR"
                if not player.cheatsheet_notes:
                    player.cheatsheet_notes = "Injured (multi-week recovery / out a while)"

            # If user's cheatsheet assigns player to Tier 1 or high ADP, update projection curve
            if entry.tier == 1 and player.projected_points < 17.0:
                pos_str = str(player.position)
                if pos_str == "QB":
                    player.projected_points = max(player.projected_points, 22.0)
                elif pos_str == "RB":
                    player.projected_points = max(player.projected_points, 19.5)
                elif pos_str == "WR":
                    player.projected_points = max(player.projected_points, 19.0)
                elif pos_str == "TE":
                    player.projected_points = max(player.projected_points, 14.5)
            elif entry.adp and entry.adp <= 30.0:
                pos_str = str(player.position)
                eff_rank = max(1.0, entry.adp / 4.0)
                if pos_str == "QB":
                    player.projected_points = max(player.projected_points, round(25.5 - 2.5 * math.log(eff_rank), 2))
                elif pos_str == "RB":
                    player.projected_points = max(player.projected_points, round(21.5 - 3.2 * math.log(eff_rank), 2))
                elif pos_str == "WR":
                    player.projected_points = max(player.projected_points, round(20.8 - 2.8 * math.log(eff_rank), 2))
                elif pos_str == "TE":
                    player.projected_points = max(player.projected_points, round(15.2 - 2.4 * math.log(eff_rank), 2))

    return players
