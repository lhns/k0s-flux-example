# storage-usage

Who uses the Ceph space: an exporter that splits every pool's stored bytes over CephFS directories,
RBD images and S3 buckets, labelled with their owner, and the dashboard *Ceph storage*
(`/d/ceph-storage-owners`, folder *Ceph*). Its top rows (device classes, pools, filesystems, MDS
strays) use the mgr metrics only and work without the exporter; the rows below use the exporter's.
Why it runs here, as a script on the Ceph image:
[ADR 0063](../../../docs/adr/0063-ceph-usage-measured-in-the-cluster.md).

- `exporter/storage_usage.py`: the exporter (Python 3.9, standard library and the Ceph bindings of
  the image); `exporter/config.json`: what it measures and when. Both reach the pod as one
  hashed ConfigMap, so an edit rolls it.
- `resources.yaml`: Deployment, ServiceAccount and a ClusterRole to list PersistentVolumes.
- `secret.yaml`: the Ceph and RGW credentials (below).
- `grafana.yaml`, `dashboards/`: the dashboard and the stale-exporter alert.
- `tests/`: the walk, attribution and rendering against fakes, and that the dashboard only queries
  metrics the exporter emits (`tests/run.sh`, in `validate.sh`).

## What it measures

Two threads: one for the daily `rbd_k8s`, one for all other tasks, which run one at a time in the
order of the table. `/metrics` on port 9922 serves what the last task rendered and computes
nothing; `/healthz` fails if either thread died. Times are UTC and avoid Velero's runs at :00 and :10. The cost
column is the measured duration of one run.

| task | schedule | reads | cost |
| --- | --- | --- | --- |
| `pools` | every 10 min | `ceph df`, CRUSH rules, `fs ls` (mon) | 0.1 s |
| `cephfs` | :20 and :50 | all four filesystems through libcephfs, no mount: `ceph.dir.rbytes` of every walked directory, `.snap` listings, the oldest snapshot's `rbytes` for the lower bounds; the PersistentVolume list, also for `ceph_usage_pv_info` | 20-31 s, about 300 leaves and 50 snapshot directories |
| `rbd_proxmox` | :25 and :55 | `rbd.vms/vms`, `rbd.vms-nvme/vms-nvme`: head and snapshot-only bytes from the object map (fast-diff), like `rbd du` | 3 s, 32 images |
| `rgw` | hourly at :35 | the admin API, `GET /admin/bucket?stats`: size and object count per bucket | 0.5 s |
| `rbd_k8s` | daily at 04:30 | `rbd.kube/kube`: the head of every `csi-vol-*`, a scan of its objects (no object map) | 7.2 min, 59 images, one at a time |

Each task opens its own Ceph session and closes it; the CephFS session is unmounted after each
walk, so it holds no caps between runs. A task that fails keeps its last result on `/metrics`,
and its item errors.

A run can be **partial**: `ceph_usage_task_partial{task}` is 1, `last_run_ok` 0, `last_success` is
not renewed, and the data it did measure is kept.

