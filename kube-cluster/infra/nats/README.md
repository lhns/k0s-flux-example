# nats

[NATS](https://nats.io) with JetStream, from the official `nats` chart
(`https://nats-io.github.io/k8s/helm/charts/`). No NACK: streams and consumers are created by
the apps, not by CRs.

- `release.yaml` — HelmRepository + HelmRelease. 3 servers, one per worker (required
  anti-affinity), JetStream file store on `ceph-rbd` (PVC 10Gi, `max_file_store` 8Gi), memory
  store 256Mi, `sync_interval: always`. Static accounts in `config.merge`.
- `certificates.yaml` — self-signed CA → CA Issuer → `nats-route-tls`, mounted as the route
  (`:6222`) TLS with `verify: true`. kube-vnet cannot filter by port, so mTLS is what keeps a
  pod with ingress into the servers from joining the cluster.
- `vnet.yaml` — `nats` (clients → `:4222`), `nats-cluster` (route peering, home namespace
  only), and the servers' ingress binding into the `otel-scrape` vnet.
- `namespace.yaml` — namespace + the `default-requests` LimitRange.

Clients connect to `nats://nats.nats.svc:4222`.

## Auth

Static config, no operator/JWT resolver. `system_account: SYS`. There are no users yet, and
**no anonymous access**: the server requires auth only once at least one user exists, so the
`NOBODY` account holds an nkey user whose private key was never generated, with deny-all
permissions. Keep it; it is what makes removing the last real user safe.

nats-box is disabled until SYS has a user.

### Adding an app

1. The user generates a keypair with `nk -gen user -pubout` and hands the **seed** over with
   `python3 scripts/secret-from-clipboard.py kube-cluster/apps/<app>/nats-secret.yaml <name> <app-ns> NKEY_SEED`
   (AGENTS.md "Receiving secrets"). The seed stays in the app's namespace.
2. Add an account with its public key to `config.merge.accounts` in `release.yaml`. An account
   needs `jetstream: enabled` to use JetStream; limits go there too:
   ```yaml
   APP:
     jetstream: {max_file: 1G, max_mem: 64M}
     users:
       - nkey: U...
   ```
   The reloader applies it without a restart.
3. Add the app's namespace to `allowedNamespaces.names` of the `nats` vnet, and label its pods
   `kube-vnet/net.nats.nats: egress`.

For nats-box, do the same for a SYS user (seed into the `nats` namespace), then enable
`natsBox` with `contexts.default.nkey.secretName`.

## Monitoring

The `prom-exporter` sidecar serves `:7777/metrics` (`nats_*`), scraped via the
`prometheus.io/*` pod annotations; `nats` is in the collector's `namespaces.names`
(`infra/opentelemetry-monitoring/release.yaml`).

The monitoring port `:8222` (`/healthz`, `/varz`, `/jsz`, `/routez`, `/connz`) is
**unauthenticated**: any pod with a membership into the servers (client vnet, `otel-scrape`)
can read it, including connection and account names.

## Volumes

Never delete the PVs: pv-reaper destroys the RBD image (AGENTS.md).

- `fullnameOverride: nats` names the StatefulSet and its PVCs `nats-js-nats-{0,1,2}`. Changing
  it, or the release name, starts empty servers next to the old volumes.
- `volumeClaimTemplates` are immutable. To grow the store, patch each PVC's size (ceph-rbd
  expands online), then `kubectl -n nats delete sts nats --cascade=orphan` and let Flux
  recreate it with the new size. Raise `maxSize` with it.
- Each server holds its own replicas; losing one PVC costs only a resync, for streams with
  `replicas: 3`. An R1 stream lives on one volume only.

## Backups

The `nats` namespace is excluded from every Velero schedule: a crash-consistent snapshot of a
JetStream raft member is not a restore point. There are no accounts or streams yet, so there is
no backup either.

Follow-up once there are streams: a CronJob running `nats account backup` (or `nats stream
backup` per stream) with a SYS or per-account user, uploading to S3 (Ceph RGW, like
`infra/postgres`).

## Upgrading

Read the chart's `UPGRADING.md` and the nats-server release notes before a minor bump, and take
a backup first: JetStream's on-disk format may not downgrade. Renovate opens those PRs with the
`nats` label; patches automerge.
