# snikket

[Snikket](https://snikket.org) — XMPP server at **`xmpp.example.com`**, with the web portal on
`xmpp.example.com`, group chats on `groups.xmpp.example.com` and HTTP file sharing on
`share.xmpp.example.com`. Federates with the public XMPP network.

## Shape

- **Server:** one Deployment, `replicas: 1`, `strategy: Recreate`, **three containers in one
  Pod** — `server` (prosody), `web-proxy`, `web-portal`. Upstream ships four containers on
  `network_mode: host`, talking over `localhost`; one Pod reproduces that exactly, where three
  Deployments would need Services and rewritten internal addresses.
- **Dropped from upstream:** `snikket-cert-manager` (cert-manager does it, DNS-01, no inbound
  HTTP) and the bundled `coturn` (one shared TURN in [`../coturn`](../coturn/README.md) means one
  UDP relay range to forward; `SNIKKET_TWEAK_TURNSERVER=0`).
- **Files:** one PVC at `/snikket`, `ceph-rbd` 20 Gi (a single writer with its own on-disk format,
  so block). `SNIKKET_UPLOAD_STORAGE_GB=10` keeps uploads from filling it. No database.
- **kube-vnet:** `net.traefik.traefik: ingress`, `net.lldap.lldap: egress` for authentication, and
  `net.snikket-component: ingress` for cheogram-sip's component port 5347 (`vnet.yaml`).
- **Component secrets:** this component owns the XEP-0114 secrets (`secret-sip-components.yaml`),
  reflected into `cheogram-sip`; `apps/cheogram-sip` depends on it.

## Ports, and why none of them need a VIP

Everything is TCP, and Traefik carries TCP, so Snikket runs on the shared VIP `10.20.176.210` and
needs no address of its own.

| port | entrypoint | what |
| --- | --- | --- |
| 443 | `websecure` | portal, group chat, file sharing (TLS terminated by Traefik) |
| 5222 | `xmpp-c2s` | client connections |
| 5269 | `xmpp-s2s` | federation |
| 5000 | `xmpp-proxy65` | SOCKS5 file-transfer proxy |

The three raw ones are `IngressRouteTCP` with ``HostSNI(`*`)`` and **no `tls:` block**: XMPP opens
in cleartext and upgrades by STARTTLS in-band, so Traefik must not terminate. Prosody presents
the cert mounted into the Pod. Each entrypoint (`infra/traefik/release.yaml`) is 1:1 with this
backend; a second XMPP server would need SNI routing or its own VIP.

## Certificates

The `*.example.com` wildcard does **not** cover `groups.xmpp.example.com` or `share.xmpp.example.com` (a
wildcard matches one label), so `snikket-tls` carries all three as SANs (`routing.yaml`). One
Secret serves Traefik and the Pod; Snikket expects certbot's layout, so the volume renames
`tls.crt`/`tls.key` to `fullchain.pem`/`privkey.pem` in place, mounted at
`/snikket/letsencrypt/live/xmpp.example.com`. `SNIKKET_TWEAK_WEB_PROXY_RELOAD_INTERVAL` makes the
proxy re-read them, so renewal needs no restart. If a Snikket release insists on certbot's
`archive/` symlinks, the fallback is an initContainer that builds that layout.

## Authentication (lldap)

Members of the `xmpp` or `admin` group in lldap log in with their directory password: no invite
link, no second password. The username is the JID localpart (`admin` is `admin@xmpp.example.com`).
Prosody creates rosters and archives lazily on first login, keyed by JID, so a user exists once
they are in lldap. Admin accounts need nothing on the Snikket side either: `admin` members
become `prosody:admin` at their next login. Pieces: `ldap.cfg.lua` (prosody config, the reasoning
inline), the module below, `ldap-secret.yaml` (bind password, `ENV_LDAP_BIND_PASSWORD`).

- **The module** is the image's own `mod_auth_ldap` plus two additions: `provider.users()` (the
  portal's user list iterates `nil` and returns HTTP 500 without it) and an
  `authentication-success` hook mapping the `admin` group to the admin role. It is not copied
  here: `artifacts/ldap-module.yaml` builds it, patching the image's copy at build time, and it
  is mounted as the directory `/var/lib/prosody/custom_plugins/mod_auth_lldap/`, which prosody's
  loader searches (image volumes take no file subPath; under `/etc/prosody` the entrypoint's
  `chown -R` would hit the read-only mount). It is named `lldap` because `ldap` is the stock module. Bumping the server image bumps
  the build's `FROM` with it (`scripts/validate.sh` checks); a patch that no longer applies fails
  the build, which holds the component and leaves the pod on the last good module. lualdap, which
  the image lacks, is `artifacts/lualdap.yaml`.
- **PLAIN is enabled** (`ldap.cfg.lua`), the one concession: bind mode cannot offer SCRAM, and
  with PLAIN off no mechanism is advertised. Alternatives are dead ends: `getpasswd` mode and
  `mod_auth_ldap2` read `userPassword` back, which lldap (OPAQUE) cannot return, and syncing
  hashes needs the same. The server now sees the password at login; nothing becomes sniffable
  (`c2s_require_encryption`, `tls_profile = "modern"`), and s2s and the web keep their settings.
- **Revocation is not immediate.** Group changes apply at the next authentication: someone
  removed from `admin` stays admin, and anyone removed from `xmpp` stays connected, until they
  log in again. To cut access now, remove the group and end their sessions
  (`prosodyctl shell user clients <jid>`).
- **Password changes** from a client work only if the bind account is in lldap's
  `lldap_password_manager` group (the only thing lldap accepts for `userPassword`); in
  `lldap_strict_readonly` they work only in lldap's UI. The manager group can rewrite any
  password, which is the trade-off taken. It also grants search; with neither group lldap shows
  the account only itself, and the symptom is "wrong password".
- **Invites:** `invites_register`, `invites_register_api` and `register` are disabled, because
  an invite would be created fine and fail on redemption. Circles and contact invites stay.
- **Accounts and rollback:** the internal password hash is neither read nor written while LDAP
  auth is active, so reverting restores the old behaviour with nothing to migrate. Data is keyed
  by `username@host`, so an lldap uid equal to the old localpart serves the same history.
- **lldap setup** is manual: bind account `uid=snikket,ou=people,dc=example,dc=com`, groups `xmpp`
  and `admin` under `ou=groups`.

Check the effective value first, because a lost override is silent:
`prosodyctl shell config get xmpp.example.com authentication` must print `lldap`. Then: a member logs
in; a valid lldap user in neither group is rejected; the portal's user list renders; a password
change from a client works.

## Router and edge

Router: forward TCP `5222`, `5269` and `5000` to the Traefik VIP `10.20.176.210` without TLS
termination. Without 5222, clients outside the LAN cannot connect (there is no split-horizon
DNS). Without 5269 there is no inbound s2s, so `push.snikket.net` cannot authenticate by dialback
and **push notifications never work**: a backgrounded phone is never woken, which looks like calls
not ringing and late messages. From the LAN, `timeout 5 bash -c "(echo > /dev/tcp/203.0.113.10/$p)"`
for 5222, 5269 and 443 (the control) checks them; push works when the log shows
`Push notifications enabled for <jid>` and `s2s:show()` lists `push.snikket.net` both ways.

The edge certificate must cover `*.xmpp.example.com`, not just `*.example.com`, or the edge falls back to
its self-signed default and clients fail TLS verification before sending a request, which looks
like a connection error. The alternative is SNI passthrough of the three hosts to `10.20.176.210:443`,
which needs no edge certificate. DNS needs nothing: all three resolve through `*.example.com`.

## Notes

- **`SNIKKET_DOMAIN` is permanent**: it is written into prosody's store on first boot and has no
  rename path.
- **`SNIKKET_TWEAK_IPV6=0`**: the cluster is IPv4-only while pods keep a link-local IPv6.
- **No liveness probe on the server**: first boot generates state, and a restart part-way
  through leaves the store half-written. Readiness already pulls it from the Service.
- **Federation is on**, so 5269 is reachable from the internet by design. Check it end to end at
  `https://observe.jabber.network/xmpp.example.com` (s2s, certificates and SRV records).
