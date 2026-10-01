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
