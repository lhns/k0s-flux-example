# opentelemetry

The k8s-native equivalent of the Swarm `docker-observer` (per-host `otelcol-contrib`
doing hostmetrics + syslog, plus `docker_stats`). Deploys the
[`opentelemetry-kube-stack`](https://github.com/open-telemetry/opentelemetry-helm-charts/tree/main/charts/opentelemetry-kube-stack)
chart = the **OpenTelemetry Operator** + two preset **collectors**.

- `release.yaml` — HelmRepository + HelmRelease (chart pinned; Renovate
  tracks it via the Flux manager). The operator's admission webhook uses
  **cert-manager** (`dependsOn: infra-cert-manager`); the chart installs the otel +
  prometheus-operator CRDs itself.
- `vnet.yaml` — kube-vnet wiring for the pod scrape and the export (see below).

## Collectors

| Collector | Kind | Collects | Presets |
|---|---|---|---|
| `opentelemetry-kube-stack-daemon-collector` | DaemonSet (one/node) | node **hostmetrics**, **kubelet**/pod/container metrics, **container logs**, **node syslog** | logsCollection, hostMetrics, kubeletMetrics, kubernetesAttributes |
| `opentelemetry-kube-stack-cluster-collector` | Deployment (singleton) | **cluster-state** metrics, **k8s events**, **Prometheus**-scraped app metrics | clusterMetrics, kubernetesEvents, kubernetesAttributes |

`clusterMetrics`/`kubernetesEvents` are disabled on the daemon (chart default has them
on) so cluster-scoped data isn't duplicated per node.

## Export — one OTLP endpoint (the routing knob)

Everything (metrics + logs, and a traces passthrough seed) exports through a single
`otlp_grpc` exporter to the **in-cluster aggregation gateway**
(`infra/opentelemetry-gateway`) → Kafka → Victoria, i.e. the same pipeline as the rest
of the estate (not written straight into the in-cluster Victoria backends). It was the
Swarm collector at `10.20.176.187:4317` until that stack was retired; because a ClusterIP
is isolated where an external address is not, the collectors now need vnet membership
for the export, granted by a `VirtualNetworkBinding` in `vnet.yaml` (they are
operator-managed, so there is no pod template to label). To re-route — a Kafka
exporter, direct-to-Victoria, a different gateway — change the `otlp_grpc` exporter in **both** collectors in
`release.yaml`. That is the only knob.

## Node syslog (full parity with the Docker hosts)

The stock collector image has no `journalctl`, so node syslog is not a journald
receiver. Instead the daemon runs a **`syslog` receiver on hostPort `14514/udp`**, and
each worker's `rsyslog` forwards `*.info` to `127.0.0.1:14514` — configured out-of-band
via **`k0sctl.yaml`** (`k0s-files/50-otel-rsyslog.conf` + a reload hook), the same
host-config mechanism as the inotify sysctl. Apply with `k0sctl apply` (not Flux).

## Host/node labeling

Every signal — especially syslog, which `k8sattributes` can't enrich (a syslog record
isn't a pod) — is stamped by a `resourcedetection/env` processor (`env` + `system` +
`k8snode` detectors) so it carries `host.name` (= the node, from the downward-API node
name, not the pod), `host.arch`, `os.*`, `k8s.node.name`/`k8s.node.uid`. Filter by
host/node in Grafana.

## Prometheus scraping (annotation-based)

The `cluster` collector runs a `prometheus` receiver with a `kubernetes_sd` job
honouring `prometheus.io/scrape|port|path` — the classic annotation convention.
**kube-vnet caveat:** scrape dials to in-cluster
pods are ingress-isolated (not an auto-allow), so each scrape-target namespace must
admit the collector. `vnet.yaml` sets up the `otel-scrape` egress vnet (collector side)
and one ingress binding per scraped namespace; add an equivalent one in any
other namespace whose annotated pods should be scraped. The collector's
`namespaces.names` must list that namespace too: either edit alone is silent.