- `rbd_k8s` over `max_seconds`: the images measured are emitted, the rest as 0 (the dashboard reads
  this task over 26 h, so yesterday's value would be counted twice next to the residual), and the
  pool's residual is `(unmeasured + snapshots)` for that run, so the leaves still sum to the pool.
- `cephfs`, one filesystem failing: it keeps its previous samples (stale), or one `unmeasured` leaf
  per data pool if it had none. Its `ceph_usage_task_item_errors{task="cephfs", fs}` is 1. If every
  filesystem fails the run fails and nothing is replaced.

### The partition

`ceph_usage_bytes` splits each pool's `stored` bytes into leaves, so a pool's leaves add up to it
and the dashboard can show raw bytes as leaves times `ceph_usage_pool_raw_ratio`.

- **CephFS**: directories to the configured depth (`depth`, `depths` per subtree), deeper where
  a static PV's `rootPath` lies below, and every subvolume `/volumes/<group>/<name>`. Each expanded
  directory adds a `(files)` leaf for the files directly in it. A leaf's pool is its directory's
  layout (`ceph.dir.layout.pool`, inherited); files below it with another layout land in the other
  pool's residual.
- **RBD**: per image, `live` (head) and, for Proxmox, `snapshot` (snapshot-only). `csi-snap-*`
  clones write nothing and are skipped.
- **RGW**: `size_actual` per bucket, and `ceph_usage_rgw_bucket_objects`. The mgr's `ceph_rgw_*` metrics are
  request counters, so bucket sizes exist only here.
- **Residual**, per measured pool: stored minus its leaves, **signed**. CephFS: snapshots beyond
  the lower bound, deleted files not yet purged (MDS strays) and sparse files (negative on
  `archive` and `userdata`, whose sparse images make `rbytes` exceed what is stored). k8s RBD:
  Velero's snapshots, which `rbd du` cannot see because ceph-csi keeps them in the trash namespace.
  Proxmox and RGW: rounding to whole objects and garbage not yet collected.
- **Other pools** (metadata, `.mgr`, RGW index and log) are one leaf each, consumer `ceph`.

### Owners

| storage | consumer | app | volume |
| --- | --- | --- | --- |
| subvolume with a PV (`subvolumePath`), RBD image with a PV (`imageName`) | `k8s` | claim namespace | PVC; ` (released)` when the PV is |
| directory at or below a static PV's `rootPath` (longest match; `/` mounts are readers and own nothing) | `k8s-static` | claim namespace; the directory's name if several namespaces mount it | PVC(s) |
| `/docker/<stack>`, `/eph-docker/<stack>`, `/eph/docker/<stack>` | `swarm` | stack | path |
| Proxmox image `vm-<id>-*` | `proxmox` | vmid (the dashboard joins the name from `proxmox_vm_uptime`) | image |
| bucket | `s3` | bucket owner | bucket |
| anything else on a filesystem | the filesystem's name (`archive`, `userdata`, ...) | top directory | path |

A path several namespaces mount (`/media`) is named after its directory, and the label `used_by`
lists the namespaces (comma-separated); one namespace with several PVCs on a path stays its app.
A filesystem named like another consumer (`k8s`, `swarm`, ...) is prefixed `fs:`. A subvolume or
image without a PV is `k8s`, app `(no PV)`.

`ceph_usage_pv_info{app, volume, static, driver, storage_class}` is 1 per claimed PV (`static` is
true without the CSI provisioner's `provisioned-by` annotation). The dashboard joins kubelet's
`k8s.volume.*` to it for the fill level of dynamic PVCs, so it keeps no PVC name list.

### Snapshots

- **CephFS has no exclusive snapshot size.** Per directory with snapshots: count, oldest and
  newest creation time, and a lower bound of what the oldest snapshot holds that is no longer live:
  per walked leaf below it, `max(0, rbytes then - rbytes now)`. The leaf's `kind="snapshot"` is the
  largest such bound over the snapshots above it. Snapshot directories below the leaves are found
  where a leaf's `ceph.dir.rsnaps` exceeds its own count.
- **Proxmox**: exact snapshot-only bytes per image, at object granularity, and count, oldest and
  newest snapshot.
- **k8s RBD**: snapshot count and oldest per volume; the bytes are only in the pool residual.
  Measuring them needs object-level `listsnaps`, which the Python bindings do not expose.

## Credentials

Created on Ceph by hand, never by Flux; the keys are in `secret.yaml` (`CEPH_CLIENT_ID`,
`CEPH_KEY`, `RGW_ACCESS_KEY`, `RGW_SECRET_KEY`). Rotate with `scripts/secret-from-clipboard.py`
(root README, "Handing over a secret").

```
client.storage-usage
  mon 'allow r'  mgr 'allow r'  mds 'allow r'
  osd 'profile rbd-read-only pool=rbd.kube namespace=kube, profile rbd-read-only pool=rbd.vms namespace=vms, profile rbd-read-only pool=rbd.vms-nvme namespace=vms-nvme'
radosgw user storage-usage: caps buckets=read; usage=read; metadata=read; users=read
```

Measuring another RBD namespace needs it added to the `osd` cap (`ceph auth caps`, which keeps
the key) as well as to `config.json`.

## Network and scrape

The pod reaches the monitors and OSDs on the Ceph public network and `s3.example.com` through open
pod egress ([ADR 0031](../../../docs/adr/0031-beyond-ingress-isolation.md)); it has no egress
policy. The cluster collector scrapes it by pod annotation: the `otel-scrape` vnet label here and
`storage-usage` in the collector's `namespaces.names` (`infra/opentelemetry-monitoring`).

## Runbook

| symptom | look at | fix |
| --- | --- | --- |
| `storage-usage-stale` fires | `kubectl -n storage-usage logs deploy/storage-usage`, `ceph_usage_task_last_run_ok` | Ceph unreachable or a cap missing (the log names the command); the numbers on the dashboard are old meanwhile |
| a task loads Ceph too much | its duration on the dashboard | `"enabled": false` for it in `config.json` (its pools then show as `unmeasured`), or a later `offset_minutes` |
| `ceph_usage_task_item_errors` > 0 | the log | an image without a valid object map is skipped, not scanned; `rbd object-map check`. `fs` names a CephFS filesystem that failed (partial run) |
| `ceph_usage_task_partial` = 1 | the log, the dashboard's *Exporter tasks* | `rbd_k8s`: the scan outgrew `max_seconds` in `config.json` (raise it, or the pool's residual stays `unmeasured`); `cephfs`: the named filesystem's mount or walk failed |
| an app is attributed wrongly | its PV's `volumeAttributes` | the owner rules above |

## Decisions

- **Labels avoid the collector's**: `namespace` and `pod` are the scrape target's, so the claim's
  namespace is `app` and the claim `volume`, also on `ceph_usage_pv_info`. Kubelet's volume stats
  arrive as `k8s.volume.capacity` and `k8s.volume.available` with `k8s.namespace.name` and
  `k8s.persistentvolumeclaim.name`; the dashboard `label_move`s those to `app` and `volume` before
  the join. A series here carries about 40 labels from the collector, near VictoriaMetrics' default
  `-maxLabelsPerTimeseries=40`; the cluster raises it to 50, so a new label needs a count first. Pod restarts make new series: dashboard queries use
  `max by (...)` over `last_over_time`, 26 h for `rbd-k8s` so a restart does not empty it until
  the next daily run.
- **Two threads, not one per task**: the daily object scan takes minutes and would hold up the
  10-minute `pools` read and the half-hourly tasks behind it. The other tasks stay on one thread
  in their order because the load on Ceph is part of the design (see the cost column); a thread per
  task would run the CephFS walk, the Proxmox du and the scan at once. Each task writes its own
  result slot and `/metrics` is assembled under a lock; the pool read of the slow task, minutes
  old when it ends, never replaces a newer one.
- **libcephfs, no mount**: no privileges, no CSI node plugin involved. `client_permissions` is
  off because the pod's uid cannot traverse 0700 directories; access is what the read-only cap
  grants.
- **No `listsnaps` per object** for k8s RBD snapshot bytes: not in the bindings, and one CLI
  call per object (about 277k) is too slow. The pool residual stands in.
- **RGW through the admin API**, signed with SigV4 in the script, rather than
  `radosgw_usage_exporter`: one more deployment for 13 buckets.
- **Shared directories are named after the directory**, not after one of the apps that mount it:
  a path several apps use belongs to none of them. No map: the rule is the number of namespaces.
- **Consumer of unowned directories is the filesystem**, not a catch-all, so the dashboard
  separates `archive` from `userdata` without a drill-down.
- **Not `mgr/prometheus/rbd_stats_pools`**: it exports I/O counters per image, not usage.
- **Proxmox images without a valid fast-diff are skipped**: scanning a multi-TiB image every
  30 minutes is the load this exporter must not cause.
