# victoriametrics-cluster

VictoriaMetrics cluster fed from Kafka (`otlp_metrics`, `otlp_meta_metrics`) by the OTel
collector `victoriametrics-cluster-exporter`. Grafana and the vmui query `vmselect`.

## Long-term tier

```
Kafka ─┬─ group victoriametrics-cluster-exporter(-latest) ─▶ vminsert          ─▶ vmstorage           24M, full resolution
       └─ group victoriametrics-cluster-exporter-longterm ─▶ vminsert-longterm ─▶ vmstorage-longterm  5y, 1m
                                                    vmselect ◀── both (rollout step 5)
```

[`longterm.yaml`](longterm.yaml) holds the second store. Both tiers get the same writes
independently; nothing copies data from one to the other after the initial seed.

**Dedup is the downsampling.** OSS VictoriaMetrics has no `-downsampling.period`
(Enterprise); `-dedup.minScrapeInterval=D` is documented as its `0s:D` equivalent. vmstorage
keeps the sample with the biggest timestamp per series per `D` bucket, during background merges
of the current month and, for older months, in a one-off "final dedup" force-merge of every
partition whose parts were written with a smaller interval (`lib/storage/table.go`,
`partition.isFinalDedupNeeded`). So seeded full-resolution history gets thinned on its own.

**1m, and only ever raised.** Raising it later re-thins all partitions the same way; lowering it
cannot bring back dropped samples.

**All metrics, no filter.** Same series as the full tier, so any dashboard works on either.
Known cost: dedup shrinks rows, not the index. The full tier creates about 3M new series a day
(`process.*` alone is one series per pid); its indexdb is already ~126G next to ~362G of data.
The long-term indexdb grows at the same rate for 5 years. Fix that at the source if it hurts.

### Querying

One vmselect over both stores. Wherever both hold a range, the long-term samples are a subset of
the full-resolution ones (same timestamp, same value), and vmselect's `-dedup.minScrapeInterval=1ms`
drops them during the per-series merge (`app/vmselect/netstorage/netstorage.go`,
`mergeSortBlocks`: equal samples are skipped only if dedup is enabled). Nothing is summed.
vmselect stays at `1ms`, deliberately not matching `vmstorage-longterm`'s `1m`, which would
thin the full tier too.

