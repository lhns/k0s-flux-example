# arr — the *arr stack, VPN-only

Migrated from the Swarm `arr` stack. Everything here reaches the internet **only**
through `infra/pod-gateway` (NordVPN via gluetun). `seerr` must not be tunnelled and
lives in its own namespace; `autopulse` and `decluttarr` are here, and are routed.

## Why one directory for every *arr workload

The mutating webhook selects by **namespace** label, and the egress NetworkPolicy is
per-namespace. Splitting these into a namespace each would multiply both, and every one
would be a place to forget the label. Here the namespace *is* the switch: anything
scheduled into `arr` is routed, with no per-pod opt-in to omit.

Same reason there are no `setGateway: "false"` labels — nothing in this namespace is
exempt. `apps/matrix` is the precedent for a multi-workload directory.

The `Namespace` is in `namespaces/namespace.yaml`: routed namespaces own themselves
(`infra/pod-gateway/README.md`).

## The Swarm stack is stopped

These mount the same CephFS config directories the Swarm stack used, and two writers on
one *arr SQLite database corrupts it. Everything runs here now; do not start the Swarm
stack again as a rollback without scaling these to 0 first.

Rollback is: scale to 0 and start the Swarm stack again. The config directories are
untouched by the move, which is the whole point of a shared-path cutover.

## Storage

`DATA_PATH=/mnt/appdata/docker/arr`, `DOWNLOADS_PATH=/mnt/archive/media/downloads`.

| PV | fs | rootPath |
|---|---|---|
| `arr-config` | appdata | `/docker/arr` |
| `arr-media` | archive | `/media` |
| `arr-incomplete` | appdata | `/eph/docker/arr` |
| `arr-fast` | fastdata | `/docker/arr` |

One PV per filesystem root, `subPath` per workload. `/media/downloads` is reached by
subPath rather than its own PV so completed downloads and the library stay on one
filesystem and *arr imports can **hardlink** instead of copy.

`arr-media` is a second PV over the subtree jellyfin already mounts read-only —
different name, different access mode, same directory. That is fine.

These are `staticVolume: "true"` PVs pointing at directories that already existed, so
pv-reaper does not claim them — verified: no `reclaim-on-delete` finalizer on any static
CephFS PV in this cluster, including long-lived ones like `gitea-git`. Deleting one
therefore removes a pointer, not data. That is what made renaming this namespace safe.
The AGENTS.md warning still stands in full for dynamically provisioned PVs.

## The symlinks are load-bearing

The *arr databases store root folders as `/series`, `/movies`, `/downloads` — on Swarm
these were symlinks created by `${DATA_PATH}/<app>/init`. Recreated here from the
`arr-link-paths` ConfigMap rather than mounted off CephFS, so they are reviewable
in git. **If they are missing, every root folder breaks on cutover** — this is the most
likely way this migration fails quietly.

lidarr's set (`link-paths/lidarr`) links `/music`, `/videos`, `/import` and `/downloads`.

## lidarr is the hotio plugins build

`lidarr` runs `hotio/lidarr:pr-plugins` on Postgres `lidarr-main`; its `${DATA_PATH}/lidarr`
SQLite file is unused. The `blampe/lidarr` (`hearring-aid`) tree is abandoned and not deployed.
The `RandomNinjaAtk/arr-scripts` fetch is dropped: it downloaded and executed code from GitHub on
every start, against this repo's "never fetch files at pod start" rule.

## Carried over, and the coupling it creates

sonarr, radarr, prowlarr and lidarr mount their Swarm `run` file over the image's s6 service
definition. The only difference from stock is `-exitimmediately`. This is **coupled to
the image tag's s6 layout** — if a Renovate bump moves `svc-<app>`, startup breaks and
the fix is to delete that volumeMount.

## What this does not protect against

The nftables lock that closes `SO_BINDTODEVICE` is not deployed — see
`infra/pod-gateway/README.md`. `networkpolicy.yaml` is a partial substitute: it denies
all egress except DNS, the gateway namespace and the cluster CIDRs, with no `0.0.0.0/0`
rule. It is negatable by any other NetworkPolicy in this namespace.

slskd has no inbound P2P: behind the gateway a connection arriving on `eth0` would reply
via `vxlan0`, and NordVPN offers no port forwarding. Downloads work; we are not
connectable.

## Databases

sonarr, radarr, prowlarr, lidarr and autopulse use the CNPG cluster via `Database`/`DatabaseRole` in the
`postgres` namespace (`database.yaml`), with `retain` on both: never drop them, they hold the
libraries.

## Verifying after cutover

```sh
# routed correctly: one default route, on vxlan0, and NordVPN's IP
kubectl -n arr exec deploy/sonarr -- ip route
kubectl -n arr exec deploy/sonarr -- curl -s https://ifconfig.io

# the symlinks exist
kubectl -n arr exec deploy/sonarr -- ls -l /series /movies /downloads

# fail-closed: kill gluetun in pod-gateway, these must lose the internet
# rather than falling back to eth0
```

## Sonarr queue tool

`tools/sonarr-queue.py` (stdlib Python, not deployed) triages the "needs user interaction"
queue (`importBlocked`/`importPending`). The unit is the download, not the queue row: a season
pack has one row per episode.

```sh
T=kube-cluster/apps/arr/tools/sonarr-queue.py
python3 $T list                       # one row per download, with reasons
python3 $T report                     # by problem category and series; --json
python3 $T approve --auto-language    # dry run: clean files with a wanted language
python3 $T approve <id>... --yes      # ids: queue id, downloadId or 6+ char prefix
python3 $T approve <id> --episodes S17E47 --yes   # override Sonarr's mapping of one file
python3 $T approve <id> --map 'S2 - 13=S02E13' --map ...   # per file, in a pack
python3 $T deny <id>... --blocklist --yes
python3 $T refresh <id> --yes --wait     # RefreshSeries for that download's series
```

- **Key stays in the pod.** The default transport pipes a generated `sh` program into
  `kubectl exec -i deploy/sonarr -- sh -s`; it reads `ApiKey` from `/config/config.xml` and
  gives it to curl on stdin (the image has no python). One exec per batch of requests. Via a
  jumphost: `--kubectl 'ssh HOST kubectl'` or `SONARR_QUEUE_KUBECTL`.
  `--url`/`--config-xml` is the in-process alternative where python and the file are available.
- **Read-only by default.** `approve` and `deny` are dry runs until `--yes`. Samples are never
  imported; a file with a rejection is imported only when its download is approved by id.
  Manual mappings refuse episodes that already have a file unless `--allow-overwrite`.
  `--auto-language` takes only rejection-free files with a language from `--want-languages`
  (default `German,English`, owner policy), skips whole downloads whose file-to-episode mapping
  is inconsistent or whose release names another year than the series (`Show.2025.S02E03`
  matched to `Show (2005)` is usually a different show), and lists episodes claimed by two
  downloads as conflicts instead of picking.
- **`deny` never touches the download client unless asked.** Sonarr's API defaults
  `removeFromClient` to true; the tool always sends it, `false` unless `--remove-from-client`.
  With `--blocklist` Sonarr searches for a replacement unless `--skip-redownload` is given.
- Tests: `bash scripts/run-tests.sh` (synthetic fixtures, no cluster).
