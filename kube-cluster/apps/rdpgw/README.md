# rdpgw

[rdpgw](https://github.com/bolkedebruin/rdpgw), an MS-TSGU Remote Desktop Gateway, at
`https://rdp.example.com`, built from the owner's fork ([lhns/rdpgw](https://github.com/lhns/rdpgw),
branch `ldap-auth`). Native RDP clients reach the Windows VMs through it, so no raw RDP port is
published. Two logins work on the same hostname:

- **OIDC**: an Authelia login with 2FA in the browser hands out an `.rdp` file carrying a gateway
  token (mstsc).
- **HTTP Basic**: the client sends an lldap username and password to the gateway (Android Windows
  App, any client that cannot open a browser first).

## Connecting

With OIDC:

1. Open `https://rdp.example.com/` (rdpgw's web UI: pick a VM) or `https://rdp.example.com/connect?host=<name>:3389`
   in a browser and sign in to Authelia (2FA).
   The browser downloads an `.rdp` file.
2. Open the file within 5 minutes. mstsc connects through the gateway and asks for the **Windows**
   credentials of the VM, as usual.

With Basic: in the client, set the gateway to `rdp.example.com` with the **lldap** username (not the
email) and password, and the PC to the VM name (e.g. `admin-vm:3389`; IPs are refused) with the
**Windows** credentials.

| VM | `host=` |
| --- | --- |
| Admin-VM | `admin-vm:3389` (10.20.150.99) |
| User-VM | `user-vm:3389` (10.20.150.187) |
| User2-VM | `user2-vm:3389` (10.20.150.135) |
| User3-VM | `user3-vm:3389` (10.20.150.5) |

- The `.rdp` token (5 min) is tied to the client IP that downloaded it. Download it on the machine
  that opens it; a VPN or IPv4/IPv6 switch in between fails with "connection refused by the
  gateway".
- Allowed users: members of the groups `admin` or `rdp`, for both logins (OIDC: policy `rdpgw` in
  `apps/authelia`; Basic: `RDPGW_LDAP__REQUIRED_GROUPS`). Keep the two lists in sync.
- Failed Basic logins lock out the client IP (10 in 15 min, for 1 h) and the username (5 in
  15 min, for 5 min); rdpgw's defaults.

## Login modes

`RDPGW_SERVER__UNAUTHENTICATED_REQUESTS` decides how rdpgw answers a gateway request without
credentials. A request with Basic credentials always takes the Basic path.

- `paa`: the request goes to the token path. This fails for the Android app: it sends Basic
  credentials only after a 401, so its first request fails the token path's capability check.
- `challenge` (current): the request gets a Basic 401. mstsc has to survive that 401, by retrying
  with a non-Basic scheme (which still goes to the token path) or by answering with Basic.

If mstsc does not survive it, give Basic its own hostname (a second IngressRoute host and a second
Deployment with `ldap` only) and keep `rdp.example.com` OIDC-only.

Reading `ssh admin@10.20.150.84 'kubectl -n rdpgw logs deploy/rdpgw'`:

- `auth: result=success method=basic`: the Basic login worked. `reason=invalid_credentials` is a
  wrong password; `reason=access_denied` means the bind worked but the user is in neither `admin`
  nor `rdp`.
- `Invalid PAA cookie`, or a capability mismatch at handshake, on a request without credentials:
  a Basic client landed on the token path.
- `rejecting reuse of Rdg-Connection-Id`: the two halves of a non-websocket session did not pair.
  That is a transport problem, not the login mode.

## Shape

- One Deployment, env-only config (`resources.yaml`). Keys and the OIDC client secret are in the
  SOPS Secrets `rdpgw` and `rdpgw-paa`. rdpgw serves plain HTTP on :8080, Traefik terminates TLS.
- `routing.yaml` routes sign-in and the web UI (`/connect`, `/callback`, `/`, `/static/`,
  `/assets/`, `/api/v1/`) and `/remoteDesktopGateway/`, each with a per-IP `rateLimit`. `/metrics` (scraped by otel) and `/tokeninfo` are not exposed.
- `networkpolicy.yaml`: egress only to DNS, the four VMs on :3389, Traefik's VIP (Authelia) and
  lldap :3890 (the pod also joins the `lldap` vnet, `apps/lldap/vnet.yaml`). Adding a VM means
  editing `RDPGW_SERVER__HOSTS` and this policy.
- The Authelia client `rdpgw` and policy `rdpgw` are in `apps/authelia/configuration.yml`.
  Rotating the client secret means replacing `secret.yaml` and the hash in that client together.

## Decisions

- **Why rdpgw.** It is the only option that speaks the gateway protocol native clients (mstsc,
  Android) already have, runs in a container and is reachable over plain HTTPS. The price is a
  one-maintainer project whose docs lag its code; config here is derived from the source
  (`cmd/rdpgw/config/configuration.go`), not the README. The fallback is a rate-limiting TCP proxy.
- **The fork.** Upstream has no LDAP backend and refuses OIDC and Basic on one instance. The fork
  adds both; the image is built by the fork's CI (`ghcr-image.yml`, on push to `ldap-auth`) and
  pinned by digest.
- **One instance, both logins.** One hostname for every client, no second Deployment and
  certificate. The fork routes by request, not by session: Basic credentials go to a gateway that
  checks them against lldap and the host allow-list; everything else goes to one that requires
  the signed token. A browser session never stands in for either.
- **Basic is password-only.** It skips Authelia and its 2FA. What remains: the lldap password,
  the `rdp` group, rdpgw's lockout, the host allow-list, and then the Windows login. The OIDC
  path keeps 2FA for clients that can use it.
- **No forwardAuth, no Anubis.** The RDP client has no browser and cannot follow a redirect or
  solve a challenge. Authelia's `access_control` never sees this host, so the `rdpgw`
  authorization policy gates OIDC sign-in and the lldap group gates Basic.
- **`unsigned` host selection.** The user chooses the VM, rdpgw checks it against the allow-list
  (at download and in the tunnel for OIDC, in the tunnel for Basic), and the egress policy backs
  it. `signed` and `any` are refused by the fork when Basic is stacked.
- **Plain HTTP behind Traefik** (`RDPGW_SERVER__TLS=disable`). Basic credentials are accepted
  only with `TLS_TERMINATED_BY_PROXY`, i.e. from a trusted proxy reporting `https` (`wss` for a
  websocket upgrade). Traefik's `readTimeout: 0s` on `websecure` lets the long-lived RDG_IN_DATA
  request through; websocket sessions are upgraded and unaffected by timeouts.
- **Client IP from `TRUSTED_PROXIES`.** rdpgw takes the rightmost untrusted `X-Forwarded-For`
  hop, so both hops in front of it are listed (see `resources.yaml`). It feeds the token's IP
  binding and the per-IP lockout; with a hop missing, every client is a traefik pod.
- **Env-only config.** The image's default config path does not exist, and env values keep all
  secrets out of files. Names are `RDPGW_<SECTION>__<KEY>` with `_` between words.
- **`verifyclientip` stays on.** Turning it off would let a leaked `.rdp` token work from
  anywhere for 5 minutes.
- **Single replica, in-memory state.** OIDC login state (2 min), lockout counters and live
  tunnels are not shared; a pod restart ends open sessions.
