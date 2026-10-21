# rustdesk

Self-hosted RustDesk server (the OSS `rustdesk/rustdesk-server`): ID/rendezvous (`hbbs`) and
relay (`hbbr`) on MetalLB VIP **`10.20.176.159`**, websockets via Traefik at `rustdesk.example.com`.
No user accounts or web console; that is Pro. Access control is the server key.

## Shape

- **One pod, two containers** from the same image, sharing the `rustdesk-data` PVC as `/root`.
  `replicas: 1`, `Recreate`: the VIP (`externalTrafficPolicy: Local`) and the RWO volume follow
  the pod.
- **Raw ports** (`rustdesk-lb`): TCP 21115 (NAT test), TCP+UDP 21116 (rendezvous, UDP
  hole-punching), TCP 21117 (relay).
- **Websockets** (`rustdesk` ClusterIP, `routing.yaml`): `/ws/id` → hbbs 21118, `/ws/relay` →
  hbbr 21119. Only Traefik reaches them: hbbs takes the client address from `X-Real-IP` there.
  Upstream's web client is self-hostable only in Pro; `https://rustdesk.example.com` itself serves
  nothing but the two `/ws/` paths.

## Why not coturn

RustDesk does not speak STUN/TURN. NAT traversal is its own UDP hole-punching through `hbbs`,
and the fallback relay is `hbbr` with its own protocol on 21117. `apps/coturn` cannot stand in
for either.

## Keys

`hbbs` generates the keypair into the PVC on first start; it never leaves the cluster. The
image is `FROM scratch`, so read the public key from the log rather than with `exec`:

```sh
kubectl -n rustdesk logs deploy/rustdesk -c hbbs | grep 'Key:'
```

- **The keypair is the server's identity.** A new PVC is a new key and every client must be
  reconfigured; see the PV-deletion warning in `AGENTS.md`.
- On the very first start both daemons race to create the key. If the `Key:` lines of `hbbs`
  and `hbbr` differ, delete the pod once: both then read the persisted file.

## Clients

| setting | value |
| --- | --- |
| ID server | `rustdesk.example.com` |
| Relay server | `rustdesk.example.com:21117` |
| API server | empty (OSS) |
| Key | the public key above |

Dashy → Servers → RustDesk copies all four as one string for the client's "Import server config".

LAN clients use the same public name and rely on the router's hairpin NAT.

## Router

Forward on `203.0.113.10` to `10.20.176.159`:

| port | protocol |
| --- | --- |
| 21115 | TCP |
| 21116 | TCP + UDP |
| 21117 | TCP |

21118/21119 ride Traefik on 443; no extra forwards.
