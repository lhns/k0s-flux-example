# postgres

A CloudNativePG `Cluster` — a 2-instance HA PostgreSQL (1 primary + 1 hot
standby) on the `ceph-rbd` (Ceph RBD block) StorageClass. Requires the operator
(`infra/cloudnative-pg`), so `kustomization.yaml` carries
`dependsOn: infra-cloudnative-pg`.

- `cluster.yaml` — the `Cluster` (2 instances, ceph-rbd 20Gi, `initdb` db/owner `app`).
  CNPG generates the `postgres-app` secret (user + password) in-cluster. Also wires
  backups (`spec.backup.volumeSnapshot` + the Barman Cloud Plugin as WAL archiver) and
  tuning (`max_connections: 500`, `shared_buffers: 512MB`, 1Gi/2Gi memory) — see
  **Connections & pooling** below.
- `pooler.yaml` — a CNPG `Pooler` (pgbouncer, transaction mode) — see **Connections & pooling**.
- `objectstore.yaml` / `scheduledbackup.yaml` / `s3-secret.yaml` — see **Backups** below.
- `vnet.yaml` — three kube-vnet channels:
  - **`postgres-internal`** — CNPG's own traffic (instance↔instance replication +
    operator→instance), scoped to the cluster + `cnpg-system`. Not for clients.
  - **`postgres`** — the direct client channel (like the `traefik` vnet): any namespace
    may join. The instances accept ingress; clients never see replication/operator.
  - **`postgres-pooler`** — the client channel to the pgbouncer pooler (same opt-in model).

## Connections & pooling
`max_connections` is **500** — a *ceiling*, not a target. Postgres forks a backend process
per connection, so serving ~1000 app connections directly would be wasteful. Instead, the
`postgres-pooler` `Pooler` (pgbouncer, **transaction** mode) fronts the primary and
multiplexes many client connections onto a few dozen real backends (`2 pods × 25
default_pool_size ≈ 50` per user/db pair). `shared_buffers` is `512MB` and each instance is
guaranteed 1Gi / limited to 2Gi RAM.

Two ways to connect (both opt into a vnet with **egress**, then dial port 5432):

| Path | Service | vnet (egress) | When |
|------|---------|---------------|------|
| **Pooled** (default) | `postgres-pooler` | `postgres-pooler` | Most apps. Transaction pooling — no session-scoped features. |
| **Direct** | `postgres-rw` (primary) / `postgres-ro` (replicas) | `postgres` | Apps needing LISTEN/NOTIFY, advisory locks, session `SET`, etc. Counts against `max_connections`. |

Pooler auth is automatic (the operator manages the `cnpg_pooler_pgbouncer` role + a
`user_search()` function + TLS client-cert auth); no secret to manage. Credentials for the
app user are still in `secret/postgres-app`. If some apps genuinely need session pooling, add
a second `Pooler` with `poolMode: session` rather than downgrading this one.

## Backups
Point-in-time recovery, via the Barman Cloud Plugin (`infra/barman-cloud-plugin`):
- **Base backups** are cheap **Ceph volume snapshots** (daily `ScheduledBackup`, method
  `volumeSnapshot`, class `ceph-rbd-snapshotclass`) — they stay in Ceph, so restores are fast.
- **WAL** is continuously archived to **Ceph RGW** (`s3://kube-barman`, 30d) by the plugin's
  `ObjectStore` — that's what makes PITR possible.
- Restore: a new `Cluster` with `spec.bootstrap.recovery` (snapshot base + WAL replay).

Trade-off: snapshots live in Ceph → this survives logical/oops errors and gives PITR, but
**not a full Ceph loss** (WAL-in-S3 alone can't rebuild a DB). For off-Ceph DR, add a weekly
`method: plugin` object-store base backup (same plugin/bucket).

## Connecting a client (from any namespace)
**Pooled (default):** join the `postgres-pooler` vnet with **egress** and connect to
service `postgres-pooler`:
```yaml
metadata:
  labels:
    kube-vnet/net.postgres.postgres-pooler: egress
```
**Direct** (session-feature apps): join the `postgres` vnet with **egress** instead:
```yaml
metadata:
  labels:
    kube-vnet/net.postgres.postgres: egress
```
(Either can also be a `VirtualNetworkBinding` — `direction: egress`, `virtualNetworkRef` →
the matching vnet in namespace `postgres`.) Then connect on port 5432 — `postgres-pooler` for
pooled, or `postgres-rw` (primary) / `postgres-ro` (replicas) for direct; credentials in
`secret/postgres-app` (namespace `postgres`).

## Storage migration — NEVER delete + recreate a cluster with data

CNPG **cannot change `spec.storage.storageClass` in place** — a bound PVC's class is
immutable — and **deleting the `Cluster` destroys all its data**. So do *not* "change
the class and recreate the cluster." Migrate **with zero downtime and no data loss**
instead, by rebuilding one instance at a time from a live peer (`pg_basebackup`):

1. Change `spec.storage.storageClass` (and/or `size`) in `cluster.yaml`; commit + push.
   Existing PVCs stay put — this only governs *newly created* ones.
2. **Rebuild each replica on the new class, one at a time.** For a replica `postgres-N`:
   ```sh
   kubectl -n postgres delete pod  postgres-N
   kubectl -n postgres delete pvc  postgres-N
   ```
   CNPG re-creates that instance with a fresh PVC on the new StorageClass, re-cloning
   from the primary. Wait until `kubectl -n postgres get cluster postgres` is healthy
   again before touching the next instance.
3. **Move the primary onto the new class** via a switchover, so the old
   (still-on-old-storage) primary becomes a replica:
   ```sh
   kubectl cnpg promote postgres postgres-N     # a replica already migrated in step 2
   ```
4. Repeat step 2 for the now-demoted former primary.

Every instance is rebuilt from a live peer, so nothing is ever dropped. The old PVs
are `Retain`, so they linger `Released` afterward — pv-reaper reclaims each on
`kubectl delete pv <name>` (it covers both `cephfs.csi.ceph.com` and `rbd.csi.ceph.com`).

> This cluster was itself moved cephfs → ceph-rbd. It was **empty**, so it was
> recreated rather than rolled — that shortcut is *only* safe with no data.
