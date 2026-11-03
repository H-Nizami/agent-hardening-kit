# 0005. One worker replica with SQLite on a ReadWriteOnce PVC

## Status

Accepted for the T4 local lab (2026-10-29).

## Context

The worker keeps budget reservations, cached answers and synthetic ticket effects
in SQLite. SQLite transactions make admission atomic on one host. Kubernetes can
run several worker replicas, but SQLite file locking is not safe across nodes or
network filesystems, and a ReadWriteOnce volume attaches to one node.

The goal of T4 is to prove recovery after a killed worker pod on a local machine, inside
a small local Docker budget, with no cloud resources.

## Decision

The Helm chart runs exactly one worker replica with the `Recreate` rollout
strategy. The SQLite store lives on a 1 GiB ReadWriteOnce PVC mounted at `/data`.
The Temporal development server runs as a separate single replica with its own
PVC. Deleting the worker pod keeps both volumes.

## Consequences

- Pod-kill recovery is real but limited to one node. The worker is unavailable
  between pod deletion and replacement Ready; Temporal holds the pending tasks.
- Horizontal scale-out or multi-node failover is **not** supported. It needs a
  shared transactional store (for example PostgreSQL) for admission and effects,
  and a production Temporal deployment. Docs must not imply otherwise.
- `Recreate` avoids two pods opening the same SQLite file during a rollout.
- The kind evidence is observed local recovery, not an availability SLO.
