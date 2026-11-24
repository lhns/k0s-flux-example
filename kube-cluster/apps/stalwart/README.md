# stalwart

Mail-server stack. Four parts in the `stalwart` namespace:

- **stalwart** — the mail server (SMTP/IMAP/JMAP). HTTP/admin/JMAP on `:8080` behind Traefik at
  `mail.example.com`; raw mail ports (SMTP 25, SMTPS 465, IMAPS 993, ManageSieve 4190) on a **dedicated
  MetalLB VIP `10.20.176.66`** (`stalwart-lb`, `externalTrafficPolicy: Local` → real client source IPs).
- **bulwark** — webmail at `webmail.example.com` (JMAP client; OIDC login via Stalwart). Stalwart 1.0
  accepts only registered OAuth clients: `bulwark` (secret from `secret/bulwark`, one redirect URI
  per Bulwark locale) is registered in Stalwart's database, not here. Do not use Bulwark's
  "Auto-configure OAuth": it saves a second client in `/app/data` that overrides these env vars.
- **umleiter** — one-way Gmail→Stalwart IMAP mirror (background).
- **DB** — in the shared CNPG cluster: role `stalwart`, database `stalwart-v1` (`postgres` ns).

## Data
- **Postgres** (shared CNPG, database `stalwart-v1`): every setting (domains, DKIM, listeners, OIDC,
  OAuth clients) and all mail metadata. The only on-disk file is the bootstrap
  `/etc/stalwart/config.json` (the store connection, `secret/stalwart-v1`); the data dir is an
  `emptyDir`.
- **Mail bodies** are not in Postgres: they are blobs in the RGW bucket `stalwart` (`s3.example.com`),
  configured in the registry. Neither barman nor Velero backs that bucket up.

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
- Stalwart: none on disk (DB, RGW bucket, emptyDir).
- Bulwark: `ceph-rbd` `bulwark-data` (2Gi) → `/app/data` (small state).
- Umleiter: `ceph-rbd` `umleiter-state` (2Gi) → `/state` (SQLite `umleiter.db` — **RBD not
  CephFS**, per the syncthing SQLite-fsync lesson). Config (`umleiter.yaml`, IMAP creds) from `secret/umleiter`.

## Verify
- CNPG `stalwart` db row counts match source; admin UI at `mail.example.com` loads.
- `openssl s_client -connect 10.20.176.66:{465,993}` shows a valid `mail.example.com` cert.
- Test SMTP in/out round-trips; Stalwart logs show the **real** client IP; DKIM signs outbound.
- `webmail.example.com` → Bulwark → OIDC login → mailbox loads.
- Umleiter resumes mirroring from its copied state (Message-ID dedup → no duplicates).

## Stalwart 1.0 migration
Done: 1.0 runs on `stalwart-v1`, copied from the 0.16 database by `stalwart-migrate`, with the same
bucket. The rollback kit is kept until 0.16 is retired deliberately:

- database `stalwart` (0.16, never written by the migration) and its config `secret/stalwart`
  (`stalwart-config.json`);
- PVC `stalwart-migrate-state`, which `stalwart-migrate rollback` reads;
- PVC `stalwart-blobs-premigrate`, a copy of the bucket from before the cutover.

Rollback is `stalwart-migrate rollback` into another new database, never repointing 0.16 at the old
one: v1 deletes blobs it no longer references (restore them from `stalwart-blobs-premigrate`).
Retiring 0.16 means removing all four together; mind the AGENTS.md rules on PVCs and PVs.
Renovate holds Stalwart majors on the dependency dashboard because each one may need this again.
