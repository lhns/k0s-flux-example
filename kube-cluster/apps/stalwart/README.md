# stalwart

Mail-server stack — migrated from Docker Swarm. Four parts in the `stalwart` namespace:

- **stalwart** — the mail server (SMTP/IMAP/JMAP). HTTP/admin/JMAP on `:8080` behind Traefik at
  `mail.example.com`; raw mail ports (SMTP 25, SMTPS 465, IMAPS 993, ManageSieve 4190) on a **dedicated
  MetalLB VIP `10.20.2.8`** (`stalwart-lb`, `externalTrafficPolicy: Local` → real client source IPs).
- **bulwark** — webmail at `webmail.example.com` (JMAP client; OIDC login via Stalwart).
- **umleiter** — one-way Gmail→Stalwart IMAP mirror (background).
- **DB** — folded into the shared CNPG cluster (`stalwart` role/db in the `postgres` ns).

## DB-centric
Stalwart keeps **all config *and* all mail in Postgres**. The only on-disk file is the bootstrap
`/etc/stalwart/config.json` (the Postgres store connection — `secret/stalwart`, host pointed at
`postgres-rw.postgres.svc.cluster.local`, password reused from Swarm). The data dir is an `emptyDir`. So
migration = a `pg_dump`/restore of the 1.5G DB; everything (domains, DKIM, listeners, OIDC) comes with it.

## TLS
Stalwart terminates its own TLS on the mail ports. It reuses the **`*.example.com` wildcard**: the cert-manager
`traefik/lhns-de` secret is Reflector-mirrored into this ns (`infra/traefik/tls.yaml`) and mounted at
`/etc/stalwart/certs`. **Post-cutover you must point Stalwart's default certificate at
`/etc/stalwart/certs/{tls.crt,tls.key}` and disable its ACME** (via the admin UI or a local config
override) — otherwise it keeps trying to ACME `mail.example.com` behind Traefik.

## PROXY protocol — must be turned OFF
On Swarm, Traefik sent PROXY protocol to Stalwart (`serversTransport=proxyprotocol@file`), so the
smtp/smtps/imaps/sieve listeners are configured to **expect** it. MetalLB `externalTrafficPolicy: Local`
delivers the real source IP with **no** PROXY header, so **proxy-protocol must be disabled on those
listeners** (admin UI / config) or every mail connection fails parsing a missing header.

## Storage
- Stalwart: none on disk (DB + emptyDir).
- Bulwark: `ceph-rbd` `bulwark-data` (2Gi) → `/app/data` (small state, copied).
- Umleiter: `ceph-rbd` `umleiter-state` (2Gi) → `/state` (SQLite `umleiter.db`, copied — **RBD not
  CephFS**, per the syncthing SQLite-fsync lesson). Config (`umleiter.yaml`, IMAP creds) from `secret/umleiter`.

## Cutover
Ships all three Deployments at `replicas: 0`.

1. Confirm applied: ns; CNPG `stalwart` role/db; `stalwart-db` + `lhns-de` reflected into `stalwart`;
   `stalwart-lb` = `10.20.2.8`; RBD PVCs bound.
2. **Stop the Swarm stalwart stack.**
3. **DB**: `pg_dump -h 10.20.2.10 -p 5436 -U stalwart stalwart` (pw `Mexo5mJdhoIvCxHS`) → restore into
   `postgres-rw…/stalwart` (`--no-owner --no-acl`, ~1.5G).
4. **Copy** Bulwark `/app/data` + Umleiter `umleiter.db` into their PVCs (tar-pipe from the swarm node's
   kernel CephFS mount → RBD; chown to each image's uid).
5. Scale **stalwart** to 1 → loads config+mail from the DB. Fix TLS (mount the wildcard, disable ACME) +
   disable listener proxy-protocol; verify with `openssl s_client -connect 10.20.2.8:993`. Then scale
   **bulwark** + **umleiter**.
6. **DNS**: `mail.example.com` + `webmail.example.com` (+ `.kube`) → Traefik `10.20.2.15`. **Router: forward
   25/465/993/4190 → `10.20.2.8`** (MX/PTR/public-IP unchanged).

## Verify
- CNPG `stalwart` db row counts match source; admin UI at `mail.example.com` loads.
- `openssl s_client -connect 10.20.2.8:{465,993}` shows a valid `mail.example.com` cert.
- Test SMTP in/out round-trips; Stalwart logs show the **real** client IP; DKIM signs outbound.
- `webmail.example.com` → Bulwark → OIDC login → mailbox loads.
- Umleiter resumes mirroring from its copied state (Message-ID dedup → no duplicates).
