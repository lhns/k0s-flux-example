# 0007 — Storage is the external Ceph cluster through ceph-csi; the class follows the access pattern

- Status: Accepted; amended by [0035](0035-a-csi-driver-per-failure-domain.md)
- Date: 2026-07-07, amended 2026-07-08, 2026-07-23, 2026-08-06 and 2026-08-28
- Current answer: three dynamic StorageClasses on the existing Ceph cluster, all
  `reclaimPolicy: Retain`. `ceph-rbd` (RWO block, NVMe pool, mounted with `discard`) for
  databases, SQLite and anything fsync-heavy; `cephfs` (RWX, the SSD `fastappdata` filesystem)
  for config and files, the default; `cephfs-appdata` (RWX, the HDD `appdata` filesystem) for
  bulk data. What decides is how the app writes, not whether it is single-instance.

## Context

The Swarm already stored its data on an external Ceph cluster (CephFS filesystems on SSD and
HDD, RBD pools, RGW). The cluster consumes it rather than running its own storage.

## Decision

- **CephFS first** (2026-07-07), dynamic subvolumes under group `kube` rather than hand-named
  directories, through a Ceph client of the cluster's own rather than the Swarm's, so its access
  is scoped and revocable on its own. The group must be created by hand on each filesystem; the
  driver does not create it.
- **RBD** (2026-07-08) for block semantics: CNPG advises against a shared filesystem for PGDATA,
  and fsync-heavy SQLite is too slow on CephFS, where each small create, rename or fsync is a
  metadata-server round trip. VictoriaMetrics' hot volumes are on RBD for the same reason
  (2026-10-04).
- **`cephfs-appdata`** (2026-08-06): dynamic provisioning on the HDD filesystem, so bulk data
  need not land on SSD.
- **Retain on every class**, because [0008](0008-pv-reaper-split-reclaim.md) depends on it.
- **Files-only volumes go on CephFS RWX** (2026-07-23): if the database lives in Postgres or
  nowhere, the volume is RWX and the Deployment can roll start-first. A single-instance app keeps
  `strategy: Recreate`; that, not the access mode, is what prevents two writers, and RWO on RBD only
  adds Multi-Attach stalls on reschedule. RBD is for a live embedded database (SQLite, BoltDB) or
  fsync-heavy work; a small file rewritten by tmp-and-rename is not a database.
- **`discard` on RBD** (2026-08-28): ext4 on RBD never trims by default, so deleted files do not
  return space to the pool. CephFS has no block layer and needs nothing.

## Consequences

- `PVC.spec.storageClassName` and `PV.spec.csi.driver` are immutable: changing class means a
  new PVC and a copy (AGENTS.md).
- Unmounting CephFS can panic a node on the kernels in use; see the
  [ceph-csi-cephfs README](../../kube-cluster/infra/ceph-csi-cephfs/README.md#kernel-caveat-unmounting-cephfs-can-panic-the-node).
- Each CSI driver needs its own VolumeSnapshotClass, or Velero fails on its volumes
  ([0017](0017-backups-stay-in-ceph.md)).
- Whether to copy data at all, or mount it where it lies, is [0020](0020-swarm-data-mounted-in-place.md).

## References

- `68902ae8`, `2a1de0ac`, `1e99b6db`, `a42fb9b0`, `1ee3d2b3`, `d9743f17`, `fbb2679e` (discard),
  PR #619-#623 (VictoriaMetrics hot volumes to RBD).
- Conversation 2026-07-07 (Ceph client), 2026-07-23 and 2026-07-30 (files-only rule),
  2026-08-04 (tmp-and-rename), 2026-10-04 (fsync measurement).
- [docs/migrating-swarm-to-kubernetes.md, 5.1](../migrating-swarm-to-kubernetes.md#51-choose-the-storage-class).
