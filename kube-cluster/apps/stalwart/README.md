# stalwart

Mail-server stack. Four parts in the `stalwart` namespace:

- **stalwart** — the mail server (SMTP/IMAP/JMAP). HTTP/admin/JMAP on `:8080` behind Traefik at
  `mail.example.com`; raw mail ports (SMTP 25, SMTPS 465, IMAPS 993, ManageSieve 4190) on a **dedicated
  MetalLB VIP `10.20.176.66`** (`stalwart-lb`, `externalTrafficPolicy: Local` → real client source IPs).
- **bulwark** — webmail at `webmail.example.com` (JMAP client; OIDC login via Stalwart).
- **umleiter** — one-way Gmail→Stalwart IMAP mirror (background).
- **DB** — folded into the shared CNPG cluster (`stalwart` role/db in the `postgres` ns).

## DB-centric
Stalwart keeps **all config *and* all mail in Postgres**. The only on-disk file is the bootstrap
`/etc/stalwart/config.json` (the Postgres store connection — `secret/stalwart`, host pointed at
`postgres-rw.postgres.svc.cluster.local`,). The data dir is an `emptyDir`. So
everything (domains, DKIM, listeners, OIDC) lives in that DB.

## TLS
Stalwart terminates its own TLS on the mail ports. It reuses the **`*.example.com` wildcard**: the cert-manager
`traefik/lhns-de` secret is Reflector-mirrored into this ns (`infra/traefik/tls.yaml`) and mounted at
`/etc/stalwart/certs`. The default certificate must point Stalwart's default certificate at
`/etc/stalwart/certs/{tls.crt,tls.key}` and ACME must be disabled** (admin UI or a local config
override), set in the DB, not here; otherwise it keeps trying to ACME `mail.example.com` behind Traefik.

## PROXY protocol — must be OFF
The smtp/smtps/imaps/sieve listeners (set in the DB, not here) must not expect PROXY protocol. MetalLB `externalTrafficPolicy: Local`
delivers the real source IP with **no** PROXY header, so a listener that expects it
(admin UI / config) makes every mail connection fails parsing a missing header.

## Storage
- Stalwart: none on disk (DB + emptyDir).
- Bulwark: `ceph-rbd` `bulwark-data` (2Gi) → `/app/data` (small state).
- Umleiter: `ceph-rbd` `umleiter-state` (2Gi) → `/state` (SQLite `umleiter.db` — **RBD not
  CephFS**, per the syncthing SQLite-fsync lesson). Config (`umleiter.yaml`, IMAP creds) from `secret/umleiter`.

## Verify
- CNPG `stalwart` db row counts match source; admin UI at `mail.example.com` loads.
- `openssl s_client -connect 10.20.176.66:{465,993}` shows a valid `mail.example.com` cert.
- Test SMTP in/out round-trips; Stalwart logs show the **real** client IP; DKIM signs outbound.
- `webmail.example.com` → Bulwark → OIDC login → mailbox loads.
- Umleiter resumes mirroring from its copied state (Message-ID dedup → no duplicates).
