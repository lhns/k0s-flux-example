# pv-reaper

Deploys [kube-pv-reaper](https://github.com/lhns/kube-pv-reaper) from its
published OCI Helm chart (`oci://ghcr.io/lhns/charts/kube-pv-reaper`). Full
design, mechanism, and caveats live in that repo's README.

It gives CephFS PVs a **split reclaim**: deleting a PVC keeps the volume,
deleting the PV reclaims the subvolume (via a disposable Delete-policy clone).

- `release.yaml` — the `OCIRepository` (pinned to chart `0.1.0`) plus the
  `HelmRelease` (`driver: cephfs.csi.ceph.com` scopes it to CephFS).

Roll a new version by cutting an upstream `vX.Y.Z` release, then bumping
`ref.tag` in `release.yaml`.

**Required:** `../ceph-csi/storageclass.yaml` must stay `reclaimPolicy: Retain`
for the split behavior to work — see the upstream README.
