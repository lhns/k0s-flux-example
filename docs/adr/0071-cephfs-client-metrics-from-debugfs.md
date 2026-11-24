# 0071 — CephFS client metrics come from the kernel's debugfs, read by a script on the Python image

- Status: Accepted
- Date: 2026-11-20, amended 2026-11-20
- Current answer: `infra/cephfs-client-metrics` runs one unprivileged pod per kube worker that reads
  `/sys/kernel/debug/ceph/*/metrics/` and maps each kernel client to its mount and PV through the
  `ceph.client_id` vxattr. It is a standard-library Python script in a ConfigMap on the pinned
  upstream `python` image, an exception to [0009](0009-own-code-in-own-repos.md) like
  [0063](0063-ceph-usage-measured-in-the-cluster.md).

## Context

OSD and MDS metrics say how fast Ceph answers, not what a client waits for: the network, the
client's queue and readahead are in between. The kernel client keeps request counts and latencies
per superblock in debugfs only. The one exporter found (CM4all's, C++, no published image) takes
its latency from a `metrics/counters` file that mainline kernels do not have, and labels by cephx
user, not by mount.

## Decision

- **debugfs, in the cluster.** The mount behind each client is only visible on the node, so the
  exporter is a DaemonSet with the kubelet's mount tree (`HostToContainer`) and debugfs as
  read-only hostPaths. uid 0 suffices; no capability, no `privileged`. Its namespace is PSA
  `privileged` because baseline forbids hostPath.
- **A script on the upstream image, not an image of its own**: it reads text files and needs
  nothing beyond the standard library. It moves to its own repo once it needs a dependency or a
  build.
- **No CephFS access on scrape.** The vxattr lookup can hang on a stuck MDS, so it runs in the
  background, once per new superblock.
- **Amendment (2026-11-20):** the exporter samples debugfs once a second itself and exports
  per-sample latency, windowed maxima and in-flight request ages, instead of a shorter scrape.

## Consequences

- debugfs has no ABI: a kernel upgrade can change the tables. Parse failures are a metric.
- The latency sum is reconstructed from a rounded lifetime average; short windows are noisy.
- Swarm hosts are not covered. The MDS's own per-client counters would cover every client and are
  the alternative if that is needed.

## References

- [infra/cephfs-client-metrics](../../kube-cluster/infra/cephfs-client-metrics/README.md).
