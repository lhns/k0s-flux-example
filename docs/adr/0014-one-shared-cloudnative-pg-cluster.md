# 0014 — One shared CloudNativePG cluster, on RBD, with synchronous replication

- Status: Accepted
- Date: 2026-07-08, amended 2026-07-09, 2026-07-10, 2026-08-21, 2026-10-08 and 2026-10-09
- Current answer: Postgres-backed apps use one shared CloudNativePG `Cluster` in
  `infra/postgres`, each with its own role and database declared from the app's directory. Three
  instances on `ceph-rbd`, synchronous replication with `dataDurability: required`, a pgbouncer
  `Pooler` in transaction mode as the default entry point. Other engines (MongoDB, MariaDB,
  Redis/Valkey) run as StatefulSets inside the app that needs them.

## Context

The Swarm ran a Postgres per stack. One shared cluster is more resource-efficient, gives one place
to configure replication, backups and failover, and is easy to use from many apps through CNPG's
custom resources.

## Decision

- An operator (`infra/cloudnative-pg`) and one `Cluster` in `infra/` as a platform data service
  ([0005](0005-groups-infra-apps-rbac.md)). Credentials are generated in-cluster or declared per
  app; Reflector mirrors a role's Secret into the app's namespace rather than duplicating it in
  git ([0021](0021-cross-namespace-copies-are-reflected.md)).
- **A role and a database per app** (2026-07-10): `DatabaseRole` and `Database` objects in the
  app's directory, with `databaseReclaimPolicy: retain` so deleting the manifest never drops
  data, and no edit to the shared `cluster.yaml`. A dedicated role, not the bootstrap `app` role.
- **RBD**, not CephFS: CNPG advises against a shared filesystem for PGDATA.
- `max_connections: 500` as a ceiling, with pgbouncer multiplexing (2026-07-09); apps that need
  session features connect to `postgres-rw` directly.
- **Synchronous replication, 3 instances** (2026-08-21): an asynchronous promotion can discard
  committed transactions. `required` durability pauses all writes while no synchronous standby is
  available, hence a third instance, so losing one standby is a non-event. One synchronous
  standby, not two: both give zero data loss, and two would block writes on any single standby
  outage. (The first two instances were chosen because CNPG fails over through the operator, not a
  quorum, so an odd count alone buys nothing.)
- **Health by instances, not `Ready`** (2026-10-08): Flux judges a `Cluster` healthy when all
  instances are ready and no switchover is pending (`healthCheckExprs` in
  `templates/kustomizations.yaml`), because CNPG's `Ready` reflects its whole reconcile loop, not
  whether the database serves. Drop the expression once
  [cloudnative-pg#11627](https://github.com/cloudnative-pg/cloudnative-pg/issues/11627) is fixed in
  a deployed release.
- **Required anti-affinity, switchover updates** (2026-10-08): instances use required pod
  anti-affinity on the hostname, because a PDB cannot stop a drain evicting two co-located
  instances; primary updates go by `switchover`, not an in-place restart. Applies to the dedicated
  cluster below too.
- Backups are [0017](0017-backups-stay-in-ceph.md).

## Exceptions

- One invoicing stack in `apps/` runs a dedicated CNPG cluster in its own namespace (2026-08-06):
  its database is mostly invoice PDFs as `bytea`, too large to share the cluster's volume headroom
  with the other apps. Storage for one stack is not shared infrastructure, so it lives with the app
  ([0005](0005-groups-infra-apps-rbac.md)). It must meet the same backup checks
  ([0017](0017-backups-stay-in-ceph.md)).
- immich runs its own Postgres (2026-07-24): it needs the `vectorchord` extensions, which the
  shared plain-Postgres cluster cannot host.

## Consequences

- Every Postgres-backed app depends on `infra-postgres`, and a failure there is shared.
- Synchronous replication treats the symptom; frequent failovers come from Ceph instability.
- Draining the primary's node needs two eviction attempts; that is the PDB and the switchover
  working ([docs/operations.md](../operations.md#node-maintenance-reboot-shutdown)).
- Copying rows with explicit ids without `setval` desynchronises sequences (AGENTS.md).
- Primary moves can leave a sequence behind its client's recorded position even with zero data
  loss (Postgres logs sequences ahead on standbys). Apps that record positions guard against it
  themselves: Synapse's init guard moves sequences forward only
  ([README](../../kube-cluster/apps/matrix/README.md#stream-sequences)) (2026-10-09).

## References

- `08a2bda6`, `ba0ef6b5`, `39334ad1`, `1e99b6db`, `2c959fcc`, `17de34ed`, `26e9ef2b`,
  `78deadd7` (the dedicated cluster), `153671ed` (immich), `af9c4df6` (health check).
- Conversation 2026-07-08 (two instances), 2026-07-10 (role per app), 2026-08-09 and
  2026-08-20 (instance count), 2026-10-05 (why one cluster).
- [infra/postgres](../../kube-cluster/infra/postgres/README.md),
  [docs/migrating-swarm-to-kubernetes.md, 4.1](../migrating-swarm-to-kubernetes.md#41-postgres--shared-cnpg-the-canonical-path).
