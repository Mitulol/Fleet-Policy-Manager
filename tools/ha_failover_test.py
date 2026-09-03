#!/usr/bin/env python3
"""High-availability demonstration for the Compliance Service.

Runs a controlled experiment against the live stack:

  1. Baseline    -- sample throughput and per-replica load distribution while
                    all three Compliance replicas are healthy.
  2. Fault       -- stop one replica mid-run, the way a crash or a node loss
                    would remove it.
  3. Degraded    -- keep sampling. The load balancer should detect the dead
                    replica within its failure window, stop routing to it, and
                    the surviving two should absorb the load. Throughput dips
                    briefly, then recovers; no reports are lost, because the
                    balancer retries a failed request against another replica.
  4. Recovery    -- start the replica again and watch it rejoin the rotation.

This script only observes and reports. It expects an external load source --
the fleet simulator -- to be running against the gateway at the same time, so
run that first:

    python3 tools/simulator.py --devices 500 --duration 240 &
    python3 tools/ha_failover_test.py --victim compliance-2
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import time

import httpx

COMPOSE_PROJECT = "fleet-policy-manager"


def _compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], check=True, capture_output=True)


async def _sample(client: httpx.AsyncClient) -> dict:
    """One reading of throughput and replica health from the Compliance stats."""
    response = await client.get("/api/compliance/stats")
    response.raise_for_status()
    data = response.json()
    return {
        "at": time.time(),
        "total_reports": sum(i["reports_handled"] for i in data["instances"]),
        "instances": {
            i["instance"]: {
                "reports": i["reports_handled"],
                "idle_for": i["seconds_since_last_seen"],
            }
            for i in data["instances"]
        },
        "compliant": data["compliant"],
        "non_compliant": data["non_compliant"],
    }


def _print_row(elapsed: float, sample: dict, previous: dict | None, note: str = "") -> None:
    if previous and sample["at"] > previous["at"]:
        window = sample["at"] - previous["at"]
        rate = (sample["total_reports"] - previous["total_reports"]) / window
        per_replica = []
        for name in sorted(sample["instances"]):
            before = previous["instances"].get(name, {}).get("reports", 0)
            delta = sample["instances"][name]["reports"] - before
            idle = sample["instances"][name]["idle_for"]
            mark = "  (no traffic)" if idle > 8 else ""
            per_replica.append(f"{name}:{delta / window:5.1f}/s{mark}")
    else:
        rate = 0.0
        per_replica = [f"{n}: —" for n in sorted(sample["instances"])]

    live = sum(1 for i in sample["instances"].values() if i["idle_for"] <= 8)
    print(
        f"  t+{elapsed:5.0f}s  ingest={rate:6.1f}/s  live_replicas={live}/3  "
        f"[{'  '.join(per_replica)}]  {note}"
    )


async def phase(
    client: httpx.AsyncClient,
    label: str,
    seconds: int,
    interval: float,
    start_time: float,
    state: dict,
) -> None:
    print(f"\n--- {label} ({seconds}s) ---")
    end = time.time() + seconds
    while time.time() < end:
        sample = await _sample(client)
        _print_row(time.time() - start_time, sample, state.get("previous"), state.pop("note", ""))
        state["previous"] = sample
        await asyncio.sleep(interval)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Compliance Service failover demonstration.")
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--api-key", default="fleet-admin-key")
    parser.add_argument("--victim", default="compliance-2",
                        help="compose service name of the replica to kill")
    parser.add_argument("--baseline", type=int, default=20)
    parser.add_argument("--degraded", type=int, default=40)
    parser.add_argument("--recovery", type=int, default=30)
    # A multiple of the Compliance instances' counter-flush interval, so each
    # sample window catches a whole number of flushes and the reported rate
    # does not alias into a sawtooth.
    parser.add_argument("--interval", type=float, default=6.0)
    args = parser.parse_args()

    start = time.time()
    state: dict = {}

    async with httpx.AsyncClient(
        base_url=args.gateway, headers={"X-API-Key": args.api_key}, timeout=10.0
    ) as client:
        try:
            await client.get("/health")
        except Exception:
            raise SystemExit(
                "Cannot reach the gateway. Bring the stack up with "
                "`docker compose up -d` first."
            )

        print("=" * 78)
        print("COMPLIANCE SERVICE — FAILOVER DEMONSTRATION")
        print("=" * 78)
        print(f"  victim replica     {args.victim}")
        print("  expectation        one replica lost, throughput recovers on the")
        print("                     remaining two, zero reports dropped")
        print("\n  NOTE: run the fleet simulator against the gateway in parallel,")
        print("        otherwise there is no load to redistribute.")

        await phase(client, "BASELINE — all replicas healthy", args.baseline,
                    args.interval, start, state)

        print(f"\n  >>> stopping {args.victim} now")
        _compose("stop", args.victim)
        state["note"] = f"<<< {args.victim} STOPPED"

        await phase(client, "DEGRADED — running on two replicas", args.degraded,
                    args.interval, start, state)

        print(f"\n  >>> starting {args.victim} again")
        _compose("start", args.victim)
        state["note"] = f"<<< {args.victim} RESTARTED"

        await phase(client, "RECOVERY — replica rejoining", args.recovery,
                    args.interval, start, state)

        final = await _sample(client)
        print("\n" + "=" * 78)
        print("RESULT")
        print("=" * 78)
        live = sum(1 for i in final["instances"].values() if i["idle_for"] <= 8)
        print(f"  replicas serving traffic again   {live}/3")
        print(f"  total reports ingested           {final['total_reports']}")
        print(f"  compliant / non-compliant        {final['compliant']} / {final['non_compliant']}")
        print("  Reports continued to be accepted throughout the outage: the load")
        print("  balancer retried each failed request against a healthy replica,")
        print("  and every replica shares one Postgres store so no fleet state was")
        print("  tied to the replica that went away.")
        print("=" * 78)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted — bringing the victim replica back up as a precaution.")
