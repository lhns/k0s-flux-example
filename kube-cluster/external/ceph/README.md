# ceph

The Ceph cluster (cephadm, outside Kubernetes) runs its own OpenTelemetry Collector on each host,
under cephadm, exporting logs and metrics to the gateway
([ADR 0056](../../../docs/adr/0056-external-group-for-systems-outside-the-cluster.md)). The spec
is `cephadm/otel-collector.yaml` (two services). This Flux component holds only the Ceph dashboards
and alert rules, as Grafana Operator objects in `grafana.yaml`.
The cluster collector stays generic, see
[infra/opentelemetry-monitoring](../../infra/opentelemetry-monitoring/README.md).

| job | port | interval | what | scraped on |
| --- | --- | --- | --- | --- |
| `ceph-exporter` | 9926 https | 30s | per-daemon perf counters (`ceph_osd_stat_bytes`, op latencies, ...) | every host, its own exporter |
| `ceph-mgr` | 9283 https | 15s | the mgr prometheus module: health, pools, PGs, `*_metadata` | every host with the `mgr` label, by the `otelcol-mgr` service |

Series carry `namespace=ceph`, `service` (the job), and `cluster` (the fsid, for ceph-mixin's
`$cluster`). ceph-exporter series also carry `hostname`, and `service.instance.id` is the host. The
mgr series have `service.instance.id=ceph_cluster` and no `hostname`: they describe the cluster,
whichever mgr is active, and their `*_metadata` series carry their own `hostname`. A scraped
`instance` label (`ceph_disk_occupation`) arrives as `exported_instance`. The metrics pipeline has
no resource detection on purpose: `host.name` would change the series' identity.

## cephadm collector

`cephadm/otel-collector.yaml` is a cephadm service spec (`service_type: container`), not a
Kubernetes object: Flux never builds it, the owner applies it by hand. It holds two services:
`otelcol` runs on every Ceph host (`placement: '*'`) with the logs and the ceph-exporter scrape;
`otelcol-mgr` runs on the hosts with the `mgr` label and only scrapes the local mgr (a second
service rather than a hostname filter, so a new host needs no edit here either). Both ship to the
gateway. The one cluster-specific value is `CEPH_FSID` (the `cluster` label, ceph-mixin's
`$cluster`; cephadm sets no fsid env in the container), in both services. The logs get their
`ceph.fsid` from the directory name. The collector lives on the hosts so that Ceph stays self-contained: its logs and
exporters are read locally, with no cluster-side targets to keep in step with `ceph orch host`.

Logs (service `ceph`, one VictoriaLogs stream per host and daemon):

- Daemon logs `ceph-*.log` as `ceph.daemon` (`osd.24`, `mon.ceph-a`, ...), with Ceph's level
  mapped to severity. Python tracebacks and RocksDB dumps arrive as one record.
- The cluster log (`ceph.log`, `ceph.audit.log`, `ceph.cephadm.log`) as `ceph.channel`. Every mon
  writes it in full, so each line arrives once per mon host; tell the copies apart by `host.name`.
- Metrics: the two jobs above, scraped on localhost.
- Not shipped: `ceph-volume*.log`, `ceph.pg_autoscaler.log`, debug levels and known chatter
  (`filter/ceph-noise`). Everything stays on disk and in cephadm's Loki.

### Applying a change

cephadm never acts on a changed spec by itself: `apply` only stores it. Apply **and** redeploy
after every edit, including a Renovate image bump (which is why that file is never automerged).
A config error fails the unit five times and parks it for 30 minutes, so validate first, on any
machine with `otelcol-contrib` of the spec's version:

```sh
# extract `files: etc/config.yaml` of each document to its own file, then, for both
NODE_NAME=ceph-a CEPH_FSID=<fsid> otelcol-contrib validate --config=config.yaml
ceph orch apply -i kube-cluster/external/ceph/cephadm/otel-collector.yaml --dry-run
ceph orch apply -i kube-cluster/external/ceph/cephadm/otel-collector.yaml     # both documents
ceph orch redeploy container.otelcol
ceph orch redeploy container.otelcol-mgr
ceph orch ps --daemon-type container          # one otelcol per host, one otelcol-mgr per mgr host
```

Drift check: `ceph orch ls container --export` against the file. Logs of a daemon:
`journalctl -u ceph-<fsid>@container.otelcol.<host>`. Rollback: `ceph orch rm container.otelcol`
and `ceph orch rm container.otelcol-mgr` (the first deletes the checkpoints; the next start reads
from the end, no backfill).

Traps:

