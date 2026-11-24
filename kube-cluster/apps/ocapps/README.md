# ocapps

[ocapps](https://github.com/gnacho/ocapps), the backend of the Notes and ocphotos web extensions
in [`apps/opencloud`](../opencloud/README.md). One Go binary on :8096, routed on
`opencloud.example.com`.

## Shape
- **Modules**: Notes and photos. News is off (`OCAPPS_NEWS_ENABLED=false`) and not routed.
- **Image**: built from the git tag by oci-composer (`artifacts/ocapps.yaml`); upstream publishes
  none.
- **Auth**: every request carries the user's own OpenCloud token, which ocapps checks against
  `https://opencloud.example.com/graph/v1.0/me` and scopes its data by. There is no service account.
  Photo files stay in OpenCloud and are read with that token.
- **State**: `ocapps-data` (ceph-rbd, RWO) at `/var/lib/ocapps`: SQLite for notes and the photo
  index, note attachments, thumbnails, and secrets ocapps generates itself. **Notes exist only
  here**, not in OpenCloud's files.
- **Routes**: `/index.php/apps/notes/` and `/ocphotos-api/` on `opencloud.example.com`, through the
  `ocapps-headers` middleware.
- **kube-vnet**: `net.traefik.traefik: ingress`. It reaches OpenCloud through Traefik's VIP
  (`hostAliases`).

## Decisions
- **Its own component, not part of `apps/opencloud`**, so a failing build does not hold OpenCloud
  back and the third-party server stays out of the namespace that holds OpenCloud's Secret. Only
  the extension zips are composed in `apps/opencloud`, as the OpenCloud pod mounts them.
- **Same-origin path routes.** The extensions call these paths on OpenCloud's origin, so ocapps
  cannot have its own host. In OpenCloud 9.1 those paths are only the web UI or a 404.
- **No `/ocs/` route.** Upstream sends `/ocs/v2.php/cloud/*` to ocapps, which would put it in
  every sync client's path. Only the Nextcloud Notes mobile apps need it; the web extensions do
  not. So no mobile clients, app tokens or OpenCloud `auth-app`.
- **The headers middleware ships with the route.** ocapps serves note attachments, proxied note
  images (SVG included) and photo originals on OpenCloud's origin, with no CSP or `nosniff` of its
  own. The `sandbox` CSP stops any of them running as a document there.
- **No admin panel and no background scans.** `OCAPPS_ADMIN_TOKEN` would enable an extension
  installer. `OCAPPS_PHOTOS_USERS` would keep app tokens in env; photos are indexed when a user
  opens the app.
- **A one-replica StatefulSet on RBD**, per ADR
  [0007](../../../docs/adr/0007-ceph-csi-storage-classes-by-access-pattern.md): live SQLite.
- **Egress to the internet only, plus Traefik.** The notes image proxy fetches URLs from note
  content, and Places sends photo coordinates to public `nominatim.openstreetmap.org`
  (hardcoded; accepted). The policy keeps it off the LAN, Ceph and cluster services.
- **Built with pinned bases, not upstream's Dockerfile** (ADR 0027): upstream's `FROM`s float.
  The version ldflags are dropped, as the context has no `.git`, so `/healthz` reports `dev`.

## Upgrading
ocapps and the two extensions have no compatibility matrix: bump them together, to releases from
the same day (Renovate groups them as `ocapps`). Re-diff upstream's `Dockerfile` against
`artifacts/ocapps.yaml`, and the golang tag must satisfy `go.mod`.
