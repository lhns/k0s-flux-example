# arr — the *arr stack, VPN-only

Migrated from the Swarm `arr` stack. Everything here reaches the internet **only**
through `infra/pod-gateway` (NordVPN via gluetun). Workloads that must not be
tunnelled — seerr, autopulse, decluttarr — live elsewhere, not in this namespace.

## Why one directory for six workloads

The mutating webhook selects by **namespace** label, and the egress NetworkPolicy is
per-namespace. Splitting these into six namespaces would multiply both, and every one
would be a place to forget the label. Here the namespace *is* the switch: anything
scheduled into `arr` is routed, with no per-pod opt-in to omit.

Same reason there are no `setGateway: "false"` labels — nothing in this namespace is
exempt. `apps/matrix` is the precedent for a multi-workload directory.

The `Namespace` object is **not** here; `infra/pod-gateway` owns it. Its `namespace.yaml`
explains why.

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
| `arr-fast` | fastappdata | `/docker/arr` |

One PV per filesystem root, `subPath` per workload. `/media/downloads` is reached by
subPath rather than its own PV so completed downloads and the library stay on one
filesystem and *arr imports can **hardlink** instead of copy.

`arr-media` is a second PV over the subtree jellyfin already mounts read-only —
different name, different access mode, same directory. That is fine.

These are `staticVolume: "true"` PVs pointing at directories that already existed, so
pv-reaper does not claim them — verified: no `reclaim-on-delete` finalizer on any static
CephFS PV in this cluster, including long-lived ones like `gitea-git`. Deleting one
therefore removes a pointer, not data. That is what made renaming this namespace safe.
The CLAUDE.md warning still stands in full for dynamically provisioned PVs.

## The symlinks are load-bearing

The *arr databases store root folders as `/series`, `/movies`, `/downloads` — on Swarm
these were symlinks created by `${DATA_PATH}/<app>/init`. Recreated here from the
`arr-link-paths` ConfigMap rather than mounted off CephFS, so they are reviewable
in git. **If they are missing, every root folder breaks on cutover** — this is the most
likely way this migration fails quietly.

lidarr has none: its config uses `/media/...` paths directly.

## lidarr is the plugins build, and the only one

Swarm ran two: a `lidarr` service on `hotio/lidarr:pr-plugins` whose database is 401 KB
and last written **Dec 2024**, and a commented-out `lidarr2` on `blampe/lidarr` whose
`hearring-aid` database is **42 MB from Nov 2025**. The running one was an empty shell.

So `lidarr` here is the plugins build against `hearring-aid/data`. Dropped: the hotio
service, its `run`/`init`/`services` overrides, and the `RandomNinjaAtk/arr-scripts`
fetch — which downloaded and executed code from GitHub on every start, against this
repo's "never fetch files at pod start" rule. `${DATA_PATH}/lidarr` stays on disk, unused.

## Carried over, and the coupling it creates

sonarr, radarr and prowlarr mount their Swarm `run` file over the image's s6 service
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

The *arr apps and seerr shared a Postgres container on Swarm
(`/mnt/fastappdata/docker/arr/db`, published on `:5438`). That does not move — they go
to the CNPG cluster via `Database`/`DatabaseRole` in the `postgres` namespace. Not done
yet: dump each database out of the Swarm Postgres, restore into CNPG, then rewrite each
app's `config.xml`. Verify row counts per table, not just that the restore exited 0.

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
