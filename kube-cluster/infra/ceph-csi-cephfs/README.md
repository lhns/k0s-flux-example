# ceph-csi-cephfs

Ceph **CephFS** shared-filesystem storage (`cephfs.csi.ceph.com`), providing the `cephfs`
StorageClass on the `fastappdata` (SSD) filesystem — the default for config, databases-on-files
and anything latency-sensitive. Each PVC becomes a subvolume under `/volumes/kube/` (external
Ceph cluster, monitors `10.20.2.101-103`).

The HDD tier is `cephfs-appdata`, in `../ceph-csi-cephfs-appdata/` under a **driver of its own**
so that a hang on `appdata` cannot take out this one.

The **subvolume group has to exist on each filesystem** — `ceph fs subvolumegroup create <fs> kube`
is a manual step; the CSI driver does not create it.

**clusterID** identifies the Ceph *cluster* (monitor set), not a filesystem — the
same cluster also hosts the `archive` filesystem. The clusterID is `ceph-cluster`.

- `release.yaml` — HelmRepository `ceph-csi` + HelmRelease `ceph-csi-cephfs`
  (clusterID `ceph-cluster`, subvolumeGroup `kube`; nodeplugin metrics on `:8090`).
- `secret.yaml` — SOPS-encrypted `csi-cephfs-secret` (the `client.kube` cephx
  creds). **Also read cross-namespace by `ceph-csi-rbd`.**
- `storageclass.yaml` — `cephfs` (Retain, expandable).

nodeplugins are hostNetwork (kube-vnet skips them); the provisioner only egresses
to the Ceph monitors — no kube-vnet rule.

## Image: x86-64-v2 build

All three ceph-csi drivers run `ghcr.io/lhns/ceph-csi-x86-64-v2`, not `quay.io/cephcsi/cephcsi`. From 3.18
upstream's image is EL10, which needs x86-64-v3 (AVX2/FMA/BMI); kube-worker-04 (Ivy Bridge) has none of these,
and the 3.18.0 image dies there with `CPU does not support x86-64-v3`. The build is upstream's, on an EL9 base:
<https://github.com/lhns/ceph-csi-x86-64-v2>. Renovate reads the chart version and the image from that image's
tags, so a chart bump waits until our build of that release is published.

## Kernel caveat: unmounting CephFS can panic the node

`kernel BUG at fs/super.c:650` ("Busy inodes after unmount of ceph"), always preceded by
`ceph: [...]: umount timed out, 0`. The machine dies instantly; `last -x` shows `crash`.

`ceph_kill_sb()` waits at most `mount_timeout` (default 60s) for in-flight OSD requests, then
tears the superblock down with inodes still referenced. Mainline still has that bounded wait, so
**no kernel fixes it**: Ceph trackers [#79149](https://tracker.ceph.com/issues/79149) and
[#74156](https://tracker.ceph.com/issues/74156) are open. Do not move to trixie-backports for it:
that kernel keeps this bug and adds CVE-2026-89646.

- **It is not only draining.** Deleting or restarting *any* pod with a CephFS volume can panic
  its node, and `umount timed out` has been fatal every time here. Let a stuck pod stay stuck
  until Ceph is healthy rather than restarting it, and prefer an in-place container restart
  (`kubectl exec … -- kill 1`), which never calls NodeUnstageVolume.
- `Aborted: operation with the given Volume ID already exists` afterwards is the volume lock
  held by the original hang: a symptom, not a second fault.
- **Not every node crash is this bug.** Check `journalctl -b -1 -k` on the node (needs sudo) for
  `umount timed out`. A cutoff with no kernel output at all (no BUG, Oops, hung task, OOM or MCE)
  is the hypervisor resetting the guest, not CephFS.
