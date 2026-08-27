"""Unit tests for administrative CLI tool."""

from nfl_helper.cli import build_invite_url, generate_invite_code, main


def test_generate_invite_code_format() -> None:
    """Verify generated invite codes follow prefix-random format."""
    sleeper_code = generate_invite_code("sleeper")
    assert sleeper_code.startswith("SLE-")
    assert len(sleeper_code) == 9

    espn_code = generate_invite_code("espn")
    assert espn_code.startswith("ESP-")
    assert len(espn_code) == 9


def test_build_invite_url() -> None:
    """Verify magic invite link URL construction."""
    code, url = build_invite_url(
        platform="sleeper",
        league_id="987654321",
        team_id="3",
        base_url="http://localhost:8000",
    )
    assert f"invite={code}" in url
    assert "platform=sleeper" in url
    assert "league=987654321" in url
    assert "team=3" in url


def test_cli_create_invite_execution(capsys) -> None:
    """Verify CLI create-invite subcommand prints magic link."""
    main(["create-invite", "--platform", "sleeper", "--league-id", "12345", "--team-id", "1"])
    captured = capsys.readouterr()
    assert "MAGIC INVITE CREATED" in captured.out
    assert "Platform  : SLEEPER" in captured.out
    assert "League ID : 12345" in captured.out
