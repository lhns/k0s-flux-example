# pv-reaper

Deploys [kube-pv-reaper](https://github.com/lhns/kube-pv-reaper) from its
published OCI Helm chart (`oci://ghcr.io/lhns/charts/kube-pv-reaper`). Full
design, mechanism, and caveats live in that repo's README.

It gives dynamically provisioned PVs a **split reclaim**: deleting a PVC keeps
the volume, deleting the PV reclaims it. **Read `AGENTS.md` before deleting any
PV** — it has the trap, the scope, and the move-a-volume procedure.

- `release.yaml` — the `OCIRepository` plus the `HelmRelease`. `values.drivers`
  scopes it; it currently lists `cephfs.csi.ceph.com` and `rbd.csi.ceph.com`,
  so `cephfs-appdata.csi.ceph.com` is **not** reaped.

Roll a new version by cutting an upstream `vX.Y.Z` release, then bumping
`ref.tag` in `release.yaml`.

**Required:** the `storageclass.yaml` of every driver above must stay
`reclaimPolicy: Retain` for the split behavior to work.
