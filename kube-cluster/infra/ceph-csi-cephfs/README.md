# ceph-csi-cephfs

Ceph **CephFS** shared-filesystem storage (`cephfs.csi.ceph.com`), in two StorageClasses that
differ only in which filesystem they provision on. Each PVC becomes a subvolume under
`/volumes/kube/` (external Ceph cluster, monitors `10.20.2.101-103`).

| class | filesystem | default data pool | for |
| --- | --- | --- | --- |
| `cephfs` | `fastappdata` | `cephfs.fastappdata.data` (SSD) | the default — config, databases-on-files, anything latency-sensitive |
| `cephfs-appdata` | `appdata` | `cephfs.appdata.data` (HDD) | bulk data — media, repository trees, archives |

`cephfs-appdata` was added when Forgejo needed an HDD volume; before it, the only way onto
`appdata` was a hand-written static PV with `staticVolume: "true"` and a `rootPath` pointing at a
pre-created directory (see `apps/gitea`). Note that the **subvolume group has to exist on each
filesystem** — `ceph fs subvolumegroup create appdata kube` was a manual step; the CSI driver does
not create it. cephx and `csiConfig` needed no change, because `client.kube`'s MDS cap is not
scoped to one filesystem and `subvolumeGroup` is configured per clusterID.

**clusterID** identifies the Ceph *cluster* (monitor set), not a filesystem — the
same cluster also hosts the `archive` filesystem. The clusterID is `ceph-cluster`.
(It was originally named `fastappdata` after the main fs; all volumes were migrated
onto `ceph-cluster` and the transient alias removed.)

- `release.yaml` — HelmRepository `ceph-csi` + HelmRelease `ceph-csi-cephfs`
  (clusterID `ceph-cluster`, subvolumeGroup `kube`; nodeplugin metrics on `:8090`).
- `secret.yaml` — SOPS-encrypted `csi-cephfs-secret` (the `client.kube` cephx
  creds). **Also read cross-namespace by `ceph-csi-rbd`.**
- `storageclass.yaml` — `cephfs` and `cephfs-appdata` (both Retain, expandable).

nodeplugins are hostNetwork (kube-vnet skips them); the provisioner only egresses
to the Ceph monitors — no kube-vnet rule.
