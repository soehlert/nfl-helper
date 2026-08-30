"""Plain-text cheatsheet parser with tier segmentation and strategy rules extraction."""

import re

from nfl_helper.core.name_normalizer import normalize_player_name
from nfl_helper.core.strategy_parser import parse_strategy_rule
from nfl_helper.models.cheatsheet import CheatsheetContext, CheatsheetEntry

POSITION_HEADERS = {
    "QB": "QB",
    "QUARTERBACK": "QB",
    "QUARTERBACKS": "QB",
    "RB": "RB",
    "RUNNING BACK": "RB",
    "RUNNING BACKS": "RB",
    "WR": "WR",
    "WIDE RECEIVER": "WR",
    "WIDE RECEIVERS": "WR",
    "TE": "TE",
    "TIGHT END": "TE",
    "TIGHT ENDS": "TE",
    "K": "K",
    "KICKER": "K",
    "KICKERS": "K",
    "PK": "K",
    "DEF": "D/ST",
    "DST": "D/ST",
    "D/ST": "D/ST",
    "DEFENSE": "D/ST",
    "DEFENSES": "D/ST",
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

    cleaned = cleaned.replace("|", " ").replace("\t", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    notes = default_notes
    if " - " in cleaned:
        parts = cleaned.split(" - ", 1)
        cleaned = parts[0].strip()
        notes = parts[1].strip()
    elif " : " in cleaned or (":" in cleaned and not cleaned.startswith("Tier")):
        parts = cleaned.split(":", 1)
        cleaned = parts[0].strip()
        notes = parts[1].strip()

    # Check for leading rank/round decimal format (e.g. '1.1 Christian McCaffrey' -> ADP 1.1)
    lead_adp_match = re.match(r"^\s*(\d+\.\d+)\s+", cleaned)
    lead_adp = float(lead_adp_match.group(1)) if lead_adp_match else None

    # Strip leading rank numbers / bullets (e.g. '1. ', '1) ', '#1 ', '1.1 ')
    cleaned = re.sub(r"^\s*#?\d+(?:\.\d+)?[\.\)\:\-]?\s*", "", cleaned).strip()

    # Check for injury marker
    is_inj = "*" in cleaned or "(Q)" in cleaned or "(IR)" in cleaned or "(O)" in cleaned or "(PUP)" in cleaned
    if is_inj and not notes:
        notes = (legend_notes or {}).get("*", "Injured (multi-week recovery)")
    cleaned = re.sub(r"[\*\(\)]", " ", cleaned).strip()

    tokens = cleaned.split()
    if not tokens:
        return None

    adp_val = None
    if tokens and re.match(r"^\d+(\.\d+)?$", tokens[-1]):
        try:
            adp_val = float(tokens[-1])
            tokens = tokens[:-1]
        except ValueError:
            pass

    if adp_val is None and lead_adp is not None:
        adp_val = lead_adp

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
            continue
        else:
            filtered_tokens.append(tok)

    if not filtered_tokens:
        return None

    player_name = " ".join(filtered_tokens).strip()
    if len(player_name) < 2:
        return None

    norm_key = normalize_player_name(player_name)

    return CheatsheetEntry(
        player_name=player_name,
        normalized_name=norm_key,
        position=pos_found,
        team=team_found or "",
        tier=current_tier,
        adp=adp_val,
        is_injured=is_inj,
        notes=notes,
    )


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


def parse_plain_text_cheatsheet(text: str, sheet_name: str | None = None) -> CheatsheetContext:
    """Parse plain-text cheatsheet into structured CheatsheetContext."""
    context = CheatsheetContext()
    if not text or not text.strip():
        return context

    platform_prefix = ""
    if sheet_name:
        clean_sn = sheet_name.upper()
        if "ESPN" in clean_sn and "SLEEPER" in clean_sn and any(w in clean_sn for w in ("&", "AND", "COMBINED", "/")):
            platform_prefix = "ESPN SLEEPER "
        elif "ESPN" in clean_sn:
            platform_prefix = "ESPN "
        elif clean_sn.startswith("SLEEPER ") or clean_sn == "SLEEPER":
            platform_prefix = "Sleeper "

    lines = [line.strip() for line in text.splitlines()]
    current_pos = ""
    current_tier: int | None = None
    current_section_note: str | None = None
    legend_notes: dict[str, str] = {}
    previous_was_blank = False
    i = 0

    while i < len(lines):
        line = lines[i]
        if not line:
            previous_was_blank = True
            i += 1
            continue

        line_lower = line.lower()

        # Section note headers
        if any(w in line_lower for w in ("sleeper", "bust", "fade", "breakout", "target")):
            if ("sleeper" in line_lower or "sleepers" in line_lower) and len(line_lower.split()) <= 4:
                current_section_note = f"{platform_prefix}Sleeper".strip()
                current_tier = None
                current_pos = ""
                previous_was_blank = False
                i += 1
                continue
            if ("bust" in line_lower or "fade" in line_lower) and len(line_lower.split()) <= 4:
                current_section_note = f"{platform_prefix}Bust".strip()
                current_tier = None
                current_pos = ""
                previous_was_blank = False
                i += 1
                continue
            if "breakout" in line_lower and len(line_lower.split()) <= 4:
                current_section_note = f"{platform_prefix}Breakout".strip()
                current_tier = None
                current_pos = ""
                previous_was_blank = False
                i += 1
                continue

        # Strategy rule headers
        if any(
            line.startswith(prefix)
            for prefix in (
                "#",
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
            if line.startswith(("#", "Strategy:", "Rules:", "Rule:")):
                line = re.sub(r"^(#\s*|Strategy:\s*|Rules:\s*|Rule:\s*)", "", line).strip()
            if not line:
                i += 1
                continue
            context.strategy_rules.append(line)
            rnd_rule, pos_rule, dl_quota = parse_strategy_rule(line)
            if rnd_rule:
                context.round_targets.append(rnd_rule)
            if pos_rule:
                context.positional_strategy.append(pos_rule)
            if dl_quota:
                context.quota_deadlines.append(dl_quota)
            previous_was_blank = False
            i += 1
            continue

        if re.search(r"\badp\s+adp\b", line, re.IGNORECASE) or re.search(r"\btier\s+adp\b", line, re.IGNORECASE):
            previous_was_blank = False
            i += 1
            continue

        tier_match = re.match(r"^\s*Tier\s+(\d+)[\s\:\-]*$", line, re.IGNORECASE)
        if tier_match:
            current_tier = int(tier_match.group(1))
            previous_was_blank = False
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
            previous_was_blank = False
            i += 1
            continue

        # If previous line was blank and we're in a tiered position block, increment tier
        if previous_was_blank and current_pos and current_tier is not None and current_section_note is None:
            current_tier += 1
            previous_was_blank = False

        # Legend / footnote definitions (e.g. '* = injured a while')
        legend_match = re.match(r"^\s*([*^#])\s*=\s*(.+)$", line)
        if legend_match:
            symbol, meaning = legend_match.group(1), legend_match.group(2).strip()
            legend_notes[symbol] = meaning
            previous_was_blank = False
            i += 1
            continue

        entry = _parse_player_line(line, current_pos, current_tier, legend_notes, default_notes=current_section_note)
        if entry and entry.normalized_name:
            _record_player_entry(entry, entry.position or current_pos, context)

        i += 1

    return context
