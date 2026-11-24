# victorialogs

VictoriaLogs (logs DB, `:9428`) + its OTel collector `victorialogs-exporter`, which reads OTLP
logs from Kafka (`otlp_logs`) and writes them in. Config in `victorialogs-exporter.yaml`.

## Cardinality metrics

`victorialogs-cardinality-exporter` is a [json_exporter](https://github.com/prometheus-community/json_exporter)
(module in `cardinality-exporter.yaml`). The `victorialogs-exporter` collector probes it every 5m
(job `victorialogs-cardinality`; the metrics' `service.name` is the job name), and it queries `/select/logsql/stats_query`.

| Metric | Labels | Meaning |
| --- | --- | --- |
| `victorialogs_cardinality_streams_today` | `source` = `service.name` | distinct `_stream`s with data since 00:00 UTC today, per service |

`source=""` is logs without a `service.name`. For the total, `sum()` over `source`.

Like the other Victoria stores, this counts streams with data **since 00:00 UTC today**, per source: it
resets at midnight and is not a rate. New per hour per source: `rate(<metric>[30m]) * 3600` (rate treats the midnight drop as a reset; the first half hour after midnight counts every still-active one again).
Store-wide truly new streams: `rate(vl_streams_created_total[1h]) * 3600`.

**In storage (hourly)**: separate jobs `victorialogs-cardinality-retention` and `-retention-total` (modules
`retention`, `retention_total`; `scrape_timeout` 5m) count `count_uniq(_stream_id)` over the whole
retention (`_time:62d` = `-retentionPeriod=2`; keep them in sync).

| Metric | Labels | Meaning |
| --- | --- | --- |
| `victorialogs_cardinality_streams_retention` | `source` = `service.name` | distinct log streams in storage, per service |
| `victorialogs_cardinality_streams_retention_total` | | distinct log streams in storage, all sources |

Compare with `_today` for size vs. churn. Per-source query takes ~1 s, the total under 1 s.

**New per hour (hourly)**: jobs `victorialogs-cardinality-new-1h` and `-new-1h-total` (modules `new_1h`, `new_1h_total`)
count streams in the last hour that were not in the window `[now-62d, now-1h)`, i.e. exactly new ones.
Unlike the change in `_retention_total`, that is not net of streams expiring. A source with none has no series (read as 0); the total is 0.

| Metric | Labels | Meaning |
| --- | --- | --- |
| `victorialogs_cardinality_streams_new_1h` | `source` = `service.name` | log streams first seen in the last hour, per service |
| `victorialogs_cardinality_streams_new_1h_total` | | the same, all sources |

### Decisions

- **json_exporter, one per store**: the numbers exist only as API responses. It carries `both` on
  the store's vnet (queries the store, is probed by the collector).
- **Every 5m**: the numbers move slowly and the queries are heavy.
