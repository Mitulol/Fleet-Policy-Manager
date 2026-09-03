#!/usr/bin/env python3
"""Fleet simulator.

Spins up hundreds of virtual devices that behave like real enrolled endpoints:
each one enrolls, then independently sends heartbeats, periodically checks what
policy its group is expected to run, applies it after a realistic delay, and
reports its own posture.

All of that traffic goes through the API gateway with a device-scoped
credential, so the simulator exercises the same authentication, rate limiting,
routing and load balancing path a real agent would.

Two design points, both learned from measuring this against the running stack:

  Multiple processes. A single Python event loop saturates long before the
  platform does -- driving one process harder actually *lowers* throughput,
  because the added concurrency queues inside the client rather than at the
  server. Devices are therefore split across worker processes so the numbers
  reported describe the platform rather than the load generator.

  Ramped start. Enrolling every device in the same instant is a connection
  storm no real fleet produces, and it dominates the latency percentiles for
  the rest of the run. Devices are brought online spread over a ramp window.

Two cohorts make the dashboard show something other than a flat 100%:

  drifting devices -- never converge on new policy versions, so rollout
                      progress plateaus below 100% the way a real fleet does
  failing devices  -- report a failed local posture check, so there is always
                      a population of genuinely non-compliant endpoints

Usage:
    python3 tools/simulator.py --devices 500 --duration 120
"""

from __future__ import annotations

import argparse
import asyncio
import multiprocessing as mp
import os
import queue as queue_module
import random
import statistics
import time
from collections import Counter, deque
from dataclasses import dataclass, field

import httpx

DEFAULT_GROUPS = ["engineering", "sales", "executive", "kiosk"]
OPERATING_SYSTEMS = [
    ("windows", "11 23H2"),
    ("windows", "10 22H2"),
    ("macos", "14.5"),
    ("linux", "ubuntu-24.04"),
    ("android", "14"),
]
POSTURE_CHECKS = ["disk_encryption", "firewall", "antivirus", "screen_lock", "os_patch_level"]

# Cap on latency samples shipped from a worker in one snapshot, so the parent
# can compute real percentiles without moving megabytes between processes.
LATENCY_SAMPLE_CAP = 3000


@dataclass
class Stats:
    """Counters for one worker process."""

    heartbeats_ok: int = 0
    reports_ok: int = 0
    policy_reads_ok: int = 0
    enrollments_ok: int = 0
    errors: Counter = field(default_factory=Counter)
    served_by: Counter = field(default_factory=Counter)
    # Latency observed since the last snapshot was taken.
    window_latencies: deque[float] = field(default_factory=lambda: deque(maxlen=50_000))
    all_latencies: deque[float] = field(default_factory=lambda: deque(maxlen=200_000))

    def record_error(self, label: str) -> None:
        self.errors[label] += 1

    def record_latency(self, ms: float) -> None:
        self.window_latencies.append(ms)
        self.all_latencies.append(ms)

    def snapshot(self, worker_id: int, final: bool = False) -> dict:
        """Drain the window into a picklable snapshot for the parent process.

        Counters are cumulative, so the parent keeps only the newest snapshot
        per worker and sums across workers. Latency, by contrast, is drained
        each time so the progress line reports the most recent window rather
        than an average dragged down by the ramp.
        """
        window = list(self.window_latencies)
        self.window_latencies.clear()
        if len(window) > LATENCY_SAMPLE_CAP:
            window = random.sample(window, LATENCY_SAMPLE_CAP)

        # Errors and replica hits are drained, not sent cumulatively, because
        # the parent accumulates them across snapshots. Sending running totals
        # here would multiply-count them on every tick.
        errors = dict(self.errors)
        self.errors.clear()
        served_by = dict(self.served_by)
        self.served_by.clear()

        payload = {
            "worker_id": worker_id,
            "final": final,
            "heartbeats_ok": self.heartbeats_ok,
            "reports_ok": self.reports_ok,
            "policy_reads_ok": self.policy_reads_ok,
            "enrollments_ok": self.enrollments_ok,
            "errors": errors,
            "served_by": served_by,
            "window_latencies": window,
        }
        if final:
            overall = list(self.all_latencies)
            if len(overall) > LATENCY_SAMPLE_CAP * 4:
                overall = random.sample(overall, LATENCY_SAMPLE_CAP * 4)
            payload["all_latencies"] = overall
        return payload


