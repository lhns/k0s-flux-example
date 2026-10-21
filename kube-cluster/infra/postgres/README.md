# postgres

A CloudNativePG `Cluster` — a 3-instance HA PostgreSQL (1 primary + 2 hot
standbys) on the `ceph-rbd` (Ceph RBD block) StorageClass. Requires the operator
(`infra/cloudnative-pg`), so `kustomization.yaml` carries
`dependsOn: infra-cloudnative-pg`.

- `cluster.yaml` — the `Cluster` (3 instances, ceph-rbd 20Gi, `initdb` db/owner `app`).
  CNPG generates the `postgres-app` secret (user + password) in-cluster. Also wires
  backups (`spec.backup.volumeSnapshot` + the Barman Cloud Plugin as WAL archiver) and
  tuning (`max_connections: 500`, `shared_buffers: 512MB`, 1Gi/2Gi memory) — see
  **Connections & pooling** below.
- `pooler.yaml` — a CNPG `Pooler` (pgbouncer, transaction mode) — see **Connections & pooling**.
- `objectstore.yaml` / `scheduledbackup.yaml` / `s3-secret.yaml` — see **Backups** below.
- `monitoring-queries.yaml` — custom exporter queries; see **Monitoring** below.
- `vnet.yaml` — three kube-vnet channels, plus the scrape binding (**Monitoring**):
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
Point-in-time recovery, via the Barman Cloud Plugin (`infra/barman-cloud-plugin`). Both halves
go to **Ceph RGW** (`s3://kube-barman`, 30d) through the plugin's `ObjectStore`:
- **Base backups** — daily `ScheduledBackup`, `method: plugin`, gzip. Not `volumeSnapshot`;
  `scheduledbackup.yaml` records why.
- **WAL** — continuously archived by the same plugin. That is what makes PITR possible.
- Restore: a new `Cluster` with `spec.bootstrap.recovery` (base + WAL replay).

### serverName

WAL and base backups land under `s3://kube-barman/<serverName>/`, set in the Cluster's
barman-cloud plugin `parameters`. On every new incarnation (re-init, restore into a new cluster,
major upgrade) bump it to `<name>-g<yyyymmdd>`, e.g. `postgres-g20261001`. Reusing a prefix that
holds the old incarnation's WAL makes barman refuse with `Expected empty archive`, archiving
stalls and `pg_wal` fills the disk. The recovery source (`externalClusters[].plugin.parameters`)
must then name the **old** `serverName`.

`spec.backup.volumeSnapshot` in `cluster.yaml` is retained only so an ad-hoc
`method: volumeSnapshot` `Backup` still works; nothing schedules one.

An on-demand backup before something risky is a `Backup` object applied **outside git** — it
is a point-in-time action, not desired state, so Flux would prune it:

```sh
kubectl -n postgres get backup --sort-by=.metadata.creationTimestamp | tail -3
```

Trade-off: base backups and WAL now both live in RGW, so this survives losing the Postgres
volumes and gives PITR — but the bucket is **on the same Ceph**, so it is not full-Ceph-loss
DR. Off-cluster copies of that bucket are the missing piece, not another backup method.

## Monitoring

