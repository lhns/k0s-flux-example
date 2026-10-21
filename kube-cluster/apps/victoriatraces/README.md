# victoriatraces

VictoriaTraces (distributed tracing DB) + its OTel collector.

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

The exporter carries two egress vnet labels, `net.opentelemetry-gateway.kafka` and
`net.opentelemetry-gateway.otel-gateway`, because both its Kafka and its `otlp/feedback`
self-monitoring target are ClusterIPs.

## Verify
- `kubectl -n victoriatraces get pods` — both `1/1`.
- victoriatraces `/health` 200; historical spans queryable (data mounted).
- exporter logs: connected to Kafka (`otel-kafka-bootstrap.opentelemetry-gateway`), group `victoriatraces-exporter`, exporting to
  `victoriatraces:10428` with no retry errors.
- `https://victoriatraces.example.com` (through Authelia) → VT UI shows traces ingested.
