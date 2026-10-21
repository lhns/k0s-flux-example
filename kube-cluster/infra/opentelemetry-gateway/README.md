# opentelemetry-gateway

The estate's telemetry front door, migrated off the Swarm `opentelemetry-ingest` stack.
Everything that emits telemetry — hosts, containers, network gear, the cluster's own
monitoring collectors — sends here, and everything lands in Kafka. The *consumer* side
(the Victoria exporters) lives under `apps/victoria*`.

```
senders ──► gateway (x2)  ──►  Kafka (3 dual-role KRaft nodes)  ──►  Victoria exporters
            10.20.176.58              tiered storage ──► s3.example.com/otel/kube/
```

| Piece | What it is |
|---|---|
| `resources.yaml` | gateway Deployment (x2), kafkametrics Deployment (x1), ClusterIP + LoadBalancer Services |
| `opentelemetry-gateway.yaml` | gateway collector config (`configMapGenerator`) |
| `opentelemetry-gateway-kafkametrics.yaml` | broker/topic/consumer scraper config |
| `kafka.yaml` | `KafkaNodePool` + `Kafka` (Strimzi; the operator itself is `infra/strimzi`) |
| `topics.yaml` | the four `otlp_*` topics |
| `artifacts/tiered-storage-plugin.yaml` | the Aiven tiered-storage jars, as an OCI artifact |
| `vnet.yaml` | the `kafka` and `otel-gateway` VirtualNetworks |
| `secret.yaml` | SOPS: S3 credentials for tiered storage |

## Reaching it

- **From outside the cluster** — `10.20.176.58`, a MetalLB VIP. `externalTrafficPolicy: Local`
  preserves the client source IP, which syslog and carbon use to identify the sender.
- **From inside** — `opentelemetry-gateway.opentelemetry-gateway.svc.cluster.local`, plus
  the sender's namespace in the `otel-gateway` vnet and a matching egress label on its pod.

Ports: 4317/4318 OTLP, 2003 carbon, 8006 fluentforward, 8086 influxdb, 14250/14268 jaeger
TCP, 6831/6832 jaeger UDP, 54526 syslog on **both** TCP and UDP. 8888 (own metrics) and
13133 (health) stay pod-local.

## Two collectors, not one

`kafkametrics` is a *cluster-wide* scraper: running it in both gateway replicas would
double-count every broker, topic and consumer metric it emits. It is therefore a separate
singleton Deployment, and it tags its output `cluster_name: otel-kafka-kube`.

The gateway itself runs 2 replicas. MetalLB L2 elects one node for the VIP and ETP-Local
means only the pod on that node serves external traffic, so this is hot standby rather than
load sharing — its value is removing the blackhole window a single replica would have during
a rollout.

## After a cluster rebuild, restart the consumers

Destroying and recreating the Kafka cluster (e.g. renaming the node pool) gives it a new
cluster ID, and the Victoria exporters do **not** all recover from that. Observed: the
`kafka/metrics` receivers rejoined on their own while every `kafka/meta_metrics` receiver
silently stopped — no error logged, pod healthy, just no consumer group. Meta-metrics were
lost for ~45 minutes before anyone noticed.

So: `kubectl rollout restart` every `victoria*-exporter` after a rebuild, and verify by
**counting the groups, not reading them** — there should be ten:

```
kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --all-groups
```

A list of healthy groups looks fine whether or not four are missing from it. Check for
absence explicitly.

## Gotchas worth knowing before you edit

- **`syslog` needs two receivers.** A single receiver with both `tcp:` and `udp:` set only
  ever listens on TCP — `Build()` returns on the first transport it finds. It logs nothing
  and passes config validation. The Swarm collector had exactly this shape and had been
  discarding every UDP datagram it was configured to accept.
- **The Kafka exporter's `topic` lives in per-signal blocks** (`traces:` / `metrics:` /
  `logs:`), not at the top level. The old top-level key is now rejected outright.