- `uid`/`gid: 167` is required: cephadm chowns `/var/lib/ceph/<fsid>` and `crash/` to it on every
  deploy, and any other owner breaks crash dumps on that host. The process user is the separate
  `--user=167:167`, which also reads the 0600 cluster logs.
- Custom containers get no host network; `--net=host` comes from `extra_container_args`.
- Never relabel `/var/log/ceph` (`:z`/`:Z`): it is shared with every Ceph daemon.
- Self-telemetry is on `127.0.0.1:8889` (`otelcol`) and `8890` (`otelcol-mgr`); 8888 belongs to the Ansible collector on the OSD hosts.
- A dead collector emits no `up=0`, its series just end: `ceph-exporter-down` covers that, and
  `CEPHADM_FAILED_DAEMON` is in the health-check alert. A host removed from Ceph fires for up to a day.
- Keep the image tag equal to the cluster collectors': `scope.version` is a label on every series.
- `ceph orch upgrade` does not touch the image: bump it by editing, applying and redeploying.

## Adding a host

Nothing to edit: `otelcol` places on every host and `otelcol-mgr` on the `mgr` label. Check
`ceph orch ps --daemon-type container` lists it.

## Dashboards

Nine Grafana Operator `GrafanaDashboard`s in folder *Ceph* (`grafana.yaml`, JSON in
`dashboards/`) for the cephadm cluster, from the exporter and mgr series its collector
scrapes (above). They derive from ceph-mixin v20.2.4
(`monitoring/ceph-mixin/dashboards_out`), adapted to this pipeline: the `ds` variable, no `job` or
`instance` matchers, the OSD device panels read the hostmetrics `system.disk.*` instead of node_exporter,
and the Prometheus-alert panels read `ceph_health_detail` and the Grafana alerts instead. Every panel
expression was run against the live series; the dashboards link to each other and to *Servers*.

- **Ceph cluster** (`/d/ceph-cluster`): health, capacity, throughput, *Active health checks*, OSD
  fullness against the nearfull and full ratios, PG states, latency, versions.
- **Ceph OSDs** (`/d/ceph-osds`): op latency (average, max, p95, slowest ten), PGs per OSD, sizes,
  BlueStore onode hits, slow ops.
- **Ceph OSD device** (`/d/ceph-osd-device`): one OSD's ops, and the I/O of the device behind it.
- **Ceph pools** (`/d/ceph-pools`) and **Ceph pool** (`/d/ceph-pool`): capacity, compression, growth, time to
  full, client IOPS and bandwidth.
- **Ceph CephFS** (`/d/ceph-cephfs`): per MDS, sessions, request rate and latency, slow replies, caps,
  inodes and strays, memory, journal. The last eight panels are not from the mixin.
- **Ceph RGW** (`/d/ceph-rgw`) and **Ceph RGW instance** (`/d/ceph-rgw-detail`): latency, requests and
  bandwidth per gateway.
- **Ceph storage** (`/d/ceph-storage-owners`, provisioned from `apps/storage-usage`): what uses the
  space. Its first two rows are built from the mgr metrics alone (raw capacity by device class,
  pools with growth and time to full, filesystems and their pools, MDS strays) and work without the
  exporter; the rows below are per owner and come from the
  [storage-usage exporter](../../apps/storage-usage/README.md). Not from the mixin.

#### Runbook

| Symptom | Look at | Fix |
| --- | --- | --- |
| Health not OK | *Active health checks* | `ceph health detail`; `AUTH_INSECURE_*` are known and accepted |
| Writes fail or hang, an OSD near full | *OSD fullness*, *Fullest OSDs*, *Ceph pools* | Free space or add OSDs, `ceph osd reweight-by-utilization`; raising the ratios only buys time |
| Pool nearly full | *Ceph pool*: *Capacity used*, *Time till full*; pools sharing a device class share `max_avail` | As above, for that device class |
| Slow I/O | *Ceph OSDs*: *Highest READ/WRITE Latencies*, *Top Slow Ops*, then *Ceph OSD device* | Device latency or util high: failing disk (`smartctl`), replace the OSD; device fine: PG balance, CPU |
| PGs inactive or degraded | *PGs State*, *Stuck PGs* | `ceph pg dump_stuck`; an OSD or host down: bring it back (`ceph osd tree`) |
| Recovery or backfill slow | *Recovery Operations*, *Cluster Throughput* | Let it finish; a `noout` or `norebalance` flag set during maintenance is in `OSDMAP_FLAGS` |
| CephFS hangs or lags | *Ceph CephFS*: *Reply latency*, *Slow replies*, *Capabilities*, *Client sessions* | Metadata pool's OSDs; `MDS_CLIENT_RECALL`: lower `mds_max_caps_per_client` or restart the client |
| S3 errors or latency | *Ceph RGW*, *Ceph RGW instance* | RGW log, the `default.rgw.*` pools |
| Monitor out of quorum | *Monitors* | Its host, clock sync, disk space |
| Alerts silent | `ceph-metrics-stale`, `ceph-exporter-down` | `ceph mgr stat`; `ceph orch ps --daemon-type container` (the collector on the Ceph hosts) |

