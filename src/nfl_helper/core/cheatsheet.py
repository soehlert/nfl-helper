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


KNOWN_PLAYER_LOOKUP: dict[str, tuple[str, str, str]] = {
    "hurts": ("Jalen Hurts", "QB", "PHI"),
    "barkley": ("Saquon Barkley", "RB", "PHI"),
    "gibbs": ("Jahmyr Gibbs", "RB", "DET"),
    "mccaffrey": ("Christian McCaffrey", "RB", "SF"),
    "burrow": ("Joe Burrow", "QB", "CIN"),
    "maye": ("Drake Maye", "QB", "NE"),
    "daniels": ("Jayden Daniels", "QB", "WAS"),
    "herbert": ("Justin Herbert", "QB", "LAC"),
    "williams": ("Caleb Williams", "QB", "CHI"),
    "lawrence": ("Trevor Lawrence", "QB", "JAX"),
    "prescott": ("Dak Prescott", "QB", "DAL"),
    "purdy": ("Brock Purdy", "QB", "SF"),
    "henry": ("Derrick Henry", "RB", "BAL"),
    "achane": ("De'Von Achane", "RB", "MIA"),
    "taylor": ("Jonathan Taylor", "RB", "IND"),
    "cook": ("James Cook", "RB", "BUF"),
    "corum": ("Blake Corum", "RB", "LAR"),
    "pollard": ("Tony Pollard", "RB", "TEN"),
    "dobbins": ("J.K. Dobbins", "RB", "LAC"),
    "hubbard": ("Chuba Hubbard", "RB", "CAR"),
    "hampton": ("Omarion Hampton", "RB", "LAC"),
    "mason": ("Jordan Mason", "RB", "SF"),
    "gainwell": ("Kenneth Gainwell", "RB", "PHI"),
    "monangai": ("Kyle Monangai", "RB", "CHI"),
    "croskey-merritt": ("Jacory Croskey-Merritt", "RB", "WAS"),
    "jeanty": ("Ashton Jeanty", "RB", "DAL"),
    "mitchell": ("Keaton Mitchell", "RB", "BAL"),
    "marks": ("Woody Marks", "RB", "HOU"),
    "white": ("Rachaad White", "RB", "TB"),
    "robinson": ("Bijan Robinson", "RB", "ATL"),
    "walker": ("Kenneth Walker", "RB", "SEA"),
    "jackson": ("Lamar Jackson", "QB", "BAL"),
}


KNOWN_ANALYSTS = {
    "bell",
    "bowen",
    "clay",
    "cockcroft",
    "dopp",
    "fulghum",
    "karabell",
    "loza",
    "moody",
    "yates",
    "stephania bell",
    "matt bowen",
    "mike clay",
    "tristan cockcroft",
    "tristan h cockcroft",
    "daniel dopp",
    "tyler fulghum",
    "eric karabell",
    "liz loza",
    "eric moody",
    "field yates",
}


def _clean_position_header(raw_header: str | None) -> tuple[str, bool] | None:
    """Extract standard position from header line, returning (position, is_continuation)."""
    if not raw_header or not isinstance(raw_header, str):
        return None
    norm_header = raw_header.replace("\u2019", "'").replace("`", "'")
    cleaned = re.sub(r"[^A-Za-z0-9/ ']", "", norm_header).strip()
    tokens = cleaned.split()
    if not tokens:
        return None

    is_continuation = any(w in norm_header.lower() for w in ("con't", "cont", "continued"))
    first = tokens[0].upper()
    if first in POSITION_HEADERS:
        return POSITION_HEADERS[first], is_continuation
    if len(tokens) >= 2 and f"{tokens[0]} {tokens[1]}".upper() in (
        "RUNNING BACKS",
        "WIDE RECEIVERS",
        "TIGHT ENDS",
        "RUNNING BACK",
        "WIDE RECEIVER",
        "TIGHT END",
    ):
        return POSITION_HEADERS[tokens[0].upper()], is_continuation

    return None


