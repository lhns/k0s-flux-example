# snikket

[Snikket](https://snikket.org) — XMPP server at **`xmpp.example.com`**, with the web portal on
`xmpp.example.com`, group chats on `groups.xmpp.example.com` and HTTP file sharing on
`share.xmpp.example.com`. Federates with the public XMPP network.

## Shape

- **Server:** one Deployment, `replicas: 1`, `strategy: Recreate`, **three containers in one
  Pod** — `server` (prosody), `web-proxy`, `web-portal`.
- **Files:** one PVC at `/snikket`, `ceph-rbd` 20 Gi. Prosody is a single writer with its own
  on-disk format, so block rather than CephFS. `SNIKKET_UPLOAD_STORAGE_GB=10` keeps uploads
  from filling the volume out from under prosody's state.
- **TURN:** none of its own — `SNIKKET_TWEAK_TURNSERVER=0`, pointing at
  [`../coturn`](../coturn/README.md) on `turn.example.com`.
- **Certs:** cert-manager, DNS-01 via IONOS. Not Snikket's certbot.
- **kube-vnet:** `net.traefik.traefik: ingress` — everything arrives through Traefik — plus
  `net.lldap.lldap: egress` for authentication.
- **Auth:** lldap. Members of the `xmpp` or `admin` group log in with their directory password;
  `admin` maps to prosody's admin role. See [`ldap-integration.md`](ldap-integration.md).
- **DB:** none. Prosody uses its own file storage.

## Why one Pod with three containers

Upstream ships four containers on `network_mode: host`, talking to each other over
`localhost` — the portal is reachable only from the proxy, the proxy asks prosody's internal
HTTP API on `127.0.0.1:5280`, and none of it is configurable per-host. A single Pod reproduces
that exactly: one network namespace, one `localhost`. Splitting them into three Deployments
would mean inventing Services and rewriting the internal addresses Snikket hardcodes.

Two of upstream's four are deliberately **absent**:

| dropped | why |
| --- | --- |
| `snikket-cert-manager` | it is a certbot runner; cert-manager already does this, better (DNS-01, no inbound HTTP) |
| bundled `coturn` | one shared TURN in `apps/coturn` means one UDP relay range to forward, not one per app |

## Ports, and why none of them need a VIP

With TURN split out, everything left is TCP, and Traefik carries TCP fine — so Snikket runs
entirely on the shared Traefik VIP `10.20.2.15` and needs no address of its own. That matters:
VIPs here are a scarce, hand-allocated resource.

| port | entrypoint | what |
| --- | --- | --- |
| 443 | `websecure` | portal, group chat, file sharing (TLS terminated by Traefik) |
| 5222 | `xmpp-c2s` | client connections |
| 5269 | `xmpp-s2s` | federation with other servers |
| 5000 | `xmpp-proxy65` | SOCKS5 file-transfer proxy |

The three raw ones are `IngressRouteTCP` with ``HostSNI(`*`)`` and **no `tls:` block** —
plain TCP passthrough. That is required, not a shortcut: XMPP opens in cleartext and upgrades
via STARTTLS in-band, so Traefik must not attempt termination. Prosody presents the cert
mounted into the Pod.

Each of the three entrypoints is defined in `infra/traefik/release.yaml` and is **1:1 with
this backend** — a second XMPP server would need SNI routing or its own VIP.

## Certificates

The `*.example.com` wildcard covers `xmpp.example.com` but **not** `groups.xmpp.example.com` or
`share.xmpp.example.com`: a single-label wildcard does not match two labels deep. Hence a
dedicated `Certificate` with all three as SANs (`routing.yaml`).

One Secret serves both ends — Traefik reads it for TLS termination, and the Pod mounts it for
prosody's c2s/s2s listeners. Snikket expects certbot's layout, so the volume renames the keys
in place rather than copying them:

```yaml
items:
  - {key: tls.crt, path: fullchain.pem}
  - {key: tls.key, path: privkey.pem}
```

mounted at `/snikket/letsencrypt/live/xmpp.example.com`. `SNIKKET_TWEAK_WEB_PROXY_RELOAD_INTERVAL`
makes the proxy re-read them, so renewal needs no restart.

## Adding a user

In lldap: create the account and put it in the `xmpp` group (or `admin`, which additionally
grants the prosody admin role on next login). Nothing to do on the Snikket side — prosody
creates the roster and archives lazily on first login, keyed by JID.

Snikket's own invite flow is disabled (`register`, `invites_register`, `invites_register_api`),
because account creation cannot work against a directory Snikket does not own. Contact invites
and circles still work; they create no accounts.

The username must match the JID localpart you want: `lhns` in lldap means
`lhns@xmpp.example.com`.

## Router and edge

**Router** → the Traefik VIP `10.20.2.15`, TCP, no TLS termination: `5222`, `5269`, `5000`.
TURN's ports are separate — see [`../coturn`](../coturn/README.md).

**The edge certificate must cover `*.xmpp.example.com`**, not just `*.example.com`. A DNS wildcard
matches exactly one label, so `*.example.com` covers `xmpp.example.com` but **not**
`groups.xmpp.example.com` or `share.xmpp.example.com`. Miss this and the edge has no matching
certificate, falls back to its self-signed default, and clients fail on TLS verification
*before* sending a request — which presents as a connection error rather than anything
resembling a certificate problem.

The alternative is SNI passthrough of the three hosts to `10.20.2.15:443`, letting the cluster
terminate with its own cert. That needs no certificate at the edge at all, and is the better
option if you would rather not issue wildcards there.

Either way the cluster side is already complete: `snikket-tls` carries all three names as SANs
(`routing.yaml`).

DNS needs nothing — all three resolve through the existing `*.example.com` → `static.example.com`
chain.

## Notes

- **`SNIKKET_DOMAIN` is permanent.** It is the JID domain (`user@xmpp.example.com`), it is written
  into prosody's store on first boot, and Snikket has no rename path. Changing it means
  starting over.
- **`SNIKKET_TWEAK_IPV6=0`** because the cluster is IPv4-only while pods keep a link-local
  IPv6 — the same trap that needed `disable_ipv6` for uptime-kuma (see `k0sctl.yaml`).
- **No liveness probe on the server**, on purpose: first boot generates state, and a restart
  part-way through leaves prosody's store half-written. Readiness already pulls it from the
  Service.
- **Federation is on**, so 5269 is reachable from the internet by design. Check it end to end
  at `https://observe.jabber.network/xmpp.example.com` — it verifies s2s, certificates and SRV
  records together.
- **The certs container is dropped, which is a deviation from upstream.** If a future Snikket
  release insists on certbot's `archive/` symlink layout rather than plain files, the fallback
  is a small initContainer that materialises that layout from the mounted Secret.
