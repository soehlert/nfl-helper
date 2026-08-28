import argparse
import random
import string
import sys
import urllib.parse
from pathlib import Path

import httpx
import uvicorn


def generate_invite_code(platform: str) -> str:
    """Generate a clean single-use invite code (e.g. SLE-8K2L9 or ESP-7X4W1)."""
    prefix = platform[:3].upper()
    random_chars = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"{prefix}-{random_chars}"


def build_invite_url(
    platform: str,
    league_id: str | None = None,
    team_id: str | None = None,
    swid: str | None = None,
    espn_s2: str | None = None,
    base_url: str = "http://127.0.0.1:8000",
) -> tuple[str, str]:
    """Construct full single-use invite magic link URL with optional pre-configured credentials."""
    code = generate_invite_code(platform)
    clean_base = base_url.rstrip("/")
    params: dict[str, str] = {
        "invite": code,
        "platform": platform,
    }
    if league_id:
        params["league"] = league_id
    if team_id:
        params["team"] = team_id
    if swid:
        params["swid"] = swid
    if espn_s2:
        params["espn_s2"] = espn_s2

    query_str = urllib.parse.urlencode(params)
    url = f"{clean_base}/?{query_str}"
    return code, url


def create_invite_command(args: argparse.Namespace) -> None:
    """Handle create-invite CLI command."""
    code, url = build_invite_url(
        platform=args.platform,
        league_id=args.league_id,
        team_id=args.team_id,
        swid=args.swid,
        espn_s2=args.espn_s2,
        base_url=args.base_url,
    )
    print("=" * 60)
    print("🏈 FANTASY WAR ROOM - SINGLE-USE MAGIC INVITE CREATED")
    print("=" * 60)
    print(f"Platform   : {args.platform.upper()}")
    print(f"League ID  : {args.league_id if args.league_id else '(User will enter in Settings)'}")
    if args.team_id:
        print(f"Team ID    : {args.team_id}")
    if args.swid:
        print(f"ESPN SWID  : {args.swid[:8]}... (Pre-configured)")
    if args.espn_s2:
        print(f"ESPN S2    : {args.espn_s2[:8]}... (Pre-configured)")
    print(f"Invite Code: {code}")
    print("-" * 60)
    print("Share this private magic link with your friend:")
    print(url)
    print("=" * 60)


def qa_command(args: argparse.Namespace) -> None:
    """Handle QA runtime mode toggle and status inspection."""
    base_url = args.base_url.rstrip("/")
    try:
        if args.on:
            res = httpx.post(f"{base_url}/api/admin/qa-mode", json={"enabled": True}, timeout=3.0)
            if res.status_code == 200:
                print("=" * 60)
                print("🧪 FANTASY WAR ROOM - QA TESTING MODE: ENABLED")
                print("=" * 60)
                print("Simulation controls (Simulate Cliff / Simulate Tier Roll) are now ACTIVE.")
                print("=" * 60)
            else:
                print(f"[ERROR] Failed to enable QA mode: {res.status_code} {res.text}")
        elif args.off:
            res = httpx.post(f"{base_url}/api/admin/qa-mode", json={"enabled": False}, timeout=3.0)
            if res.status_code == 200:
                print("=" * 60)
                print("🛡️  FANTASY WAR ROOM - QA TESTING MODE: DISABLED")
                print("=" * 60)
                print("Simulation controls are now HIDDEN from the Web UI and API.")
                print("=" * 60)
            else:
                print(f"[ERROR] Failed to disable QA mode: {res.status_code} {res.text}")
        else:
            res = httpx.get(f"{base_url}/api/config", timeout=3.0)
            if res.status_code == 200:
                data = res.json()
                status_str = "ENABLED (Simulation tools active)" if data.get("qa_mode") else "DISABLED (Standard mode)"
                print(f"QA Testing Mode: {status_str}")
            else:
                print(f"[ERROR] Failed to fetch config: {res.status_code} {res.text}")
    except httpx.ConnectError:
        print(f"[ERROR] Could not connect to running server at {base_url}.")
        print("Ensure the Fantasy War Room server is running with 'uv run python -m nfl_helper.cli serve'.")


