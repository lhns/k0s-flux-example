# 0049 — VictoriaMetrics: one store split over two volumes, and dedup as downsampling (an exception)

- Status: Accepted
- Date: 2026-10-04, amended 2026-10-05
- Current answer: each `vmstorage` keeps its bulk (`/storage`) on HDD CephFS and its hot
  `data/small` on an NVMe `ceph-rbd` volume. The two volumes are one store and must only be
  snapshotted, moved or restored together. A second store, `vmstorage-longterm`, keeps 5 years at
  1-minute resolution, fed independently from Kafka and downsampled by
  `-dedup.minScrapeInterval=1m`. vmselect does not query it yet.

## Why this is an exception

Elsewhere a volume is a self-contained unit on the class its access pattern needs
([0007](0007-ceph-csi-storage-classes-by-access-pattern.md)), and Velero snapshots each PVC on its
own ([0017](0017-backups-stay-in-ceph.md)). Here one data directory spans two classes, and a
restore of one volume without the other loses data.

## Context

- `data/small` is constant small-file creates, renames and fsyncs: each an MDS round trip on CephFS,
  local on RBD's ext4. The full-resolution history was already hundreds of gigabytes on HDD.
- Open-source VictoriaMetrics has no downsampling; dedup is documented as its equivalent, and
  older partitions are thinned by a one-off final dedup.

## Decision

- Hot on RBD with `subPath: small` (ext4's `lost+found` would read as a partition); cold on
  `cephfs-appdata`, or the existing static PV for the full-resolution store. RBD is much faster
  on fsync; cold stays on CephFS, where rare large writes gain nothing from RBD.
- Long-term tier: same writes, its own consumer group, seeded once from snapshots. The interval is
  only ever raised; lowering it cannot restore dropped samples. All metrics, no filter, so any
  dashboard works on either tier.
- **vmselect does not query the long-term store yet** (PR #641): while it replays Kafka with a
  cold HDD index, queries time out as a whole rather than returning partial data. Deferred until
  the replay is done.

## Alternatives considered

- Stream aggregation in vmagent or an OTel interval processor: they aggregate by ingestion time,
  which breaks when Kafka replays a backlog.
- vmctl exports: no aggregation.
- Enterprise downsampling or per-tenant retention: not in the open-source build.

## Consequences

- `data/small/<month>/parts.json` lists the big parts too; starting vmstorage against a stale or
  foreign `small/` deletes every big part it does not list.
- vmstorage reads `df` as free space: on CephFS that is the quota, so volumes are expanded well
  before they fill.
- Restarting vmstorage unmounts CephFS, which can panic its node; only while Ceph is healthy.

## References

- PR #605, #612-#625, #631, #633-#635, #641, #642.
- Conversation 2026-10-04 (alternatives, RBD measurement).
- [apps/victoriametrics-cluster](../../kube-cluster/apps/victoriametrics-cluster/README.md#hotcold-storage).