def percentiles(samples: list[float]) -> tuple[float, float, float]:
    if not samples:
        return (0.0, 0.0, 0.0)
    ordered = sorted(samples)

    def pick(p: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 1)

    return (round(statistics.median(ordered), 1), pick(0.95), pick(0.99))


class VirtualDevice:
    """One simulated endpoint running its own independent lifecycle."""

    def __init__(
        self,
        index: int,
        client: httpx.AsyncClient,
        stats: Stats,
        args: argparse.Namespace,
    ) -> None:
        self.device_id = f"sim-device-{index:05d}"
        self.hostname = f"WS-{index:05d}"
        self.os, self.os_version = random.choice(OPERATING_SYSTEMS)
        self.group = random.choice(args.groups)
        self.client = client
        self.stats = stats
        self.args = args

        # Policy id to the version this device has actually applied.
        self.applied: dict[str, int] = {}
        # A device that never converges, modelling an endpoint that is powered
        # off, out of contact, or failing to install the payload.
        self.drifts = random.random() < args.drift_rate
        # A device with a genuinely failing posture check.
        self.failing_check = (
            random.choice(POSTURE_CHECKS)
            if random.random() < args.noncompliance_rate
            else None
        )

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response | None:
        started = time.perf_counter()
        try:
            response = await self.client.request(method, url, **kwargs)
        except httpx.TimeoutException:
            self.stats.record_error("timeout")
            return None
        except httpx.RequestError as exc:
            self.stats.record_error(f"transport:{type(exc).__name__}")
            return None

        self.stats.record_latency((time.perf_counter() - started) * 1000)
        served_by = response.headers.get("X-Served-By")
        if served_by:
            self.stats.served_by[served_by] += 1

        if response.status_code >= 400:
            self.stats.record_error(f"http:{response.status_code}")
            return None
        return response

    async def enroll(self) -> bool:
        response = await self._send(
            "POST",
            "/api/registry/devices",
            json={
                "device_id": self.device_id,
                "hostname": self.hostname,
                "os": self.os,
                "os_version": self.os_version,
                "device_group": self.group,
                "agent_version": "1.4.2",
            },
        )
        if response is None:
            return False
        self.stats.enrollments_ok += 1
        return True

    async def heartbeat_loop(self, deadline: float) -> None:
        await asyncio.sleep(random.uniform(0, self.args.heartbeat_interval))
        while time.time() < deadline:
            response = await self._send(
                "POST",
                f"/api/registry/devices/{self.device_id}/heartbeat",
                json={"agent_version": "1.4.2"},
            )
            if response is not None:
                self.stats.heartbeats_ok += 1
            await asyncio.sleep(self.args.heartbeat_interval * random.uniform(0.85, 1.15))

    async def policy_loop(self, deadline: float) -> None:
        """Fetch the group's expected policy set and converge on it.

        Convergence is deliberately not instant: a real endpoint installs a
        payload and reboots, so the dashboard should show a rollout climbing
        rather than snapping to 100%.
        """
        await asyncio.sleep(random.uniform(0, self.args.policy_interval))
        while time.time() < deadline:
            response = await self._send("GET", f"/api/policy/policies/for-group/{self.group}")
            if response is not None:
                self.stats.policy_reads_ok += 1
                if not self.drifts:
                    for policy in response.json():
                        current = self.applied.get(policy["id"], 0)
                        if policy["version"] > current and random.random() < 0.6:
                            self.applied[policy["id"]] = policy["version"]
            await asyncio.sleep(self.args.policy_interval * random.uniform(0.8, 1.2))

    async def report_loop(self, deadline: float) -> None:
        await asyncio.sleep(random.uniform(0, self.args.report_interval))
        while time.time() < deadline:
            checks = {name: True for name in POSTURE_CHECKS}
            if self.failing_check:
                checks[self.failing_check] = False

            response = await self._send(
                "POST",
                "/api/compliance/reports",
                json={
                    "device_id": self.device_id,
                    "device_group": self.group,
                    "applied_policies": self.applied,
                    "checks": checks,
                },
            )
            if response is not None:
                self.stats.reports_ok += 1
            await asyncio.sleep(self.args.report_interval * random.uniform(0.85, 1.15))

    async def run(self, start_at: float, deadline: float) -> None:
        # Ramped start: the fleet comes online spread over the ramp window
        # instead of every device connecting in the same instant.
        delay = start_at - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        if not await self.enroll():
            return
        await asyncio.gather(
            self.heartbeat_loop(deadline),
            self.policy_loop(deadline),
            self.report_loop(deadline),
            return_exceptions=True,
        )