def diff_cheatsheet_command(args: argparse.Namespace) -> None:
    """Handle dry-run cheatsheet diff comparing candidate rankings against active baseline."""
    base_url = args.base_url.rstrip("/")

    if args.url:
        try:
            res = httpx.post(f"{base_url}/api/cheatsheet/url-diff", json={"url": args.url}, timeout=10.0)
            if res.status_code != 200:
                print(f"[ERROR] Web diff failed ({res.status_code}): {res.text}")
                return
            diff_data = res.json()
        except httpx.ConnectError:
            print("[ERROR] Could not connect to running server. Web URL diff requires the server to be running.")
            return
    elif args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"[ERROR] Cheatsheet file not found: {file_path}")
            sys.exit(1)

        text_content = file_path.read_text(encoding="utf-8", errors="replace")

        try:
            res = httpx.post(f"{base_url}/api/cheatsheet/diff", json={"text": text_content}, timeout=5.0)
            if res.status_code != 200:
                print(f"[ERROR] Diff failed ({res.status_code}): {res.text}")
                return
            diff_data = res.json()
        except httpx.ConnectError:
            # Fallback to local deterministic computation if server is offline
            from nfl_helper.core.cheatsheet import parse_cheatsheet_content
            from nfl_helper.core.cheatsheet_diff import compute_cheatsheet_diff
            from nfl_helper.core.db import get_active_cheatsheet
            from tests.fixtures.demo_rosters import get_mock_player_pool

            candidate = parse_cheatsheet_content(text_content)
            active = get_active_cheatsheet()
            report = compute_cheatsheet_diff(active, candidate, get_mock_player_pool(), top_n=args.top_n)
            diff_data = report.model_dump()
    else:
        print("[ERROR] Please provide either --file <path> or --url <web_url>.")
        sys.exit(1)

    print("=" * 70)
    print("📊 CHEATSHEET DRY-RUN IMPACT REPORT (TOP MOVERS)")
    print("=" * 70)

    # Top Risers
    risers = diff_data.get("top_risers", [])
    print(f"\n🚀 TOP {len(risers)} BIGGEST RISERS (Rank Improved):")
    print(f"{'PLAYER':<24} {'POS':<5} {'TEAM':<5} {'OLD RANK':<10} {'NEW RANK':<10} {'SHIFT':<8}")
    print("-" * 70)
    for r in risers:
        tier_info = (
            f" (Tier {r['old_tier']} -> {r['new_tier']})"
            if r.get("old_tier") and r.get("new_tier") and r["old_tier"] != r["new_tier"]
            else ""
        )
        print(
            f"{r['player_name']:<24} {r['position']:<5} {r.get('team', ''):<5} #{r['old_rank']:<9} #{r['new_rank']:<9} +{r['rank_delta']} spots{tier_info}"
        )

    # Top Fallers
    fallers = diff_data.get("top_fallers", [])
    print(f"\n📉 TOP {len(fallers)} BIGGEST FALLERS (Rank Dropped):")
    print(f"{'PLAYER':<24} {'POS':<5} {'TEAM':<5} {'OLD RANK':<10} {'NEW RANK':<10} {'SHIFT':<8}")
    print("-" * 70)
    for f in fallers:
        tier_info = (
            f" (Tier {f['old_tier']} -> {f['new_tier']})"
            if f.get("old_tier") and f.get("new_tier") and f["old_tier"] != f["new_tier"]
            else ""
        )
        print(
            f"{f['player_name']:<24} {f['position']:<5} {f.get('team', ''):<5} #{f['old_rank']:<9} #{f['new_rank']:<9} {f['rank_delta']} spots{tier_info}"
        )

    # Rule Deltas
    added_rules = diff_data.get("added_rules", [])
    removed_rules = diff_data.get("removed_rules", [])
    if added_rules or removed_rules:
        print("\n📋 STRATEGY RULE CHANGES:")
        for rule in added_rules:
            print(f"  [+] ADDED: {rule}")
        for rule in removed_rules:
            print(f"  [-] REMOVED: {rule}")

    print("\n" + "=" * 70)
    print(
        f"Total Players Affected: {diff_data.get('total_players_affected', 0)} | Total Rule Deltas: {diff_data.get('total_rules_affected', 0)}"
    )
    print("Dry-run preview complete. No production database tables were modified.")
    print("=" * 70)


def serve_command(args: argparse.Namespace) -> None:
    """Handle serve CLI command to start FastAPI application."""
    if args.qa_mode:
        import nfl_helper.main

        nfl_helper.main._QA_MODE = True
        print("🧪 Starting server with QA TESTING MODE ENABLED.")
    print(f"🚀 Starting Fantasy War Room on http://{args.host}:{args.port}...")
    uvicorn.run("nfl_helper.main:app", host=args.host, port=args.port, reload=args.reload)


def main(argv: list[str] | None = None) -> None:
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="nfl-helper",
        description="Fantasy War Room administrative CLI and server launcher",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Available commands")

    invite_parser = subparsers.add_parser("create-invite", help="Generate single-use magic invite link for a friend")
    invite_parser.add_argument(
        "--platform", choices=["sleeper", "espn"], default="sleeper", help="League platform (sleeper or espn)"
    )
    invite_parser.add_argument("--league-id", default=None, help="Optional friend's league ID (if known)")
    invite_parser.add_argument("--team-id", default=None, help="Optional friend's team ID (if known)")
    invite_parser.add_argument("--swid", default=None, help="Optional ESPN SWID cookie for private leagues")
    invite_parser.add_argument("--espn-s2", default=None, help="Optional ESPN espn_s2 cookie for private leagues")
    invite_parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base application URL")
    invite_parser.set_defaults(func=create_invite_command)

    qa_parser = subparsers.add_parser("qa", help="Inspect or toggle QA simulation mode at runtime")
    qa_group = qa_parser.add_mutually_exclusive_group()
    qa_group.add_argument("--on", action="store_true", help="Enable QA simulation mode")
    qa_group.add_argument("--off", action="store_true", help="Disable QA simulation mode")
    qa_group.add_argument("--status", action="store_true", help="Check current QA mode status")
    qa_parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base application URL")
    qa_parser.set_defaults(func=qa_command)

    diff_parser = subparsers.add_parser("diff-cheatsheet", help="Dry-run impact diff for candidate cheatsheet")
    diff_parser.add_argument("--file", default=None, help="Path to plain text or CSV cheatsheet file")
    diff_parser.add_argument("--url", default=None, help="URL to online cheatsheet or ESPN rankings")
    diff_parser.add_argument("--top-n", type=int, default=5, help="Number of top risers/fallers to display")
    diff_parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base application URL")
    diff_parser.set_defaults(func=diff_cheatsheet_command)

    serve_parser = subparsers.add_parser("serve", help="Start local web application server")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host address to bind")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to bind")
    serve_parser.add_argument("--reload", action="store_true", default=True, help="Enable live auto-reload")
    serve_parser.add_argument("--qa-mode", action="store_true", default=False, help="Launch server in QA mode")
    serve_parser.set_defaults(func=serve_command)

    parsed_args = parser.parse_args(argv or sys.argv[1:])
    if not parsed_args.subcommand:
        parser.print_help()
        sys.exit(0)

    parsed_args.func(parsed_args)


if __name__ == "__main__":
    main()
