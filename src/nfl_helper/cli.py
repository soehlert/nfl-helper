"""Command Line Interface for administrative operations and server launching."""

import argparse
import random
import string
import sys

import uvicorn


def generate_invite_code(platform: str) -> str:
    """Generate a clean single-use invite code (e.g. SLE-8K2L9 or ESP-7X4W1)."""
    prefix = platform[:3].upper()
    random_chars = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"{prefix}-{random_chars}"


def build_invite_url(
    platform: str,
    league_id: str,
    team_id: str,
    base_url: str = "http://127.0.0.1:8000",
) -> tuple[str, str]:
    """Construct full single-use invite magic link URL."""
    code = generate_invite_code(platform)
    clean_base = base_url.rstrip("/")
    url = f"{clean_base}/?invite={code}&platform={platform}&league={league_id}&team={team_id}"
    return code, url


def create_invite_command(args: argparse.Namespace) -> None:
    """Handle create-invite CLI command."""
    code, url = build_invite_url(
        platform=args.platform,
        league_id=args.league_id,
        team_id=args.team_id,
        base_url=args.base_url,
    )
    print("=" * 60)
    print("🏈 FANTASY WAR ROOM - SINGLE-USE MAGIC INVITE CREATED")
    print("=" * 60)
    print(f"Platform  : {args.platform.upper()}")
    print(f"League ID : {args.league_id}")
    print(f"Team ID   : {args.team_id}")
    print(f"Invite Code: {code}")
    print("-" * 60)
    print("Share this private magic link with your friend:")
    print(url)
    print("=" * 60)


def serve_command(args: argparse.Namespace) -> None:
    """Handle serve CLI command to start FastAPI application."""
    print(f"🚀 Starting Fantasy War Room on http://{args.host}:{args.port}...")
    uvicorn.run("nfl_helper.main:app", host=args.host, port=args.port, reload=args.reload)


def main(argv: list[str] | None = None) -> None:
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="nfl-helper",
        description="Fantasy War Room administrative CLI and server launcher",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Available commands")

    # create-invite subcommand
    invite_parser = subparsers.add_parser("create-invite", help="Generate single-use magic invite link for a friend")
    invite_parser.add_argument(
        "--platform", choices=["sleeper", "espn"], default="sleeper", help="League platform (sleeper or espn)"
    )
    invite_parser.add_argument("--league-id", required=True, help="Friend's league ID")
    invite_parser.add_argument("--team-id", required=True, help="Friend's team ID")
    invite_parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base application URL")
    invite_parser.set_defaults(func=create_invite_command)

    # serve subcommand
    serve_parser = subparsers.add_parser("serve", help="Start local web application server")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host address to bind")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to bind")
    serve_parser.add_argument("--reload", action="store_true", default=True, help="Enable live auto-reload")
    serve_parser.set_defaults(func=serve_command)

    parsed_args = parser.parse_args(argv or sys.argv[1:])
    if not parsed_args.subcommand:
        parser.print_help()
        sys.exit(0)

    parsed_args.func(parsed_args)


if __name__ == "__main__":
    main()
