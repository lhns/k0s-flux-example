# opencloud

[OpenCloud](https://opencloud.eu) (file sync and share, the ownCloud Infinite Scale fork) at
`opencloud.example.com`, with [Euro-Office](https://github.com/Euro-Office/DocumentServer) (the
ONLYOFFICE Document Server fork) as its web office at `euro-office.example.com`. Laid out after
upstream's [opencloud-compose](https://github.com/opencloud-eu/opencloud-compose) Euro-Office module.

## Shape
- **OpenCloud** (`opencloudeu/opencloud`, the stable channel), one process running all services,
  on :9200 behind Traefik (`PROXY_TLS=false`).
- **Config**: `/etc/opencloud` is the read-only ConfigMap from `config/`: `opencloud.yaml`,
  `csp.yaml` and `proxy.yaml`. The secrets are env from the SOPS Secret `opencloud`; everything
  else is env in the StatefulSet.
- **Storage**:
  - `opencloud-data` (ceph-rbd, RWO) at `/var/lib/opencloud`: IDM (BoltDB), NATS JetStream, search
    index, system metadata, thumbnails.
  - `opencloud-files` (cephfs-appdata, RWX) at `/var/lib/opencloud-files`: the users' files. The
    `posix` driver keeps them as plain files with metadata in xattrs.
- **Login**: OIDC against Authelia; OpenCloud's own IdP is off. Four public PKCE clients
  (`opencloud`, `-desktop`, `-android`, `-ios`) and the `opencloud` authorization policy are in
  `apps/authelia/configuration.yml`. Accounts are created on first login. The role comes from the
  `groups` claim (`config/proxy.yaml`): `admin` → Admin, `opencloud` → User. Anyone else is refused.
- **Office**: the collaboration (WOPI) service runs inside OpenCloud. Euro-Office calls
  `https://opencloud.example.com/wopi`, and OpenCloud fetches `https://euro-office.example.com/hosting/discovery`.
  Both go out to the public address and back in through Traefik, as `*.example.com` resolves there in
  the cluster too. So the pods need no vnet between them. If Euro-Office is down, OpenCloud still
  runs and retries discovery on an interval.
- **Euro-Office** (`ghcr.io/euro-office/documentserver`): the all-in-one image, with its own
  Postgres, Redis and RabbitMQ for in-flight editing state, on the container filesystem. It has no
  volume. Its `JWT_SECRET` (SOPS `euro-office`) guards the document server's own API.
- **Mail**: notifications through `msmtpd` as `admin+opencloud@example.com`.
- **Web apps**: draw.io, Excalidraw, Apollon, JSON Viewer and Unzip from
  `opencloudeu/web-extensions`, and JankariTech's Presentation Viewer, one image volume each at
  `/web/apps/<app id>` (`WEB_ASSET_APPS_PATH`). Notes and ocphotos are composed from their
  release zips (`artifacts/`); their backend is [`apps/ocapps`](../ocapps/README.md). Their
  settings are `config/apps.yaml`.
- **draw.io editor** (`jgraph/drawio`) at `drawio.example.com`, stateless; the draw-io app embeds it.
- **Yjs** (`opencloudeu/yjs`) at `opencloud.example.com/yjs`: relays live edits between the users of
  one file in the text editor, Excalidraw and Apollon, and stores nothing. Collaborative editing is
  on through `WEB_OPTION_YJS_SERVER_URL`.
- **kube-vnet**: every pod `net.traefik.traefik: ingress`; OpenCloud `net.msmtpd.msmtpd: egress`.

## Decisions
- **Production channel, not rolling.** Upstream's compose follows `opencloud-rolling`. The
  production line (9.1) has everything this setup uses (`/wopi` on the proxy, the in-process
  collaboration service). Upstream announced that production releases need a subscription from
  April 2027; revisit this before then.
- **Major upgrades follow upstream's guide for the target line:** `opencloud init --diff --insecure no`
  with the new image, offline, against a copy of `config/opencloud.yaml`. The patch also lists the
  secrets the Secret already holds, empty or newly generated: drop those lines. A new id goes into
  `config/opencloud.yaml`, a new secret into the Secret and the StatefulSet's env, under its
  documented env var. Then whatever the guide runs after the start
  (9.1: `opencloud search index --all-spaces --force-rescan`).
- **Plain manifests, no chart.** The community chart (`opencloud-eu/helm`) is archived.
- **`opencloud init` ran once; its output is split.** The ids baked into the data (storage mount
  id, system user, Graph app, service account) are in `config/opencloud.yaml`, so the config is
  reviewable. The secrets (JWT, transfer, URL-signing and WOPI secrets, machine and system-user
  keys, the service-account secret, the IDM service passwords) are env from the Secret
  `opencloud`, under OpenCloud's documented env vars. **Never re-run `init` against live data.**
  New ids orphan the existing spaces. The passwords contain `$`, so the Secret opts out of Flux
  substitution, which `artifacts/` turns on for the component.
- **Login follows Authelia's OpenCloud guide, with two-factor required.** The desktop and mobile
  apps learn their client id and scopes, `groups` included, from OpenCloud's webfinger. Older app
  versions ignore those, get no `groups` claim, and are refused. The browser client asks without
  `offline_access`, because in Authelia that scope always forces the consent screen; it renews its
  token through a silent iframe on the Authelia session. The native apps keep refresh tokens, so
  they see consent once per sign-in. Every signed-in sync client refreshes its token through the
  day, and all of Authelia's clients share one token bucket (authelia README, "Rate limits"). If
  that bucket runs short, the three native clients are the ones to drop.
- **A one-replica StatefulSet**, per ADR
  [0007](../../../docs/adr/0007-ceph-csi-storage-classes-by-access-pattern.md): BoltDB and
  JetStream must never have two writers. After a node loss the pod waits for the owner:
  [operations](../../../docs/operations.md#a-node-is-lost-statefulset-pods-stay-terminating).
- **The files on the HDD filesystem**, on a separate volume from the embedded stores: bulk data,
  per ADR 0007. The volume is 200Gi to start; that is the CephFS quota, and it can be raised.
  Moving it later means a new PVC and a copy (AGENTS.md).
- **Public, like nextcloud and immich: no forwardAuth and no Anubis.** Sync clients, WebDAV, share
  links and the WOPI callbacks cannot pass a browser challenge.
- **Euro-Office is public too, and is new surface.** The browser loads the editor from it, so it
  cannot be internal. Its admin panel and example app are off (the image's defaults), and it
  will not fetch from private addresses (`ALLOW_PRIVATE_IP_ADDRESS=false`), so it cannot be
  pointed at the cluster. Accepted risk: anyone can still use it as an editor for some other
  public WOPI host.
- **Euro-Office proof keys are off** (`COLLABORATION_APP_PROOF_DISABLE`), as upstream's module has
  it. The image makes new keys on every start, because it has no volume.
- **Converter workers capped** (`NODE_CONFIG` in `euro-office.yaml`), for memory.
- **Euro-Office rolls with `RollingUpdate`** (ha-surge covers it). Each pod keeps its editing
  state to itself, so a document opened by two users while both pods run can land on different
  servers. The WOPI lock refuses the second one.
- **Only Euro-Office.** Upstream's app registry sends ODF to Collabora. With one office app
  registered, OpenCloud's default registry opens every office format in Euro-Office, so no
  `app-registry.yaml` is needed.
- **Web extensions are image volumes; only zip-only ones are composed.** Where upstream
  publishes an image, it is mounted as is
  ([ADR 0032](../../../docs/adr/0032-files-come-from-images-never-fetched.md)). Presentation
  Viewer comes from its vendor's image, which keeps the app under `/app/<id>`. Notes and ocphotos
  ship only as zips, so they are composed in `artifacts/`. That substitutes the whole component:
  every Secret opts out (`kustomize.toolkit.fluxcd.io/substitute: disabled`) and nothing else may
  hold a bare `${`. A failing composition holds back the component, not the running pod. Before a
  bump, check `minOpenCloud`/`maxOpenCloud` in awesome-apps' `webApps/apps.json`.
- **draw.io self-hosted, not `embed.diagrams.net`**, so no third party sees who opens diagrams.
  Public like Euro-Office, as browsers and share-link guests load it; it holds no data, `/proxy` is
  off (the image default) and there is no export server, so server-side PNG/PDF export is hidden.
  Not collaborative: a second save of the same file is refused, not merged.
- **Yjs through Traefik, not OpenCloud's proxy as upstream has it.** Same origin, so no CSP entry,
  and no vnet between the pods. Yjs checks each connection's token against Graph itself, so nothing
  in front authenticates and no secret is needed.
- **One Yjs pod, `Recreate`.** Rooms live in memory, and two pods would split a file's editors
  without either noticing. So a restart or drain drops live sessions, and since Yjs never writes the
  file, edits since the last autosave are lost if every editor closes.

## First deploy (owner)
1. In lldap, create the group `opencloud` and add the users. `admin` members get the Admin role.
2. Merge. Euro-Office's first start generates fonts and takes a few minutes.
3. Log in at `https://opencloud.example.com`. Authelia asks for consent once (remember it). Then open
   a `.docx`.

## Notes
- No Microsoft core fonts: upstream mounts the host's TrueType fonts, which this cluster does
  not have. Documents using Calibri and the like render with substitutes. Adding them means an
  image volume, composed by `infra/oci-composer` (README, "Getting files into a workload").
