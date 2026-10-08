# 0020 — Large Swarm data is mounted in place through static PVs (an exception)

- Status: Accepted
- Date: 2026-07-11, amended 2026-07-25, 2026-09-03, 2026-09-20 and 2026-10-04
- Current answer: an app's large existing data directory on CephFS is not copied. A static PV
  (`staticVolume: "true"`, `rootPath` pointing at the Swarm-era directory, `storageClassName: ""`,
  `Retain`) binds it by name. These PVs are the exception to dynamic provisioning: pv-reaper
  ignores them, Velero skips them, and Flux never prunes them. New data gets a dynamic volume.

## Context

Some Swarm data was hundreds of gigabytes (metrics, media, mail, repository trees), and the HDD
tier it would be copied onto was nearly full. Copying would also have made cutover slow.

## Decision

- **Mount in place** for large stores; copy into a fresh dynamic PVC only for small ones. The
  placement rule (2026-07-25): big data is mounted where it lies, small data is copied into a fresh
  dynamic volume, config goes into git as a ConfigMap or Secret, and SQLite goes on RBD
  ([0007](0007-ceph-csi-storage-classes-by-access-pattern.md)). The Swarm service is stopped before
  the cluster mounts the same directory.
- Pod hygiene for such a store: run as the data's owner and set no `fsGroup` (a recursive
  `chown` of the tree), `strategy: Recreate`, a generous readiness threshold and no liveness
  probe, since a large store can take many minutes to open.
- **`prune: disabled` on every static PV** (2026-09-03): renaming one, reverting a commit or
  deleting an app directory would prune it and leave the PVC `Lost`. An availability risk, not a
  data-loss one: pv-reaper keys on `pv.kubernetes.io/provisioned-by`, which a static PV lacks.
- **Velero skips them** by volume policy (2026-09-20): with `storageClassName: ""` there is
  nothing to CSI-snapshot.
- **New stores are dynamic** (2026-10-04), e.g. VictoriaMetrics' long-term store on
  `cephfs-appdata`, not the Swarm-era pattern. The existing static PVs (media libraries,
  whole-filesystem backups) stay static by design.

## Consequences

- These volumes have no Velero snapshot. Their protection is the Ceph-side snapshot schedule of
  the legacy trees ([0017](0017-backups-stay-in-ceph.md)) and, for the paths it covers, Backrest's
  file-level restic plans.
- Moving one to a dynamic volume is a copy with downtime, as for any class change (AGENTS.md).
- Freeing a retired one is a deliberate manual step (`kube-cluster/TODO.md` holds the open one).

## References

- `e91f1203` (first static PV), `9059442f`, `d68a0f81`, PR #406.
- Conversation 2026-07-25 (placement rule), 2026-09-06 (a move done by the procedure),
  2026-10-04 (dynamic for new stores).
- [docs/migrating-swarm-to-kubernetes.md,
  5.2](../migrating-swarm-to-kubernetes.md#52-large-volumes--mount-in-place-never-copy).