#### Limits of the data

- Ceph metrics have no node_exporter: the host panels live in *Servers* (`host.name` is the Ceph host), and
  the OSD device panels join `ceph_disk_occupation_human` (the OSD's LVM device `dm-N`) to `system.disk.*`.
- Counters of the monitors, the main MDSs and RGW come from the mgr module (`exclude_perf_counters false`,
  see [Decisions](#decisions)); the exporter skips the mgr hosts.
- No HAProxy, RGW bucket-notification, RBD or `ceph_unclean_pgs` series: those mixin panels are removed.
  The mixin's `ceph_rgw_get` and `ceph_rgw_put` are not exported by Ceph 20; the `op_get_obj_ops` and
  `op_put_obj_ops` counters stand in. `backfillfull_ratio` is not exported.
- Empty by nature: *Objectstore Types* (no filestore OSD), size classes no OSD falls in, GET latency while
  nothing reads through RGW.

## Alerts

`grafana.yaml`: rule group `ext-ceph`, folder *Ceph*. Added before there was 7 days of history (the
Ceph series are hours old), so they were backtested over what exists.

| Rule | Fires when | For | Severity |
| --- | --- | --- | --- |
| `ceph-health-check` | a curated `ceph_health_detail` check is raised (OSD, pool, PG, MON, MDS and device checks, `RECENT_CRASH`); one alert per check. `PG_DEGRADED` is left out: recovery after a reboot is normal and `PG_AVAILABILITY` covers the dangerous case | 5m | warning |
| `ceph-health-slow` | `SLOW_OPS`, `MDS_SLOW_REQUEST` or `MDS_SLOW_METADATA_IO` is raised; one alert per check. Split off because they come and go under load | 15m | warning |
| `ceph-health-error-other` | any other check of error severity, except `AUTH_INSECURE_*` (known, accepted for now) | 15m | warning |
| `ceph-metrics-stale` | no `ceph_health_status` sample for 5m (also on NoData); emailed | 2m | critical |
| `ceph-exporter-down` | `up` is 0 for a ceph-exporter, per host (also on NoData) | 10m | warning |
| `ceph-osd-time-to-full` | an OSD over 80% and below the nearfull ratio reaches the full ratio within 7d (24h trend); `OSD_NEARFULL` takes over from there | 1h | warning |
| `ceph-pool-time-to-full` | a pool over 1 GB and below 85% used reaches `max_avail` 0 within 7d (24h trend); `ceph-pool-above-85`/`-92` take over from there | 1h | warning |
| `ceph-pool-above-85` / `-92` | `ceph_pool_percent_used` 85-92% / > 92%, per pool | 1h | warning / critical |
| `ceph-pg-inactive` | a pool has PGs that are not active | 5m | critical |

Rule group `ext-ceph-opencas` (same folder), from the host script `otelcol-opencas.sh` in the Ansible repo
(`opencas.cache_running`, `opencas.cache.*`, `opencas.core.*`); runbook (not published):
"Runbook: cache errors". A write error clears a cache's running state and its cleaner stops for good.

| Rule | Fires when | For | Severity |
| --- | --- | --- | --- |
| `opencas-cache-not-running` | `opencas.cache_running` is 0 | 2m | critical |
| `opencas-cache-errors` | cache read or write errors increased over 15m | 0 | critical |
| `opencas-core-errors` | core (HDD) read or write errors increased over 1h, per core and device | 0 | warning |
| `opencas-metrics-absent` | a cache seen in the last day sent no `opencas.cache_running` | 10m | warning |
| `opencas-ioclass-core-class` | `opencas.ioclass_core_class_ok` is 0 (core in another io class than its role needs) | 15m | warning |
| `opencas-ioclass-check-error` | `opencas.ioclass_check_error` is 1 | 0 | warning |
| `opencas-ioclass-differences` | `opencas.ioclass_check_diffs` > 0 or `opencas.ioclass_file_drift` is 1 | 1h | warning |
| `opencas-ioclass-check-stale` | `opencas.ioclass_check_timestamp_seconds` older than 2 days or absent on a host with caches (the check runs daily from Semaphore) | 0 | warning |

A stalled-cleaner rule waits for a week of `opencas.cache.dirty_blocks` to set its threshold. `opencas-metrics-absent`
also covers a cache that vanished (NVMe missing, not loaded at boot); it fires for up to a day after a cache is removed on purpose.

Both health rules keep firing for 30m after a check clears (`keepFiringFor`), so a check that blinks does not
resolve and re-fire. The warning rules here use `group_interval: 1h` instead of the cluster's 10m: a group whose
checks change sends at most one update an hour. The critical ones keep 10m.

`ceph-health-check` carries Ceph's own severity in `check_severity` and the rule keeps a fixed
`severity: warning`. The health series appear only for checks in the mgr's built-in list or once raised, so
`FS_WITH_FAILED_MDS`, `DEVICE_HEALTH*` and `MGR_DOWN` are matched by name without ever having been seen.

Backtests, over the 2 hours or so of Ceph history:

| Rule | Result | Decision |
| --- | --- | --- |
| `ceph-health-check` | nothing: no curated check was raised | as specified |
| `ceph-health-error-other` | `MGR_MODULE_ERROR` for the whole window: real, a mgr module fails | as specified |
| `ceph-metrics-stale`, `ceph-exporter-down`, `ceph-pg-inactive` | nothing | as specified |
| `ceph-osd-time-to-full` | eight OSDs crossed, two of them for over an hour: the fastest-growing ones (about 2.5% a day) while the fullest are at 94% of a 96.8% full ratio; the trend is noisy over this little history | as specified; expect noise until a day of history exists. Guard added: 14 days of state history, 55 of 115 firings began at or above nearfull (95%); 61 ten-minute buckets with a firing become 43 |
| `ceph-pool-time-to-full` | the seven pools sharing the nvme `max_avail` crossed together for 35m (a growth burst) | as specified; they fire together by design, one per pool. Guard added: 14 days of state history, 10 of 58 firings began on a pool at or above 85% (`cephfs.archive.data`, `cephfs.appdata.data`), none of them alone in its ten-minute bucket |
| `ceph-pool-above-85` / `-92` (7 days, 2026-11-14 to 11-21) | `cephfs.archive.data` over 92% throughout, one interval (93.6-99.97%); `cephfs.appdata.data` 85-92% most of the week and over 92% three times for 1h40-11h; `cephfs.userdata.data` over 92% once for 5h on 11-20; `default.rgw.buckets.data` and `rbd.vms` crossed both levels on 11-20 for under an hour each (a `max_avail` dip), no alert | as specified; the archive pool fires from the start |

## Decisions

- **The collector runs on the hosts, as a cephadm service.** Ceph stays self-contained and a new
  host gets one with no repo edit; scraping from the cluster needed static targets or cephadm
  credentials. cephadm does not reconcile a changed spec, so the file and the live cluster can
  drift: compare `ceph orch ls container --export`.
- **The cluster log is shipped from every mon.** Each mon writes it in full; shipping it from all
  of them needs no host filter and survives a mon being down. Copies are told apart by `host.name`.
- **Every `mgr`-labelled host is scraped** (the `otelcol-mgr` service). Only the active mgr serves HTTPS on 9283; the standby answers
  plain HTTP with an empty body, so its scrape fails by design and failover needs no change here.
  The two scrapes share one identity, so the collector drops their `up`/`scrape_*`, which would
  otherwise interleave 1 and 0. Nothing else comes from a failed scrape. A dead mgr scrape shows
  as `ceph_health_status` going stale.
- **Per-daemon counters come from the exporters where they cover the daemon.** ceph-exporter skips
  every admin socket whose name contains `mgr`, which on the `ceph-[abc]` hosts is all of them, so
  mons, the main MDS and RGW get counters only from the mgr. That needs
  `ceph config set mgr mgr/prometheus/exclude_perf_counters false`, set on Ceph, not in git; revert
  with `ceph config rm` once ceph-exporter matches the daemon type. The `ceph-mgr` job mirrors that
  rule without naming hosts: it keeps counters whose `ceph_daemon` contains `mgr` and drops the
  other OSD and MDS ones, keeping state series such as `ceph_osd_up`: the exporters' copies are per host, carry `hostname` and don't load the mgr.
  The mgr labels RGW counters with `instance_id`, not `ceph_daemon`; join on `ceph_rgw_metadata`.
  cephadm's own Prometheus has no such filter and stores OSD counters twice.
- **TLS is not verified.** cephadm's certificates are self-signed and name the host IP, not loopback.
- **Noisy families are dropped** at the collector: `AsyncMessenger_Worker`, `bluestore_pricache`,
  `prioritycache` and `mds_client_metrics` (one series set per CephFS client session). The cephadm
  Prometheus keeps the full set.
