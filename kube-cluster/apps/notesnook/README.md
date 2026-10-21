# notesnook

Self-hosted [Notesnook](https://notesnook.com) sync stack: web client at `notes.example.com`, sync API
`notes-sync.example.com`, accounts `notes-auth.example.com`, realtime events `notes-events.example.com`
(`routing.yaml`). Notes are end-to-end encrypted; the keys are derived in the client.

## Shape
- **notesnook-db**: MongoDB as a one-member replica set `rs0` (Notesnook needs multi-document
  transactions), StatefulSet on a `ceph-rbd` PVC. The readiness probe initiates the set on first run.
- **identity-server**: accounts, login, JWT, 2FA; sends mail through the `msmtpd` relay
  (`notesnook` is in its vnet).
- **notesnook-server**: the sync API; stores encrypted notes and mints presigned S3 URLs for
  attachments.
- **sse-server**: realtime push (Server-Sent Events).
- **notesnook-web**: the web SPA; server URLs come from `NN_*` env vars.
- **kube-vnet**: one same-namespace VirtualNetwork per internal edge (`vnet.yaml`); Traefik reaches
  the fronted services via `net.traefik.traefik`.

## Decisions
- **MongoDB has no authentication.** The upstream compose has none, and kube-vnet restricts ingress
  to identity-server and notesnook-server.
- **MongoDB stays on 8.x.** Notesnook's C# drivers do not support server 9; see
  [ADR 0051](../../../docs/adr/0051-notesnook-mongodb-held-at-8.md).
- **Attachments go to S3**: bucket `notes-attachments` on the Ceph RGW (`s3.example.com`, path-style, SigV4).
  Presigned URLs are public, so clients upload and download directly.
- **The web client is a community build** (`heyfworld/notesnook-web`, built from a personal fork
  of upstream `apps/web`; only `:latest` is published, so the digest is pinned). The SPA handles
  the encryption keys, so this build is trusted with every note. It was chosen because it reads
  the newer `plan` field and the runtime `NN_*` server URLs. Moving to an upstream build is open.