Gaps: for range queries MetricsQL estimates each series' interval as the 0.6 quantile of its
**last 20** intervals in the fetched range, adds a jitter margin (15s becomes 22.5s, 60s becomes
67.5s) and uses that as the lookbehind when the query gives none, and to find the previous
sample for `rate`/`increase` (`app/vmselect/promql/rollup.go`, `getScrapeInterval`,
`getMaxPrevInterval`). A range entirely in thinned data therefore shows no gaps. A range that
crosses the full-res/thinned boundary (exists only after step 6) is judged by its dense recent
end, so with a step under ~1m the thinned part draws dotted or empty. `-search.minStalenessInterval`
is a floor on that value (flag help: "removing gaps on graphs generated from time series with
irregular intervals"); `-search.maxStalenessInterval` (= `-search.maxLookback`, default auto) is a
ceiling and must stay unset, since anything under ~70s cuts thinned data into dots.
Set `-search.minStalenessInterval=2m` with step 5: 1m is too tight for jittered 1m samples.
Costs: real gaps under 2m are bridged on the full tier, and the look-back before a query's start
shrinks from the default 5m to 2m (`maxSilenceInterval` in `promql/eval.go`).

Grafana: use `rate(x[$__rate_interval])`. It is at least 4x the datasource's scrape interval
(1m at the default 15s); a hard-coded window under 1m (`rate(x[30s])`) is gappy on thinned data.

### Rollout

Each step is its own change; stop after any of them.

1. **Done.** vmstorage-longterm and vminsert-longterm run.
2. **Done.** (Was: create a static `rootPath`; the store now uses dynamic volumes.)
3. **Done: seeded with every month vmstorage held.** Each month is self-contained in v1.153.0
   (`data/small/YYYY_MM`, `data/big/YYYY_MM`, `data/indexdb/YYYY_MM`). To repeat it:
   - `curl http://vmstorage:8482/snapshot/create`, note the name.
   - Scale `vmstorage-longterm` to 0 (in git, or `kustomize.toolkit.fluxcd.io/reconcile: disabled`).
   - Helper pod (`victoriametrics/vmstorage` image, it has `sha256sum`) mounting both stores
     the way their vmstorage does: `vmstorage-data` at `/src` with `vmstorage-hot-rbd`
     (`subPath: small`) at `/src/data/small`, both `readOnly`, `vmstorage-longterm-cold` at
     `/dst` with `vmstorage-longterm-hot-rbd` (`subPath: small`) at `/dst/data/small`. The RBD
     volumes are RWO: pin the pod to vmstorage's node (`nodeName`), which is still running.
   - Copy whole months; the target month dirs must not exist. A running store, even an empty
     one, has created the current month's: remove those only if they hold no parts.
     ```
     cp -a /src/data/small/snapshots/<name>/<month>   /dst/data/small/<month>  # has parts.json
     cp -a /src/data/big/snapshots/<name>/<month>     /dst/data/big/<month>
     cp -a /src/data/indexdb/snapshots/<name>/<month> /dst/data/indexdb/<month>
     ```
     That `parts.json` lists the month's big parts too: never copy small without big. Upstream
     warns that `cp`/`rsync` of a snapshot may miss data; compare per-file `sha256sum` and the
     `find` tree per month, and the summed `RowsCount` of `<month>/*/metadata.json` (small and
     big) with the `partsCount`/`rowsCount` of the next `successfully opened storage` line.
     Several months in parallel reached ~80 MB/s on HDD CephFS; one stream ~50 MB/s.
   - Delete the pod, start vmstorage-longterm. The final dedup of each past month starts 1h to
     1h15 after start (`-storage.finalDedupScheduleCheckInterval`) and takes minutes per month.
   - `curl 'http://vmstorage:8482/snapshot/delete?snapshot=<name>'`: the snapshot pins every
     part it references, so it grows while it lives.

   The documented route is `vmbackup` (`-snapshotName=<name>`, `-dst=fs:///...`) then
   `vmrestore` into `vmstorage-longterm-cold` with vmstorage-longterm stopped; it copies whole
   stores, not single months.
4. **Done: the feed is on**, the `metrics/longterm` pipeline in
   [`victoriametrics-cluster-exporter.yaml`](victoriametrics-cluster-exporter.yaml). Its consumer
   group started at `earliest`; `otlp_metrics` and `otlp_meta_metrics` keep `retention.ms:
   864000000` (10 days), so the replay overlapped the snapshot and dedup absorbs the overlap.
   The replay is slow: `otlp_metrics` has one partition and the exporter sends one batch at a
   time, ~1 CPU, ~40k rows/s against ~17k rows/s live, so 10 days take about a week to catch up.
   Lag: `kafka-consumer-groups.sh --describe --group victoriametrics-cluster-exporter-longterm`
   on `otel-kafka-0`; the exporter's `otelcol_kafka_receiver_offset_lag` has no group label.
5. **Done: vmselect queries both stores** ([`resources.yaml`](resources.yaml),
   `-storageNode=vmstorage:8401,vmstorage-longterm:8401`, `-search.minStalenessInterval=2m`).
   vmselect waits for the slower store, and a slow one fails the query: the per-node search
   timeout is the whole query's `-search.maxQueryDuration` (1m). A down vmstorage-longterm makes
   queries partial (`isPartial: true`). Both stores have the same disk layout, so the long-term
   one is as fast once its caches are warm; after a restart, old ranges are slow until then.
6. **After at least one month and repeated verification, lower `vmstorage`'s `-retentionPeriod`**
   (e.g. `90d`). Irreversible: vmstorage drops the older partitions.

### Verification

Against a vmselect that sees **only** the long-term store, e.g. a temporary pod:

```
kubectl -n victoriametrics-cluster run vmselect-lt --restart=Never \
  --image=victoriametrics/vmselect:<running tag> --labels=kube-vnet/net.vmstorage=egress \
  -- -storageNode=vmstorage-longterm:8401 -dedup.minScrapeInterval=1ms
kubectl -n victoriametrics-cluster port-forward pod/vmselect-lt 18482:8481
```

Query it through the port-forward: kube-vnet refuses other pods. Compare with the normal
vmselect at the same `time=` (end of a past month). Dots in names need `\.` in MetricsQL. Use
metrics that exist in that month (`vm_*` are missing in 2025_09); a `{__name__=~...}` matching
many series hits the series limit (422). On HDD, 30d windows can exceed 30s; use 1d.

- Series present: `count(last_over_time(container_cpu_usage_seconds_total[30d]))` equal on both.
- Thinning: `sum(count_over_time(vm_rows{service\.name="vmstorage"}[30d]))` about 1/4 of the
  full tier's for 15s series, after the final dedup.
- Counters: `sum(increase(vm_rows_added_to_storage_total{service\.name="vmstorage"}[30d]))`
  within ~1% on both.
- Continuity: across the snapshot / Kafka-replay boundary,
  `min_over_time(count_over_time(vm_rows{service\.name="vmstorage"}[1h])[7d:1h])` stays near 60 per series,
  never 0.

Delete the pod afterwards.

## Hot/cold storage

Both stores split `-storageDataPath` over two volumes:

| path | vmstorage | vmstorage-longterm |
| --- | --- | --- |
| `/storage` (cold): `data/big`, `data/indexdb`, `metadata`, `cache` | static PV `vmstorage-data`, CephFS `appdata` (HDD) | `vmstorage-longterm-cold`, StorageClass `cephfs-appdata` (HDD), 1Ti |
| `/storage/data/small` (hot) | `vmstorage-hot-rbd`, StorageClass `ceph-rbd` (`rbd.kube`, NVMe), 32Gi, `subPath: small` | `vmstorage-longterm-hot-rbd`, same class, 16Gi, `subPath: small` |

Hot is RBD, not CephFS: `data/small` is constant small-file creates, renames and fsyncs, each an
MDS round trip on CephFS and local to ext4 on RBD. `subPath: small` because ext4 puts
`lost+found` at the volume root, and vmstorage reads every dir in `data/small` as a partition.
RBD is RWO: nothing else may mount a hot volume while its vmstorage runs, except a pod on the
same node.

Ingest and the frequent small merges stay on NVMe. A merge writes its output straight into
`data/small` or `data/big` (`partition.getDstPartPath`); nothing is renamed or linked across dirs
later. So only the whole `data/small`, `data/big` or `data/indexdb` can be split off: month dirs are
created with `MustMkdirFailIfExist`, and a pre-made mount or symlink there panics.

- **The two volumes are one store.** `data/small/<month>/parts.json` also lists the big parts,
  and on start every big part it does not list is deleted (`mustOpenParts`). Never start vmstorage
  against a stale or foreign `small/`, and never restore one volume without the other. Without a
  `parts.json` it falls back to listing the dirs: big parts survive, small parts are gone.
- **vmstorage reads `df` as free space.** On RBD that is the volume size; on a dynamic CephFS
  volume, its quota.
  - Hot: no read-only guard (`-storage.minFreeDiskSpaceBytes` watches `-storageDataPath`
    only), and the small-part threshold is `min(memory.Remaining()/15, free small space/CPUs)`
    (`getMaxSmallPartSize`, 20 CPUs per node), so a full hot volume pushes merges to HDD early.
    Sized so memory decides: 32Gi (~573 MB at 8Gi remaining), 16Gi (~286 MB at 4Gi).
  - Cold: big merges are capped at free/4 and the final dedup needs free ≥ the partition, and a
    full volume turns vmstorage read-only. Expand (all three classes allow it) well before that.
- **Snapshots** hardlink small parts into `data/small/snapshots/` on the hot volume;
  `snapshots/<name>/data/small` is a relative symlink to it.
- **`-inmemoryDataFlushInterval`:** 1m on vmstorage-longterm, the default 5s on vmstorage.
- Restarting a vmstorage pod unmounts CephFS, which can panic its node
  ([ceph-csi-cephfs README](../../infra/ceph-csi-cephfs/README.md)). Only while Ceph is healthy.

### Stale copies of data/small

Each is a rollback for one move only, valid until vmstorage first started after it: its
`parts.json` does not list the big parts merged since, and they would be deleted. Delete them
once unwanted; delete the PVC, not the PV (AGENTS.md).

- `small.pre-nvme` on `vmstorage-data`: from the move off the cold volume.
- PVC `vmstorage-hot` (CephFS): from the move to RBD.
- PVC `vmstorage-longterm-hot` (CephFS): empty, never held data.

### Moving data/small to RBD

The longterm store was empty and simply switched over. vmstorage has live data, so it needs
downtime and a copy. Never write to `vmstorage-data` or the old `vmstorage-hot`. Ceph must be
`HEALTH_OK` before each step that restarts a pod.

1. **Add `vmstorage-hot-rbd`.** Unused; check it is `Bound`.
2. **Stop vmstorage** (`replicas: 0`). Note `vm_parts`/`vm_rows` by type and the last
   `successfully opened storage ... partsCount ... rowsCount` log line first. vminsert then
   fails, the exporter's main group stops committing offsets, and Kafka holds ~10 days
   (`retention.ms`). The `-latest` group drops, by design. Queries fail until step 4.
3. **Copy, by hand**, in a helper pod (`victoriametrics/vmstorage:<running tag>`,
   `command: [sh, -c, "sleep 1000000"]`, `runAsUser: 0`) mounting `vmstorage-data` at `/cold`
   and `vmstorage-hot` at `/src`, both `readOnly`, and `vmstorage-hot-rbd` at `/dst`:
   ```
   ls /src/snapshots /cold/data/big/snapshots   # must be empty
   count() { find "$@" -mindepth 3 -maxdepth 3 -name metadata.json -exec grep -ho '"RowsCount":[0-9]*' {} + |
     awk -F: '{n++; r+=$2} END {printf "parts %d rows %.0f\n", n, r}'; }
   count /src /cold/data/big               # = partsCount/rowsCount at the next start
   count /cold/data/big                    # = vm_parts{type="storage/big"}
   ls -A /dst                              # only lost+found
   mkdir /dst/small && cp -a /src/. /dst/small/
   sums() { (cd "$1" && find . -type f -exec sha256sum {} + | sort -k2); }
   sums /src > /tmp/a; sums /dst/small > /tmp/b; cmp /tmp/a /tmp/b
   (cd /src && find . | sort) > /tmp/c; (cd /dst/small && find . | sort) > /tmp/d; cmp /tmp/c /tmp/d
   ```
   Delete the pod before step 4: the RBD volume is RWO.
4. **Mount it with `subPath: small`, `replicas: 1`.** Check:
   - `df /storage/data/small` in the pod shows a `/dev/rbd*` device;
   - the startup line's `partsCount`/`rowsCount` equal step 3's first count;
   - `vm_parts{type="storage/big"}` equals step 3's big count and the value noted in step 2;
   - no `deleting ".../data/big/..." because it isn't listed` line in the log;
   - old months answer queries;
   - the exporter's Kafka lag drains.

**Rollback.** Before step 4 started vmstorage: revert the mount. After it: stop vmstorage and
copy `/dst/small/.` into an empty volume, verified the same way; `vmstorage-hot` is stale.

## Capacity

At the old series churn: vmstorage at 24 months ≈ 1.65 TiB logical, vmstorage-longterm at 5y
≈ 1.9 TiB, against ~4.3 TiB logical free on HDD with the Ceph HDD pools 82% used. Most of that was
indexdb from per-pid `process.*` series; the host collectors, configured outside this repo, now
aggregate them by process name, which should cut indexdb growth from ~20 GiB to ~1–1.5 GiB a month.

## Alerts

`grafana.yaml`: rule group `app-victoriametrics-cluster`, folder *Cluster*; backtested over 7 days before it was added.

| Rule | Fires when | For | Severity |
| --- | --- | --- | --- |
| `victoriametrics-component-down` | `up` is 0 for a vminsert, vmselect or vmstorage (also on NoData); emailed | 10m | warning |
| `kafka-exporter-members` | fewer than 2 consumers in a `victoriametrics-cluster-exporter*` group (also on NoData); emailed | 15m | warning |

Backtest over 7 days:

| Rule | Result | Decision |
| --- | --- | --- |
| `victoriametrics-component-down` | vmstorage: 44 brief blips, 3 of 5m or more (rollouts, the longest 19m) | `for` raised from 5m to 10m: one hit |
| `kafka-exporter-members` | one dip of under 5m per group | as specified, no hit with `for` 15m |
