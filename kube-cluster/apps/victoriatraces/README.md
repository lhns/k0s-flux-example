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

## Cardinality metrics

`victoriatraces-cardinality-exporter` is a [json_exporter](https://github.com/prometheus-community/json_exporter)
(module in `cardinality-exporter.yaml`). The `victoriatraces-exporter` collector probes it every 5m
(job `victoriatraces-cardinality`; the metrics' `service.name` is the job name), and it queries `/select/logsql/stats_query` (the LogsQL API, which VictoriaTraces serves).

| Metric | Labels | Meaning |
| --- | --- | --- |
| `victoriatraces_cardinality_streams_today` | `source` = `service.name` | distinct streams with data since 00:00 UTC today, per service |

The query excludes the `trace_id_idx_stream` index streams (1024 internal streams). For the total, `sum()` over `source`.

Like the other Victoria stores, this counts streams with data **since 00:00 UTC today**, per source: it
resets at midnight and is not a rate. New per hour per source: `rate(<metric>[30m]) * 3600` (rate treats the midnight drop as a reset; the first half hour after midnight counts every still-active one again).
Store-wide truly new streams: `rate(vt_streams_created_total[1h]) * 3600`.

**In storage (hourly)**: separate jobs `victoriatraces-cardinality-retention` and `-retention-total` (modules
`retention`, `retention_total`; `scrape_timeout` 5m) count `count_uniq(_stream_id)` over the whole
retention (`_time:62d` = `-retentionPeriod=2`; keep them in sync).

| Metric | Labels | Meaning |
| --- | --- | --- |
| `victoriatraces_cardinality_streams_retention` | `source` = `service.name` | distinct streams in storage, per service |
| `victoriatraces_cardinality_streams_retention_total` | | distinct streams in storage, all sources |

Compare with `_today` for size vs. churn. Per-source query takes ~16 s, the total under 1 s.

**New per hour (hourly)**: jobs `victoriatraces-cardinality-new-1h` and `-new-1h-total` (modules `new_1h`, `new_1h_total`)
count streams in the last hour that were not in the window `[now-62d, now-1h)`, i.e. exactly new ones.
Unlike the change in `_retention_total`, that is not net of streams expiring. A source with none has no series (read as 0); the total is 0.

| Metric | Labels | Meaning |
| --- | --- | --- |
| `victoriatraces_cardinality_streams_new_1h` | `source` = `service.name` | span streams first seen in the last hour, per service |
| `victoriatraces_cardinality_streams_new_1h_total` | | the same, all sources |

### Decisions

- **json_exporter, one per store**: the numbers exist only as API responses. It carries `both` on
  the store's vnet (queries the store, is probed by the collector).
- **Every 5m**: the numbers move slowly and the queries are heavy.
