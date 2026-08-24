# coturn

Shared STUN/TURN server on MetalLB VIP **`10.20.2.80`** (`turn.example.com`). Relays audio/video
media for clients that cannot reach each other directly — which is most of them, since
symmetric NAT defeats plain hole-punching.

## Shape

- **Server:** one Deployment, `replicas: 1`, `strategy: Recreate`. A single VIP with
  `externalTrafficPolicy: Local` means the address follows the pod; a second replica would
  split relay state and strand allocations.
- **Config:** `turnserver.conf` via `configMapGenerator`, with the one secret line appended at
  startup by the `render-config` initContainer.
- **Certs:** the `*.example.com` wildcard, mirrored out of the `traefik` namespace by Reflector
  (`infra/traefik/tls.yaml`), for `turns:5349`. `turn.example.com` is a single label, so the
  wildcard covers it.
- **kube-vnet:** none, deliberately — coturn has no in-cluster edges. Every port arrives via
  the `coturn-lb` LoadBalancer, which kube-vnet auto-allows (`ext.svc`), and relaying to peers
  is ordinary external egress.

## Why it is its own app

Snikket bundles a coturn and Matrix could run one, so the obvious thing is two or three TURN
servers. Each would want its own VIP and its own UDP relay range punched through the router,
for no benefit — the protocol is stateless-ish and one instance serves any number of apps.

Both consumers speak coturn's **REST-API credential scheme** (`use-auth-secret`): the
application derives `username=<expiry>:<user>`, `password=HMAC(shared-secret, username)` and
hands it to the client, so no user accounts exist in coturn itself. That is what makes sharing
possible at all, and why all three components need the *same* secret:

| consumer | how it is wired |
| --- | --- |
| `snikket` | `SNIKKET_TWEAK_TURNSERVER=0` disables its bundled one; `_DOMAIN`/`_SECRET`/`_PORT` point here |
| `matrix` (synapse) | `turn_uris` + `turn_shared_secret` in `secret-synapse.yaml` |

`coturn-shared-secret` is owned by this namespace and mirrored to both by Reflector, so
rotating it is a single edit rather than three that can drift.

## The two things that must stay in sync

**Relay ports.** Kubernetes Services have no port ranges, so `49152-49201` is enumerated one
entry per port on `coturn-lb` (the LiveKit precedent, `apps/matrix/resources.yaml`).
`min-port`/`max-port` in `turnserver.conf` must match exactly. Widen one without the other and
coturn hands out ports nothing forwards — calls fail with nothing obviously wrong in any log.

**`external-ip=203.0.113.10`.** Behind NAT, coturn must advertise the address clients actually
reach, not its pod IP. Get this wrong and every relay candidate it offers is unroutable, which
looks like "calls just don't connect" rather than a configuration error.

## Router

Forward to `10.20.2.80`, the same mechanism already used for LiveKit's range:

| port | protocol |
| --- | --- |
| 3478 | TCP + UDP |
| 5349 | TCP + UDP |
| 49152-49201 | UDP |

## Notes

- **The relay is deny-by-default toward private space.** `denied-peer-ip` covers RFC1918,
  loopback, link-local and multicast. Without it a TURN server is an open proxy into the LAN
  for anyone holding a credential — and credentials are derived, not issued, so "anyone" is
  broader than it sounds.
- **No liveness probe on purpose.** coturn holds live allocations in memory; a restart drops
  every call in progress. Readiness failing already pulls it out of the VIP, which is the
  behaviour we want.
- **`realm=example.com`** is the credential realm, not a hostname to connect to. It must match
  what the consumers send; both derive it from the shared-secret scheme rather than
  configuring it separately.
- Verify a relay candidate (not just `srflx`) with a trickle-ICE page against
  `turn:turn.example.com:3478` — that is the check that proves the router forwards the UDP range,
  which is the part most likely to be missing.
