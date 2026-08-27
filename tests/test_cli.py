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


def test_build_invite_url_sleeper() -> None:
    """Verify Sleeper magic invite link URL construction with and without league ID."""
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

    # Open invite without league ID
    code2, url2 = build_invite_url(platform="sleeper", base_url="http://localhost:8000")
    assert f"invite={code2}" in url2
    assert "platform=sleeper" in url2
    assert "league=" not in url2


def test_build_invite_url_espn_with_credentials() -> None:
    """Verify ESPN magic invite link URL with optional pre-configured cookies."""
    code, url = build_invite_url(
        platform="espn",
        league_id="12345678",
        team_id="1",
        swid="{MY-SWID-123}",
        espn_s2="AECb_secret_cookie",
        base_url="http://localhost:8000",
    )
    assert f"invite={code}" in url
    assert "platform=espn" in url
    assert "swid=%7BMY-SWID-123%7D" in url or "swid={MY-SWID-123}" in url
    assert "espn_s2=AECb_secret_cookie" in url


def test_cli_create_invite_execution(capsys) -> None:
    """Verify CLI create-invite subcommand prints magic link."""
    main(["create-invite", "--platform", "sleeper"])
    captured = capsys.readouterr()
    assert "MAGIC INVITE CREATED" in captured.out
    assert "Platform   : SLEEPER" in captured.out
