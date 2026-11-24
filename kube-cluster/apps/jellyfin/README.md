# jellyfin

Jellyfin media server — the `ghcr.io/jpvenson/jellyfin.pgsql` fork, which keeps its
library database in **PostgreSQL** instead of SQLite. Migrated from Docker Swarm.

- **Public** at `jellyfin-pg.example.com` (Jellyfin's own login; no Authelia, matching
  Swarm); `jellyfin.kube.example.com` redirects there. `jellyfin-redirect-home` ports the Swarm
  `/web` → `/web/index.html` redirect.
- **Database** — a dedicated `jellyfin` role + database in the shared CNPG cluster
  (`infra/postgres`, PG 18). `database.yaml` holds the `DatabaseRole` + `Database`
  CRDs; `db-role-secret.yaml` is the SOPS-encrypted password (in the `postgres` ns,
  Reflector-mirrored into `jellyfin`). The pod dials `postgres-rw.postgres.svc:5432`
  via the `postgres` kube-vnet channel.

## Storage
The old Swarm `/mnt/{appdata,fastdata,archive}` paths are CephFS filesystems on
the same `ceph-cluster` (fs names `appdata` / `fastdata` / `archive`).

| Mount | Source | Volume | Mode |
|---|---|---|---|
| `/config` | fastdata `.../config/*` | `ceph-rbd` PVC `jellyfin-config` (copied in once) | RW |
| `/config/metadata` | appdata `.../config/metadata` | static CephFS PV (fs `appdata`, RWX) | RW |
| `/config/data/trickplay` | appdata `jellyfin/trickplay` | static CephFS PV (fs `appdata`) | RO |
| `/config/subbuzz` | appdata `jellyfin/subbuzz` | static CephFS PV (fs `appdata`) | RO |
| `/jellyfin/jellyfin-web/config.json` | fastdata `.../config/web/config.json` | ConfigMap `jellyfin-web-config` | RO |
| `/media` | archive `/media` | static CephFS PV `jellyfin-media-stream` (fs `archive`, 64 MiB readahead) | RO |

`/config` is a fresh RW block volume (the small ~3.3G fastdata subtree copied in)
so cache/writes work. The big `appdata` metadata is mounted in place, RW, rather than copied.

## One-time migration (with Swarm jellyfin stopped for a consistent snapshot)
1. Deploy with the Deployment at `replicas: 0` (CNPG creates the DB/role, Reflector
   mirrors the secret, PVCs bind).
2. **Copy** fastdata `.../config` → the `jellyfin-config` RBD PVC (one-shot Job,
   `rsync -a --exclude web`).
3. **Import** the Postgres data: `pg_dump` the old DB at `10.20.176.187:5428` and
   `pg_restore` into the CNPG `jellyfin` database (one-shot Job).
4. Flip the Deployment to `replicas: 1`.

## Client IPs and `KnownProxies` — settings that live outside git

Jellyfin's network settings are in `/config/config/network.xml` on the `jellyfin-config` PVC,
copied from Swarm during the migration above. They are **not** in this repo and Flux will never
reconcile them, so they are recorded here or they are invisible:

```xml
<KnownProxies><string>0.0.0.0/0</string></KnownProxies>
<LocalNetworkSubnets><string>10.20.0.0/16</string></LocalNetworkSubnets>
<EnableRemoteAccess>true</EnableRemoteAccess>
```

External users used to appear as `10.20.176.190`. That was not Jellyfin's doing: the cluster traefik
had no `forwardedHeaders.trustedIPs`, so it discarded the edge's `X-Forwarded-For` and replaced it
with the peer — one of the three swarm nodes running the edge traefik. Fixed in `infra/traefik`,
and Jellyfin needed no change, because `KnownProxies: 0.0.0.0/0` makes it walk the whole chain.

**That `0.0.0.0/0` should be narrowed, and matters more now than it did.** It means Jellyfin
believes whatever is leftmost in `X-Forwarded-For`. While traefik was overwriting the header that
was unreachable; now that the chain is preserved, whether a client can inject a forged entry
depends on how the *edge* traefik is configured — which is outside this repo. Narrow it to
`172.18.0.0/16` plus `10.20.176.125/.185/.190` (Dashboard → Networking → Known proxies), and check the
edge's own `forwardedHeaders` while you are there.

## Metrics: a setting outside git

Both instances serve Prometheus metrics on `:8096/metrics` because `/config/config/system.xml` on
their config volumes has `<EnableMetrics>true</EnableMetrics>` (Dashboard → General → Enable
metrics). It is not in this repo; without it the scrape (pod annotations) gets 404 and the playback
dashboard's Jellyfin tiles stay empty. `/metrics` has no authentication, so every public route
excludes it (`routing.yaml`).

## jellyfin-sqlite

