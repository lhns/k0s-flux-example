# 0010 — Ingress: one Traefik behind one MetalLB VIP, with a wildcard certificate

- Status: Accepted
- Date: 2026-07-08, amended 2026-07-21, 2026-07-22, 2026-07-23, 2026-07-24, 2026-08-15 and
  2026-08-30
- Current answer: Traefik runs as a DaemonSet behind a single MetalLB L2 VIP
  (`externalTrafficPolicy: Local`). A wildcard certificate from cert-manager (Let's Encrypt,
  DNS-01 through the IONOS webhook) is the default `TLSStore`, so a route writes `tls: {}`.
  Raw TCP services get a Traefik TCP entrypoint on the same VIP rather than a VIP of their own
  where possible. Public traffic arrives through the outer Traefik on the old Swarm nodes, whose
  addresses are the only trusted `X-Forwarded-For` sources.

## Context

Traefik, MetalLB and cert-manager were adopted in place from k0s Helm extensions
([0001](0001-k0s-and-flux-replace-docker-swarm.md)); the pool and certificates were hand-applied
before that.

## Decision

- **One VIP for HTTP(S).** DNS for the public hosts points at it.
- **Wildcard via DNS-01**, so no route needs its own certificate. DNS-01 because it issues
  wildcards and can certify names that are not publicly reachable.
- **One default certificate** (2026-07-21) covering the apex and both wildcard levels, rather
  than a Certificate per app copied around: Traefik picks certificates by SNI from one pool, so a
  shared default serves every route with no per-app config.
- **TCP entrypoints on the shared VIP** (2026-07-22): MetalLB cannot share a VIP between two
  LoadBalancer Services when one is `externalTrafficPolicy: Local`, which Traefik is. MQTT, LDAP,
  Authelia's forwardAuth for the Swarm proxies and XMPP ride Traefik entrypoints instead. TLS for
  such a protocol terminates at Traefik with the wildcard too (MQTT, 2026-09-10).
- **Own VIP when Traefik cannot carry it** (2026-07-24): a service that needs UDP or its own
  ports gets a MetalLB LoadBalancer Service with a `/32` pool of its own, mixed protocols in one
  Service. Pod addresses on a LAN interface come from a separate block, since MetalLB cannot see
  them and would hand out the same address ([0031](0031-beyond-ingress-isolation.md)).
- **Canonical hosts** (2026-07-23): `<app>.<domain>` is canonical and the `<app>.kube.<domain>`
  host redirects to it, reversing the first convention for every public app. One canonical host
  per app, the other a plain redirect, because most OIDC apps support only one base URL.
- **Trusted edge** (2026-08-15): `forwardedHeaders.trustedIPs` lists the outer proxy's nodes as
  `/32`s. Traefik v3's empty default discards `X-Forwarded-For`, so every external client would look
  like one proxy node to rate limits, bans and logs.
- **No read timeout** (2026-08-30): Traefik v3's 60 s `readTimeout` covers the request body and
  cut off long uploads; the outer proxy sets none. cert-manager's webhook runs two replicas,
  because a single replica's restart fails every dependent dry-run.

## Consequences

- An app's own NetworkPolicy membership in the `traefik` vnet is what lets Traefik reach it
  ([0013](0013-kube-vnet-default-deny-ingress.md)).
- Trusting a wider range than the proxy nodes would let any LAN client assert a client IP.
- An app whose base URL is baked into its config or its OIDC client changes in both places when
  its canonical host moves.

## References

- `fd71286b` (wildcard + TLSStore), `a75d13b3`..`b7a442ca` (one default certificate),
  `c0b69b60`, `2c9ba795`, `e6c9f7ac`, `a2eaff60`, `19159c99`, `8948b70e`, `de62e1c5`,
  `24e924c2`, `c9a2e37b`, `8a67cab4` (MQTT over TLS).
- Conversation 2026-07-21 (one certificate, one canonical host), 2026-07-24 and 2026-07-27
  (own VIPs), 2026-10-05 (why DNS-01).
- [infra/traefik](../../kube-cluster/infra/traefik/README.md),
  [infra/cert-manager](../../kube-cluster/infra/cert-manager/README.md).
