"""Unit tests for Cheatsheet & Custom Rules Context Engine."""

from nfl_helper.core.cheatsheet import (
    apply_cheatsheet_context,
    parse_csv_cheatsheet,
    parse_json_cheatsheet,
    parse_plain_text_cheatsheet,
)
from nfl_helper.models.player import Player, Position


def test_parse_plain_text_cheatsheet_with_tiers_and_rules() -> None:
    """Verify plain-text cheatsheet parser extracts positional tiers, ADPs, injury flags, and strategy notes."""
    sample_text = """
QB
Allen BUF 34.8

Jackson BAL 69.1
Burrow CIN 65.5

RB
Gibbs DET 1.1
Robinson ATL 2.1

McCaffrey SF 8.7
Charbonnet* SEA 151.7

Rounds 1-2 - Only RB/WR at least 1 RB
TE - Target the top 4 in rounds 3-5
* = injured a while
"""
    context = parse_plain_text_cheatsheet(sample_text)

    gibbs_key = next((k for k in context.entries if "gibbs" in k), None)
    assert gibbs_key is not None
    assert context.entries[gibbs_key].position == "RB"
    assert context.entries[gibbs_key].tier == 1
    assert context.entries[gibbs_key].adp == 1.1

    cmc_key = next((k for k in context.entries if "mccaffrey" in k), None)
    assert cmc_key is not None
    assert context.entries[cmc_key].tier == 2

    charb_key = next((k for k in context.entries if "charbonnet" in k), None)
    assert charb_key is not None
    assert context.entries[charb_key].is_injured is True

    assert len(context.round_targets) == 1
    assert context.round_targets[0].target_rounds == [1, 2]
    assert "RB" in context.round_targets[0].allowed_positions
    assert context.round_targets[0].min_counts == {"RB": 1}

    assert len(context.positional_strategy) == 1
    assert context.positional_strategy[0].position == "TE"
    assert context.positional_strategy[0].top_n_target == 4


def test_parse_full_multi_page_cheatsheet() -> None:
    """Verify parser handles WR, WR con't, TE across pages with multiple tiers and injury flags."""
    full_sample = """
WR
Chase CIN 3.1
Nacua LAR 4.8
St Brown DET 5.9
Smith-Njigba SEA 5.5

Lamb DAL 11.3
Jefferson MIN 12.3

WR con't
Thomas JAX 67.7
Harrison ARI 62.1

Tyson* NO 123.5
Lemon PHI 107.6

TE
Bowers LV 21.6
McBride ARI 28.4

LaPorta DET 100.1
Pitts ATL 81.9
"""
    context = parse_plain_text_cheatsheet(full_sample)

    # Check WR Page 2 start
    assert any("chase" in k for k in context.entries)
    assert any("nacua" in k for k in context.entries)
    assert any("st brown" in k for k in context.entries)

    # Check WR con't continuity into WR position
    tyson_key = next((k for k in context.entries if "tyson" in k), None)
    assert tyson_key is not None
    assert context.entries[tyson_key].position == "WR"
    assert context.entries[tyson_key].is_injured is True

    # Check TE parsing
    bowers_key = next((k for k in context.entries if "bowers" in k), None)
    assert bowers_key is not None
    assert context.entries[bowers_key].position == "TE"
    assert context.entries[bowers_key].tier == 1


def test_parse_csv_cheatsheet() -> None:
    """Verify CSV format cheatsheet parser."""
    csv_data = """Player,Position,Team,Tier,ADP,Notes
Justin Jefferson,WR,MIN,1,12.3,Elite WR1 anchor
Breece Hall,RB,NYJ,1,8.9,Three down workhorse
Kenneth Walker III,RB,SEA,3,45.0,High ceiling
"""
    context = parse_csv_cheatsheet(csv_data)
    assert len(context.entries) == 3
    assert "justin jefferson" in context.entries
    assert context.entries["justin jefferson"].notes == "Elite WR1 anchor"
    assert context.entries["kenneth walker"].tier == 3


def test_parse_json_cheatsheet() -> None:
    """Verify JSON format cheatsheet parser."""
    json_data = """{
        "players": [
            {"name": "CeeDee Lamb", "position": "WR", "tier": 1, "adp": 4.2, "notes": "Target monster"}
        ],
        "strategy_rules": [
            "Draft elite WR early"
        ]
    }"""
    context = parse_json_cheatsheet(json_data)
    assert len(context.entries) == 1
    assert "ceedee lamb" in context.entries
    assert context.strategy_rules == ["Draft elite WR early"]


def test_apply_cheatsheet_context_to_players() -> None:
    """Verify contextual notes, tiers, and flags attach to player objects without altering projected points."""
    sample_text = """
WR
Jefferson MIN 12.3
St Brown DET 15.0

Tyson* NO 123.5
"""
    context = parse_plain_text_cheatsheet(sample_text)

    players = [
        Player(id="101", name="Justin Jefferson", position=Position.WR, team="MIN", projected_points=18.4),
        Player(id="102", name="Amon-Ra St. Brown", position=Position.WR, team="DET", projected_points=17.8),
        Player(id="103", name="Lamar Jackson", position=Position.QB, team="BAL", projected_points=22.0),
    ]

    enriched = apply_cheatsheet_context(players, context)

    p_jeff = next(p for p in enriched if p.id == "101")
    assert p_jeff.cheatsheet_tier == 1
    assert p_jeff.projected_points == 18.4

    p_amon = next(p for p in enriched if p.id == "102")
    assert p_amon.cheatsheet_tier == 1

    p_lamar = next(p for p in enriched if p.id == "103")
    assert p_lamar.cheatsheet_tier is None
