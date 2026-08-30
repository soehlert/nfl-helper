"""Natural language draft strategy rules tokenizer and structured model parser."""

import re

from nfl_helper.models.cheatsheet import (
    DraftRoundTarget,
    PositionalQuotaDeadline,
    PositionalStrategyBranch,
    PositionalStrategyRule,
)


def parse_round_target_rule(norm_line: str) -> DraftRoundTarget | None:
    """Parse round exclusivity rules (e.g. 'Rounds 1-2 only RB/WR, at least 1 RB')."""
    line_lower = norm_line.lower()
    if not any(line_lower.startswith(prefix) for prefix in ("rounds", "round", "target", "wait")):
        return None

    rounds_match = re.search(r"rounds?\s+(\d+)(?:\s*-\s*(\d+))?", norm_line, re.IGNORECASE)
    if not rounds_match:
        return None

    start_rnd = int(rounds_match.group(1))
    end_rnd = int(rounds_match.group(2)) if rounds_match.group(2) else start_rnd
    target_rounds = list(range(start_rnd, end_rnd + 1))
    allowed = [pos for pos in ("RB", "WR", "QB", "TE", "K", "D/ST") if pos in norm_line.upper()]
    min_counts = {"RB": 1} if "at least 1 rb" in line_lower else {}

    return DraftRoundTarget(
        target_rounds=target_rounds,
        allowed_positions=allowed,
        min_counts=min_counts,
        rule_description=norm_line,
    )


def parse_positional_deadline_quota(norm_line: str) -> PositionalQuotaDeadline | None:
    """Parse positional acquisition deadline quotas (e.g. 'RB - Get 4 in first 10 rounds', 'WR - Get 4 minimum')."""
    m = re.search(
        r"(RB|WR|QB|TE|K|D/ST|DEF)\s*[-:]?\s*.*?(?:get|draft)\s+(\d+)\s+.*?(?:in\s+the\s+first|by\s+round)\s+(\d+)",
        norm_line,
        re.IGNORECASE,
    )
    if m:
        pos = m.group(1).upper()
        pos = "D/ST" if pos in ("DEF", "DST") else pos
        return PositionalQuotaDeadline(
            position=pos,
            required_count=int(m.group(2)),
            deadline_round=int(m.group(3)),
            rule_description=norm_line,
        )

    m_min = re.search(
        r"(RB|WR|QB|TE|K|D/ST|DEF)\s*[-:]?\s*.*?(?:minimum|get)\s+(\d+)",
        norm_line,
        re.IGNORECASE,
    )
    if m_min:
        pos = m_min.group(1).upper()
        pos = "D/ST" if pos in ("DEF", "DST") else pos
        return PositionalQuotaDeadline(
            position=pos,
            required_count=int(m_min.group(2)),
            deadline_round=16,
            rule_description=norm_line,
        )

    return None


def _parse_positional_branch(clause: str, pos: str, branch_id: str = "") -> PositionalStrategyBranch | None:
    """Parse a single positional strategy clause into a structured branch."""
    clause_lower = clause.strip().lower()
    target_rounds: list[int] = []
    target_tiers: list[int] = []
    quotas: dict[int, int] = {}
    top_n: int | None = None
    trigger_tiers: list[int] = []
    max_cap: int | None = None

    # Check top N target
    top_m = re.search(r"top\s+(\d+)", clause_lower)
    if top_m:
        top_n = int(top_m.group(1))

    # Check tier range (e.g. 'tier 3-4' or 'tiers 3 to 5')
    tier_range = re.search(r"tiers?\s+(\d+)\s*(?:-|to)\s*(\d+)", clause_lower)
    if tier_range:
        start_t, end_t = int(tier_range.group(1)), int(tier_range.group(2))
        target_tiers = list(range(start_t, end_t + 1))
    else:
        tier_matches = re.findall(r"(?:(one|two|1|2)\s+(?:from\s+)?)?tier\s+(\d+)", clause_lower)
        for count_str, tier_str in tier_matches:
            t_num = int(tier_str)
            if t_num not in target_tiers:
                target_tiers.append(t_num)
            c_val = 2 if count_str in ("two", "2") else 1
            quotas[t_num] = c_val

    # Check round ranges and single round targets
    rnd_range = re.search(r"rounds?\s+(\d+)\s*-\s*(\d+)", clause_lower)
    if rnd_range:
        target_rounds = list(range(int(rnd_range.group(1)), int(rnd_range.group(2)) + 1))
    else:
        single_rnd = re.search(r"(?:in\s+round\s+|in\s+the\s+)(\d+)(?:st|nd|rd|th)?\s+round", clause_lower)
        if not single_rnd:
            single_rnd = re.search(r"round\s+(\d+)", clause_lower)
        if single_rnd and "first" not in clause_lower:
            target_rounds = [int(single_rnd.group(1))]

    # Check trigger tier conditions (e.g. 'if you get a tier 1')
    trigger_m = re.search(r"if\s+you\s+(?:get|have|draft)\s+(?:a\s+)?tier\s+(\d+)", clause_lower)
    if trigger_m:
        trigger_tiers = [int(trigger_m.group(1))]
    elif "tier 1" in clause_lower and ("only one" in clause_lower or "no second" in clause_lower):
        trigger_tiers = [1]

    # Check max cap in branch
    if "only one" in clause_lower or "only 1" in clause_lower or "no second" in clause_lower:
        max_cap = 1
    elif "only two" in clause_lower or "only 2" in clause_lower:
        max_cap = 2

    if not target_tiers and not target_rounds and top_n is None and not trigger_tiers and max_cap is None:
        return None

    return PositionalStrategyBranch(
        branch_id=branch_id,
        trigger_drafted_tiers=trigger_tiers,
        max_position_cap=max_cap,
        target_rounds=target_rounds,
        target_tiers=target_tiers,
        target_tier_quotas=quotas,
        top_n_target=top_n,
    )


