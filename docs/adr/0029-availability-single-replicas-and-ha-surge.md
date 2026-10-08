# 0029 — Availability: one replica, ha-surge for drains, replication only where state needs it

- Status: Accepted
- Date: 2026-07-29, amended 2026-08-10
- Current answer: workloads run one replica. Node drains are covered by ha-surge, the owner's
  controller, enabled for every eligible Deployment: it starts the replacement before the evicted
  pod goes. Replication is kept for state ha-surge cannot move, Postgres
  ([0014](0014-one-shared-cloudnative-pg-cluster.md)) and Authelia's Valkey/Sentinel session
  store, and for Authelia itself, which runs two replicas as the login path for everything.

## Context

Drains, by hand and in every k0s upgrade ([0018](0018-k0s-upgrades-through-autopilot.md)), take a
single-replica app down for its restart time; the Swarm had rolled them start-first.

## Decision

- **ha-surge** ([0009](0009-own-code-in-own-repos.md)): an eviction webhook holds the eviction
  with a 429 and removes the pod's `pod-template-hash`, so the ReplicaSet creates a replacement
  while the old pod keeps serving behind the Service; once the replacement is Ready the eviction
  goes through. It never touches `spec.replicas` and creates no pod, so Flux and admission
  policies see nothing to fight. Fail-open.
- **On for everything eligible** (`defaultEnabled: true`), with opt-out per namespace or
  Deployment, rather than labelling each workload. It refuses RWO volumes, `Recreate` and
  Deployments already at two replicas, so files-only volumes on RWX
  ([0007](0007-ceph-csi-storage-classes-by-access-pattern.md)) are what make an app coverable.
  The operator's chart defaults to two replicas, since it is itself on the eviction path.
- **A stateless tier needs only `RollingUpdate`** to be covered, and no PDB: a PDB would refuse
  the drain before the webhook is asked.
- **`maxSurgeSeconds: 600`** (2026-08-10): the default is too short for a slow start under a
  drain. It stays below k0sctl's 12-minute drain timeout, so `k0sctl apply` cannot force-delete a
  pod ha-surge still covers.
- **`strategy: Recreate` needs a reason** stated in a comment (single writer such as SQLite or
  RBD, singleton semantics, fixed address). Otherwise Deployments use `RollingUpdate` 0/1, so
  ha-surge covers them.

## Alternatives considered

- Two permanent replicas with a PDB and spread for every app.
- k0sctl hooks scaling apps up around an upgrade: wasteful.
- `preStop` sleeps: fixed delays.
- Cloning the pod instead of re-parenting it: the clone carries kube-vnet's runtime labels and is
  refused.

## Consequences

- Cover is per Deployment: an app made of several is as available as its least covered part.
  StatefulSets and crashes are not covered (`kube-cluster/TODO.md`).
- Draining the Postgres primary's node needs two eviction attempts; that is CNPG's switchover,
  not ha-surge, which never touches its pods.

## References

- `00e16011`, `9c7cdc00`, `3bfda72f`, `a97ca385` (ha-surge), `0a537167` (600 s, drain timeout),
  `1b3af452`, `53090115` (Authelia).
- Conversation 2026-07-28 and 2026-07-29 (rejected stopgaps, design), 2026-08-09 and 2026-08-10
  (measurements).
- [infra/ha-surge](../../kube-cluster/infra/ha-surge/README.md).
