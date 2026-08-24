# traefik

The cluster ingress controller — a DaemonSet exposed via a metallb LoadBalancer at
**`10.20.2.15`** (ports 80/443), with the dashboard at `traefik.kube.example.com`.

- `release.yaml` — HelmRepository `traefik` + HelmRelease `traefik` `39.0.6`
  (DaemonSet, `LoadBalancer`, `externalTrafficPolicy: Local`).
- `tls.yaml` — a `Certificate` for the wildcard `*.kube.example.com` (cert-manager
  `letsencrypt-ionos-prod`) + a default `TLSStore`, so every host gets TLS without
  a per-host cert.
- `vnet.yaml` — the `traefik` vnet: backends join it (ingress) to receive from
  traefik; traefik pods are egress members.

External traffic to the LB is auto-allowed by kube-vnet (`ext.svc`).

## Client IPs: why three swarm addresses are trusted

`web` and `websecure` set `forwardedHeaders.trustedIPs` to `10.20.2.31/32`, `10.20.2.32/32` and
`10.20.2.33/32` — the docker swarm nodes running the traefik that sits in front of this one. Every
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

**`/32` each, not `10.20.2.0/24`.** Trusting an address means allowing it to assert any client IP
it likes. LAN clients reach this VIP directly and come from `10.20.2.0/24` themselves; they must
stay ordinary untrusted peers, or anyone on the LAN could claim to be anyone.

Consumers still need to trust the extra hop themselves — see the `internalProxies` list in
`apps/guacamole/resources.yaml` and the `KnownProxies` note in `apps/jellyfin/README.md`. Only the
raw-TCP entrypoints (`mqtt`, `ldap`, `authelia`, `xmpp-*`) are unaffected, forwarded headers being
an HTTP concept.