async def _worker_main(
    worker_id: int, indices: list[int], args: argparse.Namespace, out: mp.Queue
) -> None:
    random.seed((args.seed or 0) + worker_id if args.seed is not None else None)
    stats = Stats()

    limits = httpx.Limits(
        max_connections=args.connections_per_worker,
        max_keepalive_connections=args.connections_per_worker,
    )
    async with httpx.AsyncClient(
        base_url=args.gateway,
        headers={"X-API-Key": args.api_key},
        timeout=httpx.Timeout(20.0),
        limits=limits,
    ) as client:
        devices = [VirtualDevice(i, client, stats, args) for i in indices]

        now = time.time()
        deadline = now + args.ramp_seconds + args.duration
        runners = [
            asyncio.create_task(
                device.run(now + (position / max(1, len(devices))) * args.ramp_seconds, deadline)
            )
            for position, device in enumerate(devices)
        ]

        async def emit() -> None:
            while True:
                await asyncio.sleep(args.progress_interval)
                out.put(stats.snapshot(worker_id))

        emitter = asyncio.create_task(emit())
        try:
            await asyncio.gather(*runners, return_exceptions=True)
        finally:
            emitter.cancel()
            out.put(stats.snapshot(worker_id, final=True))


def _worker_entry(worker_id: int, indices: list[int], args: argparse.Namespace, out: mp.Queue) -> None:
    try:
        asyncio.run(_worker_main(worker_id, indices, args, out))
    except KeyboardInterrupt:
        pass


def _print_progress_header() -> None:
    print(
        f"\n{'elapsed':>8} {'req/s':>9} {'beats':>9} {'reports':>9} {'policy':>8} "
        f"{'p50':>9} {'p95':>9} {'errors':>7}  replica distribution"
    )


def run_fleet(args: argparse.Namespace) -> None:
    total_run = args.ramp_seconds + args.duration
    indices = list(range(1, args.devices + 1))
    chunks: list[list[int]] = [indices[i :: args.processes] for i in range(args.processes)]
    chunks = [c for c in chunks if c]

    out: mp.Queue = mp.Queue()
    workers = [
        mp.Process(target=_worker_entry, args=(wid, chunk, args, out), daemon=True)
        for wid, chunk in enumerate(chunks)
    ]

    print(f"Starting {args.devices} virtual devices against {args.gateway}")
    print(f"  worker processes    {len(workers)} ({[len(c) for c in chunks]} devices each)")
    print(f"  groups              {', '.join(args.groups)}")
    print(f"  ramp / duration     {args.ramp_seconds}s ramp, then {args.duration}s steady state")
    print(f"  drift rate          {args.drift_rate:.0%} of devices never converge")
    print(f"  failing checks      {args.noncompliance_rate:.0%} of devices")

    started = time.time()
    for worker in workers:
        worker.start()

    COUNTERS = ("heartbeats_ok", "reports_ok", "policy_reads_ok", "enrollments_ok")

    # Newest cumulative snapshot per worker. Summing every snapshot ever
    # received would multiply-count, so each worker keeps exactly one slot.
    latest: dict[int, dict] = {}
    # Errors and replica hits are drained per snapshot, so these accumulate.
    errors: Counter = Counter()
    served_by: Counter = Counter()
    final_latencies: list[float] = []
    window_latencies: list[float] = []
    finals_seen = 0

    _print_progress_header()
    previous_total = 0
    previous_at = started
    hard_stop = started + total_run + 30

    while finals_seen < len(workers) and time.time() < hard_stop:
        try:
            snapshot = out.get(timeout=1.0)
        except queue_module.Empty:
            snapshot = None

        if snapshot is not None:
            latest[snapshot["worker_id"]] = snapshot
            window_latencies.extend(snapshot["window_latencies"])
            errors.update(snapshot["errors"])
            served_by.update(snapshot["served_by"])
            if snapshot["final"]:
                finals_seen += 1
                final_latencies.extend(snapshot.get("all_latencies", []))

        now = time.time()
        if now - previous_at >= args.progress_interval:
            grand = {k: sum(s[k] for s in latest.values()) for k in COUNTERS}
            total_requests = (
                grand["heartbeats_ok"] + grand["reports_ok"] + grand["policy_reads_ok"]
            )
            rate = (total_requests - previous_total) / max(1e-6, now - previous_at)
            p50, p95, _ = percentiles(window_latencies)
            spread = " ".join(f"{n}={c}" for n, c in sorted(served_by.items())) or "—"
            print(
                f"{now - started:7.0f}s {rate:9.1f} {grand['heartbeats_ok']:9d} "
                f"{grand['reports_ok']:9d} {grand['policy_reads_ok']:8d} "
                f"{p50:7.1f}ms {p95:7.1f}ms {sum(errors.values()):7d}  {spread}"
            )
            previous_total, previous_at = total_requests, now
            window_latencies = []

    for worker in workers:
        worker.join(timeout=10)

    grand = {k: sum(s[k] for s in latest.values()) for k in COUNTERS}
    _print_summary(grand, errors, served_by, final_latencies, time.time() - started, args)