def _parse_player_line(
    line: str,
    current_pos: str,
    current_tier: int | None,
    legend_notes: dict[str, str] | None = None,
    default_notes: str | None = None,
) -> CheatsheetEntry | None:
    """Parse flexible line formats into CheatsheetEntry."""
    cleaned = line.strip()
    if not cleaned or len(cleaned) < 2:
        return None

    # Replace pipe table separators and tabs with spaces
    cleaned = cleaned.replace("|", " ").replace("\t", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Check for notes separated by hyphen or colon
    notes = default_notes
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

    norm_key = normalize_player_name(player_name)

    # Filter noise, analyst names, and header rows
    if norm_key in KNOWN_ANALYSTS:
        return None
    name_lower = player_name.lower()
    if (
        "adp" in name_lower
        or "tier" in name_lower
        or name_lower.startswith("page")
        or name_lower in ("pos., player", "pos, player", "pos. player")
    ) and len(player_name.split()) <= 4:
        return None

    # Canonical player resolution for ambiguous names
    if norm_key in KNOWN_PLAYER_LOOKUP:
        full_name, can_pos, can_team = KNOWN_PLAYER_LOOKUP[norm_key]
        player_name = full_name
        pos_found = pos_found or can_pos
        team_found = team_found or can_team

    return CheatsheetEntry(
        player_name=player_name,
        normalized_name=norm_key,
        position=pos_found or current_pos or "",
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
    norm_full = normalize_player_name(entry.player_name)
    if norm_full != entry.normalized_name:
        context.entries[norm_full] = entry
    if pos_key and entry.tier is not None and entry.tier > 0:
        if pos_key not in context.positional_tiers:
            context.positional_tiers[pos_key] = []
        while len(context.positional_tiers[pos_key]) < entry.tier:
            context.positional_tiers[pos_key].append([])
        context.positional_tiers[pos_key][entry.tier - 1].append(entry.player_name)


def _clean_kerning(text: str) -> str:
    """Normalize whitespace and basic characters."""
    return text.strip() if text else ""


def parse_plain_text_cheatsheet(text: str) -> CheatsheetContext:
    """Parse plain-text cheatsheet tracking blank-line tiers, ADPs, multi-column tables, and strategy rules."""
    context = CheatsheetContext()
    current_pos = ""
    current_tier: int | None = 1
    current_section_note: str | None = None
    previous_was_blank = False
    legend_notes: dict[str, str] = {}

    lines = [ln.strip() for ln in text.splitlines()]
    i = 0
    while i < len(lines):
        raw_line = lines[i]
        line = raw_line.strip()
        if not line:
            if current_pos and current_tier is not None and not previous_was_blank:
                current_tier += 1
            previous_was_blank = True
            i += 1
            continue

        previous_was_blank = False

        line_lower = line.lower()
        if line_lower in ("sleepers", "sleeper", "top sleepers", "sleepers:", "sleeper:"):
            current_section_note = "ESPN Sleeper"
            current_tier = None
            current_pos = ""
            i += 1
            continue
        if line_lower in ("busts", "bust", "top busts", "busts:", "bust:"):
            current_section_note = "ESPN Bust"
            current_tier = None
            current_pos = ""
            i += 1
            continue
        if line_lower in ("breakouts", "breakout", "top breakouts", "breakouts:", "breakout:"):
            current_section_note = "ESPN Breakout"
            current_tier = None
            current_pos = ""
            i += 1
            continue

        # Check for 4-column table header: Quarterback, Running back, Wide Receiver, Tight end
        if i + 3 < len(lines) and [
            lines[i].lower(),
            lines[i + 1].lower(),
            lines[i + 2].lower(),
            lines[i + 3].lower(),
        ] == ["quarterback", "running back", "wide receiver", "tight end"]:
            i += 4
            while i < len(lines):
                if lines[i].lower() in (
                    "quarterback",
                    "running back",
                    "wide receiver",
                    "tight end",
                    "pos., player",
                    "pos, player",
                ):
                    break
                analyst = lines[i].strip()
                if not analyst:
                    i += 1
                    continue
                i += 1
                if i + 3 < len(lines):
                    row_players = [
                        ("QB", lines[i]),
                        ("RB", lines[i + 1]),
                        ("WR", lines[i + 2]),
                        ("TE", lines[i + 3]),
                    ]
                    i += 4
                    for pos, p_name in row_players:
                        clean_p = p_name.strip()
                        if clean_p and len(clean_p) >= 2:
                            norm = normalize_player_name(clean_p)
                            entry = CheatsheetEntry(
                                player_name=clean_p,
                                normalized_name=norm,
                                position=pos,
                                tier=None,
                                notes=f"{current_section_note or 'Analyst Pick'} ({analyst})",
                            )
                            _record_player_entry(entry, pos, context)
                else:
                    break
            continue

        # Check for 2-column header: Pos., Player
        if lines[i].lower() in ("pos., player", "pos, player", "pos. player"):
            i += 1
            if i < len(lines) and lines[i].lower() in ("pos., player", "pos, player", "pos. player"):
                i += 1
            while i < len(lines):
                if lines[i].lower() in ("quarterback", "running back", "wide receiver", "tight end"):
                    break
                analyst = lines[i].strip()
                if not analyst:
                    i += 1
                    continue
                i += 1
                if i < len(lines):
                    pos_player = lines[i].strip()
                    i += 1
                    tokens = pos_player.split()
                    pos = (
                        tokens[0].upper()
                        if tokens and tokens[0].upper() in ("RB", "WR", "QB", "TE", "K", "DST")
                        else ""
                    )
                    pname = " ".join(tokens[1:]) if pos else pos_player
                    clean_p = pname.strip()
                    if clean_p and len(clean_p) >= 2:
                        norm = normalize_player_name(clean_p)
                        entry = CheatsheetEntry(
                            player_name=clean_p,
                            normalized_name=norm,
                            position=pos,
                            tier=None,
                            notes=f"{current_section_note or 'Analyst Pick'} ({analyst})",
                        )
                        _record_player_entry(entry, pos, context)
                else:
                    break
            continue

        if normalize_player_name(line) in KNOWN_ANALYSTS:
            i += 1
            continue

        if re.search(r"\badp\s+adp\b", line, re.IGNORECASE) or re.search(r"\btier\s+adp\b", line, re.IGNORECASE):
            i += 1
            continue

        tier_match = re.match(r"^\s*Tier\s+(\d+)[\s\:\-]*$", line, re.IGNORECASE)
        if tier_match:
            current_tier = int(tier_match.group(1))
            previous_was_blank = True
            i += 1
            continue

        pos_info = _clean_position_header(line)
        if pos_info and len(line.split()) <= 4 and not re.search(r"\d", line):
            pos_header, is_continuation = pos_info
            current_pos = pos_header
            if not is_continuation:
                current_tier = 1 if current_section_note is None else None
            else:
                if current_tier is not None:
                    current_tier += 1
            previous_was_blank = True
            i += 1
            continue

        # Legend / footnote definitions (e.g. '* = injured a while', '^ = rookie target')
        legend_match = re.match(r"^\s*([*^#])\s*=\s*(.+)$", line)
        if legend_match:
            symbol, meaning = legend_match.group(1), legend_match.group(2).strip()
            legend_notes[symbol] = meaning
            i += 1
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
                i += 1
                continue
            context.strategy_rules.append(line)
            rnd_rule, pos_rule = _parse_strategy_rule(line)
            if rnd_rule:
                context.round_targets.append(rnd_rule)
            if pos_rule:
                context.positional_strategy.append(pos_rule)
            i += 1
            continue

        # Multi-column horizontal line detection (e.g. 'Allen 34.8 Gibbs 1.1 Price')
        multi_col_pattern = r"([A-Za-z\s.'-]+?)(?:\s+([A-Z]{2,3}))?\s+(\d+(?:\.\d+)?)(?=\s+[A-Za-z]|$)"
        col_matches = list(re.finditer(multi_col_pattern, line))

        if len(col_matches) > 1 or (col_matches and len(line[col_matches[-1].end() :].strip()) >= 3):
            last_end = 0
            for m in col_matches:
                chunk_str = m.group(0).strip()
                last_end = m.end()
                entry = _parse_player_line(
                    chunk_str, current_pos, current_tier, legend_notes, default_notes=current_section_note
                )
                if entry and entry.normalized_name:
                    _record_player_entry(entry, entry.position or current_pos, context)

            trailing = line[last_end:].strip()
            if trailing and len(trailing) >= 3 and not re.match(r"^\d", trailing):
                entry = _parse_player_line(
                    trailing, current_pos, current_tier, legend_notes, default_notes=current_section_note
                )
                if entry and entry.normalized_name:
                    _record_player_entry(entry, entry.position or current_pos, context)
        else:
            entry = _parse_player_line(
                line, current_pos, current_tier, legend_notes, default_notes=current_section_note
            )
            if entry and entry.normalized_name:
                _record_player_entry(entry, entry.position or current_pos, context)

        i += 1

    return context


def parse_csv_cheatsheet(csv_text: str) -> CheatsheetContext:
    """Parse CSV format cheatsheet with Player, Position, Team, Tier, ADP, Notes headers and # Rule comments."""
    context = CheatsheetContext()
    csv_lines: list[str] = []

    for raw_line in csv_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            rule_text = line.lstrip("#").strip()
            if rule_text:
                context.strategy_rules.append(rule_text)
                rnd_tgt, pos_tgt = _parse_strategy_rule(rule_text)
                if rnd_tgt:
                    context.round_targets.append(rnd_tgt)
                if pos_tgt:
                    context.positional_strategy.append(pos_tgt)
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
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
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
