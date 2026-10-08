# 0008 — pv-reaper: deleting a PVC keeps the volume, deleting the PV destroys it

- Status: Accepted
- Date: 2026-07-07, amended 2026-07-21 and 2026-08-21
- Current answer: dynamic PVs on the drivers pv-reaper watches (`cephfs.csi.ceph.com`,
  `rbd.csi.ceph.com`) get a **split reclaim**: deleting the PVC keeps the PV and its data;
  deleting the PV reclaims the backing subvolume or image. `reclaimPolicy: Retain` does not
  protect a PV from that. Never delete a dynamic PV as part of a move; leave old PVCs and PVs
  bound until the rollback is genuinely unwanted.

## Context

Kubernetes has one `reclaimPolicy` for both transitions. `Delete` destroys the data when the PVC
goes, so a pruned app or a deleted namespace takes its volumes with it. `Retain` keeps the data
on both, so nothing ever reclaims a released volume and storage leaks. The decision is the
middle that Kubernetes lacks: keep on PVC delete, reclaim on PV delete, so a `Released` PV is an
explicit tombstone deleted when the storage is really unwanted (kube-pv-reaper's README). The
component was written for this repo.

## Decision

- A small controller, [kube-pv-reaper](https://github.com/lhns/kube-pv-reaper) (its own repo,
  deployed from its OCI chart, [0009](0009-own-code-in-own-repos.md)), puts the finalizer
  `pv-reaper.lhns.de/reclaim-on-delete` on `Retain` PVs. When a PV is deleted it creates a
  throwaway `Delete`-policy clone sharing the volume handle, and the CSI driver reclaims through
  that. The original PV is never flipped to `Delete`, so there is no finalizer race. It watches
  PVs as events, it does not poll.
- Every reaped driver's StorageClass must stay `Retain`.
- Static PVs are skipped (2026-07-21): the reaper keys on `pv.kubernetes.io/provisioned-by`,
  which only the CSI provisioner sets. A static PV points at storage nobody provisioned for it,
  and `DeleteVolume` on it fails or destroys shared data.

## Alternatives considered (2026-07-07)

- Flip the PV to `Delete` when it is deleted: the KEP-2644 finalizer exists only on PVs born
  `Delete`, so the PV could vanish before `DeleteVolume` ran and leak the subvolume.
- Flip to `Delete` at creation: plain `Delete`, losing keep-on-PVC-delete.
- Flip a `Released` PV on an annotation: correct, but manual.
- Clone on delete was adopted: the clone is born `Delete` and gets the normal, race-free
  reclaim.

## Consequences

- **Deleting a dynamic PV destroys its volume**, whatever `Retain` suggests; moving a volume
  between drivers by deleting its PV loses it. AGENTS.md carries the rule and the move-a-volume
  procedure.
- Released PVs are not a leak to fix by switching classes to `Delete`: `Retain` is what this
  design needs.
- Scaling pv-reaper to 0 does not make PV deletion safe: its finalizer then has nobody to
  remove it and the PV hangs in `Terminating`.
- `cephfs-appdata.csi.ceph.com` is not in the reaped list, so its volumes are plain `Retain`.
  The gap is deliberate (2026-08-17); it is not a licence to delete those PVs either.
- Snapshots are not covered: pv-reaper watches PVs, not VolumeSnapshotContents
  ([0017](0017-backups-stay-in-ceph.md)).

## References

- `7bbd69b6`, `b7414201` (clone-on-delete), `93f0fd24` (published chart), `7a1db072` (rbd too),
  `c3ceff45`/`73c072a6` (the loss and restore), `a4dba12e` (the rule, then in CLAUDE.md),
  `28fbf84b`, PR #396.
- Conversation 2026-07-07 (design), 2026-07-21 (static PVs), 2026-07-31 (not a leak).
- [AGENTS.md](../../AGENTS.md), [infra/pv-reaper](../../kube-cluster/infra/pv-reaper/README.md).
