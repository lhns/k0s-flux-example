# snapshot-controller

The cluster-wide [external-snapshotter](https://github.com/kubernetes-csi/external-snapshotter)
— the `snapshot.storage.k8s.io` CRDs (`VolumeSnapshot`/`VolumeSnapshotContent`/
`VolumeSnapshotClass`) + the snapshot-controller. k0s ships neither, and everything that
takes CSI volume snapshots needs them: ceph-csi's `VolumeSnapshotClass`es, CNPG's
snapshot-based Postgres backups, and Velero's CSI snapshots.

- `release.yaml` — piraeus's `snapshot-controller` chart (a packaging of the upstream
  controller + CRDs). The v1beta1→v1 conversion webhook is disabled (fresh v1-only install).

Egress-only controller → no kube-vnet rule. The per-driver `csi-snapshotter` sidecars
already ship inside ceph-csi; this adds the missing cluster-wide half.
