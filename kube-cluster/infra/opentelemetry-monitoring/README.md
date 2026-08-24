# opentelemetry

The k8s-native equivalent of the Swarm `docker-observer` (per-host `otelcol-contrib`
doing hostmetrics + syslog, plus `docker_stats`). Deploys the
[`opentelemetry-kube-stack`](https://github.com/open-telemetry/opentelemetry-helm-charts/tree/main/charts/opentelemetry-kube-stack)
chart = the **OpenTelemetry Operator** + two preset **collectors**.

- `release.yaml` — Namespace + HelmRepository + HelmRelease (chart pinned; Renovate
  tracks it via the Flux manager). The operator's admission webhook uses
  **cert-manager** (`dependsOn: infra-cert-manager`); the chart installs the otel +
  prometheus-operator CRDs itself.
- `vnet.yaml` — kube-vnet wiring for the Prometheus scrape only (see below).

## Collectors

| Collector | Kind | Collects | Presets |
|---|---|---|---|
| `otelstack-daemon` | DaemonSet (one/node) | node **hostmetrics**, **kubelet**/pod/container metrics, **container logs**, **node syslog** | logsCollection, hostMetrics, kubeletMetrics, kubernetesAttributes |
| `otelstack-cluster` | Deployment (singleton) | **cluster-state** metrics, **k8s events**, **Prometheus**-scraped app metrics | clusterMetrics, kubernetesEvents, kubernetesAttributes |

`clusterMetrics`/`kubernetesEvents` are disabled on the daemon (chart default has them
on) so cluster-scoped data isn't duplicated per node.

## Export — one OTLP endpoint (the routing knob)

Everything (metrics + logs, and a traces passthrough seed) exports through a single
`otlp_grpc` exporter to the **in-cluster aggregation gateway**
(`infra/opentelemetry-gateway`) → Kafka → Victoria, i.e. the same pipeline as the rest
of the estate (not written straight into the in-cluster Victoria backends). It was the
Swarm collector at `10.20.2.10:4317` until that stack was retired; because a ClusterIP
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
honouring `prometheus.io/scrape|port|path` — the classic annotation convention (today
only the flux controllers carry it). **kube-vnet caveat:** scrape dials to in-cluster
pods are ingress-isolated (not an auto-allow), so each scrape-target namespace must
admit the collector. `vnet.yaml` sets up the `otel-scrape` egress vnet (collector side)
and an ingress binding for **flux-system**; add an equivalent ingress binding in any
other namespace whose annotated pods should be scraped.
