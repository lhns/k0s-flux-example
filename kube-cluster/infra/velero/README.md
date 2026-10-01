# velero

[Velero](https://velero.io) — backup/restore + DR of Kubernetes resources and (CSI)
volumes, to Ceph RGW (`s3.example.com`), the `kube-barman` bucket under a `velero/` prefix
(a dedicated bucket wasn't created; switch by editing `release.yaml`).

- `release.yaml` — HelmRelease `velero` with the AWS object-store plugin
  (`velero-plugin-for-aws`) and `EnableCSI`. Three `Schedule`s — `hourly` (72h TTL),
  `daily` (720h), `monthly` (4320h) — back up every namespace but `postgres`,
  `opentelemetry-gateway` and `nats`, and **CSI-snapshot** their volumes via the Ceph
  `VolumeSnapshotClass`es (`infra/snapshot-controller` + `infra/ceph-csi-*`).
- `s3-secret.yaml` — SOPS-encrypted RGW credentials (AWS `cloud` file).
- `volume-policy.yaml` — the `skip` volume policy every schedule references (below).

**Scope:** resources everywhere; volume snapshots are cheap Ceph CoW (no data-movement to
S3), so they're an in-cluster safety net, not full-Ceph-loss DR. **`postgres` is excluded**
because CNPG owns its backups (snapshots + WAL), `opentelemetry-gateway` because its
Kafka volumes are a buffer, not a record, and `nats` because a snapshot of a JetStream raft
member is not a consistent restore point. Egress-only → no kube-vnet rule.

## What Velero does NOT cover

A CSI snapshot resolves its `VolumeSnapshotClass` from the PVC's StorageClass. The static
`rootPath` PVs in `apps/*` set `storageClassName: ""` — a subdirectory view into
an existing CephFS tree, not a provisioned volume. There is nothing to snapshot. With
`deployNodeAgent: false` there is no file-level fallback either.

That is correct, not a gap. Do not "fix" it by enabling the node agent.

Velero does not skip them on its own: its CSI plugin only guards a *nil* storage class, so an
empty one sends it looking up a StorageClass named `""` and each such PVC adds an `error
getting storage class: resource name may not be empty` to the backup. That made every run
`PartiallyFailed`, hiding real snapshot failures. `volume-policy.yaml` skips them by
`csi.volumeAttributes.staticVolume`, which only static ceph-csi PVs carry, so dynamic PVCs are
untouched. Expect zero errors per run: a `PartiallyFailed` is now worth reading.

| Data | Covered by |
|---|---|
| Dynamically provisioned PVCs | Velero CSI snapshots (in-cluster CoW) |
| Static `rootPath` PVs (`arr-*`, `gitea-git`, `immich-*`, `jellyfin-*`, `matrix-synapse-data`, `backrest-*`, …) | **backrest** (restic → remote repo) |
| `postgres` | CNPG — base backups + WAL to S3, both via the Barman Cloud Plugin |
| `nats` (JetStream) | **nothing yet**: `nats backup` per account is planned, `infra/nats/README.md` |

`snapshotVolumes: true` in the schedules therefore means "every volume Velero *can* snapshot",
not "every volume".

backrest mounts the `appdata`, `fastappdata` and `userdata` roots, so the statics on the
`archive` filesystem (`arr-media`, `jellyfin-media`, `sftpgo-archive`, `restic-*`) are outside
what it can reach: bulk media and other hosts' restic repos, covered by neither tool.
