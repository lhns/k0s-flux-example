# victoriatraces

VictoriaTraces (distributed tracing DB) + its OTel collector — migrated from Docker Swarm (`10.20.2.10`).

## Shape
- **victoriatraces** — the traces store + HTTP API/UI on `:10428`. Its 72G data dir lives on the
  `appdata` CephFS and is **mounted in place** (`/docker/observability/victoriatraces`), never copied.
  Runs as **root** (the data is root-owned; no fsGroup to avoid a recursive chown of 72G).
  Exposed as `victoriatraces.example.com` via Traefik, behind the Authelia middleware.
- **victoriatraces-exporter** — an `otel/opentelemetry-collector-contrib` that reads OTLP spans from
  Kafka and writes them into VictoriaTraces. Config in a ConfigMap (`victoriatraces-exporter.yaml`).

## Trace flow (entirely in-cluster)
apps → the OTel gateway (`infra/opentelemetry-gateway`) → Kafka `otlp_spans` (Strimzi, in-cluster) →
**this exporter** → **victoriatraces `:10428`**.

Nothing on Swarm any more. Both of the exporter's former LAN dependencies moved: Kafka
`10.20.2.10:9092` (the `*_swarm_drain` receiver, removed once its consumer group hit lag 0) and the
central collector `10.20.2.10:4317` (its `otlp/feedback` self-monitoring). Because both replacements
are ClusterIPs rather than external addresses, the exporter now carries two egress vnet labels —
`net.opentelemetry-gateway.kafka` and `net.opentelemetry-gateway.otel-gateway` — where previously it
needed neither.

## Cutover (one-time)
1. Confirm manifests applied — ns, PVC bound, ConfigMap present.
2. **Stop the Swarm victoriatraces stack** — frees the 72G data dir and releases the Kafka consumer
   group `victoriatraces-exporter` (so there's a single active consumer).
3. The Deployments come up: `victoriatraces` mounts the data in place; the exporter joins the Kafka
   group, drains `otlp_spans`, and writes to the DB.
4. **DNS**: point `victoriatraces.example.com` at the Traefik VIP `10.20.2.15`.

## Verify
- `kubectl -n victoriatraces get pods` — both `1/1`.
- victoriatraces `/health` 200; historical spans queryable (data mounted).
- exporter logs: connected to Kafka `10.20.2.10:9092`, group `victoriatraces-exporter`, exporting to
  `victoriatraces:10428` with no retry errors.
- `https://victoriatraces.example.com` (through Authelia) → VT UI shows traces ingested after cutover.

## Rollback
The Swarm stack + its 72G CephFS data are untouched (mounted in place). Restart the Swarm stack to
revert; the kube exporter will step out of the Kafka group.
