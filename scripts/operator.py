"""Manage operator accounts: add a reviewer, list accounts, revoke one.

A new token is printed exactly once and only its hash is stored, so rotation is
"add the new account, hand over the token, revoke the old one" — no redeploy and
no shared secret to edit in a dashboard.

    python -m scripts.operator add --name priya
    python -m scripts.operator list
    python -m scripts.operator revoke --name priya
"""

from __future__ import annotations

import argparse

from agent import db


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage RTI Pilot operator accounts.")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="create an account and print its token once")
    add.add_argument("--name", required=True)
    add.add_argument("--role", default="operator", choices=("operator", "admin"))

    revoke = sub.add_parser("revoke", help="stop a token from working")
    revoke.add_argument("--name", required=True)

    sub.add_parser("list", help="show accounts without their tokens")

    args = parser.parse_args()
    conn = db.init_db()
    try:
        if args.command == "add":
            operator_id, token = db.create_operator(conn, args.name, args.role)
            print(f"Created {args.name} ({operator_id}, {args.role}).")
            print("Token, shown once:")
            print(token)
            print("Store it in a password manager and send it over a private channel.")
        elif args.command == "revoke":
            if db.revoke_operator(conn, args.name):
                print(f"Revoked {args.name}. Existing sessions and tokens stop working.")
            else:
                print(f"No active account named {args.name}.")
                return 1
        else:
            rows = db.list_operators(conn)
            if not rows:
                print("No operator accounts yet. Run `add --name ...` or start the agent with RTI_OPERATOR_TOKEN.")
                return 0
            for row in rows:
                state = "revoked" if row["revoked_at"] else "active"
                print(f"{row['name']:<20} {row['role']:<10} {state:<8} created {row['created_at']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
