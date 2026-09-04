# opentelemetry-gateway

The estate's telemetry front door, migrated off the Swarm `opentelemetry-ingest` stack.
Everything that emits telemetry — hosts, containers, network gear, the cluster's own
monitoring collectors — sends here, and everything lands in Kafka. The *consumer* side
(the Victoria exporters) lives under `apps/victoria*`.

```
senders ──► gateway (x2)  ──►  Kafka (3 dual-role KRaft nodes)  ──►  Victoria exporters
            10.20.2.14              tiered storage ──► s3.example.com/otel/kube/
```

| Piece | What it is |
|---|---|
| `resources.yaml` | gateway Deployment (x2), kafkametrics Deployment (x1), ClusterIP + LoadBalancer Services |
| `opentelemetry-gateway.yaml` | gateway collector config (`configMapGenerator`) |
| `opentelemetry-gateway-kafkametrics.yaml` | broker/topic/consumer scraper config |
| `kafka.yaml` | `KafkaNodePool` + `Kafka` (Strimzi; the operator itself is `infra/strimzi`) |
| `topics.yaml` | the four `otlp_*` topics |
| `tiered-storage-plugin.yaml` | the Aiven tiered-storage jars, as an OCI artifact |
| `vnet.yaml` | the `kafka` and `otel-gateway` VirtualNetworks |
| `secret.yaml` | SOPS: S3 credentials for tiered storage |

## Reaching it

- **From outside the cluster** — `10.20.2.14`, a MetalLB VIP. `externalTrafficPolicy: Local`
  preserves the client source IP, which syslog and carbon use to identify the sender.
- **From inside** — `opentelemetry-gateway.opentelemetry-gateway.svc.cluster.local`, plus
  the sender's namespace in the `otel-gateway` vnet and a matching egress label on its pod.

Ports: 4317/4318 OTLP, 2003 carbon, 8006 fluentforward, 8086 influxdb, 14250/14268 jaeger
TCP, 6831/6832 jaeger UDP, 54526 syslog on **both** TCP and UDP. 8888 (own metrics) and
13133 (health) stay pod-local.

## Two collectors, not one

`kafkametrics` is a *cluster-wide* scraper: running it in both gateway replicas would
double-count every broker, topic and consumer metric it emits. It is therefore a separate
singleton Deployment, and it tags its output `cluster_name: otel-kafka-kube` — deliberately
distinct from the Swarm broker's `otel-kafka`, so the two do not silently merge in the
queries in `docs/observability-exporter-lag.md`.

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
  kubectl run otelval --rm -i --restart=Never --image=otel/opentelemetry-collector-contrib:0.157.0 \
    --command -- /otelcol-contrib validate --feature-gates=transform.flatten.logs --config=/dev/stdin \
    < opentelemetry-gateway.yaml
  ```
- **vnet membership labels are `net.<namespace>.<vnetName>`**, namespace first. The reversed
  form fails *silently*: the webhook emits no `kube-vnet.system` label, kube-vnet reports no
  error, and you find out from `connection refused` at runtime.
- **Rotating the S3 credentials needs a manual roll.** Reloader cannot restart a
  `StrimziPodSet`; annotate the pods with `strimzi.io/manual-rolling-update`.
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
volume** — see `tiered-storage-plugin.yaml`. They used to be mounted in place from a CephFS
directory unpacked by hand for the Swarm broker, which meant the plugin version existed only as
two directory names on Ceph and Ceph sat in the broker startup path.

**To upgrade, edit two `url`/`digest` pairs and nothing else.** The digest is not optional and
not derivable — get it with the archive:

```sh
curl -sL <url> | tee >(sha256sum) | tar tzf - | head   # digest AND the wrapper dir name
```

A URL/digest mismatch is **terminal** in oci-composer, which is why Renovate is dashboard-gated
for this dependency (`renovate.json`): it can rewrite the URL but cannot recompute the digest, so
an automerged bump would break the artifact and, in time, the brokers.

The classpath is deliberately **version-free** (`/mnt/tiered-storage/core/*:.../s3/*`) so it never
has to change, and deliberately **two entries** — both archives ship `commons-*.jar`, `httpclient`
and different `commons-codec` versions, so flattening them would silently pick one.

Two things about image volumes worth knowing here: they are pulled by the **kubelet**, not by the
pod, so kube-vnet and cluster DNS are not involved (the node's containerd resolves
`oci-composer.internal:30500` via a containerd registry drop-in); and the reference in `kafka.yaml` is a bare
placeholder that only gets rewritten because of `kustomizeconfig.yaml` — Strimzi's
`spec.template.pod.volumes[]` is not among kustomize's builtin image fieldspecs. Delete that file
and the brokers will try to pull an image called `kafka-tiered-storage`.

## Migration state

**The drain is finished.** Repointing the Swarm gateway at this one froze the Swarm broker
mid-flight while the guaranteed metrics consumer groups still held millions of un-exported
records, so each Victoria exporter temporarily consumed **both** clusters — its live receiver
on the in-cluster bootstrap plus a `kafka/*_swarm_drain` twin on `10.20.2.10:9092`. Nothing was
ingested twice: the old topics are frozen as of the switch and the new ones begin there.

Every group reached lag 0 (`otlp_logs`, `otlp_metrics`, `otlp_meta_metrics`, `otlp_spans` —
`CURRENT-OFFSET == LOG-END-OFFSET` on all of them), so the drain receivers and their pipeline
entries have been removed. **Nothing in this cluster consumes the Swarm broker any more**, and
the Kafka side of that stack can be retired.

Verify with, and this remains a read-only command against the Swarm host:

```sh
kubectl -n opentelemetry-gateway exec otel-kafka-0 -c kafka -- \
  bin/kafka-consumer-groups.sh --bootstrap-server 10.20.2.10:9092 --describe --all-groups
```

**Nothing points at Swarm any more.** The exporters' `otlp/feedback` self-monitoring, the
`opentelemetry-kube-stack` daemon/cluster collectors, `mosquitto` and `mqtt-monitoring` have
all been repointed from `10.20.2.10:4317` to this gateway.

The trap in each of those was the same and is worth remembering for any future sender: the
Swarm collector was an **external** address, which kube-vnet leaves alone, while this gateway
is a **ClusterIP**, which is isolated. Repointing the endpoint without also granting vnet
membership blocks the telemetry instead of redirecting it — and because self-monitoring is not
the data path, that failure is invisible in Grafana until you go looking for the collector's
own metrics. Membership is a `kube-vnet/net.opentelemetry-gateway.otel-gateway: egress` label,
or a `VirtualNetworkBinding` where the pods are operator-managed
(`infra/opentelemetry-monitoring/vnet.yaml`).
