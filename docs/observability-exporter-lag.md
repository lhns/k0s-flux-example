# Kafka→VictoriaMetrics exporter lag: measurement runbook

The `victoriametrics-exporter` and `victoriametrics-cluster-exporter` OTel collectors each
run **two** Kafka consumer pipelines over the same topics. The **best-effort** one keeps up;
the **guaranteed** one falls millions of messages behind and periodically stalls to *zero*
throughput. Restarting appears to help for a few hours.

This doc records the metrics, the exact queries, and a measured baseline so the behaviour can
be picked up and analysed later — the symptom is intermittent on a multi-hour scale, so it
cannot be characterised in one sitting.

**Status: root cause found in source; fix APPLIED 2026-07-30.** A single export timeout made
the kafka receiver call `PauseFetchPartitions`, which **pauses the partition until a
rebalance** — so one slow write stopped the guaranteed pipeline until the pod was restarted.
An *availability* problem, not a throughput one (§6).

**Fix shipped:** `error_backoff.enabled: true` on every kafka receiver in both collectors
(commit `afbf014`) — *not* `sending_queue`, which would lose data. Confirmed live: the
receivers now log *"Backing off due to error from the next consumer"* with `delay=5` on the
guaranteed receivers and `delay=1` on the best-effort ones (i.e. both profiles applied), and
**zero `pausing partition` events** since. Consumption after the change: guaranteed 21–37
msg/s, best-effort 5.7 msg/s, against 5.8 msg/s production.

**Still to confirm:** the real test is the next backend slow-period — the pipeline should slow
and recover instead of wedging. Re-run query #4 over 24h and look for the flat-zero plateaus
to be gone.