The cluster collector stays generic: annotation-based scraping and cluster-wide receivers. Scrape
rules specific to one app or system (relabeling, filters, TLS quirks, static targets) belong to
that component or system, e.g. a collector exporting to the gateway that Ceph runs on its own hosts, see
[external/ceph](../../external/ceph/README.md) ([ADR 0056](../../../docs/adr/0056-external-group-for-systems-outside-the-cluster.md)).
The one exception is the CNPG `role` rule in the `kubernetes-pods` job.

## Dashboards

Two Grafana Operator `GrafanaDashboard`s in folder *Cluster* (`grafana.yaml`, JSON in
`dashboards/`), for finding a bottleneck and fixing it.

- **Servers** (`/d/servers`): every host running the OTel hostmetrics receiver (`system.*`):
  the Ceph, Docker and k8s nodes. Variables: server, disk device.
- **Kubernetes Containers** (`/d/k8s-containers`): kubeletstats and k8s_cluster metrics.
  Variables: node, namespace, workload, pod. The node variable is named `server` so the
  dashboard links carry the selection both ways.

The top of each is the at-a-glance view: a colour means act. The rows below drill down, and each
panel's description says what bad looks like and what fixes it. Capacity is judged by
**time to full** from the trend, not by fixed percentages; fixed fractions only appear as guards
(over half full, so an empty disk taking writes stays quiet, and at least 97% full, so a full
static disk still alerts).

### Runbook: servers

| Symptom | Look at | Fix |
| --- | --- | --- |
| Everything slow, CPU pinned | CPU busy, CPU time by mode, Top pods by CPU | Move or limit the consumer, add cores |
| Load high, CPU busy low, iowait high | iowait, Disk I/O latency and queue depth, Blocked processes | Storage is the bottleneck: find the writer (*Disk I/O by pod*), check Ceph |
| VM slow with no visible cause | Steal | Hypervisor overcommitted: rebalance VMs |
| One core pinned | Hottest core, softirq/interrupt in CPU time by mode | Single-threaded process or IRQ hotspot: spread IRQs, scale out |
| Memory leak, OOM kills | Memory TTF, Available memory trend, Swap activity, Top pods by memory | Restart or limit the leaking service, add RAM |
| Disk filling | Filesystems (growth/day, time to full), Free space projected in 24h | Prune logs, images or data, or grow the disk |
| Inodes running out | Inodes used | Delete small-file caches |
| Filesystem read-only | `server-filesystem-readonly` alert | I/O errors: dmesg, fsck, remount |
| Link saturated, packet loss | Throughput, Errors and drops | Find the top talker; check NIC, cable, MTU |
| Socket leak, connection churn | TCP connections (CLOSE_WAIT, TIME_WAIT) | Fix the app's socket handling, use keep-alive |
| Host down | Server health *Last seen* | Host or its collector |
| Noisy neighbour on a k8s node | Top pods by CPU, memory, disk I/O | Requests and limits, move the pod |

### Runbook: containers

| Symptom | Look at | Fix |
| --- | --- | --- |
| CPU throttling | CPU against limit (1.0 = throttled) | Raise or drop the CPU limit, keep the request |
| Near memory limit, OOM kills | RSS against limit, Memory time to limit, Last terminated reason | Raise the limit or fix the leak |
| Restart loop | Restarts per hour, Last terminated reason | `kubectl logs -p` |
| Pods Pending | Pods needing attention, Node capacity (*requested*) | Lower over-sized requests, add a node, fix the PVC or image |
| Not ready, unavailable replicas | Pods needing attention, Unavailable workloads | Read the log and probe events; roll back a stuck rollout |
| Over- or under-provisioned | CPU and Memory right-sizing (p95 / request) | Request ≈ p95; limit above the max |
| Noisy neighbours | Select a node, then the top-15 panels and Disk I/O by pod | Requests and limits, move the pod |
| PVC filling | PVCs (time to full) | Prune or grow the PVC; never delete the PV ([AGENTS.md](../../../AGENTS.md)) |
| Ephemeral storage | Ephemeral storage by container, emptyDir usage | Write to a volume, rotate logs |

