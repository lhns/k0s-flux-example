# ceph-csi-cephfs-appdata

A second CephFS CSI driver (`cephfs-appdata.csi.ceph.com`), serving the
`appdata` filesystem only. `../ceph-csi-cephfs` keeps `fastappdata`.

## Why

The node plugin is **one process per node per driver name**, and it dies as a
unit. ceph-csi 3.17.0 calls `os.Exit(1)` when any gRPC handler is stuck for 10
minutes (upstream #6286), and a mount whose PGs are inactive blocks in the
kernel forever. On 2026-08-17 a single hung `appdata` volume (`forgejo-git`)
restarted the shared cephfs plugin ~40 times and unmounted every *other* cephfs
volume on those nodes — NVMe-backed `fastappdata` volumes included — because
they shared only the process, not the pool. Nine unrelated apps went `Unknown`.

Splitting by driver name is the only way to split the process: kubelet resolves
`PV.spec.csi.driver` to exactly one socket per node
(`<kubeletDir>/plugins/<driverName>/csi.sock`). Pools and filesystems are just
StorageClass parameters handed to the same binary, so they cannot isolate
anything on their own.

## Layout

| | |
| --- | --- |
| driver | `cephfs-appdata.csi.ceph.com` |
| filesystem | `appdata` (HDD tier, default pool `cephfs.appdata.data`) |
| StorageClass | `cephfs-appdata` (moved here from `../ceph-csi-cephfs`) |
| metrics port | 8092 (cephfs 8090, rbd 8091; hostNetwork, must not collide) |
| secret | **shared** — `csi-cephfs-secret` in the `ceph-csi-cephfs` namespace |

`SlowGRPCRestart=false` is set here too, via `postRenderers` — most important on
this driver, since it is the one deliberately aimed at the filesystem that
hangs.

## Moving an existing volume onto this driver

`PersistentVolume.spec.csi.driver` is **immutable**, so changing the
StorageClass only affects newly provisioned volumes.

**`reclaimPolicy: Retain` does not make deleting the PV safe.** pv-reaper
reclaims any *dynamic* `cephfs.csi.ceph.com` PV that is deleted, Retain or not
(see the top of the repo's `AGENTS.md`). Recreating the PV in place is only safe
for a **static** PV (no `pv.kubernetes.io/provisioned-by` annotation), which
pv-reaper skips. For a dynamic one, copy into a new PVC as `AGENTS.md` describes.

For a static PV, the subvolume is reused and no data is touched:

1. Scale the workload to 0 (pin it with
   `kustomize.toolkit.fluxcd.io/reconcile: disabled` so Flux does not restart it).
2. Save the PV and PVC YAML.
3. Confirm the PV has **no** `pv.kubernetes.io/provisioned-by` annotation.
4. Delete the PVC, then the PV.
5. Recreate the PV with `spec.csi.driver: cephfs-appdata.csi.ceph.com`, the same
   `volumeHandle` and `volumeAttributes`, and a `claimRef` to the PVC.
6. Recreate the PVC with `storageClassName: cephfs-appdata`.
7. Scale the workload back up and remove the annotation.

Volumes done so far: `forgejo/forgejo-git`.
