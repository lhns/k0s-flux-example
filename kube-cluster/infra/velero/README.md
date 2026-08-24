# velero

[Velero](https://velero.io) — backup/restore + DR of Kubernetes resources and (CSI)
volumes, to Ceph RGW (`s3.example.com`), the `kube-barman` bucket under a `velero/` prefix
(a dedicated bucket wasn't created; switch by editing `release.yaml`).

- `release.yaml` — HelmRelease `velero` `12.1.0` with the AWS object-store plugin
  (`velero-plugin-for-aws`) and `EnableCSI`. A daily `Schedule` backs up all namespaces
  (30d TTL) and **CSI-snapshots** their volumes via the Ceph `VolumeSnapshotClass`es
  (`infra/snapshot-controller` + `infra/ceph-csi-*`).
- `s3-secret.yaml` — SOPS-encrypted RGW credentials (AWS `cloud` file).

**Scope:** resources everywhere; volume snapshots are cheap Ceph CoW (no data-movement to
S3), so they're an in-cluster safety net, not full-Ceph-loss DR. **`postgres` is excluded**
— CNPG owns its backups (snapshots + WAL). Egress-only → no kube-vnet rule.
