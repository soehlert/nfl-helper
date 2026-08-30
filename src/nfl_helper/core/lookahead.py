"""Snake draft schedule arithmetic and turn lookahead engine."""


def calculate_snake_pick_owner(overall_pick: int, total_teams: int) -> int:
    """Return the 1-indexed draft slot owner for an overall pick number."""
    if overall_pick < 1 or total_teams < 1:
        raise ValueError("Pick number and team count must be positive integers")
    round_num = (overall_pick - 1) // total_teams + 1
    round_pick = (overall_pick - 1) % total_teams + 1
    return round_pick if round_num % 2 == 1 else (total_teams - round_pick + 1)


def calculate_user_draft_schedule(user_slot: int, total_teams: int, total_rounds: int = 16) -> list[int]:
    """Pre-compute the list of overall pick numbers for a given draft slot."""
    if user_slot < 1 or user_slot > total_teams:
        raise ValueError(f"User slot {user_slot} is out of bounds for {total_teams} teams")
    schedule: list[int] = []
    for r in range(1, total_rounds + 1):
        round_pick = user_slot if r % 2 == 1 else (total_teams - user_slot + 1)
        schedule.append((r - 1) * total_teams + round_pick)
    return schedule


def calculate_lookahead(
    overall_pick: int, user_slot: int, total_teams: int, total_rounds: int = 16
) -> tuple[int, int, bool]:
    """Compute picks until user turn, subsequent turn gap, and on-the-clock status."""
    schedule = calculate_user_draft_schedule(user_slot, total_teams, total_rounds)
    if overall_pick in schedule:
        is_on_the_clock = True
        picks_until_turn = 0
        idx = schedule.index(overall_pick)
        subsequent_pick = schedule[idx + 1] if idx + 1 < len(schedule) else schedule[-1] + total_teams
        turn_gap = max(0, subsequent_pick - overall_pick - 1)
        return picks_until_turn, turn_gap, is_on_the_clock

    is_on_the_clock = False
    future_picks = [p for p in schedule if p > overall_pick]
    if not future_picks:
        return 0, 0, False

    next_pick = future_picks[0]
    picks_until_turn = next_pick - overall_pick
    idx = schedule.index(next_pick)
    subsequent_pick = schedule[idx + 1] if idx + 1 < len(schedule) else schedule[-1] + total_teams
    turn_gap = max(0, subsequent_pick - next_pick - 1)
    return picks_until_turn, turn_gap, is_on_the_clock
