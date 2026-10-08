# 0030 — Telemetry flows through Kafka; the consumer is the retry mechanism

- Status: Accepted
- Date: 2026-07-30
- Current answer: every telemetry source, the cluster's own collectors included, sends OTLP (or
  syslog, carbon and the like) to the OpenTelemetry gateway in `infra/opentelemetry-gateway`,
  which writes everything into Kafka (Strimzi, `infra/strimzi`). The Victoria stores in `apps/`
  consume from Kafka. A write that fails is retried by the Kafka consumer rewinding, not by an
  exporter queue. Kafka runs replication factor 2 with `min.insync.replicas: 1` and tiered
  storage to Ceph RGW.

## Context

The Swarm already ran an ingest gateway into Kafka. The cluster's own collectors export into that
pipeline, not straight into the stores, and the gateway and Kafka moved into the cluster.

## Decision

- **One path in**: collectors export to the gateway, the gateway to Kafka, consumers to the
  stores. Operator and gateway are separate components
  ([0004](0004-one-kustomization-per-component-from-a-root-chart.md)).
- **Kafka is the retry mechanism**, not the exporter. The consumers' exporters have no
  `sending_queue` (it acknowledges on enqueue, so the offset is committed before the write lands
  and a crash loses it) and no exporter retry; the Kafka receivers enable `error_backoff`, which
  rewinds and retries. Without it a failed write pauses the partition until a restart.
- **RF 2, `min.insync.replicas: 1`**: reads don't need HA, writes do, and losing a broker never
  blocks writes. Tiered storage uploads each segment once, from the leader, so RF 2 doubles only
  the small local window.

## Consequences

- A consumer that stops reading shows as lag, not as an error; the runbook is
  [docs/observability-exporter-lag.md](../observability-exporter-lag.md).
- Kafka is a buffer, not a record: Velero does not snapshot it ([0017](0017-backups-stay-in-ceph.md)).
- Tiered storage depends on RGW; an RGW outage stops uploads and the brokers' disks fill.

## References

- `6f997d23`, `c06f574a` (Strimzi, Kafka), `afbf014b` (`error_backoff`), `5144122c` (node syslog
  into the collectors).
- Conversation 2026-07-28 (export into the existing pipeline), 2026-07-30 (retry, sizing).
- [infra/opentelemetry-gateway](../../kube-cluster/infra/opentelemetry-gateway/README.md).
