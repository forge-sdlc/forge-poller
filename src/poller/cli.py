import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import httpx

CONFIG_PATH = Path.home() / ".config" / "forge-watch" / "config.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def configure() -> int:
    existing = load_config()

    print("Configure forge-watch\n")

    current_url = existing.get("forge_url", os.environ.get("FORGE_POLLER_URL", ""))
    prompt = f"Forge server URL [{current_url}]: " if current_url else "Forge server URL: "
    url = input(prompt).strip() or current_url

    if not url:
        print("Error: server URL is required.", file=sys.stderr)
        return 1

    current_code = existing.get("invite_code", "")
    code_prompt = "Invite code [leave blank to keep existing]: " if current_code else "Invite code: "
    code = getpass.getpass(code_prompt).strip() or current_code

    if not code:
        print("Error: invite code is required.", file=sys.stderr)
        return 1

    config = {"forge_url": url.rstrip("/"), "invite_code": code}
    save_config(config)
    print(f"\nConfiguration saved to {CONFIG_PATH}")
    return 0


def register(ticket_ids: str | list[str], base_url: str, code: str) -> int:
    if isinstance(ticket_ids, str):
        ticket_ids = [ticket_ids]
    ticket_ids = [t.upper() for t in ticket_ids]
    resp = httpx.post(
        f"{base_url}/watch",
        json={"tickets": ticket_ids},
        headers={"X-Invite-Code": code},
    )
    if resp.status_code == 202:
        for ticket_id in ticket_ids:
            print(f"Ticket {ticket_id} is now being watched.")
        if len(ticket_ids) == 1:
            print("\nWelcome to the Forge beta!")
        return 0
    if resp.status_code == 403:
        print(
            "Wrong password.\n"
            "Forge is running on an exclusive beta right now — please ask to join on #forge-sdlc"
        )
        return 1
    print(f"Unexpected error: {resp.status_code}")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="forge-watch")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("configure", help="Save server URL and invite code")

    reg = sub.add_parser("register", help="Start watching one or more Jira tickets")
    reg.add_argument("ticket_id", nargs="+", help="Jira ticket key(s) (e.g. MYPROJ-123)")

    args = parser.parse_args()

    if args.command == "configure":
        sys.exit(configure())

    if args.command == "register":
        config = load_config()
        base_url = (
            os.environ.get("FORGE_POLLER_URL")
            or config.get("forge_url")
        )
        code = config.get("invite_code", "")

        if not base_url:
            print(
                "Error: Forge server URL not set. Run `forge-watch configure` first.",
                file=sys.stderr,
            )
            sys.exit(1)

        if not code:
            code = getpass.getpass("Invite code: ")

        sys.exit(register(args.ticket_id, base_url, code))