- **Validate before you push.** `otelcol-contrib validate --config=...` in a throwaway pod
  catches all of the above in seconds:
  ```
  kubectl run otelval --rm -i --restart=Never --image=<collector image from resources.yaml> \
    --command -- /otelcol-contrib validate --feature-gates=transform.flatten.logs --config=/dev/stdin \
    < opentelemetry-gateway.yaml
  ```
- **vnet membership labels are `net.<namespace>.<vnetName>`**, namespace first. The reversed
  form fails *silently*: the webhook emits no `kube-vnet.system` label, kube-vnet reports no
  error, and you find out from `connection refused` at runtime.
- **Rotating the S3 credentials needs a manual roll.** Reloader cannot restart a
  `StrimziPodSet`; annotate the pods with `strimzi.io/manual-rolling-update`.
- **Brokers have required pod anti-affinity on the hostname** (`kafka.yaml`): a PDB cannot stop a
  drain evicting two co-located brokers. A fourth node is not needed, but fewer than three leaves a
  broker Pending.
- **Changing the `Kafka` CR rolls all three brokers sequentially.** RF=2 means a partition
  keeps a leader throughout, so producers never block.

## Kafka shape and why

3 dual-role KRaft nodes, RF=2 on everything, `min.insync.replicas: 1` — losing a broker
degrades reads but never blocks writes, which is the property that matters for an ingest
path. One partition per topic is enough for this volume.

Tiered storage offloads to `s3.example.com/otel` under `key.prefix: kube/`. Only the **leader**
uploads and remote segments are *not* replicated (KIP-405), so the 10-day retention is
stored in S3 exactly once regardless of RF — RF=2 costs 2x only the ~2 GB local hot window
per topic. The Swarm cluster wrote to the bucket root, so its objects stay trivially
separable for cleanup.

### The Aiven plugin jars

Composed into one OCI artifact from the upstream release archives and mounted as an **image
volume** — see `artifacts/tiered-storage-plugin.yaml`. They used to be mounted in place from a CephFS
directory unpacked by hand for the Swarm broker, which meant the plugin version existed only as
two directory names on Ceph and Ceph sat in the broker startup path.

**Renovate bumps each layer's `url` and `subpath`**, and `scripts/check-fetch-digests.py --fix`
(its postUpgradeTask) the `digest`. A URL/digest mismatch is **terminal** in oci-composer, so the
lint fails on one. By hand: edit `url` and `subpath`, then run that script.

The classpath is deliberately **version-free** (`/mnt/tiered-storage/core/*:.../s3/*`) so it never
has to change, and deliberately **two entries** — both archives ship `commons-*.jar`, `httpclient`
and different `commons-codec` versions, so flattening them would silently pick one.

Two things about image volumes worth knowing here: they are pulled by the **kubelet**, not by the
pod, so kube-vnet and cluster DNS are not involved (the node's containerd resolves
`oci-composer.internal:30500` via a containerd registry drop-in); and the reference in `kafka.yaml` is
`${KAFKA_TIERED_STORAGE_REF}`, the digest-pinned ref the composition exports (see
[`infra/oci-composer`](../oci-composer/README.md#how-a-workload-names-an-artifact)). A new digest
makes Strimzi roll the brokers one at a time.

## Senders

Nothing consumes the Swarm broker any more. This gateway is a **ClusterIP**, which kube-vnet
isolates (the old Swarm collector was external, which it leaves alone): a sender needs the
`kube-vnet/net.opentelemetry-gateway.otel-gateway: egress` label, or a `VirtualNetworkBinding`
where the pods are operator-managed (`infra/opentelemetry-monitoring/vnet.yaml`). Without it
the telemetry is blocked, not redirected, and self-monitoring failures stay invisible.

## Alerts

`grafana.yaml`: rule group `infra-opentelemetry-gateway`, folder *Cluster*; backtested over 7 days before it was added.

| Rule | Fires when | For | Severity |
| --- | --- | --- | --- |
| `kafka-consumer-lag` | consumer group more than 100k messages behind | 30m | warning |

Backtest over 7 days:

| Rule | Result | Decision |
| --- | --- | --- |
| `kafka-consumer-lag` | nothing | as specified |
