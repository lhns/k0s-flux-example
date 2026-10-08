# 0017 — Backups: Velero CSI snapshots and barman for Postgres, both inside Ceph

- Status: Accepted
- Date: 2026-07-09, amended 2026-08-27, 2026-09-20, 2026-09-26 and 2026-09-27
- Current answer: Velero backs up every namespace's resources to Ceph RGW and CSI-snapshots
  their volumes in Ceph: hourly (72h), daily (30d), monthly (6 months), one backup per hour slot.
  Postgres is excluded and backs up through CNPG's barman plugin (base backups and WAL to RGW,
  30 days, point-in-time recovery). Snapshot classes delete their Ceph snapshot with the
  `VolumeSnapshot`. All of it lives on the same Ceph cluster: it protects against mistakes, not
  against losing Ceph. Off-site backup of dynamic volumes is an open item.

## Context

Ceph snapshots are copy-on-write and cheap; moving data to another target is neither. The only
off-site target available is a restic REST server.

## Decision

- **CSI snapshots, no data movement** (2026-07-09): snapshot-controller, a VolumeSnapshotClass
  per CSI driver labelled for Velero, `EnableCSI`, no node agent.
- **Excluded:** `postgres` (CNPG owns it), the Kafka buffer (a buffer, not a record; excluded
  outright rather than given a shorter TTL), NATS (a snapshot of a JetStream raft member is not a
  consistent restore point).
- **Snapshot classes `Delete`** (2026-08-27): under `Retain`, deleting a `VolumeSnapshot` left
  its Ceph snapshot with nothing to reclaim it; nothing reaps VolumeSnapshotContents
  ([0008](0008-pv-reaper-split-reclaim.md)). Tiered schedules came only after this.
- **Three tiers from cron and TTL** (2026-08-27): Velero retains by TTL only, so each tier skips
  the slot the next tier owns, which gives grandfather-father-son retention with one backup per
  slot.
- **Postgres base backups via barman, not volume snapshots** (2026-08-27): CNPG applies no
  retention to snapshot backups, and barman trims WAL only back to its oldest barman backup, so
  the bucket grew without bound.
- **Static PVs are skipped** by a volume policy (2026-09-20,
  [0020](0020-swarm-data-mounted-in-place.md)); their errors would mark every backup
  `PartiallyFailed`, hiding real failures.
- **Keep the retention, fix the mechanism** (2026-09-20 and 2026-09-26): the Ceph-side per-directory
  snapshot limit is raised to fit the tiers, snapshot timeouts are raised and the RBD provisioner's
  self-kill disabled ([0035](0035-a-csi-driver-per-failure-domain.md)). Velero is not restarted
  mid-backup, since that fails the backup and leaks its snapshots.
- **Coverage is checked** (2026-09-27): every CSI StorageClass has a Velero snapshot class, and
  every CNPG cluster archives WAL and has a scheduled backup.

## Alternatives considered

- A separate snapshot operator: Velero already does it, and none can adopt snapshots it did not
  create.
- Fewer snapshots instead of fixing the Ceph-side limits.

## Consequences

- No off-site copy of dynamically provisioned volumes yet; the options are researched in
  `kube-cluster/TODO.md`.
- Velero deletes its VolumeSnapshots but keeps their Ceph snapshots, so a script that treats
  unreferenced snapshots as orphans would destroy live backups.
- The CephFS root snapshot schedule is the only snapshot protection of static PVs, so it is
  narrowed by moving it to specific directories, never by deleting it.

## References

- `4ce7a4ea`, `3eb8f228`, `f952f4d7`, `90432b8d`, `eec87e8b`, `dce4201b`, `7f8154f4`,
  `cef8b8bc`, `d68a0f81`, `2dfc2bdd`, PR #362, #431, #439, #449.
- Conversation 2026-08-27 (tiers, barman), 2026-09-20 (snapshot limit), 2026-09-26 (keep the
  retention).
- [infra/velero](../../kube-cluster/infra/velero/README.md),
  [infra/postgres](../../kube-cluster/infra/postgres/README.md#backups).
