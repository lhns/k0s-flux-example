# 0054 — The descheduler re-spreads replicas

- Status: Accepted
- Date: 2026-10-08
- Current answer: `infra/descheduler` evicts replicas that bunch on one node, every 15 minutes,
  with spread-only plugins and wide pod protections.

## Context

The scheduler places a pod once, at creation, and ignores preferred spread afterwards. After a
drain or node loss the replicas of a set end up on few nodes and stay there.

## Decision

- Plugins: `RemoveDuplicates`, `RemovePodsViolatingTopologySpreadConstraint` (hard and soft) and
  `RemovePodsViolatingInterPodAntiAffinity`. No `LowNodeUtilization`: it churns pods for balance
  nobody asked for.
- `DefaultEvictor`: `minReplicas: 2`, `nodeFit`, `minPodAge: 15m`. Pods with a PVC, system-critical
  pods and DaemonSet pods are protected, as is `kube-system`.
- At most one eviction per node and three per cycle.
- Quorum and HA data sets (databases, brokers) must not depend on this: they use required
  anti-affinity, as in [0014](0014-one-shared-cloudnative-pg-cluster.md).

## Consequences

- A replica of a set restarts when moved; PDBs bound how many at once.
- Singletons and `Recreate` apps are never touched.
- Spread recovers within 15 minutes of a node returning, not instantly.