Each instance runs CNPG's exporter on `:9187` and each pooler pod pgbouncer's on `:9127`. The otel
cluster collector scrapes both like any annotated pod (`infra/opentelemetry-monitoring`): the
`prometheus.io/*` annotations come from `inheritedMetadata` on the `Cluster` and the pod template on
the `Pooler`, `postgres` is in the collector's `namespaces.names`, and `postgres-scrape-ingress` in
`vnet.yaml` admits it. Dashboards: [below](#dashboards).

- `monitoring-queries.yaml` adds per-database sessions and busy time, the role that owns each
  database, lock waits, statement age, `pg_stat_io` and each database's top 5 statements from
  `pg_stat_statements` (primary only), on top of the operator's default queries.
  Edits reload without a restart (`cnpg.io/reload`). Per-table statistics would need
  `target_databases`, one connection per database per scrape; read them in psql instead.
- `track_io_timing` and `track_wal_io_timing` are on (reload only) for the I/O times.
- `pg_stat_statements` is loaded through its `pg_stat_statements.*` parameters (`track: top`, 5000
  entries); CNPG creates the extension in every database. Removing the parameters unloads it, again
  with a restart of every instance.
- The collector blanks the pod labels `role` and `cnpg.io/instanceRole`, which flip on every
  switchover; queries find the primary with `cnpg_pg_replication_in_recovery == 0`.
- No `PodMonitor` (`enablePodMonitor`): nothing in this cluster reads them, and upstream deprecates
  the field.
- `cnpg_collector_last_available_backup_timestamp` and `first_recoverability_point` are not
  maintained for plugin backups; `barman_cloud_cloudnative_pg_io_*` from the plugin sidecar are.

### Dashboards

Two Grafana Operator `GrafanaDashboard`s in folder *Postgres* (`grafana.yaml`, JSON in
`dashboards/`), from the instance (:9187) and pooler (:9127) exporters plus
kubeletstats.

- **Postgres** (`/d/postgres`): the cluster. At a glance on top; rows for connections, performance
  of the primary, I/O (`pg_stat_io` and the nodes' RBD devices), resources, storage, WAL and
  archiving, replication, the pgbouncer pooler and the exporters themselves. Variables: instance,
  pooler pod.
- **Postgres databases** (`/d/postgres-database`): per app. Pick a role and its databases follow
  (`matrix` selects its nine); the table on top has one row per database, sortable, and a click on a
  name narrows everything below to it. *Top statements* lists each database's five heaviest
  statements by `queryid`; an admin reads the text in psql on the primary, in that database:
  `SELECT query FROM pg_stat_statements WHERE queryid = <id>;`. All panels read the primary.

Panels find the primary by metric, never by pod name or role label
(see [Monitoring](#monitoring)).

#### Runbook

| Symptom | Look at | Fix |
| --- | --- | --- |
| Writes hang cluster-wide | *Sync standbys* 0, *Instances up* | A standby is down and `dataDurability: required` pauses commits: bring it back |
| An app is slow | *Postgres databases*: *Busy*, *Read wait*, *Cache hit*, *Lock waits*, *Idle in tx*, *Top statements* | Lock waits or long statements: the app's queries; read wait with low cache hit: memory; one statement dominating: its index or plan |
| Cache hit ratio falling, major faults rising | *Cache hit ratio*, *Cache pressure*, *Memory* | Raise the memory limit in `cluster.yaml` (restarts the instances; batch it) |
| *Connections used* high | *Backends by database*, *New sessions by database* | Shrink that app's pool, or move it to `postgres-pooler` |
| Idle in transaction | *Transaction age by database* | The app holds transactions open; it also blocks vacuum (*Transaction ID age*) |
| Requested checkpoints above timed | *Checkpoints*, *WAL generated* | Raise `max_wal_size` (reload only) |
| Clients waiting at the pooler | *Pooler clients*, *Pooler server connections* | Pool pinned at `default_pool_size`: raise it; else the database is the bottleneck |
| WAL waiting to archive rises | *WAL archive backlog*, *pg_wal* | Archiving to RGW fails: barman-cloud sidecar log, RGW, `serverName` ([above](#servername)) |
| Backup failed | *Last failed backup* | `kubectl -n postgres get backup`, describe the failed one |
| Standby behind or not streaming | *Standby lag*, *Standby health* | The standby's log; a slow disk or a network problem |
| Primary moved | *Instance restarts and role changes* | Check Synapse (sequence guard, `apps/matrix/README.md`) |
| pgdata filling | *pgdata used*, *Database size*, *Database growth per day* | Grow `storage.size`; never delete a PV ([AGENTS.md](../../../AGENTS.md)) |

#### Limits of the data

- No per-pod block I/O on k8s nodes: the *Node disk* panels show every RBD volume on the node.
  `pg_stat_io` and the block read times are the Postgres-side view.
- The cache hit ratio counts shared_buffers only; a miss may still come from the OS page cache.
- `cnpg_collector_last_available_backup_timestamp` is not maintained for plugin backups; the
  dashboards use the barman-cloud plugin's `barman_cloud_cloudnative_pg_io_*` instead.
- No app connects through `postgres-pooler` yet, so the pooler panels show nothing until one does.
- Statement statistics exist on the primary only and start over after a switchover; the top 5 of a
  database is ranked by cumulative time, so a new hot statement shows in *other* until it overtakes.

### Decisions

- **Query statistics carry `queryid`, never query text.** Every Authelia user is at least a Grafana
  Viewer, and the text is every app's SQL. Resolve an id in psql:
  `SELECT query FROM pg_stat_statements WHERE queryid = ...`.

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
