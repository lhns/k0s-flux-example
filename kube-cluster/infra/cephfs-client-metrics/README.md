# cephfs-client-metrics

What each CephFS mount on a kube worker sees: request counts and latency of the kernel client,
per mount and labelled with its PersistentVolume. Ceph's own metrics stop at the OSD and MDS; this
is the client side of the same reads, including the network and queueing in between. Why it is a
script on the Python image: [ADR 0071](../../../docs/adr/0071-cephfs-client-metrics-from-debugfs.md).

- `exporter/cephfs_client_metrics.py`: the exporter (standard library only), shipped as a hashed
  ConfigMap, so an edit rolls the DaemonSet.
- `resources.yaml`: the DaemonSet, one pod per worker, port 9923, scraped by the OTel `cluster`
  collector (annotations, `namespaces.names` and the `otel-scrape` pod label) every 60 s, the
  receiver's default.
- `tests/`: parsing, sampling and rendering against the kernel's text (`tests/run.sh`, in `validate.sh`).

## How it works

The kernel gives each CephFS superblock its own client, with a directory
`/sys/kernel/debug/ceph/<fsid>.client<id>/metrics/` (`latency`, `size`, `caps`, `file`). Bind
mounts of the same superblock (the CSI global mount and every pod's mount) share it. Every 30 s
the exporter reads its own `/proc/self/mountinfo`, which holds the kubelet's mounts through a
`HostToContainer` hostPath, and resolves each new superblock's client with the `ceph.client_id`
vxattr of one mountpoint. That lookup can hang on a stuck MDS, so it runs in a thread of its own
and is never stacked; a scrape reads only debugfs and the cached map.

A 60 s scrape of since-mount counters averages a 10 s stall away. So a sampler thread reads each
client's `metrics/latency`, `mdsc` and `osdc` once a second and keeps, per op, the requests and
summed latency completed in that second, and per client the MDS and OSD requests still waiting.
`mdsc` and `osdc` print no ages: a request's age counts from the first sample that listed it.

| metric | labels | meaning |
| --- | --- | --- |
| `cephfs_client_ops_total` | `op` = read, write, metadata, copyfrom | requests completed since mount |
| `cephfs_client_latency_seconds_total` | `op` | summed latency (below) |
| `cephfs_client_latency_max_seconds` | `op` | slowest single request since mount |
| `cephfs_client_sample_latency_seconds` | `op`, histogram | mean latency of the requests completed in one 1 s sample |
| `cephfs_client_op_latency_upper_bound_seconds` | `op` | largest summed latency of one sample in the last 120 s, plus the floor: no single request completed in the window took longer |
| `cephfs_client_latency_floor_seconds` | `op` | the rounding floor (below): samples whose summed latency is below twice it are skipped |
| `cephfs_client_requests_in_flight` | `kind` = mds, osd | requests waiting for a reply at the last sample |
| `cephfs_client_oldest_request_age_seconds` | `kind` | longest wait of a request still in flight, the maximum over the last 120 s |
| `cephfs_client_bytes_total` | `op` = read, write, copyfrom | bytes; see the read caveat |
| `cephfs_client_caps_total` | `item` = caps, d_lease; `result` = hit, miss | cap and dentry lease lookups |
| `cephfs_client_open_files` | | files open on the mount |
| `cephfs_client_unresolved` | | client directories whose mount is not known yet |
| `cephfs_client_errors_total` | | client directories that failed to parse |

Every per-client series carries `client`, `fs`, `root` (the mounted path in the filesystem), `pv`
(from the pod mount paths, comma-joined if several PVs share a superblock, empty if no pod mounts
it) and `node`. Latency per window is
`rate(cephfs_client_latency_seconds_total[5m]) / rate(cephfs_client_ops_total[5m])`; the share of
1 s samples slower than 1 s is
`1 - rate(cephfs_client_sample_latency_seconds_bucket{le="1"}[5m]) / rate(cephfs_client_sample_latency_seconds_count[5m])`.
A hung request shows in `cephfs_client_oldest_request_age_seconds` while it hangs, and in the
upper bound once it completes. The window is `WINDOW_SECONDS`, two scrapes, so one missed scrape
loses nothing.

Outside Kubernetes, `LABEL_BY=mountpoint` labels by the comma-joined mountpoints (`mountpoint`
instead of `pv`), and `--once` prints one scrape after one sample interval. Neither is used here.

## Limits of the data

- **Latency is reconstructed.** The kernel prints only the lifetime average in whole µs, so the
  sum is ops × average. Its error is up to ops-since-mount × 1 µs, which matters only against
  few ops in the window: use 5 m windows or longer. The exporter never lets it decrease, so a
  rounding step does not read as a counter reset (`rate()` would count the whole lifetime sum as
  new). The clamp lives in memory: a restart of the container, not the pod (a new pod is a new
  series), can show one such spike.
- **The same error sets the per-sample floor**, `cephfs_client_latency_floor_seconds` (ops since
  mount × 1 µs): a second's summed latency is known only to within one floor either way, so a
  sample counts from two floors on, the size of a 1 µs tick of the average. That is about 0.1 s on
  a mount with tens of thousands of ops and 44 s on one with 22 million, which then shows only
  stalls beyond that. The floor grows until the next mount. No histogram sample means nothing
  above two floors, not nothing slow.
- **Stdev is not exported:** the kernel sums squared deviations in 64 bits of ns², which overflow
  past one request over about 3 s or a million with a 3 ms spread; most mounts here are past that.
- **Read bytes stay 0 on most mounts on kernel 6.12** while read ops and latency count (seen on
  every busy mount, including the Jellyfin media one). Read throughput comes from the pool
  (`ceph_pool_rd_bytes`) or the node's NIC instead.
- Clients sharing a superblock cannot be split: both Jellyfin pods on one node share the
  `jellyfin-media-stream` client.
- debugfs has no ABI. A format change shows as `cephfs_client_errors_total`, not as a crash.
- Only kube workers. The Swarm hosts' CephFS mounts (the GPU transcode worker reads the archive
  through one) are not covered.

## Decisions

- Unprivileged: uid 0 owns debugfs, so reading it needs no capability (`drop: [ALL]`,
  `privileged: false`). The namespace is PSA `privileged` only because baseline forbids hostPath.
- Sampling in the exporter, not a shorter scrape: the collector stays generic, and the 1 s view
  costs about 15m CPU per node instead of 60× the stored samples.
- The MDS also receives these metrics from every client (`ceph fs perf stats`, the
  `mds_client_metrics-<fs>` labelled counters), but they are not in VictoriaMetrics and map to a
  mount only through the MDS session list. Exporting them would cover the Swarm hosts as well; it
  is a Ceph-side change under [external/ceph](../../external/ceph/README.md).
