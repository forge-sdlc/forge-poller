import argparse
import getpass
import os
import sys

import httpx


def register(ticket_id: str, base_url: str, code: str) -> int:
    ticket_id = ticket_id.upper()
    resp = httpx.post(
        f"{base_url}/watch",
        json={"tickets": [ticket_id]},
        headers={"X-Invite-Code": code},
    )
    if resp.status_code == 202:
        print(f"Welcome to the Forge beta!\nTicket {ticket_id} is now being watched.")
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
    parser = argparse.ArgumentParser(prog="forge-cli")
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register", help="Start watching a Jira ticket")
    reg.add_argument("ticket_id", help="Jira ticket key (e.g. AISOS-123)")
    args = parser.parse_args()

    code = getpass.getpass("Invite code: ")
    base_url = os.environ.get("FORGE_POLLER_URL", "http://localhost:8001")
    sys.exit(register(args.ticket_id, base_url, code))