def _print_summary(
    grand: dict[str, int],
    errors: Counter,
    served_by: Counter,
    latencies: list[float],
    wall: float,
    args: argparse.Namespace,
) -> None:
    total = grand["heartbeats_ok"] + grand["reports_ok"] + grand["policy_reads_ok"]
    p50, p95, p99 = percentiles(latencies)
    error_count = sum(errors.values())

    print("\n" + "=" * 78)
    print("FLEET SIMULATION SUMMARY")
    print("=" * 78)
    print(f"  virtual devices          {args.devices}")
    print(f"  worker processes         {args.processes}")
    print(f"  wall clock               {wall:.1f}s")
    print(f"  devices enrolled         {grand['enrollments_ok']}")
    print(f"  heartbeats delivered     {grand['heartbeats_ok']}")
    print(f"  compliance reports       {grand['reports_ok']}")
    print(f"  policy reads             {grand['policy_reads_ok']}")
    print(f"  total requests           {total}")
    print(f"  sustained throughput     {total / max(wall, 1e-6):.1f} req/s")
    print(f"  latency p50 / p95 / p99  {p50}ms / {p95}ms / {p99}ms")
    print(
        f"  errors                   {error_count} "
        f"({100 * error_count / max(total + error_count, 1):.2f}%)"
    )

    if served_by:
        print("\n  Compliance replica distribution")
        handled = sum(served_by.values())
        for instance, count in sorted(served_by.items()):
            share = 100 * count / handled
            print(f"    {instance:<18} {count:>8}  {share:5.1f}%  {'#' * int(share / 2)}")

    if errors:
        print("\n  Errors by type")
        for label, count in errors.most_common():
            print(f"    {label:<30} {count}")
    print("=" * 78)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate a fleet of managed devices.")
    parser.add_argument("--devices", type=int, default=500)
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--api-key", default="fleet-device-key")
    parser.add_argument("--duration", type=int, default=120, help="seconds of steady state after ramp")
    parser.add_argument("--ramp-seconds", type=float, default=15.0,
                        help="window over which devices come online")
    parser.add_argument("--processes", type=int, default=min(4, os.cpu_count() or 1),
                        help="load-generator worker processes")
    parser.add_argument("--connections-per-worker", type=int, default=60)
    parser.add_argument("--heartbeat-interval", type=float, default=15.0)
    parser.add_argument("--report-interval", type=float, default=10.0)
    parser.add_argument("--policy-interval", type=float, default=12.0)
    parser.add_argument("--drift-rate", type=float, default=0.12,
                        help="fraction of devices that never converge on new policy versions")
    parser.add_argument("--noncompliance-rate", type=float, default=0.15,
                        help="fraction of devices with a failing posture check")
    parser.add_argument("--groups", default=",".join(DEFAULT_GROUPS))
    parser.add_argument("--progress-interval", type=float, default=5.0)
    parser.add_argument("--seed", type=int, default=None)

    args = parser.parse_args()
    args.groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    args.processes = max(1, min(args.processes, args.devices))
    return args


if __name__ == "__main__":
    try:
        run_fleet(parse_args())
    except KeyboardInterrupt:
        print("\nInterrupted.")