The official Jellyfin on SQLite (`sqlite.yaml`), the Swarm `jellyfin` stack's instance. It runs
beside the Postgres fork until Jellyfin supports Postgres officially. Public at `jellyfin.example.com`
(Authelia OIDC client `jellyfin` redirects here), `jellyfin.mirror.example.com` and
`jellyfin.local.example.com` (own certificate; its LAN DNS record points at the edge Traefik, `10.20.176.187`, so
LAN clients cross both Traefiks like public ones);
`jellyfin.example.net` redirects. Remote ffmpeg as for the fork, but on its own worker
(Swarm `jellyfin-ffmpeg-worker-12`, port 1523) whose image matches this Jellyfin version, so
ffmpeg matches too.

| Mount | Source | Volume | Mode |
|---|---|---|---|
| `/config` | fastdata `docker/jellyfin/{config,data,log,plugins,root}` | `ceph-rbd` PVC `jellyfin-sqlite-config` (copied in once) | RW |
| `/config/metadata` | appdata `docker/jellyfin/metadata` | static CephFS PV | RW |
| `/config/subbuzz` | appdata `docker/jellyfin/subbuzz` | static CephFS PV; the fork reads it RO | RW |
| `/config/data/trickplay` | appdata `docker/jellyfin/trickplay` | static CephFS PV; the fork reads it RO | RW |
| `/cache/transcodes` | fastdata `eph-docker/jellyfin/cache/transcodes-sqlite` | static CephFS PV; the 12 worker mounts it at the same path | RW |
| `/media`, web `config.json` | shared with the fork | | RO |

### Decisions
- **Not migrated from the Swarm stack:** `db` and `pgloader` (the fork's old database and its
  import; the fork is on CNPG), `meilisearch` (its plugin is installed in neither instance) and
  `jellyfin-music` (0 replicas on Swarm).
- **Own transcode dir beside the fork's** (`TranscodingTempPath` `/cache/transcodes` in
  `encoding.xml`, the dir the Jellyfin 12 worker mounts there too): Jellyfin empties its transcode dir recursively at start, so a dir nested in the
  fork's would lose its running streams whenever the fork restarts.
- **Settings on the config volume, not in git:** LDAP server `lldap.lldap.svc.cluster.local`
  (was `10.20.176.210`), `TranscodingTempPath` as above, `network.xml` as for the fork (above).

### One-time copy (Swarm `jellyfin` stopped)
`replicas: 0` until done. The source PV `jellyfin-sqlite-swarm-fast` is read-only.

```sh
kubectl -n jellyfin apply -f - <<'YAML'
apiVersion: batch/v1
kind: Job
metadata: {name: jellyfin-sqlite-copy, namespace: jellyfin}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: copy
        image: alpine:3.22
        command: [sh, -ec]
        args:
        - |
          apk add -q rsync
          rsync -a /src/config /src/data /src/log /src/plugins /src/root /dst/
          cd /src && find config data log plugins root -type f -exec sha256sum {} + | sort -k2 > /tmp/a
          cd /dst && find config data log plugins root -type f -exec sha256sum {} + | sort -k2 > /tmp/b
          cmp /tmp/a /tmp/b && echo VERIFIED $(wc -l < /tmp/a) files
        volumeMounts:
        - {name: src, mountPath: /src, readOnly: true}
        - {name: dst, mountPath: /dst}
      volumes:
      - name: src
        persistentVolumeClaim: {claimName: jellyfin-sqlite-swarm-fast, readOnly: true}
      - name: dst
        persistentVolumeClaim: {claimName: jellyfin-sqlite-config}
YAML
```

Then set the two settings above on the volume and flip `replicas` to 1.

## Playback dashboard

*Jellyfin playback* (`/d/jellyfin-playback`, folder *Cluster*, `grafana.yaml`) answers which hop
limits playback for one instance (variable *Instance*), per path:

- **Direct play**: Jellyfin streams the file itself.
- **ffmpeg path**: every remux and transcode. The SSH wrapper runs all ffmpeg on the GPU worker,
  which writes HLS segments into the transcode dir, and Jellyfin serves them from there.

The top row gives the outcome first. **Pace** is content seconds fetched per wall second per HLS
session (segment rate × segment length / sessions, 2 m window): below 1 the player's buffer drains.
Sessions are the playback progress reports × 10 minus direct streams (*HLS sessions*), because the
segment rate falls exactly when a player stalls. *Transcode speed* classifies a low pace, then the
verdict. *Delivery share* and *Segments over 4.1 s* are context only.

