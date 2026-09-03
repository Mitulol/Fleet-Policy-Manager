# Architecture

This document explains why the system is shaped the way it is. It assumes you
have read the overview in the top-level [README](../README.md).

There are three decisions worth understanding in depth: how the system is split
into services, how those services communicate, and how one of them stays
available when an instance is lost.

---

## 1. Splitting the system into services

### The rule

Three backend services, each a separate process with a separate database:

- **Device Registry Service** — who is in the fleet and whether each device is
  currently checking in.
- **Policy Service** — what configuration the fleet is supposed to have, and the
  history of rolling those configurations out.
- **Compliance Service** — whether each device actually matches what it is
  supposed to have.

No service holds a connection string to another service's database. If the
Compliance Service needs to know a device's group, it either receives it on the
report or asks the Registry over HTTP. If it needs to know what policy a group
should run, it learns that from an event. This is enforced by construction:
nothing in the compose file wires one service to another's store.

### Why split it here and not somewhere else

These three responsibilities change for different reasons and at different rates.
Policy definitions change when an operator decides they should — rarely, and
through a human. Heartbeats arrive constantly and are almost pure writes.
Compliance evaluation is read-heavy against policy state and write-heavy against
device state, and its load scales with fleet size. Bundling them would mean one
schema, one deployment, and one scaling decision serving three workloads that
want different things.

The split also contains failure. The graceful-degradation behaviour — Policy
down, Compliance still evaluating against the last known rollout — is only
possible because the two do not share a process or a database.

### Why the data stores differ

| Service | Store | Reason |
|---|---|---|
| Registry | SQLite | One writer. Access is essentially by primary key. Write volume is heartbeats, which are cheap single-row updates. |
| Policy | SQLite | One writer. Very low write volume. |
| Compliance | PostgreSQL | **Several** writers — the service runs as multiple instances — that must agree on one view of the fleet. |

The Compliance store is the interesting one. The service scales horizontally,
which rules out a database file private to each instance: three instances would
maintain three diverging pictures of fleet compliance, and the dashboard would
show whichever instance happened to answer. One PostgreSQL database behind all
the instances keeps them consistent.

This does not break the "database per service" rule. The rule is about services
not reaching into each other's data. A store shared between identical copies of
one service is still that service's private store — the copies are an
implementation detail of how the service scales.

### What each instance writes, and the contention that caused

Every compliance report does three writes in one transaction: upsert the
device's current compliance row, append to the report history, and — originally
— bump this instance's row in a per-instance activity table.

That third write was a mistake. There is one activity row per instance, so every
concurrent report on an instance took a row lock on the same record and the
whole ingest path serialised behind it. Load testing put the ceiling around 48
reports per second per instance regardless of hardware.

The fix: the per-instance counters are now kept in memory and flushed to the
database on a short interval. The ingest transaction no longer touches them. The
exposure is that an instance killed between flushes loses up to one interval of
its own throughput metrics — which, for a metric, is fine.

### Report durability

Compliance reports are relaxed to group-commit rather than flushing each
transaction to disk. The justification is specific to what a compliance report
is: high-frequency telemetry that supersedes itself. Every device re-reports its
full posture within seconds, so the newest report always replaces the last. If
the database server itself crashes and loses the last fraction of a second of
reports, the fleet re-reports that window on its next interval. It is not a
corruption risk — just a durability window that this particular data does not
need. Enrollment and policy definitions, where a lost write would genuinely
matter, are owned by other services and are not affected.

On this development machine the change lifts fleet-wide ingest from roughly 50 to
over 600 reports per second.

---

## 2. How the services communicate

### Two mechanisms, chosen per interaction

**Synchronous REST** wherever a caller is blocked waiting for an answer:
the gateway to every service, a device agent reading its group's policy set, the
dashboard aggregate.

**Asynchronous events** between the Policy Service and the Compliance Service.
Publishing a policy writes a durable event and returns. The Policy Service holds
no reference to the Compliance Service — not its address, not a client, nothing.

### Why the Policy → Compliance link is asynchronous

A policy rollout is an announcement, not a request. When an operator publishes a
policy, the meaningful outcome is "the fleet's expected configuration has
changed." Nothing about that needs the Compliance Service to be reachable at
that instant. Making it a synchronous call would mean:

- a rollout fails if Compliance is mid-deploy;
- the operator waits on Compliance's latency to publish;
- the Policy Service has to know how many Compliance instances exist and how to
  reach them.

As an event, none of that is true. The rollout is recorded, the event is
durable, and Compliance applies it whenever it is ready — now, or after it
finishes restarting.

### Why a consumer group and not publish/subscribe

This is the decision that the horizontal scaling of Compliance forces.

Plain publish/subscribe delivers every message to every connected subscriber and
keeps no history. With one Compliance instance that is adequate. With three, it
is a correctness bug: all three would receive each rollout event and all three
would apply it. And any instance that happened to be restarting when an event
was published would miss it permanently, because publish/subscribe does not
retain anything.

