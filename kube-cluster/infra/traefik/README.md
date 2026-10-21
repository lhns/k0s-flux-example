# traefik

The cluster ingress controller — a DaemonSet exposed via a metallb LoadBalancer at
**`10.20.176.210`** (ports 80/443), with the dashboard at `traefik.kube.example.com`.

- `release.yaml` — HelmRepository `traefik` + HelmRelease `traefik`
  (DaemonSet, `LoadBalancer`, `externalTrafficPolicy: Local`).
- `tls.yaml` — a `Certificate` for the wildcard `*.kube.example.com` (cert-manager
  `letsencrypt-ionos-prod`) + a default `TLSStore`, so every host gets TLS without
  a per-host cert.
- `vnet.yaml` — the `traefik` vnet: backends join it (ingress) to receive from
  traefik; traefik pods are egress members.

External traffic to the LB is auto-allowed by kube-vnet (`ext.svc`).

## Client IPs: why three swarm addresses are trusted

`web` and `websecure` set `forwardedHeaders.trustedIPs` to `10.20.176.125/32`, `10.20.176.185/32` and
`10.20.176.190/32` — the docker swarm nodes running the traefik that sits in front of this one. Every
external request arrives from one of them.

Traefik v3's default is an empty trust list, and that does not mean "leave the inbound header
alone". It means **discard `X-Forwarded-For` and rewrite it with the peer address**. So before
this was set, every external user reached every app as a swarm node address: jellyfin logged it,
guacamole's brute-force extension banned it, and a per-IP `rateLimit` was one shared bucket for
the whole internet rather than a limit per user. It read as a NAT problem and was written up as
one in `apps/guacamole/README.md` for a while; it was not.

`externalTrafficPolicy: Local` on the Service is a separate mechanism and was never the issue —
it preserves the *TCP* source faithfully, but for a proxied request the TCP source really is the
edge node.

**`/32` each, not `10.20.176.0/24`.** Trusting an address means allowing it to assert any client IP
it likes. LAN clients reach this VIP directly and come from `10.20.176.0/24` themselves; they must
stay ordinary untrusted peers, or anyone on the LAN could claim to be anyone.

Consumers still need to trust the extra hop themselves — see the `internalProxies` list in
`apps/guacamole/resources.yaml` and the `KnownProxies` note in `apps/jellyfin/README.md`. Only the
raw-TCP entrypoints (`mqtt`, `ldap`, `authelia`, `xmpp-*`) are unaffected, forwarded headers being
an HTTP concept.

## Alerts

`grafana.yaml`: rule group `infra-traefik`, folder *Cluster*; every rule was backtested over 7 days before it was added.

| Rule | Fires when | For | Severity |
| --- | --- | --- | --- |
| `traefik-cert-expiry-21d` / `-7d` | edge certificate expires within 21d / 7d | 1h | warning / critical |
| `traefik-5xx` | over 10% 5xx for a service with over 0.2 requests/s (15m) | 15m | warning |
| `traefik-metrics-stalled` | no edge Traefik request counter for 10m (also on NoData) | 5m | warning |

Backtests over 7 days:

| Rule | Result | Decision |
| --- | --- | --- |
| `traefik-cert-expiry-*` | nothing; the shortest is 27 days | as specified; a superseded `example.com` certificate with an `xmpp` SAN will trip the 21d rule first |
| `traefik-5xx` | at 0.05 requests/s: `ceph-s3` 7 times | request floor raised to 0.2/s: only `kube-traefik-websecure`, 3 episodes on one day |
| `traefik-metrics-stalled` | nothing | as specified |