*Worst stage now*, ffmpeg path: ok while pace is at least 0.9. Below that, speed under 1.3 means the
encoder is the limit and names its hottest stage past yellow (GPU host CPU, NVENC, NVDEC, GPU memory
or power, the HDD OSD mean), else "Encoder at its limit". Otherwise Jellyfin node CPU or the
transcode-dir read if past yellow, else "Segment serving or delivery". The ffmpeg verdict is scored
by pace (1 at 0.9, 2 at 0.7). Direct play names its hottest stage. *Stages over time* has one row
per verdict stage, plus pace and the speed classifier. Each path then
has a row of tiles, green/yellow/red against what a stream needs (each tile's description gives the
reasoning). A tile counts only while its path is active, from Jellyfin's own request metrics
(direct-play requests in flight, HLS segment requests), and shows *idle* otherwise.

| Path | Stages |
| --- | --- |
| Direct play | CephFS media read latency; node CPU and the slowest HDD OSD's read latency (also count for the ffmpeg path) |
| ffmpeg path | CephFS transcode-dir read latency (pod side), GPU host CPU (ffmpeg cores / host cores), NVENC, NVDEC, GPU memory, GPU power / limit |

**Transcode speed** is an estimate: ffmpeg's write rate on the GPU host / the instance's Traefik
bytes out. It cannot fall much below 1: an encoder that limits a paced player writes what is
delivered. So it is a classifier, not an outcome. With pace low, near 1 means the encoder is the
limit. Well above 1 means segment serving, delivery or a client that stopped fetching (3 to 9 during
a fetch pause). It under-reads while a player fills its buffer.

**Not separable: segment serving and delivery.** No metric ends at the first byte. On both paths most
of a slow segment is Jellyfin's wait before it (a client probe measured 63–91% of segment time as
time to first byte, at 200–280 Mbit/s downloads), so a high *Delivery share* is mostly server-side.
Separating them needs a Traefik access log with `OriginDuration` or a Jellyfin log line for the wait.

**Blind or approximate:**

- **CephFS client on the GPU host**: no data. `infra/cephfs-client-metrics` runs on the kube workers
  only; the *Source read* tile approximates it with ffmpeg's read rate and the HDD OSD mean.
- **Traefik**: no hop-delay tile. Request-time averages cover different request sets and are
  dominated by long streams and websockets; the edge cannot be split per app either.
- **NICs** come from Proxmox's VM counters and show Mbit/s without a verdict: Proxmox reports no
  link speed. In-guest counters on the kube workers only see the collector pod.
- **Traefik bytes miss direct play**: a range stream the client closes is recorded with no bytes.
  *Bytes to clients* adds the pod's network transmit, which counts it.
- **Pace with a paused player**: clients report progress while paused, so a paused session reads as
  stalled.
- **OSDs**: the read-weighted mean of all HDD OSDs, with the slowest (reads of at least 0.05/s) as
  context, as no metric maps the archive pool to its OSDs.
- **CephFS client latency**: both instances share the media mount when they run on one node.
- A progressive (non-HLS) transcode counts as direct play.

### Decisions

- Thresholds follow what one stream needs, not the hosts' history: a stream must be fetched at
  least at real time (pace), and a media read must return before the
  64 MiB readahead drains, about 6.5 s at 4K remux rate (red at 6 s for the CephFS client and the
  OSDs).
- Backtested over 3 days of playback (Traefik above 250 kB/s or ffmpeg running, as Jellyfin's
  metrics were not scraped yet; 10-minute steps, about 6 hours): red 3% for node CPU and the HDD
  OSDs (single spikes), 0% for the other measured stages. Pace and the verdict were backtested
  against a scripted client's runs (PR #1708).
- The verdict is gated by outcome, not a ranking of every stage, and the outcome is pace: the
  mean segment time fires on healthy streams, mostly on the 25–40 s first segment of a 4K source.
  Pace yellow below 0.9, red below 0.7.
- The speed cut is 1.3: healthy single streams ran 1.8–2.7 by this estimate (ffmpeg's own `speed=`
  2.0–3.0), so anything near 1 with low pace is the encoder.
- The HDD OSD stage uses the read-weighted mean: the slowest OSD (2.8 s) turned the direct-play
  verdict yellow while the client never blocked. NVENC and NVDEC are 5-minute means: a 60 s sample
  swings from 0 to 0.9 within one healthy stream.
- The Traefik services are matched by regex: their hash suffix changes with the route's rule.
- `$instance` carries the instance as its text and the worker's Swarm service as its value
  (`${instance:text}` / `${instance}`).

## Sessions exporter

`sessions-exporter/` polls each instance's `GET /Sessions` every 15 s with an admin API key and
exposes playback state per playing session on `:9924` (`jellyfin_sessions.py` in a ConfigMap on the
stock Python image, tests in `tests/`). The *Sessions from the Jellyfin API* row of the playback
dashboard reads it. It is **not deployed** until the keys exist (owner steps).