A Redis stream consumed by a named consumer group fixes both:

- **Exactly-once delivery within the group.** Each event goes to one instance,
  whichever is free.
- **Durability.** The stream retains events. An instance that was down reads what
  it missed when it reconnects.
- **Reclaim of abandoned work.** An event that an instance claimed but did not
  acknowledge — the signature of an instance killed mid-handler — is reclaimed
  by a peer after an idle timeout and retried.

The event handler is written to be safe under redelivery: applying a policy
expectation is an upsert that never moves a policy's version backwards, so
processing the same event twice is harmless.

### Tolerating an event bus outage

The Policy Service treats publishing as best-effort. If Redis is unreachable, the
policy write still commits, the rollout is still recorded locally, and the
failure is logged rather than raised. An event bus outage degrades propagation;
it does not take policy authoring down.

---

## 3. Staying available when a Compliance instance is lost

### The setup

- Three identical Compliance instances.
- An nginx load balancer in front of them using least-connections routing —
  compliance reports vary in cost with how many policies apply to the device's
  group, so routing to the least-busy instance spreads real work better than
  round-robin spreads request counts.
- The balancer health-checks each instance and removes one from rotation after
  repeated failures, re-probing it later.
- If a request fails because its instance died mid-flight, the balancer retries
  the same request against another instance.
- All three share the PostgreSQL store, so any instance can serve any request
  and no state is pinned to a particular instance.

### What failure looks like

From `tools/ha_failover_test.py`, run with the fleet simulator generating load:

```
--- BASELINE — all instances healthy ---
  t+ 15s  ingest= 62.9/s  live_replicas=3/3  [c-1:21.7/s  c-2:20.5/s  c-3:20.7/s]

  >>> stopping compliance-2
--- DEGRADED — running on two instances ---
  t+ 27s  ingest= 52.6/s  live_replicas=3/3  [c-1:26.1/s  c-2: 0.0/s  c-3:26.5/s]   ← balancer still probing c-2
  t+ 32s  ingest= 61.1/s  live_replicas=2/3  [c-1:30.8/s  c-2: 0.0/s (no traffic)  c-3:30.2/s]
  t+ 57s  ingest= 57.4/s  live_replicas=2/3  [c-1:27.9/s  c-2: 0.0/s (no traffic)  c-3:29.5/s]

  >>> starting compliance-2
--- RECOVERY — instance rejoining ---
  t+ 69s  ingest= 61.4/s  live_replicas=3/3  [c-1:27.7/s  c-2: 5.4/s  c-3:28.3/s]
  t+ 84s  ingest= 61.2/s  live_replicas=3/3  [c-1:20.7/s  c-2:19.3/s  c-3:21.1/s]
```

The window between stopping the instance and the balancer noticing is a few
seconds, during which some requests to the dead instance are retried against a
live one and succeed with added latency. After that, the two survivors carry the
full load at the same aggregate throughput. When the third instance returns it
rejoins within one health-check interval and load rebalances.

No reports are lost across the whole sequence — confirmed by the report count
climbing monotonically throughout.

### Why this is the availability feature worth having

Health-check-with-restart would have been simpler, but Docker's own restart
policy already does most of it. Horizontal scaling with failover is the feature
that actually matches how a fleet-scale ingest service is run: capacity is added
by adding instances, and losing one is expected rather than exceptional.

---

## Request paths

### A device heartbeat

```
device agent
  → POST /api/registry/devices/{id}/heartbeat   (X-API-Key: device key)
  → gateway: resolve key → device role
  → gateway: authorise (device role may call this route)
  → gateway: consume a rate-limit token for this credential
  → gateway: forward to registry:8001
  → registry: update last-seen timestamp and heartbeat count
  ← 200
```

### Publishing a policy

```
operator
  → POST /api/policy/policies/{id}/publish       (X-API-Key: admin key)
  → gateway: resolve key → admin role → forward to policy:8002
  → policy: write a "policy.published" event to the Redis stream
  → policy: mark the policy published, record the rollout locally
  ← 202 Accepted   (devices converge asynchronously)

  ... independently, some time later ...

  compliance instance (one of three, whichever the consumer group hands it to)
    → reads the event from the stream
    → upserts its local expectation: group X should run policy Y at version N
    → acknowledges the event
```

### A compliance evaluation

```
device agent
  → POST /api/compliance/reports                 (X-API-Key: device key)
     body: applied policy versions + local check results
  → gateway → compliance load balancer → least-busy instance
  → instance: look up what this device's group is expected to run
  → instance: compare applied versions against expected; collect failed checks
  → instance: write the verdict, append to history
  ← 202   { compliant: false, findings: ["policy_drift:…", "check_failed:firewall"], served_by: "compliance-3" }
```

The `served_by` field and the `X-Served-By` response header are how instance
selection is observed from outside the cluster.
