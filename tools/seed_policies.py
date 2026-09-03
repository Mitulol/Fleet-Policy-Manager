#!/usr/bin/env python3
"""Seed a starter set of configuration policies through the API gateway.

Creates one baseline policy per device group and publishes it, so a freshly
started stack has rollouts for the simulator to converge on and the dashboard
to chart. Safe to run more than once -- re-running publishes a fresh version.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
import json

POLICIES = [
    {
        "name": "Baseline Hardening",
        "description": "Disk encryption, host firewall and a minimum patch level.",
        "target_group": "engineering",
        "settings": {"disk_encryption": True, "firewall": True, "min_patch_level": "2025-06"},
    },
    {
        "name": "Sales Laptop Standard",
        "description": "Encryption and screen lock for field devices.",
        "target_group": "sales",
        "settings": {"disk_encryption": True, "screen_lock_seconds": 300},
    },
    {
        "name": "Executive Device Policy",
        "description": "Full disk encryption, firewall and conditional access.",
        "target_group": "executive",
        "settings": {"disk_encryption": True, "firewall": True, "conditional_access": True},
    },
    {
        "name": "Kiosk Lockdown",
        "description": "Single-app mode with automatic nightly reboot.",
        "target_group": "kiosk",
        "settings": {"kiosk_mode": True, "nightly_reboot": True},
    },
]


def _call(method: str, url: str, api_key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("X-API-Key", api_key)
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or "{}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed and publish starter policies.")
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--api-key", default="fleet-admin-key")
    args = parser.parse_args()

    try:
        existing = _call("GET", f"{args.gateway}/api/policy/policies", args.api_key)
    except urllib.error.URLError as exc:
        print(f"  cannot reach the gateway: {exc}", file=sys.stderr)
        return 1
    by_key = {(p["name"], p["target_group"]): p["id"] for p in existing}

    for spec in POLICIES:
        try:
            key = (spec["name"], spec["target_group"])
            if key in by_key:
                # Already seeded once -- update it, which bumps the version,
                # rather than creating a second copy of the same policy.
                policy = _call(
                    "PUT",
                    f"{args.gateway}/api/policy/policies/{by_key[key]}",
                    args.api_key,
                    {"description": spec["description"], "settings": spec["settings"]},
                )
                verb = "re-published"
            else:
                policy = _call(
                    "POST", f"{args.gateway}/api/policy/policies", args.api_key, spec
                )
                verb = "published"

            rollout = _call(
                "POST",
                f"{args.gateway}/api/policy/policies/{policy['id']}/publish",
                args.api_key,
            )
            print(
                f"  {verb:<13} {spec['name']:<26} -> group '{spec['target_group']}'  "
                f"v{rollout['version']} (event {rollout['event_id'][:8]})"
            )
        except urllib.error.URLError as exc:
            print(f"  FAILED     {spec['name']}: {exc}", file=sys.stderr)
            return 1

    print("\nDone. Start the simulator to watch devices converge on these policies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