## Table of contents
1. [How the two pipelines differ](#1-how-the-two-pipelines-differ)
2. [Metric inventory](#2-metric-inventory)
3. [Running the queries](#3-running-the-queries)
4. [The queries that matter](#4-the-queries-that-matter)
5. [Measured baseline](#5-measured-baseline-2026-07-29-2306-cest)
6. [Findings and open hypotheses](#6-findings-and-open-hypotheses)
7. [Remediation (1 applied, rest untested)](#7-remediation-1-applied-rest-untested)
8. [Gotchas](#8-gotchas)

## 1. How the two pipelines differ

Both configs (`kube-cluster/apps/victoriametrics/victoriametrics-exporter.yaml`,
`kube-cluster/apps/victoriametrics-cluster/victoriametrics-cluster-exporter.yaml`) define:

| | pipeline `metrics` (**guaranteed**) | pipeline `metrics/latest` (**best-effort**) |
| --- | --- | --- |
| consumer group | `<name>-exporter` | `<name>-exporter-latest` |
| `initial_offset` | default (earliest) | `latest` |
| `message_marking.on_error` | **`false`** — on failure the record is *not* marked ⇒ the receiver sets `fatalRecord` and **pauses the partition** (§6) | **`true`** — marks anyway ⇒ drops the batch ⇒ always progresses |
| processors | `transform/truncate_attributes` | `+ interval/downsample` (60s) |
| request size | one Kafka message per write | 60s of accumulated data (~**100k datapoints**) per write |

⚠️ **A common misreading (I made it):** "offset not committed ⇒ the message is redelivered".
It is **not**. Classic Kafka consumer groups do not redeliver un-marked records — the receiver
source says so explicitly (redelivery would need KIP-932 share groups). Not committing only
affects where a *new* consumer session starts. Within a session, something has to rewind the
cursor, and only `error_backoff` does that.

Both exporters keep `sending_queue: false` / `retry_on_failure: false` (deliberately — see
§7). The setting that actually mattered was the *receiver's* `error_backoff`, which was unset
and is what allowed the partition pause; it is now enabled on every kafka receiver (§6/§7).

Neither Deployment sets `resources:` — so there are **no CPU/memory limits** and CFS
throttling is *not* a factor here (unlike the valkey readiness-probe incident).

## 2. Metric inventory

**Use the `kafka.*` metrics as the primary signal.** They come from a `kafkametricsreceiver`
(in the Swarm collector, `cluster_name="otel-kafka"`) and carry a **`group`** label, so each
consumer group is directly addressable:

| metric | labels | meaning |
| --- | --- | --- |
| `kafka.consumer_group.lag_sum` | `group`, `topic`, `cluster_name` | **messages behind** — the headline number |
| `kafka.consumer_group.offset_sum` | `group`, `topic` | committed offset; `rate()` = **consumption msg/s** |
| `kafka.consumer_group.members` | `group` | consumer count (0 ⇒ nothing attached) |
| `kafka.partition.current_offset` | `topic`, `partition` | head of log; `rate()` = **production msg/s** |
| `kafka.partition.oldest_offset` | `topic`, `partition` | retention floor — if this passes a group's offset, data was lost |

Groups present: `victoriametrics-exporter`, `victoriametrics-exporter-latest`,
`victoriametrics-cluster-exporter`, `victoriametrics-cluster-exporter-latest` (plus
`victorialogs-exporter`, `victoriatraces-exporter`).

**Collector self-metrics** (via each exporter's `prometheus/feedback` → Kafka → back):
`otelcol_exporter_sent_metric_points`, `otelcol_exporter_send_failed_metric_points`,
`otelcol_exporter_queue_size`/`_capacity`, `otelcol_exporter_in_flight_requests`,
`otelcol_receiver_accepted_metric_points`, `otelcol_receiver_refused_metric_points`,
`otelcol_kafka_receiver_offset_lag`/`_messages`/`_records`/`_read_latency_*`,
`otelcol_process_memory_rss`, `otelcol_process_runtime_heap_alloc_bytes`,
`otelcol_process_uptime`, `otelcol_process_cpu_seconds`.

> ⚠️ **The `otelcol_*` metrics cannot be attributed to a specific exporter.** Their only
> distinguishing label is `service.instance.id` (a UUID that changes on every restart);
> `service.name` is `otelcol-contrib` for all of them. Map UUID→pod once per restart via the
> pod logs (each line carries `service.instance.id`), or just prefer the `kafka.*` metrics.

## 3. Running the queries

No Grafana datasource needed — query vmselect directly from the jumphost:

```sh
ssh lhns@10.20.5.15
kubectl port-forward -n victoriametrics-cluster svc/vmselect 18481:8481 >/dev/null 2>&1 &
Q=http://127.0.0.1:18481/select/0/prometheus/api/v1
# always url-encode: dotted metric names must be written {__name__="kafka...."}
curl -sG "$Q/query" --data-urlencode 'query=<PROMQL>'
curl -sG "$Q/query_range" --data-urlencode 'query=<PROMQL>' \
     --data-urlencode "start=$(($(date +%s)-86400))" --data-urlencode "end=$(date +%s)" \
     --data-urlencode 'step=1800'
```

## 4. The queries that matter

```promql
# 1. Lag per consumer group — the headline
sum by (group) ({__name__="kafka.consumer_group.lag_sum", group=~"victoriametrics.*"})

# 2. Consumption rate per group (msg/s). Guaranteed groups at ~0 = STALLED.
sum by (group) (rate({__name__="kafka.consumer_group.offset_sum", group=~"victoriametrics.*exporter.*"}[10m]))

# 3. Production rate per topic (msg/s) — compare against #2 to see if it can keep up
sum by (topic) (rate({__name__="kafka.partition.current_offset", topic=~"otlp_(meta_)?metrics"}[10m]))

# 4. Guaranteed-only consumption, for the stall/recover pattern over 12-24h
sum by (group) (rate({__name__="kafka.consumer_group.offset_sum", group=~"victoriametrics(-cluster)?-exporter"}[10m]))

# 5. Are we losing data to retention? (>0 means the backlog aged out)
sum by (topic) ({__name__="kafka.partition.oldest_offset", topic=~"otlp_.*"})
  - on(topic) group_right() sum by (topic) ({__name__="kafka.consumer_group.offset_sum", group="victoriametrics-exporter"})

# 6. Write failures + throughput at the exporter (all instances aggregated)
sum(rate(otelcol_exporter_send_failed_metric_points[10m]))
sum(rate(otelcol_exporter_sent_metric_points[10m]))

# 7. Restart detector / memory growth (per collector instance)
otelcol_process_uptime
otelcol_process_memory_rss

# 8. WEDGE DISCRIMINATORS — run these while it is stalled (see §6 table)
#    in-flight requests: 0 while wedged, 1 when healthy => not blocked in HTTP
otelcol_exporter_in_flight_requests
#    group membership: stays 2 while wedged => not a rebalance/session timeout
sum by (group) ({__name__="kafka.consumer_group.members", group=~"victoriametrics.*"})
#    backend saturation: limiter never engaged, current stays « capacity(40)
max_over_time(vm_concurrent_insert_limit_reached_total[24h])
max_over_time(vm_concurrent_insert_current[24h])
#    backend connectivity (stale cumulative is fine; watch the RATE)
sum(rate(vm_rpc_dial_errors_total[10m]))
```

To attribute an `otelcol_*` series to a pod, grab its `service.instance.id` from the pod log
(every line carries it) and filter with a quoted label name:
`otelcol_exporter_in_flight_requests{"service.instance.id"="<uuid>"}`.

Backend health, which is the suspected driver — the VM backends' own `/metrics` scrapes were
timing out during the stall, so watch these too:

```sh
kubectl port-forward -n victoriametrics-cluster svc/vminsert 18480:8480 &
curl -s localhost:18480/metrics | grep -E '^vm_rows_inserted_total|^vm_rpc_
# and on vmstorage:8482 —
#   vm_slow_row_inserts_total, vm_slow_per_day_index_inserts_total,
#   vm_data_size_bytes, vm_cache_misses_total{type="storage/tsid"}
```

## 5. Measured baseline (2026-07-29, 23:06 CEST)

Compare future readings against this.

```
production          otlp_metrics 5.5 msg/s   otlp_meta_metrics 0.3 msg/s   (~5.8 total)

consumption         victoriametrics-exporter                  0.0 msg/s   lag 4,039,335
                    victoriametrics-exporter-latest           5.8 msg/s   lag 1
                    victoriametrics-cluster-exporter          0.0 msg/s   lag 2,597,828
                    victoriametrics-cluster-exporter-latest    5.8 msg/s   lag 1

24h lag band        victoriametrics-exporter          3.92M – 4.20M  (oscillates, never drains)
                    victoriametrics-cluster-exporter  2.52M – 2.98M

guaranteed rate     11:06–12:36  0.0
(both exporters,    13:06–18:36  12–33 msg/s   ← working, ~6h
 in lockstep)       19:06–23:06  0.0           ← stalled, 4h+

exporters           no resource limits; RSS ~407Mi / ~428Mi; CPU 124m / 141m; uptime 10h
vmstorage           67 GB data + 42.7 GB index; 3.39M series; RSS 5.8/12 GiB;
                    tsid cache miss 1.7%; 68 TB free; data on appdata CephFS (HDD tier)
failing batches     ~102,000 datapoints per request (the 60s downsample flush)

FROZEN OFFSETS (otlp_metrics) — committed offset, hourly:
  victoriametrics-exporter          15:11 17,359,834 → 19:11 17,586,452 (+~57k/h) → 20:11..23:11 +0
  victoriametrics-cluster-exporter  15:11 18,632,054 → 19:11 19,018,306 (+~99k/h) → 20:11..23:11 +0
                                    ...pinned at 19,021,494 = the offset in its error log

RETENTION HEADROOM (otlp_metrics: floor 16,702,704, head 21,591,251 ⇒ window ~4.89M msgs)
  victoriametrics-exporter            883,748 msgs above floor  ⇒  ~44.6 h until data loss
  victoriametrics-cluster-exporter  2,318,790 msgs above floor  ⇒  ~117 h until data loss
```

Key ratio: best-effort consumption (5.8) **exactly equals** production (5.8) — it keeps up by
dropping. Guaranteed peaks at 12–33 msg/s, which *is* above production, so it can drain the
backlog **while it runs**; it just doesn't run most of the time.

## 6. Findings and open hypotheses

**Established:**
- The stall is total (0.0 msg/s), not gradual degradation.
- Both exporters stall and recover **in lockstep** despite writing to *different* backends
  (`victoriametrics:8428` vs `vminsert:8480`) ⇒ a **shared dependency**, not a per-process leak.
- The write path is timing out:
  `Post "http://vminsert:8480/insert/0/opentelemetry/v1/metrics": context deadline exceeded
  (Client.Timeout exceeded while awaiting headers)`, `rejected_items: 102633`.
- The VM backends' own `/metrics` endpoints were *also* timing out during the stall ⇒ the
  backends themselves are stalled, not just the write handler.
- Kafka **reads** are healthy (best-effort consumes at full production rate from the same
  topics/brokers), so the shared dependency is on the **write/backend** side. Both VM stores
  live on the same appdata CephFS.
- A restart does **not** reset lag (offsets are committed in Kafka), so whatever a restart
  fixes, it is throughput — not position.

**CONFIRMED (restart experiment, 2026-07-30 11:37): the consumer WEDGES — it is not a poison
message.** An earlier revision of this doc concluded the pipeline was pinned on an
un-writable message at offset `19,021,494`. **That was wrong.** A `kubectl rollout restart`
of both exporters resumed consumption *immediately*, and the very message at that offset
wrote without incident:

```
             offset delta since restart (otlp_metrics)
  t+1m   vm-exporter   +305     cluster-exporter  +1,831
  t+6m   vm-exporter +5,920     cluster-exporter +11,644
  ⇒      ~20 msg/s              ~34 msg/s           (production is 5.8 msg/s)
```

So the corrected mechanism is: **a single transient write timeout permanently wedges the
guaranteed pipeline until the process is restarted.** The receiver cannot skip (`on_error:
false`), has no retry loop (`retry_on_failure: false`) and no buffer (`sending_queue: false`),
so after the failed export it simply stops making progress on that partition — silently. That
matches every observation: the offset freezes *exactly* (not a retry loop), the error logs go
quiet after the initial failures rather than repeating, and a restart fixes it every time.

**Why the backlog never drains, despite ~5x headroom.** Per wedge/restart cycle: ~6h working
at ~14 msg/s *net* drain ≈ 300k messages recovered, then ~12h wedged accumulating
5.8 msg/s ≈ 250k. Net ≈ +50k per cycle — which is exactly why §5's 24h lag band oscillates
(3.92M–4.20M) instead of trending down. **The pipeline is not capacity-limited; it is
availability-limited.**

### What the wedge is *not* (measured across the 16h wedge, 2026-07-29/30)

| hypothesis | metric | verdict |
| --- | --- | --- |
| Requests pile up / block against vminsert | `vm_concurrent_insert_limit_reached_total` = **0**; `vm_concurrent_insert_current` max **2–3** of capacity **40** | **ruled out** — limiter never engaged |
| vminsert↔vmstorage connectivity | `rate(vm_rpc_dial_errors_total)` = **0/s** all 24h (the 298 cumulative are stale) | **ruled out** |
| Exporter stuck holding open HTTP requests | `otelcol_exporter_in_flight_requests` = **0** for the whole wedge (it is 1 when healthy) | **ruled out** — it wasn't even attempting requests |
| Consumer kicked from the group / rebalance | `kafka.consumer_group.members` = **2**, constant, throughout | **ruled out** — it held its partitions the entire time |
| CPU throttling / memory growth | no `resources:` set; RSS flat ~410 MiB | **ruled out** |

Together these say: the backend was **not saturated** (2–3 concurrent inserts of 40), and the
collector was **not blocked in the HTTP layer** — it simply stopped consuming while still
holding its partitions. The wedge is **inside the collector, upstream of the exporter**,
almost certainly in the kafka receiver's error path when `message_marking.on_error: false`
(it can neither skip nor retry, and apparently stops polling that partition).

The write timeouts that *trigger* it are a **latency** problem, not a concurrency one: only
2–3 inserts are ever in flight precisely *because* each one is slow — a ~102k-datapoint OTLP
POST against 110 GB of vmstorage on the HDD-tier appdata CephFS. The VM backends' own
`/metrics` scrapes were timing out in the same window, which is the tell.

### The mechanism, verified in source (`kafkareceiver` v0.157.0, `consumer_franz.go`)

**One export timeout pauses the partition indefinitely.** The exact path for our config
(`message_marking: {after: true, on_error: false}`, `error_backoff` unset, timeout ⇒
*non*-permanent error):

1. `handleMessage` → `shouldMark = (!isPermanent && OnError) || (isPermanent && OnPermanentError)`
   = `false`; then `if After && !shouldMark { return err }` — the error is returned (**not**
   marked, **not** skipped).
2. Caller: `if !shouldMark { fatalRecord = msg; break }` — stops processing the batch.
3. Then the decisive switch:
   ```go
   case c.config.ErrorBackOff.Enabled && !fatalIsPermanent:   // rewind + retry next poll
   case fatalIsPermanent:                                     // PauseFetchPartitions
   default:                                                   // ← WE LAND HERE
       c.client.PauseFetchPartitions(tp)
   ```
   with the source comment: *"Permanent errors and partitions without backoff configured are
   **paused until a rebalance** triggers assigned(), which calls ResumeFetchPartitions"* — and
   *"PauseFetchPartitions persists across rebalances in franz-go"*.

That is the wedge, and it explains every observation: offset frozen exactly, logs silent
(nothing is being polled), `in_flight_requests = 0` (no writes attempted), `members = 2`
(still in the group — only *fetching* is paused), and a restart fixing it every time (new
client → `assigned()` → `ResumeFetchPartitions`).

**Why the best-effort pipeline never wedges:** `on_error: true` ⇒ `shouldMark = true` ⇒
`fatalRecord` is never set ⇒ `PauseFetchPartitions` is never called. It logs *"skipping due to
message_marking config"* and consumes on. The asymmetry is entirely this branch.

Note also the source comment that classic consumer groups do **not** redeliver un-marked
messages (that would need KIP-932 share groups) — so "don't commit" alone does not produce a
retry. Something must rewind the cursor, and only `error_backoff` does.

**Still open:** why both exporters wedge in the same window — different backends, same hour ⇒
a shared trigger (Ceph, or the Swarm host `10.20.2.10`). Next wedge: overlay query #4 with
vmstorage `vm_slow_row_inserts_total` rate and Ceph health.
(An earlier "413 too large" reading was a **false positive** — grep matched digits in a
timestamp. There is no 413.)

⏰ **Deadline while wedged:** the retention floor advances ~5.5 msg/s.
`victoriametrics-exporter` had **~45 h** of headroom when measured at 4M lag. Re-run the §5
headroom calculation whenever it has been wedged for a while.

> **Method note.** The "poison message" error was mine: I inferred it from a frozen offset
> plus a matching offset in the logs, and stated it as confirmed on circumstantial evidence.
> The user's repeated observation ("a restart always fixes it") was the stronger evidence and
> falsified it in one experiment. When a cheap experiment can discriminate two hypotheses,
> run it before writing a conclusion down.

What is *not* the cause: CPU throttling (no limits set), exporter memory growth (~410 MiB
steady, no limits), Kafka read-side problems, and vmstorage cache thrash (1.7% miss ratio).

## 7. Remediation (1 applied, rest untested)

Roughly in order of effort/benefit. **None applied yet** — deliberately, so the measurement
isn't confounded.

0. **Immediate unblock: `kubectl rollout restart` both exporter Deployments.** Verified to
   work every time (§6). No offset surgery and no Swarm access needed — the wedge is process
   state, so a fresh pod resumes from the committed offset and re-sends the same message
   successfully. This is the stop-gap; it must be repeated every few hours, which is why 1–3
   matter.
1. ✅ **APPLIED (commit `afbf014`) — `error_backoff.enabled: true` on the kafka receivers**
   (`kafka/metrics`, `kafka/meta_metrics`) in each collector. This is a receiver-side option,
   so **Kafka stays the retry mechanism** — exactly the intended design. It switches the
   switch-case in §6 from `default: PauseFetchPartitions` to
   `case ErrorBackOff.Enabled && !fatalIsPermanent:`, which retries in-process with backoff
   and then **rewinds the fetch cursor via `SetOffsets`** so the record is re-polled
   (*"rewinding partition to retry failed record on next poll"*). The partition is never
   paused, and the offset is still only committed after a successful write — at-least-once is
   preserved.

   ```yaml
   receivers:
     kafka/metrics:            # and kafka/meta_metrics — the GUARANTEED ones only
       # ... existing config ...
       error_backoff:
         enabled: true         # defaults: 5s initial, 30s max interval, 5m max_elapsed_time
         initial_interval: 5s
         max_interval: 1m
         # max_elapsed_time: 0 would retry in-process forever; leaving it at the 5m default
         # is better — it falls through to the cursor rewind, i.e. re-reads from Kafka.
   ```
   Applied to the `*_latest` receivers too, with a deliberately shorter profile
   (`1s`/`10s`/`30s`). They mark on error so they can *never* hit the pause path — backoff
   there is not about wedging, it converts transient failures into successful writes instead
   of drops. Kept short so that pipeline stays CURRENT rather than falling behind on a slow
   backend. The dormant recovery exporter gets the long profile: it is a backfill, so
   completeness beats freshness.

2. **What NOT to do (both were considered and rejected):**
   - **`sending_queue: enabled: true` — would lose data.** The queue ACKs on *enqueue*, so the
     receiver commits the Kafka offset *before* the write lands; a crash with a non-empty
     queue loses those messages. It silently converts the guaranteed pipeline into a second
     best-effort one. (The collector's error text suggests it; ignore that half.)
   - **`retry_on_failure` alone — only delays the wedge.** It retries inside the *exporter*;
     when it finally exhausts, the receiver still gets an error, still sets `fatalRecord`, and
     still hits `default: PauseFetchPartitions`. It lowers the wedge frequency without
     removing it. Harmless to add alongside `error_backoff`, but it is not the fix.

   **Design invariant: Kafka is the retry mechanism, not the exporter.** `on_error: false` +
   uncommitted offsets *is* the durability model; anything that ACKs before the write lands
   breaks it.
3. **Bound the request size.** ~102k-datapoint POSTs are what time out. Add a `batch`
   processor (e.g. `send_batch_max_size`) so one slow flush can't blow the timeout.
4. **Set `timeout:` explicitly** on the `otlphttp` exporters (currently commented out, so the
   default applies) — a larger timeout trades latency for far fewer hard failures.
5. **Fix the backend I/O — the underlying trigger.** vmstorage's 110 GB of data+index sits on the
   HDD-tier appdata CephFS. Moving it to `ceph-rbd` would address H1 at source. Big job
   (110 GB copy, and it's the in-place Swarm dir).
6. **Question whether the guaranteed pipeline should exist in this form.** It is ~4M messages
   behind on a topic whose retention may well be shorter than the time needed to drain it
   (check query #5). If the backlog can never be replayed within retention, the guarantee is
   nominal and the honest options are: fix the backend first, or accept best-effort and drop
   the guaranteed groups.

## 8. Gotchas

- **Never run `{__name__=~".+"}`** against this cluster. It matches >644,245 series, trips
  `-search.maxUniqueTimeseries`, and pins vmstorage for ~30s — during which *unrelated*
  queries fail with `search_v7 ... i/o timeout` and vmstorage logs `broken pipe`. This looks
  exactly like "reads are broken" but is self-inflicted. Always use narrow selectors.
- Dotted metric names need `{__name__="kafka.consumer_group.lag_sum"}`; `kafka.consumer_group.lag_sum{...}`
  is a PromQL syntax error.
- Metadata queries (`/series`, `/label/<l>/values`) are fast even when data queries are slow —
  use them to check *existence*/freshness cheaply.
- `vmselect` enforces a 30s server-side search budget (`-search.maxQueryDuration`); long
  range queries over 24h+ at fine steps will hit it. Use `step=1800` for day-scale views.
- `kubectl top` needs no port-forward and is the quickest check of exporter RSS/CPU.