| Metric | Labels | Meaning |
| --- | --- | --- |
| `jellyfin_sessions_up` | `jellyfin` | 1 if the last poll succeeded and is under 4 poll intervals old |
| `jellyfin_sessions` | `jellyfin`, `play_method` | playing sessions per method (Transcode, DirectStream, DirectPlay) |
| `jellyfin_session_position_seconds` | + `session` | playback position; pace is its `deriv` |
| `jellyfin_session_paused` | + `session` | 1 if paused |
| `jellyfin_session_transcode_framerate` | + `session` | ffmpeg output fps, while present |
| `jellyfin_session_transcode_speed` | + `session` | output fps / source fps, while video is re-encoded |
| `jellyfin_session_transcode_completion_ratio` | + `session` | share of the item transcoded, 0 to 1 |
| `jellyfin_session_transcode_reason` | + `session`, `reason` | 1 per transcode reason |
| `jellyfin_session_transcode_direct` | + `session`, `stream` | 1 if the video or audio stream is copied |

**Owner steps**

1. In each instance, Dashboard → API Keys, add a key named `sessions-exporter`
   (`jellyfin-pg.example.com` for `jellyfin`, `jellyfin.example.com` for `jellyfin-sqlite`). Copy it and run
   `python3 scripts/secret-from-clipboard.py kube-cluster/apps/jellyfin/secret-sessions-key-jellyfin.yaml jellyfin-sessions-key-jellyfin jellyfin API_KEY`,
   and for the other instance
   `python3 scripts/secret-from-clipboard.py kube-cluster/apps/jellyfin/secret-sessions-key-jellyfin-sqlite.yaml jellyfin-sessions-key-jellyfin-sqlite jellyfin API_KEY`.
2. Add `sessions-exporter` and the secret file(s) to `resources:` in `kustomization.yaml` (a listed
   file must exist, so only now) and merge.
3. Check `jellyfin_sessions_up` is 1 for both instances in Grafana.

### Decisions

- An admin key, because a non-admin key sees only its own sessions. One key per instance: each has
  its own database.
- Session ids are hashed and no user, device or item names are exported.
- Position moves only on the client's progress reports (about every 10 s), so pace needs a 2 m
  window.
- No placeholder secrets in the repo: the component stays out of the kustomization until the keys
  exist (the hermes README follows the same owner-adds-the-file pattern).

## Standing probe

`probe/` runs a paced play against `jellyfin-sqlite` four times a day as the non-admin user
`jellyfin-probe` (password: `secret-jellyfin-probe.yaml`, mounted as a file). It is a scripted
player: HLS segments or one range stream fetched only while less than 30 s is buffered, progress
reported every 10 s, and the session, transcode and token stopped on exit or SIGTERM. Results go to
the OTLP gateway. The *Standing probe* row of the playback dashboard shows them.

| CronJob | at (Europe/Berlin) | play |
| --- | --- | --- |
| `jellyfin-probe-transcode` | 05:20, 11:20, 17:20, 23:20 | *Feature Film A* (4K DV, PGS burn-in) at 8 Mbit/s, 150 s from 10:00 |
| `jellyfin-probe-direct` | 05:50, 11:50, 17:50, 23:50 | *Anthology B* (1080p H.264), direct play, 150 s |

Metrics (gauges, labels `jellyfin`, `mode`): `jellyfin_probe_pace`, `_startup_seconds`, `_stalls`,
`_stall_seconds`, and for transcodes `_ttfb_p50_seconds`, `_ttfb_p95_seconds`,
`_first_segment_seconds` and `_segments`. Pace is content played per wall second after startup: 1
without stalls. `jellyfin_probe_runs{result}` is ok, skipped, check_failed (the skip check got no
answer, so the probe did not play) or error; the pod log says why. Each run pushes once and
daytime slots often skip, so queries use `last_over_time(…[25h])`.

### Decisions

- **Skip while anyone streams**, on either instance: segment requests or HLS or stream requests in
  flight in the last 2 minutes, or ffmpeg on docker-d (Jellyfin's own analysis tasks
  included). The source is VictoriaMetrics, because it is the only place that also sees the GPU
  host; it lags by the 60 s Jellyfin scrape. A query is retried for 15 s (a new pod's traffic is
  dropped until kube-router has programmed it); still unanswered, the run does not play.
- **Schedule**: every 6 h, none in 03:00–05:00, when ffmpeg runs without any stream on some nights
  (Jellyfin's scheduled analysis). Transcode and direct play are 30 minutes apart, so one run's own
  requests never make the other skip.
- **The LAN hostname**, through the edge and both Traefiks, so a run measures a LAN client's path
  and the dashboard's Traefik-based tiles see it.
- A pod killed without SIGTERM leaves its transcode to Jellyfin's own inactivity kill: the stop
  call needs the play session, which only the running pod knows.

## Caveats
- **No GPU** in the cluster → the pod only transcodes on CPU as a fallback; ffmpeg is offloaded
  over SSH to the Swarm GPU worker (`resources.yaml`).
