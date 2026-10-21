# 0035 — A CSI driver per failure domain

- Status: Accepted
- Date: 2026-08-17, amended 2026-09-26
- Current answer: the HDD `appdata` CephFS filesystem is served by its own CSI driver,
  `cephfs-appdata.csi.ceph.com`, a separate ceph-csi release, so a hang on it cannot take down
  volumes of the SSD `fastdata` filesystem. Isolation between storage is by driver name; pools
  and filesystems alone isolate nothing. ceph-csi's slow-gRPC self-kill is disabled on the node
  plugins and the RBD provisioner.

## Context

The node plugin is one process per node per driver name, and it dies as a unit. ceph-csi 3.17
exits when a gRPC call is stuck for ten minutes, and a mount whose PGs are inactive blocks in the
kernel forever. One hung volume therefore restarts the shared plugin in a loop and unmounts every
other volume of that driver on the node.

## Decision

- kubelet resolves `PV.spec.csi.driver` to exactly one socket per node, so the only way to split
  the process is a second driver name. The `cephfs-appdata` StorageClass kept its name and moved
  to the new driver; the cephx secret and cluster config are shared.
- `SlowGRPCRestart=false` on the node plugins (2026-08-17) and, after slow RBD snapshots
  collided with the same watchdog, on the RBD provisioner (2026-09-26): a wedged volume then
  stays wedged until a manual restart, which is preferred to losing every volume on the node.
  The fix for the hang that triggers it existed only upstream, unreleased.

## Alternatives considered

- A controller that restarts pods stuck after such a hang: it would paper over the restart loop
  the feature gate switches off.

## Consequences

- Existing PVs stay on the old driver: `PV.spec.csi.driver` is immutable, and moving a volume
  means a new PVC, never deleting its PV ([0008](0008-pv-reaper-split-reclaim.md)).
- The new driver is not in pv-reaper's list, and needs its own VolumeSnapshotClass before Velero
  can back up its volumes ([0017](0017-backups-stay-in-ceph.md)).
- The planned move to the Ceph-CSI operator must keep the driver names, and this one fails the
  operator's name check (`kube-cluster/TODO.md`).

## References

- `e9fbad18`, `01f772f4`, `c3ceff45`, `73c072a6`, PR #396, #431, #439.
- Conversation 2026-08-17 (the split, no auto-heal controller).
- [infra/ceph-csi-cephfs-appdata](../../kube-cluster/infra/ceph-csi-cephfs-appdata/README.md).