### Limits of the data

- On k8s nodes hostmetrics runs in the collector pod's network namespace: network and TCP
  panels show pod traffic, and host-network traffic (Ceph kernel clients, hostNetwork pods) is
  not visible. `system.uptime` and process counts exist only on some hosts.
- hostmetrics has no PSI, OOM-kill counter or clock offset. The Docker nodes do export them through
  node-exporter (`node_vmstat_oom_kill`, `node_timex_offset_seconds`, `node_pressure_*`), but the k8s
  nodes do not, so there is no cluster-wide host OOM or clock-skew alert.
- kubeletstats has no CFS throttling counter: *usage at the limit* stands in for throttling.
  `container-oom-killed` relies on `k8s.container.status.last_terminated_reason`, enabled in
  `release.yaml`.

## Alerts

`grafana.yaml`, folder *Cluster*, rule groups `infra-opentelemetry-monitoring-{containers,servers,capacity,telemetry,workloads}`; every rule was backtested over 7 days before it was added.

| Rule | Fires when | For | Severity |
| --- | --- | --- | --- |
| `server-down` | no host metrics for 5m while other hosts report | 5m | critical |
| `server-metrics-stalled` | no host reports for 10m (pipeline, not hosts) | 5m | critical |
| `server-disk-fill-24h` / `-4h` | time to full < 24h / < 4h (6h trend, over half full), or ≥ 97% | 30m / 15m | warning / critical |
| `server-inodes-fill` | inodes full within 24h | 30m | warning |
| `server-memory-exhaustion` | available memory gone within 2h (1h trend), under a quarter left | 15m | warning |
| `server-cpu-saturated` | CPU > 95% busy (15m average) | 30m | warning |
| `server-iowait-high` | iowait > 40% | 30m | warning |
| `server-cpu-steal` | steal > 25% | 1h | warning |
| `server-load-high` | 15m load > 2× cores | 30m | warning |
| `server-network-errors` | errors + drops > 1% of packets | 15m | warning |
| `server-filesystem-readonly` | a filesystem is mounted `ro` | 5m | critical |
| `k8s-node-not-ready` / `k8s-node-pressure` | Ready false / Memory- or DiskPressure | 5m | critical / warning |
| `pvc-fill-24h` / `-4h` | as for server disks | 30m / 15m | warning / critical |
| `container-oom-risk` | RSS reaches the limit within 1h (over 70% of it), or ≥ 90% | 15m | warning |
| `container-oom-killed` | restarted with last reason OOMKilled | 0 | warning |
| `container-restart-loop` | 3+ distinct `container.id` of one container in 15m (series count would misfire on relabels) | 10m | warning |
| `container-cpu-at-limit` | usage > 90% of the CPU limit | 30m | warning |
| `pod-pending` / `container-not-ready` | Pending / not ready (restart loops excluded) | 15m | warning |
| `k8s-metrics-stalled` / `k8s-kubelet-metrics-stalled` | no `k8s.pod.phase` / `k8s.pod.cpu.time` for 10m (also on NoData); emailed | 5m | critical |
| `job-failed` | a Job gets failed pods (fires once, 6h after it first shows) | 0 | warning |

Backtests over 7 days of these rules:

| Rule | Result | Decision |
| --- | --- | --- |
| `container-restart-loop` | an ARC listener (79 ids/15m at peak), `gitea-runner-0` act-runner (409 restarts), a brief `cheogram-sip` episode; none on the 2026-10-18 collector-upgrade burst, which the series-count form flagged | as specified |
| `k8s-metrics-stalled`, `k8s-kubelet-metrics-stalled` | nothing (newest sample seconds old) | as specified |
| `job-failed` | the two failed `synapse-media-sweep` jobs, once each | as specified |
