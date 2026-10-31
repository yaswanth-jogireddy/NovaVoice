"""Switch the running server's milestone (internal; not shown in the product UI).

    python scripts/milestone.py 3            (reads ADMIN_TOKEN from .env)
    python scripts/milestone.py 2 --port 8936

The open pages reload themselves with the new capabilities.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("milestone", type=int, choices=[1, 2, 3])
    parser.add_argument("--port", type=int, default=8935)
    args = parser.parse_args()
    token = dotenv_values(ROOT / ".env").get("ADMIN_TOKEN", "")
    if not token:
        sys.exit("Set ADMIN_TOKEN in .env first.")
    try:
        r = httpx.post(f"http://localhost:{args.port}/api/admin/milestone", json={"milestone": args.milestone},
                       headers={"X-Admin-Token": token}, timeout=10)
    except httpx.ConnectError:
        sys.exit(f"No server on port {args.port}. Start it first.")
    if r.status_code != 200:
        sys.exit(f"Switch refused ({r.status_code}): {r.text}")
    on = [name for name, enabled in r.json()["features"].items() if enabled]
    print(f"Milestone {r.json()['milestone']} is live. On: {', '.join(on)}")