def parse_positional_strategy_rule(norm_line: str, pos: str) -> PositionalStrategyRule:
    """Parse positional draft targets, conditional branching, and tier constraints."""
    line_lower = norm_line.lower()
    cond_caps: dict[int, int] = {}
    no_second_top = False

    # 1. Parse conditional max caps
    if "no second" in line_lower and ("tier 1" in line_lower or "top" in line_lower):
        no_second_top = True
        cond_caps[1] = 1

    cond_m = re.search(
        r"(?:if\s+you\s+(?:get|have)\s+a\s+tier\s+(\d+)|if\s+tier\s+(\d+)).*?only\s+(one|two|1|2)",
        line_lower,
    )
    if cond_m:
        t_val = int(cond_m.group(1) or cond_m.group(2))
        c_str = cond_m.group(3)
        c_val = 2 if c_str in ("two", "2") else 1
        cond_caps[t_val] = c_val

    # 2. Split strategy branches on ' or ' / 'otherwise'
    main_part = re.split(r"(?:if\s+you\s+get|if\s+you\s+have|no\s+second)", line_lower)[0]
    segments = re.split(r"\s+or\s+|\s+otherwise\s+", main_part, flags=re.IGNORECASE)
    branches: list[PositionalStrategyBranch] = []
    for idx, seg in enumerate(segments, start=1):
        branch = _parse_positional_branch(seg, pos, branch_id=f"branch_{idx}")
        if branch:
            branches.append(branch)

    if not branches:
        b = _parse_positional_branch(main_part, pos, branch_id="main")
        if b:
            branches.append(b)

    all_rounds: list[int] = []
    all_tiers: list[int] = []
    top_n: int | None = None
    for b in branches:
        for r in b.target_rounds:
            if r not in all_rounds:
                all_rounds.append(r)
        for t in b.target_tiers:
            if t not in all_tiers:
                all_tiers.append(t)
        if b.top_n_target is not None:
            top_n = b.top_n_target

    default_cap = 1 if no_second_top else 2

    return PositionalStrategyRule(
        position=pos,
        target_rounds=sorted(all_rounds),
        target_tiers=sorted(all_tiers),
        top_n_target=top_n,
        conditional_max_count=cond_caps,
        default_max_cap=default_cap,
        branches=branches,
        no_second_if_top_tier=no_second_top,
        rule_description=norm_line,
    )


def parse_strategy_rule(
    line: str,
) -> tuple[DraftRoundTarget | None, PositionalStrategyRule | None, PositionalQuotaDeadline | None]:
    """Dispatch natural language line into typed round target, positional rule, or quota deadline."""
    norm_line = line.strip()
    round_target = parse_round_target_rule(norm_line)
    deadline_quota = parse_positional_deadline_quota(norm_line)
    pos_target = None

    if any(
        norm_line.upper().startswith(f"{pos} -") or norm_line.upper().startswith(f"{pos}:")
        for pos in ("TE", "QB", "RB", "WR", "K", "D/ST")
    ):
        pos_match = re.match(r"^(TE|QB|RB|WR|K|D/ST|DEF)", norm_line, re.IGNORECASE)
        if pos_match:
            pos = pos_match.group(1).upper()
            pos = "D/ST" if pos in ("DEF", "DST") else pos
            pos_target = parse_positional_strategy_rule(norm_line, pos)

    return round_target, pos_target, deadline_quota
